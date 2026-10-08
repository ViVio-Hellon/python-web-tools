"""GitHub Actions(デスクトップ版 Windows)の作り。

Node.js 20 で動く版の actions は打ち切り(ログに「Node.js 20 is deprecated … forced to run on
Node.js 24」と出ていた)。Node.js 24 で動く版より古いものに戻さない。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/desktop-windows.yml"
# Node.js 24 で動く最初の版(action.yml の runs.using を確かめた)
NODE24_FROM = {"actions/checkout": 5, "actions/setup-python": 6, "actions/upload-artifact": 6}


class WorkflowTests(unittest.TestCase):
    def test_NodeJS20の版を使わない(self) -> None:
        text = WORKFLOW.read_text("utf-8")
        used = re.findall(r"uses:\s*([\w.-]+/[\w.-]+)@v(\d+)", text)
        seen = {name for name, _ in used}
        for name, major in used:
            if name in NODE24_FROM:
                with self.subTest(action=name):
                    self.assertGreaterEqual(int(major), NODE24_FROM[name])
        self.assertTrue(set(NODE24_FROM) <= seen, seen)

    def test_試験と起動の確かめを続ける(self) -> None:
        text = WORKFLOW.read_text("utf-8")
        for step in ("python -m pytest", "cargo test --release", "scripts/desktop_smoke.py"):
            self.assertIn(step, text)


if __name__ == "__main__":
    unittest.main()
