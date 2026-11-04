#!/usr/bin/env python3
"""対象プロセスの確認と安全な停止 (基盤仕様書 2.8)

**このアプリだけを止める。** 同じPCで動く別のPythonアプリを巻き添えに
しないことが、このモジュールの唯一の目的。

順序は仕様書のとおり:

    1. まずアプリ自身へ正常終了を要求する (`POST /api/shutdown`)
    2. 応答しないときだけ、記録したPIDを使う
    3. 落とす前に、そのPIDが**本当にこのアプリか**を確かめる
    4. `python.exe` をプロセス名だけで一括終了することはしない

実行中の長時間処理があるときは、既定では止めずに知らせる。
中断してよいかは利用者が決める(`--force` で中断する)。

使い方:

    python process_manager.py                  現場モードを止める
    python process_manager.py --mode material  資材モードを止める
    python process_manager.py --all            両方止める
    python process_manager.py --force          実行中の処理を中断してでも止める
    python process_manager.py --status         状態を見るだけ

戻り値(`stop.bat` もそのまま返す。業務ツール統合ランチャーが読む):

    0  止めた / もともと動いていない
    1  止められなかった(応答しない・このアプリか確かめられない)
    3  止めなかった(処理中: 保存していない図・実行中の処理がある。`--force` で止める)
       ── ランチャー 1.8.9 のおすすめに合わせた(VER4.8.125。前は 2)

**標準出力の1行目に理由を書く**(ランチャーが管理者の一覧と利用者への知らせに出す)。
例「処理中のため止めません: Access の変換(表を持ってくる)」。

**デスクトップ版(exe の窓)も止める**(VER4.8.125。前は止めなかった)。窓に「閉じて」と頼む
(窓の × と同じ。保存していない図・実行中の処理があれば窓で訊く)。そのあいだ窓が残っていれば
「処理中」として 3 を返す。`--force` なら窓とその子(Python)をまとめて止める。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
# **ツールのフォルダの中に __pycache__ を書かない**(VER4.8.125。下で置き場所を外へ向けるまで書かない)
sys.dont_write_bytecode = True
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))
from packaging_tool import app_config as _app_config  # noqa: E402
_app_config.use_local_pycache()

from packaging_tool import app_config, modes  # noqa: E402
from packaging_tool.logging_utils import get_logger  # noqa: E402

log = get_logger("process_manager")

# 正常終了を要求したあと、実際に落ちるのを待つ上限(秒)
GRACEFUL_WAIT_SEC = 8.0
# 戻り値(`main`)。ランチャー 1.8.9 のおすすめ: 処理中で止めないときは 3
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BUSY = 3
# PIDで落としたあと、消えるのを待つ上限(秒)
FORCE_WAIT_SEC = 5.0


class StopResult:
    """止められたか、どうやって止めたか。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.stopped = False
        self.method = ""
        self.message = ""
        self.busy_jobs: list[str] = []

    def __str__(self) -> str:
        mark = "済" if self.stopped else "--"
        return f"[{mark}] {self.mode}: {self.message}"


# ------------------------------------------------------------------
# 状態
# ------------------------------------------------------------------
def status(mode: str) -> Optional[dict]:
    """そのmodeが動いているか。動いていれば `/api/health` の中身。"""
    import launch_guard

    info = launch_guard.read_lock(mode)
    if info is None:
        return None
    health = launch_guard.probe_health(info.port)
    if not launch_guard.is_our_app(health, mode, pid=info.pid):
        return None
    health["_lock"] = {"pid": info.pid, "port": info.port,
                       "started": info.started_text, "app_root": info.app_root}
    return health


# ------------------------------------------------------------------
# 停止
# ------------------------------------------------------------------
def stop(mode: str, *, force: bool = False) -> StopResult:
    import launch_guard

    result = StopResult(mode)
    info = launch_guard.read_lock(mode)
    if info is None:
        result.stopped = True
        result.method = "none"
        result.message = "起動していません"
        return result

    health = launch_guard.probe_health(info.port)
    if not launch_guard.is_our_app(health, mode, pid=info.pid):
        # 応答しない、または別のアプリ。ロックだけ残っている状態。
        # **番号と起動時刻の両方で**書いた本人か見る(VER4.8.127。PC の終了・再起動のあと、別のプログラムが
        # 同じ番号を持つことがある。前は番号だけで見て「このアプリではないので止めません」で終わり、ロックも残った)
        if launch_guard.lock_owner_alive(info):
            return _stop_by_pid(mode, info, result, force=force)
        return _clean_stale_lock(mode, info, result)

    # --- 1. 正常終了を要求する ---
    asked = _request_shutdown(info.port, info.token, force=force)
    if asked.get("busy"):
        result.busy_jobs = asked.get("running", [])
        # 実行中の処理だけでなく、保存していない図も入る(`/api/shutdown` の `running`)
        result.message = (f"止めませんでした: {'、'.join(result.busy_jobs)}\n"
                          f"    保存せず・中断して止めるには --force を付けてください")
        return result

    # **待ち受けが閉じただけでは「終わった」と言わない。** プロセスが消えるまで見る
    # ── 残った Python はフォルダを掴んだままで、消せなくなる(all-tools で起きた形)
    if (asked.get("ok") and _wait_gone(info.port, GRACEFUL_WAIT_SEC)
            and _wait_pid_gone(info.pid, GRACEFUL_WAIT_SEC)):
        launch_guard.remove_lock(mode, expect=info)
        result.stopped = True
        result.method = "graceful"
        result.message = "正常に終了しました"
        return result

    # --- 2. 応答しないときだけPIDを使う ---
    return _stop_by_pid(mode, info, result, force=force)


# ------------------------------------------------------------------
# デスクトップ版(exe の窓)
# ------------------------------------------------------------------
# 窓に「閉じて」と頼んでから、消えるのを待つ上限(秒)。保存していない図・実行中の処理があれば
# 窓が訊いて残るので、待ちきれなければ「処理中」とみなす
DESKTOP_WAIT_SEC = 10.0


def stop_desktop(desktop: dict, *, force: bool = False) -> StopResult:
    """デスクトップ版の窓を止める(VER4.8.125)。

    ふだんは窓に「閉じて」と頼む(**見えている窓にだけ WM_CLOSE**。窓の × と同じで、ランチャーの［ツール停止］・
    CI の確かめ `scripts/desktop_smoke.py` と同じ頼み方)。窓は閉じる前に、保存していない図・実行中の処理を訊く ──
    そのあいだ窓は残るので「処理中」。`force` なら窓とその子(Python)をまとめて止める(保存していないものは捨てる)。

    止める相手は**この窓だと確かめられたものだけ**(番号は使い回されるので、別のプロセスを止めない):
      - 外枠が環境変数で渡した窓の番号(`exe_pid_from` = env)・このツールのフォルダの exe として見つけたもの(path)・
        確かめ済みのもの(checked)はそのまま
      - 親の番号しか無いとき(前の版の外枠。py.exe のこともある)は、コマンドラインがこのツールの exe のときだけ
    """
    import launch_guard

    result = StopResult("デスクトップ版")
    py_pid = int(desktop.get("pid") or 0)
    exe_pid = int(desktop.get("exe_pid") or 0)
    if not _exe_alive(desktop):
        if _python_alive(desktop):
            # 窓が居ないのに Python だけ残っている(外枠が落ちた)。Python を止める(bridge.py だと確かめ済み)
            return _stop_lone_python(py_pid, result)
        result.stopped = True
        result.method = "already-gone"
        result.message = "すでに終了していました"
        if desktop.get("started_at"):
            launch_guard.remove_desktop_marker(expect=desktop)
            result.message = f"すでに終了していました(残っていた印を{CLEANED_HINT})"
        return result
    if desktop.get("exe_pid_from") not in ("env", "path", "checked") and not _looks_like_our_exe(exe_pid):
        result.message = (f"窓(pid {exe_pid})がこのツールのものか確かめられないので止めません。"
                          "窓の × で閉じてください")
        return result
    if force:
        _kill_tree(exe_pid)
        if _wait_pid_gone(exe_pid, FORCE_WAIT_SEC):
            _finish_python(desktop)
            result.stopped = True
            result.method = "force"
            result.message = "強制終了しました"
            return result
        result.message = (f"強制終了を頼みましたが、pid {exe_pid} がまだ残っています。"
                          "タスクマネージャーで 梱包資材総合ツール.exe を確かめてください")
        return result
    sent = _ask_windows_to_close({exe_pid})
    if sent == 0:
        result.message = ("窓が見つからないので、閉じてと頼めませんでした。"
                          "窓の × で閉じるか、--force を付けてください")
        return result
    if _wait_pid_gone(exe_pid, DESKTOP_WAIT_SEC):
        _finish_python(desktop)
        result.stopped = True
        result.method = "close"
        result.message = "窓を閉じました"
        return result
    result.busy_jobs = ["デスクトップ版の窓で、保存していない図・実行中の処理を確かめています"]
    result.message = ("窓が閉じませんでした(保存していない図・実行中の処理を窓で訊いています)。\n"
                      "    窓で答えるか、捨てて・中断して止めるには --force を付けてください")
    return result


EXE_NAMES = ("梱包資材総合ツール", "packagingtool", "packaging-tool-desktop")
# このツールのフォルダに置く exe の名前(配るとき `梱包資材総合ツール.exe`、CI が作る名前 `PackagingTool.exe`)
EXE_FILES = ("梱包資材総合ツール.exe", "PackagingTool.exe")


def desktop_targets(desktop: Optional[dict]) -> list[dict]:
    """止めるデスクトップ版の窓(VER4.8.125)。

    1. 中の Python が置いた印(`desktop_running`。Python が生きていて bridge.py を動かしている)
    2. 印はあるが Python が居ない(落ちた): 窓(exe)だけ残っていれば。**このツールの exe だと確かめられたときだけ**
    3. 印が無くても、**このツールのフォルダの exe** が動いていれば(Python が見つからず理由の画面のまま、など)。
       窓が残っていると exe のファイルを掴んだままで、更新の上書きが止まる
    """
    import launch_guard

    found: list[dict] = []
    if desktop is not None:
        found.append(desktop)
    else:
        raw = launch_guard.read_desktop_marker()
        exe_pid = int((raw or {}).get("exe_pid") or 0)
        if (exe_pid > 0 and launch_guard.is_process_alive(exe_pid)
                and launch_guard.desktop_exe_alive(raw) is not False and _looks_like_our_exe(exe_pid)):
            found.append({**raw, "pid": 0, "exe_pid_from": "checked"})
        elif raw is not None:
            # 窓も Python も居ない(PC の終了・再起動のあと、など)。残った印を片付ける(VER4.8.127)
            launch_guard.remove_desktop_marker(expect=raw)
    known = {int(d.get("exe_pid") or 0) for d in found}
    for pid in _our_exe_pids():
        if pid not in known:
            found.append({"pid": 0, "exe_pid": pid, "exe_pid_from": "path"})
    return found


def _our_exe_pids() -> set[int]:
    """**このツールのフォルダにある exe** で動いているプロセスの番号(Windows だけ。取れなければ空)。"""
    if os.name != "nt":
        return set()
    import subprocess
    root = str(APP_ROOT).replace("'", "''")
    names = " -or ".join(f"$_.Name -ieq '{n}'" for n in EXE_FILES)
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
              f"([IO.Path]::GetDirectoryName($_.ExecutablePath) -ieq '{root}') -and ({names}) }} | "
              "ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("このツールの exe を探せませんでした: %s", exc)
        return set()
    return {int(t) for t in (out.stdout or "").split() if t.strip().isdigit()}


def _looks_like_our_exe(pid: int) -> bool:
    """そのプロセスがこのツールの exe か(コマンドラインで見る。取れなければ違うとみなす)。"""
    import launch_guard
    command = (launch_guard.process_command_line(pid) or "").lower()
    return bool(command) and any(name in command for name in EXE_NAMES)


def _exe_alive(desktop: dict) -> bool:
    """止める相手の窓(exe)がまだ動いているか。印から来たものは**番号と起動時刻で**本人か見る(VER4.8.127)。"""
    import launch_guard
    exe_pid = int(desktop.get("exe_pid") or 0)
    if exe_pid <= 0 or not launch_guard.is_process_alive(exe_pid):
        return False
    if desktop.get("started_at") and launch_guard.desktop_exe_alive(desktop) is False:
        log.info("印の窓 pid=%s は動いていますが、別のプログラムです(番号の使い回し)", exe_pid)
        return False
    return True


def _python_alive(desktop: dict) -> bool:
    """印を書いた Python(bridge.py)がまだ動いているか。番号と起動時刻、取れなければコマンドラインで見る。"""
    import launch_guard
    py_pid = int(desktop.get("pid") or 0)
    if py_pid <= 0 or not launch_guard.is_process_alive(py_pid):
        return False
    same = launch_guard.desktop_python_alive(desktop) if desktop.get("started_at") else None
    if same is not None:
        return same
    # 起動時刻が取れない: `desktop_running` と同じ確かめ方(取れたコマンドラインが bridge.py でなければ別物)
    command = launch_guard.process_command_line(py_pid)
    return not command or "bridge.py" in command


def _finish_python(desktop: dict) -> None:
    """窓が終わったあと、中の Python も終わるのを待つ(外枠は終わるとき子をまとめて止める。VER4.8.84)。
    残っていれば止める(フォルダを掴んだままにしない)。本人と確かめられたときだけ。"""
    py_pid = int(desktop.get("pid") or 0)
    if py_pid > 0 and not _wait_pid_gone(py_pid, FORCE_WAIT_SEC) and _python_alive(desktop):
        _kill_tree(py_pid)
        _wait_pid_gone(py_pid, FORCE_WAIT_SEC)


def _stop_lone_python(py_pid: int, result: StopResult) -> StopResult:
    _kill_tree(py_pid)
    if _wait_pid_gone(py_pid, FORCE_WAIT_SEC):
        result.stopped = True
        result.method = "python"
        result.message = "窓は終わっていて、残っていた Python を止めました"
    else:
        result.message = f"残っていた Python(pid {py_pid})を止められませんでした"
    return result


def _kill_tree(pid: int) -> None:
    """そのプロセスと子をまとめて止める(Windows は taskkill /T /F)。"""
    import subprocess
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("強制終了を頼めませんでした: %s", exc)
        return
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError as exc:
        log.warning("強制終了を頼めませんでした: %s", exc)


def _ask_windows_to_close(pids: set[int]) -> int:
    """そのプロセスの**見えている最上位の窓**(持ち主のいる窓は除く)に WM_CLOSE を送る。送った窓の数。

    ランチャーの `desktop._windows_of`・`scripts/desktop_smoke.py` の `ask_to_close` と同じ選び方。
    見えない窓(外枠の中の仕組みの窓)には送らない。Windows 以外では SIGTERM(試験・開発用)。
    """
    if os.name != "nt":
        sent = 0
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                sent += 1
            except OSError:
                pass
        return sent
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    found: list = []

    def visit(hwnd, _param):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):   # 4 = GW_OWNER
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value in pids:
                found.append(hwnd)
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return sum(1 for hwnd in found if user32.PostMessageW(hwnd, 0x0010, 0, 0))   # 0x0010 = WM_CLOSE


def _request_shutdown(port: int, token: str, *, force: bool) -> dict:
    """`POST /api/shutdown` で正常終了を要求する(基盤仕様書 2.8 の1段目)。

    トークンはロックファイルから読む。ロックは利用者ごとのローカル領域に
    あるので、読める相手はそもそもプロセスを直接落とせる。
    """
    import launch_guard

    url = f"http://127.0.0.1:{port}/api/shutdown"
    body = json.dumps({"force": force}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Tool-Token"] = token
    try:
        # プロキシを経由しない送信口を使う(`launch_guard` の注釈を参照)。
        # 社内PCのプロキシ設定に 127.0.0.1 の除外が無いと、自分自身への
        # 停止要求までプロキシへ送られて届かない
        with launch_guard.local_request(url, timeout=5, data=body,
                                        method="POST", headers=headers) as res:
            payload = json.loads(res.read().decode("utf-8"))
            return {"ok": bool(payload.get("stopped"))}
    except urllib.error.HTTPError as exc:
        if exc.code == 409:                        # 実行中の処理がある
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:                      # noqa: BLE001
                payload = {}
            return {"ok": False, "busy": True,
                    "running": payload.get("running", ["(不明)"])}
        log.info("停止要求は %s で拒否されました。PIDで止めます", exc.code)
        return {"ok": False}
    except (urllib.error.URLError, OSError) as exc:
        log.info("停止要求を送れませんでした (%s)。PIDで止めます", exc)
        return {"ok": False}


CLEANED_HINT = "片付けました。Start.vbs で起動できます"


def _clean_stale_lock(mode: str, info, result: StopResult) -> StopResult:
    """ツールは動いていない(ロックだけ残っている)。**ロックを片付ける**(プロセスは止めない)。"""
    import launch_guard

    launch_guard.remove_lock(mode, expect=info)
    log.info("%s: 動いていないのにロックが残っていました(pid=%s)。%s", mode, info.pid, CLEANED_HINT)
    result.stopped = True
    result.method = "stale-lock"
    result.message = f"動いていませんでした(残っていたロックを{CLEANED_HINT})"
    return result


def _stop_by_pid(mode: str, info, result: StopResult, *, force: bool) -> StopResult:
    """PIDで止める。**落とす前に本当にこのアプリかを確かめる。**"""
    import launch_guard

    if not launch_guard.lock_owner_alive(info):
        return _clean_stale_lock(mode, info, result)

    if not _looks_like_our_process(info):
        command = launch_guard.process_command_line(info.pid)
        if command and "bridge.py" not in command:
            # コマンドラインが取れて、このツールではない = 別のプログラムが同じ番号を使っている。
            # このツールは動いていないので、止めずにロックだけ片付ける
            log.info("pid=%s は別のプログラムです(番号の使い回し): %s", info.pid, command[:200])
            return _clean_stale_lock(mode, info, result)
        result.message = (
            f"pid {info.pid} はこのアプリではないようなので止めません。\n"
            f"    手動で確認してください(ロック: {launch_guard.lock_path(mode)})")
        log.warning("pid=%s の照合に失敗したため停止しません", info.pid)
        return result

    log.info("pid=%s を停止します", info.pid)
    try:
        os.kill(info.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        result.message = f"停止できませんでした: {exc}"
        return result

    if _wait_pid_gone(info.pid, FORCE_WAIT_SEC):
        launch_guard.remove_lock(mode, expect=info)
        result.stopped = True
        result.method = "sigterm"
        result.message = "終了しました"
        return result

    if not force:
        result.message = ("終了要求に応じません。"
                          "強制的に止めるには --force を付けてください")
        return result

    try:
        os.kill(info.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError as exc:
        result.message = f"強制終了できませんでした: {exc}"
        return result
    # **消えたのを確かめてから**「強制終了しました」と言う
    if not _wait_pid_gone(info.pid, FORCE_WAIT_SEC):
        result.message = (f"強制終了を頼みましたが、pid {info.pid} がまだ残っています。"
                          "タスクマネージャーで python / pythonw を確かめてください")
        return result
    launch_guard.remove_lock(mode, expect=info)
    result.stopped = True
    result.method = "sigkill"
    result.message = "強制終了しました"
    return result


def _looks_like_our_process(info) -> bool:
    """そのPIDが本当にこのアプリか(`launch_guard.looks_like_our_process`。起動し直すときも同じ確かめ方)。"""
    import launch_guard

    return launch_guard.looks_like_our_process(info.pid, info.app_root)


def _wait_gone(port: int, timeout: float) -> bool:
    """そのポートが応答しなくなるまで待つ。"""
    import launch_guard

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if launch_guard.probe_health(port, timeout=0.3) is None:
            return True
        time.sleep(0.2)
    return False


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    import launch_guard

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not launch_guard.is_process_alive(pid):
            return True
        time.sleep(0.2)
    return False


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="梱包資材総合ツールを安全に停止する")
    parser.add_argument("--mode", default=modes.FIELD,
                        choices=list(modes.KEYS) + list(modes.LEGACY_NAMES))
    parser.add_argument("--all", action="store_true", help="現場と資材の両方")
    parser.add_argument("--force", action="store_true",
                        help="実行中の処理を中断してでも止める")
    parser.add_argument("--status", action="store_true", help="状態を見るだけ")
    args = parser.parse_args(argv)

    targets = list(modes.KEYS) if args.all else [modes.normalize(args.mode)]

    import launch_guard

    desktop = launch_guard.desktop_running()
    if args.status:
        if desktop is not None:
            print(f"[稼働] デスクトップ版(exe の窓): pid={desktop.get('pid')} "
                  f"mode={desktop.get('mode')}")
        for mode in targets:
            health = status(mode)
            if health is None:
                print(f"[--] {mode}: 起動していません")
            else:
                lock = health["_lock"]
                print(f"[稼働] {mode}: pid={lock['pid']} port={lock['port']} "
                      f"ready={health['ready']} 起動={lock['started']}")
        return 0

    # **管理者以外停止モード中は使わない**(VER4.8.129。ランチャーの「全 PC で止める」と同時に使わない、現場の決め)
    from packaging_tool import remote_stop
    refusal = remote_stop.stop_bat_refusal()
    if refusal:
        print(f"止められませんでした: {refusal}")
        return EXIT_FAILED

    results = [stop(mode, force=args.force) for mode in targets]
    for window in desktop_targets(desktop):
        results.append(stop_desktop(window, force=args.force))
    code, lines = report(results)
    for line in lines:
        print(line)
    return code


def report(results: list[StopResult]) -> tuple[int, list[str]]:
    """戻り値と、出力する行。**1行目は理由**(ランチャー 1.8.9: 管理者の一覧と利用者への知らせに出る)。

    止められなかった(1)を、処理中で止めなかった(3)より先に言う。全部止まれば 0。
    """
    failed = [r for r in results if not r.stopped and not r.busy_jobs]
    busy = [r for r in results if not r.stopped and r.busy_jobs]
    if failed:
        first = f"止められませんでした: {failed[0].message.splitlines()[0]}"
        code = EXIT_FAILED
    elif busy:
        jobs = [job for r in busy for job in r.busy_jobs]
        first = f"処理中のため止めません: {'、'.join(jobs)}"
        code = EXIT_BUSY
    else:
        return EXIT_OK, [str(r) for r in results]
    rest = [str(r) for r in sorted(results, key=lambda r: r.stopped)]
    return code, [first, *rest]


if __name__ == "__main__":
    raise SystemExit(main())
