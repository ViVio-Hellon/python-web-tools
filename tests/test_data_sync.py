"""取り込み元(sqlite3)と手元DBの同期のテスト。

取り込み元が sqlite3 になったので、**実ファイルを作って本物を通せる**
(以前は Access ドライバか mdbtools が要り、読み取りは偽物で代用して
いた)。ここでは探索・取り込み・書き戻し・同期記録を確かめる。
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from packaging_tool import config, data_sync, db, import_specs, outbox_sync


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def spec_for(sqlite_table: str) -> outbox_sync.WriteBackSpec:
    """data_sync.WRITEBACK_SPECS からテーブル名で1件引く(テスト用)。"""
    return next(s for s in data_sync.WRITEBACK_SPECS if s.sqlite_table == sqlite_table)


class FindFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(__file__).resolve().parent / "_tmp_source"
        self._tmp.mkdir(exist_ok=True)

    def tearDown(self) -> None:
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def test_finds_the_master_by_its_usual_name(self):
        (self._tmp / config.MATERIAL_DB_NAME).write_text("")
        self.assertEqual(data_sync.find_material_db(self._tmp).name,
                         config.MATERIAL_DB_NAME)

    def test_falls_back_to_any_other_source(self):
        """ファイル名を変えられていても動くようにする。"""
        (self._tmp / "資材マスタ_2026.sqlite3").write_text("")
        self.assertEqual(data_sync.find_material_db(self._tmp).name,
                         "資材マスタ_2026.sqlite3")

    def test_finds_the_db_suffix_too(self):
        """上流の付け方に合わせてこちらが折れる(.db でも拾う)。"""
        (self._tmp / "梱包資材マスタ.db").write_text("")
        self.assertEqual(data_sync.find_material_db(self._tmp).name,
                         "梱包資材マスタ.db")

    def test_lot_files_are_never_taken_as_the_master(self):
        for name in config.LOT_DB_FILES.values():
            (self._tmp / name).write_text("")
        self.assertIsNone(data_sync.find_material_db(self._tmp))

    def test_missing_master(self):
        self.assertIsNone(data_sync.find_material_db(self._tmp))

    def test_finds_the_three_lot_files(self):
        for name in config.LOT_DB_FILES.values():
            (self._tmp / name).write_text("")
        found = data_sync.find_lot_dbs(self._tmp)
        self.assertEqual(set(found), set(config.LOT_DB_FILES))

    def test_partial_lot_files(self):
        (self._tmp / "SIKALOTNOW.sqlite3").write_text("")
        found = data_sync.find_lot_dbs(self._tmp)
        self.assertEqual(set(found), {"仕掛ロット"})


class ReadTests(unittest.TestCase):
    """取り込み元は sqlite3 なので、**本物のファイルで確かめられる**。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="src_"))
        self.path = self.dir / "src.sqlite3"
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE 表 (名前 TEXT, 数 INTEGER, 実数 REAL)")
        conn.executemany("INSERT INTO 表 VALUES (?,?,?)",
                         [("あ", 1, 1.5), ("い", None, None)])
        conn.commit()
        conn.close()

    def test_読み取り方式は1つ(self) -> None:
        """環境で分岐しない。ドライバの有無で動いたり動かなかったりしない。"""
        self.assertEqual(data_sync.backend_name(), "sqlite3")

    def test_表をそのまま読める(self) -> None:
        rows = data_sync.read_table(self.path, "表")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["名前"], "あ")
        # 値は型が付いたまま返る(以前の mdb-export は何でも文字列だった)
        self.assertEqual(rows[0]["数"], 1)
        self.assertIsNone(rows[1]["数"])

    def test_テーブル一覧が引ける(self) -> None:
        self.assertEqual(data_sync.list_tables(self.path), ["表"])

    def test_無い表はSyncErrorになる(self) -> None:
        """1テーブル欠けただけで取り込み全体を落とさないための包み直し。"""
        with self.assertRaises(data_sync.SyncError):
            data_sync.read_table(self.path, "無い表")

    def test_sqlite3でないファイルは読めない(self) -> None:
        """拡張子が合っていても中身が別物ということはある。"""
        bogus = self.dir / "偽物.sqlite3"
        bogus.write_text("これは sqlite3 ではありません", encoding="utf-8")
        self.assertEqual(data_sync.list_tables(bogus), [])


class ImportDoesNotEchoBackTests(unittest.TestCase):
    """**取り込みのたびに取り込み元が倍にならないこと。**

    取り込みは総入れ替えで、管理番号は手元で振り直される。同期記録に
    古い行IDが残ると、取り込んだ行がどれも「未送信」に見え、次の取り込みの
    前に走る書き戻しが**取り込み元から来た行をもう一度**送ってしまう。
    現場のファイルで 32件 → 64件(同一内容の重複62行)になっていた。
    """

    TABLE = config.TBL_WAREHOUSE_ORDER
    COLUMNS = ("登録日時", "LotNo", "品名", "発注コード", "単位", "材質", "調質",
               "厚", "幅", "丈", "用途コード", "納入先", "発注数",
               "取り消し済", "取り消し日時", "確認済み", "確認日時")

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="echo_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(self.src)
        cols = ", ".join(f'"{c}"' for c in self.COLUMNS)
        conn.execute(f'CREATE TABLE "{self.TABLE}"'
                     f" (管理番号 INTEGER PRIMARY KEY, {cols})")
        marks = ", ".join("?" for _ in self.COLUMNS)
        conn.executemany(
            f'INSERT INTO "{self.TABLE}" ({cols}) VALUES ({marks})',
            [self._row(f"L{i}") for i in range(3)])
        conn.commit()
        conn.close()
        self.conn = make_conn()
        self.addCleanup(self.conn.close)

    def _row(self, lot: str) -> tuple:
        return ("2026-01-01 00:00:00", lot, "パレット", "", "", "", "",
                0.0, 0, 0, "", "", 1, "", "", "", "")

    def source_lots(self) -> list[str]:
        conn = sqlite3.connect(self.src)
        try:
            return [r[0] for r in conn.execute(
                f'SELECT LotNo FROM "{self.TABLE}" ORDER BY 管理番号')]
        finally:
            conn.close()

    def test_何度取り込んでも増えない(self) -> None:
        for _ in range(4):
            data_sync.import_master(self.conn, self.src)
            self.assertEqual(self.source_lots(), ["L0", "L1", "L2"])

    def test_取り込んだ行は送信済みになる(self) -> None:
        data_sync.import_master(self.conn, self.src)
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn), {})

    def test_こちらで出した発注はちゃんと送る(self) -> None:
        """増やさないために**送らなくなる**、では意味が無い。"""
        data_sync.import_master(self.conn, self.src)
        cols = ", ".join(f'"{c}"' for c in self.COLUMNS)
        marks = ", ".join("?" for _ in self.COLUMNS)
        self.conn.execute(
            f'INSERT INTO "{self.TABLE}" ({cols}) VALUES ({marks})',
            self._row("NEW"))
        self.conn.commit()
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn),
                         {self.TABLE: 1})
        data_sync.write_back(self.conn, self.src)
        self.assertEqual(self.source_lots(), ["L0", "L1", "L2", "NEW"])
        # 送ったあとに取り込み直しても、もう一度は送らない
        data_sync.import_master(self.conn, self.src)
        self.assertEqual(self.source_lots(), ["L0", "L1", "L2", "NEW"])


class DedupeScriptTests(unittest.TestCase):
    """`scripts/dedupe_writeback.py` ── **すでに増えた行**を片付ける道具。

    原因は直したが、直す前に増えてしまった行は残る。数えるのは既定で、
    消すのは頼まれたときだけ(しかも控えを取ってから)。
    """

    def setUp(self) -> None:
        import importlib.util

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "dedupe_writeback", root / "scripts" / "dedupe_writeback.py")
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)

        self.dir = Path(tempfile.mkdtemp(prefix="dedupe_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(self.src)
        conn.execute('CREATE TABLE "資材パレット注文管理"'
                     " (管理番号 INTEGER PRIMARY KEY, LotNo TEXT, 発注数 INTEGER,"
                     " 送信ID TEXT)")
        for lot, qty in [("L0", 1), ("L1", 2)] * 3:      # 2件が6件に増えた形
            conn.execute('INSERT INTO "資材パレット注文管理"'
                         " (LotNo, 発注数, 送信ID) VALUES (?, ?, hex(randomblob(4)))",
                         (lot, qty))
        conn.commit()
        conn.close()

    def open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.src)
        self.addCleanup(conn.close)
        return conn

    def test_送信IDが違っても中身が同じなら重複(self) -> None:
        """送信IDは送るときに振る番号で、中身ではない。"""
        drop, total = self.script.duplicates(self.open(), "資材パレット注文管理")
        self.assertEqual(total, 6)
        self.assertEqual(len(drop), 4)

    def test_先に入った行を残す(self) -> None:
        drop, _total = self.script.duplicates(self.open(), "資材パレット注文管理")
        self.assertNotIn(1, drop)
        self.assertNotIn(2, drop)

    def test_重複が無ければ何も出さない(self) -> None:
        conn = self.open()
        conn.execute('DELETE FROM "資材パレット注文管理" WHERE 管理番号 > 2')
        conn.commit()
        drop, total = self.script.duplicates(conn, "資材パレット注文管理")
        self.assertEqual((drop, total), ([], 2))

    def test_控えは元とは別の名前で残る(self) -> None:
        copy = self.script.backup(self.src)
        self.assertTrue(copy.exists())
        self.assertNotEqual(copy, self.src)
        self.assertEqual(copy.read_bytes(), self.src.read_bytes())


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _import(self, rows, table="BoardMaster"):
        with mock.patch.object(data_sync, "read_table", return_value=rows):
            return data_sync.import_tables(
                self.conn, Path("dummy.sqlite3"),
                {table: import_specs.IMPORT_SPECS[table]},
                required=import_specs.REQUIRED_KEY_COLUMNS,
                fallbacks=import_specs.NULL_FALLBACKS)

    def test_rows_land_in_sqlite(self):
        result = self._import([
            {"ボード幅": "1150", "ボード丈": "2500", "ボードタイプ": "ハードボード",
             "データラベル": "lblItem1"},
            {"ボード幅": "900", "ボード丈": "1800", "ボードタイプ": "ハードボード",
             "データラベル": ""},
        ])
        self.assertEqual(result.imported["BoardMaster"], 2)
        self.assertTrue(result.ok)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM BoardMaster").fetchone()[0], 2)

    def test_import_replaces_everything(self):
        """再取り込みは総入れ替え(古い行が残らない)。"""
        self._import([{"ボード幅": "1", "ボード丈": "2", "ボードタイプ": "A",
                       "データラベル": ""}])
        self._import([{"ボード幅": "3", "ボード丈": "4", "ボードタイプ": "B",
                       "データラベル": ""}])
        rows = self.conn.execute("SELECT ボード幅 FROM BoardMaster").fetchall()
        self.assertEqual([r[0] for r in rows], [3])

    def test_missing_key_columns_are_skipped(self):
        """PalletMasterは幅・丈が必須。欠けた行は0で埋めずに飛ばす。"""
        with mock.patch.object(data_sync, "read_table", return_value=[
                {"幅": "1150", "丈": "2500"}, {"幅": "", "丈": ""}]):
            result = data_sync.import_tables(
                self.conn, Path("d.sqlite3"),
                {"PalletMaster": import_specs.IMPORT_SPECS["PalletMaster"]},
                required=import_specs.REQUIRED_KEY_COLUMNS,
                fallbacks=import_specs.NULL_FALLBACKS)
        self.assertEqual(result.imported["PalletMaster"], 1)
        self.assertEqual(result.skipped["PalletMaster"], 1)

    def test_blank_values_use_the_column_default(self):
        """NOT NULL列に空欄が来ても落ちない(NULLではなく既定値を入れる)。"""
        with mock.patch.object(data_sync, "read_table", return_value=[
                {"ﾛｯﾄ番号": "A123456", "BOX実績_板厚": "", "製造板幅": ""}]):
            result = data_sync.import_tables(
                self.conn, Path("d.sqlite3"),
                {"仕掛ロット": import_specs.LOT_IMPORT_SPECS["仕掛ロット"]},
                source_table="仕掛",
                required=import_specs.REQUIRED_KEY_COLUMNS,
                fallbacks=import_specs.NULL_FALLBACKS)
        self.assertEqual(result.imported["仕掛ロット"], 1)
        row = self.conn.execute("SELECT * FROM 仕掛ロット").fetchone()
        self.assertEqual(row["BOX実績_板厚"], 0)
        self.assertEqual(row["製造板幅"], 0)

    def test_a_failing_table_does_not_stop_the_others(self):
        calls = {"n": 0}

        def flaky(_path, table):
            calls["n"] += 1
            if table == "BoardMaster":
                raise data_sync.SyncError("読めません")
            return [{"アングル丈": "2000", "データラベル": ""}]

        with mock.patch.object(data_sync, "read_table", side_effect=flaky):
            result = data_sync.import_tables(
                self.conn, Path("d.sqlite3"),
                {"BoardMaster": import_specs.IMPORT_SPECS["BoardMaster"],
                 "CornerboardMaster": import_specs.IMPORT_SPECS["CornerboardMaster"]},
                required=import_specs.REQUIRED_KEY_COLUMNS,
                fallbacks=import_specs.NULL_FALLBACKS)
        self.assertEqual(calls["n"], 2)
        self.assertFalse(result.ok)
        self.assertEqual(result.imported["CornerboardMaster"], 1)
        self.assertIn("BoardMaster", result.errors[0])

    def test_summary_mentions_failures(self):
        result = data_sync.ImportResult(imported={"A": 3}, errors=["B: だめでした"])
        text = result.summary()
        self.assertIn("A: 3件", text)
        self.assertIn("B: だめでした", text)

    def test_merge_combines_two_runs(self):
        a = data_sync.ImportResult(imported={"A": 1})
        b = data_sync.ImportResult(imported={"B": 2}, errors=["x"])
        a.merge(b)
        self.assertEqual(a.total, 3)
        self.assertFalse(a.ok)


class WriteBackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()
        for lot in ("A1", "A2"):
            self.conn.execute(
                "INSERT INTO 資材パレット注文管理"
                " (登録日時, LotNo, 品名, 発注コード, 単位, 厚, 幅, 丈)"
                " VALUES ('2026-07-28 00:00:00', ?, 'テスト', '058901', '台',"
                "         3.0, 1000, 2000)", (lot,))
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_pending_rows_start_as_everything(self):
        spec = spec_for(config.TBL_WAREHOUSE_ORDER)
        rows = outbox_sync.pending_rows(self.conn, spec)
        self.assertEqual(len(rows), 2)

    def test_marked_rows_are_not_pending_again(self):
        """同じ行を二度Accessへ送らないこと。"""
        spec = spec_for(config.TBL_WAREHOUSE_ORDER)
        outbox_sync.mark_synced(self.conn, spec, [1])
        rows = outbox_sync.pending_rows(self.conn, spec)
        self.assertEqual([r["管理番号"] for r in rows], [2])

    def test_marking_twice_is_harmless(self):
        spec = spec_for(config.TBL_WAREHOUSE_ORDER)
        outbox_sync.mark_synced(self.conn, spec, [1])
        outbox_sync.mark_synced(self.conn, spec, [1, 2])
        rows = outbox_sync.pending_rows(self.conn, spec)
        self.assertEqual(rows, [])

    def test_write_back_is_skipped_without_the_source_file(self):
        """送り先に届かない端末では何もせず、理由だけ返す(現場を止めない)。"""
        with mock.patch.object(data_sync, "find_material_db", return_value=None):
            result = data_sync.write_back(self.conn)
        self.assertFalse(result.ok)
        self.assertIn("見つかりません", result.skipped_reason)
        self.assertEqual(result.total, 0)

    def test_write_back_is_skipped_when_the_file_cannot_be_opened(self):
        """開けなかった理由をそのまま出す。手元の登録は残っている。"""
        with mock.patch.object(data_sync, "find_material_db",
                               return_value=Path("m.sqlite3")), \
             mock.patch.object(data_sync.source_db, "connect",
                               side_effect=data_sync.source_db.SourceError("閉じています")):
            result = data_sync.write_back(self.conn)
        self.assertIn("閉じています", result.skipped_reason)
        self.assertEqual(result.total, 0)

    def test_write_back_sends_pending_rows_and_marks_them(self):
        sent: list[tuple[str, dict]] = []

        class FakeSource:
            path = Path("m.sqlite3")

            def execute(self, sql, params=()):
                return 0

            def insert(self, table, values):
                sent.append((table, values))
                return 1

            def close(self):
                pass

        with mock.patch.object(data_sync.source_db, "connect",
                               return_value=FakeSource()), \
             mock.patch.object(data_sync, "find_material_db",
                               return_value=Path("m.sqlite3")):
            result = data_sync.write_back(self.conn)

        self.assertTrue(result.ok)
        self.assertEqual(result.sent[config.TBL_WAREHOUSE_ORDER], 2)
        self.assertEqual([t for t, _ in sent],
                         [config.TBL_WAREHOUSE_ORDER] * 2)
        # 主キーは送らない(Access側で採番されるため)
        self.assertNotIn("管理番号", sent[0][1])
        # 2回目は送るものが無い
        with mock.patch.object(data_sync.source_db, "connect",
                               return_value=FakeSource()), \
             mock.patch.object(data_sync, "find_material_db", return_value=Path("m.sqlite3")):
            again = data_sync.write_back(self.conn)
        self.assertEqual(again.total, 0)

    def test_one_bad_row_does_not_block_the_rest(self):
        calls = {"n": 0}

        class FlakySource:
            path = Path("m.sqlite3")

            def execute(self, sql):
                return 0

            def insert(self, table, values):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise data_sync.source_db.SourceError("型が合いません")
                return 1

            def close(self):
                pass

        with mock.patch.object(data_sync.source_db, "connect",
                               return_value=FlakySource()), \
             mock.patch.object(data_sync, "find_material_db",
                               return_value=Path("m.sqlite3")):
            result = data_sync.write_back(self.conn)

        self.assertEqual(result.sent[config.TBL_WAREHOUSE_ORDER], 1)
        self.assertEqual(len(result.errors), 1)
        # 失敗した行は次回また送られる
        pending = outbox_sync.pending_rows(self.conn, spec_for(config.TBL_WAREHOUSE_ORDER))
        self.assertEqual([r["管理番号"] for r in pending], [1])


class AutoImportTests(unittest.TestCase):
    """起動時の自動取り込み: 更新されたファイルだけを読む。"""

    def setUp(self) -> None:
        self.conn = make_conn()
        self._tmp = Path(__file__).resolve().parent / "_tmp_stamp"
        self._tmp.mkdir(exist_ok=True)
        self.file = self._tmp / "x.sqlite3"
        self.file.write_text("a")

    def tearDown(self) -> None:
        self.conn.close()
        for f in self._tmp.glob("*"):
            f.unlink()
        self._tmp.rmdir()

    def test_a_new_file_needs_importing(self):
        self.assertTrue(data_sync.needs_import(self.conn, self.file))

    def test_an_unchanged_file_is_skipped(self):
        data_sync.mark_imported(self.conn, self.file)
        self.assertFalse(data_sync.needs_import(self.conn, self.file))

    def test_a_changed_file_is_picked_up_again(self):
        import os
        import time
        data_sync.mark_imported(self.conn, self.file)
        newer = time.time() + 60
        os.utime(self.file, (newer, newer))
        self.assertTrue(data_sync.needs_import(self.conn, self.file))

    def test_a_missing_file_is_not_requested(self):
        self.assertFalse(data_sync.needs_import(self.conn, self._tmp / "none.sqlite3"))

    def test_auto_import_does_nothing_without_a_backend(self):
        with mock.patch.object(data_sync, "backend_name", return_value="なし"):
            result = data_sync.auto_import(self.conn)
        self.assertEqual(result.total, 0)
        self.assertTrue(result.ok)


class UnsentGuardTests(unittest.TestCase):
    """未送信の行が残っているテーブルは総入れ替えしない。"""

    def setUp(self) -> None:
        self.conn = make_conn()
        self.conn.execute(
            "INSERT INTO 資材パレット注文管理"
            " (登録日時, LotNo, 品名, 発注コード, 単位, 厚, 幅, 丈)"
            " VALUES ('2026-07-28 00:00:00', 'A1', 'テスト', '058901', '台',"
            "         3.0, 1000, 2000)")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_unsent_rows_are_detected(self):
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn),
                         {config.TBL_WAREHOUSE_ORDER: 1})

    def test_synced_rows_are_not_counted(self):
        outbox_sync.mark_synced(self.conn, spec_for(config.TBL_WAREHOUSE_ORDER), [1])
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn), {})

    def test_import_skips_the_table_while_rows_are_unsent(self):
        """Accessへ送れていない発注が、取り込みで消えないこと。"""
        seen = {}

        def fake_import(conn, path, specs, **kw):
            seen["tables"] = set(specs)
            return kw.get("result") or data_sync.ImportResult()

        with mock.patch.object(data_sync, "find_material_db", return_value=None), \
             mock.patch.object(data_sync, "find_material_db", return_value=Path("m.sqlite3")), \
             mock.patch.object(data_sync, "import_tables", side_effect=fake_import):
            result = data_sync.import_master(self.conn)

        self.assertNotIn(config.TBL_WAREHOUSE_ORDER, seen["tables"])
        self.assertIn("未送信", result.errors[0])
        # 行は消えていない
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM 資材パレット注文管理").fetchone()[0], 1)


class EnvironmentTests(unittest.TestCase):
    def test_describe_mentions_the_places_it_looked(self):
        text = data_sync.describe_environment()
        self.assertIn("読み取り方式", text)
        self.assertIn(str(config.master_db_dir()), text)
        self.assertIn("書き戻し", text)


if __name__ == "__main__":
    unittest.main()


class WriteBackKeyColumnTests(unittest.TestCase):
    """書き戻しの主キー列がスキーマと一致しているか。

    ここがずれると、そのテーブルは**一度もAccessへ送られない**。
    しかもSQLのエラーは握られて空リストになるので「送るものが無い」と
    区別がつかない。実際、入出庫履歴が 管理番号 扱いになっていて
    ずっと送られていなかった。
    """

    def setUp(self) -> None:
        self.conn = make_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_every_writeback_table_has_its_key_column(self):
        for spec in data_sync.WRITEBACK_SPECS:
            columns = {row[1] for row in
                       self.conn.execute(f"PRAGMA table_info([{spec.sqlite_table}])")}
            self.assertIn(spec.key_column, columns,
                          f"{spec.sqlite_table} に {spec.key_column} 列がありません")

    def test_the_history_rows_are_actually_picked_up(self):
        """入出庫履歴が拾えること(拾えていなかった回帰の防止)。"""
        self.conn.execute(
            "INSERT INTO パレット入出庫履歴"
            " (幅, 丈, 位置, 区分, 数量, 在庫数_更新後, 更新日時)"
            " VALUES (1000, 2000, 'A1', '受入', 1, 1, '2026-07-29 00:00:00')")
        self.conn.commit()
        rows = outbox_sync.pending_rows(self.conn, spec_for(config.TBL_STOCK_HISTORY))
        self.assertEqual(len(rows), 1)


class ConcurrentWriteBackTests(unittest.TestCase):
    """並行アクセス: 実スキーマ相手でも同じ行を二重にAccessへ送らないこと。

    予約(claim)・拾い直し・送信ID等の細かい状態遷移は汎用エンジン側の
    tests/test_outbox_sync.py で確かめている。ここでは
    `data_sync.WRITEBACK_SPECS` が実際のテーブル・列名で正しく配線
    されていることを、実際にスレッドを並走させて確認する。
    """

    def setUp(self) -> None:
        # スレッド間で同じDBを見る必要があるのでファイルにする
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._dir.name) / "t.db"
        self.conn = self._open()
        db.apply_schema(self.conn)
        for i in range(6):
            self.conn.execute(
                "INSERT INTO 資材パレット注文管理"
                " (登録日時, LotNo, 品名, 発注コード, 単位, 厚, 幅, 丈)"
                " VALUES ('2026-07-29 00:00:00', ?, 'テスト', 'C1', '台',"
                "         3.0, 1000, 2000)", (f"L{i}",))
        self.conn.commit()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def tearDown(self) -> None:
        self.conn.close()
        self._dir.cleanup()
        outbox_sync._op_id_column_cache.clear()

    def test_parallel_write_back_sends_each_row_once(self):
        """3本同時に走らせても、送られるのは6行ぶんだけ。"""
        sent: list[str] = []
        sent_lock = threading.Lock()

        class FakeSource:
            path = Path("m.sqlite3")

            def execute(self, sql, params=()):
                return 0

            def insert(self, _table, values):
                time.sleep(0.005)          # 共有フォルダ越しの往復ぶん
                with sent_lock:
                    sent.append(values["LotNo"])
                return 1

            def close(self):
                pass

        def run() -> None:
            # スレッドごとに接続を開く(SQLiteの接続はスレッドをまたげない)
            conn = self._open()
            try:
                data_sync.write_back(conn)
            finally:
                conn.close()

        with mock.patch.object(data_sync.source_db, "connect",
                               return_value=FakeSource()), \
             mock.patch.object(data_sync, "find_material_db", return_value=Path("m.sqlite3")):
            threads = [threading.Thread(target=run) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(sorted(sent), [f"L{i}" for i in range(6)])
        self.assertEqual(len(sent), len(set(sent)), f"二重送信: {sent}")
