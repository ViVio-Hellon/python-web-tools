"""取り違え防止: 手元の中身がどの共有マスタのものかを覚える(`packaging_tool.shared_link`)。

置き場所の設定を別の共有マスタ(本番 ⇔ Test環境)へ変えたとき、前の共有向けに作って
まだ送っていない行を新しい共有へ送ると、2つの共有の中身が混ざる(現場の端末が、
Test環境 には無い表の行を「済」で持っていた)。

【ここで守りたいこと】
1. 同じ共有なら何もしない。はじめての端末は、最初の共有を覚える
2. 違う共有で、送る物が無ければ付け替える(取り込みで中身も入れ替わる)
3. 違う共有で、送る物が残っていれば**新しい共有へ送らない・書き戻す表を入れない**。理由を出す
4. 前の置き場所に戻せば送れて、送り終われば新しい置き場所へ切り替えられる
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import config, data_sync, shared_link  # noqa: E402
from packaging_tool import warehouse_service as wh  # noqa: E402
from tests.test_warehouse_handoff import ORDER, make_local, make_shared  # noqa: E402


class SharedLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="link_"))
        (base / "本番").mkdir()
        (base / "Test環境").mkdir()
        self.prod = base / "本番" / config.MATERIAL_DB_NAME
        self.test = base / "Test環境" / config.MATERIAL_DB_NAME
        make_shared(self.prod)
        make_shared(self.test)
        self.conn = make_local()
        self.addCleanup(self.conn.close)

    def count(self, path: Path, lot: str) -> int:
        with sqlite3.connect(path) as raw:
            return raw.execute(f'SELECT COUNT(*) FROM "{ORDER}" WHERE LotNo = ?', (lot,)).fetchone()[0]

    def order(self, lot: str) -> None:
        made = wh.create_order(self.conn, lot_no=lot, hinmei="テスト品", hatchu_code="X1", tani="台",
                               atu=1, haba=1000, take=2000, hatchu_suu=1, terminal="GENBA-1")
        self.assertTrue(made.ok, made.message)

    def test_はじめての端末は最初の共有を覚える_同じなら何もしない(self) -> None:
        data_sync.import_master(self.conn, self.prod)
        self.assertTrue(shared_link.same_place(shared_link.remembered(self.conn), self.prod))
        link = shared_link.bind(self.conn, self.prod)
        self.assertFalse(link.blocked or link.switched)

    def test_書き方の揺れは同じ場所とみなす(self) -> None:
        self.assertTrue(shared_link.same_place(r"\\SV\共有\a.sqlite3", "//sv/共有/A.sqlite3"))
        self.assertFalse(shared_link.same_place(self.prod, self.test))

    def test_送る物が無ければ付け替える(self) -> None:
        data_sync.import_master(self.conn, self.prod)
        data_sync.import_master(self.conn, self.test)
        self.assertTrue(shared_link.same_place(shared_link.remembered(self.conn), self.test))
        self.order("NEW")
        data_sync.write_back(self.conn, self.test)
        self.assertEqual((self.count(self.test, "NEW"), self.count(self.prod, "NEW")), (1, 0))

    def test_前の共有向けの物が残っていれば新しい共有へ送らない(self) -> None:
        data_sync.import_master(self.conn, self.prod)
        self.order("NEW")                      # 本番向けに作った(まだ送っていない)
        result = data_sync.write_back(self.conn, self.test)
        self.assertEqual(self.count(self.test, "NEW"), 0)
        self.assertIn("置き場所が変わりました", result.skipped_reason)
        self.assertIn("1件", result.skipped_reason)
        # 取り込みも、書き戻す表は入れない(中身が混ざるため)
        imported = data_sync.import_master(self.conn, self.test)
        self.assertNotIn(ORDER, imported.imported)
        self.assertTrue(any("置き場所が変わりました" in e for e in imported.errors), imported.errors)
        self.assertEqual(self.conn.execute(f'SELECT COUNT(*) FROM "{ORDER}" WHERE LotNo = "NEW"')
                         .fetchone()[0], 1)          # 手元の発注は消えていない

    def test_前の置き場所に戻せば送れて_そのあと切り替えられる(self) -> None:
        data_sync.import_master(self.conn, self.prod)
        self.order("NEW")
        data_sync.write_back(self.conn, self.test)              # 断られる
        data_sync.write_back(self.conn, self.prod)              # 前の置き場所へは送れる
        self.assertEqual(self.count(self.prod, "NEW"), 1)
        data_sync.import_master(self.conn, self.test)           # 送る物が無いので切り替わる
        self.assertTrue(shared_link.same_place(shared_link.remembered(self.conn), self.test))
        self.assertEqual(self.count(self.test, "NEW"), 0)


if __name__ == "__main__":
    unittest.main()
