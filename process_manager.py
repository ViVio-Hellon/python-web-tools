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
        # 応答しない、または別のアプリ。ロックだけ残っている状態
        if launch_guard.is_process_alive(info.pid):
            return _stop_by_pid(mode, info, result, force=force)
        launch_guard.remove_lock(mode, expect=info)
        result.stopped = True
        result.method = "stale-lock"
        result.message = "動いていませんでした(残っていたロックを片付けました)"
        return result

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

    ふだんは窓に「閉じて」と頼む(× と同じ。Windows は `taskkill` の /F なしで窓に閉じる合図を送る)。
    窓が閉じる前に、保存していない図・実行中の処理を訊く ── そのあいだ窓は残るので「処理中」。
    `force` なら窓とその子(Python)をまとめて止める(窓の中で保存していないものは捨てる)。
    """
    import launch_guard

    result = StopResult("デスクトップ版")
    exe_pid = int(desktop.get("exe_pid") or 0)
    py_pid = int(desktop.get("pid") or 0)
    target = exe_pid if exe_pid > 0 and launch_guard.is_process_alive(exe_pid) else py_pid
    if target <= 0 or not launch_guard.is_process_alive(target):
        result.stopped = True
        result.method = "already-gone"
        result.message = "すでに終了していました"
        return result
    _close_window(target, force=force)
    if _wait_pid_gone(target, DESKTOP_WAIT_SEC if not force else FORCE_WAIT_SEC):
        if py_pid > 0:
            _wait_pid_gone(py_pid, FORCE_WAIT_SEC)
        result.stopped = True
        result.method = "force" if force else "close"
        result.message = "強制終了しました" if force else "窓を閉じました"
        return result
    if force:
        result.message = (f"強制終了を頼みましたが、pid {target} がまだ残っています。"
                          "タスクマネージャーで 梱包資材総合ツール.exe を確かめてください")
        return result
    result.busy_jobs = ["デスクトップ版の窓で、保存していない図・実行中の処理を確かめています"]
    result.message = ("窓が閉じませんでした(保存していない図・実行中の処理を窓で訊いています)。\n"
                      "    窓で答えるか、捨てて・中断して止めるには --force を付けてください")
    return result


def _close_window(pid: int, *, force: bool) -> None:
    """窓に閉じる合図を送る(`force` なら子ごと強制終了)。"""
    import subprocess
    if os.name == "nt":
        args = ["taskkill", "/PID", str(pid)] + (["/T", "/F"] if force else [])
        try:
            subprocess.run(args, capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("窓に閉じる合図を送れませんでした: %s", exc)
        return
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM)
    except OSError as exc:
        log.warning("窓に閉じる合図を送れませんでした: %s", exc)


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


def _stop_by_pid(mode: str, info, result: StopResult, *, force: bool) -> StopResult:
    """PIDで止める。**落とす前に本当にこのアプリかを確かめる。**"""
    import launch_guard

    if not launch_guard.is_process_alive(info.pid):
        launch_guard.remove_lock(mode, expect=info)
        result.stopped = True
        result.method = "already-gone"
        result.message = "すでに終了していました"
        return result

    if not _looks_like_our_process(info):
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

    results = [stop(mode, force=args.force) for mode in targets]
    if desktop is not None:
        results.append(stop_desktop(desktop, force=args.force))
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
