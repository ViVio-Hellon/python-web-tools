"""`stop.bat`(`process_manager.py`)の答え方と、デスクトップ版が動いている印

業務ツール統合ランチャーは `stop.bat` を呼び、戻り値と出力を読む(docs/ランチャー連携.md)。

    0  止めた / もともと動いていない
    1  止められなかった
    3  止めなかった(処理中: 保存していない図・実行中の処理。`--force` で止める)

出力の**1行目は理由**(ランチャー 1.8.9: 管理者の一覧と利用者への知らせに出す)。
デスクトップ版(exe の窓)も止める(窓に「閉じて」と頼む。× と同じ。`--force` なら強制)。VER4.8.125

以前は 0 以外を全部 1 にしていたうえ、**デスクトップ版が動いていても「起動していません」で
0 を返していた**(ブラウザ版のロックだけを見ていた)。VER4.8.124 までは処理中が 2、デスクトップ版は止めなかった。
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
    def run_main(self, results: dict, *, desktop=None, desktop_kind="stopped",
                 argv=("--all",)) -> tuple[int, str]:
        def fake_stop(mode, *, force=False):
            result = process_manager.StopResult(mode)
            kind = results.get(mode, "stopped")
            result.stopped = kind == "stopped"
            if kind == "busy":
                result.busy_jobs = ["保存していない変更(配置編集)"]
            result.message = kind
            return result

        def fake_desktop(info, *, force=False):
            self.desktop_force = force
            result = process_manager.StopResult("デスクトップ版")
            result.stopped = desktop_kind == "stopped" or force
            if not result.stopped:
                result.busy_jobs = ["デスクトップ版の窓で、保存していない図・実行中の処理を確かめています"]
            result.message = desktop_kind
            return result

        out = io.StringIO()
        with mock.patch.object(process_manager, "stop", side_effect=fake_stop), \
                mock.patch.object(process_manager, "stop_desktop", side_effect=fake_desktop), \
                mock.patch.object(launch_guard, "desktop_running", return_value=desktop), \
                redirect_stdout(out):
            code = process_manager.main(list(argv))
        return code, out.getvalue()

    def test_止めた(self) -> None:
        self.assertEqual(self.run_main({})[0], 0)

    def test_処理中なら3_1行目に理由(self) -> None:
        """ランチャー 1.8.9: 処理中で止めないときは 0 以外(おすすめは 3)、1行目に理由。"""
        code, out = self.run_main({"field": "busy"})
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[0], "処理中のため止めません: 保存していない変更(配置編集)")

    def test_止められなければ1_1行目に理由(self) -> None:
        code, out = self.run_main({"material": "failed"})
        self.assertEqual(code, 1)
        self.assertTrue(out.splitlines()[0].startswith("止められませんでした: "), out)

    def test_両方なら止められなかったほうを返す(self) -> None:
        self.assertEqual(self.run_main({"field": "failed", "material": "busy"})[0], 1)
        self.assertEqual(self.run_main({"field": "busy", "material": "failed"})[0], 1)

    def test_デスクトップ版も止める_窓で訊いていれば処理中(self) -> None:
        """前は止めずに 1 を返していた(ランチャーが更新のために止められなかった)。"""
        self.assertEqual(self.run_main({}, desktop={"pid": 1, "mode": "field"})[0], 0)
        code, out = self.run_main({}, desktop={"pid": 1, "mode": "field"}, desktop_kind="asking")
        self.assertEqual(code, 3)
        self.assertIn("デスクトップ版の窓", out.splitlines()[0])

    def test_forceならデスクトップ版も強制で止める(self) -> None:
        code, _ = self.run_main({}, desktop={"pid": 1, "mode": "field"}, desktop_kind="asking",
                                argv=("--all", "--force"))
        self.assertEqual(code, 0)
        self.assertTrue(self.desktop_force)


class StopDesktopTests(_LocalDir):
    """デスクトップ版の窓を止める(VER4.8.125)。"""

    INFO = {"pid": 100, "exe_pid": 99, "exe_pid_from": "env", "mode": "field"}

    def stop(self, info=None, *, force=False, alive=True, sent=1, gone=True):
        with mock.patch.object(launch_guard, "is_process_alive",
                               side_effect=alive if callable(alive) else (lambda pid: alive)), \
                mock.patch.object(launch_guard, "process_started_at", return_value=None), \
                mock.patch.object(process_manager, "_ask_windows_to_close", return_value=sent) as ask, \
                mock.patch.object(process_manager, "_kill_tree") as kill, \
                mock.patch.object(process_manager, "_wait_pid_gone", return_value=gone):
            got = process_manager.stop_desktop(info or self.INFO, force=force)
        return got, ask, kill

    def test_見えている窓に閉じてと頼み_閉じれば止めた(self) -> None:
        got, ask, kill = self.stop()
        self.assertTrue(got.stopped)
        ask.assert_called_once_with({99})                              # 窓(exe)に。Python ではない
        kill.assert_not_called()

    def test_窓が残れば処理中(self) -> None:
        got, _ask, _kill = self.stop(gone=False)
        self.assertFalse(got.stopped)
        self.assertTrue(got.busy_jobs)

    def test_forceなら窓と子をまとめて止める(self) -> None:
        got, ask, kill = self.stop(force=True)
        self.assertTrue(got.stopped)
        ask.assert_not_called()
        kill.assert_called_once_with(99)

    def test_窓が見つからなければ止められなかったと言う(self) -> None:
        got, _ask, _kill = self.stop(sent=0)
        self.assertFalse(got.stopped)
        self.assertFalse(got.busy_jobs)                                # 処理中ではない(1 で返る)

    def test_親の番号しか無く_このツールの窓と確かめられなければ止めない(self) -> None:
        """前の版の外枠では Python の親(py.exe のこともある)を書いていた。違うプロセスを止めない。"""
        info = {**self.INFO, "exe_pid_from": "parent"}
        with mock.patch.object(process_manager, "_looks_like_our_exe", return_value=False):
            got, ask, kill = self.stop(info)
        self.assertFalse(got.stopped)
        ask.assert_not_called()
        kill.assert_not_called()

    def test_窓が居なくて_Pythonだけ残っていれば止める(self) -> None:
        # 仮の番号 100 のコマンドラインは固定する(この PC で番号 100 が別のプロセスのことがある)
        with mock.patch.object(launch_guard, "process_command_line", return_value="python C:/tool/bridge.py"):
            got, ask, kill = self.stop(alive=lambda pid: pid == 100)
        self.assertTrue(got.stopped)
        ask.assert_not_called()
        kill.assert_called_once_with(100)

    def test_外枠が渡した窓の番号を印に書く(self) -> None:
        with mock.patch.dict(os.environ, {"PACKAGING_TOOL_EXE_PID": "4242"}):
            launch_guard.write_desktop_marker("field")
        raw = launch_guard.read_desktop_marker()
        self.assertEqual((raw["exe_pid"], raw["exe_pid_from"]), (4242, "env"))
        os.environ.pop("PACKAGING_TOOL_EXE_PID", None)
        launch_guard.write_desktop_marker("field")
        self.assertEqual(launch_guard.read_desktop_marker()["exe_pid_from"], "parent")


class DesktopTargetsTests(_LocalDir):
    def test_Pythonが落ちて窓だけ残っていても_このツールのexeなら止める相手にする(self) -> None:
        path = launch_guard.desktop_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1, "exe_pid": 99, "exe_pid_from": "env"}), encoding="utf-8")
        with mock.patch.object(launch_guard, "is_process_alive", side_effect=lambda pid: pid == 99), \
                mock.patch.object(launch_guard, "process_started_at", return_value=None), \
                mock.patch.object(process_manager, "_looks_like_our_exe", return_value=True), \
                mock.patch.object(process_manager, "_our_exe_pids", return_value=set()):
            found = process_manager.desktop_targets(None)
        self.assertEqual([t["exe_pid"] for t in found], [99])

    def test_印が無くても_このツールのフォルダのexeが動いていれば止める相手にする(self) -> None:
        with mock.patch.object(process_manager, "_our_exe_pids", return_value={77}):
            found = process_manager.desktop_targets(None)
        self.assertEqual([(t["exe_pid"], t["exe_pid_from"]) for t in found], [(77, "path")])

    def test_同じ窓を2回数えない(self) -> None:
        desktop = {"pid": 1, "exe_pid": 77, "exe_pid_from": "env"}
        with mock.patch.object(process_manager, "_our_exe_pids", return_value={77}):
            self.assertEqual(len(process_manager.desktop_targets(desktop)), 1)


class ExitCodeStatusTests(_LocalDir):
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
