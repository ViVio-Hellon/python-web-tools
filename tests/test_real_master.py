"""実マスタ(梱包資材マスタ.sqlite3)を使った検証

作り物のデータでロジックの分岐だけを試していると、**現実にはあり得ない
寸法**でテストが通ってしまう(製品より小さいパレットを「適合」と
扱ってしまう、など)。実マスタが手元にあるときは、そのデータで
選定結果とマスタの健全性を確かめる。

`梱包資材マスタ.sqlite3` は業務データなのでリポジトリには入れない
(`.gitignore`)。ファイルが無い環境ではこのテストは丸ごとスキップする。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import board_selection_service as svc  # noqa: E402
from packaging_tool import config, data_sync, db, source_db, user_log  # noqa: E402

MASTER = Path(__file__).resolve().parent.parent / config.MATERIAL_DB_NAME
CAN_READ = source_db.is_readable(MASTER)


@unittest.skipUnless(CAN_READ, f"実マスタ({config.MATERIAL_DB_NAME})が読めないためスキップ")
class RealMasterTestCase(unittest.TestCase):
    conn = None

    @classmethod
    def setUpClass(cls) -> None:
        import sqlite3

        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        db.apply_schema(cls.conn)
        data_sync.import_master(cls.conn, MASTER)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.conn is not None:
            cls.conn.close()

    def rows(self):
        return self.conn.execute(
            "SELECT * FROM PalletMaster WHERE 幅 > 0 AND 丈 > 0").fetchall()


class MasterHealthTests(RealMasterTestCase):
    """取り込んだあとの状態。取り込みは適合範囲の再計算まで含む。"""

    def test_the_master_is_not_empty(self):
        self.assertGreater(len(self.rows()), 1000)

    def test_importing_leaves_no_row_claiming_more_than_it_can_hold(self):
        """取り込み後は 適合max ≦ 現物サイズ。

        Access側の値が(再計算の頭打ち漏れで)広がっていても、
        取り込み時に計算し直すのでこちらは常に正しい。
        """
        broken = [
            f"{r['幅']}x{r['丈']} 巾適合max={r['巾適合max']} 丈適合max={r['丈適合max']}"
            for r in self.rows()
            if (r["巾適合max"] or 0) > r["幅"] or (r["丈適合max"] or 0) > r["丈"]
        ]
        self.assertEqual(broken, [], f"適合範囲が現物を超える行があります: {broken[:5]}")

    def test_inverted_fit_ranges_are_reported_not_hidden(self):
        """min>maxで逆転した行は実在する。数を固定して増減に気づけるようにする。"""
        inverted = [r for r in self.rows()
                    if r["巾適合max"] < r["巾適合min"] or r["丈適合max"] < r["丈適合min"]]
        # 2026年時点の実データは70件。増えたらマスタ側が悪化している
        self.assertLessEqual(len(inverted), 70)
        # 逆転行はすべて幅300以下の細物(業務上ここだけ運用が違う)
        for row in inverted:
            self.assertLessEqual(row["幅"], 300, f"{row['幅']}x{row['丈']}")


class RecomputeAgainstRealMasterTests(RealMasterTestCase):
    """適合範囲の再計算が、Accessに入っている値を再現するか。

    再計算(VBA `RunUpdatePalletAll`)は基準表から適合範囲を作り直す。
    基準表は「製品サイズの帯 → その帯の標準パレット」の表なので、
    標準サイズでないパレットにそのまま当てると自分より大きい製品を
    受けられると名乗ってしまう。現物サイズで頭打ちにすれば、
    Accessに実際に入っている値と一致する。
    """

    def test_it_reproduces_what_access_itself_holds(self):
        """Accessの生の値と突き合わせる。

        `import_master` は取り込みのあとに再計算まで走るので、SQLite側の
        値と比べても意味がない(必ず一致してしまう)。Accessのテーブルを
        直接読んで比べる。
        """
        raw = data_sync.read_table(MASTER, "PalletMaster")
        self.assertGreater(len(raw), 1000)

        def num(value):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

        from packaging_tool import db, pallet_service as ps, pallet_threshold

        # 閾値はマスタの表から読む。ここでは初期値(=社内基準表)のまま
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        th = pallet_threshold.load(conn)

        # 管理番号はSQLite側で振り直されるので突き合わせに使えない。
        # 行ごとに「その寸法なら我々は何を書くか」を出してAccessの値と比べる
        same = same_uncapped = total = 0
        for row in raw:
            width, length = num(row.get("幅")), num(row.get("丈"))
            w_max, l_max = num(row.get("巾適合max")), num(row.get("丈適合max"))
            if not width or not length or w_max is None or l_max is None:
                continue
            total += 1
            band = (th.haba_max(width), th.dake_max(length))
            capped = (ps.cap_to_pallet(band[0], width),
                      ps.cap_to_pallet(band[1], length))
            if capped == (w_max, l_max):
                same += 1
            if band == (w_max, l_max):
                same_uncapped += 1

        self.assertGreater(total, 1000)
        # 現物サイズで頭打ちにした値がAccessの実データと一致する
        self.assertGreater(same / total, 0.95,
                           f"頭打ちありで一致したのは {same}/{total} 行")
        # 頭打ちを外すと(移植元の RunUpdatePalletAll と同じ)大きく外れる
        self.assertLess(same_uncapped / total, 0.5,
                        f"頭打ち無しでも {same_uncapped}/{total} 行一致してしまう")

    def test_recomputing_never_lets_a_row_overclaim(self):
        """再計算のあと、適合maxが現物を超える行が1行も無いこと。

        ここが崩れると、現物に載らないパレットが検索に乗る。
        """
        from packaging_tool import pallet_service

        pallet_service.recompute_fit_ranges(self.conn)
        over = self.conn.execute(
            "SELECT 幅, 丈, 巾適合max, 丈適合max FROM PalletMaster "
            "WHERE 幅 > 0 AND 丈 > 0 AND (巾適合max > 幅 OR 丈適合max > 丈)"
        ).fetchall()
        detail = [f"{r['幅']}x{r['丈']}(巾max={r['巾適合max']} 丈max={r['丈適合max']})"
                  for r in over[:5]]
        self.assertEqual(len(over), 0, f"現物を超える行: {detail}")

    def test_recomputing_is_stable(self):
        """2回流しても値が動かないこと(取り込みのたびに走るため)。"""
        from packaging_tool import pallet_service

        pallet_service.recompute_fit_ranges(self.conn)
        first = self.conn.execute(
            "SELECT 管理番号, 巾適合min, 巾適合max, 丈適合min, 丈適合max "
            "FROM PalletMaster ORDER BY 管理番号").fetchall()
        pallet_service.recompute_fit_ranges(self.conn)
        second = self.conn.execute(
            "SELECT 管理番号, 巾適合min, 巾適合max, 丈適合min, 丈適合max "
            "FROM PalletMaster ORDER BY 管理番号").fetchall()
        self.assertEqual([tuple(r) for r in first], [tuple(r) for r in second])


class RealSelectionTests(RealMasterTestCase):
    """実データでの選定結果。"""

    def _select(self, width: str, length: str, **kw):
        log = user_log.UserLog()
        result = svc.auto_select_pallet(
            self.conn, product_width_text=width, product_length_text=length,
            user_log=log, **kw)
        return result, log

    def test_a_real_lot_gets_a_pallet_big_enough_to_hold_it(self):
        """H574B51相当(1528x3053)。選ばれたパレットに製品が載ること。"""
        result, _ = self._select("1528", "3053", manufactured_thickness=12.0)
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.width, 1528)
        self.assertGreaterEqual(result.length, 3053)

    def test_no_size_ever_gets_a_pallet_it_does_not_fit_on(self):
        """代表的な製品サイズを一通り流し、はみ出す選定が1件も無いこと。

        ここが本丸。「載るわけない」組み合わせを1件でも返したら失格。
        """
        sizes = [(1000, 2000), (1219, 2438), (1250, 2500), (1300, 3000),
                 (1500, 3000), (1524, 3048), (1528, 3053), (1550, 3100),
                 (900, 1800), (1100, 2200), (1000, 4000), (1250, 6000)]
        failures = []
        for width, length in sizes:
            for two_stack in (False, True):
                result, _ = self._select(str(width), str(length),
                                         two_stack=two_stack)
                if not result.ok:
                    continue
                # 回転・2山を反映した「載せたい寸法」は結果が持っている
                if (result.search_width > result.width + svc.PHYSICAL_MARGIN
                        or result.search_length > result.length + svc.PHYSICAL_MARGIN):
                    failures.append(
                        f"製品{width}x{length}(2山={two_stack}) → "
                        f"パレット{result.width}x{result.length} "
                        f"(載せたい寸法 {result.search_width}x{result.search_length})")
        self.assertEqual(failures, [], f"現物に載らない選定: {failures}")

    def test_the_searched_size_is_reported_back(self):
        """回転すると幅丈が入れ替わって探されることが結果から分かる。"""
        result, _ = self._select("1528", "3053", manufactured_thickness=12.0)
        self.assertTrue(result.ok)
        expected = (3053, 1528) if result.rotated else (1528, 3053)
        self.assertEqual((result.search_width, result.search_length), expected)

    def test_the_log_explains_the_decision(self):
        _result, log = self._select("1528", "3053", manufactured_thickness=12.0)
        self.assertIn("製品サイズ: 1528 x 3053", log.text)
        self.assertIn("決定", log.text)


LOT_LEDGER = [Path(__file__).resolve().parent.parent / name
              for name in config.LOT_DB_FILES.values()]
CAN_READ_LEDGER = all(source_db.is_readable(p) for p in LOT_LEDGER)


@unittest.skipUnless(CAN_READ_LEDGER, "実仕掛台帳が読めないためスキップ")
class RealLotLedgerTests(unittest.TestCase):
    """実仕掛台帳での表示値。作り物では気づけない型の崩れを見る。"""

    conn = None

    @classmethod
    def setUpClass(cls) -> None:
        import sqlite3

        from packaging_tool import lot_service

        cls.lot_service = lot_service
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        db.apply_schema(cls.conn)
        data_sync.import_lot_ledger(cls.conn, LOT_LEDGER[0].parent)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.conn is not None:
            cls.conn.close()

    def test_every_hiki_number_reads_as_a_plain_number(self):
        """指数表記(6.0717e+07)が1件も混ざらないこと。"""
        rows = self.conn.execute("SELECT 引当番号 FROM 仕掛引当").fetchall()
        self.assertGreater(len(rows), 0)
        bad = [r["引当番号"] for r in rows
               if not str(r["引当番号"]).isdigit()]
        self.assertEqual(bad[:5], [], f"番号として読めない引当番号: {bad[:5]}")

    def test_a_lot_never_has_more_hiki_rows_than_the_list_shows(self):
        """引当一覧の高さ(5行)で全件見えること。

        画面の定数を直接読むと tkinter を要求してしまうので、
        ここでは行数だけを見る(画面側は tests/test_web_lot.py で固定)。
        """
        HIKI_ROWS = 5
        most = self.conn.execute(
            "SELECT MAX(c) FROM (SELECT COUNT(*) c FROM 仕掛引当 GROUP BY ロット番号)"
        ).fetchone()[0]
        self.assertLessEqual(most, HIKI_ROWS)

    def test_the_test_slip_is_decided_by_advance_check(self):
        """VBA `AdvanceCheck`: 品質グレード_表面処理がS/T以外で、かつ
        製造板厚が3mm以下または用途コードの頭がT/D1/D2のいずれかなら
        試験指示票が要る(試験NOという、実際には判定に使われていない
        列で判定していた不具合の回帰確認)。

        `search_lot` は同じロット番号の複数行のうち`管理番号`最小の
        行を採るので、ここでも同じ行(グループの先頭)だけを対象にする。
        """
        first_rows = ("SELECT 管理番号, ロット番号, 品質グレード_表面処理,"
                      " 製造板厚, 用途コード FROM 仕掛ロット"
                      " WHERE 管理番号 IN"
                      " (SELECT MIN(管理番号) FROM 仕掛ロット GROUP BY ロット番号)")
        not_needed = self.conn.execute(
            f"{first_rows} AND 品質グレード_表面処理 IN ('S', 'T') LIMIT 1").fetchone()
        needed = self.conn.execute(
            f"{first_rows} AND 品質グレード_表面処理 NOT IN ('S', 'T')"
            "   AND (製造板厚 <= 3.0 OR 用途コード LIKE 'T%'"
            "        OR 用途コード LIKE 'D1%' OR 用途コード LIKE 'D2%') LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(not_needed, "表面処理がS/Tのロットが1件も無い")
        self.assertIsNotNone(needed, "試験指示票が要るはずのロットが1件も無い")

        not_needed_lot = self.lot_service.search_lot(self.conn, not_needed["ロット番号"]).lot
        needed_lot = self.lot_service.search_lot(self.conn, needed["ロット番号"]).lot
        self.assertEqual(not_needed_lot.test_slip_text,
                         self.lot_service.TEST_SLIP_NOT_NEEDED)
        self.assertEqual(needed_lot.test_slip_text,
                         self.lot_service.TEST_SLIP_NEEDED)


if __name__ == "__main__":                # pragma: no cover
    unittest.main()
