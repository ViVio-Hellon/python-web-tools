"""**「無い」とするのは、読めたうえで本当に無いと確かめたときだけ。読めないときは書かない・消さない・覚えない**
(VER4.8.122)。

現場の声(別ツールで起きたこと):「設定ファイルが読めないと空とみなしていた。そのため保存するたびにほかの設定が
消え、配布設定で PC の値が上書きされていた」「表を『無い』とみなして共有に作りに行っていた。作れないと手元の表を
消していた」「書くときに、一瞬消えたマスタの場所に空のファイルを作ることがあった」「第1パスに届かないのを
『ファイルが無い』とみなし、第2パスや手元の写しで手元の表をまるごと入れ替えていた」…
「無い場合に作る・書く の処理があるところは危ない」。

ここでは、このツールで同じ形だったところを1つずつ確かめる(設定ファイル・配置図・仕掛台帳は
`test_user_settings` / `test_web_layout` / `test_lot_second_dir`)。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from packaging_tool import (admin_password, distribution, master_admin, master_common, source_db,
                            sync_import, sync_sources, trace_log, user_settings, warehouse_service)
from packaging_tool.sync_sources import SyncError


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


class _Locked:
    """読むと「混んでいる」で断る接続。"""

    row_factory = None

    def __init__(self, message: str = "database is locked") -> None:
        self.message = message

    def execute(self, *_a, **_k):
        raise sqlite3.OperationalError(self.message)


class NoEmptyFileTests(TmpCase):
    """共有のマスタが一瞬見えないときに、その場所へ空のファイルを作らない。"""

    def test_書くために開くときも無いファイルは作らない(self) -> None:
        missing = self.root / "梱包資材マスタ.sqlite3"
        for attempt in (source_db._try_uri, source_db._try_plain):
            with self.assertRaises(sqlite3.Error):
                attempt(missing, read_only=False)
            self.assertFalse(missing.exists(), attempt.__name__)
        with self.assertRaises(source_db.SourceError):
            source_db.connect(missing)
        self.assertFalse(missing.exists())

    def test_控えを取るときも元を作らない(self) -> None:
        from packaging_tool import dedupe
        missing = self.root / "梱包資材マスタ.sqlite3"
        with self.assertRaises(sqlite3.Error):
            dedupe.backup(missing)
        self.assertFalse(missing.exists())


class CandidateTests(TmpCase):
    """既定の名前が見えないときに、ほかのファイルを梱包資材マスタとして使わない。"""

    def test_控えの_bak_は梱包資材マスタの候補にしない(self) -> None:
        with closing(sqlite3.connect(self.root / "梱包資材マスタ.bak-20261029120000.sqlite3")) as raw:
            raw.execute("CREATE TABLE PalletMaster (管理番号 INTEGER)")
        self.assertIsNone(sync_sources.find_material_db(self.root))

    def test_フォルダの一覧を読めなければほかの候補を使わない(self) -> None:
        with closing(sqlite3.connect(self.root / "別の名前.sqlite3")) as raw:
            raw.execute("CREATE TABLE PalletMaster (管理番号 INTEGER)")
        self.assertIsNotNone(sync_sources.find_material_db(self.root))          # 読めれば候補を使う
        with mock.patch.object(source_db, "listing_of", return_value=None):
            self.assertIsNone(sync_sources.find_material_db(self.root))

    def test_拡張子違いは一覧で決める(self) -> None:
        with closing(sqlite3.connect(self.root / "SIKALOT.db")) as raw:
            raw.execute("CREATE TABLE 仕掛 (a)")
        self.assertEqual(source_db.find(self.root, "SIKALOT.sqlite3"), self.root / "SIKALOT.db")
        with mock.patch.object(source_db, "listing_of", return_value=None):
            self.assertIsNone(source_db.find(self.root, "SIKALOT.sqlite3"))


class ImportTests(unittest.TestCase):
    """読めなかった表を「無い」にせず、取り込んだと覚えない。"""

    def run_import(self, message: str) -> sync_import.ImportResult:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with mock.patch.object(sync_sources, "read_table", side_effect=SyncError(message)), \
                mock.patch.object(sync_sources, "note_source_read") as noted:
            result = sync_import.import_tables(conn, Path("x.sqlite3"), {"予定ロット": []},
                                               optional=frozenset({"予定ロット"}))
        self.noted = noted.called
        return result

    def test_任意の表でも読めなければ読めなかったと言う(self) -> None:
        result = self.run_import("database is locked")
        self.assertEqual(result.unreadable, ["予定ロット"])
        self.assertEqual(result.missing_optional, [])
        self.assertTrue(result.errors)

    def test_無いと確かめた任意の表は無いと言う(self) -> None:
        result = self.run_import("x.sqlite3 の 予定ロット を読めませんでした: no such table: 予定ロット")
        self.assertEqual(result.unreadable, [])
        self.assertEqual(result.missing_optional, ["予定ロット"])


class StrictReadTests(unittest.TestCase):
    def test_足した列の記録を読めなければ作り直しの判断に使わない(self) -> None:
        with mock.patch.object(source_db, "read_query", side_effect=source_db.SourceError("database is locked")):
            self.assertEqual(master_common.added_columns(Path("m.sqlite3"), "PalletMaster"), {})  # 画面用
            with self.assertRaises(source_db.SourceError):
                master_common.added_columns(Path("m.sqlite3"), "PalletMaster", strict=True)
        with mock.patch.object(source_db, "read_query",
                               side_effect=source_db.SourceError("no such table: ツールで足した列")):
            self.assertEqual(master_common.added_columns(Path("m.sqlite3"), "PalletMaster", strict=True), {})

    def test_列を読めなければ_strict_では断る(self) -> None:
        with mock.patch.object(source_db, "_opened", side_effect=source_db.SourceError("database is locked")):
            self.assertEqual(source_db.columns(Path("m.sqlite3"), "PalletMaster"), [])
            with self.assertRaises(source_db.SourceError):
                source_db.columns(Path("m.sqlite3"), "PalletMaster", strict=True)

    def test_送信済みの発注を読めなければ送っていないとは言わない(self) -> None:
        with self.assertRaises(warehouse_service.SentUnreadable):
            warehouse_service.already_sent(_Locked(), "H1", strict=True)
        self.assertEqual(warehouse_service.already_sent(_Locked("no such table: x"), "H1", strict=True), [])

    def test_予定ロット_切断依頼を読めなければ1件も無いとは言わない(self) -> None:
        from packaging_tool import cut_requests, lot_plans
        for load in (lot_plans._events, cut_requests._load):
            with self.assertRaises(sqlite3.OperationalError):
                load(_Locked())
            self.assertEqual(load(_Locked("no such table: x")), [])

    def test_何行あるか数えられなければ最後の行を消さない(self) -> None:
        table = next(iter(master_admin.KEEP_LAST_ROW))
        with mock.patch.object(source_db, "read_query", side_effect=source_db.SourceError("database is locked")):
            self.assertIn("確かめられない", master_admin._last_row_why(Path("m.sqlite3"), table))


class SettingsUnreadableTests(TmpCase):
    def test_この端末の設定を読めなければ配布設定を読まない(self) -> None:
        bundle = mock.Mock(settings={"position": "HVC"}, maps={})
        with mock.patch.object(user_settings, "load_all_strict",
                               side_effect=user_settings.SettingsUnreadable("読めない")), \
                mock.patch.object(user_settings, "save") as saved:
            result = distribution._apply(bundle, overwrite=False)
        self.assertFalse(result.ok)
        saved.assert_not_called()

    def test_設定を読めなければ既定の管理者パスワードで通さない(self) -> None:
        from packaging_tool import config
        with mock.patch.object(user_settings, "get", return_value=None), \
                mock.patch.object(user_settings, "readable", return_value=(False, "読めない")):
            self.assertFalse(admin_password.verify(config.ADMIN_PASSWORD))


class LogFolderTests(TmpCase):
    def test_指定したフォルダが無ければ作らない(self) -> None:
        missing = self.root / "届かない共有"
        why = trace_log._writable(missing / "packagingtool")
        self.assertIn("見つかりません", why)
        self.assertFalse(missing.exists())

    def test_指定したフォルダがあればその下にツールのフォルダを作る(self) -> None:
        shared = self.root / "共有"
        shared.mkdir()
        self.assertEqual(trace_log._writable(shared / "packagingtool"), "")
        self.assertTrue((shared / "packagingtool").is_dir())


class WriteBackTests(unittest.TestCase):
    def test_送り先の列を確かめられなければ送らない(self) -> None:
        """前は読めないのを「列が無い」とみなし、発注キーなどを抜いた行を送って、送った印まで付けていた。"""
        import dataclasses
        from packaging_tool import outbox_sync, sync_writeback
        spec = next(s for s in sync_writeback.WRITEBACK_SPECS if s.optional_columns)
        spec = dataclasses.replace(spec, use_op_id_guard=False, identity_columns=())
        source = mock.Mock()
        source.query.side_effect = source_db.SourceError("database is locked")
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with mock.patch.object(outbox_sync, "claim_rows") as claim:
            result = outbox_sync.write_back(conn, source, [spec])
        claim.assert_not_called()                                   # 送る行を取りにも行かない
        self.assertTrue(any("確かめられない" in e for e in result.errors), result.errors)
        source.insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
