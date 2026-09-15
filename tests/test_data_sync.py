"""取り込み元(sqlite3)と手元DBの同期のテスト。

取り込み元が sqlite3 になったので、**実ファイルを作って本物を通せる**
(以前は Access ドライバか mdbtools が要り、読み取りは偽物で代用して
いた)。ここでは探索・取り込み・書き戻し・同期記録を確かめる。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from packaging_tool import config, data_sync, db, import_specs, outbox_sync
# 差し替えの当て先は**持ち主のモジュール**。ハブ(`data_sync`)へ
# 当てても、持ち主から呼んでいる側には効かない
from packaging_tool import sync_import as imports
from packaging_tool import sync_sources as sources
from packaging_tool import sync_writeback as writeback
from packaging_tool import warehouse_service as svc


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

    def test_古い名前で残った仕掛台帳も梱包資材マスタにしない(self):
        """上流がファイル名を変えたとき、**古いほうが残る。**

        仕掛台帳は SIKALOTNOW → SIKALOT と改名された。いまの3つの名前と
        ぴったり一致するものだけを外していると、共有に残った古い
        `SIKALOTNOW.sqlite3` が梱包資材マスタとして拾われてしまう。
        仕掛台帳はどれも SIKA で始まるので、頭で外す。
        """
        for name in ("SIKALOTNOW.sqlite3", "SIKAHIKINOW.sqlite3",
                     "SIKAODRNOW.sqlite3", "SIKAODR_2025.sqlite3"):
            (self._tmp / name).write_text("")
        self.assertIsNone(data_sync.find_material_db(self._tmp))

    def test_missing_master(self):
        self.assertIsNone(data_sync.find_material_db(self._tmp))

    def test_finds_the_kanban_master_by_its_usual_name(self):
        (self._tmp / config.KANBAN_DB_NAME).write_text("")
        self.assertEqual(data_sync.find_kanban_db(self._tmp).name,
                         config.KANBAN_DB_NAME)

    def test_finds_the_kanban_db_suffix_too(self):
        (self._tmp / "看板マスタ.db").write_text("")
        self.assertEqual(data_sync.find_kanban_db(self._tmp).name,
                         "看板マスタ.db")

    def test_missing_kanban_master(self):
        self.assertIsNone(data_sync.find_kanban_db(self._tmp))

    def test_kanban_does_not_fall_back_to_other_files(self):
        """`find_material_db` と違い、緩い一致はしない。

        既定の置き場所は梱包資材マスタと同じ共有フォルダなので、緩い
        一致にすると梱包資材マスタ自身を誤って拾いかねない。
        """
        (self._tmp / config.MATERIAL_DB_NAME).write_text("")
        self.assertIsNone(data_sync.find_kanban_db(self._tmp))

    def test_finds_the_three_lot_files(self):
        for name in config.LOT_DB_FILES.values():
            (self._tmp / name).write_text("")
        found = data_sync.find_lot_dbs(self._tmp)
        self.assertEqual(set(found), set(config.LOT_DB_FILES))

    def test_partial_lot_files(self):
        (self._tmp / "SIKALOT.sqlite3").write_text("")
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
        with mock.patch.object(sources, "read_table", return_value=rows):
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
        with mock.patch.object(sources, "read_table", return_value=[
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
        with mock.patch.object(sources, "read_table", return_value=[
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

        with mock.patch.object(sources, "read_table", side_effect=flaky):
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
        with mock.patch.object(sources, "find_material_db", return_value=None):
            result = data_sync.write_back(self.conn)
        self.assertFalse(result.ok)
        self.assertIn("見つかりません", result.skipped_reason)
        self.assertEqual(result.total, 0)

    def test_write_back_is_skipped_when_the_file_cannot_be_opened(self):
        """開けなかった理由をそのまま出す。手元の登録は残っている。"""
        with mock.patch.object(sources, "find_material_db",
                               return_value=Path("m.sqlite3")), \
             mock.patch.object(sources.source_db, "connect",
                               side_effect=sources.source_db.SourceError("閉じています")):
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

        with mock.patch.object(sources.source_db, "connect",
                               return_value=FakeSource()), \
             mock.patch.object(sources, "find_material_db",
                               return_value=Path("m.sqlite3")):
            result = data_sync.write_back(self.conn)

        self.assertTrue(result.ok)
        self.assertEqual(result.sent[config.TBL_WAREHOUSE_ORDER], 2)
        self.assertEqual([t for t, _ in sent],
                         [config.TBL_WAREHOUSE_ORDER] * 2)
        # 主キーは送らない(Access側で採番されるため)
        self.assertNotIn("管理番号", sent[0][1])
        # 2回目は送るものが無い
        with mock.patch.object(sources.source_db, "connect",
                               return_value=FakeSource()), \
             mock.patch.object(sources, "find_material_db", return_value=Path("m.sqlite3")):
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
                    raise sources.source_db.SourceError("型が合いません")
                return 1

            def close(self):
                pass

        with mock.patch.object(sources.source_db, "connect",
                               return_value=FlakySource()), \
             mock.patch.object(sources, "find_material_db",
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
        with mock.patch.object(sources, "backend_name", return_value="なし"):
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

        with mock.patch.object(sources, "find_material_db", return_value=None), \
             mock.patch.object(sources, "find_material_db", return_value=Path("m.sqlite3")), \
             mock.patch.object(imports, "import_tables", side_effect=fake_import):
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



class OpIdGuardTests(unittest.TestCase):
    """**二重登録の防止が、黙って切れないこと。**

    書き戻しは送信IDを取り込み元の一意インデックスに賭けている。
    インデックスを作れないと防止なしで送り続けるが、以前はログに1行
    出るだけで、現場からは何も変わって見えなかった。
    """

    def setUp(self) -> None:
        from packaging_tool import outbox_sync, source_db
        self.outbox_sync = outbox_sync
        self.source_db = source_db
        self.dir = Path(tempfile.mkdtemp(prefix="guard_"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "src.sqlite3"
        self.spec = outbox_sync.WriteBackSpec(
            sqlite_table="注文", access_table="注文", key_column="管理番号")
        outbox_sync._op_id_column_cache.clear()
        self.addCleanup(outbox_sync._op_id_column_cache.clear)

    def make(self, *, op_id_column: bool, blanks: int = 0) -> None:
        conn = sqlite3.connect(self.path)
        extra = ", 送信ID TEXT" if op_id_column else ""
        conn.execute(f"CREATE TABLE 注文 "
                     f"(管理番号 INTEGER PRIMARY KEY, LotNo TEXT{extra})")
        for i in range(blanks):
            conn.execute("INSERT INTO 注文 (LotNo, 送信ID) VALUES (?, '')",
                         (f"LOT{i}",))
        conn.commit()
        conn.close()

    def ensure(self) -> bool:
        with self.source_db.connect(self.path) as src:
            return self.outbox_sync.ensure_op_id_column(src, self.spec)

    def state(self):
        with self.source_db.connect(self.path) as src:
            return self.outbox_sync.guard_state(src, self.spec)

    def test_列も索引も無ければ作る(self) -> None:
        self.make(op_id_column=False)
        self.assertTrue(self.ensure())
        self.assertTrue(self.state().ok)

    def test_二度目も効いたまま(self) -> None:
        """列がもうある状態で「もうある」を失敗と読むと、以後ずっと切れる。"""
        self.make(op_id_column=False)
        self.ensure()
        self.outbox_sync._op_id_column_cache.clear()   # 起動し直した想定
        self.assertTrue(self.ensure())

    def test_未採番のNULLが並んでいても作れる(self) -> None:
        """NULLどうしは別物なので、古い行が何件あっても邪魔しない。"""
        self.make(op_id_column=True)
        conn = sqlite3.connect(self.path)
        for i in range(3):
            conn.execute("INSERT INTO 注文 (LotNo) VALUES (?)", (f"L{i}",))
        conn.commit(); conn.close()
        self.assertTrue(self.ensure())
        self.assertTrue(self.state().ok)

    def test_空文字が2件以上あると作れないので直してから作る(self) -> None:
        """**ここが黙って切れていた。**

        空文字どうしは同じ値なので一意制約に当たり、しかもデータを
        直さないかぎり毎回当たり続ける。空文字は送信IDとして意味を
        持たない(採番するのは uuid4 の16進)ので、未採番と同じ NULL に
        寄せてから作り直す。
        """
        self.make(op_id_column=True, blanks=3)
        self.assertFalse(self.state().ok)         # 直す前は効いていない
        self.assertEqual(self.state().blanks, 3)

        self.assertTrue(self.ensure())            # 直して作る

        after = self.state()
        self.assertTrue(after.ok)
        self.assertEqual(after.blanks, 0)
        conn = sqlite3.connect(self.path)
        rows = [r[0] for r in conn.execute("SELECT 送信ID FROM 注文")]
        conn.close()
        self.assertEqual(rows, [None, None, None])   # 空欄は未採番に寄せた

    def test_効いていない理由を言う(self) -> None:
        """「効いていません」だけでは、次に何をすればよいか分からない。"""
        self.make(op_id_column=True, blanks=3)
        why = self.state().why()
        self.assertIn("3件", why)
        self.assertIn("取り込み元へ反映", why)

    def test_効いているときは黙っている(self) -> None:
        """全部の項目に印が付くと、印が意味を持たなくなる。"""
        self.make(op_id_column=False)
        self.ensure()
        self.assertEqual(self.state().why(), "")

    def test_状態を見るだけでは形を変えない(self) -> None:
        """見に行っただけで取り込み元が変わるのは、見る側の期待と違う。"""
        self.make(op_id_column=False)
        self.state()
        conn = sqlite3.connect(self.path)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(注文)")]
        conn.close()
        self.assertNotIn("送信ID", columns)


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

        with mock.patch.object(sources.source_db, "connect",
                               return_value=FakeSource()), \
             mock.patch.object(sources, "find_material_db", return_value=Path("m.sqlite3")):
            threads = [threading.Thread(target=run) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(sorted(sent), [f"L{i}" for i in range(6)])
        self.assertEqual(len(sent), len(set(sent)), f"二重送信: {sent}")


class KanbanImportTests(unittest.TestCase):
    """看板(在庫薄警告)マスタは梱包資材マスタとは**別ファイル**から読む。

    以前はこの8テーブル(Form状態管理・看板_*)も梱包資材マスタから
    読む定義になっていたが、現場の梱包資材マスタには一度も入っておらず、
    取り込みのたびに「取り込めませんでした」の8件に数えられていた。
    現場から渡された実データは看板マスタ.sqlite3という別ファイルに
    あったので、そちらから読むように分けた。
    """

    def setUp(self) -> None:
        from packaging_tool import user_settings
        self.conn = make_conn()
        self.master_dir = Path(tempfile.mkdtemp(prefix="master_"))
        self.kanban_dir = Path(tempfile.mkdtemp(prefix="kanban_"))
        self.master_src = self.master_dir / config.MATERIAL_DB_NAME
        self.kanban_src = self.kanban_dir / config.KANBAN_DB_NAME
        self._make_master(self.master_src)
        # 既定の看板マスタの置き場所を隔離する(本物の共有フォルダを
        # 見に行かせない。他の試験が残した設定を引き継がせない)
        saved = user_settings.get(config.KEY_KANBAN_DB_DIR)
        user_settings.save(config.KEY_KANBAN_DB_DIR, str(self.kanban_dir))
        self.addCleanup(user_settings.save, config.KEY_KANBAN_DB_DIR,
                        saved if saved is not None else "")

    def tearDown(self) -> None:
        self.conn.close()

    def _make_master(self, path: Path) -> None:
        """梱包資材マスタ側のテーブルだけを持つ取り込み元。

        看板系テーブルはここには**入れない** ── 現場の実ファイルと
        同じ状態(看板系は無い)を再現する。BoardMaster以外は空(0件)
        でよい ── `import_tables` は空の結果を列の突き合わせより先に
        `imported[table]=0` として通すので、列名を合わせる必要が無い。
        """
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE BoardMaster (ボード幅 INTEGER, ボード丈 INTEGER,"
                     " ボードタイプ TEXT, データラベル TEXT)")
        conn.execute("INSERT INTO BoardMaster VALUES (900, 1800, 'ハードボード', '')")
        for table in import_specs.IMPORT_SPECS:
            if table in import_specs.KANBAN_TABLES or table in (
                    "BoardMaster", *import_specs.OPTIONAL_TABLES):
                continue
            conn.execute(f'CREATE TABLE "{table}" (dummy TEXT)')
        conn.commit()
        conn.close()

    def _make_kanban(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE Form状態管理 (ライン名 TEXT, 状態 TEXT)")
        conn.execute("INSERT INTO Form状態管理 VALUES ('L1', '稼働中')")
        for table in import_specs.KANBAN_TABLES:
            if table == "Form状態管理":
                continue
            conn.execute(
                f'CREATE TABLE "{table}" (資材 TEXT, サイズ TEXT, 欲 TEXT, 不 TEXT,'
                " 更新日 TEXT, 発送 TEXT, 倉庫確認日時 TEXT, 常設品 TEXT)")
            conn.execute(f'INSERT INTO "{table}" (資材, サイズ) VALUES (?, ?)',
                        (table, "900x1800"))
        conn.commit()
        conn.close()

    def test_kanban_tables_are_read_from_the_kanban_file(self):
        self._make_kanban(self.kanban_src)
        result = data_sync.import_master(
            self.conn, self.master_src, kanban_path=self.kanban_src)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.imported["BoardMaster"], 1)
        for table in import_specs.KANBAN_TABLES:
            self.assertEqual(result.imported.get(table), 1,
                             f"{table} が看板マスタから取り込まれていません")
        row = self.conn.execute(
            "SELECT ライン名, 状態 FROM Form状態管理").fetchone()
        self.assertEqual((row[0], row[1]), ("L1", "稼働中"))

    def test_master_file_never_has_kanban_tables_and_that_is_fine(self):
        """梱包資材マスタ側に看板テーブルが無くても`不足`エラーにしない。

        以前はここが8件の "テーブル名: ...を読めませんでした" という
        エラーになっていた(このテストが失敗していたはずの状態)。
        """
        self._make_kanban(self.kanban_src)
        result = data_sync.import_master(
            self.conn, self.master_src, kanban_path=self.kanban_src)
        for table in import_specs.KANBAN_TABLES:
            self.assertFalse(any(table in e for e in result.errors),
                             f"{table} がエラーに含まれています: {result.errors}")

    def test_missing_kanban_file_is_a_note_not_an_error(self):
        """置き場所が未設定・見つからないときは、梱包資材マスタ側を
        失敗にせず、案内を1件だけ `notes` に足す。

        `kanban_path` を明示して指定したのに開けない場合は(梱包資材
        マスタの `source_path` と同じ扱いで)テーブルごとの詳しいエラーに
        なる ── 「置き場所が未設定」と「指定した場所が間違っている」は
        別の状況なので、ここで確かめるのは前者(`find_kanban_db` が
        自動探索で見つけられない場合)。
        """
        with mock.patch.object(sources, "find_kanban_db", return_value=None):
            result = data_sync.import_master(self.conn, self.master_src)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.imported["BoardMaster"], 1)
        self.assertTrue(any(config.KANBAN_DB_NAME in n for n in result.notes),
                        result.notes)
        for table in import_specs.KANBAN_TABLES:
            self.assertNotIn(table, result.imported)

    def test_explicit_wrong_kanban_path_gives_detailed_errors(self):
        """`kanban_path` を明示したのに開けないときは、テーブルごとの
        詳しいエラーになる(梱包資材マスタの `source_path` と同じ扱い)。

        明示したパスは信用して、そのまま開こうとする ──
        `source_path` を明示したときに `find_material_db()` の
        自動探索へ回さないのと同じ理由。
        """
        result = data_sync.import_master(
            self.conn, self.master_src, kanban_path=self.kanban_dir / "無い.sqlite3")
        self.assertFalse(result.ok)
        for table in import_specs.KANBAN_TABLES:
            self.assertTrue(any(table in e for e in result.errors),
                            f"{table} の詳しいエラーがありません: {result.errors}")

    def test_default_kanban_path_is_found_via_config(self):
        """`kanban_path` を省略すると `find_kanban_db()`(設定のパス)を見る。"""
        self._make_kanban(self.kanban_src)
        result = data_sync.import_master(self.conn, self.master_src)
        self.assertEqual(result.imported.get("看板_AIM"), 1)


class ConfirmMarkWriteBackTests(unittest.TestCase):
    """**確認・取消の印が共有へ届くこと。**

    書き戻しは長らく「新しい行を足す」だけだった。確認済み・取り消し済は
    行を送ったあとに手元で付く印なので、共有には一度も届いていなかった。

    そのせいで2つ起きていた(実測で再現した)。

        1. 現場から「確認されたかどうか」が見えない
        2. 印が手元にしか無いので、**次の取り込みの総入れ替えで消える**
           ── 起動時の自動取り込みも総入れ替えなので、資材が今日
           確認した印は翌朝の起動で消えていた

    手元の管理番号は取り込みのたびに振り直される(共有41,42 → 手元1,2)
    ので、共有のどの行かは `取込元管理番号`(取り込みで受け取った行)か
    `送信ID`(手元で作って送った行)で決める。
    """

    TABLE = config.TBL_WAREHOUSE_ORDER
    COLUMNS = ImportDoesNotEchoBackTests.COLUMNS

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mark_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(self.src)
        cols = ", ".join(f'"{c}"' for c in self.COLUMNS)
        conn.execute(f'CREATE TABLE "{self.TABLE}"'
                     f" (管理番号 INTEGER PRIMARY KEY, {cols})")
        # **共有側の番号はわざと手元とずらす。** 同じ番号だと、取り違えて
        # いても試験が通ってしまう
        conn.execute(
            f'INSERT INTO "{self.TABLE}" (管理番号, 登録日時, LotNo, 品名,'
            ' 発注コード, 単位, 厚, 幅, 丈, 発注数)'
            " VALUES (41, '2026-01-01 00:00:00', 'L41', 'パレット',"
            " 'P9', '台', 0.0, 0, 0, 1)")
        conn.execute(
            f'INSERT INTO "{self.TABLE}" (管理番号, 登録日時, LotNo, 品名,'
            ' 発注コード, 単位, 厚, 幅, 丈, 発注数)'
            " VALUES (42, '2026-01-01 00:00:00', 'L42', 'パレット',"
            " 'P9', '台', 0.0, 0, 0, 1)")
        conn.commit()
        conn.close()
        self.conn = make_conn()
        self.addCleanup(self.conn.close)

    # -- 覗き見の道具 ------------------------------------------------
    def source_row(self, lot: str) -> dict:
        conn = sqlite3.connect(self.src)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f'SELECT * FROM "{self.TABLE}" WHERE LotNo = ?', (lot,)
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    def local_row(self, lot: str) -> dict:
        row = self.conn.execute(
            f'SELECT * FROM "{self.TABLE}" WHERE LotNo = ?', (lot,)).fetchone()
        return dict(row) if row else {}

    def mgr_no(self, lot: str) -> int:
        return int(self.local_row(lot)["管理番号"])

    # -- 取り込みで受け取った行 --------------------------------------
    def test_確認の印が共有へ届く(self) -> None:
        data_sync.import_master(self.conn, self.src)
        svc.confirm_order(self.conn, self.mgr_no("L41"))
        data_sync.write_back(self.conn, self.src)
        self.assertEqual(self.source_row("L41")["確認済み"], "1")
        self.assertTrue(self.source_row("L41")["確認日時"])

    def test_印を付けた行だけが変わる(self) -> None:
        """**書き換えは足すのと違って、取り違えると別の発注を汚す。**"""
        data_sync.import_master(self.conn, self.src)
        svc.confirm_order(self.conn, self.mgr_no("L41"))
        data_sync.write_back(self.conn, self.src)
        self.assertIn(self.source_row("L42")["確認済み"], (None, ""))

    def test_手元の番号ではなく共有の番号で当てている(self) -> None:
        """手元は1,2 / 共有は41,42。手元の番号で書いたら当たらない。"""
        data_sync.import_master(self.conn, self.src)
        self.assertEqual(self.local_row("L41")["取込元管理番号"], 41)
        self.assertNotEqual(self.mgr_no("L41"), 41)

    def test_取り込み直しても印は消えない(self) -> None:
        """いちばん効くところ。翌朝の起動で消えていたのがこれ。"""
        data_sync.import_master(self.conn, self.src)
        svc.confirm_order(self.conn, self.mgr_no("L41"))
        data_sync.write_back(self.conn, self.src)
        data_sync.import_master(self.conn, self.src)
        self.assertEqual(self.local_row("L41")["確認済み"], "1")

    def test_印を付けた直後は送り待ちとして数える(self) -> None:
        """総入れ替えの前に見る数(`_unsent_writeback_tables`)に入ること。

        ここに入らないと、取り込みが何の遠慮もなく印を消していく。
        """
        data_sync.import_master(self.conn, self.src)
        svc.confirm_order(self.conn, self.mgr_no("L41"))
        self.assertEqual(
            data_sync._unsent_writeback_tables(self.conn), {self.TABLE: 1})

    def test_取り込みは先に印を送ってから入れ替える(self) -> None:
        """送り待ちがあっても止まらない。**先に送ってから**入れ替える。"""
        data_sync.import_master(self.conn, self.src)
        svc.confirm_order(self.conn, self.mgr_no("L41"))
        result = data_sync.import_master(self.conn, self.src)   # 書き戻さずに
        self.assertIn(self.TABLE, result.imported)              # 入れ替わり
        self.assertEqual(self.source_row("L41")["確認済み"], "1")  # 印は届いた
        self.assertEqual(self.local_row("L41")["確認済み"], "1")   # 消えてない

    def test_どうしても送れない印は取り込みを見送らせる(self) -> None:
        """送れないまま入れ替えたら、手元にしか無い印が消える。

        ここでは共有の行そのものが消えている場合を作る(誰かが消した、
        取り込み元を差し替えた等)。印の行き先が無いので送れない ──
        そのときは**その表を取り込まない**のが正しい。
        """
        data_sync.import_master(self.conn, self.src)
        svc.confirm_order(self.conn, self.mgr_no("L41"))
        conn = sqlite3.connect(self.src)
        conn.execute(f'DELETE FROM "{self.TABLE}" WHERE 管理番号 = 41')
        conn.commit()
        conn.close()

        result = data_sync.import_master(self.conn, self.src)
        self.assertNotIn(self.TABLE, result.imported)
        self.assertEqual(self.local_row("L41")["確認済み"], "1")
        self.assertTrue(any("見送りました" in e for e in result.errors),
                        result.errors)

    def test_送り終われば見送りは解ける(self) -> None:
        data_sync.import_master(self.conn, self.src)
        svc.confirm_order(self.conn, self.mgr_no("L41"))
        data_sync.write_back(self.conn, self.src)
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn), {})

    # -- 手元で作って送った行 ----------------------------------------
    def test_手元で出した発注の取消も届く(self) -> None:
        """こちらは取込元管理番号を持たない。送信IDで相手の行を決める。"""
        data_sync.import_master(self.conn, self.src)
        made = svc.create_order(
            self.conn, lot_no="NEW", hinmei="パレット", hatchu_code="P9",
            tani="台", atu=3.0, haba=1000, take=2000, hatchu_suu=1)
        self.conn.commit()
        data_sync.write_back(self.conn, self.src)          # まず行を送る
        self.assertEqual(self.source_row("NEW")["LotNo"], "NEW")

        svc.cancel_order(self.conn, made.mgr_no)
        data_sync.write_back(self.conn, self.src)          # 次に印を送る
        self.assertEqual(self.source_row("NEW")["取り消し済"], "1")

    def test_送る前に取り消したぶんは行と一緒に届く(self) -> None:
        """まだ送っていない行に印を付けても、送信は1回で足りる。"""
        data_sync.import_master(self.conn, self.src)
        made = svc.create_order(
            self.conn, lot_no="NEW", hinmei="パレット", hatchu_code="P9",
            tani="台", atu=3.0, haba=1000, take=2000, hatchu_suu=1)
        self.conn.commit()
        svc.cancel_order(self.conn, made.mgr_no)
        data_sync.write_back(self.conn, self.src)
        self.assertEqual(self.source_row("NEW")["取り消し済"], "1")
        # 送り終わったら見送りの理由も残らない
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn), {})

    # -- 手元にしか無い列 --------------------------------------------
    def test_手元だけの列は共有へ送らない(self) -> None:
        """共有にその列は無い。混ぜると1行も入らなくなる。"""
        data_sync.import_master(self.conn, self.src)
        svc.create_order(
            self.conn, lot_no="NEW", hinmei="パレット", hatchu_code="P9",
            tani="台", atu=3.0, haba=1000, take=2000, hatchu_suu=1)
        self.conn.commit()
        result = data_sync.write_back(self.conn, self.src)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.source_row("NEW")["LotNo"], "NEW")


class OldLocalDbGetsMarkColumnsTests(unittest.TestCase):
    """**前の版から入れ替えた端末にも、足した2列が入ること。**

    `CREATE TABLE IF NOT EXISTS` はすでにある表には何もしない。列が
    無いままだと確認の印が共有へ届かず、次の取り込みで消える ── つまり
    直したはずの不具合が、入れ替えた端末にだけ残る。
    """

    TABLE = config.TBL_WAREHOUSE_ORDER
    ADDED = ("取込元管理番号", "印未反映")

    def old_shape(self) -> sqlite3.Connection:
        """VER2.57.1 までの形の発注テーブルだけを持つ手元DB。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            f'CREATE TABLE "{self.TABLE}" ('
            " 管理番号 INTEGER PRIMARY KEY AUTOINCREMENT,"
            " 登録日時 TEXT, LotNo TEXT NOT NULL, 品名 TEXT NOT NULL,"
            " 発注コード TEXT NOT NULL, 単位 TEXT NOT NULL, 材質 TEXT,"
            " 調質 TEXT, 厚 REAL NOT NULL, 幅 INTEGER NOT NULL,"
            " 丈 INTEGER NOT NULL, 用途コード TEXT, 納入先 TEXT,"
            " 発注数 INTEGER, 取り消し済 TEXT, 取り消し日時 TEXT,"
            " 確認済み TEXT, 確認日時 TEXT)")
        conn.execute(
            f'INSERT INTO "{self.TABLE}"'
            " (登録日時, LotNo, 品名, 発注コード, 単位, 厚, 幅, 丈, 発注数)"
            " VALUES ('2026-01-01 00:00:00','L1','パレット','P9','台',"
            " 0.0, 0, 0, 1)")
        conn.commit()
        return conn

    def columns(self, conn: sqlite3.Connection) -> set:
        return {r[1] for r in conn.execute(
            f'PRAGMA table_info(["{self.TABLE}"])'.replace('"', ''))}

    def test_足りない列は足される(self) -> None:
        conn = self.old_shape()
        self.addCleanup(conn.close)
        self.assertFalse(self.columns(conn) & set(self.ADDED))
        db.apply_schema(conn)
        self.assertTrue(set(self.ADDED) <= self.columns(conn))

    def test_すでにある行は消えない(self) -> None:
        """列を足すだけ。**中の発注は触らない。**"""
        conn = self.old_shape()
        self.addCleanup(conn.close)
        db.apply_schema(conn)
        row = conn.execute(f'SELECT * FROM "{self.TABLE}"').fetchone()
        self.assertEqual(row["LotNo"], "L1")
        self.assertIsNone(row["取込元管理番号"])

    def test_二度当てても落ちない(self) -> None:
        """起動のたびに当たる。2回目に「列がもうある」で落ちないこと。"""
        conn = self.old_shape()
        self.addCleanup(conn.close)
        db.apply_schema(conn)
        db.apply_schema(conn)
        self.assertTrue(set(self.ADDED) <= self.columns(conn))


class RefreshOrdersTests(unittest.TestCase):
    """**開いたままでも、届いた発注に気づくこと。**

    取り込みは起動時と手動だけで、画面の自動更新は無かった。資材の端末を
    朝から開きっぱなしにすると、その朝の一覧を一日出し続ける ── しかも
    「最新にする」は手元DBを引き直すだけなので、押しても出てこない。
    現場と資材で「送った」「来ていない」が食い違う形になっていた。
    """

    TABLE = config.TBL_WAREHOUSE_ORDER
    COLUMNS = ImportDoesNotEchoBackTests.COLUMNS

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="refresh_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(self.src)
        cols = ", ".join(f'"{c}"' for c in self.COLUMNS)
        conn.execute(f'CREATE TABLE "{self.TABLE}"'
                     f" (管理番号 INTEGER PRIMARY KEY, {cols})")
        conn.commit()
        conn.close()
        self.add_order("L1")
        self.conn = make_conn()
        self.addCleanup(self.conn.close)
        sources._last_stamp.clear()
        self.addCleanup(sources._last_stamp.clear)

    def add_order(self, lot: str) -> None:
        """現場が発注を1件出した(共有のファイルに行が増える)。"""
        conn = sqlite3.connect(self.src)
        conn.execute(
            f'INSERT INTO "{self.TABLE}"'
            " (登録日時, LotNo, 品名, 発注コード, 単位, 厚, 幅, 丈, 発注数)"
            " VALUES ('2026-01-01 00:00:00', ?, 'パレット', 'P9', '台',"
            " 0.0, 0, 0, 1)", (lot,))
        conn.commit()
        conn.close()
        # 更新時刻の刻みが細かすぎて、同じ秒だと見分けが付かない環境がある
        time.sleep(0.01)

    def lots(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            f'SELECT LotNo FROM "{self.TABLE}" ORDER BY 管理番号')]

    # -- 1. 押されたとき(念押し) -----------------------------------
    def test_押せば取り込み元まで見に行く(self) -> None:
        data_sync.import_master(self.conn, self.src)
        self.add_order("L2")                       # 現場が昼に1件出した
        self.assertEqual(self.lots(), ["L1"])      # まだ手元には無い

        got = data_sync.refresh_orders(self.conn, self.src)
        self.assertTrue(got.updated)
        self.assertEqual(self.lots(), ["L1", "L2"])

    def test_変わっていなければそう言う(self) -> None:
        """押した意味を画面に出す。黙って何も起きないのが一番困る。"""
        data_sync.import_master(self.conn, self.src)
        got = data_sync.refresh_orders(self.conn, self.src)
        self.assertTrue(got.looked)
        self.assertFalse(got.changed)
        self.assertIn("新しいものはありません", got.message())

    def test_取り込み元が無ければ理由を言う(self) -> None:
        got = data_sync.refresh_orders(self.conn, self.dir / "無い.sqlite3")
        self.assertFalse(got.looked)
        self.assertEqual(self.lots(), [])

    # -- 2. 見張り ---------------------------------------------------
    def test_見張りは変わったときだけ取り込む(self) -> None:
        data_sync.import_master(self.conn, self.src)
        got = data_sync.refresh_orders(self.conn, self.src, only_if_changed=True)
        self.assertFalse(got.changed)
        self.assertEqual(got.imported, 0)          # 開いてすらいない

        self.add_order("L2")
        got = data_sync.refresh_orders(self.conn, self.src, only_if_changed=True)
        self.assertTrue(got.updated)
        self.assertEqual(self.lots(), ["L1", "L2"])

    def test_起動直後に取り込み直さない(self) -> None:
        """起動時の自動取り込みの直後に、見張りがもう一度取り込まない。"""
        data_sync.import_master(self.conn, self.src)
        got = data_sync.refresh_orders(self.conn, self.src, only_if_changed=True)
        self.assertFalse(got.changed)

    def test_一度も取り込んでいなければ見に行く(self) -> None:
        """分からないときは**見落とさない方へ倒す**。"""
        got = data_sync.refresh_orders(self.conn, self.src, only_if_changed=True)
        self.assertTrue(got.changed)
        self.assertEqual(self.lots(), ["L1"])

    def test_取り込み元に無い表があっても取り込み直し続けない(self) -> None:
        """**1つでも読めたなら、読んだと覚える。**

        「1つも失敗しなかったら覚える」にすると、取り込み元に無い表が
        1つでもあるかぎり永久に覚えず、見張りが60秒ごとに総入れ替えを
        走らせ続ける(実際にそうなっていた。共有に パレット入出庫履歴 が
        無い環境で踏んだ)。読めなかった表は待っても読めるようにならない。
        """
        data_sync.import_master(self.conn, self.src)
        self.add_order("L2")
        first = data_sync.refresh_orders(self.conn, self.src,
                                         only_if_changed=True)
        self.assertTrue(first.changed)
        self.assertTrue(first.errors)              # 入出庫履歴は元に無い
        second = data_sync.refresh_orders(self.conn, self.src,
                                          only_if_changed=True)
        self.assertFalse(second.changed)           # 2回目は行かない

    def test_何も読めなければ次にやり直す(self) -> None:
        """1つも読めなかったのは「読んだ」ではない。"""
        sources._last_stamp.clear()
        with mock.patch.object(imports, "import_tables") as fake:
            fake.return_value = data_sync.ImportResult(
                errors=["資材パレット注文管理: 読めませんでした"])
            got = data_sync.refresh_orders(self.conn, self.src)
        self.assertEqual(got.tables, 0)
        self.assertTrue(data_sync.source_changed(self.src))

    # -- 消してはいけないもの ----------------------------------------
    def test_送れていない発注は消さない(self) -> None:
        """総入れ替えなので、手元にしか無いものを先に送る。"""
        data_sync.import_master(self.conn, self.src)
        svc.create_order(self.conn, lot_no="MINE", hinmei="パレット",
                         hatchu_code="P9", tani="台", atu=3.0, haba=1000,
                         take=2000, hatchu_suu=1)
        self.conn.commit()
        self.add_order("L2")
        data_sync.refresh_orders(self.conn, self.src)
        self.assertIn("MINE", self.lots())
        self.assertIn("L2", self.lots())

    def test_送れていない確認の印も消さない(self) -> None:
        data_sync.import_master(self.conn, self.src)
        mgr = self.conn.execute(
            f'SELECT 管理番号 FROM "{self.TABLE}" WHERE LotNo = ?',
            ("L1",)).fetchone()[0]
        svc.confirm_order(self.conn, mgr)
        self.add_order("L2")
        data_sync.refresh_orders(self.conn, self.src)
        row = self.conn.execute(
            f'SELECT 確認済み FROM "{self.TABLE}" WHERE LotNo = ?',
            ("L1",)).fetchone()
        self.assertEqual(row[0], "1")
