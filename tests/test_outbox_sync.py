"""outbox_sync (汎用SQLite→Access書き戻しエンジン) のテスト。

このモジュールは業務固有のテーブル名を一切知らないので、テストでも
梱包資材ツールとは無関係なテーブル名(点検記録 等)を使い、
「どのプロジェクトに持っていっても同じ動きになる」ことを確かめる。
Accessへの実接続は要らないので、`ScriptedAccess` という単純な
ダブルで代用する。
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from packaging_tool import outbox_sync, source_db
from packaging_tool.outbox_sync import WriteBackSpec

# テスト全体で使う、業務と無関係な書き戻し対象の例
SPEC = WriteBackSpec(sqlite_table="点検記録", access_table="T_点検記録", key_column="id")


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE 点検記録 (id INTEGER PRIMARY KEY, 場所 TEXT)")
    conn.commit()
    return conn


def insert_row(conn: sqlite3.Connection, label: str) -> None:
    conn.execute("INSERT INTO 点検記録 (場所) VALUES (?)", (label,))
    conn.commit()


class ScriptedAccess:
    """Accessの代わりに使う単純なダブル。副作用を差し替えて挙動を試せる。"""

    def __init__(self, path: str = "m.sqlite3") -> None:
        self.path = Path(path)
        self.ddl_log: list[str] = []
        self.insert_log: list[tuple[str, dict]] = []
        self.execute_side_effect = None   # callable(sql) -> None (raiseしてよい)
        self.insert_side_effect = None    # callable(table, values) -> None (raiseしてよい)

    def execute(self, sql: str) -> int:
        self.ddl_log.append(sql)
        if self.execute_side_effect is not None:
            self.execute_side_effect(sql)
        return 0

    def insert(self, table: str, values: dict) -> int:
        self.insert_log.append((table, dict(values)))
        if self.insert_side_effect is not None:
            self.insert_side_effect(table, values)
        return 1

    def close(self) -> None:
        pass


class SyncTableTests(unittest.TestCase):
    def test_ensure_sync_table_is_idempotent_and_has_the_expected_columns(self):
        conn = make_conn()
        outbox_sync.ensure_sync_table(conn)
        outbox_sync.ensure_sync_table(conn)  # 2回目もエラーにならない
        columns = {row[1] for row in conn.execute(
            f"PRAGMA table_info([{outbox_sync.SYNC_LOG_TABLE}])")}
        self.assertEqual(columns, {"テーブル名", "行ID", "送信ID", "同期日時", "状態"})


class PendingRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()
        insert_row(self.conn, "A")
        insert_row(self.conn, "B")

    def tearDown(self) -> None:
        self.conn.close()

    def test_pending_rows_start_as_everything(self):
        self.assertEqual(len(outbox_sync.pending_rows(self.conn, SPEC)), 2)

    def test_marked_rows_are_not_pending_again(self):
        outbox_sync.mark_synced(self.conn, SPEC, [1])
        rows = outbox_sync.pending_rows(self.conn, SPEC)
        self.assertEqual([r["id"] for r in rows], [2])

    def test_unsent_tables_reports_only_tables_with_pending_rows(self):
        missing_table_spec = WriteBackSpec(
            sqlite_table="存在しないテーブル", access_table="T_不良報告", key_column="id")
        remaining = outbox_sync.unsent_tables(self.conn, [SPEC, missing_table_spec])
        self.assertEqual(remaining, {"点検記録": 2})


class MarkAllSentTests(unittest.TestCase):
    """総入れ替えの取り込み直後に、**全部を送り済みにする**。

    取り込んだ行は取り込み元から来たもの。送り返す必要はないし、
    送り返すと取り込み元が倍になる。
    """

    def setUp(self) -> None:
        self.conn = make_conn()
        insert_row(self.conn, "A")
        insert_row(self.conn, "B")

    def tearDown(self) -> None:
        self.conn.close()

    def test_全部が送信済みになる(self) -> None:
        self.assertEqual(outbox_sync.mark_all_sent(self.conn, SPEC), 2)
        self.assertEqual(outbox_sync.pending_rows(self.conn, SPEC), [])

    def test_古い記録は消す(self) -> None:
        """番号が振り直されると、古い行IDは**別の行**を指す。

        残すと、こちらで新しく登録した行が「送信済み」に化けて、
        二度と送られなくなる。
        """
        outbox_sync.mark_synced(self.conn, SPEC, [1, 2, 3, 4, 5])
        self.conn.execute("DELETE FROM 点検記録")
        insert_row(self.conn, "取り込み直した1件")
        outbox_sync.mark_all_sent(self.conn, SPEC)
        kept = self.conn.execute(
            f"SELECT 行ID FROM [{outbox_sync.SYNC_LOG_TABLE}]"
            " WHERE テーブル名 = ?", (SPEC.sqlite_table,)).fetchall()
        self.assertEqual([r[0] for r in kept],
                         [r["id"] for r in self.conn.execute(
                             "SELECT id FROM 点検記録")])

    def test_あとから足した行はちゃんと送る(self) -> None:
        """全部を済にするのは**そのときテーブルにある行だけ**。"""
        outbox_sync.mark_all_sent(self.conn, SPEC)
        insert_row(self.conn, "新しい発注")
        rows = outbox_sync.pending_rows(self.conn, SPEC)
        self.assertEqual([r["場所"] for r in rows], ["新しい発注"])

    def test_空でも落ちない(self) -> None:
        self.conn.execute("DELETE FROM 点検記録")
        self.assertEqual(outbox_sync.mark_all_sent(self.conn, SPEC), 0)

    def test_何度呼んでも同じ(self) -> None:
        outbox_sync.mark_all_sent(self.conn, SPEC)
        outbox_sync.mark_all_sent(self.conn, SPEC)
        self.assertEqual(outbox_sync.pending_rows(self.conn, SPEC), [])


class ClaimReleaseTests(unittest.TestCase):
    """予約(claim)・確定(mark_synced)・解放(release_claim)の状態遷移。"""

    def setUp(self) -> None:
        self.conn = make_conn()
        insert_row(self.conn, "A")
        insert_row(self.conn, "B")

    def tearDown(self) -> None:
        self.conn.close()

    def test_claim_assigns_a_distinct_op_id_per_row(self):
        rows, op_ids = outbox_sync.claim_rows(self.conn, SPEC)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(op_ids), 2)
        self.assertEqual(len(set(op_ids.values())), 2)

    def test_claiming_twice_returns_nothing_the_second_time(self):
        first, _ = outbox_sync.claim_rows(self.conn, SPEC)
        second, second_ids = outbox_sync.claim_rows(self.conn, SPEC)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        self.assertEqual(second_ids, {})

    def test_mark_synced_keeps_the_op_id_assigned_at_claim_time(self):
        """INSERT OR REPLACEではなくUPSERTなので、済にしても送信IDが消えない。"""
        rows, op_ids = outbox_sync.claim_rows(self.conn, SPEC)
        row_id = int(rows[0]["id"])
        outbox_sync.mark_synced(self.conn, SPEC, [row_id])
        saved = self.conn.execute(
            f"SELECT 送信ID, 状態 FROM [{outbox_sync.SYNC_LOG_TABLE}]"
            " WHERE テーブル名 = ? AND 行ID = ?",
            (SPEC.sqlite_table, row_id)).fetchone()
        self.assertEqual(saved["送信ID"], op_ids[row_id])
        self.assertEqual(saved["状態"], outbox_sync.SYNC_DONE)

    def test_release_claim_lets_the_row_be_claimed_again_with_a_fresh_id(self):
        rows, op_ids = outbox_sync.claim_rows(self.conn, SPEC)
        row_id = int(rows[0]["id"])
        outbox_sync.release_claim(self.conn, SPEC, [row_id])
        again_rows, again_ids = outbox_sync.claim_rows(self.conn, SPEC)
        claimed_ids = {int(r["id"]) for r in again_rows}
        self.assertIn(row_id, claimed_ids)
        self.assertNotEqual(again_ids[row_id], op_ids[row_id])

    def test_a_stale_claim_is_reclaimed_with_the_same_op_id(self):
        """途中で落ちて残った予約は拾い直すが、送信IDは引き継ぐ
        (Access側の一意インデックスで二重登録を防ぐための鍵になる)。"""
        rows, op_ids = outbox_sync.claim_rows(self.conn, SPEC)
        self.conn.execute(
            f"UPDATE [{outbox_sync.SYNC_LOG_TABLE}]"
            " SET 同期日時 = datetime('now','localtime','-60 minutes')"
            " WHERE 状態 = ?", (outbox_sync.SYNC_SENDING,))
        self.conn.commit()
        again_rows, again_ids = outbox_sync.claim_rows(self.conn, SPEC)
        self.assertEqual(len(again_rows), 2)
        self.assertEqual(again_ids, op_ids)


class OpIdColumnTests(unittest.TestCase):
    """ensure_op_id_column: キャッシュと、失敗時に書き戻しを止めないこと。"""

    def setUp(self) -> None:
        outbox_sync._op_id_column_cache.clear()

    def tearDown(self) -> None:
        outbox_sync._op_id_column_cache.clear()

    def test_success_is_cached_so_the_ddl_runs_only_once(self):
        access = ScriptedAccess()
        self.assertTrue(outbox_sync.ensure_op_id_column(access, SPEC))
        self.assertEqual(len(access.ddl_log), 2)   # ALTER + CREATE INDEX
        access.ddl_log.clear()
        self.assertTrue(outbox_sync.ensure_op_id_column(access, SPEC))
        self.assertEqual(access.ddl_log, [])        # 2回目はキャッシュから

    def test_already_exists_counts_as_ready_and_is_cached(self):
        access = ScriptedAccess()

        def already_exists(_sql: str) -> None:
            raise source_db.SourceError("duplicate column name: 送信ID")

        access.execute_side_effect = already_exists
        self.assertTrue(outbox_sync.ensure_op_id_column(access, SPEC))
        access.ddl_log.clear()
        self.assertTrue(outbox_sync.ensure_op_id_column(access, SPEC))
        self.assertEqual(access.ddl_log, [])

    def test_a_lock_conflict_is_not_cached_so_it_retries_next_time(self):
        access = ScriptedAccess()

        def locked(_sql: str) -> None:
            raise source_db.SourceError("database is locked")

        access.execute_side_effect = locked
        self.assertFalse(outbox_sync.ensure_op_id_column(access, SPEC))
        access.ddl_log.clear()
        self.assertFalse(outbox_sync.ensure_op_id_column(access, SPEC))
        self.assertTrue(access.ddl_log, "ロック競合はキャッシュせず再試行するはず")

    def test_a_permanent_failure_is_cached_as_unavailable(self):
        access = ScriptedAccess()

        def denied(_sql: str) -> None:
            raise source_db.SourceError("アクセスが拒否されました")

        access.execute_side_effect = denied
        self.assertFalse(outbox_sync.ensure_op_id_column(access, SPEC))
        access.ddl_log.clear()
        self.assertFalse(outbox_sync.ensure_op_id_column(access, SPEC))
        self.assertEqual(access.ddl_log, [], "無駄なDDLを再試行しないはず")


class WriteBackTests(unittest.TestCase):
    def setUp(self) -> None:
        outbox_sync._op_id_column_cache.clear()
        self.conn = make_conn()

    def tearDown(self) -> None:
        self.conn.close()
        outbox_sync._op_id_column_cache.clear()

    def test_write_back_sends_the_op_id_when_the_guard_is_ready(self):
        insert_row(self.conn, "A")
        access = ScriptedAccess()
        result = outbox_sync.write_back(self.conn, access, [SPEC])
        self.assertEqual(result.sent["点検記録"], 1)
        table, values = access.insert_log[0]
        self.assertEqual(table, "T_点検記録")
        self.assertIn("送信ID", values)
        self.assertTrue(values["送信ID"])
        # 主キー自体は送らない(Access側で採番されるため)
        self.assertNotIn("id", values)

    def test_a_unique_index_violation_is_treated_as_already_delivered(self):
        insert_row(self.conn, "A")
        access = ScriptedAccess()

        def duplicate(_table: str, _values: dict) -> None:
            raise source_db.SourceError("UNIQUE constraint failed: 送信ID")

        access.insert_side_effect = duplicate
        result = outbox_sync.write_back(self.conn, access, [SPEC])
        self.assertEqual(result.sent["点検記録"], 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(outbox_sync.pending_rows(self.conn, SPEC), [])

    def test_when_the_guard_cannot_be_prepared_rows_still_send_without_it(self):
        """列を追加できない環境でも、書き戻し自体は止めない(旧来動作へ自動で落ちる)。"""
        insert_row(self.conn, "A")
        access = ScriptedAccess()

        def denied(_sql: str) -> None:
            raise source_db.SourceError("アクセスが拒否されました")

        access.execute_side_effect = denied
        result = outbox_sync.write_back(self.conn, access, [SPEC])
        self.assertEqual(result.sent["点検記録"], 1)
        _table, values = access.insert_log[0]
        self.assertNotIn("送信ID", values)

    def test_use_op_id_guard_false_skips_schema_changes(self):
        insert_row(self.conn, "A")
        access = ScriptedAccess()
        spec = WriteBackSpec(sqlite_table="点検記録", access_table="T_点検記録",
                             key_column="id", use_op_id_guard=False)
        result = outbox_sync.write_back(self.conn, access, [spec])
        self.assertEqual(result.sent["点検記録"], 1)
        self.assertEqual(access.ddl_log, [])
        _table, values = access.insert_log[0]
        self.assertNotIn("送信ID", values)

    def test_one_bad_row_does_not_block_the_rest(self):
        insert_row(self.conn, "A")
        insert_row(self.conn, "B")
        access = ScriptedAccess()
        calls = {"n": 0}

        def flaky(_table: str, _values: dict) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise source_db.SourceError("型が合いません")

        access.insert_side_effect = flaky
        result = outbox_sync.write_back(self.conn, access, [SPEC])
        self.assertEqual(result.sent["点検記録"], 1)
        self.assertEqual(len(result.errors), 1)
        # 失敗した行は次回また送られる(新しい送信IDで)
        pending = outbox_sync.pending_rows(self.conn, SPEC)
        self.assertEqual(len(pending), 1)

    def test_an_unexpected_exception_leaves_the_row_reserved_for_stale_reclaim(self):
        """AccessErrorではない想定外の例外は、予約を外さずそのまま残す。

        「Accessに届いたかどうか分からない」ケースなので、ここで新しい
        送信IDを振り直して再送すると二重登録の恐れが生まれる。予約を
        残しておけば、期限切れ拾い直しが同じ送信IDで再送し、Access側の
        一意インデックスが安全に二重登録を弾いてくれる。
        """
        insert_row(self.conn, "A")
        access = ScriptedAccess()

        def boom(_table: str, _values: dict) -> None:
            raise RuntimeError("想定外のエラー")

        access.insert_side_effect = boom
        with self.assertRaises(RuntimeError):
            outbox_sync.write_back(self.conn, access, [SPEC])

        # 予約は残ったまま。今すぐは拾われない
        rows, _ = outbox_sync.claim_rows(self.conn, SPEC)
        self.assertEqual(rows, [])
        row = self.conn.execute(
            f"SELECT 状態, 送信ID FROM [{outbox_sync.SYNC_LOG_TABLE}]"
            " WHERE テーブル名 = ?", (SPEC.sqlite_table,)).fetchone()
        self.assertEqual(row["状態"], outbox_sync.SYNC_SENDING)
        self.assertTrue(row["送信ID"])


class ConcurrentWriteBackTests(unittest.TestCase):
    """並行アクセス: 同じ行を二重にAccessへ送らないこと(スレッド越しの検証)。"""

    def setUp(self) -> None:
        outbox_sync._op_id_column_cache.clear()
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._dir.name) / "t.db"
        self.conn = self._open()
        self.conn.execute(
            "CREATE TABLE 点検記録 (id INTEGER PRIMARY KEY, 場所 TEXT)")
        for i in range(6):
            self.conn.execute("INSERT INTO 点検記録 (場所) VALUES (?)", (f"L{i}",))
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
        sent: list[str] = []
        sent_lock = threading.Lock()

        class FakeAccess:
            path = Path("m.sqlite3")

            def execute(self, _sql: str) -> int:
                return 0

            def insert(self, _table: str, values: dict) -> int:
                time.sleep(0.005)          # ネットワーク越しの往復ぶん
                with sent_lock:
                    sent.append(values["場所"])
                return 1

            def close(self) -> None:
                pass

        def run() -> None:
            conn = self._open()
            try:
                outbox_sync.write_back(conn, FakeAccess(), [SPEC])
            finally:
                conn.close()

        threads = [threading.Thread(target=run) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(sent), [f"L{i}" for i in range(6)])
        self.assertEqual(len(sent), len(set(sent)), f"二重送信: {sent}")


if __name__ == "__main__":
    unittest.main()
