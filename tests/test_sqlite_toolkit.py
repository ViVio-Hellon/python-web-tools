"""sqlite_toolkit (汎用SQLiteヘルパー) のテスト。

業務のテーブル名やスキーマに一切依存しないモジュールなので、テストでも
梱包資材ツールとは無関係なテーブル(点検記録)を使う。ロック競合時の
リトライ挙動は、実のsqlite3.Connectionを差し替えるのが難しいため、
`execute`/`with` だけを備えた簡易ダブルで検証する。
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from packaging_tool import sqlite_toolkit as st


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE 点検記録 (id INTEGER PRIMARY KEY, 場所 TEXT, 値 REAL)")
    conn.commit()
    return conn


class _FakeCursor:
    def __init__(self, rows=(), rowcount=1, lastrowid=1):
        self._rows = list(rows)
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchall(self):
        return self._rows


class ScriptedConnection:
    """execute_with_retry / fetch_all のリトライだけを試すための簡易conn。"""

    def __init__(self, fail_times: int = 0, message: str = "database is locked",
                rows=(), rowcount: int = 1, lastrowid: int = 7):
        self.fail_times = fail_times
        self.message = message
        self.rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.calls = 0

    def __enter__(self) -> "ScriptedConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql, params=()):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise sqlite3.OperationalError(self.message)
        return _FakeCursor(self.rows, self.rowcount, self.lastrowid)


class LockErrorTests(unittest.TestCase):
    def test_lock_snippets_are_recognised(self):
        for message in ("database is locked", "database table is locked", "busy"):
            self.assertTrue(st.is_lock_error(sqlite3.OperationalError(message)), message)

    def test_other_sqlite_errors_are_not_lock_conflicts(self):
        self.assertFalse(st.is_lock_error(sqlite3.IntegrityError("UNIQUE constraint failed")))

    def test_non_sqlite_exceptions_are_never_lock_errors(self):
        """メッセージが似ていても、sqlite3由来でなければロックとは判定しない。"""
        self.assertFalse(st.is_lock_error(RuntimeError("database is locked")))


class ExecuteWithRetryTests(unittest.TestCase):
    def test_a_successful_insert_reports_rowcount_and_lastrowid(self):
        conn = make_conn()
        try:
            result = st.execute_with_retry(
                conn, "INSERT INTO 点検記録 (場所, 値) VALUES (?, ?)", ("A", 1.0))
            self.assertTrue(result.ok)
            self.assertEqual(result.rowcount, 1)
            self.assertIsNotNone(result.lastrowid)
        finally:
            conn.close()

    def test_a_lock_conflict_is_retried_until_it_succeeds(self):
        conn = ScriptedConnection(fail_times=2)
        with mock.patch.object(st.time, "sleep") as sleep:
            result = st.execute_with_retry(conn, "INSERT ...", retry_wait_sec=0.01)
        self.assertTrue(result.ok)
        self.assertEqual(conn.calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_it_gives_up_after_the_retry_limit(self):
        conn = ScriptedConnection(fail_times=99)
        with mock.patch.object(st.time, "sleep"):
            result = st.execute_with_retry(conn, "INSERT ...", max_retry=2)
        self.assertFalse(result.ok)
        self.assertEqual(conn.calls, 3)   # 最初の1回 + リトライ2回

    def test_a_non_lock_error_fails_immediately(self):
        conn = ScriptedConnection(fail_times=99, message="UNIQUE constraint failed")
        with mock.patch.object(st.time, "sleep") as sleep:
            result = st.execute_with_retry(conn, "INSERT ...")
        self.assertFalse(result.ok)
        self.assertEqual(conn.calls, 1)
        sleep.assert_not_called()


class FetchTests(unittest.TestCase):
    def test_fetch_all_returns_the_rows(self):
        conn = make_conn()
        try:
            conn.execute("INSERT INTO 点検記録 (場所) VALUES ('A')")
            conn.commit()
            rows = st.fetch_all(conn, "SELECT * FROM 点検記録")
            self.assertEqual(len(rows), 1)
        finally:
            conn.close()

    def test_fetch_all_retries_on_a_lock_conflict(self):
        conn = ScriptedConnection(fail_times=1, rows=[{"a": 1}])
        with mock.patch.object(st.time, "sleep"):
            rows = st.fetch_all(conn, "SELECT ...", retry_wait_sec=0.01)
        self.assertEqual(rows, [{"a": 1}])

    def test_fetch_all_returns_none_on_a_non_lock_failure(self):
        conn = ScriptedConnection(fail_times=99, message="そもそも列がありません")
        with mock.patch.object(st.time, "sleep"):
            rows = st.fetch_all(conn, "SELECT ...")
        self.assertIsNone(rows)

    def test_fetch_one_returns_the_first_row_or_none(self):
        conn = make_conn()
        try:
            self.assertIsNone(st.fetch_one(conn, "SELECT * FROM 点検記録"))
            conn.execute("INSERT INTO 点検記録 (場所) VALUES ('A')")
            conn.commit()
            row = st.fetch_one(conn, "SELECT 場所 FROM 点検記録")
            self.assertEqual(row["場所"], "A")
        finally:
            conn.close()


class UpdateRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()
        self.conn.execute("INSERT INTO 点検記録 (場所, 値) VALUES ('A', 1.0)")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_updates_an_existing_row(self):
        result = st.update_record(self.conn, "点検記録", "id", 1, {"値": 2.0})
        self.assertTrue(result.ok)
        self.assertEqual(result.rowcount, 1)
        row = self.conn.execute("SELECT 値 FROM 点検記録 WHERE id = 1").fetchone()
        self.assertEqual(row[0], 2.0)

    def test_a_missing_row_is_reported_as_not_found(self):
        result = st.update_record(self.conn, "点検記録", "id", 999, {"値": 2.0})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    def test_a_where_clause_can_replace_the_key_field(self):
        result = st.update_record(
            self.conn, "点検記録", "id", None, {"値": 5.0},
            where_clause="場所 = ?", where_params=("A",))
        self.assertTrue(result.ok)
        row = self.conn.execute("SELECT 値 FROM 点検記録 WHERE 場所 = 'A'").fetchone()
        self.assertEqual(row[0], 5.0)


class InsertRecordTests(unittest.TestCase):
    def test_inserts_a_new_row_and_returns_its_id(self):
        conn = make_conn()
        try:
            result = st.insert_record(conn, "点検記録", {"場所": "B", "値": 3.0})
            self.assertTrue(result.ok)
            row = conn.execute(
                "SELECT 場所, 値 FROM 点検記録 WHERE id = ?", (result.lastrowid,)).fetchone()
            self.assertEqual((row[0], row[1]), ("B", 3.0))
        finally:
            conn.close()


class UtilityTests(unittest.TestCase):
    def test_now_db_string_is_sortable_iso_like_format(self):
        text = st.now_db_string()
        # "YYYY-MM-DD HH:MM:SS" の形になっていること
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_sanitize_for_db_flattens_tabs_and_newlines(self):
        self.assertEqual(st.sanitize_for_db("上\n下\tよこ\r\n"), "上 下 よこ")

    def test_enable_wal_does_not_raise_even_when_wal_is_unavailable(self):
        """:memory:DBはWALにできないが、例外にはせず既定のまま続けること。"""
        conn = sqlite3.connect(":memory:")
        try:
            st.enable_wal(conn)   # 例外を投げなければOK
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
