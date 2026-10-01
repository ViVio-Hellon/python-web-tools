"""マスタ管理の見出しクリックで並び替える ── **数値は数値として、空欄は最後に。**

取り込み元の表は列に型が付いていないことが多く(変換ツールは型を書かずに
表を作る)、数値が文字で入っている。そのまま並べると辞書順になり、
「200 が 1100 より後ろ」「空欄が途中に混ざる」で、押しても並び替わって
いないように見えた(現場の声:「列名クリックでソートするようにしてください」)。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import master_browse  # noqa: E402


def make_untyped(directory: Path) -> Path:
    """変換ツールと同じく、**列に型を書かない**表。値は文字・数値・空が混ざる。"""
    path = directory / "並び替え.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE "PalletMaster" ("記号", "幅", "丈", "備考")')
    conn.executemany('INSERT INTO "PalletMaster" VALUES (?,?,?,?)', [
        ("A", "1100", "1100", ""),
        ("B", "200", "300", "x"),
        ("C", 1000, 950, None),        # 数値で入っている値も混ざる
        ("D", "", "1200", "y"),
        ("E", "95", "80", "z"),
        ("F", "1100.5", "10", ""),
        ("G", None, "5", "w"),
        ("H", "12A", "7", ""),         # 数値ではない(文字のまま比べる)
        ("I", " -5 ", "6", "v"),       # 前後の空白・負の数
        ("J", "1-2", "8", "u"),        # 数値ではない
    ])
    conn.commit()
    conn.close()
    return path


class MasterSortTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = make_untyped(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def keys(self, column: str, direction: str) -> list[str]:
        page = master_browse.page(self.path, "PalletMaster", sort=column, sort_dir=direction)
        self.assertEqual(page.error, "")
        self.assertEqual((page.sort, page.sort_dir), (column, direction))
        return [r["記号"] for r in page.rows]

    def test_文字で入った数値も大きさの順に並ぶ(self) -> None:
        # -5 < 95 < 200 < 1000 < 1100 < 1100.5、そのあと文字(1-2, 12A)、最後に空欄
        self.assertEqual(self.keys("幅", "asc"), ["I", "E", "B", "C", "A", "F", "J", "H", "D", "G"])

    def test_降順は文字が先で数値は大きい順_空欄は最後のまま(self) -> None:
        self.assertEqual(self.keys("幅", "desc"), ["H", "J", "F", "A", "C", "B", "E", "I", "D", "G"])

    def test_数値だけの列(self) -> None:
        self.assertEqual(self.keys("丈", "asc"), ["G", "I", "H", "J", "F", "E", "B", "C", "A", "D"])

    def test_空欄は向きに関係なく最後で_取り込み順(self) -> None:
        asc = self.keys("備考", "asc")
        desc = self.keys("備考", "desc")
        blanks = ["A", "C", "F", "H"]                                       # ''・NULL・''・''
        self.assertEqual(asc[-4:], blanks)
        self.assertEqual(desc[-4:], blanks)
        self.assertEqual(asc[:6], ["J", "I", "G", "B", "D", "E"])           # u v w x y z
        self.assertEqual(desc[:6], ["E", "D", "B", "G", "I", "J"])

    def test_文字の列はふつうに文字の順(self) -> None:
        self.assertEqual(self.keys("記号", "asc"), list("ABCDEFGHIJ"))
        self.assertEqual(self.keys("記号", "desc"), list("JIHGFEDCBA"))

    def test_無い列を指したら取り込み順に戻す(self) -> None:
        page = master_browse.page(self.path, "PalletMaster", sort="無い列", sort_dir="asc")
        self.assertEqual(page.sort, "")
        self.assertEqual([r["記号"] for r in page.rows], list("ABCDEFGHIJ"))

    def test_絞り込みと一緒に使える(self) -> None:
        page = master_browse.page(self.path, "PalletMaster", query="1", sort="幅", sort_dir="asc")
        # D は 丈=1200 で当たり、幅が空欄なので最後
        self.assertEqual([r["記号"] for r in page.rows], ["C", "A", "F", "J", "H", "D"])


if __name__ == "__main__":
    unittest.main()
