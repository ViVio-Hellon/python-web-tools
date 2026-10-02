"""デスクトップ版(Tauri)からの入口 ── **ポートを使わない**

ブラウザ版(`Start.vbs` → `start_app.py`)は、127.0.0.1 のポートで待ち受けて
ブラウザから繋いでもらっていた。デスクトップ版では、窓を持つ外枠
(Rust/Tauri、`src-tauri/`)がこのプロセスを子として起動し、**標準入出力**で
要求を渡す。ソケットは1つも開かない。

    外枠(Rust) ──標準入力──▶ bridge.py ──▶ Flask アプリ(WSGI として呼ぶだけ)
               ◀──標準出力──

Flask は「URL を関数に振り分ける部品」として使い続ける(画面 137 経路と
試験 3,000 件余りがその形で書かれている)。待ち受け(waitress)は使わない。

【やりとりの形】1件 = 見出し1行(JSON)+ 本文(見出しの `len` バイト)

    要求  {"id": 7, "method": "POST", "path": "/api/x", "query": "a=1",
           "headers": [["Content-Type", "application/json"], ...], "len": 12}\\n<本文>
    応答  {"id": 7, "status": 200, "headers": [[...], ...], "len": 345}\\n<本文>
    知らせ {"event": "quit"}\\n                     (`len` なし。本文なし)

本文を JSON に埋めない(base64 にしない)のは、資材選択の応答が 1 回 400KB を
超えるため。見出しと生のバイト列を分ければ、余計な変換が要らない。

【知らせ】
    started  … 待ち受けを始めた(待機画面を出せる)
    quit     … 終了してよい(画面の「終了」・閉じる確認を通った)
    fatal    … 起動できない(`message` `hint` `log_dir`)。外枠が理由を画面に出す

【標準出力を守る】
やりとりに使う標準出力へ、ほかの誰かが1文字でも書くと、以降すべてずれる。
そこで**最初に**本物の標準出力を別に取っておき、ファイル記述子 1 は標準エラーへ
付け替える(`print` も C 拡張の出力も標準エラーへ行く)。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))

# 同時に捌く要求の数。画面は「操作 + 進捗の見張り + 心拍 + 静的ファイル」を
# 同時に投げる。書く要求はアプリ側の錠が1つずつ通す(`app._WRITE_LOCK`)
WORKERS = 8

# 1件の本文の上限(壊れた見出しで巨大な読み込みをしないため)。
# 「表を持ってくる」の Access ファイルが数十MBになることがある
MAX_BODY = 512 * 1024 * 1024

# 外枠がデスクトップ版の画面を読み込む宛先のホスト名(`app_config.BRIDGE_HOSTS`)
DEFAULT_HOST = "app.localhost"


# ==================================================================
# やりとりの形
# ==================================================================
def read_frame(stream: BinaryIO) -> Optional[tuple[dict, bytes]]:
    """1件読む。終わり(相手が閉じた)なら `None`。"""
    line = stream.readline()
    if not line:
        return None
    head = json.loads(line.decode("utf-8"))
    size = int(head.get("len", 0) or 0)
    if size < 0 or size > MAX_BODY:
        raise ValueError(f"本文の長さが不正です: {size}")
    body = b""
    while len(body) < size:
        chunk = stream.read(size - len(body))
        if not chunk:
            raise EOFError("本文の途中で終わりました")
        body += chunk
    return head, body


class FrameWriter:
    """応答と知らせを書く。**1件ずつ丸ごと**書く(複数のスレッドから来る)。"""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, head: dict, body: bytes = b"") -> None:
        head = dict(head)
        if body or "id" in head:
            head["len"] = len(body)
        data = json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n" + body
        with self._lock:
            self._stream.write(data)
            self._stream.flush()

    def event(self, name: str, **fields: Any) -> None:
        self.write({"event": name, **fields})


def call_wsgi(app: Callable, head: dict, body: bytes) -> tuple[int, list, bytes]:
    """要求1件を WSGI アプリに渡して、応答を丸ごと返す。"""
    import io

    from werkzeug.datastructures import Headers
    from werkzeug.test import EnvironBuilder

    headers = Headers([(str(k), str(v)) for k, v in head.get("headers", [])])
    host = headers.get("Host") or DEFAULT_HOST
    builder = EnvironBuilder(
        path=head.get("path") or "/",
        method=(head.get("method") or "GET").upper(),
        query_string=head.get("query") or "",
        headers=headers,
        base_url=f"http://{host}",
        input_stream=io.BytesIO(body),
        content_length=len(body),
    )
    try:
        environ = builder.get_environ()
    finally:
        builder.close()
    environ["REMOTE_ADDR"] = "127.0.0.1"

    captured: dict[str, Any] = {}

    def start_response(status: str, response_headers: list, exc_info=None):
        captured["status"] = int(status.split(" ", 1)[0])
        captured["headers"] = [[k, v] for k, v in response_headers]
        return lambda data: captured.setdefault("written", []).append(data)

    result = app(environ, start_response)
    try:
        chunks = list(captured.pop("written", [])) + [c for c in result]
    finally:
        close = getattr(result, "close", None)
        if close is not None:
            close()
    return captured.get("status", 500), captured.get("headers", []), b"".join(chunks)


# ==================================================================
# サーバ(待ち受けの代わり)
# ==================================================================
def _protect_stdout() -> BinaryIO:
    """本物の標準出力を取っておき、記述子 1 は標準エラーへ向ける。"""
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    return proto


def make_server_class():
    """`server.AppServer` を受け継ぐ。**段・準備完了・失敗の伝え方は共通**。

    違うのは「どう待ち受けて、どう止めるか」だけ。ここで遅れて読むのは、
    `server` が `packaging_tool` を読むので、標準出力を守ったあとにしたいから。
    """
    from server import AppServer

    class BridgeServer(AppServer):
        def __init__(self, mode: str, token: str, writer: FrameWriter,
                     reader: BinaryIO) -> None:
            # **受け付けを始める前に、この(主)スレッドで読み込み終えておく。**
            # 1件目の要求は本体(Flask)を組み立てている最中に届く。2つの
            # スレッドが werkzeug を同時に読み始めると、片方が読みかけの
            # モジュールを掴んで「cannot import name ... from partially
            # initialized module」で落ちる(試しの起動で両方向とも起きた)
            import werkzeug  # noqa: F401
            import werkzeug.datastructures  # noqa: F401
            import werkzeug.test  # noqa: F401
            super().__init__(mode, 0, token=token)
            self.writer = writer
            self.reader = reader
            self.url = f"http://{DEFAULT_HOST}/"

        def build(self) -> None:
            super().build()
            # 画面は外枠の窓から来る。Host の確認(DNS リバインディング対策)は
            # TCP の待ち受けのためのものなので、外枠の宛先名を足すだけにする
            self.app.config["BRIDGE"] = True

        def serve_forever(self) -> None:
            """標準入力から要求を読み、別スレッドで答える。相手が閉じたら終わる。"""
            from packaging_tool.logging_utils import get_logger
            log = get_logger("bridge")
            pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="bridge")

            def answer(head: dict, body: bytes) -> None:
                try:
                    status, headers, data = call_wsgi(self.wsgi, head, body)
                except Exception as exc:          # noqa: BLE001 - 1件の失敗で止めない
                    log.exception("要求を処理できませんでした: %s", head.get("path"))
                    status, headers = 500, [["Content-Type", "text/plain; charset=utf-8"]]
                    data = f"内部エラー: {exc}".encode("utf-8")
                try:
                    self.writer.write({"id": head.get("id"), "status": status,
                                       "headers": headers}, data)
                except (BrokenPipeError, OSError):
                    pass                          # 外枠が先に終わった

            log.info("外枠からの要求を受け付けます(ポートは使いません)")
            self.writer.event("started")
            try:
                while not self._stop_requested:
                    try:
                        frame = read_frame(self.reader)
                    except (ValueError, EOFError) as exc:
                        log.error("要求を読めませんでした: %s", exc)
                        break
                    if frame is None:
                        log.info("外枠が閉じました")
                        break
                    pool.submit(answer, *frame)
            finally:
                pool.shutdown(wait=True, cancel_futures=False)
                self._stop_requested = True
                self._stopped.set()

        def stop(self) -> None:
            """終了してよいことを外枠へ知らせる。外枠が標準入力を閉じて終わる。"""
            with self._stop_lock:
                if self._stop_requested:
                    return
                self._stop_requested = True
            try:
                self.writer.event("quit")
            except (BrokenPipeError, OSError):
                pass
            self._stopped.set()

    return BridgeServer


# ==================================================================
# 入口
# ==================================================================
def main(argv: Optional[list[str]] = None) -> int:
    proto = _protect_stdout()
    writer = FrameWriter(proto)
    reader = sys.stdin.buffer

    import start_app

    def fatal(error: "start_app.StartupError") -> int:
        try:
            from packaging_tool import config
            log_dir = str(config.log_dir())
        except Exception:                         # noqa: BLE001 - 失敗の報告で失敗しない
            log_dir = ""
        try:
            start_app.log().error("起動に失敗: %s / %s", error, error.hint)
        except Exception:                         # noqa: BLE001
            pass
        writer.event("fatal", message=str(error), hint=error.hint, log_dir=log_dir)
        return 1

    try:
        start_app.run_environment_checks(bridge=True)
        return start_app.start_bridge(
            os.environ.get("PACKAGING_TOOL_MODE", start_app.AUTO),
            token=os.environ.get("PACKAGING_TOOL_TOKEN", ""),
            server_factory=lambda mode, token: make_server_class()(
                mode, token, writer, reader))
    except start_app.StartupError as exc:
        return fatal(exc)
    except Exception as exc:                      # noqa: BLE001 - 理由を外枠へ渡す
        return fatal(start_app.StartupError(f"起動中に思わぬエラー: {exc}",
                                            "ログを確認してください。"))


if __name__ == "__main__":
    raise SystemExit(main())
