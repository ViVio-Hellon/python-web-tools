"""ログの書き出し先。

**開いたままにする端末のための試験。** 書き出し先を起動時に1度だけ
決めていたので、日付をまたいだ端末は翌日ぶんを前日のファイルへ書き
続けていた(13日の不具合を追うのに、13日のログが12日のファイルに
入っている)。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import logging_utils  # noqa: E402


class DailyFileHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="log_"))
        patcher = unittest.mock.patch.object(
            logging_utils.config, "LOG_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        # **今日がいつかを試験が決める。**
        #
        # 実際の日付に寄りかかると、その日しか通らない試験になる ──
        # 「9/12 に起動した」つもりの手口が、9/13 に走らせると
        # 初めの1行から日またぎの扱いになって落ちた(実際に落ちた)。
        # 直したいのは「日付が変わったら書き先も変わる」ことなので、
        # いつ走らせても同じ筋になるよう、こちらで日付を握る
        self.today = date(2026, 9, 12)
        clock = unittest.mock.patch.object(logging_utils, "date")
        fake = clock.start()
        fake.today.side_effect = lambda: self.today
        self.addCleanup(clock.stop)

    def record(self, text: str) -> logging.LogRecord:
        return logging.LogRecord("packaging_tool.試験", logging.INFO,
                                 __file__, 1, text, None, None)

    def names(self) -> list[str]:
        return sorted(p.name for p in self.dir.glob("*.log"))

    def test_その日のファイルに書く(self) -> None:
        handler = logging_utils.DailyFileHandler(date(2026, 9, 12),
                                                 encoding="utf-8")
        self.addCleanup(handler.close)
        handler.emit(self.record("12日の出来事"))
        self.assertEqual(self.names(), ["packaging_tool_20260912.log"])

    def test_日付が変わったら書き先も変わる(self) -> None:
        """**ここが本題。** 夜勤をまたいでも、その日のファイルへ行く。"""
        handler = logging_utils.DailyFileHandler(date(2026, 9, 12),
                                                 encoding="utf-8")
        self.addCleanup(handler.close)
        handler.emit(self.record("12日の出来事"))

        self.today = date(2026, 9, 13)            # 日付が変わった
        handler.emit(self.record("13日の出来事"))

        self.assertEqual(self.names(), ["packaging_tool_20260912.log",
                                        "packaging_tool_20260913.log"])
        self.assertIn("12日の出来事",
                      (self.dir / "packaging_tool_20260912.log").read_text("utf-8"))
        self.assertIn("13日の出来事",
                      (self.dir / "packaging_tool_20260913.log").read_text("utf-8"))

    def test_前の日のぶんは消さない(self) -> None:
        handler = logging_utils.DailyFileHandler(date(2026, 9, 12),
                                                 encoding="utf-8")
        self.addCleanup(handler.close)
        handler.emit(self.record("先に書いたもの"))
        self.today = date(2026, 9, 13)
        handler.emit(self.record("あとで書いたもの"))
        self.assertIn("先に書いたもの",
                      (self.dir / "packaging_tool_20260912.log").read_text("utf-8"))

    def test_名前の作り方は1か所(self) -> None:
        self.assertEqual(logging_utils.log_path_for(date(2026, 1, 2)).name,
                         "packaging_tool_20260102.log")


if __name__ == "__main__":
    unittest.main()
