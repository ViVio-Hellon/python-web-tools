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
    1  止められなかった(応答しない・このアプリか確かめられない)、
       またはデスクトップ版(exe の窓)が動いている
    2  止めなかった(保存していない図・実行中の処理がある。`--force` で止める)

**デスクトップ版(exe の窓)は止めない。** 窓の × か画面の「終了」で閉じる
(閉じるときに保存していない図・実行中の処理を訊くため)。ランチャーは exe の窓に
「閉じて」と頼んで止める(× と同じ)。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))

from packaging_tool import app_config, modes  # noqa: E402
from packaging_tool.logging_utils import get_logger  # noqa: E402

log = get_logger("process_manager")

# 正常終了を要求したあと、実際に落ちるのを待つ上限(秒)
GRACEFUL_WAIT_SEC = 8.0
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
    if not launch_guard.is_our_app(health, mode):
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
    if not launch_guard.is_our_app(health, mode):
        # 応答しない、または別のアプリ。ロックだけ残っている状態
        if launch_guard.is_process_alive(info.pid):
            return _stop_by_pid(mode, info, result, force=force)
        launch_guard.remove_lock(mode)
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

    if asked.get("ok") and _wait_gone(info.port, GRACEFUL_WAIT_SEC):
        launch_guard.remove_lock(mode)
        result.stopped = True
        result.method = "graceful"
        result.message = "正常に終了しました"
        return result

    # --- 2. 応答しないときだけPIDを使う ---
    return _stop_by_pid(mode, info, result, force=force)


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
        launch_guard.remove_lock(mode)
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
        launch_guard.remove_lock(mode)
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
    launch_guard.remove_lock(mode)
    result.stopped = True
    result.method = "sigkill"
    result.message = "強制終了しました"
    return result


def _looks_like_our_process(info) -> bool:
    """そのPIDが本当にこのアプリか。

    コマンドラインに**このアプリの置き場所**が入っていることを確かめる。
    別のフォルダにある同じツールや、無関係なPythonを巻き添えにしない。
    コマンドラインが取れない環境では False にして、止めずに知らせる
    (誤って別のアプリを落とすより、止まらないほうが害が小さい)。
    """
    import launch_guard

    cmdline = launch_guard.process_command_line(info.pid)
    if not cmdline:
        log.warning("pid=%s のコマンドラインを取得できませんでした", info.pid)
        return False

    root = (info.app_root or str(app_config.APP_ROOT)).replace("\\", "/")
    normalized = cmdline.replace("\\", "/")
    if root and root in normalized:
        return True
    # 起動スクリプト名でも照合する(相対パスで起動された場合)
    return "start_app.py" in normalized


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

    code = 0
    results = [stop(mode, force=args.force) for mode in targets]
    for result in results:
        if not result.stopped:
            # 断られた(2)より、止められなかった(1)を先に知らせる
            code = 1 if code == 1 or not result.busy_jobs else 2
    # **止めなかった理由を先に出す。** ランチャー(1.7.1〜)は stop.bat が 0 以外で
    # 終わると、出力の**はじめの2行**を理由として利用者に見せる。「起動していません」が
    # 先に来ると、理由がそれになる
    if desktop is not None:
        print(DESKTOP_MESSAGE)
        code = code or 1
    for result in sorted(results, key=lambda r: r.stopped):
        print(result)
    return code


# デスクトップ版は止めない。**「起動していません」で 0 を返さない** ──
# 以前はブラウザ版のロックだけを見ていたので、exe の窓が動いていても
# 「止めた」と同じ答えになっていた
DESKTOP_MESSAGE = ("[--] デスクトップ版(exe の窓)が動いています。stop.bat では止めません。\n"
                   "    窓の × か画面の「終了」で閉じてください"
                   "(保存していない図・実行中の処理があれば訊きます)")


if __name__ == "__main__":
    raise SystemExit(main())
