"""画面側(`lotlist.js`)の「ロットを探す」欄を、Nodeで実際に動かして確かめる。

打つそばから検索するため、返事が打ち足した後に届くと欄が古い文字に戻り、
打ち足した文字が消えていた(現場の声: 文字が差し戻る)。画面側の順番の
問題なのでサーバの試験では見えない(`tests/js/lotlist_typing.mjs` が本体)。

Node が無い環境では飛ばす(`test_js_mapedit.py` と同じ。配布・実行には不要)。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "js" / "lotlist_typing.mjs"
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node が無いためスキップ(配布・実行には不要)")
class LotListTypingTests(unittest.TestCase):
    def test_打った文字が差し戻らない(self) -> None:
        result = subprocess.run(
            [_NODE, str(_SCRIPT)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f"\n{result.stdout}\n{result.stderr}")
