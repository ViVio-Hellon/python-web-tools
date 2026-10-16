"""待機画面だけを出す、いちばん小さいサーバ (基盤仕様書 2.2 / 2.3)

【なぜ要るのか】
起動でいちばん時間を食うのは **Flask とアプリ本体の import** です。
そのあとに待機画面を出していたら、「待たせるために見せるもの」を
待たせていることになります ── 出す意味がありません。

そこで、待ち受けだけを先に始めます。ここが答えるのは2つだけ:

    GET /             待機画面(1往復で出る。外部への要求は無し)
    GET /api/health   まだ準備できていないことを、本体と同じ形で返す
    POST /api/shutdown 止める(トークン必須)。本体を組み立てられなかったときも
                      止められるように(下の【止める合図に答える】)

`packaging_tool.boot_screen` と `app_config` 以外は読み込みません
(Flask も、業務のモジュールも読みません)。

【止める合図に答える】
本体を組み立てられなかった(フォルダの一部が欠けた・設定が壊れた・部品が無い)ときは、
待機画面に理由を出したまま Python が残る。以前はここが `/api/shutdown` に 503 を返して
いたので、stop.bat は正常終了を頼めず、PID で落とす道しか無かった ── その道は
Windows で `wmic` が無いとコマンドラインを照合できず、`--force` でも止まらなかった
(業務ツール統合の all-tools で、Python が残ってフォルダを消せなくなった不具合と同じ形)。
待機画面のあいだは実行中の処理も保存していない図も無いので、トークンが合えばそのまま止める。

【本体との入れ替え】
`Handover` が WSGI の呼び先を1つ持ちます。本体ができたら
`install()` で差し替えるので、**待ち受けを開き直しません** ──
開き直すと、その瞬間にブラウザの問い合わせが落ちて、画面が
「接続できません」に見えます。
"""
from __future__ import annotations

import hmac
import json
import threading
from typing import Any, Callable, Optional

from packaging_tool import app_config, boot_screen
from packaging_tool.logging_utils import get_logger

log = get_logger("boot_server")

# 本体ができるまでのあいだ、業務の要求に返す答え。
# 404 にすると「無い」に見えるので、**まだであることを言う**
_NOT_READY = {"error": {"code": "starting",
                        "message": "起動しています。しばらくお待ちください。"}}

# 止める合図を受けてから止めるまでの猶予(秒)。応答を返しきってから止める(本体と同じ)
SHUTDOWN_DELAY_SEC = 0.4


class BootApp:
    """待機画面だけを出す WSGI アプリ。

    **状態は外から書き換えられる。** 段が進むたびに `stage` を差し替え、
    待機画面の問い合わせがそれを読む(本体の `AppServer.mark_stage` と
    同じ役目を、本体ができる前の時間帯だけ引き受ける)。
    """

    def __init__(self, mode: str, port: int, token: str) -> None:
        self.mode = mode
        self.port = port
        self.token = token
        self.stage = "アプリを準備中"
        self.stage_key = "prepare"
        self.startup_error = ""
        self._page: Optional[bytes] = None
        # 止め方。`server.AppServer` が差し込む(ここが waitress を知らないように)
        self.on_stop: Optional[Callable[[], None]] = None
        # デスクトップ版は外枠(exe)からしか要求が来ない(ポートを開かない)ので、トークンを見ない
        self.trusted = False

    # -- 状態 ---------------------------------------------------------
    def mark_stage(self, stage: str, key: str = "prepare") -> None:
        self.stage = stage
        self.stage_key = key

    def mark_error(self, message: str) -> None:
        self.startup_error = message

    # -- 中身 ---------------------------------------------------------
    def page(self) -> bytes:
        """待機画面。**1度組み立てて覚える**(毎回組むほどのものではない)。"""
        if self._page is None:
            self._page = boot_screen.render(
                display_name=app_config.display_name(),
                version_label=app_config.version_label(),
                token=self.token,
                app_id=app_config.app_id(),
                poll_ms=app_config.job_poll_ms(),
                # 本体がまだ無いので、待たずに入る道は「開き直す」だけ。
                # 押しても待機画面に戻るが、押せる道が消えるよりはよい
                home_url="/",
            ).encode("utf-8")
        return self._page

    def health(self) -> dict[str, Any]:
        """本体の `/api/health` と**同じ形**で返す。

        形が違うと、待機画面と `launch_guard` の両方が場合分けを持つ
        ことになる。まだ準備できていないことは `ready` が言う。
        """
        return {
            "app_id": app_config.app_id(),
            "display_name": app_config.display_name(),
            "version": app_config.version(),
            "mode": self.mode,
            "port": self.port,
            "pid": _pid(),
            "ready": False,
            "stage": self.stage,
            "stage_key": self.stage_key,
            "startup_error": self.startup_error,
            "uptime_sec": 0.0,
            "job": None,
        }

    # -- WSGI ---------------------------------------------------------
    def __call__(self, environ: dict, start_response: Callable) -> list:
        path = environ.get("PATH_INFO", "/")
        if path == "/api/health":
            return _json(start_response, self.health())
        if path == "/api/shutdown" and environ.get("REQUEST_METHOD") == "POST":
            return self.shutdown(environ, start_response)
        if path == "/" or path == "":
            body = self.page()
            start_response("200 OK", [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                # 待機画面は**残さない**。次に開いたときは本体が出る
                ("Cache-Control", "no-store"),
            ])
            return [body]
        # 業務の要求。まだ答えられないので、そう言う
        return _json(start_response, _NOT_READY, status="503 Service Unavailable")


    def shutdown(self, environ: dict, start_response: Callable) -> list:
        """止める(本体の `/api/shutdown` と同じ答えの形)。**トークンが合うときだけ。**"""
        if not self.trusted and not self._token_ok(environ):
            return _json(start_response, {"stopped": False, "error": {
                "code": "forbidden", "message": "トークンが合いません"}},
                status="403 Forbidden")
        if self.on_stop is None:
            return _json(start_response, {"stopped": False, "reason": "no_hook",
                                          "message": "このプロセスは停止操作に対応していません"},
                         status="501 Not Implemented")
        log.info("待機画面のまま止めます(%s)", self.startup_error or "起動の途中")
        threading.Timer(SHUTDOWN_DELAY_SEC, self.on_stop).start()
        return _json(start_response, {"stopped": True, "message": "終了します"})

    def _token_ok(self, environ: dict) -> bool:
        sent = environ.get("HTTP_X_TOOL_TOKEN", "")
        if not sent:
            from urllib.parse import parse_qs
            sent = (parse_qs(environ.get("QUERY_STRING", "")).get("t") or [""])[0]
        return bool(self.token) and hmac.compare_digest(sent.encode(), self.token.encode())


class Handover:
    """WSGI の呼び先を1つ持ち、あとから差し替えられるようにする。

    waitress には**これ**を渡す。待ち受けを開き直さずに本体へ移れる。
    """

    def __init__(self, first: Callable) -> None:
        self._app = first
        self._lock = threading.Lock()
        self.handed_over = False

    def install(self, app: Callable) -> None:
        with self._lock:
            self._app = app
            self.handed_over = True
        log.info("待ち受けを本体へ引き継ぎました")

    @property
    def current(self) -> Callable:
        with self._lock:
            return self._app

    def __call__(self, environ: dict, start_response: Callable):
        return self.current(environ, start_response)


def _json(start_response: Callable, body: dict, *, status: str = "200 OK") -> list:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(raw))),
        ("Cache-Control", "no-store"),
    ])
    return [raw]


def _pid() -> int:
    import os
    return os.getpid()
