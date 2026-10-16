"""本体を組み立てられずに待機画面のまま残った Python を、stop.bat で止められること

業務ツール統合の all-tools で起きた不具合と同じ形が、このツールにもあった:

    1. 起動の途中で本体を組み立てられない(フォルダの一部が欠けた・設定が壊れた・部品が無い)
       → 待機画面に理由を出したまま Python が残る
    2. 待機画面が止める合図(`POST /api/shutdown`)に 503 を返していた
       → stop.bat は正常終了を頼めず、PID で落とす道だけになる
    3. PID で落とす前の照合(コマンドライン)は Windows で `wmic` に頼っていた。
       新しい Windows には `wmic` が無く、照合できずに「このアプリではないようなので止めません」
       ── `--force` でも止まらない
    4. 正常終了は「待ち受けが閉じた」だけで「終わった」と言い、強制終了は消えたかを見ずに
       「強制終了しました」と言っていた

【ここで守りたいこと】
1. 待機画面はトークンが合えば止める合図に答える(合わなければ断る)
2. 本体を組み立てられなかった起動を、stop.bat(`process_manager`)が正常終了で止められる
3. Windows で `wmic` が無くても、PowerShell でコマンドラインを引ける
4. プロセスが消えたのを確かめてから「終了しました」と言う
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import boot_server  # noqa: E402
import launch_guard  # noqa: E402
import process_manager  # noqa: E402


def call(app, method: str, path: str, *, token: str = "", query: str = "") -> tuple[str, dict]:
    got = {}

    def start_response(status, headers):
        got["status"] = status

    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
               "wsgi.input": io.BytesIO(b"{}")}
    if token:
        environ["HTTP_X_TOOL_TOKEN"] = token
    body = b"".join(app(environ, start_response))
    return got["status"], json.loads(body.decode("utf-8"))


class BootShutdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = boot_server.BootApp("field", 8713, "tok")
        self.stopped = []
        self.app.on_stop = lambda: self.stopped.append(True)
        patcher = mock.patch.object(boot_server, "SHUTDOWN_DELAY_SEC", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait_stopped(self) -> bool:
        deadline = time.monotonic() + 2
        while not self.stopped and time.monotonic() < deadline:
            time.sleep(0.01)
        return bool(self.stopped)

    def test_トークンが合えば止める(self) -> None:
        self.app.mark_error("アプリを組み立てられませんでした: 部品がありません")
        status, body = call(self.app, "POST", "/api/shutdown", token="tok")
        self.assertEqual((status, body["stopped"]), ("200 OK", True))
        self.assertTrue(self.wait_stopped())

    def test_画面のトークン_クエリでも止められる(self) -> None:
        status, _ = call(self.app, "POST", "/api/shutdown", query="t=tok")
        self.assertEqual(status, "200 OK")
        self.assertTrue(self.wait_stopped())

    def test_トークンが無い_違うなら断る(self) -> None:
        for token in ("", "nope"):
            with self.subTest(token=token):
                status, body = call(self.app, "POST", "/api/shutdown", token=token)
                self.assertEqual((status, body["stopped"]), ("403 Forbidden", False))
        time.sleep(0.05)
        self.assertEqual(self.stopped, [])

    def test_デスクトップ版はトークンを見ない(self) -> None:
        self.app.trusted = True
        status, _ = call(self.app, "POST", "/api/shutdown")
        self.assertEqual(status, "200 OK")
        self.assertTrue(self.wait_stopped())

    def test_GETでは止めない(self) -> None:
        status, _ = call(self.app, "GET", "/api/shutdown", token="tok")
        self.assertTrue(status.startswith("503"))
        self.assertEqual(self.stopped, [])

    def test_止め方を差し込むのはサーバ(self) -> None:
        import server
        srv = server.AppServer("field", 0)
        self.assertEqual(srv.boot.on_stop, srv.stop)


class StopAfterBuildFailureTests(unittest.TestCase):
    """本物の起動(別プロセス)で、本体の組み立てに失敗させてから stop.bat と同じ手順で止める。"""

    def setUp(self) -> None:
        self.local = tempfile.mkdtemp(prefix="bootfail_")
        patcher = mock.patch.dict(os.environ, {"PACKAGING_TOOL_LOCAL_DIR": self.local})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_待機画面のまま残ったPythonを止められる(self) -> None:
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "import server, start_app\n"
            "def boom(self):\n"
            "    raise ImportError('部品がありません(試験)')\n"
            "server.AppServer.build = boom\n"
            "sys.exit(start_app.start('field', open_browser=False))\n") % str(_ROOT)
        env = {**os.environ, "PACKAGING_TOOL_LOCAL_DIR": self.local, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen([sys.executable, "-c", script], cwd=str(_ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        # 終わったらすぐ片付ける(親が待たないと、Linux では終わった子が「居る」に見える。
        # stop.bat は親ではないので、本番では起きない)
        import threading
        reaped = threading.Thread(target=proc.wait, daemon=True)
        reaped.start()

        # 待機画面が理由を出すまで待つ
        deadline = time.monotonic() + 60
        health = None
        while time.monotonic() < deadline:
            health = process_manager.status("field")
            if health and health.get("startup_error"):
                break
            if not reaped.is_alive():
                self.fail("起動側が先に終わりました: " + proc.stdout.read().decode("utf-8", "replace"))
            time.sleep(0.2)
        self.assertIn("組み立てられませんでした", (health or {}).get("startup_error", ""))
        self.assertEqual(health["_lock"]["pid"], proc.pid)

        # PID で落とす道を塞いでも(Windows で照合できない場合)、正常終了で止まる
        with mock.patch.object(process_manager, "_stop_by_pid",
                               side_effect=AssertionError("PID で落とす道に来た")):
            result = process_manager.stop("field")
        self.assertTrue(result.stopped, result.message)
        self.assertEqual(result.method, "graceful")
        reaped.join(timeout=15)
        self.assertEqual(proc.returncode, 1)                   # 組み立てに失敗した起動の戻り値
        self.assertIsNone(launch_guard.read_lock("field"))


class VerifyGoneTests(unittest.TestCase):
    """消えたのを確かめてから「終了しました」と言う。**ここでは本物のプロセスを落とさない。**"""

    def info(self):
        return launch_guard.LockInfo(app_id=launch_guard.app_config.app_id(), mode="field",
                                     pid=424242, port=1, url="", started_at=0.0,
                                     app_root=str(_ROOT), token="t")

    def test_待ち受けが閉じてもプロセスが残っていれば正常終了と言わない(self) -> None:
        with mock.patch.object(launch_guard, "read_lock", return_value=self.info()), \
             mock.patch.object(launch_guard, "probe_health",
                               return_value={"app_id": launch_guard.app_config.app_id(),
                                             "mode": "field"}), \
             mock.patch.object(process_manager, "_request_shutdown", return_value={"ok": True}), \
             mock.patch.object(process_manager, "_wait_gone", return_value=True), \
             mock.patch.object(process_manager, "_wait_pid_gone", return_value=False), \
             mock.patch.object(process_manager, "_stop_by_pid",
                               side_effect=lambda mode, info, result, force: result) as by_pid:
            result = process_manager.stop("field")
        self.assertFalse(result.stopped)
        by_pid.assert_called_once()

    def test_強制終了しても残っていれば止めたと言わない(self) -> None:
        result = process_manager.StopResult("field")
        with mock.patch.object(launch_guard, "is_process_alive", return_value=True), \
             mock.patch.object(process_manager, "_looks_like_our_process", return_value=True), \
             mock.patch.object(process_manager.os, "kill") as kill, \
             mock.patch.object(process_manager, "_wait_pid_gone", return_value=False), \
             mock.patch.object(launch_guard, "remove_lock") as remove:
            got = process_manager._stop_by_pid("field", self.info(), result, force=True)
        self.assertEqual(kill.call_count, 2)                    # 終了要求 → 強制終了
        self.assertFalse(got.stopped)
        self.assertIn("まだ残っています", got.message)
        remove.assert_not_called()


class CommandLineTests(unittest.TestCase):
    def test_wmicが無ければPowerShellで引く(self) -> None:
        with mock.patch.object(launch_guard.os, "name", "nt"), \
             mock.patch.object(launch_guard, "_command_line_wmic", return_value="") as wmic, \
             mock.patch.object(launch_guard, "_command_line_powershell",
                               return_value="pythonw C:/tool/start_app.py") as ps:
            self.assertEqual(launch_guard.process_command_line(1234), "pythonw C:/tool/start_app.py")
        wmic.assert_called_once_with(1234)
        ps.assert_called_once_with(1234)

    def test_wmicが実行できなくても落ちない(self) -> None:
        with mock.patch.object(launch_guard.subprocess, "run", side_effect=FileNotFoundError("wmic")):
            self.assertEqual(launch_guard._command_line_wmic(1234), "")

    @unittest.skipUnless(os.name == "nt", "Windows だけ")
    def test_PowerShellで自分のコマンドラインを引ける(self) -> None:
        line = launch_guard._command_line_powershell(os.getpid())
        self.assertIn("python", line.lower())
        self.assertIn("python", launch_guard.process_command_line(os.getpid()).lower())


if __name__ == "__main__":
    unittest.main()
