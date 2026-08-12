"""アプリは tkinter に触らない

【なぜ要るのか】
**表示環境の無いサーバでも動く**ことの保証です。開発機に tkinter が
入っていると、うっかり掴んでも手元では動いてしまい、置いた先で初めて
落ちます。ここで固定しておけば、`import tkinter` を足した時点で落ちます。

もとは tkinter 版を消せる(消しても Web版が動く)ことを確かめるための
試験でしたが、消したあとも「二度と戻さない」ための網として残しています。

【やり方】
子プロセスで `tkinter` の import を禁止したうえで、アプリが使うものを
片端から読み込む。1つでも掴んでいれば ImportError で落ちる。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"

# tkinter を掴んでいたら落ちる形で読み込むモジュール。
# **Web版が通る道を全部**挙げる(routes → presenters → services)
WEB_MODULES = (
    "app",
    "app.shell",
    "app.routes.catalog", "app.routes.settings", "app.routes.health",
    "app.routes.inventory", "app.routes.layout", "app.routes.log",
    "app.routes.lot", "app.routes.pending", "app.routes.selection",
    "app.routes.spec_sheet", "app.routes.warehouse",
    "packaging_tool.presenters.settings", "packaging_tool.presenters.fs_browse",
    "packaging_tool.presenters.inventory", "packaging_tool.presenters.layout",
    "packaging_tool.presenters.lot", "packaging_tool.presenters.outputs",
    "packaging_tool.presenters.render_json",
    "packaging_tool.presenters.selection", "packaging_tool.presenters.warehouse",
    "packaging_tool.layout_session", "packaging_tool.selection_session",
    "packaging_tool.work_context", "packaging_tool.jobs",
    "packaging_tool.reports", "packaging_tool.printing",
)

# 起動基盤。ここが tkinter を掴むと、表示環境の無いサーバで起動できない
LAUNCH_MODULES = ("launch_guard", "process_manager", "server", "start_app")

_SCRIPT = """
import sys

class _Blocked:
    \"\"\"tkinter を import しようとしたら、その場で落とす。\"\"\"

    def find_module(self, name, path=None):
        return self if name == "tkinter" or name.startswith("tkinter.") else None

    def find_spec(self, name, path=None, target=None):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError(name + " は Web版から使ってはいけません")
        return None

sys.meta_path.insert(0, _Blocked())

for name in {modules!r}:
    __import__(name)
print("OK")
"""


def _import_without_tkinter(modules: tuple[str, ...]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SCRIPT.format(modules=modules)],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=120)


@unittest.skipUnless(HAS_WEB, _SKIP)
class WebIsIndependentTests(unittest.TestCase):
    def test_Web版はtkinterを掴まない(self) -> None:
        """画面・API・サービス層のどこも tkinter を掴まないこと。"""
        result = _import_without_tkinter(WEB_MODULES)
        self.assertEqual(result.returncode, 0,
                         f"Web版が tkinter を掴んでいます:\n{result.stderr}")

    def test_起動基盤もtkinterを掴まない(self) -> None:
        """表示環境の無いサーバでも起動できることの保証。"""
        result = _import_without_tkinter(LAUNCH_MODULES)
        self.assertEqual(result.returncode, 0,
                         f"起動基盤が tkinter を掴んでいます:\n{result.stderr}")

    def test_禁止の仕掛けそのものが効いている(self) -> None:
        """止められていなければ、上の2つは何も試していないことになる。"""
        result = _import_without_tkinter(("tkinter",))
        self.assertNotEqual(result.returncode, 0,
                            "tkinter を止められていません")


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
