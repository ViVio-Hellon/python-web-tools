"""exe起動(デスクトップ版 = bridge.py)とvbs起動(ブラウザ版 = start_app.py)を混ぜて、共有フォルダ越しに動かす点検。

現場の声:「倉庫モードはexe起動、現場はvbs起動をしてた場合や、違うラインでvbs起動、exe起動していた場合でのテスト」。

端末ごとに 端末名・ローカル領域・手元DB・設定・ログ を分け、梱包資材マスタの共有フォルダだけ同じにして、
本物の起動の入口から立ち上げる(1台 = 1プロセス)。見るもの:

  1) 別々の端末: 倉庫=exe・作業長=vbs・L-1=vbs・HVC=exe
     起動したモード / 発注 → 倉庫が確認 → 作業長に確認済み / 予定ロットを各ラインへ → 各ラインで作業済み →
     作業長の「作業予定ライン」に作業済み
  2) 同じ端末で exe(資材)と vbs(現場)を同時に起動: 送る → 同じ端末の exe で確認 → vbs に確認済み、共有に2重に送らない
  3) 倉庫の端末を起動し直しても資材モード / 同じ端末で exe を閉じても vbs は続く

使い方(1〜2分。ポート 8713〜 を使う):
    python scripts/mixed_launch_check.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402
from packaging_tool import config  # noqa: E402
from tests.test_warehouse_handoff import make_shared  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="mixed_"))
SHARED = WORK / "共有"
SHARED.mkdir()
SRC = SHARED / config.MATERIAL_DB_NAME
make_shared(str(SRC))
with sqlite3.connect(SRC) as c:
    c.execute('CREATE TABLE "アクセス権限" (ID INTEGER PRIMARY KEY, ログインID TEXT, PC名 TEXT,'
              ' 権限 TEXT, 有効 INTEGER, 備考 TEXT)')
    for pc, perm in (("PC-SOKO", "mode:material"), ("PC-CHO", "mode:field、作業長"),
                     ("PC-L1", "mode:field、L-1"), ("PC-HVC", "mode:field、HVC"),
                     ("PC-MIX", "mode:field、mode:material、作業長")):
        c.execute('INSERT INTO "アクセス権限" (ログインID, PC名, 権限, 有効, 備考) VALUES ("", ?, ?, 1, "")',
                  (pc, perm))

NG: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(f"{'OK' if ok else 'NG'} {label}" + (f"  {extra}" if extra else ""), flush=True)
    if not ok:
        NG.append(label)


class Terminal:
    """1台の端末(1つのプロセス)。kind = exe / vbs。"""

    def __init__(self, pc: str, kind: str, home: str | None = None, mode: str = "") -> None:
        self.pc, self.kind = pc, kind
        home_dir = WORK / (home or pc)
        (home_dir / "nowhere").mkdir(parents=True, exist_ok=True)
        cfg = home_dir / "user_config.json"
        if not cfg.exists():
            cfg.write_text(json.dumps({"auto_import_on_start": True}), encoding="utf-8")
        env = dict(os.environ,
                   PACKAGING_TOOL_PC_NAME=pc, PACKAGING_TOOL_LOGIN_ID="worker",
                   PACKAGING_TOOL_LOCAL_DIR=str(home_dir / "local"),
                   PACKAGING_TOOL_DB_PATH=str(home_dir / "db" / "packaging_tool.db"),
                   PACKAGING_TOOL_CONFIG_PATH=str(cfg),
                   PACKAGING_TOOL_LOG_DIR=str(home_dir / "logs"),
                   PACKAGING_TOOL_MASTER_DB_DIR=str(SHARED),
                   PACKAGING_TOOL_LOT_DB_DIR=str(home_dir / "nowhere"),
                   PACKAGING_TOOL_KANBAN_DB_DIR=str(home_dir / "nowhere"),
                   PACKAGING_TOOL_THRESHOLD_DB_DIR=str(home_dir / "nowhere"),
                   PACKAGING_TOOL_ACCDB_DIR=str(home_dir / "nowhere"))
        self.home = home_dir
        self.err: list[str] = []
        if kind == "exe":
            env["PACKAGING_TOOL_TOKEN"] = self.token = f"tok-{pc}"
            if mode:
                env["PACKAGING_TOOL_MODE"] = mode
            self.proc = subprocess.Popen([sys.executable, str(ROOT / "bridge.py")], env=env,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
            self.replies: dict[int, tuple] = {}
            self.events: list[dict] = []
            self.next_id = 0
            self.lock = threading.Lock()
            threading.Thread(target=self._read, daemon=True).start()
        else:
            args = [sys.executable, str(ROOT / "start_app.py"), "--no-browser"]
            if mode:
                args += ["--mode", mode]
            self.proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        threading.Thread(target=lambda: self.err.extend(
            self.proc.stderr.read().decode("utf-8", "replace").splitlines()), daemon=True).start()
        self._wait_ready()

    # --- exe(標準入出力) ---
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

    def _exe(self, method: str, path: str, body: bytes) -> tuple[int, bytes]:
        with self.lock:
            self.next_id += 1
            i = self.next_id
            path_only, _, query = path.partition("?")
            head = {"id": i, "method": method, "path": path_only, "query": query,
                    "headers": [["Host", "app.localhost"], ["X-Tool-Token", self.token],
                                ["Content-Type", "application/json"], ["X-Screen-Id", "scr"]], "len": len(body)}
            self.proc.stdin.write(json.dumps(head).encode("utf-8") + b"\n" + body)
            self.proc.stdin.flush()
        deadline = time.monotonic() + 60
        while i not in self.replies:
            if time.monotonic() > deadline or self.proc.poll() is not None:
                raise RuntimeError(f"{self.pc}: {path} の応答が来ない {self.err[-10:]}")
            time.sleep(0.01)
        head, data = self.replies.pop(i)
        return head["status"], data

    # --- vbs(ブラウザと同じ HTTP) ---
    def _vbs_info(self) -> dict | None:
        for p in (self.home / "local" / "runtime").glob("*.lock"):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return None

    def _vbs(self, method: str, path: str, body: bytes) -> tuple[int, bytes]:
        info = self._vbs_info()
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", data=body if method == "POST" else None,
                                     method=method, headers={"X-Tool-Token": info["token"], "X-Screen-Id": "scr",
                                                             "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def call(self, method: str, path: str, data: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(data or {}).encode("utf-8") if method == "POST" else b""
        status, raw = (self._exe if self.kind == "exe" else self._vbs)(method, path, body)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {"_raw": raw[:200].decode("utf-8", "replace")}

    def get(self, path: str) -> dict:
        status, body = self.call("GET", path)
        if status != 200:
            raise RuntimeError(f"{self.pc}: GET {path} → {status} {body}")
        return body

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.pc} ({self.kind}) が終わった: {self.err[-15:]}")
            try:
                if self.kind == "vbs" and not self._vbs_info():
                    raise RuntimeError("まだ")
                if self.call("GET", "/api/health")[1].get("ready"):
                    return
            except Exception:                           # noqa: BLE001
                pass
            time.sleep(0.3)
        raise RuntimeError(f"{self.pc} ({self.kind}) が準備完了にならない: {self.err[-15:]}")

    @property
    def mode(self) -> str:
        return self.get("/api/health")["mode"]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def wait_for(label: str, fn, timeout: float = 40.0):
    """共有へ届く(書き戻しは背景)のを待つ。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.5)
    return last


ORDER = {"hinmei": "一般　P1　1100x1300", "hatchu_code": "PL-001", "tani": "台", "zaisitu": "A5052",
         "choshitu": "H32", "atu": "3.0", "haba": "1100", "take": "1300", "yoto_code": "K100",
         "nounyusaki": "A社", "hatchu_suu": "4"}


def order_row(t: Terminal, lot: str) -> dict | None:
    _s, body = t.call("POST", "/api/warehouse/refresh")
    return next((r for r in body.get("rows", []) if r.get("lot_no") == lot), None)


def beat_then(t: Terminal, path: str) -> dict:
    """画面と同じく心拍を打ってから(予定ロットは心拍のついでに背景で取り込む)引く。"""
    t.call("GET", "/api/health?beat=1")
    return t.get(path)


def plans(t: Terminal) -> list[str]:
    return sorted(i["lot_no"] for i in t.get("/api/lot/plans?refresh=1")["items"])


terminals: list[Terminal] = []
try:
    print(f"作業フォルダ: {WORK}")
    print("\n=== 1) 別々の端末: 倉庫=exe・作業長=vbs・L-1=vbs・HVC=exe ===")
    soko = Terminal("PC-SOKO", "exe"); terminals.append(soko)
    cho = Terminal("PC-CHO", "vbs"); terminals.append(cho)
    l1 = Terminal("PC-L1", "vbs"); terminals.append(l1)
    hvc = Terminal("PC-HVC", "exe"); terminals.append(hvc)
    for t, want in ((soko, "material"), (cho, "field"), (l1, "field"), (hvc, "field")):
        check(f"1 {t.pc}({t.kind}) は {want} モードで開く", t.mode == want, t.mode)

    # 作業長(vbs)が発注 → 倉庫(exe)が確認 → 作業長に確認済み
    s, r = cho.call("POST", "/api/warehouse/send", {**ORDER, "lot_no": "7001001"})
    check("1 作業長(vbs)が倉庫へ送信できる", s == 200, f"{s} {r.get('message') or r.get('error')}")
    row = wait_for("倉庫に届く", lambda: order_row(soko, "7001001"))
    check("1 倉庫(exe)に発注が届く", bool(row))
    if row:
        s, r = soko.call("POST", "/api/warehouse/confirm", {"mgr_no": row["mgr_no"]})
        check("1 倉庫(exe)が確認できる", s == 200, f"{s} {r.get('message') or r.get('error')}")
        got = wait_for("作業長に確認済み",
                       lambda: (lambda x: x if x and "確認" in x["status"] and "未" not in x["status"] else None)(
                           order_row(cho, "7001001")))
        check("1 作業長(vbs)に確認済みが届く", bool(got), str(got and got["status"]))

    # 作業長(vbs)が予定ロットを L-1(vbs)と HVC(exe)へ → 各ラインで作業済み
    for lot, line in (("8001001", "L-1"), ("8001002", "L-1"), ("8001003", "HVC")):
        s, r = cho.call("POST", "/api/warehouse/lot-plan", {"lot_no": lot, "line": line})
        check(f"1 作業長が {lot} を {line} に入れる", s == 200, f"{s} {r.get('message') or r.get('error')}")
    got = wait_for("L-1 に届く", lambda: plans(l1) == ["8001001", "8001002"])
    check("1 L-1(vbs)に2件届く", bool(got), str(plans(l1)))
    got = wait_for("HVC に届く", lambda: plans(hvc) == ["8001003"])
    check("1 HVC(exe)に1件届く", bool(got), str(plans(hvc)))
    for t, lot in ((l1, "8001001"), (hvc, "8001003")):
        item = next(i for i in t.get("/api/lot/plans")["items"] if i["lot_no"] == lot)
        s, r = t.call("POST", f"/api/lot/plans/{item['plan_id']}/done")
        check(f"1 {t.pc}({t.kind})が {lot} を作業済み", s == 200, f"{s} {r.get('message') or r.get('error')}")
    got = wait_for("作業長に作業済み", lambda: (beat_then(cho, "/api/warehouse/lot-plan?lot_no=8001003").get("done") or {}).get("terminal"))
    check("1 作業長(vbs)に HVC(exe)の作業済みが届く", bool(got), str(got))
    got = wait_for("作業長に作業済み L-1", lambda: (beat_then(cho, "/api/warehouse/lot-plan?lot_no=8001001").get("done") or {}).get("terminal"))
    check("1 作業長(vbs)に L-1(vbs)の作業済みが届く", bool(got), str(got))
    check("1 L-1 の欄に残りの1件", plans(l1) == ["8001002"], str(plans(l1)))
    check("1 HVC の欄は空", plans(hvc) == [], str(plans(hvc)))

    print("\n=== 2) 同じ端末で exe(資材)と vbs(現場)を同時に起動 ===")
    mix_exe = Terminal("PC-MIX", "exe", home="PC-MIX", mode="material"); terminals.append(mix_exe)
    mix_vbs = Terminal("PC-MIX", "vbs", home="PC-MIX", mode="field"); terminals.append(mix_vbs)
    check("2 同じ端末の exe は資材モード", mix_exe.mode == "material", mix_exe.mode)
    check("2 同じ端末の vbs は現場モード", mix_vbs.mode == "field", mix_vbs.mode)
    s, r = mix_vbs.call("POST", "/api/warehouse/send", {**ORDER, "lot_no": "7002001"})
    check("2 vbs(現場)が送信できる", s == 200, f"{s} {r.get('message') or r.get('error')}")
    row = wait_for("同じ端末の exe に届く", lambda: order_row(mix_exe, "7002001"))
    check("2 同じ端末の exe(資材)に届く", bool(row))
    if row:
        s, r = mix_exe.call("POST", "/api/warehouse/confirm", {"mgr_no": row["mgr_no"]})
        check("2 exe(資材)が確認できる", s == 200, f"{s} {r.get('message') or r.get('error')}")
    row = wait_for("ほかの倉庫にも届く", lambda: order_row(soko, "7002001"))
    check("2 ほかの端末の倉庫(exe)にも届く", bool(row))
    got = wait_for("vbs に確認済み", lambda: (lambda x: x if x and "未" not in x["status"] and "確認" in x["status"] else None)(order_row(mix_vbs, "7002001")))
    check("2 同じ端末の vbs(現場)に確認済みが届く", bool(got), str(got and got["status"]))
    for t in (mix_exe, mix_vbs):
        check(f"2 {t.kind} は落ちていない", t.proc.poll() is None)
    time.sleep(3)
    with sqlite3.connect(SRC) as c:
        for lot in ("7001001", "7002001"):
            n = c.execute(f'SELECT COUNT(*) FROM "{config.TBL_WAREHOUSE_ORDER}" WHERE LotNo = ?', (lot,)).fetchone()[0]
            check(f"2 共有の発注 {lot} は1行(2重に送らない)", n == 1, f"{n}行")
        n = c.execute(f'SELECT COUNT(*) FROM "{config.TBL_LOT_PLAN}"').fetchone()[0]
        check("2 共有の予定ロットの行数(入れる3 + 作業済み2)", n == 5, f"{n}行")

    print("\n=== 3) 倉庫の端末を起動し直しても資材モード / 同じ端末で exe を閉じても vbs は続く ===")
    soko.stop(); terminals.remove(soko)
    soko2 = Terminal("PC-SOKO", "exe"); terminals.append(soko2)
    check("3 起動し直した倉庫(exe)は資材モード", soko2.mode == "material", soko2.mode)
    mix_exe.stop(); terminals.remove(mix_exe)
    s, r = mix_vbs.call("POST", "/api/warehouse/send", {**ORDER, "lot_no": "7003001"})
    check("3 同じ端末の exe を閉じても vbs から送れる", s == 200, f"{s}")
    row = wait_for("倉庫に届く", lambda: order_row(soko2, "7003001"))
    check("3 起動し直した倉庫(exe)に届く", bool(row))
finally:
    for t in terminals:
        t.stop()
    print("\n結果:", "すべてOK" if not NG else f"NG {len(NG)}件: {NG}")
    if NG:
        for t in terminals:
            print(f"--- {t.pc}({t.kind}) stderr 末尾")
            print("\n".join(t.err[-8:]))
    sys.exit(1 if NG else 0)
