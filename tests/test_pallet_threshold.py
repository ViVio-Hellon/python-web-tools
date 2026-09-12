"""パレット適合閾値をマスタの表へ出したことの検証。

このテストが守っているのは2つ。

  1. **表に出したことで値が1つも変わっていない**こと
     移植前は `pallet_service.py` にPythonの固定表として書かれていた。
     その数値をここに写してあり(`旧固定表`)、DBから引いた値と
     全寸法で突き合わせる。片方だけ直せばここが落ちる。
  2. **読めないときは1行も直さない**こと(移植元 VBA の 案X)
     帯に穴があるまま通った行だけ直すと、正しい行と間違った行が
     混ざったマスタができる。

実行方法:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import (db, import_specs, master_admin,  # noqa: E402
                            pallet_service as svc, pallet_threshold)


# ==================================================================
# 移植前の固定表(`pallet_service.py` にあったもの)
# ==================================================================
# **ここは触らないこと。** DB化で値が動いていないことを言うための
# 独立した写しで、実装から引いてくると突き合わせの意味が無くなる。
#
# 【意図して変えた帯は、ここではなく下の一覧に書く】
# 基準表が変われば値も変わります。そのときこの写しを書き換えてしまうと、
# **意図した変更と、うっかり壊したのとが見分けられなくなります。**
# 写しは移植前のまま置いて、変えた帯だけを `_変えた帯` に挙げます。
# 全寸法の突き合わせはそこを避け、代わりに新しい値を名指しで確かめます。
_旧_DAKE_MIN = (
    (200, 110), (300, 191), (400, 291), (500, 391), (600, 491),
    (700, 591), (800, 691), (900, 791), (1000, 891),
    (1100, 991), (1200, 1091), (1300, 1191), (1450, 1291), (1550, 1441),
    (1800, 1541), (2100, 1791), (2350, 2091), (2650, 2341), (3150, 2641),
    (3650, 3141), (4150, 3641), (4600, 4141), (5100, 4591), (5600, 5091),
    (6100, 5591), (6600, 6091), (7100, 6591),
)
_旧_DAKE_MAX = (
    (200, 190), (300, 290), (400, 390), (500, 490), (600, 590),
    (700, 690), (800, 790), (900, 890), (1000, 990),
    (1100, 1090), (1200, 1190), (1300, 1290), (1450, 1440), (1550, 1540),
    (1800, 1790), (2100, 2090), (2350, 2340), (2650, 2640), (3150, 3140),
    (3650, 3640), (4150, 4140), (4600, 4590), (5100, 5090), (5600, 5590),
    (6100, 6090), (6600, 6590), (7100, 7090),
)
_旧_HABA_MIN = (
    (150, 50), (200, 141), (250, 191), (300, 241),
    (400, 300), (450, 391), (500, 441), (550, 491), (600, 541),
    (650, 591), (700, 641), (750, 691), (800, 741), (850, 791),
    (900, 841), (950, 891), (1000, 941), (1050, 991), (1100, 1041),
    (1150, 1091), (1200, 1141), (1250, 1191), (1300, 1241), (1350, 1291),
    (1400, 1341), (1450, 1391), (1500, 1441), (1550, 1491), (1600, 1541),
    (1650, 1591), (1700, 1641), (1750, 1691), (1800, 1741),
    (1850, 1791), (1900, 1841), (1950, 1891), (2000, 1941), (2050, 1991),
)
_旧_HABA_MAX = (
    (150, 140), (200, 190), (250, 240), (300, 290),
    (400, 390), (450, 440), (500, 490), (550, 540), (600, 590),
    (650, 640), (700, 690), (750, 740), (800, 790), (850, 840),
    (900, 890), (950, 940), (1000, 990), (1050, 1040), (1100, 1090),
    (1150, 1140), (1200, 1190), (1250, 1240), (1300, 1290), (1350, 1340),
    (1400, 1390), (1450, 1440), (1500, 1490), (1550, 1540), (1600, 1590),
    (1650, 1640), (1700, 1690), (1750, 1740), (1800, 1790),
    (1850, 1840), (1900, 1890), (1950, 1940), (2000, 1990), (2050, 2040),
)


# 移植後に基準表そのものが変わった帯。**避ける理由と版を必ず書くこと。**
_変えた帯: tuple[tuple[int, int, str], ...] = (
    (301, 400, "VER2.52.1: 幅301〜400を50mm刻みの2本に割った"),
)


def _変えた帯か(value: int) -> bool:
    return any(lo <= value <= hi for lo, hi, _why in _変えた帯)


def _旧引き(table, value: int, over: int) -> int:
    for threshold, found in table:
        if value <= threshold:
            return found
    return over


def _旧_脚数(dake: int) -> int:
    for threshold, found in ((1550, 2), (2650, 3), (3150, 4),
                             (4150, 5), (4600, 6)):
        if dake <= threshold:
            return found
    return 7


def _旧_桁数(haba: int) -> int:
    for threshold, found in ((350, 2), (700, 3), (1280, 4),
                             (1400, 5), (1800, 6)):
        if haba <= threshold:
            return found
    return 7


class ThresholdTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)          # 初期値もここで入る

    def tearDown(self) -> None:
        self.conn.close()


# ------------------------------------------------------------------
# 1. 値が変わっていないこと
# ------------------------------------------------------------------
class SameAsBeforeTests(ThresholdTestCase):
    def test_全寸法で移植前と同じ値を返す(self):
        """0〜12000mmの全寸法で、6つの引きが移植前と一致する。

        境界だけを見ると、帯を1本落としても隣の帯が吸収して気づけない。
        1mm刻みで全部見る。
        """
        th = pallet_threshold.load(self.conn)
        for value in range(0, 12001):
            if _変えた帯か(value):
                continue                     # 下の専用の試験が見ている
            with self.subTest(value=value):
                self.assertEqual(th.dake_min(value),
                                 _旧引き(_旧_DAKE_MIN, value, 6591))
                self.assertEqual(th.dake_max(value),
                                 _旧引き(_旧_DAKE_MAX, value, 99999))
                self.assertEqual(th.haba_min(value),
                                 _旧引き(_旧_HABA_MIN, value, 1991))
                self.assertEqual(th.haba_max(value),
                                 _旧引き(_旧_HABA_MAX, value, 99999))
                self.assertEqual(th.ashi(value), _旧_脚数(value))
                self.assertEqual(th.keta(value), _旧_桁数(value))

    def test_幅301から400は50mm刻みの2本に割れた(self):
        """VER2.52.1 で基準表そのものが変わった帯。

        以前は「301〜400 → 適合300〜390」の1本で、**適合最小値だけが
        入力最小値を下回って**いた(2山計算で305等の半端値を吸収するため)。
        50mm刻みの2本に割り、他の帯と同じ「帯の下限 - 10」に揃った。

        この帯だけは上の突き合わせを避けているので、**ここが唯一の
        見張り番**になる。境界の4点を名指しで押さえる。
        """
        th = pallet_threshold.load(self.conn)
        # 301〜350 の帯
        self.assertEqual((th.haba_min(301), th.haba_max(301)), (291, 340))
        self.assertEqual((th.haba_min(350), th.haba_max(350)), (291, 340))
        # 351〜400 の帯
        self.assertEqual((th.haba_min(351), th.haba_max(351)), (341, 390))
        self.assertEqual((th.haba_min(400), th.haba_max(400)), (341, 390))
        # 隣の帯は動いていない(割ったときに巻き込んでいないこと)
        self.assertEqual((th.haba_min(300), th.haba_max(300)), (241, 290))
        self.assertEqual((th.haba_min(401), th.haba_max(401)), (391, 440))

    def test_幅の帯は端を除いて下限マイナス10で揃っている(self):
        """**割った目的そのもの。**

        301〜400 の1本だけが「帯の下限 - 10」の規則から外れていた。
        外れた帯があると、そのパレットには載らない小さい製品まで適合に
        なる(帯が広い分だけ下限が甘くなるため)。

        両端の2本は昔から別扱いで、ここでも見ない。

            最初(0〜150)   … 下限が -10 になってしまうので 50 で止める
            最後(2051〜)    … 範囲外を捕まえるための帯。上限は 99999 固定
        """
        rows = pallet_threshold.rows(self.conn, "PalletHabaThreshold")
        for row in rows[1:-1]:
            with self.subTest(帯=f"{row['入力最小値']}〜{row['入力最大値']}"):
                self.assertEqual(row["適合最小値"], row["入力最小値"] - 10)
                self.assertEqual(row["適合最大値"], row["入力最大値"] - 10)
        # 端の2本は決め打ちで押さえる(見ない理由と、いまの値を残す)
        self.assertEqual((rows[0]["入力最小値"], rows[0]["入力最大値"],
                          rows[0]["適合最小値"], rows[0]["適合最大値"]),
                         (0, 150, 50, 140))
        self.assertEqual((rows[-1]["入力最小値"], rows[-1]["入力最大値"],
                          rows[-1]["適合最小値"], rows[-1]["適合最大値"]),
                         (2051, 999999, 1991, 99999))

    def test_固定適合表も移植前と同じ(self):
        """記号・業界・複合の値。移植前は `add_range` の並びだった。"""
        t = svc.build_pallet_range_table(self.conn)
        self.assertEqual((t.w_min["C8"], t.w_max["C8"],
                          t.l_min["C8"], t.l_max["C8"]), (920, 1000, 640, 1080))
        self.assertEqual((t.keta["C8"], t.ashi["C8"]), (4, 2))
        self.assertEqual((t.w_min["P5"], t.keta["P5"]), (1015, 5))
        self.assertEqual((t.w_min["4×8"], t.l_max["4×8"]), (1185, 2505))
        self.assertEqual(t.combo["5×10|強度UP"], (1485, 1540, 2700, 3090))
        # 記号15 + 業界6 = 21鍵(重なりは無い)
        self.assertEqual(len(t.w_min), 21)


# ------------------------------------------------------------------
# 2. 初期値の入り方
# ------------------------------------------------------------------
class SeedTests(ThresholdTestCase):
    def test_件数は移植元のとおり(self):
        counts = {t: self.conn.execute(
            f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            for t in pallet_threshold.TABLES}
        self.assertEqual(counts, {
            "PalletDakeThreshold": 28, "PalletHabaThreshold": 40,
            "PalletAshiThreshold": 6, "PalletKetaThreshold": 6,
            "PalletSymbolMaster": 15, "PalletIndustryMaster": 6,
            "PalletComboMaster": 1,
        })

    def test_入っている表には二度と入れない(self):
        """**直した値を起動のたびに書き戻さないこと。**

        入れ直してしまうと、資材課が直した基準が翌朝には元に戻る。
        気づけないまま古い基準で選定が続く。
        """
        self.conn.execute(
            "UPDATE PalletKetaThreshold SET 桁数 = 99 WHERE 入力最小値 = 0")
        self.conn.commit()
        db.apply_schema(self.conn)          # 起動をもう一度
        row = self.conn.execute(
            "SELECT 桁数 FROM PalletKetaThreshold WHERE 入力最小値 = 0"
        ).fetchone()
        self.assertEqual(row["桁数"], 99)

    def test_空になった表には入れ直す(self):
        self.conn.execute("DELETE FROM PalletAshiThreshold")
        self.conn.commit()
        db.apply_schema(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM PalletAshiThreshold").fetchone()[0], 6)


# ------------------------------------------------------------------
# 3. 帯が繋がっていないときは中断する
# ------------------------------------------------------------------
class BrokenBandTests(ThresholdTestCase):
    def test_帯に穴があれば読み込みを断る(self):
        self.conn.execute(
            "DELETE FROM PalletDakeThreshold WHERE 入力最小値 = 601")
        self.conn.commit()
        with self.assertRaises(pallet_threshold.ThresholdError) as caught:
            pallet_threshold.load(self.conn)
        # **どこで切れているかを言う。** 「失敗しました」では直せない
        self.assertIn("PalletDakeThreshold", str(caught.exception))
        self.assertIn("701", str(caught.exception))

    def test_有効を0にしても穴として見つかる(self):
        """行を消さずに外せるが、外した分の穴は見逃さない。"""
        self.conn.execute(
            "UPDATE PalletHabaThreshold SET 有効 = 0 WHERE 入力最小値 = 401")
        self.conn.commit()
        with self.assertRaises(pallet_threshold.ThresholdError):
            pallet_threshold.load(self.conn)

    def test_最終行が999999でなければ断る(self):
        self.conn.execute(
            "UPDATE PalletKetaThreshold SET 入力最大値 = 5000 "
            "WHERE 入力最大値 = 999999")
        self.conn.commit()
        with self.assertRaises(pallet_threshold.ThresholdError) as caught:
            pallet_threshold.load(self.conn)
        self.assertIn("999999", str(caught.exception))

    def test_1行も無ければ断る(self):
        self.conn.execute("DELETE FROM PalletAshiThreshold")
        self.conn.commit()
        with self.assertRaises(pallet_threshold.ThresholdError):
            pallet_threshold.load(self.conn)

    def test_読めないときは1行も直さない(self):
        """移植元 案X。**通った行だけ直さない。**

        直った行と直っていない行が混ざると、マスタのどの値が信用
        できるのか分からなくなる。何も書かずに理由を返す。
        """
        self.conn.execute(
            "INSERT INTO PalletMaster (幅, 丈, 巾適合min, 巾適合max, "
            "丈適合min, 丈適合max, 業界, 記号, 位置, 在庫数, リスト管理, "
            "桁数, 脚数, コード, 単位, 備考, 更新日時) "
            "VALUES (999, 999, 7, 7, 7, 7, '', '', 'A-01', 1, '', 0, 0, "
            "'', '台', '', '')")
        self.conn.execute("DELETE FROM PalletDakeThreshold")
        self.conn.commit()

        got = svc.recompute_fit_ranges(self.conn)
        self.assertFalse(got.ok)
        self.assertIn("PalletDakeThreshold", got.error or "")
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual((row["巾適合min"], row["丈適合min"]), (7, 7))


# ------------------------------------------------------------------
# 4. 帯の外の寸法
# ------------------------------------------------------------------
class BandMissTests(ThresholdTestCase):
    def test_帯の外なら触らずに報告する(self):
        """帯は0〜999999を覆うので、外れるのは999999超のときだけ。

        移植元はここで `-999999` をマスタへ書いていた。適合minに
        マイナス99万が入った行は**以後どんな検索にも当たり続ける**ので、
        壊れていることが結果の形に出ない。触らずに数えて報告する。
        """
        self.conn.execute(
            "INSERT INTO PalletMaster (幅, 丈, 巾適合min, 巾適合max, "
            "丈適合min, 丈適合max, 業界, 記号, 位置, 在庫数, リスト管理, "
            "桁数, 脚数, コード, 単位, 備考, 更新日時) "
            "VALUES (1000000, 1000000, 5, 5, 5, 5, '', '', 'A-01', 1, '', "
            "0, 0, '', '台', '', '')")
        self.conn.commit()

        got = svc.recompute_fit_ranges(self.conn)
        self.assertTrue(got.ok)
        self.assertTrue(got.band_miss)
        self.assertIn("帯の範囲外", got.summary())
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual((row["巾適合min"], row["巾適合max"],
                          row["丈適合min"], row["丈適合max"]), (5, 5, 5, 5))
        self.assertNotIn(pallet_threshold.NO_HIT,
                         (row["桁数"], row["脚数"]))


# ------------------------------------------------------------------
# 5. 積む順序(業界 → 記号が上書き → 複合が最優先)
# ------------------------------------------------------------------
class PriorityTests(ThresholdTestCase):
    def test_同じ鍵なら記号が業界に勝つ(self):
        self.conn.execute(
            "INSERT INTO PalletIndustryMaster "
            "(業界, 巾適合最小値, 巾適合最大値, 丈適合最小値, 丈適合最大値) "
            "VALUES ('C1', 1, 2, 3, 4)")
        self.conn.commit()
        t = svc.build_pallet_range_table(self.conn)
        # 記号 C1 の値(410〜490 / 600〜750)が残る
        self.assertEqual((t.w_min["C1"], t.l_max["C1"]), (410, 750))

    def test_複合が記号より優先される(self):
        """業界5×10 + 記号 強度UP の行は、複合の巾1485を使う。

        業界単独の 5×10 は巾1470。**同じ業界でも記号で値が変わる**
        ことがこの表の存在理由なので、ここが逆転すると意味が無い。
        """
        self.conn.execute(
            "INSERT INTO PalletMaster (幅, 丈, 巾適合min, 巾適合max, "
            "丈適合min, 丈適合max, 業界, 記号, 位置, 在庫数, リスト管理, "
            "桁数, 脚数, コード, 単位, 備考, 更新日時) "
            "VALUES (1470, 2700, 0, 0, 0, 0, '5×10', '強度UP', 'A-01', 1, "
            "'', 0, 0, '', '台', '', '')")
        self.conn.commit()
        svc.recompute_fit_ranges(self.conn)
        row = self.conn.execute("SELECT * FROM PalletMaster").fetchone()
        self.assertEqual(row["巾適合min"], 1485)

    def test_有効を0にした固定適合は使わない(self):
        self.conn.execute(
            "UPDATE PalletSymbolMaster SET 有効 = 0 WHERE 記号 = 'C8'")
        self.conn.commit()
        t = svc.build_pallet_range_table(self.conn)
        self.assertNotIn("C8", t.w_min)


# ------------------------------------------------------------------
# 6. 直せる場所が既存のマスタ管理画面であること
# ------------------------------------------------------------------
class MasterAdminTests(ThresholdTestCase):
    def test_7表とも管理画面に載っている(self):
        for table in pallet_threshold.TABLES:
            with self.subTest(table=table):
                self.assertIn(table, master_admin.BY_TABLE)
                self.assertEqual(master_admin.view_only_why(table), "")

    def test_取り込み元に無くてもよい表として扱う(self):
        """移植元は別ファイル(accdb)に持っていたので、現場の梱包資材
        マスタにはまだ無い。無いことを取り込みの失敗として数えない。
        """
        for table in pallet_threshold.TABLES:
            with self.subTest(table=table):
                self.assertIn(table, import_specs.OPTIONAL_TABLES)
                self.assertTrue(master_admin.can_create(table))

    def test_打ち込める列が並ぶ(self):
        cols = {c.name for c in master_admin.columns(
            self.conn, "PalletSymbolMaster")}
        self.assertEqual(cols, {
            "記号", "巾適合最小値", "巾適合最大値", "丈適合最小値",
            "丈適合最大値", "桁数", "脚数", "有効", "並び順", "備考", "更新日時"})
        # 採番用の ID は打ち込ませない(取り込みで振り直すため)
        self.assertNotIn("ID", cols)

    def test_更新日時は空欄なら今の日時が入る列(self):
        stamps = [c for c in master_admin.columns(self.conn, "PalletDakeThreshold")
                  if c.stamp]
        self.assertEqual([c.name for c in stamps], ["更新日時"])


# ------------------------------------------------------------------
# 6.5 別ファイルから読むこと
# ------------------------------------------------------------------
class SeparateFileTests(unittest.TestCase):
    """閾値の7表は**梱包資材マスタには入っていない。**

    移植元(VBA)が閾値だけを別ファイルに持っていて、現場の写しもその形で
    配られている(現場の指摘:「取り込み元にパレット閾値の条件がないので
    テーブルを読み込めていない」)。読む先も、直す先も、そちらにする。
    """

    def test_取り込み元は閾値マスタのほう(self):
        from packaging_tool import data_sync
        with unittest.mock.patch.object(
                data_sync, "find_threshold_db",
                return_value=Path("/tmp/PalletThresholdMaster.sqlite3")):
            for table in pallet_threshold.TABLES:
                with self.subTest(table=table):
                    self.assertEqual(
                        master_admin.source_for(table).name,
                        "PalletThresholdMaster.sqlite3")

    def test_ほかの表は梱包資材マスタのまま(self):
        from packaging_tool import data_sync
        with unittest.mock.patch.object(
                data_sync, "find_material_db",
                return_value=Path("/tmp/梱包資材マスタ.sqlite3")):
            self.assertEqual(master_admin.source_for("PalletMaster").name,
                             "梱包資材マスタ.sqlite3")

    def test_見つからない理由は探した場所ごと言う(self):
        """「見つかりません」だけでは、どこを直せばよいか分からない。"""
        why = master_admin.source_label("PalletHabaThreshold")
        self.assertIn("パレット閾値マスタ", why)
        self.assertIn("PalletThresholdMaster", why)


# ------------------------------------------------------------------
# 7. 取り込みの案内が埋もれないこと
# ------------------------------------------------------------------
class ImportNoticeTests(unittest.TestCase):
    def test_元に無い任意テーブルは1行にまとめる(self):
        """**案内で8行埋めない。**

        閾値7表を足したことで、取り込み元に無い任意テーブルは
        アクセス権限と合わせて8つになった。1表1行で出すと、案内だけで
        まとめが8行になり、本当に直すべき失敗がその下に埋もれる。
        """
        from packaging_tool import data_sync
        got = data_sync.ImportResult(imported={"PalletMaster": 3})
        got.missing_optional.extend(pallet_threshold.TABLES)
        text = got.summary()
        self.assertEqual(text.count("取り込み元に無かった表"), 1)
        self.assertIn("7件", text)
        self.assertIn("PalletComboMaster", text)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
