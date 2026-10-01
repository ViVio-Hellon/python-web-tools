"""簡易在庫(pallet_service)のユニットテスト。

pytestではなく標準ライブラリの`unittest`を使う
(本ツールは追加pip installなしで動かす方針のため、テストも同方針に揃える)。

VBAの数値テーブル(CalcDakeMinLocal等)やRunUpdatePalletAllの優先順位、
受入/払出の在庫増減・在庫0時の扱いなど、業務ルールの核心部分を検証する。

実行方法:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import db, pallet_service as svc, pallet_threshold


def insert_pallet(conn, *, width, length, industry="一般", symbol="", position="A-01",
                   qty=1, list_mgmt="", unit="台", note="", keta=0, ashi=0):
    """試験用の1行。`keta`/`ashi` の既定 0 は「まだ入っていない」の意味。"""
    now = db.now_db_string()
    conn.execute(
        "INSERT INTO PalletMaster "
        "(幅, 丈, 巾適合min, 巾適合max, 丈適合min, 丈適合max, 業界, 記号, 位置, "
        " 在庫数, リスト管理, 桁数, 脚数, コード, 単位, 備考, 更新日時) "
        "VALUES (?, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
        (width, length, industry, symbol, position, qty, list_mgmt,
         keta, ashi, unit, note, now),
    )
    conn.commit()


class PalletServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        # 閾値はマスタの表から読む(初期値=社内基準表が入った状態)
        self.th = pallet_threshold.load(self.conn)

    def tearDown(self) -> None:
        self.conn.close()


# ------------------------------------------------------------------
# 段階判定表 (数値は社内基準表準拠、変更してはいけない)
# ------------------------------------------------------------------
class CalcTableTests(unittest.TestCase):
    """基準表の数値そのもの。**いまはマスタの表から読む。**

    以前はこの数値が `pallet_service` にPythonの表として書かれていて、
    ここではその関数を直に呼んでいた。数値をDB(`PalletDakeThreshold`
    ほか)へ出したので、テストも**出したあとの表を読んで**確かめる
    ── 表に出したことで値が変わっていないことは、ここが担保する。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        db.apply_schema(cls.conn)          # 初期値もここで入る
        cls.th = pallet_threshold.load(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def test_calc_dake_min_boundaries(self):
        self.assertEqual(self.th.dake_min(600), 491)
        self.assertEqual(self.th.dake_min(601), 591)
        # 200以下まで刻む(旧版は600以下が一括で410だった)
        self.assertEqual(self.th.dake_min(200), 110)
        self.assertEqual(self.th.dake_min(300), 191)
        self.assertEqual(self.th.dake_min(700), 591)
        self.assertEqual(self.th.dake_min(6100), 5591)
        # 基準表に無い6100超も現物に合わせて2段延長してある
        self.assertEqual(self.th.dake_min(6600), 6091)
        self.assertEqual(self.th.dake_min(7100), 6591)
        self.assertEqual(self.th.dake_min(9999), 6591)  # 7100超は頭打ち

    def test_calc_dake_max_pairs_with_min(self):
        self.assertEqual(self.th.dake_max(600), 590)
        self.assertEqual(self.th.dake_max(601), 690)
        self.assertEqual(self.th.dake_max(6100), 6090)
        self.assertEqual(self.th.dake_max(6600), 6590)
        self.assertEqual(self.th.dake_max(7100), 7090)
        self.assertEqual(self.th.dake_max(9999), 99999)  # 7100超は上限なし

    def test_calc_haba_max_pairs_with_min(self):
        self.assertEqual(self.th.haba_max(150), 140)
        self.assertEqual(self.th.haba_max(400), 390)
        self.assertEqual(self.th.haba_max(1850), 1840)
        self.assertEqual(self.th.haba_max(2050), 2040)
        self.assertEqual(self.th.haba_max(9999), 99999)  # 2050超は上限なし

    def test_fit_range_can_never_invert(self):
        """適合範囲の min > max は「何を検索してもヒットしない行」を生む。

        旧実装は max を「寸法-10」で求めていたため、寸法が小さい行で
        負の値になり逆転していた(実データに70件存在した)。
        全域で min <= max を保つことを固定する。
        """
        for value in range(0, 7000):
            self.assertLessEqual(
                self.th.dake_min(value), self.th.dake_max(value),
                f"丈={value} で適合範囲が逆転した")
            self.assertLessEqual(
                self.th.haba_min(value), self.th.haba_max(value),
                f"幅={value} で適合範囲が逆転した")

    def test_tiny_width_no_longer_produces_negative_max(self):
        # 実データにあった 幅2.5mm の行。旧実装では 300〜-8 になっていた
        self.assertEqual((self.th.haba_min(2), self.th.haba_max(2)), (50, 140))

    def test_calc_haba_min_boundaries(self):
        # 301〜400 は50mm刻みの2本(VER2.52.1で割った。それまでは
        # 1本で、適合最小値だけが入力最小値を下回っていた)
        self.assertEqual(self.th.haba_min(350), 291)
        self.assertEqual(self.th.haba_min(400), 341)
        self.assertEqual(self.th.haba_min(401), 391)
        # 400以下も刻む(旧版は400以下が一括で300だった)
        self.assertEqual(self.th.haba_min(150), 50)
        self.assertEqual(self.th.haba_min(300), 241)
        # 1050超も50mm刻み。1100は「1051〜1100」の帯で1041
        self.assertEqual(self.th.haba_min(1100), 1041)
        self.assertEqual(self.th.haba_min(1101), 1091)
        self.assertEqual(self.th.haba_min(1850), 1791)
        self.assertEqual(self.th.haba_min(2050), 1991)
        self.assertEqual(self.th.haba_min(5000), 1991)  # 2050超は頭打ち

    def test_haba_bands_are_50mm_all_the_way_up(self):
        """1050超だけ100mm刻みで、帯が倍の幅を持っていた。

        帯が広い分だけ、そのパレットには載らない小さい製品まで
        適合と判定されていた(幅1100が1050幅と同じ下限を名乗る)。
        全域が50mm刻み = 帯の下限-10 であることを固定する。
        """
        for upper in range(1100, 2051, 50):
            self.assertEqual(self.th.haba_min(upper), upper - 50 - 9,
                             f"幅{upper}の帯の下限がずれている")
            self.assertEqual(self.th.haba_max(upper), upper - 10,
                             f"幅{upper}の帯の上限がずれている")

    def test_calc_ashi_boundaries(self):
        self.assertEqual(self.th.ashi(1550), 2)
        self.assertEqual(self.th.ashi(1551), 3)
        self.assertEqual(self.th.ashi(4600), 6)
        self.assertEqual(self.th.ashi(4601), 7)

    def test_calc_keta_boundaries(self):
        self.assertEqual(self.th.keta(350), 2)
        self.assertEqual(self.th.keta(351), 3)
        self.assertEqual(self.th.keta(1280), 4)
        self.assertEqual(self.th.keta(1281), 5)
        self.assertEqual(self.th.keta(1800), 6)

    def test_桁数は1801以上で7本(self):
        """基準表に無い範囲だが、現物の1900幅パレットの桁が7本だった。

        以前は6で止めていたので、1801以上のパレットの桁数が1本
        少なく入っていた(移植元 `CalcKeta` の注記どおりに直した)。
        """
        self.assertEqual(self.th.keta(1801), 7)
        self.assertEqual(self.th.keta(1900), 7)
        self.assertEqual(self.th.keta(9999), 7)

    def test_normalize_key_fullwidth_and_multiply_sign(self):
        self.assertEqual(svc.normalize_key("ｃ１"), "C1")     # 全角英数字→半角+大文字化
        self.assertEqual(svc.normalize_key(" c1 "), "C1")     # 前後空白除去+大文字化
        self.assertEqual(svc.normalize_key("5X10"), "5×10")
        self.assertEqual(svc.normalize_key("５×１０"), "5×10")

    def test_かけると読める文字はすべて同じ鍵になる(self):
        """**同じ寸法の書き方で結果が割れない。**

        移植元は `Select Case` の評価順の都合で、半角小文字 x と
        全角Ｘ/ｘが「×」ではなく半角大文字 X になっていた。そのため
        `5X10` は固定表に当たるのに `5x10` は当たらず、マスタに
        小文字で書かれた行だけが固定適合を受け取れなかった。
        """
        for text in ("5×10", "5X10", "5x10", "５×１０", "５Ｘ１０", "５ｘ１０",
                     "5 × 10", "5*10", "５＊１０", "5✕10", "5✖10", "5╳10"):
            with self.subTest(text=text):
                self.assertEqual(svc.normalize_key(text), "5×10")

    def test_かけるではないXは変えない(self):
        """**位置を見る。** 数字に挟まれていない X は「かける」ではない。

        実マスタには `EXﾀｲﾄ`(74行)`EX2方向`(26行)があり、そこの X は
        EX受注 の X。無条件に × へ変えると `E×ﾀｲﾄ` になってしまう。
        固定表の鍵は `1×2` `5×10` などすべて数字に挟まれた形なので、
        この規則で取りこぼしは出ない。
        """
        for text in ("EXﾀｲﾄ", "EX2方向", "XY", "X", "AX8"):
            with self.subTest(text=text):
                self.assertNotIn("×", svc.normalize_key(text))
        self.assertEqual(svc.normalize_key("EXﾀｲﾄ"), "EXﾀｲﾄ")
        self.assertEqual(svc.normalize_key("EX2方向"), "EX2方向")

    def test_中の空白も詰める(self):
        self.assertEqual(svc.normalize_key("強度 UP"), "強度UP")
        self.assertEqual(svc.normalize_key("　C　8　"), "C8")

    def test_そろえる必要が無ければそのまま(self):
        for text in ("5×10", "C8", "P1", "強度UP", "一般"):
            with self.subTest(text=text):
                self.assertEqual(svc.normalize_key(text), text)


# ------------------------------------------------------------------
# recompute_fit_ranges (RunUpdatePalletAll) の優先順位
# ------------------------------------------------------------------
class RecomputeFitRangesTests(PalletServiceTestCase):
    def test_combo_priority_first(self):
        insert_pallet(self.conn, width=1470, length=2700, industry="5×10", symbol="強度UP")
        summary = svc.recompute_fit_ranges(self.conn)
        self.assertTrue(summary.ok)
        self.assertEqual(summary.fixed, 1)
        self.assertEqual(summary.calculated, 0)

        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(
            (row["巾適合min"], row["巾適合max"], row["丈適合min"], row["丈適合max"]),
            (1485, 1540, 2700, 3090),
        )

    def test_symbol_sets_keta_ashi(self):
        insert_pallet(self.conn, width=920, length=640, industry="", symbol="C8")
        svc.recompute_fit_ranges(self.conn)
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(
            (row["巾適合min"], row["巾適合max"], row["丈適合min"], row["丈適合max"]),
            (920, 1000, 640, 1080),
        )
        self.assertEqual(row["桁数"], 4)
        self.assertEqual(row["脚数"], 2)

    def test_業界だけで当たった行にも脚数と桁数を補う(self):
        """**範囲は固定表、脚数・桁数は寸法から補う。**

        移植元では、どの道を通った行も最後に同じ後始末(`SkipCalc:`)を
        通り、そこで脚数・桁数を埋めます。以前の移植はこの合流を
        取り違えていて、業界だけで当たった行(1×2 / 4×8 など)は
        **脚数・桁数が空のまま**でした。
        """
        insert_pallet(self.conn, width=1185, length=1800, industry="4×8", symbol="")
        svc.recompute_fit_ranges(self.conn)
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(
            (row["巾適合min"], row["巾適合max"], row["丈適合min"], row["丈適合max"]),
            (1185, 1255, 1800, 2505),
        )
        self.assertEqual(row["脚数"], self.th.ashi(1800))
        self.assertEqual(row["桁数"], self.th.keta(1185))

    def test_複合キーで当たった行にも脚数と桁数を補う(self):
        insert_pallet(self.conn, width=1470, length=2700,
                      industry="5×10", symbol="強度UP")
        svc.recompute_fit_ranges(self.conn)
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(row["脚数"], self.th.ashi(2700))
        self.assertEqual(row["桁数"], self.th.keta(1470))

    def test_calculated_fallback(self):
        insert_pallet(self.conn, width=999, length=999, industry="", symbol="")
        svc.recompute_fit_ranges(self.conn)
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(row["丈適合min"], self.th.dake_min(999))
        self.assertEqual(row["丈適合max"], svc.cap_to_pallet(self.th.dake_max(999), 999))
        self.assertEqual(row["巾適合min"], self.th.haba_min(999))
        self.assertEqual(row["巾適合max"], svc.cap_to_pallet(self.th.haba_max(999), 999))
        self.assertEqual(row["脚数"], self.th.ashi(999))
        self.assertEqual(row["桁数"], self.th.keta(999))


class KetaAshiFillTests(PalletServiceTestCase):
    """脚数・桁数は**補完**であって、上書きではない。

    移植元は `IsEmpty2` で空かどうかを見てから埋めます。現場が手で
    入れた本数を潰さないためです。以前の移植は計算した行で毎回
    上書きしていました。
    """

    def fill(self, **over):
        insert_pallet(self.conn, width=999, length=999, industry="", symbol="",
                      **over)
        svc.recompute_fit_ranges(self.conn)
        return self.conn.execute("SELECT * FROM PalletMaster").fetchone()

    def test_入っている値は残す(self):
        row = self.fill(keta=9, ashi=8)
        self.assertEqual(row["桁数"], 9)
        self.assertEqual(row["脚数"], 8)

    def test_空なら埋める(self):
        row = self.fill(keta=0, ashi=0)
        self.assertEqual(row["桁数"], self.th.keta(999))
        self.assertEqual(row["脚数"], self.th.ashi(999))

    def test_0もNULLも空とみなす(self):
        """取り込み元の欄は0で埋まっていることがある。空欄と意味は同じ。"""
        for value in (None, "", "  ", 0, "0"):
            with self.subTest(value=value):
                self.assertTrue(svc.is_blank(value))
        for value in (1, "3", 7):
            with self.subTest(value=value):
                self.assertFalse(svc.is_blank(value))

    def test_記号で当たった行は表の値で上書きする(self):
        """記号は現物の型そのもの。表のほうが確かな事実なので上書きする。"""
        insert_pallet(self.conn, width=920, length=640, industry="", symbol="C8",
                      keta=9, ashi=8)
        svc.recompute_fit_ranges(self.conn)
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(row["桁数"], 4)
        self.assertEqual(row["脚数"], 2)


class RecomputeCountsTests(PalletServiceTestCase):
    """**何を何件直したか**を項目ごとに数える(移植元の完了メッセージ)。

    「終わりました」だけでは、直ったのか何も起きなかったのかが
    分かりません。
    """

    def test_項目ごとに数える(self):
        # 計算で埋まる行(脚数・桁数は空)
        insert_pallet(self.conn, width=999, length=999, industry="", symbol="")
        # 記号の固定表で当たる行
        insert_pallet(self.conn, width=920, length=640, industry="", symbol="C8")
        got = svc.recompute_fit_ranges(self.conn)

        self.assertEqual(got.total, 2)
        self.assertEqual(got.fixed, 1)
        self.assertEqual(got.calculated, 1)
        self.assertEqual(got.dake_min, 1)
        self.assertEqual(got.dake_max, 1)
        self.assertEqual(got.haba_min, 1)
        self.assertEqual(got.haba_max, 1)
        # 脚数・桁数は計算した行だけ(C8は表の値で入るので補完は0)
        self.assertEqual(got.ashi, 1)
        self.assertEqual(got.keta, 1)

    def test_まとめに内訳が出る(self):
        insert_pallet(self.conn, width=999, length=999, industry="", symbol="")
        text = svc.recompute_fit_ranges(self.conn).summary()
        for name in ("丈適合min更新", "丈適合max更新", "巾適合min補完",
                     "巾適合max補完", "脚数補完", "桁数補完", "固定適合上書き"):
            self.assertIn(name, text)

    def test_失敗したら理由を出す(self):
        got = svc.RecomputeSummary(ok=False, error="読めませんでした")
        self.assertIn("読めませんでした", got.summary())


class CapToPalletTests(PalletServiceTestCase):
    """段階表の上限を現物サイズで頭打ちにする。

    基準表は「製品サイズの帯 → その帯の**標準**パレット」の表なので、
    標準サイズでないパレットにそのまま当てると、自分より大きい製品を
    受けられると名乗ってしまう。実マスタの値(適合max = 現物 - 10)は
    この頭打ちを掛けたものと一致する。
    """

    def test_a_standard_size_keeps_the_table_value(self):
        # 幅1550は「1451〜1550」の帯の標準サイズ。表の上限1540がそのまま残る
        self.assertEqual(svc.cap_to_pallet(self.th.haba_max(1550), 1550), 1540)
        self.assertEqual(svc.cap_to_pallet(self.th.dake_max(3150), 3150), 3140)

    def test_an_odd_size_is_capped_to_itself(self):
        """帯の上限を名乗らせないこと。

        幅の帯が50mm刻みになったので幅方向の名乗り過ぎは最大50mmに
        縮んだが、無くなってはいない(1455のパレットは「1451〜1500」の
        帯なので上限1490をもらう)。丈は帯が広いままなので差も大きい。
        """
        self.assertEqual(self.th.haba_max(1455), 1490)          # 表の値
        self.assertEqual(svc.cap_to_pallet(1490, 1455), 1445)    # 頭打ち後
        self.assertEqual(svc.cap_to_pallet(self.th.dake_max(2970), 2970), 2960)

    def test_the_cap_never_exceeds_the_pallet(self):
        for size in range(100, 6200, 37):
            self.assertLessEqual(
                svc.cap_to_pallet(self.th.haba_max(size), size), size)
            self.assertLessEqual(
                svc.cap_to_pallet(self.th.dake_max(size), size), size)

    def test_the_clearance_matches_the_master(self):
        """実マスタは 適合max = 現物 - 10 で入っている。"""
        self.assertEqual(svc.PALLET_CLEARANCE, 10)


class RecomputeDoesNotOverclaimTests(PalletServiceTestCase):
    def test_no_row_claims_a_product_bigger_than_itself(self):
        for width, length in ((1490, 2970), (1490, 3100), (1540, 3100),
                              (1550, 3100), (999, 999), (1185, 1800)):
            insert_pallet(self.conn, width=width, length=length,
                          industry="", symbol="")
        svc.recompute_fit_ranges(self.conn)
        for row in self.conn.execute("SELECT * FROM PalletMaster"):
            self.assertLessEqual(row["巾適合max"], row["幅"],
                                 f"{row['幅']}x{row['丈']} の巾適合max")
            self.assertLessEqual(row["丈適合max"], row["丈"],
                                 f"{row['幅']}x{row['丈']} の丈適合max")

    def test_the_reported_case_is_fixed(self):
        """1490x2970 のパレットが 1528x3053 の製品を受けないこと。"""
        insert_pallet(self.conn, width=1490, length=2970, industry="", symbol="")
        svc.recompute_fit_ranges(self.conn)
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual((row["巾適合min"], row["巾適合max"]), (1441, 1480))
        self.assertEqual((row["丈適合min"], row["丈適合max"]), (2641, 2960))
        self.assertFalse(row["巾適合min"] <= 1528 <= row["巾適合max"])
        self.assertFalse(row["丈適合min"] <= 3053 <= row["丈適合max"])


# ------------------------------------------------------------------
# 検索
# ------------------------------------------------------------------
class SearchTests(PalletServiceTestCase):
    def test_search_exact_and_range(self):
        insert_pallet(self.conn, width=1470, length=2700, position="A-01")
        insert_pallet(self.conn, width=1490, length=2720, position="A-02")  # 範囲内(±50)
        insert_pallet(self.conn, width=2000, length=3000, position="B-01")  # 範囲外

        exact = svc.search_pallets(self.conn, 1470, 2700, mode="exact")
        self.assertEqual(len(exact.rows), 1)
        self.assertEqual(exact.matched_positions, {"A-01"})

        ranged = svc.search_pallets(self.conn, 1470, 2700, mode="range")
        self.assertEqual(len(ranged.rows), 2)
        self.assertEqual(ranged.matched_positions, {"A-01", "A-02"})


    def test_在庫0の行は検索にも位置の一覧にも出さない(self):
        """払い出して0になった行(リスト管理=要なら行は残る)を一覧に出さない。

        現場の声:「在庫0でリストに載るのは迷惑。そのサイズのパレットが
        存在するのかと、在庫があるのかは別の話」。"""
        insert_pallet(self.conn, width=1470, length=2700, position="A-01", qty=0, list_mgmt="要")
        insert_pallet(self.conn, width=1490, length=2720, position="A-01", qty=3)
        self.assertEqual(svc.search_pallets(self.conn, 1470, 2700, mode="exact").rows, [])
        self.assertEqual(svc.search_pallets(self.conn, 1470, 2700, mode="exact").matched_positions, set())
        ranged = svc.search_pallets(self.conn, 1470, 2700, mode="range")
        self.assertEqual([r["幅"] for r in ranged.rows], [1490])
        self.assertEqual([r["幅"] for r in svc.pallets_at_position(self.conn, "A-01")], [1490])

    def test_払い出して0になったら一覧から消える(self):
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=2, list_mgmt="要")
        self.assertEqual(len(svc.pallets_at_position(self.conn, "P-01")), 1)
        svc.issue(self.conn, width=500, length=440, position="P-01", qty=2)
        self.assertEqual(svc.pallets_at_position(self.conn, "P-01"), [])
        # 受け入れ直せばまた出る(行は残っているので足すだけ)
        svc.receive(self.conn, width=500, length=440, position="P-01", qty=1)
        self.assertEqual(len(svc.pallets_at_position(self.conn, "P-01")), 1)


# ------------------------------------------------------------------
# 受入
# ------------------------------------------------------------------
class ReceiveTests(PalletServiceTestCase):
    def test_new_row_creates_pallet(self):
        result = svc.receive(self.conn, width=500, length=440, qty=5, position="P-01", symbol="P1")
        self.assertTrue(result.ok)
        self.assertEqual(result.new_stock, 5)

        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(row["在庫数"], 5)
        self.assertEqual(row["業界"], "一般")  # 未入力時のデフォルト
        # 受入後は自動で適合範囲が再計算される(P1固定表がヒットする)
        self.assertEqual((row["巾適合min"], row["巾適合max"]), (500, 580))

        history = self.conn.execute("SELECT * FROM パレット入出庫履歴").fetchone()
        self.assertEqual(history["区分"], "受入")
        self.assertEqual(history["数量"], 5)
        self.assertEqual(history["在庫数_更新後"], 5)

    def test_existing_row_adds_stock(self):
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=3)
        result = svc.receive(self.conn, width=500, length=440, qty=2, position="P-01")
        self.assertTrue(result.ok)
        self.assertEqual(result.new_stock, 5)

        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(row["在庫数"], 5)

    def test_rejects_non_positive_qty(self):
        result = svc.receive(self.conn, width=500, length=440, qty=0, position="P-01")
        self.assertFalse(result.ok)
        count = self.conn.execute("SELECT COUNT(*) AS c FROM PalletMaster").fetchone()["c"]
        self.assertEqual(count, 0)


# ------------------------------------------------------------------
# 払出
# ------------------------------------------------------------------
class IssueTests(PalletServiceTestCase):
    def test_reduces_stock(self):
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=5)
        result = svc.issue(self.conn, width=500, length=440, position="P-01", qty=2)
        self.assertTrue(result.ok)
        self.assertEqual(result.new_stock, 3)

        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(row["在庫数"], 3)

        history = self.conn.execute("SELECT * FROM パレット入出庫履歴").fetchone()
        self.assertEqual(history["区分"], "払出")
        self.assertEqual(history["数量"], 2)
        self.assertEqual(history["在庫数_更新後"], 3)

    def test_rejects_more_than_stock(self):
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=2)
        result = svc.issue(self.conn, width=500, length=440, position="P-01", qty=3)
        self.assertFalse(result.ok)

        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(row["在庫数"], 2)  # 変更されていない

    def test_to_zero_deletes_row_by_default(self):
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=2, list_mgmt="")
        result = svc.issue(self.conn, width=500, length=440, position="P-01", qty=2)
        self.assertTrue(result.ok)
        self.assertEqual(result.new_stock, 0)
        count = self.conn.execute("SELECT COUNT(*) AS c FROM PalletMaster").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_to_zero_keeps_row_when_list_kanri_required(self):
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=2, list_mgmt="要")
        result = svc.issue(self.conn, width=500, length=440, position="P-01", qty=2)
        self.assertTrue(result.ok)
        self.assertEqual(result.new_stock, 0)

        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["在庫数"], 0)

    def test_missing_row(self):
        result = svc.issue(self.conn, width=999, length=999, position="X", qty=1)
        self.assertFalse(result.ok)


# ------------------------------------------------------------------
# 楽観ロック
# ------------------------------------------------------------------
class StaleConnection:
    """読んだ行が**古くなっている**状況を作るための包み。

    本物の競合は「読んだ直後に、別の端末が同じ行を更新する」という
    時間の隙で起きる。1プロセスの中ではその隙を作れないので、
    `SELECT` が返す `更新日時` だけを古い値に差し替えて再現する。

    `UPDATE ... WHERE 管理番号=? AND 更新日時=?` が 0行になり、
    「他所が先に動かした」と判定されることを確かめられる。
    """

    def __init__(self, conn, stale_value: str = "1999/01/01 00:00:00") -> None:
        self._conn = conn
        self._stale = stale_value
        self.rewrote = False

    def execute(self, sql, params=()):
        cursor = self._conn.execute(sql, params)
        if sql.lstrip().upper().startswith("SELECT * FROM PALLETMASTER"):
            return _StaleCursor(cursor, self)
        return cursor

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *args):
        return self._conn.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _StaleCursor:
    def __init__(self, cursor, owner) -> None:
        self._cursor = cursor
        self._owner = owner

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        data["更新日時"] = self._owner._stale
        self._owner.rewrote = True
        return data

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class OptimisticLockTests(PalletServiceTestCase):
    """**黙って上書きしない**ことを確かめる。

    DBは共有フォルダに置けるので、2台が同じ行を触ることがある。
    後から書いたほうが勝つと、先に動かした人の登録が消える。
    それは現物と帳簿がずれるということで、この画面でいちばん困る。
    """

    def test_払出は他所が先に動かしたら書かない(self):
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=5)
        stale = StaleConnection(self.conn)

        result = svc.issue(stale, width=500, length=440, position="P-01", qty=2)

        self.assertTrue(stale.rewrote, "試験の仕掛けが効いていません")
        self.assertFalse(result.ok)
        self.assertTrue(result.conflict, "競合として返していません")
        row = self.conn.execute("SELECT 在庫数 FROM PalletMaster").fetchone()
        self.assertEqual(row["在庫数"], 5, "競合したのに在庫が動いています")

    def test_受入も他所が先に動かしたら書かない(self):
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=5)
        stale = StaleConnection(self.conn)

        result = svc.receive(stale, width=500, length=440, position="P-01", qty=3)

        self.assertTrue(result.conflict)
        row = self.conn.execute("SELECT 在庫数 FROM PalletMaster").fetchone()
        self.assertEqual(row["在庫数"], 5)

    def test_競合したら履歴も残さない(self):
        """書けていないのに履歴だけ残ると、経緯が読めなくなる。"""
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=5)
        svc.issue(StaleConnection(self.conn), width=500, length=440,
                  position="P-01", qty=2)
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM パレット入出庫履歴").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_競合の文言は取り直しを促す(self):
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=5)
        result = svc.issue(StaleConnection(self.conn), width=500, length=440,
                           position="P-01", qty=2)
        self.assertIn("他の端末", result.message)
        self.assertIn("やり直して", result.message)

    def test_競合していなければ普通に通る(self):
        """仕掛けが無ければ成功することも見ておく(逆側の確認)。"""
        insert_pallet(self.conn, width=500, length=440, position="P-01", qty=5)
        result = svc.issue(self.conn, width=500, length=440, position="P-01", qty=2)
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()


class LooseMatchTests(PalletServiceTestCase):
    """書き方をそろえて拾ったことを、**黙らずに知らせる**。

    拾えば選定は通りますが、マスタにゆれが残っていること自体は
    直したほうがよい事実です。両方を出します。
    """

    def test_小文字のxでも固定表に当たる(self):
        """以前は `5x10` が当たらず、段階表からの計算に落ちていた。"""
        insert_pallet(self.conn, width=1470, length=2700,
                      industry="5x10", symbol="")
        got = svc.recompute_fit_ranges(self.conn)
        self.assertEqual(got.fixed, 1)
        self.assertEqual(got.calculated, 0)
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(
            (row["巾適合min"], row["巾適合max"], row["丈適合min"], row["丈適合max"]),
            (1470, 1540, 2700, 3090),
        )

    def test_拾ったことを知らせる(self):
        insert_pallet(self.conn, width=1470, length=2700,
                      industry="5x10", symbol="")
        got = svc.recompute_fit_ranges(self.conn)
        self.assertEqual(len(got.loose), 1)
        found = got.loose[0]
        self.assertEqual(found.column, "業界")
        self.assertEqual(found.raw, "5x10")
        self.assertEqual(found.matched, "5×10")
        self.assertIn("5x10", got.summary())
        self.assertIn("5×10", got.summary())

    def test_そろっている行は知らせない(self):
        """全部が「ゆれています」では、本当のゆれが埋もれる。"""
        insert_pallet(self.conn, width=1470, length=2700,
                      industry="5×10", symbol="")
        self.assertEqual(svc.recompute_fit_ranges(self.conn).loose, [])

    def test_記号のゆれも知らせる(self):
        insert_pallet(self.conn, width=920, length=640,
                      industry="", symbol="c8")
        got = svc.recompute_fit_ranges(self.conn)
        self.assertEqual([(m.column, m.raw, m.matched) for m in got.loose],
                         [("記号", "c8", "C8")])

    def test_複合キーは業界と記号の両方を見る(self):
        insert_pallet(self.conn, width=1470, length=2700,
                      industry="5x10", symbol="強度up")
        got = svc.recompute_fit_ranges(self.conn)
        self.assertEqual(got.fixed, 1)
        self.assertEqual([(m.column, m.raw) for m in got.loose],
                         [("業界", "5x10"), ("記号", "強度up")])

    def test_当たらなかった行は知らせない(self):
        """そろえても固定表に無いものは、ゆれではなく単に対象外。"""
        insert_pallet(self.conn, width=999, length=999,
                      industry="めずらしい業界", symbol="zz")
        self.assertEqual(svc.recompute_fit_ranges(self.conn).loose, [])

    def test_多いときは数を言って切る(self):
        """全部並べると読む気が失せる。**黙って切らない。**"""
        for _ in range(svc.LOOSE_LIST_LIMIT + 5):
            insert_pallet(self.conn, width=1470, length=2700,
                          industry="5x10", symbol="")
        got = svc.recompute_fit_ranges(self.conn)
        self.assertEqual(len(got.loose), svc.LOOSE_LIST_LIMIT + 5)
        text = got.summary()
        self.assertIn(f"書き方をそろえて拾った行: {svc.LOOSE_LIST_LIMIT + 5}件", text)
        self.assertIn("ほか 5件", text)
        self.assertEqual(text.count("管理番号"), svc.LOOSE_LIST_LIMIT)


class LooseKeyRowsTests(PalletServiceTestCase):
    """設定画面の「いまの状態」が読む、**書き込まない**数え方。

    再計算を押さなくても「マスタに書き方のゆれがある」と分かるように
    するためのもの。
    """

    def test_書き込まない(self):
        insert_pallet(self.conn, width=1470, length=2700,
                      industry="5x10", symbol="")
        before = self.conn.execute(
            "SELECT 巾適合min, 巾適合max FROM PalletMaster").fetchone()
        svc.loose_key_rows(self.conn)
        after = self.conn.execute(
            "SELECT 巾適合min, 巾適合max FROM PalletMaster").fetchone()
        self.assertEqual(tuple(before), tuple(after))

    def test_ゆれている行を数える(self):
        insert_pallet(self.conn, width=1470, length=2700,
                      industry="5x10", symbol="")
        insert_pallet(self.conn, width=920, length=640,
                      industry="", symbol="c8")
        found = svc.loose_key_rows(self.conn)
        self.assertEqual([(m.column, m.raw, m.matched) for m in found],
                         [("業界", "5x10", "5×10"), ("記号", "c8", "C8")])

    def test_固定表に無いものは数えない(self):
        """`EXﾀｲﾄ` のように、そろえても表に無いものはゆれではない。"""
        insert_pallet(self.conn, width=999, length=999,
                      industry="タイト", symbol="EXﾀｲﾄ")
        self.assertEqual(svc.loose_key_rows(self.conn), [])

    def test_そろっていれば数えない(self):
        insert_pallet(self.conn, width=1470, length=2700,
                      industry="5×10", symbol="")
        self.assertEqual(svc.loose_key_rows(self.conn), [])
