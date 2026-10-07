"""Access を読む予備の部品(access_parser)をツールに同梱する。

現場の声:「Access を読む部品がこのPCの Python に入っていません … マイグレーション前には
使用できたはずだが使用できなくなっている」。PC の Python を入れ直した・デスクトップ版が
別の Python で動いている などで、pip で入れた access_parser が見えなくなると
「表を持ってくる」で Access を選べなくなる。

【ここで守りたいこと】
1. 同梱の部品(access_parser・construct・tabulate)が揃っていて、PC に何も入っていない
   Python でも読み込める
2. 変換の別プロセスは同梱の場所を `sys.path` の**最後**に足す(入っていればそちらが先)
3. 使い方の約束(ライセンス)を同梱している
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import table_bring  # noqa: E402


class BundledReaderTests(unittest.TestCase):
    def test_何も入っていないPythonでも同梱の部品で読める(self) -> None:
        # -S: site-packages を読まない(PC に何も入っていない形)
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); sys.path.append(sys.argv[2])\n"
            "import engine\n"
            "print(dict((n, ok) for n, ok, _ in engine.available_engines())['access_parser'])\n"
            "import jet_text_fix; jet_text_fix.apply()\n"
            "from access_parser import AccessParser\n")
        done = subprocess.run(
            [sys.executable, "-S", "-c", script,
             str(table_bring.BUNDLED_CONVERTER), str(table_bring.BUNDLED_LIBS)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "True")

    def test_同梱の場所は最後に足す(self) -> None:
        self.assertIn("sys.path.append(libs)", table_bring._CONVERT_SCRIPT)
        self.assertNotIn("sys.path.insert(0, libs)", table_bring._CONVERT_SCRIPT)

    def test_ライセンスを同梱している(self) -> None:
        for name in ("access_parser", "construct", "tabulate"):
            self.assertTrue((table_bring.BUNDLED_LIBS / name / "__init__.py").is_file(), name)
            self.assertTrue((table_bring.BUNDLED_LIBS / "licenses" / f"{name}-LICENSE.txt").is_file(), name)


if __name__ == "__main__":
    unittest.main()
