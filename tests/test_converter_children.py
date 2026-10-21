"""閉じても Access の変換の子プロセスを残さない(VER4.8.84)。

all-tools と共通の不具合 ⑤:
    デスクトップ版を閉じるとき、本物の Python が残ってフォルダを掴み続けることがある。
このツールでは、「表を持ってくる」が Access を変換するために起こす子の Python がそれだった。
変換中に閉じると、親は数秒で強制的に終わり(`os._exit`)、子は親を失ったまま走り続けた。
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packaging_tool import table_bring  # noqa: E402

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


class ChildTests(unittest.TestCase):
    def start_sleeper(self) -> tuple[threading.Thread, dict]:
        box: dict = {}

        def run() -> None:
            try:
                box["done"] = table_bring._run_child(SLEEPER, cwd=str(ROOT), env=None, timeout=60)
            except Exception as exc:              # noqa: BLE001
                box["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not table_bring.converting() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(table_bring.converting())
        self.addCleanup(table_bring.kill_children)
        return thread, box

    def test_変換中は覚えていて_止められる(self) -> None:
        thread, box = self.start_sleeper()
        started = time.monotonic()
        self.assertEqual(table_bring.kill_children(), 1)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 10)
        self.assertNotEqual(box["done"].returncode, 0)
        self.assertFalse(table_bring.converting())

    def test_時間切れなら子を止める(self) -> None:
        import subprocess
        with self.assertRaises(subprocess.TimeoutExpired):
            table_bring._run_child(SLEEPER, cwd=str(ROOT), env=None, timeout=0.5)
        self.assertFalse(table_bring.converting())

    def test_終わり方の後始末で子を止める(self) -> None:
        import start_app
        thread, _box = self.start_sleeper()
        start_app._stop_children()
        thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_変換中は閉じる前に聞く(self) -> None:
        from app.routes import health
        with mock.patch.object(table_bring, "converting", return_value=True):
            self.assertIn("Access の変換(表を持ってくる)", health._running_jobs())
        with mock.patch.object(table_bring, "converting", return_value=False):
            self.assertNotIn("Access の変換(表を持ってくる)", health._running_jobs())

    def test_外枠が閉じたら待つ前に子を止める(self) -> None:
        source = (ROOT / "bridge.py").read_text(encoding="utf-8")
        kill = source.index("bring.kill_children()")
        wait = source.index("pool.shutdown(wait=True")
        self.assertLess(kill, wait)


if __name__ == "__main__":
    unittest.main()
