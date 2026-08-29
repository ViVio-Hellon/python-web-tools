"""画面側(`mapedit.js`)の複数選択を、Nodeで実際に動かして確かめる。

【なぜ Python の試験と別に要るのか】
配置編集の強調表示は**画面側にしか状態が無い**ものがある。サーバの
状態を見る試験では何度やっても素通りするので、実際に

    「配置編集で付いた水色枠が、掴む以外の方法で消せない」

を3度見逃した。バグのある層で動かさないと守れない、という判断でこの
試験を足している(`tests/js/mapedit_selection.mjs` が本体)。

Node が無い環境では飛ばす。このプロジェクトはフロントのビルド工程を
持たない方針(README)なので、Node は**試験のためだけ**に使い、
配布物や実行には一切要らない。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "js" / "mapedit_selection.mjs"
_NODE = shutil.which("node")


@unittest.skipUnless(_NODE, "Node が無いためスキップ(配布・実行には不要)")
class MapEditSelectionTests(unittest.TestCase):
    def test_複数選択の付け外し(self) -> None:
        """`mapedit.js` を読み込んで、押した/掴んだときの選択集合を見る。

        中身の一つひとつは `.mjs` 側に書いてある。ここは走らせて、
        落ちたらその出力をそのまま見せる役。
        """
        result = subprocess.run(
            [_NODE, str(_SCRIPT)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f"\n{result.stdout}\n{result.stderr}")
