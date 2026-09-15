#!/usr/bin/env python3
"""Python側の起動開始点 (基盤仕様書 2.5)

アプリ本体を読む前に実行環境を整え、多重起動を判定し、サーバを立てて
ブラウザを開く。**業務機能はここに書かない。**

    Start.vbs (通常) / start.bat (診断)
        └─ start_app.py            ← ここ
             ├─ 実行環境の確認      (Python版数・必須パッケージ・書込権限)
             ├─ モードを決める      (`アクセス権限` マスタ)
             ├─ launch_guard        (多重起動の判定・ポート選び)
             ├─ server              (waitress + Flask)
             └─ ブラウザを開く

使い方:

    python start_app.py                  この端末に許されたモードで開く
    python start_app.py --mode material  モードを指定する(診断・並行運用)
    python start_app.py --no-browser     ブラウザを開かない(検証用)
    python start_app.py --check          環境の確認だけして終わる(診断用)
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))

# 起動の起点。**待機画面が出るまでの時間**をログに残すために持つ ──
# 「遅い」という感想を、測れる数字にしておく
_BOOT_AT = time.monotonic()

# 必要なPython。README が 3.9 以上を掲げているので、それに合わせる
MIN_PYTHON = (3, 9)

# `requirements.txt` に対応する import 名(pip の名前と違うものがある)
REQUIRED_PACKAGES = (("flask", "Flask"), ("waitress", "waitress"))

# ブラウザを開いたあと、待ち受けが始まるのを待つ上限(秒)
LISTEN_TIMEOUT_SEC = 15

# `--mode` の既定。**どのショートカットを押したかでは決めない**の意味。
# 開くモードは `アクセス権限` マスタが端末ごとに決める(基盤仕様書 2.1
# 「利用者向けの通常起動ファイルを1つに絞る」)
AUTO = "auto"


class StartupError(RuntimeError):
    """利用者に見せる、次の行動が分かる形のエラー。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ------------------------------------------------------------------
# 1. 実行環境の確認
# ------------------------------------------------------------------
def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        raise StartupError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上が必要です"
            f"(いまは {sys.version.split()[0]})",
            "https://www.python.org/downloads/ から新しいPythonを入れてください。")


def check_packages() -> None:
    """必須パッケージの有無。**入れ方まで示す**(基盤仕様書 ステップ5)。"""
    import importlib.util

    missing = [pip_name for module, pip_name in REQUIRED_PACKAGES
               if importlib.util.find_spec(module) is None]
    if missing:
        raise StartupError(
            f"必要なパッケージが入っていません: {', '.join(missing)}",
            f"コマンドプロンプトで次を実行してください:\n"
            f"    {console_python()} -m pip install -r requirements.txt")


def console_python() -> str:
    """`pip` を実行するときに使うPythonの名前。

    `Start.vbs` は画面を出さないために **pythonw.exe** で起動する。
    そのまま `sys.executable` を案内すると
    `pythonw.exe -m pip install ...` と出るが、pythonw には画面が無いので
    **実行しても何も表示されない**(成否すら分からない)。
    案内するときは必ずコンソール側の `python.exe` に読み替える。
    """
    # Windowsのパスを他のOSで扱うと `Path` が `\` を区切りとみなさない。
    # 案内文を組み立てるだけなので、両方の区切りで切っておく
    name = sys.executable.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().startswith("pythonw"):
        return "python" + name[len("pythonw"):]
    return name


def should_abort(check) -> bool:
    """起動確認が取れなかったとき、起動そのものを中止するか。

    中止するのは **TCPでも繋がらないとき**だけ。
    TCPが通っているならサーバ自身は待ち受けており、応答を取れないのは
    こちら側の確認経路の都合(プロキシ・セキュリティ製品)であることが
    多い。ブラウザはローカルアドレスをプロキシから除外するのが普通なので、
    そのまま開けばつながる。

    以前はここを区別せず中止していたため、**動いているサーバごと**
    終わらせてしまい、現場では「起動できません」としか見えなかった。
    """
    return not check.ok and not check.tcp_ok


def describe_loopback() -> str:
    """自分自身への通信まわりの状態(診断用)。

    「サーバは起動しているのに画面が出ない」の原因はここに集まる。
    社内PCではプロキシ設定に 127.0.0.1 の除外が無いことがあり、
    **自分自身への通信までプロキシへ送られて失敗する**。
    実際にこれで起動できなかったので、確認できるようにしておく。
    """
    import launch_guard

    lines = ["", "--- 自分自身への通信 ---"]
    proxies = launch_guard.proxy_settings()
    if proxies:
        lines.append("プロキシ設定  : "
                     + ", ".join(f"{k}={v}" for k, v in sorted(proxies.items())))
        lines.append("  ※このアプリはプロキシを経由せずに 127.0.0.1 へ接続します。")
        lines.append("    ブラウザ側でも 127.0.0.1 / localhost が除外されているか"
                     "確認してください。")
    else:
        lines.append("プロキシ設定  : なし")
    return "\n".join(lines)


def check_writable() -> Path:
    """ローカル領域を作れるか(基盤仕様書 2.7)。"""
    from packaging_tool import app_config

    try:
        root = app_config.ensure_local_dirs()
    except OSError as exc:
        raise StartupError(
            f"作業用フォルダを作れません: {exc}",
            "書き込みの権限があるか、ディスクの空きがあるか確認してください。") from None

    probe = root / "runtime" / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StartupError(
            f"作業用フォルダに書き込めません: {root}",
            f"{exc}") from None
    return root


def check_config() -> None:
    """アプリ固有値が読めているか。読めなくても既定値で動くが、記録は残す。"""
    from packaging_tool import app_config

    error = app_config.load_error()
    if error:
        log().warning("%s — 既定値で起動します", error)
    for problem in app_config.port_range_conflicts():
        log().warning("%s", problem)


def run_environment_checks() -> Path:
    """順に確認する。落ちたところで理由が分かるように分けてある。"""
    check_python_version()
    check_packages()
    root = check_writable()
    check_config()
    return root


# ------------------------------------------------------------------
# ログ
# ------------------------------------------------------------------
_log = None


def log():
    """起動入口のログ(基盤仕様書 2.6 の `launcher.log` にあたる)。

    `packaging_tool.logging_utils` は業務ログ(`app.log`)なので、
    「起動前に失敗したのか」を切り分けられるよう名前を分ける。
    """
    global _log
    if _log is None:
        from packaging_tool.logging_utils import get_logger
        _log = get_logger("launcher")
    return _log


def log_environment(mode: str) -> None:
    """起動のたびに残す1枚(基盤仕様書 2.6)。"""
    from packaging_tool import app_config

    log().info("=" * 60)
    log().info("起動: mode=%s pid=%s", mode, os.getpid())
    log().info("Python: %s (%s)", sys.version.split()[0], sys.executable)
    log().info("アプリ本体: %s", APP_ROOT)
    log().info("ローカル領域: %s", app_config.local_root())


# ------------------------------------------------------------------
# 2. 起動
# ------------------------------------------------------------------
def resolve_mode(requested: str) -> str:
    """開くモードを決める。

    **既定は「この端末に許されているモード」**(`アクセス権限` マスタ)。
    以前は現場用と資材用でショートカットを分けていたが、資材の端末で
    現場のほうを押すと「発注一覧が出ない」となり、権限の問題として
    調べることになる。押すものは1つでよく、どちらで開くかは端末が知っている。

    明示された `--mode` はそのまま使う ── 診断のときと、両方の権限を
    持つ端末が2つ並べて開くときに要る。
    """
    from packaging_tool import modes

    if requested and requested != AUTO:
        # 旧名(warehouse)をここで正す。ロックファイルの名前もモードで
        # 決まるので、`--mode warehouse` で起動して `--mode material` で
        # 止められるように**入口で1度だけ**そろえる
        return modes.normalize(requested)

    # **`app`(Flask)を読まない。** ここは待機画面より前なので、
    # 重い import を1つでも減らす(基盤仕様書 2.2)
    from packaging_tool import access_control
    return access_control.startup_grant().startup_mode()


def start(mode: str, *, open_browser: bool = True) -> int:
    """戻り値はプロセスの終了コード。

    【順番が要点】
    待機画面より前に置くものを、**できるだけ減らしてあります**。
    以前は Flask とアプリ本体を組み立ててからブラウザを開いていたので、
    いちばん重い import を「待たせるために見せるもの」より先にやって
    いました ── 出るころには待つ理由がほぼ終わっています。

        ポートを決める → 待ち受け開始(待機画面だけ)→ ブラウザ
            → 本体を組み立てて差し替え → 重い初期化

    最初の3つは標準ライブラリと `app_config` だけで済みます。
    """
    import launch_guard
    import server as server_module
    from packaging_tool import app_config

    mode = resolve_mode(mode)
    log_environment(mode)

    # --- 多重起動の判定 (基盤仕様書 2.4) ---
    #
    # **まずロックを取る。** 判定してから書くまでのあいだに始まった
    # 2つ目は「ロックなし」を見て一緒に立ち上がってしまう ── 待ち受けの
    # 確認だけで最大15秒あり、その日の最初なら取り込みも走るので、
    # 利用者が「反応が無い」ともう一度押す時間は十分にある。
    # `try_acquire` は作れたかどうかで決めるので、2つ目は必ず外れる。
    while not launch_guard.try_acquire(mode):
        guard = launch_guard.check_existing(mode)
        if not guard.should_start:
            log().info("既存のインスタンスに合流します: %s", guard.url)
            print(f"すでに起動しています。ブラウザを開きます: {guard.url}")
            if open_browser:
                webbrowser.open(guard.url)
            return 0
        # 掃除できた(死んでいた・古い版だった)ので取り直す。
        # それでも取れなければ、掃除した隙に別の1つ目が入っている
        log().info("多重起動の判定: %s", guard.reason)

    # ここから先の失敗は**必ずロックを手放してから**投げる。
    # 残すと、次の起動が「起動中のまま応答がない」を待つことになる
    try:
        # --- ポート選び ---
        port = launch_guard.pick_port(mode)
        if port is None:
            candidates = app_config.port_candidates(mode)
            raise StartupError(
                f"使えるポートがありません(試した番号: {candidates})",
                "他のアプリが使っている可能性があります。"
                "config/app.json の port を変えるか、そのアプリを終了してください。")

        # --- 待ち受けを始める(この時点ではまだ待機画面だけ) ---
        srv = server_module.AppServer(mode, port)
        thread = server_module.run_in_background(srv)

        check = server_module.diagnose_listening(port, timeout=LISTEN_TIMEOUT_SEC)
        if should_abort(check):
            raise StartupError(
                "サーバを起動できませんでした",
                f"{check.hint}\n\nログ: {app_config.local_dir('logs')}")
    except BaseException:
        launch_guard.remove_lock(mode)
        raise
    if not check.ok:
        log().warning("起動確認の応答を取れませんでしたが、待ち受けは"
                      "できているので続行します:\n%s", check.hint)
        print("[注意] 起動の確認応答を取れませんでした。"
              "画面が出ない場合は次を確認してください:")
        print(check.hint)

    # 待ち受けが始まってからロックを書く。先に書くと、起動に失敗した
    # ロックが残って次回の判定を惑わせる
    launch_guard.write_lock(
        launch_guard.build_lock_info(mode, port, srv.token))

    try:
        # --- ブラウザを開く ---
        # **ここまでが最短**。待機画面はもう出せる状態で、本体の
        # 組み立ては次の行から始まる
        if open_browser:
            log().info("ブラウザを開きます: %s", srv.url)
            webbrowser.open(srv.url)
        else:
            print(f"起動しました: {srv.url}")
        log().info("待機画面まで %.2f秒", time.monotonic() - _BOOT_AT)

        # --- 本体を組み立てて差し替える ---
        try:
            srv.build()
        except Exception as exc:                  # noqa: BLE001 - 画面に出して継続
            log().exception("アプリを組み立てられませんでした")
            srv.boot.mark_error(f"アプリを組み立てられませんでした: {exc}")
            _hold_until_stopped(srv, thread)
            return 1

        _initialize(srv)

        # --- 待ち受けが終わるまでここで止まる ---
        _hold_until_stopped(srv, thread)
        return 0
    finally:
        launch_guard.remove_lock(mode)
        log().info("終了しました: mode=%s", mode)


# 停止を頼んでから、受付の輪が終わるのを待つ上限(秒)。
# ここを過ぎたら**確実に落とす** ── 残ったプロセスは次回の起動で
# 「すでに起動しています」と判定され、入れ替えた新しい版が動かない
EXIT_WAIT_SEC = 6.0


def _hold_until_stopped(srv, thread) -> None:
    """待ち受けが終わるまで止まる。**終わらなくても必ず抜ける。**

    waitress の受付の輪は、つながったままの接続が1本でもあると
    回り続けます(`server.AppServer.stop` の説明)。そちらは直して
    ありますが、**ここでも受け止めておきます** ── 止まらないことの
    害が大きすぎるからです。残ったプロセスは、次の起動で
    「すでに起動しています」と判定され、入れ替えた新しい版が
    いつまでも動きません。
    """
    while thread.is_alive():
        thread.join(timeout=0.5)
        if not srv.stop_requested:
            continue
        # 停止を頼んである。ここからは上限つきで待つ
        thread.join(timeout=EXIT_WAIT_SEC)
        if thread.is_alive():
            log().warning("待ち受けが %.0f秒 で終わらないので、"
                          "プロセスを終了します", EXIT_WAIT_SEC)
            _hard_exit()
        return


def _hard_exit() -> None:
    """後始末をしてから、確実に落とす。

    `sys.exit()` は例外なので、受付の輪が回っている別スレッドには
    効きません。`os._exit()` は後始末をしないので、**残すべきものは
    先に書いてから**呼びます(ログは追記なので閉じなくても失われない)。
    """
    import logging

    logging.shutdown()
    os._exit(0)


def _initialize(srv) -> None:
    """重い初期化。サーバが立ってから行う。

    ここで失敗しても**サーバは落とさない**。落とすと利用者のブラウザには
    「接続できません」としか出ず、理由が伝わらない。画面に理由を出す。
    """
    from packaging_tool import config, db, selection_log_store, user_log

    try:
        srv.mark_stage("アプリを準備中")
        config.ensure_dirs()
        with db.connect() as conn:
            db.apply_schema(conn)
        # 選定ログを日ごとのファイルにも残す。**問い合わせは後日来る**
        user_log.keep_on_disk()
        selection_log_store.prune()
    except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
        log().exception("初期化に失敗しました")
        srv.mark_error(f"初期化に失敗しました: {exc}")
        return

    # 画面が居なくなったら終わる(基盤仕様書 2.8)。
    # **窓が無いアプリなので、タブを閉じたら終わったつもりになる。**
    # 残っていると、次の起動が「すでに起動しています」と判定して
    # 入れ替えた新しい版が動かない
    _watch_for_idle(srv)

    # 取り込み元の自己診断。**結果はログに残す。**
    # 「読めません」の問い合わせが来たとき、ここを見れば原因が分かる
    _diagnose_sources()

    # **取り込みが終わるまで、準備完了にしない。**
    #
    # 以前はここで完了にして、取り込みは背景に流していた。理屈は
    # 「数十秒かかるものを待たせると立ち上がらないように見える」だった
    # が、実際には逆のことが起きていた ── 準備が一瞬で終わるので
    # **起動待機画面が出る前にブラウザが業務画面へ入り**、マスタが空の
    # まま「仕掛台帳が未取り込みです」を読むことになる。基盤仕様書 2.3 の
    # 「未完成の画面が表示されることを防ぐ」の逆をやっていた。
    #
    # 待たせる代わりに、待たせ方を用意する:
    #   進捗を待機画面に出す / 待たずに入る道を置く / 打ち切りを持つ
    if not _start_auto_import(srv):
        srv.mark_ready(True)


# 取り込みを待つ上限。共有フォルダに届かない端末では、応答が返るまで
# OS 側で数十秒かかることがある。**待たせきりにはしない** ── ここを
# 過ぎたら準備完了にして、取り込みは背景で続ける
def _diagnose_sources() -> None:
    """取り込み元を1つずつ開いてみて、**結果をログに残す**。

    「マスタが読めません」の問い合わせが来たとき、これがあれば
    ログを見るだけで原因が分かります ── 大きさ・sqlite3 の印・
    付き添いファイル・どの開き方で通ったか・表の数。
    無ければ、こちらが端末に行って同じことをやり直すことになります。

    **診断で起動を止めません。** 読めないことと、アプリが使えないことは
    別です(設定画面の「いまの状態」が同じ内容を画面にも出します)。
    """
    from packaging_tool import config, data_sync, source_db

    try:
        found: dict[str, object] = {}
        material = data_sync.find_material_db()
        if material is not None:
            found[config.MATERIAL_DB_NAME] = material
        found.update(data_sync.find_lot_dbs())
        if not found:
            log().warning("取り込み元が1つも見つかりません: マスタ=%s 台帳=%s",
                          config.master_db_dir(), config.lot_db_dir())
            return
        for name, path in found.items():
            probe = source_db.probe(path)
            if probe.ok:
                log().info("取り込み元 %s: 開けました(%s / %s表 / %s / %s バイト%s)",
                           name, probe.opened_by, len(probe.tables),
                           probe.journal, f"{probe.size:,}",
                           " / 付き添い " + ",".join(probe.sidecars)
                           if probe.sidecars else "")
            else:
                log().warning("取り込み元 %s: 開けません(%s バイト / 印=%s%s): %s",
                              name, f"{probe.size:,}",
                              "あり" if probe.is_sqlite else "なし",
                              " / 付き添い " + ",".join(probe.sidecars)
                              if probe.sidecars else "",
                              probe.error)
    except Exception as exc:                      # noqa: BLE001 - 診断で止めない
        log().warning("取り込み元の診断に失敗しました: %s", exc)


def _watch_for_idle(srv) -> None:
    """画面が居なくなったら止める見張りを立てる。

    止め方は `/api/shutdown` と同じ道(`srv.stop`)を通します ──
    止め方が2つあると、片方だけ直した状態を作ってしまいます。
    処理中かどうかの判断も同じものを使います。
    """
    from packaging_tool import idle_exit, jobs

    def busy() -> bool:
        return bool(jobs.get_registry().busy_labels())

    idle_exit.install(srv.stop, busy)


IMPORT_WAIT_LIMIT_SEC = 120


def _start_auto_import(srv) -> bool:
    """起動時の自動取り込み(更新されたファイルだけ)を始める。

    始めたら `True`。始めなかったら `False`(呼び手がその場で準備完了に
    する)。始めた場合は、**終わったときに**準備完了にする。

    設定でOFFなら何もしない。ここで例外を出してもサーバは落とさない
    ── 取り込めないことと、アプリが使えないことは別。
    """
    from packaging_tool import config, data_sync, db, jobs, user_settings

    if not user_settings.get(config.KEY_AUTO_IMPORT, config.AUTO_IMPORT_DEFAULT):
        log().info("起動時の自動取り込みは設定でOFFになっています")
        return False

    def work(progress):
        # 作業スレッドで開き直す(`sqlite3` の接続はスレッドをまたげない)
        with db.connect() as conn:
            db.apply_schema(conn)
            return data_sync.auto_import(conn, progress=progress)

    try:
        job = jobs.get_registry().start("import", "起動時の自動取り込み", work)
    except jobs.JobBusy:
        # 起動直後に走っているものがあるのは通常ありえないが、
        # ここで落として起動を失敗にする理由は無い
        log().warning("すでに処理が走っているため自動取り込みは見送りました")
        return False

    srv.mark_stage("取り込み中")
    threading.Thread(target=_ready_when_imported, args=(srv, job),
                     name="ready-watch", daemon=True).start()
    return True


def _ready_when_imported(srv, job) -> None:
    """取り込みの終わりを見て、準備完了にする。

    **打ち切りを持つ。** 共有に届かない端末で待たせきりにすると、
    「立ち上がらない」と受け取られる ── 取り込めないことと、アプリが
    使えないことは別なので、上限を過ぎたら入れるようにして、取り込みは
    背景で続けさせる。
    """
    waited = 0.0
    while job.is_running and waited < IMPORT_WAIT_LIMIT_SEC:
        time.sleep(0.3)
        waited += 0.3
    if job.is_running:
        log().warning("取り込みが %s秒 で終わらないので、先に画面を開きます",
                      IMPORT_WAIT_LIMIT_SEC)
    srv.mark_ready(True)


# ------------------------------------------------------------------
# 3. 失敗の伝え方
# ------------------------------------------------------------------
def report_failure(error: StartupError, *, open_browser: bool) -> None:
    """コンソールとブラウザの両方に出す。

    `Start.vbs`(コンソール非表示)で起動された場合、標準出力は誰にも
    見えない。基盤仕様書 2.2 の「起動できない場合は、確認すべきログの
    場所を表示します」を満たすため、HTMLを書いてブラウザで開く。
    """
    print(f"\n[エラー] {error}", file=sys.stderr)
    if error.hint:
        print(error.hint, file=sys.stderr)

    try:
        from packaging_tool import app_config
        log_dir = str(app_config.local_dir("logs"))
    except Exception:                             # noqa: BLE001 - 失敗の報告で失敗しない
        log_dir = "(ローカル領域を特定できませんでした)"

    try:
        log().error("起動に失敗: %s / %s", error, error.hint)
    except Exception:                             # noqa: BLE001
        pass

    if not open_browser:
        return
    try:
        path = _write_error_page(str(error), error.hint, log_dir)
        webbrowser.open(path.as_uri())
    except Exception as exc:                      # noqa: BLE001
        print(f"(エラー画面を出せませんでした: {exc})", file=sys.stderr)


def _write_error_page(message: str, hint: str, log_dir: str) -> Path:
    """起動に失敗したことを伝えるHTMLを一時領域に書く。"""
    import html
    import tempfile

    try:
        from packaging_tool import app_config
        target = app_config.local_dir("work") / "起動エラー.html"
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:                             # noqa: BLE001
        target = Path(tempfile.gettempdir()) / "packaging_tool_起動エラー.html"

    target.write_text(f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>起動できませんでした</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
       background:#eef1f5;color:#101720;
       font-family:system-ui,"Yu Gothic UI","Meiryo UI",sans-serif;line-height:1.7}}
 .box{{width:min(560px,calc(100vw - 48px));background:#fff;border:1px solid #c9d2dc;
       border-radius:4px;padding:32px;box-shadow:0 6px 20px rgba(16,23,32,.08)}}
 h1{{margin:0 0 12px;font-size:19px;color:#b4232a}}
 .hint{{margin-top:16px;padding:14px;background:#fdeaea;border-left:4px solid #b4232a;
        border-radius:0 3px 3px 0;white-space:pre-wrap}}
 code{{font-family:ui-monospace,Consolas,monospace;font-size:13px;
       background:rgba(0,0,0,.06);padding:2px 5px;border-radius:2px;word-break:break-all}}
 dt{{color:#556171;font-size:13px;margin-top:12px}}
</style></head>
<body><main class="box">
<h1>起動できませんでした</h1>
<p>{html.escape(message)}</p>
{f'<div class="hint">{html.escape(hint)}</div>' if hint else ''}
<dt>ログの場所</dt>
<p><code>{html.escape(log_dir)}</code></p>
<dt>診断</dt>
<p>コンソールで詳しく見るには <code>start.bat</code> を実行してください。</p>
</main></body></html>
""", encoding="utf-8")
    return target


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="梱包資材総合ツールを起動する")
    # 既定は auto ── **この端末に許されたモード**で開く。
    # 旧名(warehouse)も受ける。現場に配ったショートカットがまだ
    # そちらで書かれている。値の対応は `packaging_tool/modes.py` が持つが、
    # ここは環境確認より前なのでパッケージを読み込まない(2.5 起動処理の分離)
    parser.add_argument("--mode", default=AUTO,
                        choices=[AUTO, "field", "material", "warehouse"],
                        help="開くモード。既定(auto)はアクセス権限マスタが決める")
    parser.add_argument("--no-browser", action="store_true",
                        help="ブラウザを開かない(検証用)")
    parser.add_argument("--check", action="store_true",
                        help="実行環境の確認だけして終わる(診断用)")
    args = parser.parse_args(argv)

    open_browser = not args.no_browser

    try:
        run_environment_checks()
    except StartupError as exc:
        report_failure(exc, open_browser=open_browser)
        return 1

    if args.check:
        from packaging_tool import app_config
        print("実行環境の確認: 問題ありません\n")
        print(app_config.describe())
        print(describe_loopback())
        return 0

    try:
        return start(args.mode, open_browser=open_browser)
    except StartupError as exc:
        report_failure(exc, open_browser=open_browser)
        return 1
    except KeyboardInterrupt:
        print("\n中断しました")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
