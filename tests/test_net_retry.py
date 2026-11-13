"""共有がいったん応答しないときは、待って聞き直す(VER4.8.137。`source_db._exists_retrying`)

現場のログ: 仕掛台帳(\\\\nlmsrvngy03\\...\\SIKALOT.sqlite3)への最初の1回で
`[WinError 59] 予期しないネットワーク エラー` が出て、取り込みごと落ちていた(8回)。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import source_db  # noqa: E402


class NetRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "SIKALOT.sqlite3"
        self.path.write_bytes(b"x")
        patcher = mock.patch.object(source_db, "NET_RETRY_SEC", (0.0, 0.0, 0.0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def flaky(self, failures: int):
        real = Path.stat
        count = {"n": 0}

        def stat(path, *a, **kw):
            if path == self.path and count["n"] < failures:
                count["n"] += 1
                raise OSError(59, "予期しないネットワーク エラーが発生しました。")
            return real(path, *a, **kw)
        return mock.patch.object(Path, "stat", stat), count

    def test_一度応答しなくても聞き直して読める(self) -> None:
        patcher, count = self.flaky(1)
        with patcher:
            self.assertTrue(source_db._exists_retrying(self.path))
        self.assertEqual(count["n"], 1)

    def test_ずっと応答しなければ元の失敗を出す(self) -> None:
        patcher, _count = self.flaky(99)
        with patcher, self.assertRaises(OSError):
            source_db._exists_retrying(self.path)

    def test_無いファイルは聞き直さない(self) -> None:
        self.assertFalse(source_db._exists_retrying(self.path.with_name("無い.sqlite3")))
