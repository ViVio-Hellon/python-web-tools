"""取り込み・発注で、数の小数を切り捨てない(VBA と同じ値で持つ)。

現場の共有では、注文の丈 2502.5・2787.5 などが 60件、PalletMaster の幅 2.5 が 3件、
仕掛台帳の枚本数 2.9 などが 1,700件あまりあった。ツールは取り込みで整数にしていた
(2971.5 → 2971、2.9 → 2)。VBA・Access は小数のまま持ち、注文は `FormatDimension(..., "0.0")`
で 2502.5 のまま書く。枚本数は画面には元の値のまま出し、数えるときは `CLng`(いちばん近い整数)。

【ここで守りたいこと】
1. 注文の幅・丈、PalletMaster・入出庫履歴の幅・丈、枚本数は、小数を小数のまま取り込む
2. 整数の値は今までどおり整数(表示・突き合わせを変えない)
3. ツールが出す発注も、幅・丈の小数を切り捨てない
4. 枚本数: 見せるのは元の値、数えるのは VBA の CLng と同じ丸め
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import config, data_sync, db, import_specs, lot_service  # noqa: E402
from packaging_tool import sync_sources as sources  # noqa: E402
from packaging_tool import warehouse_service as wh  # noqa: E402


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


class ConvertTests(unittest.TestCase):
    def test_小数は小数のまま_整数は整数(self) -> None:
        self.assertEqual(import_specs.to_number("2971.5"), 2971.5)
        got = import_specs.to_number("1092.0")
        self.assertEqual((got, type(got)), (1092, int))
        self.assertEqual(import_specs.to_number("2.5"), 2.5)
        self.assertIsNone(import_specs.to_number(""))
        self.assertIsNone(import_specs.to_number("abc"))
        self.assertIsNone(import_specs.to_number("inf"))

    def test_CLngと同じ丸め(self) -> None:
        for value, want in (("2.9", 3), ("1.6", 2), ("2.5", 2), ("3.5", 4), ("4", 4), ("", 0)):
            with self.subTest(value=value):
                self.assertEqual(import_specs.clng(value), want)


class ImportKeepsDecimalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()
        self.addCleanup(self.conn.close)

    def _import(self, table: str, rows: list[dict], specs=None, source_table: str = "") -> None:
        specs = specs or import_specs.IMPORT_SPECS
        with mock.patch.object(sources, "read_table", return_value=rows):
            result = data_sync.import_tables(
                self.conn, Path("d.sqlite3"), {table: specs[table]},
                source_table=source_table,
                required=import_specs.REQUIRED_KEY_COLUMNS,
                fallbacks=import_specs.NULL_FALLBACKS)
        self.assertIn(table, result.imported, result.errors)

    def test_注文の丈の小数を落とさない(self) -> None:
        self._import(config.TBL_WAREHOUSE_ORDER, [
            {"登録日時": "2026/08/12 11:55:54", "LotNo": "R3525Z0", "品名": "タイト",
             "発注コード": "059539", "発注数": "6", "厚": "8.000", "幅": "1111.0", "丈": "2971.5"}])
        row = self.conn.execute(f'SELECT 幅, 丈 FROM "{config.TBL_WAREHOUSE_ORDER}"').fetchone()
        self.assertEqual((row[0], row[1]), (1111, 2971.5))

    def test_パレットの幅の小数を落とさない(self) -> None:
        self._import("PalletMaster", [{"幅": "2.5", "丈": "620", "業界": "タイト"}])
        self.assertEqual(self.conn.execute("SELECT 幅, 丈 FROM PalletMaster").fetchone()[:], (2.5, 620))

    def test_枚本数は元の値で持ち_数えるときはCLng_見せるときは元の値(self) -> None:
        spec = import_specs.LOT_IMPORT_SPECS["仕掛ロット"]
        src = {local: source for local, source, _c in spec}
        self._import("仕掛ロット", [{src["ロット番号"]: "A000001", src["BOX最終実績_枚本数"]: "2.9",
                                     src["設計_設備コース"]: "GFS"}],
                     specs=import_specs.LOT_IMPORT_SPECS, source_table="仕掛")
        self.assertEqual(self.conn.execute("SELECT BOX最終実績_枚本数 FROM 仕掛ロット").fetchone()[0], 2.9)
        lot = lot_service.search_lot(self.conn, "A000001").lot
        self.assertEqual((lot.final_process_count, lot.final_process_text), (3, "2.9"))


class OrderKeepsDecimalsTests(unittest.TestCase):
    def test_ツールが出す発注も丈の小数を落とさない(self) -> None:
        conn = make_conn()
        self.addCleanup(conn.close)
        made = wh.create_order(conn, lot_no="L1", hinmei="タイト", hatchu_code="X1", tani="台",
                               atu="140.0", haba="1092.0", take="2502.5", hatchu_suu=1,
                               terminal="GENBA-1")
        self.assertTrue(made.ok, made.message)
        row = conn.execute(f'SELECT 幅, 丈 FROM "{config.TBL_WAREHOUSE_ORDER}"').fetchone()
        self.assertEqual((row[0], row[1]), (1092, 2502.5))

    def test_数でない幅は断る(self) -> None:
        conn = make_conn()
        self.addCleanup(conn.close)
        made = wh.create_order(conn, lot_no="L1", hinmei="タイト", hatchu_code="X1", tani="台",
                               atu="1", haba="abc", take="2000", hatchu_suu=1, terminal="GENBA-1")
        self.assertFalse(made.ok)
        self.assertIn("数値", made.message)


if __name__ == "__main__":
    unittest.main()
