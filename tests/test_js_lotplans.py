"""画面側(`lotplans_view.js`)の予定ロット: 確認済みを畳む(VER4.8.91)を Node で動かして確かめる。

「確認済みは畳んで、押したときだけ出すようにしてください」。開け閉めは画面側の動きなので
サーバの試験では見えない(`tests/js/lotplans_fold.mjs` が本体)。
Node が無い環境では飛ばす(`test_js_lotlist.py` と同じ。配布・実行には不要)。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "js" / "lotplans_fold.mjs"
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node が無いためスキップ(配布・実行には不要)")
class LotPlansFoldTests(unittest.TestCase):
    def test_確認済みは畳んで押したときだけ出す(self) -> None:
        result = subprocess.run(
            [_NODE, str(_SCRIPT)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, f"\n{result.stdout}\n{result.stderr}")
