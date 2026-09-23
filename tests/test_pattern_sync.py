"""実績を全端末で共有する(`pattern_sync`)

端末A・端末Bの手元DBと、共有の取り込み元(梱包資材マスタ.sqlite3)を
1つずつ用意して、保存・読んだ回数・削除が行き来することを確かめる。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import config, db, source_db  # noqa: E402
from packaging_tool import pattern_store as ps  # noqa: E402
from packaging_tool import pattern_sync as sync  # noqa: E402

H = config.TBL_PT_HEADER


def terminal() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


SEL = [{"区分": "下用", "行順": 1, "幅": 1150, "丈": 2500, "枚数": 1, "タグ": "主"},
       {"区分": "上用", "行順": 1, "幅": 1122, "丈": 2500, "枚数": 1, "タグ": "丈補填"}]
PLACE = [{"区分": "下用", "順番": 1, "板ID": 1, "インスタンスID": "下用_1",
          "座標X": 0, "座標Y": 14, "幅": 1122, "丈": 2500, "元幅": 1150,
          "元丈": 2500, "補填": 0}]
CUT = [{"種別": "幅カット", "キー": "1150x2500", "値": 1122}]


def save(conn, **over) -> int:
    header = {"パレット幅": 1150, "パレット丈": 2650, "製品幅": 1122,
              "製品丈": 2502, "配置方式": "別案A", "LotNo": "L1"}
    header.update(over)
    return ps.save_pattern_snapshot(conn, header, SEL, PLACE, CUT)


class SyncTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "梱包資材マスタ.sqlite3"
        with sqlite3.connect(self.path) as raw:
            raw.execute("CREATE TABLE PalletMaster (管理番号 INTEGER)")
        self.a = terminal()
        self.b = terminal()

    def push(self, conn) -> sync.PushResult:
        return sync.push_to(conn, self.path)

    def source(self, sql, params=()) -> list:
        with sqlite3.connect(self.path) as raw:
            return raw.execute(sql, params).fetchall()


class PushImportTests(SyncTestCase):
    def test_Aで保存した実績がBに届く(self) -> None:
        save(self.a)
        pushed = self.push(self.a)
        self.assertEqual((pushed.sent, pushed.errors), (1, []))
        self.assertEqual(sync.pending(self.a), {})

        outcome = sync.import_from(self.b, self.path)
        self.assertEqual(outcome.imported, 1)
        [row] = ps.get_pattern_list(self.b)
        self.assertFalse(row.unsent)
        self.assertEqual(row.board_summary,
                         "[別案A] 下:1150x2500(1) / 上:1122x2500(1)[丈]")
        snap = ps.load_pattern_snapshot(self.b, row.id)
        self.assertEqual(snap.place_rows[0]["座標Y"], 14)
        self.assertEqual(snap.cut_rows[0]["値"], 1122)

    def test_送り直しても二重に入らない(self) -> None:
        """送れたが控える前に落ちた → 次は送信IDで見つけて控えるだけ。"""
        pid = save(self.a)
        self.push(self.a)
        self.a.execute(f'UPDATE "{H}" SET 取込元実績ID = NULL WHERE 実績ID = ?', (pid,))
        self.a.commit()
        again = self.push(self.a)
        # 断られて止まるのではなく、**見つけて控える**(未送信が残らない)
        self.assertEqual(again.errors, [])
        self.assertEqual(sync.pending(self.a), {})
        self.assertEqual(self.source(f'SELECT COUNT(*) FROM "{H}"')[0][0], 1)
        self.assertEqual(self.source(
            f'SELECT COUNT(*) FROM "{config.TBL_PT_PLACE}"')[0][0], 1)

    def test_明細が入らなければヘッダも入れない(self) -> None:
        """1組まとめて。取り込み元にヘッダだけ残る、を作らない。"""
        with sqlite3.connect(self.path) as raw:
            # 列の足りない明細の表(誰かが先に違う形で作った)
            raw.execute(f'CREATE TABLE "{config.TBL_PT_PLACE}" '
                        "(明細ID INTEGER PRIMARY KEY, 実績ID INTEGER)")
        save(self.a)
        pushed = self.push(self.a)
        self.assertEqual(pushed.sent, 0)
        self.assertTrue(pushed.errors)
        self.assertEqual(self.source(f'SELECT COUNT(*) FROM "{H}"')[0][0], 0)
        self.assertEqual(sync.pending(self.a), {"未送信の実績": 1})

    def test_送るものが無ければ取り込み元に触らない(self) -> None:
        self.assertEqual(self.push(self.a).total, 0)
        self.assertNotIn(H, source_db.list_tables(self.path))


class UsageTests(SyncTestCase):
    def test_読んだ回数は足す_上書きしない(self) -> None:
        save(self.a)
        self.push(self.a)
        sync.import_from(self.b, self.path)
        [row] = ps.get_pattern_list(self.b)
        ps.load_pattern_snapshot(self.b, row.id)
        ps.load_pattern_snapshot(self.b, row.id)
        [row_a] = ps.get_pattern_list(self.a)
        ps.load_pattern_snapshot(self.a, row_a.id)
        self.push(self.b)
        self.push(self.a)
        self.assertEqual(self.source(f'SELECT 使用回数 FROM "{H}"')[0][0], 3)
        self.assertEqual(sync.pending(self.a), {})
        self.assertEqual(sync.pending(self.b), {})


class DeleteTests(SyncTestCase):
    def test_Bで消すとAからも消える(self) -> None:
        save(self.a)
        self.push(self.a)
        sync.import_from(self.b, self.path)
        [row] = ps.get_pattern_list(self.b)
        ps.delete_pattern_by_id(self.b, row.id)
        self.assertEqual(self.push(self.b).deleted, 1)
        for table in (H, config.TBL_PT_SELECT, config.TBL_PT_PLACE, config.TBL_PT_CUT):
            self.assertEqual(self.source(f'SELECT COUNT(*) FROM "{table}"')[0][0], 0, table)
        sync.import_from(self.a, self.path)
        self.assertEqual(ps.get_pattern_list(self.a), [])

    def test_送る前に消したものは取り込み元に何もしない(self) -> None:
        pid = save(self.a)
        ps.delete_pattern_by_id(self.a, pid)
        pushed = self.push(self.a)
        self.assertEqual(pushed.errors, [])
        self.assertEqual(sync.pending(self.a), {})


class ImportGateTests(SyncTestCase):
    def test_送れていないものがあれば入れ替えない(self) -> None:
        save(self.b)
        self.push(self.b)
        save(self.a)                         # A の未送信
        outcome = sync.import_from(self.a, self.path)
        self.assertIn("未送信の実績", outcome.skipped_reason)
        self.assertEqual(len(ps.get_pattern_list(self.a)), 1)   # 消えていない

    def test_取り込み元に表が無ければ消さずに送り直す(self) -> None:
        """取り込み元を作り直した(表が無くなった)とき、手元の実績を守る。"""
        save(self.a)
        self.push(self.a)
        self.path.unlink()
        with sqlite3.connect(self.path) as raw:
            raw.execute("CREATE TABLE PalletMaster (管理番号 INTEGER)")
        outcome = sync.import_from(self.a, self.path)
        self.assertTrue(outcome.missing)
        self.assertEqual(len(ps.get_pattern_list(self.a)), 1)
        self.assertEqual(sync.pending(self.a), {"未送信の実績": 1})
        self.assertEqual(self.push(self.a).sent, 1)


class VbaTableTests(SyncTestCase):
    def test_VBAが作った表に送信IDが無ければ足す(self) -> None:
        cols = ", ".join(f'"{n}" {t}' for n, t in ps.HEADER_COLUMNS if n != "送信ID")
        with sqlite3.connect(self.path) as raw:
            raw.execute(f'CREATE TABLE "{H}" (実績ID INTEGER PRIMARY KEY AUTOINCREMENT, {cols})')
        save(self.a)
        self.assertEqual(self.push(self.a).sent, 1)
        self.assertEqual(len(self.source(f'SELECT 送信ID FROM "{H}"')), 1)


class WriteBackIntegrationTests(SyncTestCase):
    def test_書き戻しで実績も送る_未送信に数える(self) -> None:
        from packaging_tool import sync_writeback
        save(self.a)
        self.assertIn(H, sync_writeback._unsent_writeback_tables(self.a))
        result = sync_writeback.write_back(self.a, self.path)
        self.assertEqual(result.sent.get(H), 1)
        self.assertNotIn(H, sync_writeback._unsent_writeback_tables(self.a))


if __name__ == "__main__":
    unittest.main()


class ImportMasterIntegrationTests(SyncTestCase):
    """取り込み(`import_master`)を通しても、実績が行き来する。"""

    def test_取り込みの前に送り_そのあと受け取る(self) -> None:
        from packaging_tool import data_sync
        save(self.a)
        result = data_sync.import_master(self.a, self.path)
        self.assertEqual(result.imported.get(H), 1)
        [row] = ps.get_pattern_list(self.a)
        self.assertFalse(row.unsent)            # 取り込み元の番号で入り直した
        data_sync.import_master(self.b, self.path)
        self.assertEqual(len(ps.get_pattern_list(self.b)), 1)

    def test_何度取り込んでも増えない(self) -> None:
        from packaging_tool import data_sync
        save(self.a)
        for _ in range(3):
            data_sync.import_master(self.a, self.path)
        self.assertEqual(self.source(f'SELECT COUNT(*) FROM "{H}"')[0][0], 1)
        self.assertEqual(len(ps.get_pattern_list(self.a)), 1)

    def test_送れなければ取り込みを見送り_実績は残る(self) -> None:
        from unittest import mock
        from packaging_tool import data_sync
        save(self.a)
        with mock.patch.object(sync, "push", return_value=sync.PushResult(
                errors=["届きません"])):
            result = data_sync.import_master(self.a, self.path)
        self.assertTrue(any(H in e and "見送り" in e for e in result.errors))
        self.assertEqual(len(ps.get_pattern_list(self.a)), 1)
