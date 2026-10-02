"""デスクトップ版の入口(`bridge.py`)── ポートを使わずに画面を返す

外枠(Rust/Tauri)は `bridge.py` を子として起動し、標準入出力で要求を渡す。
ここでは外枠の代わりをして、次を確かめる:

- やりとりの形(見出し1行 + 本文)が往復で崩れない(日本語・バイナリ・大きい本文)
- **ソケットを1つも開かない**(開こうとしたら落ちるようにして起動する)
- 待機画面 → 準備完了 → 画面・操作が返る
- トークン無し・知らない宛先は今までどおり断る
- 「終了」で `quit` を知らせ、標準入力を閉じればプロセスが終わる
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False


class FrameTests(unittest.TestCase):
    def roundtrip(self, head: dict, body: bytes) -> tuple[dict, bytes]:
        out = io.BytesIO()
        bridge.FrameWriter(out).write(head, body)
        out.seek(0)
        return bridge.read_frame(out)

    def test_日本語とバイナリの本文が崩れない(self) -> None:
        body = "配置図\n".encode("utf-8") + bytes(range(256))
        head, got = self.roundtrip({"id": 3, "status": 200, "headers": [["X", "あ"]]}, body)
        self.assertEqual(got, body)
        self.assertEqual(head["len"], len(body))
        self.assertEqual(head["headers"], [["X", "あ"]])

    def test_大きい本文(self) -> None:
        body = os.urandom(3 * 1024 * 1024)
        self.assertEqual(self.roundtrip({"id": 1}, body)[1], body)

    def test_知らせは本文なし(self) -> None:
        out = io.BytesIO()
        bridge.FrameWriter(out).event("quit")
        self.assertEqual(json.loads(out.getvalue()), {"event": "quit"})

    def test_終わりはNone_途中で切れたら例外(self) -> None:
        self.assertIsNone(bridge.read_frame(io.BytesIO(b"")))
        with self.assertRaises(EOFError):
            bridge.read_frame(io.BytesIO(b'{"id": 1, "len": 10}\nabc'))
        with self.assertRaises(ValueError):
            bridge.read_frame(io.BytesIO(b'{"id": 1, "len": -1}\n'))


@unittest.skipUnless(HAS_WEB, "flask が無い")
class CallWsgiTests(unittest.TestCase):
    def test_方法_経路_問い合わせ_本文_見出しが届き応答が返る(self) -> None:
        app = flask.Flask("試し")

        @app.post("/api/echo")
        def echo():                                      # noqa: ANN202
            return flask.jsonify({"q": flask.request.args.get("a"),
                                  "body": flask.request.get_json(),
                                  "host": flask.request.host,
                                  "token": flask.request.headers.get("X-Tool-Token")})

        status, headers, data = bridge.call_wsgi(app, {
            "method": "POST", "path": "/api/echo", "query": "a=%E3%81%82",
            "headers": [["Content-Type", "application/json"], ["X-Tool-Token", "t"],
                        ["Host", "app.localhost"]]},
            json.dumps({"幅": 1100}).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertIn(["Content-Type", "application/json"], headers)
        self.assertEqual(json.loads(data), {"q": "あ", "body": {"幅": 1100},
                                            "host": "app.localhost", "token": "t"})


@unittest.skipUnless(HAS_WEB, "flask が無い")
class BridgeProcessTests(unittest.TestCase):
    """本物の子プロセスとして起動し、外枠の代わりに要求を投げる。"""

    TOKEN = "desk-test-token"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "nosock").mkdir()
        # **ソケットを開こうとしたら落ちる**ようにして起動する
        (root / "nosock" / "sitecustomize.py").write_text(
            "import socket\n"
            "class _No(socket.socket):\n"
            "    def __init__(self, *a, **k):\n"
            "        raise RuntimeError('ソケットを開こうとしました')\n"
            "socket.socket = _No\n", encoding="utf-8")
        config = root / "user_config.json"
        # 取り込み元は無い(起動時の自動取り込みは切る)。画面が返ることを見る
        config.write_text(json.dumps({"auto_import_on_start": False}), encoding="utf-8")
        env = dict(os.environ,
                   PACKAGING_TOOL_DB_PATH=str(root / "db" / "packaging_tool.db"),
                   PACKAGING_TOOL_CONFIG_PATH=str(config),
                   PACKAGING_TOOL_LOG_DIR=str(root / "logs"),
                   PACKAGING_TOOL_LOCAL_DIR=str(root / "local"),
                   PACKAGING_TOOL_MODE="field",
                   PACKAGING_TOOL_TOKEN=self.TOKEN,
                   PYTHONPATH=str(root / "nosock"))
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bridge.py")], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._kill)
        self.err: list[str] = []
        threading.Thread(target=lambda: self.err.extend(
            self.proc.stderr.read().decode("utf-8", "replace").splitlines()),
            daemon=True).start()
        self.events: list[dict] = []
        self.replies: dict[int, tuple[dict, bytes]] = {}
        self.lock = threading.Lock()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    def _read(self) -> None:
        while True:
            try:
                frame = bridge.read_frame(self.proc.stdout)
            except (ValueError, EOFError, OSError):
                return
            if frame is None:
                return
            head, body = frame
            if "event" in head:
                self.events.append(head)
            else:
                self.replies[head["id"]] = (head, body)

    def request(self, method: str, path: str, *, body: bytes = b"",
                headers=None, token: bool = True, host: str = "app.localhost",
                timeout: float = 30.0) -> tuple[dict, bytes]:
        with self.lock:
            self.next_id += 1
            i = self.next_id
            h = [["Host", host]] + ([["X-Tool-Token", self.TOKEN]] if token else [])
            h += headers or []
            head = {"id": i, "method": method, "path": path, "headers": h, "len": len(body)}
            self.proc.stdin.write(json.dumps(head).encode("utf-8") + b"\n" + body)
            self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while i not in self.replies:
            if time.monotonic() > deadline:
                self.fail(f"{path} の応答が来ない。stderr: {self.err[-15:]}")
            time.sleep(0.01)
        return self.replies.pop(i)

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            head, body = self.request("GET", "/api/health")
            if head["status"] == 200 and json.loads(body)["ready"]:
                return
            time.sleep(0.1)
        self.fail(f"準備完了にならない。stderr: {self.err[-15:]}")

    def test_ポート無しで起動し画面を返し_終了で知らせて終わる(self) -> None:
        deadline = time.monotonic() + 30
        while not self.events and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.events[:1], [{"event": "started"}], self.err[-15:])
        self.wait_ready()

        head, body = self.request("GET", "/")
        self.assertEqual(head["status"], 302)
        for path in ("/lot", "/selection", "/settings", "/inventory", "/warehouse",
                     "/static/js/app.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0]["status"], 200)

        # 同時に来ても取り違えない(画面は見張り・心拍・操作を同時に出す)
        results: list[int] = []
        threads = [threading.Thread(target=lambda: results.append(
            self.request("GET", "/api/jobs")[0]["status"])) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results, [200] * 12)

        # 守りは今までどおり
        self.assertEqual(self.request("GET", "/api/jobs", token=False)[0]["status"], 401)
        self.assertEqual(self.request("GET", "/api/jobs", host="evil.example")[0]["status"], 400)

        head, body = self.request("POST", "/api/shutdown", body=b"{}",
                                  headers=[["Content-Type", "application/json"]])
        self.assertEqual(head["status"], 200, body)
        deadline = time.monotonic() + 10
        while {"event": "quit"} not in self.events and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn({"event": "quit"}, self.events)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=20), 0)
        self.assertFalse([line for line in self.err if "ソケットを開こうとしました" in line],
                         self.err[-20:])

    def test_外枠が先に閉じたら終わる(self) -> None:
        deadline = time.monotonic() + 30
        while not self.events and time.monotonic() < deadline:
            time.sleep(0.01)
        self.wait_ready()
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=20), 0)


@unittest.skipUnless(HAS_WEB, "flask が無い")
class FatalTests(unittest.TestCase):
    def test_起動できないときは理由を知らせる(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "ファイル"
            blocker.write_text("x", encoding="utf-8")
            env = dict(os.environ, PACKAGING_TOOL_LOCAL_DIR=str(blocker),  # 作業用フォルダを作れない
                       PACKAGING_TOOL_CONFIG_PATH=str(Path(tmp) / "c.json"),
                       PACKAGING_TOOL_LOG_DIR=str(Path(tmp) / "logs"))
            done = subprocess.run([sys.executable, str(ROOT / "bridge.py")], env=env,
                                  input=b"", capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 1, done.stderr[-500:])
        event = json.loads(done.stdout.decode("utf-8").splitlines()[0])
        self.assertEqual(event["event"], "fatal")
        self.assertIn("作業用フォルダ", event["message"])


class VersionTests(unittest.TestCase):
    def test_外枠の版はアプリの版と同じ(self) -> None:
        """exe のプロパティに出る版と、画面の帯に出る版を食い違わせない。"""
        import re
        app = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["version"]
        conf = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        cargo = re.search(r'^version = "([^"]+)"', (ROOT / "src-tauri" / "Cargo.toml")
                          .read_text(encoding="utf-8"), re.M).group(1)
        self.assertEqual((conf["version"], cargo), (app, app))

    def test_画面の宛先の名前は外枠と同じ(self) -> None:
        from packaging_tool import app_config
        main_rs = (ROOT / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        self.assertIn('const SCHEME: &str = "app";', main_rs)
        self.assertIn("app.localhost", app_config.BRIDGE_HOSTS)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
