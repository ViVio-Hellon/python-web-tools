"""`stop.bat`(`process_manager.py`)の答え方と、デスクトップ版が動いている印

業務ツール統合ランチャーは `stop.bat` を呼び、戻り値と出力を読む(docs/ランチャー連携.md)。

    0  止めた / もともと動いていない
    1  止められなかった、またはデスクトップ版(exe の窓)が動いている
    2  止めなかった(保存していない図・実行中の処理。`--force` で止める)

以前は 0 以外を全部 1 にしていたうえ、**デスクトップ版が動いていても「起動していません」で
0 を返していた**(ブラウザ版のロックだけを見ていた)。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import launch_guard  # noqa: E402
import process_manager  # noqa: E402


class _LocalDir(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"PACKAGING_TOOL_LOCAL_DIR": tempfile.mkdtemp(prefix="pm_")})
        patcher.start()
        self.addCleanup(patcher.stop)


class DesktopMarkerTests(_LocalDir):
    def bridge(self, pid: int) -> str:
        return f"python C:/tool/bridge.py (pid {pid})"

    def test_印が無ければ動いていない(self) -> None:
        self.assertIsNone(launch_guard.desktop_running())

    def test_bridgeが動いていれば動いている(self) -> None:
        launch_guard.write_desktop_marker("field")
        with mock.patch.object(launch_guard, "process_command_line", side_effect=self.bridge):
            found = launch_guard.desktop_running()
        self.assertEqual((found["pid"], found["mode"]), (os.getpid(), "field"))

    def test_書いたPythonが居なければ動いていない(self) -> None:
        """落ちたあとに印だけ残っていても、動いているとは言わない。"""
        launch_guard.write_desktop_marker("field")
        with mock.patch.object(launch_guard, "is_process_alive", return_value=False):
            self.assertIsNone(launch_guard.desktop_running())

    def test_PIDが使い回されていれば動いていない(self) -> None:
        launch_guard.write_desktop_marker("field")
        with mock.patch.object(launch_guard, "process_command_line", return_value="notepad.exe"):
            self.assertIsNone(launch_guard.desktop_running())

    def test_自分の印だけを消す(self) -> None:
        path = launch_guard.desktop_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid() + 1}), encoding="utf-8")
        launch_guard.remove_desktop_marker()
        self.assertTrue(path.exists(), "あとから起動した別の Python の印は消さない")
        launch_guard.write_desktop_marker("field")
        launch_guard.remove_desktop_marker()
        self.assertFalse(path.exists())

    def test_ランチャーがトークン探しに読む名前にしない(self) -> None:
        """ランチャーは `runtime/*.lock` を JSON として読み、停止用のトークンを探す。"""
        self.assertFalse(launch_guard.DESKTOP_MARKER.endswith(".lock"))


class ExitCodeTests(_LocalDir):
    def run_main(self, results: dict, *, desktop=None) -> tuple[int, str]:
        def fake_stop(mode, *, force=False):
            result = process_manager.StopResult(mode)
            kind = results.get(mode, "stopped")
            result.stopped = kind == "stopped"
            if kind == "busy":
                result.busy_jobs = ["保存していない変更(配置編集)"]
            result.message = kind
            return result

        out = io.StringIO()
        with mock.patch.object(process_manager, "stop", side_effect=fake_stop), \
                mock.patch.object(launch_guard, "desktop_running", return_value=desktop), \
                redirect_stdout(out):
            code = process_manager.main(["--all"])
        return code, out.getvalue()

    def test_止めた(self) -> None:
        self.assertEqual(self.run_main({})[0], 0)

    def test_保存していない図があれば2(self) -> None:
        self.assertEqual(self.run_main({"field": "busy"})[0], 2)

    def test_止められなければ1(self) -> None:
        self.assertEqual(self.run_main({"material": "failed"})[0], 1)

    def test_両方なら止められなかったほうを返す(self) -> None:
        self.assertEqual(self.run_main({"field": "failed", "material": "busy"})[0], 1)
        self.assertEqual(self.run_main({"field": "busy", "material": "failed"})[0], 1)

    def test_デスクトップ版が動いていれば1で知らせる(self) -> None:
        """**「起動していません」で 0 を返さない。** 止めもしない(窓の × で閉じる)。"""
        code, out = self.run_main({}, desktop={"pid": 1, "mode": "field"})
        self.assertEqual(code, 1)
        self.assertIn("デスクトップ版", out)
        self.assertIn("窓の ×", out)

    def test_止めなかった理由を先に出す(self) -> None:
        """ランチャー(1.7.1〜)は 0 以外のとき、出力のはじめの2行を理由として見せる
        (`process_manager._first_lines`)。「起動していません」を先に出さない。"""
        _, out = self.run_main({"material": "busy"})
        self.assertTrue(out.splitlines()[0].startswith("[--] material"), out)
        _, out = self.run_main({}, desktop={"pid": 1, "mode": "field"})
        self.assertIn("デスクトップ版", out.splitlines()[0])

    def test_状態にデスクトップ版を出す(self) -> None:
        out = io.StringIO()
        with mock.patch.object(launch_guard, "desktop_running",
                               return_value={"pid": 7, "mode": "material"}), \
                mock.patch.object(process_manager, "status", return_value=None), \
                redirect_stdout(out):
            self.assertEqual(process_manager.main(["--all", "--status"]), 0)
        self.assertIn("[稼働] デスクトップ版", out.getvalue())


class BridgeWritesMarkerTests(_LocalDir):
    def test_デスクトップ版の起動で印を置き_終われば消す(self) -> None:
        import start_app

        seen = {}

        class FakeServer:
            def __init__(self) -> None:
                self.boot = mock.Mock()

            def build(self) -> None:
                seen["running"] = launch_guard.desktop_marker_path().exists()

        with mock.patch("server.run_in_background"), \
                mock.patch.object(start_app, "_initialize"), \
                mock.patch.object(start_app, "_hold_until_stopped"), \
                mock.patch.object(start_app, "resolve_mode", return_value="field"), \
                mock.patch.object(start_app, "log_environment"):
            code = start_app.start_bridge("field", token="t",
                                          server_factory=lambda mode, token: FakeServer())
        self.assertEqual(code, 0)
        self.assertTrue(seen["running"], "動いているあいだ印がある")
        self.assertFalse(launch_guard.desktop_marker_path().exists(), "終われば消す")


if __name__ == "__main__":
    unittest.main()
