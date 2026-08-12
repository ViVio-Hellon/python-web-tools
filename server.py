"""Webサーバの起動だけを担当する (基盤仕様書 2.5)

起動監視(`launch_guard.py`)とWeb処理(`app/`)の境界。
ここは「どのアプリを、どのポートで、どう止めるか」しか知らない。

Flask 同梱の開発用サーバは本番利用を想定していないため、
純Python実装で Windows でも安定して動く **waitress** を使う。
"""
from __future__ import annotations

import threading
from typing import Optional

from packaging_tool import app_config
from packaging_tool.logging_utils import get_logger

log = get_logger("server")

# 停止要求を受けてからサーバを閉じるまでの猶予(秒)
GRACEFUL_TIMEOUT_SEC = 3


class AppServer:
    """待ち受けているサーバ1つ分。

    `serve_forever()` は呼び出したスレッドを占有する。
    停止は `/api/shutdown` から呼ばれる `stop()`(別スレッド)で行う。
    """

    def __init__(self, mode: str, port: int, *, token: Optional[str] = None) -> None:
        import secrets

        from boot_server import BootApp, Handover

        self.mode = mode
        self.port = port
        self._stopped = threading.Event()
        # 停止要求は1回だけ通す(下の `stop()` を参照)
        self._stop_lock = threading.Lock()
        self._stop_requested = False

        # **本体はまだ組み立てない。** Flask とアプリ本体の import が
        # 起動でいちばん重く、そのあとに待機画面を出したのでは
        # 「待たせるために見せるもの」を待たせることになる(基盤仕様書 2.2)。
        # 先に待ち受けを始めてブラウザを開き、本体は `build()` で後から
        # 差し込む。トークンはここで決める ── 差し替えの前後で変わると、
        # 待機画面が持っているトークンが通らなくなる
        self.token = token or secrets.token_urlsafe(24)
        self.boot = BootApp(mode, port, self.token)
        self.wsgi = Handover(self.boot)
        self.app = None
        self.url = f"http://{app_config.host()}:{port}/?t={self.token}"

        self._server = None

    # --------------------------------------------------------------
    def build(self) -> None:
        """本体を組み立てて、待ち受けを引き継ぐ。

        **待ち受けは開き直さない。** 開き直すと、その瞬間の問い合わせが
        落ちて、待機画面には「接続できません」と映る。
        """
        from app import create_app
        from app.routes import health

        self.app = create_app(self.mode, token=self.token, port=self.port)
        # 「どうやって止めるか」をルート側へ渡す。
        # ルートが waitress を直接知らないようにするため、関数で注入する
        health.set_shutdown_hook(self.stop)
        # 待機画面が読んでいた段を引き継ぐ。引き継がないと、
        # 差し替わった瞬間に「アプリを準備中」へ巻き戻って見える
        self.app.config["STAGE"] = self.boot.stage
        if self.boot.startup_error:
            self.app.config["STARTUP_ERROR"] = self.boot.startup_error
        self.wsgi.install(self.app)

    # --------------------------------------------------------------
    def serve_forever(self) -> None:
        """待ち受けを開始する。`stop()` が呼ばれるまで返らない。"""
        from waitress.server import create_server

        self._server = create_server(
            self.wsgi, host=app_config.host(), port=self.port,
            # 1人が1台で使うアプリなので、スレッドは控えめでよい。
            # 長時間処理は背景スレッドで走らせるため、待ち受け側を
            # 増やしても意味がない
            threads=8,
            # `ident` は HTTP の `Server:` ヘッダになる。ヘッダは latin-1 で
            # 符号化されるので、**日本語を入れるとすべての応答が失敗する**
            # (表示名を渡して UnicodeEncodeError になった)。ASCII に限る
            ident=f"{app_config.app_id()}/{self.mode}",
        )
        log.info("待ち受け開始: %s (mode=%s)", self.url, self.mode)
        try:
            self._server.run()
        finally:
            self._stopped.set()
            log.info("待ち受け終了: mode=%s", self.mode)

    def stop(self) -> None:
        """サーバを閉じる。`/api/shutdown` から別スレッドで呼ばれる。

        **2回以上呼ばれても安全**にしてある。画面の「終了」と `stop.bat` が
        続けて来ることがあり、閉じたソケットをもう一度閉じると
        waitress の内部で「Bad file descriptor」が出る。

        【`close()` だけでは終わらない】
        waitress の受付の輪は `while map:` で回っており、**map が空に
        なるまで返りません**。`server.close()` が閉じるのは待ち受けの
        ソケットだけで、ブラウザが張っている keep-alive の接続は map に
        残ります。つまり画面を開いたまま止めると、待ち受けは閉じるのに
        輪は回り続け、**Python のプロセスがいつまでも残ります**
        (「タブを閉じても終わらない」「stop.bat が効かない」の正体)。
        残った接続もここで閉じます。
        """
        with self._stop_lock:
            if self._stop_requested:
                log.debug("停止は要求済みです: mode=%s", self.mode)
                return
            self._stop_requested = True

        log.info("停止します: mode=%s", self.mode)
        server = self._server
        if server is None:
            self._stopped.set()
            return
        try:
            # **順序が要点**。いきなり `close()` すると、まさに処理中だった
            # 接続が閉じたあとのソケットに書きに行き、waitress が
            # 「Bad file descriptor」のトレースバックを吐く。
            # 終了ボタンを押すたびにログへ例外が残ると、本物の異常が
            # 埋もれる。先に処理スレッドを畳んで、返しかけの応答を
            # 出し切らせてからソケットを閉じる
            dispatcher = getattr(server, "task_dispatcher", None)
            if dispatcher is not None:
                dispatcher.shutdown(timeout=GRACEFUL_TIMEOUT_SEC)
            server.close()
            self._close_open_connections(server)
        except Exception as exc:                  # noqa: BLE001 - 停止は best effort
            log.warning("サーバの停止でエラー: %s", exc)
        self._stopped.set()

    @staticmethod
    def _close_open_connections(server) -> None:
        """つながったままの接続を閉じて、受付の輪を終わらせる。

        map が空にならないと `wasyncore.loop()` は返りません。ここを
        やらないとプロセスが残ります(上の説明のとおり)。
        """
        from waitress import wasyncore

        # 版によって属性名が違う(`_map` / `map`)。両方見る
        table = getattr(server, "_map", None)
        if table is None:
            table = getattr(server, "map", None)
        if not table:
            return
        left = len(table)
        wasyncore.close_all(table, ignore_all=True)
        log.info("残っていた接続を閉じました: %s本", left)

    def wait_stopped(self, timeout: Optional[float] = None) -> bool:
        return self._stopped.wait(timeout)

    @property
    def stop_requested(self) -> bool:
        """停止を頼まれたか。起動側が「待ちに上限を置く」判断に使う。"""
        return self._stop_requested

    # --------------------------------------------------------------
    # 段の知らせ方。**本体ができる前でも同じ呼び方で通る**ようにする
    # ── 呼ぶ側(`start_app`)が「いまどちらか」を気にすると、
    # 差し替えの前後で書き分けが要る
    # --------------------------------------------------------------
    def mark_ready(self, ready: bool = True, *, stage: str = "起動完了") -> None:
        """準備が終わったことを起動待機画面へ伝える。"""
        if self.app is None:
            # 本体がまだ無いのに準備完了はありえない(組み立てが先)
            log.warning("本体ができる前に準備完了が呼ばれました")
            return
        self.app.config["READY"] = ready
        self.app.config["STAGE"] = stage
        self.app.config["STAGE_KEY"] = "done" if ready else "prepare"
        log.info("準備状態: ready=%s stage=%s", ready, stage)

    def mark_stage(self, stage: str, key: str = "prepare") -> None:
        self.boot.mark_stage(stage, key)
        if self.app is not None:
            self.app.config["STAGE"] = stage
            self.app.config["STAGE_KEY"] = key

    def mark_error(self, message: str) -> None:
        """起動に失敗したことを画面へ伝える。

        サーバ自体は生かしておく。落としてしまうと、利用者のブラウザには
        「接続できません」としか出ず、理由が伝わらない。
        """
        self.boot.mark_error(message)
        if self.app is not None:
            self.app.config["STARTUP_ERROR"] = message
            self.app.config["STAGE"] = "起動に失敗しました"
        log.error("起動エラー: %s", message)


def run_in_background(server: AppServer) -> threading.Thread:
    """待ち受けを別スレッドで始める。呼び出し側は初期化を続けられる。"""
    thread = threading.Thread(target=server.serve_forever,
                              name=f"http-{server.mode}", daemon=True)
    thread.start()
    return thread


def wait_until_listening(port: int, *, timeout: float = 10.0) -> bool:
    """実際に待ち受けが始まるまで待つ。

    `create_server()` の時点で bind は済んでいるが、別スレッドで
    起動する場合は「まだ受け付けていない状態でブラウザを開く」ことが
    ありうる。基盤仕様書 2.3 の「応答を確認してから画面へ移る」に合わせ、
    ここで実際に応答するまで待つ。
    """
    return diagnose_listening(port, timeout=timeout).ok


class ListenCheck:
    """待ち受けの確認結果。失敗したときは**どこで**失敗したかを持つ。"""

    def __init__(self, ok: bool, tcp_ok: bool = False, hint: str = "") -> None:
        self.ok = ok
        self.tcp_ok = tcp_ok
        self.hint = hint


def diagnose_listening(port: int, *, timeout: float = 10.0) -> ListenCheck:
    """待ち受けを確かめ、駄目なら理由まで返す。

    「繋がらない」と「繋がるが応答が返らない」は原因も直し方も違う。
    前者はポートの取り合いやセキュリティ製品による遮断、後者は
    **プロキシ**やアプリ側の停止であることが多い。
    区別しないまま「サーバを起動できませんでした」とだけ出すと、
    現場では手の打ちようがない(実際にそうなった)。
    """
    import time

    import launch_guard

    deadline = time.monotonic() + timeout
    tcp_ok = False
    while time.monotonic() < deadline:
        if launch_guard.probe_health(port, timeout=0.5) is not None:
            return ListenCheck(True, tcp_ok=True)
        if not tcp_ok:
            tcp_ok = launch_guard.is_port_accepting(port)
        time.sleep(0.05)

    if not tcp_ok:
        return ListenCheck(False, tcp_ok=False, hint=(
            f"127.0.0.1:{port} に接続できません。\n"
            "  ・別のアプリが同じポートを使っていないか\n"
            "  ・セキュリティ製品がローカル通信を止めていないか\n"
            "  を確認してください。"))

    proxies = launch_guard.proxy_settings()
    hint = (f"127.0.0.1:{port} には繋がりますが、応答が返りません。\n")
    if proxies:
        hint += ("  プロキシ設定が有効です: "
                 + ", ".join(f"{k}={v}" for k, v in sorted(proxies.items()))
                 + "\n  自分自身への通信までプロキシへ送られている可能性があります。\n"
                 "  除外一覧に 127.0.0.1 と localhost を入れてください。")
    else:
        hint += "  セキュリティ製品がローカル通信を検査していないか確認してください。"
    log.error("待ち受けの確認に失敗: %s", hint)
    return ListenCheck(False, tcp_ok=True, hint=hint)

