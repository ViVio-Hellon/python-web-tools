"""PalletMaster の発注コードの重複チェックの試験

現場の声:「パレットマスターに発注コードが重複しているものがあった」「重複チェックは必要と感じました」

【ここで守りたいこと】
1. 同じコードの行の組を見つけ、危ない順に 寸法が違う / 単位が違う / 記号・備考だけ違う / 全く同じ に分ける
2. 在庫の行(位置のある行)・空のコード・「---」は数えない
3. 設定の「手元の中身」に数と例を出す
4. マスタ管理で PalletMaster を開くと数と内訳が出て、「発注コードの重複だけ」で重複の行だけをコードの順に出せる
5. 「表を持ってくる」で PalletMaster を入れ替えたら、結果の文でも知らせる
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from packaging_tool import db, master_browse, pallet_dupes


def row(code, width, length, unit="台", symbol="", note="", position="", number=0):
    return {"管理番号": number, "幅": width, "丈": length, "単位": unit, "記号": symbol,
            "備考": note, "コード": code, "位置": position}


class FindTests(unittest.TestCase):
    def test_重複を種類に分け危ない順に並べる(self) -> None:
        groups = pallet_dupes.find([
            row("D", "1080", "1750", "枚", "EXﾀｲﾄ ﾊﾟﾚｯﾄ蓋", "熱処理輸出"),
            row("D", 1080, 1750.0, "枚", "EXﾀｲﾄ ﾊﾟﾚｯﾄ蓋", "熱処理輸出"),   # 書き方が違っても同じ
            row("C", 1040, 1400, "台", "ﾊﾟﾚｯﾄ台", ""),
            row("C", 1040, 1400, "台", "ﾊﾟﾚｯﾄ台 マトリックス", "マトリックス"),
            row("B", 900, 3150, "台"), row("B", 900, 3150, "組"),
            row("A", 850, 2150), row("A", 950, 2050),
            row("only", 1000, 1000),
        ])
        self.assertEqual([(g.code, g.kind) for g in groups], [
            ("A", pallet_dupes.KIND_SIZE), ("B", pallet_dupes.KIND_UNIT),
            ("C", pallet_dupes.KIND_NOTE), ("D", pallet_dupes.KIND_SAME)])
        self.assertEqual(groups[0].describe(), "A(850×2150 台 / 950×2050 台)")
        self.assertEqual(pallet_dupes.summary(groups),
                         "4種(8行)。寸法が違う 1・単位が違う 1・記号・備考だけ違う 1・全く同じ 1")

    def test_在庫の行と空のコードは数えない(self) -> None:
        groups = pallet_dupes.find([
            row("A", 850, 2150), row("A", 850, 2150, position="A-1"),   # 在庫の行
            row("", 900, 900), row("", 950, 950),
            row("---", 900, 900), row("---", 950, 950),
            row(" A ", 1, 1, position=" "),                             # 空白だけの位置は在庫ではない
        ])
        self.assertEqual([(g.code, len(g.rows)) for g in groups], [("A", 2)])

    def test_重複が無ければ空(self) -> None:
        self.assertEqual(pallet_dupes.find([row("A", 1, 1), row("B", 1, 1)]), [])
        self.assertEqual(pallet_dupes.summary([]), "")


class SharedFileTests(unittest.TestCase):
    """共有(取り込み元)の PalletMaster から読む・マスタ管理の「重複だけ」。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="dupes_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE PalletMaster (管理番号, 幅, 丈, 業界, 記号, 位置, 在庫数,"
                     " コード, 単位, 備考)")
        conn.executemany("INSERT INTO PalletMaster VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (192, "850", "2150", "タイト", "ﾊﾟﾚｯﾄ台", "", "", "057417", "台", None),
            (1, "1000", "1000", "タイト", "", "", "", "000001", "台", None),
            (207, "950", "2050", "タイト", "ﾊﾟﾚｯﾄ台", "", "", "057417", "台", None),
            (455, "900", "3150", "タイト", "脚60 ﾊﾟﾚｯﾄ台", "", "", "058817", "台", "脚60"),
            (1064, "900", "3150", "タイト", "脚60,蓋15", "", "", "058817", "組", None),
        ])
        conn.commit()
        conn.close()

    def test_共有から読む(self) -> None:
        groups = pallet_dupes.from_path(self.path)
        self.assertEqual([(g.code, g.kind, [r["管理番号"] for r in g.rows]) for g in groups], [
            ("057417", pallet_dupes.KIND_SIZE, [192, 207]),
            ("058817", pallet_dupes.KIND_UNIT, [455, 1064])])

    def test_マスタ管理で数と内訳を出し重複だけを並べる(self) -> None:
        page = master_browse.page(self.path, "PalletMaster")
        self.assertEqual(page.total, 5)
        self.assertTrue(page.dupes_available)
        self.assertFalse(page.dupes_on)
        self.assertEqual(page.dupes_text, "2種(4行)。寸法が違う 1・単位が違う 1")
        page = master_browse.page(self.path, "PalletMaster", dupes=True)
        self.assertTrue(page.dupes_on)
        self.assertEqual([(r["コード"], r["管理番号"]) for r in page.rows],
                         [("057417", 192), ("057417", 207), ("058817", 455), ("058817", 1064)])
        # 絞り込みと一緒に使える
        page = master_browse.page(self.path, "PalletMaster", dupes=True, query="058817")
        self.assertEqual([r["管理番号"] for r in page.rows], [455, 1064])
        self.assertEqual(page.to_dict()["dupes"]["on"], True)

    def test_重複が無ければボタンも出さない(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE PalletMaster SET コード = '0' || 管理番号")
        conn.commit()
        conn.close()
        page = master_browse.page(self.path, "PalletMaster", dupes=True)
        self.assertFalse(page.dupes_available)
        self.assertFalse(page.dupes_on)
        self.assertEqual(page.total, 5)                  # 頼まれても全部を出す

    def test_ほかの表では出さない(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE BoardMaster (管理番号, コード)")
        conn.executemany("INSERT INTO BoardMaster VALUES (?, ?)", [(1, "X"), (2, "X")])
        conn.commit()
        conn.close()
        page = master_browse.page(self.path, "BoardMaster", dupes=True)
        self.assertFalse(page.dupes_available)
        self.assertEqual(page.total, 2)


class SettingsCheckTests(unittest.TestCase):
    """設定の「手元の中身」。"""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        db.apply_schema(self.conn)

    def add(self, width: int, length: int, code: str, unit: str = "台") -> None:
        """型録の行(位置なし)を足す。"""
        self.conn.execute("INSERT INTO PalletMaster (幅, 丈, 業界, 記号, コード, 単位, 位置, 更新日時)"
                          " VALUES (?, ?, 'タイト', '', ?, ?, '', '2026-10-14 00:00:00')",
                          (width, length, code, unit))
        self.conn.commit()

    def checks(self) -> dict:
        from packaging_tool.presenters import settings
        section = settings.Section("梱包資材マスタ", mark="材")
        settings._check_local_master(section, self.conn)
        return {c.label: c for c in section.checks}

    def test_重複があれば数と例を出す(self) -> None:
        self.add(850, 2150, "057417")
        self.add(950, 2050, "057417")
        self.add(900, 3150, "058817")
        check = self.checks()["発注コードの重複"]
        self.assertEqual(check.value, "パレットマスタに 1種(2行)")
        self.assertIn("057417(850×2150 台 / 950×2050 台)", check.detail)
        self.assertIn("「発注コードの重複だけ」", check.detail)

    def test_重複が無ければ出さない(self) -> None:
        self.add(850, 2150, "057417")
        self.assertNotIn("発注コードの重複", self.checks())


if __name__ == "__main__":
    unittest.main()
