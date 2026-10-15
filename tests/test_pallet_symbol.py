"""PalletMaster の記号の新しい書き方(「もとの記号 ﾊﾟﾚｯﾄ台|ﾊﾟﾚｯﾄ蓋 補足」)の試験

現場の声:「記号の書き方は渡した SQLite の通りになります。影響がある箇所の洗い出しと修正対応を」

【ここで守りたいこと】
1. もとの記号は「ﾊﾟﾚｯﾄ台」「ﾊﾟﾚｯﾄ蓋」より前。印が無い記号(組の行・前の書き方)はそのまま
2. 品名・帳票の記号は前と同じ(「スカシ　P7　1350x1350」「タイト　　1400x3150」)。A8 等の出さない記号も前どおり出さない
3. 5×10 の板厚の見分け(強度UP は薄板専用)が、新しい書き方でも効く
4. 固定の適合範囲・桁数・脚数(C1・P1… の表)が、新しい書き方でも当たる。表記ゆれとして数えない
"""
from __future__ import annotations

import sqlite3
import unittest

from packaging_tool import db, lot_service
from packaging_tool import pallet_service as svc
from packaging_tool.pallet_common import _thickness_5x10_ok
from packaging_tool.pallet_symbol import base_symbol


class BaseSymbolTests(unittest.TestCase):
    def test_もとの記号は品種の印より前(self) -> None:
        cases = {
            "C1 ﾊﾟﾚｯﾄ台": "C1",
            "C1 脚高 ﾊﾟﾚｯﾄ台": "C1 脚高",
            "強度UP ﾊﾟﾚｯﾄ台 薄板用5x10": "強度UP",
            "A8 ﾊﾟﾚｯﾄ台 標準5x10(新5x10)": "A8",
            " ﾊﾟﾚｯﾄ台 マトリックス": "",
            " ﾊﾟﾚｯﾄ蓋": "",
            "EXﾀｲﾄ ﾊﾟﾚｯﾄ台 ﾊﾟﾚｯﾄ台 熱処理輸出": "EXﾀｲﾄ",
            "EXﾀｲﾄ ﾊﾟﾚｯﾄ蓋 合板 ﾊﾟﾚｯﾄ蓋 熱処理/合板あり": "EXﾀｲﾄ",
            "滑川   蓋高 ﾊﾟﾚｯﾄ蓋 滑川   蓋高": "滑川   蓋高",
            "菊川工業 ﾊﾟﾚｯﾄ台 菊川工業": "菊川工業",
            "C1 パレット台": "C1",                       # 全角で打たれても同じ
        }
        for symbol, base in cases.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(base_symbol(symbol), base)

    def test_印が無い記号はそのまま(self) -> None:
        for symbol in ("C1", "Z8", "EXタイト 熱処理輸出", "両端脚追加", "", None):
            with self.subTest(symbol=symbol):
                self.assertEqual(base_symbol(symbol), (symbol or "").strip())


class HinmeiTests(unittest.TestCase):
    def test_品名は前と同じ(self) -> None:
        """倉庫へ送った品名(共有の 資材パレット注文管理 にある実際の形)と同じになる。"""
        self.assertEqual(lot_service.build_hinmei("スカシ", "P7 ﾊﾟﾚｯﾄ台", 1350, 1350),
                         "スカシ　P7　1350x1350")
        self.assertEqual(lot_service.build_hinmei("タイト", " ﾊﾟﾚｯﾄ台 マトリックス", 1400, 3150),
                         "タイト　　1400x3150")
        self.assertEqual(lot_service.build_hinmei("タイト", "菊川工業 ﾊﾟﾚｯﾄ台 菊川工業", 1100, 2000),
                         "タイト　菊川工業　1100x2000")

    def test_出さない記号は新しい書き方でも出さない(self) -> None:
        self.assertEqual(lot_service.filter_pallet_symbol("A8 ﾊﾟﾚｯﾄ台 標準5x10(新5x10)"), "")
        self.assertEqual(lot_service.filter_pallet_symbol("G1 ﾊﾟﾚｯﾄ蓋"), "")
        self.assertEqual(lot_service.filter_pallet_symbol("A8"), "")           # 前の書き方も
        self.assertEqual(lot_service.build_hinmei("5×10", "A8 ﾊﾟﾚｯﾄ台 標準5x10(新5x10)", 1550, 3100),
                         "5×10　　1550x3100")


class Thickness5x10Tests(unittest.TestCase):
    def test_薄板は強度UPだけ_厚板は強度UP以外(self) -> None:
        strong = "強度UP ﾊﾟﾚｯﾄ台 薄板用5x10"
        normal = "A8 ﾊﾟﾚｯﾄ台 標準5x10(新5x10)"
        self.assertEqual(_thickness_5x10_ok(strong, 10.0), (True, False))
        self.assertEqual(_thickness_5x10_ok(normal, 10.0), (False, False))
        self.assertEqual(_thickness_5x10_ok(strong, 20.0), (False, False))
        self.assertEqual(_thickness_5x10_ok(normal, 20.0), (True, False))
        self.assertEqual(_thickness_5x10_ok("強度UP", 10.0), (True, False))   # 前の書き方も


class FitRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        db.apply_schema(self.conn)

    def add(self, number: int, industry: str, symbol: str, width: int, length: int,
            unit: str = "台") -> None:
        self.conn.execute(
            "INSERT INTO PalletMaster (管理番号, 幅, 丈, 業界, 記号, 位置, 単位, 更新日時)"
            " VALUES (?, ?, ?, ?, ?, '', ?, '2026-10-13 00:00:00')",
            (number, width, length, industry, symbol, unit))

    def ranges(self, number: int) -> tuple:
        r = self.conn.execute("SELECT 巾適合min, 巾適合max, 丈適合min, 丈適合max, 桁数, 脚数"
                              " FROM PalletMaster WHERE 管理番号 = ?", (number,)).fetchone()
        return tuple(r)

    def test_新しい書き方でも固定表の記号に当たる(self) -> None:
        self.add(1, "スカシ", "C1", 510, 770)                    # 前の書き方
        self.add(2, "スカシ", "C1 ﾊﾟﾚｯﾄ台", 510, 770)
        self.add(3, "スカシ", "C1 ﾊﾟﾚｯﾄ蓋", 510, 770, unit="枚")
        self.add(4, "5×10", "強度UP", 1550, 3100)
        self.add(5, "5×10", "強度UP ﾊﾟﾚｯﾄ台 薄板用5x10", 1550, 3100)
        summary = svc.recompute_fit_ranges(self.conn)
        self.assertTrue(summary.ok, summary)
        self.assertEqual(self.ranges(1), (410, 490, 600, 750, 3, 3))
        self.assertEqual(self.ranges(2), self.ranges(1))
        self.assertEqual(self.ranges(3), self.ranges(1))
        self.assertEqual(self.ranges(4)[:4], (1485, 1540, 2700, 3090))  # 5×10|強度UP の組合せ
        self.assertEqual(self.ranges(5)[:4], self.ranges(4)[:4])
        self.assertEqual(summary.loose, [])                    # 表記ゆれとして数えない
        self.assertEqual(svc.loose_key_rows(self.conn), [])


if __name__ == "__main__":
    unittest.main()


class ListLabelTests(unittest.TestCase):
    """資材選択の一覧に出す「記号(品種・備考)」(表示だけ)。

    現場の声:「記号は記号として扱うべき。リストには 記号・備考・台はﾊﾟﾚｯﾄ台・枚はﾊﾟﾚｯﾄ蓋 を混ぜたものを表示」。
    """

    def test_記号と品種と備考をつなぐ(self) -> None:
        from packaging_tool.pallet_symbol import list_label
        cases = [
            (("C1", "台", ""), "C1 ﾊﾟﾚｯﾄ台"),
            (("C1", "枚", None), "C1 ﾊﾟﾚｯﾄ蓋"),
            (("EXﾀｲﾄ", "枚", "熱処理輸出"), "EXﾀｲﾄ ﾊﾟﾚｯﾄ蓋 熱処理輸出"),
            (("", "台", "マトリックス"), "ﾊﾟﾚｯﾄ台 マトリックス"),
            (("脚75", "組", ""), "脚75"),                          # 組は品種を付けない
            (("脚60", "台", "脚60"), "脚60 ﾊﾟﾚｯﾄ台"),               # 備考が記号と同じなら重ねない
            (("", "", ""), ""),
            # 記号にまだ品種の印が混ざっている(マスタを戻す前)。印を重ねない
            (("EXﾀｲﾄ ﾊﾟﾚｯﾄ台 熱処理輸出", "台", "熱処理輸出"), "EXﾀｲﾄ ﾊﾟﾚｯﾄ台 熱処理輸出"),
        ]
        for (symbol, unit, remark), want in cases:
            with self.subTest(symbol=symbol, unit=unit, remark=remark):
                self.assertEqual(list_label(symbol, unit, remark), want)


class ListColumnTests(unittest.TestCase):
    """一覧の列は「記号(品種・備考)」。品名と行の見分けはマスタの記号のまま。"""

    def test_一覧の列と行(self) -> None:
        from packaging_tool.pallet_common import _row_to_pallet_size_row
        from packaging_tool.presenters import selection as presenter
        labels = [label for label, _key, _num in presenter.PALLET_COLUMNS]
        self.assertIn("記号(品種・備考)", labels)
        self.assertNotIn("記号", labels)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        conn.execute("INSERT INTO PalletMaster (管理番号, 幅, 丈, 業界, 記号, 位置, 単位, コード, 備考, 更新日時)"
                     " VALUES (7, 1110, 2530, 'タイト', 'EXﾀｲﾄ', '', '枚', '820527', '熱処理輸出', '')")
        row = _row_to_pallet_size_row(conn.execute("SELECT * FROM PalletMaster").fetchone())
        shown = presenter._pallet_row(row)
        self.assertIn("EXﾀｲﾄ ﾊﾟﾚｯﾄ蓋 熱処理輸出", shown.values)
        self.assertEqual(shown.symbol, "EXﾀｲﾄ")                   # 行の見分けはマスタの記号
        self.assertTrue(shown.is_ex)
        self.assertEqual(lot_service.build_hinmei("タイト", row.symbol, 1110, 2530),
                         "タイト　EXﾀｲﾄ　1110x2530")                # 品名に品種・備考は入らない
