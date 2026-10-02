"""実績を全端末で共有する(`pattern_sync`)

端末A・端末Bの手元DBと、共有の取り込み元(梱包資材マスタ.sqlite3)を
1つずつ用意して、保存・読んだ回数・削除が行き来することを確かめる。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
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
        # **閉じる。** `with sqlite3.connect()` は閉じないので、Windows では
        # 一時フォルダを片付けるときに「使用中」で消せなかった
        with closing(sqlite3.connect(self.path)) as raw, raw:
            raw.execute("CREATE TABLE PalletMaster (管理番号 INTEGER)")
        self.a = terminal()
        self.b = terminal()

    def push(self, conn) -> sync.PushResult:
        return sync.push_to(conn, self.path)

    def source(self, sql, params=()) -> list:
        with closing(sqlite3.connect(self.path)) as raw, raw:
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
        with closing(sqlite3.connect(self.path)) as raw, raw:
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


class ConcurrentUsageTests(SyncTestCase):
    def test_同じ端末で書き戻しが重なっても読んだ回数は1回ぶん(self) -> None:
        """操作のあとの裏の送信と、倉庫連携の見張りなどが**同時に**走る。

        以前はどちらも同じ「未反映 1」を読んで足していた(遅い共有を
        真似た試験で +1 のはずが +4)。ここでは1本目が共有へ書いている
        最中に2本目を走らせる。
        """
        save(self.a)
        self.push(self.a)
        [row] = ps.get_pattern_list(self.a)
        ps.load_pattern_snapshot(self.a, row.id)
        before = self.source(f'SELECT 使用回数 FROM "{H}"')[0][0]

        source = source_db.connect(self.path)
        self.addCleanup(source.close)
        original = source.execute
        nested = {"done": False}

        def execute(sql, params=()):
            if not nested["done"]:
                nested["done"] = True
                sync._push_usage(self.a, source, sync.PushResult())   # 2本目
            return original(sql, params)

        source.execute = execute
        sync._push_usage(self.a, source, sync.PushResult())           # 1本目
        self.assertEqual(self.source(f'SELECT 使用回数 FROM "{H}"')[0][0], before + 1)
        self.assertEqual(sync.pending(self.a), {})

    def test_届かなかった分は戻して次に送る(self) -> None:
        save(self.a)
        self.push(self.a)
        [row] = ps.get_pattern_list(self.a)
        ps.load_pattern_snapshot(self.a, row.id)
        before = self.source(f'SELECT 使用回数 FROM "{H}"')[0][0]
        source = source_db.connect(self.path)
        self.addCleanup(source.close)

        def fail(sql, params=()):
            raise source_db.SourceError("database is locked")

        original, source.execute = source.execute, fail
        result = sync.PushResult()
        sync._push_usage(self.a, source, result)
        self.assertTrue(result.errors)
        source.execute = original
        sync._push_usage(self.a, source, sync.PushResult())
        self.assertEqual(self.source(f'SELECT 使用回数 FROM "{H}"')[0][0], before + 1)


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
        with closing(sqlite3.connect(self.path)) as raw, raw:
            raw.execute("CREATE TABLE PalletMaster (管理番号 INTEGER)")
        outcome = sync.import_from(self.a, self.path)
        self.assertTrue(outcome.missing)
        self.assertEqual(len(ps.get_pattern_list(self.a)), 1)
        self.assertEqual(sync.pending(self.a), {"未送信の実績": 1})
        self.assertEqual(self.push(self.a).sent, 1)


class VbaTableTests(SyncTestCase):
    def test_VBAが作った表に送信IDが無ければ足す(self) -> None:
        cols = ", ".join(f'"{n}" {t}' for n, t in ps.HEADER_COLUMNS if n != "送信ID")
        with closing(sqlite3.connect(self.path)) as raw, raw:
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


# 現場の取り込み元にある表の形(VBA / Access から作られたもの)。
# **型も主キーも自動採番も無い**(実績ID はただの列)、送信ID も無い
VBA_SCHEMA = [
    f'CREATE TABLE "{config.TBL_PT_HEADER}" ("実績ID", "保存形式", "登録日時", "更新日時",'
    ' "使用回数", "パレット幅", "パレット丈", "製品幅", "製品丈", "製品回転",'
    ' "ボード種別", "配置方式", "狭幅下", "狭幅上", "保護材", "アングル",'
    ' "プロテック確定値", "LotNo", "拠点")',
    f'CREATE TABLE "{config.TBL_PT_SELECT}" ("実績ID", "区分", "行順", "幅", "丈", "枚数", "タグ")',
    f'CREATE TABLE "{config.TBL_PT_PLACE}" ("実績ID", "区分", "順番", "板ID",'
    ' "インスタンスID", "座標X", "座標Y", "幅", "丈", "元幅", "元丈", "補填")',
    f'CREATE TABLE "{config.TBL_PT_CUT}" ("実績ID", "種別", "キー", "値")',
]


class VbaSourceTests(SyncTestCase):
    """VBA が保存した実績が入っている取り込み元と行き来する。"""

    def setUp(self) -> None:
        super().setUp()
        with closing(sqlite3.connect(self.path)) as raw, raw:
            for sql in VBA_SCHEMA:
                raw.execute(sql)
            raw.execute(f'INSERT INTO "{H}" VALUES (1, 2, "2026-09-23T17:54:13",'
                        ' "2026-09-23T17:54:33", 1, 1350, 1450, 1322, 1342, 0,'
                        ' "ハードボード", "別案A", 0, 0, "アングル", NULL, NULL,'
                        ' "H7085M0", "L1")')
            raw.executemany(f'INSERT INTO "{config.TBL_PT_SELECT}" VALUES (?,?,?,?,?,?,?)',
                            [(1, "下用", 1, 1000, 1400, 1, "主"),
                             (1, "上用", 1, 1250, 1250, 1, "主"),
                             (1, "上用", 2, 100, 2000, 1, "丈補填")])
            raw.execute(f'INSERT INTO "{config.TBL_PT_PLACE}" VALUES'
                        ' (1, "上用", 3, 1, "上用_T1_3", 0, 36, 1250, 1250, 1250, 1250, 0)')
            raw.executemany(f'INSERT INTO "{config.TBL_PT_CUT}" VALUES (?,?,?,?)',
                            [(1, "幅カット", "100x2000", 100),
                             (1, "丈カット長", "U_100x2000", 100)])

    def test_VBAが保存した実績を読める(self) -> None:
        self.assertEqual(sync.import_from(self.a, self.path).imported, 1)
        [row] = ps.get_pattern_list(self.a, 1350, 1450)
        self.assertEqual(row.board_summary,
                         "[別案A] 下:1000x1400(1) / 上:1250x1250(1) 100x2000(1)[丈]")
        snap = ps.load_pattern_snapshot(self.a, row.id)
        self.assertEqual((snap.place_rows[0]["座標X"], snap.place_rows[0]["座標Y"]), (0, 36))
        self.assertEqual({r["キー"] for r in snap.cut_rows}, {"100x2000", "U_100x2000"})

    def test_Pythonで保存した実績に番号を振って送る(self) -> None:
        """実績ID は自動採番ではない。振らないと空のまま入る。"""
        sync.import_from(self.a, self.path)
        save(self.a)
        pushed = self.push(self.a)
        self.assertEqual((pushed.sent, pushed.errors), (1, []))
        self.assertEqual(self.source(f'SELECT 実績ID FROM "{H}" ORDER BY 実績ID'),
                         [(1,), (2,)])
        self.assertEqual(self.source(
            f'SELECT DISTINCT 実績ID FROM "{config.TBL_PT_PLACE}" ORDER BY 実績ID'),
            [(1,), (2,)])
        # VBA の表に送信IDの列を足した(二重防止)
        self.assertEqual(self.source(f'SELECT COUNT(送信ID) FROM "{H}"'), [(1,)])

    def test_VBAの実績を読んだ回数も足す(self) -> None:
        sync.import_from(self.a, self.path)
        [row] = ps.get_pattern_list(self.a)
        ps.load_pattern_snapshot(self.a, row.id)
        self.push(self.a)
        self.assertEqual(self.source(f'SELECT 使用回数 FROM "{H}"'), [(2,)])

    def test_VBAの実績を消せる(self) -> None:
        sync.import_from(self.a, self.path)
        [row] = ps.get_pattern_list(self.a)
        ps.delete_pattern_by_id(self.a, row.id)
        self.assertEqual(self.push(self.a).deleted, 1)
        for table in (H, config.TBL_PT_SELECT, config.TBL_PT_PLACE, config.TBL_PT_CUT):
            self.assertEqual(self.source(f'SELECT COUNT(*) FROM "{table}"'), [(0,)], table)


class RefreshIfChangedTests(SyncTestCase):
    """資材選択が実績の一覧を作るたびに、共有が変わっていれば実績だけ取り込み直す。

    2台での通し試験: 資材選択を開いたままの端末Bには、端末Aが保存した実績が
    いつまでも出なかった(取り込みが起動時と発注の見張りにしか乗っていなかった)。
    """

    def setUp(self) -> None:
        super().setUp()
        sync.forget_seen()
        self.addCleanup(sync.forget_seen)

    def test_共有が変わったときだけ取り込み直す(self) -> None:
        save(self.a)
        self.push(self.a)
        self.assertEqual(sync.refresh_if_changed(self.b, self.path).imported, 1)
        self.assertIsNone(sync.refresh_if_changed(self.b, self.path))   # 変わっていない
        save(self.a, 製品幅=1000)
        self.push(self.a)
        self.assertEqual(sync.refresh_if_changed(self.b, self.path).imported, 2)
        self.assertEqual(len(ps.get_pattern_list(self.b)), 2)

    def test_送れていない実績があれば入れ替えず次にまた見る(self) -> None:
        save(self.a)
        self.push(self.a)
        save(self.b, 製品幅=900)                      # Bにまだ送っていない実績
        got = sync.refresh_if_changed(self.b, self.path)
        self.assertTrue(got.skipped_reason)
        self.assertEqual(len(ps.get_pattern_list(self.b)), 1)   # Bの分は消えない
        self.push(self.b)
        self.assertEqual(sync.refresh_if_changed(self.b, self.path).imported, 2)


class StalePatternIdTests(SyncTestCase):
    """実績の番号は、共有から取り込み直すと振り直される。古い一覧のまま押しても
    別の実績を読み込む・消すことがない(画面は見ていた実績も送る)。"""

    def setUp(self) -> None:
        super().setUp()
        from packaging_tool import selection_session
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        self.session = selection_session.get_session(self.b)
        self.session.admin = True

    def seen(self, pid: int) -> dict:
        row = next(p for p in ps.get_pattern_list(self.b) if p.id == pid)
        return {"registered_at": row.registered_at, "boards": row.board_summary}

    def test_番号が別の実績に替わっていたら読まない消さない(self) -> None:
        pid = save(self.b, 配置方式="通常")
        old = self.seen(pid)
        # 取り込み直しで、同じ番号に別の実績が入った
        self.b.execute(f'UPDATE "{H}" SET 配置方式 = ? WHERE 実績ID = ?', ("別案B", pid))
        self.b.commit()
        got = self.session.load_pattern(pid, seen=old)
        self.assertFalse(got.ok)
        self.assertIn("一覧が新しくなっています", got.message)
        got = self.session.delete_pattern(pid, seen=old)
        self.assertFalse(got.ok)
        self.assertEqual(len(ps.get_pattern_list(self.b)), 1)

    def test_見ていたとおりなら読める消せる(self) -> None:
        pid = save(self.b)
        self.assertTrue(self.session.load_pattern(pid, seen=self.seen(pid)).ok)
        self.assertTrue(self.session.delete_pattern(pid, seen=self.seen(pid)).ok)

    def test_もう無い番号は無いと言う(self) -> None:
        got = self.session.load_pattern(999, seen={"registered_at": "x", "boards": "y"})
        self.assertFalse(got.ok)
        self.assertIn("もうありません", got.message)


class ProvisionalNameTests(unittest.TestCase):
    def test_仮の名前の表で保存した実績を移す(self) -> None:
        """VER2.85.0 は VBA の名前が届く前で、仮の名前の表に保存していた。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cols = ps.HEADER_COLUMNS + ps.HEADER_LOCAL_COLUMNS
        for sql in ps.create_sql("実績ヘッダ", cols, header=True):
            conn.execute(sql)
        for name, (_t, columns, _o) in zip(("実績選定明細", "実績配置明細", "実績カット明細"),
                                          ps.DETAIL_TABLES):
            for sql in ps.create_sql(name, columns, header=False):
                conn.execute(sql)
        conn.execute('INSERT INTO "実績ヘッダ" (保存形式, パレット幅, パレット丈,'
                     ' 取込元実績ID, 送信ID) VALUES (1, 1150, 2650, 7, "abc")')
        conn.execute('INSERT INTO "実績選定明細" (実績ID, 区分, 行順, 幅, 丈, 枚数)'
                     ' VALUES (1, "下用", 1, 1150, 2500, 1)')
        db.apply_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("実績ヘッダ", tables)
        [row] = ps.get_pattern_list(conn)
        self.assertTrue(row.unsent)             # 正しい表へ送り直す
        self.assertEqual(row.board_summary, "[] 下:1150x2500(1) / 上:")


if __name__ == "__main__":
    unittest.main()
