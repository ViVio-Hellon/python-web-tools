"""取り込みの進捗表示のテスト(VBA `frmProgress` / `UpdateProgress`)。

VBA版は DB を読む間 `frmProgress` を出して「何%か・いま何をしているか」を
見せていた。Python版でも同じことができているかを確かめる。

サービス層(`data_sync`)は進捗を**コールバックで**渡すだけにしてある。
画面がどう出すかは知らないので、表示環境が無くても確かめられる
(Web版は `jobs.py` が受けて `/api/jobs` で返す)。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import data_sync  # noqa: E402


class ProgressCallbackTests(unittest.TestCase):
    """`import_tables` がテーブルごとに進捗を通知するか。"""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE [甲] (名前 TEXT)")
        self.conn.execute("CREATE TABLE [乙] (名前 TEXT)")
        self.specs = {
            "甲": [("名前", "名前", str)],
            "乙": [("名前", "名前", str)],
        }

    def tearDown(self) -> None:
        self.conn.close()

    def _import(self, **kwargs):
        seen: list[tuple[int, str]] = []
        with mock.patch.object(data_sync, "read_table",
                               return_value=[{"名前": "あ"}]):
            data_sync.import_tables(
                self.conn, Path("dummy.sqlite3"), self.specs,
                progress=lambda pct, message, ok=True: seen.append((pct, message)),
                **kwargs)
        return seen

    def test_each_table_reports_before_it_is_read(self):
        seen = self._import()
        self.assertEqual([pct for pct, _ in seen], [0, 50, 100])
        self.assertIn("甲", seen[0][1])
        self.assertIn("乙", seen[1][1])

    def test_the_last_notice_clears_the_message(self):
        """終わったら空文字を送って表示を消す(VBAも閉じていた)。"""
        self.assertEqual(self._import()[-1], (100, ""))

    def test_a_range_keeps_the_percentages_inside_it(self):
        """マスタは0-50%、仕掛台帳は50-100%、と持ち場を分ける。"""
        seen = self._import(progress_range=(50, 100))
        self.assertEqual([pct for pct, _ in seen], [50, 75, 100])

    def test_a_failed_table_reports_ok_false_without_changing_message(self):
        """1テーブルの失敗は、その段の文言を変えずに ok=False で伝える(バグ修正)。

        文言を変えて知らせると `jobs.py` 側で「新しい段が始まった」と
        誤認され、失敗した段自身ではなく次の段が失敗扱いになって
        しまう。同じ文言のまま ok=False だけを送る必要がある。
        """
        seen: list[tuple[int, str, bool]] = []

        def fake_read_table(_path, table):
            if table == "甲":
                raise data_sync.SyncError("読めません")
            return [{"名前": "あ"}]

        with mock.patch.object(data_sync, "read_table", side_effect=fake_read_table):
            data_sync.import_tables(
                self.conn, Path("dummy.sqlite3"), self.specs,
                progress=lambda pct, message, ok=True: seen.append((pct, message, ok)))

        self.assertEqual(seen[0], (0, "甲 を読み込み中...", True))
        self.assertEqual(seen[1], (0, "甲 を読み込み中...", False))
        # 乙は無事なので、段の文言は変わってもokはTrueのまま
        self.assertEqual(seen[2], (50, "乙 を読み込み中...", True))

    def test_it_still_works_without_a_listener(self):
        """進捗の受け取り手がいなくても落ちない(スクリプトからの利用)。"""
        with mock.patch.object(data_sync, "read_table",
                               return_value=[{"名前": "あ"}]):
            result = data_sync.import_tables(
                self.conn, Path("dummy.sqlite3"), self.specs)
        self.assertEqual(result.total, 2)


class AutoImportInBackgroundTests(unittest.TestCase):
    """起動時の取り込みは「始めたか」を返し、終われば必ず知らせる。"""

    def test_it_reports_that_it_did_not_start_when_turned_off(self):
        with mock.patch("packaging_tool.user_settings.get", return_value=False):
            done: list = []
            started = data_sync.auto_import_in_background(done.append)
        self.assertFalse(started)
        self.assertEqual(done, [])

    def test_a_failure_still_calls_back(self):
        """途中で落ちても呼び返す。でないと進捗表示が出たまま残る。"""
        import threading

        finished = threading.Event()
        results: list = []

        def capture(result):
            results.append(result)
            finished.set()

        with mock.patch("packaging_tool.user_settings.get", return_value=True), \
                mock.patch.object(data_sync.db, "connect",
                                  side_effect=RuntimeError("接続できません")):
            started = data_sync.auto_import_in_background(capture)
            self.assertTrue(started)
            self.assertTrue(finished.wait(5), "終了が通知されませんでした")

        self.assertFalse(results[0].ok)
        self.assertIn("接続できません", results[0].errors[0])


if __name__ == "__main__":                # pragma: no cover
    unittest.main()
