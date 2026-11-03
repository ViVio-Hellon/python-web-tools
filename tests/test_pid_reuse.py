"""PC の終了・再起動のあと、残ったロック・印の番号を別のプログラムが使っていても取り違えない(VER4.8.127)。

Windows はプロセスの番号(PID)を使い回す。ツールが動いたまま PC を終了・再起動すると、ロック
(`runtime/<mode>.lock`)・デスクトップ版の印(`runtime/desktop.json`)が残り、再起動のあと
**まったく別のプログラムが同じ番号を持つ**ことがある。

    前: stop.bat は番号が生きているので「このアプリではないようなので止めません」で 1 を返し、
        ロックも消さなかった(何もしていない)。起動中のまま残ったロックは、起動のたびに 20 秒待たされた
    今: ロック・印に**プロセスの起動時刻**も書き、番号と起動時刻の両方が合うときだけ本人とみなす。
        前の版が書いた(起動時刻の無い)ものは「書いた時刻より後に起動したプロセスは本人ではない」。
        stop.bat は止めずに片付けて 0(「片付けました。Start.vbs で起動できます」)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import launch_guard  # noqa: E402
import process_manager  # noqa: E402

ME = os.getpid()


def _signals(kill) -> list:
    """生きているかの確認(シグナル0)を除いた、止める合図。"""
    return [c for c in kill.call_args_list if c.args[1] != 0]
LONG_AGO = 1000.0            # 1970年: この試験のプロセスはそれより後に起動している = 書いた本人ではない


class _LocalDir(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {"PACKAGING_TOOL_LOCAL_DIR": tempfile.mkdtemp(prefix="reuse_")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def put_lock(self, mode: str = "field", *, port: int = 1, proc_started: float = 0.0,
                 started_at: float = LONG_AGO) -> launch_guard.LockInfo:
        info = launch_guard.LockInfo(app_id="x", mode=mode, pid=ME, port=port, url="", started_at=started_at,
                                     proc_started=proc_started)
        launch_guard.write_lock(info)
        return info


class PlatformTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform in ("win32", "linux"), "Windows と Linux で確かめる")
    def test_WindowsとLinuxでは起動時刻が取れる(self) -> None:
        """取れないと下の試験が「取れない環境」で飛ばされ、Windows で確かめたことにならない。"""
        self.assertIsNotNone(launch_guard.process_started_at(ME))


@unittest.skipIf(launch_guard.process_started_at(ME) is None, "起動時刻を取れない環境")
class StartTimeTests(_LocalDir):
    def test_自分の起動時刻が取れる(self) -> None:
        started = launch_guard.process_started_at(ME)
        self.assertLess(started, time.time() + 1)
        self.assertGreater(started, time.time() - 24 * 3600 * 30)

    def test_番号と起動時刻が合えば本人(self) -> None:
        self.assertTrue(launch_guard.is_same_process(ME, launch_guard.process_started_at(ME)))

    def test_番号が同じでも起動時刻が違えば本人ではない(self) -> None:
        self.assertFalse(launch_guard.is_same_process(ME, launch_guard.process_started_at(ME) - 3600))

    def test_前の版のファイル_書いた時刻より後に起動したプロセスは本人ではない(self) -> None:
        self.assertFalse(launch_guard.is_same_process(ME, 0.0, LONG_AGO))
        self.assertTrue(launch_guard.is_same_process(ME, 0.0, time.time()))   # 起動してから書いた = 本人でありうる

    def test_居なければ本人ではない(self) -> None:
        with mock.patch.object(launch_guard, "is_process_alive", return_value=False):
            self.assertFalse(launch_guard.is_same_process(ME, 1.0))

    def test_ロックに起動時刻を書く(self) -> None:
        info = launch_guard.build_lock_info("field", 1)
        self.assertAlmostEqual(info.proc_started, launch_guard.process_started_at(ME), delta=1)
        self.assertTrue(launch_guard.lock_owner_alive(info))

    def test_起動時刻が取れなければ前のまま番号だけで見る(self) -> None:
        info = self.put_lock()
        with mock.patch.object(launch_guard, "process_started_at", return_value=None):
            self.assertTrue(launch_guard.lock_owner_alive(info))


@unittest.skipIf(launch_guard.process_started_at(ME) is None, "起動時刻を取れない環境")
class StartupTests(_LocalDir):
    def test_起動中のまま残ったロックでも_番号が使い回されていれば待たずに起動する(self) -> None:
        self.put_lock(port=launch_guard.STARTING_PORT)
        begin = time.monotonic()
        guard = launch_guard.check_existing("field")
        self.assertTrue(guard.should_start)
        self.assertLess(time.monotonic() - begin, 5, "前は 20 秒待っていた")
        self.assertIsNone(launch_guard.read_lock("field"), "残ったロックを片付ける")

    def test_本人が動いていれば片付けない(self) -> None:
        self.put_lock(port=launch_guard.STARTING_PORT, proc_started=launch_guard.process_started_at(ME),
                      started_at=time.time())
        with mock.patch.object(launch_guard, "STARTING_WAIT_SEC", 0.5):
            guard = launch_guard.check_existing("field")
        # 立ち上がらないまま上限を過ぎた扱い(前のまま)。番号の使い回しとは言わない
        self.assertEqual(guard.reason, "起動中のまま応答がないので引き継ぎます")


@unittest.skipIf(launch_guard.process_started_at(ME) is None, "起動時刻を取れない環境")
class StopBatTests(_LocalDir):
    def test_番号が別のプログラムに使い回されていれば_止めずに片付けて0(self) -> None:
        self.put_lock()
        with mock.patch.object(process_manager, "_request_shutdown") as ask, \
                mock.patch.object(process_manager.os, "kill") as kill:
            got = process_manager.stop("field")
        self.assertTrue(got.stopped)
        self.assertEqual(got.method, "stale-lock")
        self.assertIn("Start.vbs で起動できます", got.message)
        ask.assert_not_called()
        self.assertEqual(_signals(kill), [])
        self.assertIsNone(launch_guard.read_lock("field"))
        self.assertEqual(process_manager.report([got])[0], 0)

    def test_起動時刻が取れなくても_コマンドラインが別のプログラムなら片付ける(self) -> None:
        info = self.put_lock()
        with mock.patch.object(launch_guard, "process_started_at", return_value=None), \
                mock.patch.object(launch_guard, "process_command_line", return_value="C:/Windows/notepad.exe"), \
                mock.patch.object(process_manager.os, "kill") as kill:
            got = process_manager._stop_by_pid("field", info, process_manager.StopResult("field"), force=True)
        self.assertTrue(got.stopped)
        self.assertEqual(_signals(kill), [])
        self.assertIsNone(launch_guard.read_lock("field"))

    def test_何も確かめられなければ前のまま止めない_ロックも残す(self) -> None:
        info = self.put_lock()
        with mock.patch.object(launch_guard, "process_started_at", return_value=None), \
                mock.patch.object(launch_guard, "process_command_line", return_value=""), \
                mock.patch.object(process_manager.os, "kill") as kill:
            got = process_manager._stop_by_pid("field", info, process_manager.StopResult("field"), force=True)
        self.assertFalse(got.stopped)
        self.assertEqual(_signals(kill), [])
        self.assertIsNotNone(launch_guard.read_lock("field"))


@unittest.skipIf(launch_guard.process_started_at(ME) is None, "起動時刻を取れない環境")
class DesktopMarkerTests(_LocalDir):
    def put_marker(self, **extra) -> dict:
        raw = {"app_id": "x", "mode": "field", "pid": ME, "exe_pid": ME, "exe_pid_from": "env",
               "started_at": LONG_AGO, **extra}
        path = launch_guard.desktop_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw), encoding="utf-8")
        return raw

    def test_印に起動時刻を書く(self) -> None:
        launch_guard.write_desktop_marker("field")
        raw = launch_guard.read_desktop_marker()
        self.assertAlmostEqual(raw["proc_started"], launch_guard.process_started_at(ME), delta=1)
        self.assertTrue(launch_guard.desktop_python_alive(raw))

    def test_番号が使い回されていれば_動いているとは言わない(self) -> None:
        """コマンドラインが取れない PC でも(前は「生きていることだけで信じる」だった)。"""
        self.put_marker()
        with mock.patch.object(launch_guard, "process_command_line", return_value=""):
            self.assertIsNone(launch_guard.desktop_running())

    def test_番号が使い回されていれば_窓に閉じても強制終了も送らず_印を片付ける(self) -> None:
        raw = self.put_marker()
        for force in (False, True):
            with self.subTest(force=force), \
                    mock.patch.object(process_manager, "_ask_windows_to_close") as ask, \
                    mock.patch.object(process_manager, "_kill_tree") as kill:
                got = process_manager.stop_desktop(raw, force=force)
                self.assertTrue(got.stopped)
                ask.assert_not_called()
                kill.assert_not_called()
        self.assertIsNone(launch_guard.read_desktop_marker())

    def test_stop_batの探し方でも止める相手にせず_印を片付ける(self) -> None:
        self.put_marker()
        with mock.patch.object(process_manager, "_looks_like_our_exe", return_value=True), \
                mock.patch.object(process_manager, "_our_exe_pids", return_value=set()):
            self.assertEqual(process_manager.desktop_targets(None), [])
        self.assertIsNone(launch_guard.read_desktop_marker())

    def test_あとから書かれた別の印は消さない(self) -> None:
        old = self.put_marker()
        self.put_marker(started_at=LONG_AGO + 1)
        launch_guard.remove_desktop_marker(expect=old)
        self.assertIsNotNone(launch_guard.read_desktop_marker())


if __name__ == "__main__":
    unittest.main()
