"""管理者以外停止モード ── 管理者の PC から、ほかの PC のツールを止める(VER4.8.129)

現場の声:「ランチャーを介さず、アクセス権限・管理者の条件がそろっている PC から、他の PC を停止したい」。
決めたこと(現場と相談して):

    お願いを出せる      管理者パスワード **かつ** この端末の権限に Administrator
    止める範囲          お願いを出した PC 以外の、起動中の PC
    保存していない図・   画面に「管理者が更新のため停止を求めています」と出して利用者に任せる。
    処理中               保存して何も残っていなければ自動で止まる。**5分たっても止まらなければ強制**で止める
                        (保存していない図は控えに残し、次の起動で戻す)
    更新中               お願いが出ているあいだ、お願いを出した PC 以外では**起動させない**
    ランチャー           「全 PC で止める」は据え置き。ただし**同時には使わない**
                        (お願いが出ているあいだ stop.bat は断る)

【どこに置くか】
共有の梱包資材マスタのフォルダ(`config.master_db_dir()`。配布設定で全 PC が同じ場所を見る)の
`管理者以外停止\\` に、小さな JSON を置く。共有の SQLite には書かない(錠の取り合いをしない)。

    管理者以外停止\\お願い.json        管理者の PC だけが書く。{"active": true/false, …}
    管理者以外停止\\稼働中\\<PC>__<ID>__<pid>.json
                                     動いている各プロセスが**自分のぶんだけ**書く(1分ごとの知らせと、
                                     お願いへの答え)。管理者の画面はこれを並べて「どの PC が止まったか」を見る

**丸ごと書いてから入れ替える**(書きかけを読ませない)。
**読めないときは「お願いは無い」にしない**(VER4.8.122 の決まり): 見張りは前の判断のまま、起動は止めずに記録だけ残す。

【止め方】
`/api/shutdown`・stop.bat と同じ道(`srv.stop`)。ブラウザ版も、デスクトップ版(窓ごと閉じる)も同じ。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import config
from .logging_utils import get_logger

log = get_logger("remote_stop")

FOLDER = "管理者以外停止"
REQUEST_NAME = "お願い.json"
PRESENCE_DIR = "稼働中"

# 保存していない図・処理中の PC を待つ長さ(秒)。過ぎたら強制で止める(現場の決め)
GRACE_SEC = 300
# 何も残っていない PC でも、帯を見せてから止めるまでの間(秒)。画面の見張りは15秒ごとなので、それより長く
NOTICE_SEC = 30
# 見張りの間隔(秒)と、「動いています」の知らせの間隔(秒)
POLL_SEC = 5.0
BEAT_SEC = 60.0
# 知らせがこれより古ければ、管理者の画面で「応答なし」
STALE_SEC = 180.0
# 起動を止めたとき、理由の画面を出しておく長さ(秒)。過ぎたら終わる(デスクトップ版は窓が閉じる)
BLOCK_SHOW_SEC = 60.0
# たまたま読めない(書いている最中・掴まれている)ときの読み直し(秒)
READ_RETRY_SEC = (0.2, 0.5, 1.0)

# 各プロセスの答え
STATE_RUNNING = "running"        # 動いている(お願いは無い)
STATE_NOTICE = "notice"          # お願いを受けた。帯を出している(何も残っていなければまもなく止まる)
STATE_WAITING = "waiting"        # 保存していない図・処理中。利用者を待っている
STATE_STOPPED = "stopped"        # 止まった(利用者が終えた・何も残っていなかった)
STATE_FORCED = "forced"          # 5分たったので強制で止めた
STATE_LABELS = {
    STATE_RUNNING: "動いています",
    STATE_NOTICE: "お願いを受けました(まもなく止まります)",
    STATE_WAITING: "利用者を待っています",
    STATE_STOPPED: "止まりました",
    STATE_FORCED: "強制で止めました",
}

# 断りの種類(文言から推し量らない)
REFUSE_PASSWORD = "wrong_password"
REFUSE_NOT_ADMIN = "not_administrator"
REFUSE_NO_FOLDER = "no_folder"
REFUSE_UNREADABLE = "unreadable"
REFUSE_ALREADY = "already_active"
REFUSE_NOT_ACTIVE = "not_active"
REFUSE_WRITE = "write_failed"


class Unreadable(Exception):
    """お願いのファイルが読めない(届かない・壊れている)。「無い」とは別。"""


# ------------------------------------------------------------------
# 置き場所
# ------------------------------------------------------------------
def folder() -> Path:
    return config.master_db_dir() / FOLDER


def request_path() -> Path:
    return folder() / REQUEST_NAME


def presence_dir() -> Path:
    return folder() / PRESENCE_DIR


def _identity() -> tuple[str, str]:
    from .access_control import current_identity
    me = current_identity()
    return me.pc_name, me.login_id


def same_pc(a: str, b: str) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold() and bool((a or "").strip())


def _safe(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text or "") or "不明"


# ------------------------------------------------------------------
# 読み書き
# ------------------------------------------------------------------
def _read_once(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("中身が JSON のまとまりではありません")
    return data


def read_json(path: Path, *, retry: bool = True) -> Optional[dict]:
    """読む。**無いと確かめられたときだけ** None。読めなければ `Unreadable`(たまたまなら読み直す)。"""
    waits = READ_RETRY_SEC if retry else ()
    last: Optional[BaseException] = None
    for wait in (0.0, *waits):
        if wait:
            time.sleep(wait)
        try:
            return _read_once(path)
        except (OSError, ValueError) as exc:
            last = exc
    raise Unreadable(f"{path}: {last}")


def write_json(path: Path, data: dict, *, warn: bool = True) -> bool:
    """丸ごと書いてから入れ替える(書きかけを読ませない)。書けたら True。`warn=False` なら記録は控えめに。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        (log.warning if warn else log.debug)("管理者以外停止のファイルを書けませんでした(%s): %s", path, exc)
        return False


def read_request(*, retry: bool = True) -> Optional[dict]:
    """いまのお願い。**出ていなければ None**(無い・解除済み)。読めなければ `Unreadable`。"""
    data = read_json(request_path(), retry=retry)
    if data is None or not data.get("active"):
        return None
    return data


def describe_request(req: dict) -> str:
    """「誰が・いつ・何のために」の一言。"""
    when = time.strftime("%m/%d %H:%M", time.localtime(float(req.get("created_at") or 0)))
    who = f"{req.get('by_pc') or '?'}({req.get('by_login') or '?'})"
    reason = req.get("reason") or "更新のため"
    return f"{who} が {when} に出しました: {reason}"


# ------------------------------------------------------------------
# 管理者の PC: お願いを出す・解除する・各 PC の様子
# ------------------------------------------------------------------
@dataclass
class Result:
    ok: bool
    message: str
    reason: str = ""


def can_issue(password: str, conn: Any) -> Result:
    """お願いを出せるか(管理者パスワード **かつ** この端末の権限に Administrator)。"""
    from . import access_control, admin_password
    if not admin_password.verify(str(password or "")):
        return Result(False, "管理者パスワードが違います。", REFUSE_PASSWORD)
    if not access_control.is_administrator(conn):
        pc, login = _identity()
        return Result(False, f"この端末({pc} / {login})のアクセス権限に Administrator がありません。"
                      "管理者以外停止モードは Administrator の端末からだけ使えます。", REFUSE_NOT_ADMIN)
    if not config.master_db_dir().is_dir():
        return Result(False, f"共有の梱包資材マスタのフォルダに届きません: {config.master_db_dir()}"
                      "(お願いはそのフォルダに置きます)", REFUSE_NO_FOLDER)
    return Result(True, "")


def issue(password: str, conn: Any, reason: str = "") -> Result:
    """お願いを出す。**出ているお願いがあれば出さない**(読めなければ出さない)。"""
    allowed = can_issue(password, conn)
    if not allowed.ok:
        return allowed
    try:
        current = read_request()
    except Unreadable as exc:
        return Result(False, f"いまのお願いを読めないので出しません(上書きしないため): {exc}", REFUSE_UNREADABLE)
    if current is not None:
        return Result(False, "もうお願いが出ています。" + describe_request(current), REFUSE_ALREADY)
    pc, login = _identity()
    data = {
        "active": True, "id": uuid.uuid4().hex, "by_pc": pc, "by_login": login,
        "reason": (reason or "").strip()[:200] or "更新のため",
        "created_at": time.time(), "grace_sec": GRACE_SEC,
        "version": _version(),
    }
    if not write_json(request_path(), data):
        return Result(False, f"お願いを書けませんでした: {request_path()}", REFUSE_WRITE)
    log.warning("管理者以外停止のお願いを出しました: %s", describe_request(data),
                extra={"keep": True, "kind": "管理者以外停止"})
    _forget_old_answers()
    return Result(True, "お願いを出しました。ほかの PC は保存していない図・処理中が無ければまもなく止まり、"
                  f"残っていても {GRACE_SEC // 60}分で強制で止まります。")


def clear(password: str, conn: Any) -> Result:
    """解除する(更新が終わった)。ほかの PC がまた起動できるようになる。"""
    allowed = can_issue(password, conn)
    if not allowed.ok:
        return allowed
    try:
        current = read_request()
    except Unreadable as exc:
        return Result(False, f"いまのお願いを読めないので解除しません: {exc}", REFUSE_UNREADABLE)
    if current is None:
        return Result(False, "お願いは出ていません。", REFUSE_NOT_ACTIVE)
    pc, login = _identity()
    done = {**current, "active": False, "cleared_at": time.time(), "cleared_by_pc": pc, "cleared_by_login": login}
    if not write_json(request_path(), done):
        return Result(False, f"解除を書けませんでした: {request_path()}", REFUSE_WRITE)
    log.warning("管理者以外停止を解除しました(%s / %s)。ほかの PC も起動できます", pc, login,
                extra={"keep": True, "kind": "管理者以外停止"})
    _forget_old_answers()
    return Result(True, "解除しました。ほかの PC も起動できます。")


def _forget_old_answers() -> None:
    """止まった・古い答えのファイルを片付ける(**読めたものだけ**。読めないものは消さない)。"""
    base = presence_dir()
    try:
        files = list(base.glob("*.json"))
    except OSError:
        return
    now = time.time()
    for path in files:
        try:
            data = read_json(path, retry=False)
        except Unreadable:
            continue
        if data is None:
            continue
        old = now - float(data.get("beat_at") or 0) > STALE_SEC * 10
        if data.get("state") in (STATE_STOPPED, STATE_FORCED) or old:
            try:
                path.unlink()
            except OSError:
                pass


def _version() -> str:
    try:
        from . import app_config
        return app_config.version()
    except Exception:                         # noqa: BLE001
        return ""


@dataclass
class Overview:
    """管理者の画面に出すもの。"""

    available: bool
    why: str = ""
    request: Optional[dict] = None
    request_text: str = ""
    unreadable: str = ""
    pcs: list[dict] = field(default_factory=list)
    me_pc: str = ""

    def to_dict(self) -> dict:
        return {"available": self.available, "why": self.why, "request": self.request,
                "request_text": self.request_text, "unreadable": self.unreadable,
                "pcs": self.pcs, "me_pc": self.me_pc, "grace_sec": GRACE_SEC}


def overview(*, full: bool = True) -> Overview:
    """いまのお願いと、各 PC の様子。`full=False` ならお願いだけ(各 PC の知らせは読まない)。"""
    pc, _login = _identity()
    base = folder()
    if not config.master_db_dir().is_dir():
        return Overview(False, f"共有の梱包資材マスタのフォルダに届きません: {config.master_db_dir()}", me_pc=pc)
    view = Overview(True, me_pc=pc)
    try:
        view.request = read_request(retry=False)
    except Unreadable as exc:
        view.unreadable = f"お願いのファイルを読めません: {exc}"
    if view.request:
        view.request_text = describe_request(view.request)
    rid = (view.request or {}).get("id")
    now = time.time()
    if not full:
        return view
    try:
        files = sorted((base / PRESENCE_DIR).glob("*.json"))
    except OSError:
        files = []
    for path in files:
        try:
            data = read_json(path, retry=False)
        except Unreadable:
            view.pcs.append({"pc": path.stem, "state": "", "label": "読めません", "beat": "", "stale": True})
            continue
        if data is None:
            continue
        state = data.get("state") or STATE_RUNNING
        answered = data.get("request_id") == rid and rid
        finished = state in (STATE_STOPPED, STATE_FORCED)
        stale = (not finished) and now - float(data.get("beat_at") or 0) > STALE_SEC
        if finished and not answered:
            continue                              # 前のお願いで止まったもの
        label = STATE_LABELS.get(state, state)
        if stale:
            label = "応答なし(知らせが途絶えています)"
        elif rid and not answered and not same_pc(data.get("pc", ""), (view.request or {}).get("by_pc", "")):
            label = "まだお願いを受けていません"
        issuer = bool(rid) and same_pc(data.get("pc", ""), (view.request or {}).get("by_pc", ""))
        me = same_pc(data.get("pc", ""), pc)
        if issuer and me:
            # **見ている自分の PC がお願いを出した PC**(VER4.8.136。現場の声「この案内が自分の場合は別の表示に」)。
            # 自分のツールは止まらないので、更新の前に自分で閉じることを言う
            label = "この PC(あなたがお願いを出した PC)。止めません。更新の前に、このツールを閉じてください"
        elif issuer:
            label += "(お願いを出した PC。止めません)"
        elif me:
            label += "(この PC)"
        view.pcs.append({
            "pc": data.get("pc", ""), "login": data.get("login", ""), "kind": data.get("kind", ""),
            "version": data.get("version", ""), "state": state, "label": label,
            "detail": data.get("detail", ""), "stale": stale, "me": me,
            "beat": time.strftime("%H:%M:%S", time.localtime(float(data.get("beat_at") or 0))),
        })
    return view


# ------------------------------------------------------------------
# 起動: 更新中は起動させない
# ------------------------------------------------------------------
def startup_block() -> Optional[str]:
    """起動してよいか。止めるなら理由の文、よければ None。

    お願いを出した PC は止めない(更新したあと、その PC で起動して確かめ、解除する)。
    **読めないときは止めない**(共有に届かないだけで全員が起動できなくなるのを避ける)。記録には残す。
    """
    try:
        req = read_request()
    except Unreadable as exc:
        log.warning("管理者以外停止のお願いを確かめられないまま起動します: %s", exc,
                    extra={"keep": True, "kind": "管理者以外停止"})
        return None
    except Exception as exc:                  # noqa: BLE001 - 起動の判定で落ちない
        log.warning("管理者以外停止のお願いを確かめられませんでした: %s", exc)
        return None
    if req is None:
        return None
    pc, _login = _identity()
    if same_pc(pc, req.get("by_pc", "")):
        log.info("管理者以外停止のお願いが出ていますが、お願いを出した PC なので起動します: %s", describe_request(req))
        return None
    message = ("管理者が更新のためツールを止めています(管理者以外停止モード)。更新が終わるまで起動できません。"
               f"\n{describe_request(req)}"
               f"\nこの画面は {int(BLOCK_SHOW_SEC)}秒後に閉じます。しばらくしてから起動し直してください。"
               f"\n(管理者へ: お願いを出した PC で解除できないときは、{request_path()} を"
               "別の名前に変えると解除できます)")
    log.warning("管理者以外停止モード中のため起動しません: %s", describe_request(req),
                extra={"keep": True, "kind": "管理者以外停止"})
    return message


def stop_bat_refusal() -> Optional[str]:
    """stop.bat(ランチャーの「全 PC で止める」)を断るなら1行目の理由。**同時には使わない**(現場の決め)。"""
    try:
        req = read_request()
    except (Unreadable, Exception):           # noqa: BLE001 - 読めなければ断らない(止める道を閉じない)
        return None
    if req is None:
        return None
    return ("管理者以外停止モード中のため、stop.bat(ランチャーの「全 PC で止める」)は使いません。"
            f" 解除してから使ってください({describe_request(req)})")


# ------------------------------------------------------------------
# 受け側: 動いているプロセスの見張り
# ------------------------------------------------------------------
class Watcher:
    """お願いを見張り、帯を出し、止める。プロセスに1つ。"""

    def __init__(self, stop: Callable[[], None], unsaved: Callable[[], dict],
                 busy: Callable[[], list], *, kind: str = "browser", mode: str = "",
                 before_force: Optional[Callable[[], list[str]]] = None,
                 on_leave: Optional[Callable[[], list[str]]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._stop = stop
        self._unsaved = unsaved
        self._busy = busy
        self._before_force = before_force
        self._on_leave = on_leave
        self._clock = clock
        self.kind = kind
        self.mode = mode
        pc, login = _identity()
        self.pc, self.login = pc, login
        self.started_at = clock()
        self.state = STATE_RUNNING
        self.detail = ""
        self.request: Optional[dict] = None
        self.received_at = 0.0
        self.deadline = 0.0
        self.notice_until = 0.0
        self.stopping = False
        self._last_beat = 0.0
        self._beat_failing = False
        self._stop_called = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._halt = threading.Event()

    # --- 自分の知らせ ---------------------------------------------------
    def presence_path(self) -> Path:
        return presence_dir() / f"{_safe(self.pc)}__{_safe(self.login)}__{os.getpid()}.json"

    def _beat(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_beat < BEAT_SEC:
            return
        if not config.master_db_dir().is_dir():
            return
        self._last_beat = now
        ok = write_json(self.presence_path(), {
            "pc": self.pc, "login": self.login, "pid": os.getpid(), "kind": self.kind, "mode": self.mode,
            "version": _version(), "started_at": self.started_at, "beat_at": now,
            "state": self.state, "detail": self.detail,
            "request_id": (self.request or {}).get("id", ""),
        }, warn=not self._beat_failing)
        # 書けないあいだは1分ごとに記録をためない(最初の1回と、直ったときだけ)
        if not ok and not self._beat_failing:
            log.warning("管理者以外停止の知らせを共有に書けません(書けるようになるまで記録しません): %s",
                        self.presence_path())
        elif ok and self._beat_failing:
            log.info("管理者以外停止の知らせを共有に書けるようになりました")
        self._beat_failing = not ok

    def leave(self) -> None:
        """終わるとき。止まった答えは残す(管理者が見る)、ふだんの終わりなら消す。

        **お願いを受けている最中に利用者が自分で終えた**(帯の「終了」・タブや窓を閉じた)ときも、
        「止まりました」を残す(消すと管理者の一覧から消えて、止まったのか分からない)。
        """
        self._halt.set()
        with self._lock:
            if self.state in (STATE_STOPPED, STATE_FORCED):
                return
            if self.request is not None:
                kept = []
                if self._on_leave is not None:
                    try:
                        kept = list(self._on_leave() or [])
                    except Exception:         # noqa: BLE001 - 控えに失敗しても終わりは止めない
                        log.exception("終わるときの控えを残せませんでした")
                self.state = STATE_STOPPED
                self.detail = "利用者が終了しました" + (f"(控えに残しました: {'、'.join(kept)})" if kept else "")
                self._beat(force=True)
                return
        try:
            self.presence_path().unlink(missing_ok=True)
        except OSError:
            pass

    # --- 見張り ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="remote-stop", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._halt.is_set():
            try:
                self.tick()
            except Exception:                 # noqa: BLE001 - 見張りは止めない
                log.debug("管理者以外停止の見張りで失敗しました(次で試します)", exc_info=True)
            if self.stopping:
                return
            self._halt.wait(POLL_SEC)

    def tick(self) -> None:
        """1回ぶん見る。**止めるのは錠の外で**(止めると終わりの後始末 `leave` が同じ錠を取りに来る)。"""
        with self._lock:
            self._tick()
            stop_now = self.stopping and not self._stop_called
            if stop_now:
                self._stop_called = True
        if stop_now:
            try:
                self._stop()
            except Exception:                 # noqa: BLE001
                log.exception("管理者以外停止で止められませんでした")

    def _tick(self) -> None:
        if self.stopping:
            return
        now = self._clock()
        try:
            req = read_request()
        except Unreadable as exc:
            # **読めないことを「お願いは無い」にしない。** いまの判断のまま待つ(期限は進める)
            log.debug("管理者以外停止のお願いを読めません(前の判断のまま): %s", exc)
            req = self.request
        if req is not None and same_pc(self.pc, req.get("by_pc", "")):
            req = None                            # お願いを出した PC は止めない
        if req is None:
            if self.request is not None:
                log.info("管理者以外停止のお願いが取り下げられました。止めずに続けます")
                user_note("管理者が停止のお願いを取り下げました。そのまま使えます")
            self.request = None
            self.state, self.detail = STATE_RUNNING, ""
            self._beat()
            return
        if self.request is None or self.request.get("id") != req.get("id"):
            self.request = req
            self.received_at = now
            self.deadline = now + float(req.get("grace_sec") or GRACE_SEC)
            self.notice_until = now + NOTICE_SEC
            log.warning("管理者以外停止のお願いを受けました: %s", describe_request(req),
                        extra={"keep": True, "kind": "管理者以外停止"})
        left = self._left()
        # 何も残っていなければ止める。帯を見せる間(NOTICE_SEC)は待つが、利用者を待っていた(帯はもう見えている)
        # あとに保存・終了して何も残らなくなったなら、すぐ止める
        if not left and (now >= self.notice_until or self.state == STATE_WAITING):
            self._finish(STATE_STOPPED, "保存していない図・実行中の処理が無いので止めました")
            return
        if now >= self.deadline:
            saved = self._save_before_force()
            detail = "5分たっても止まらないので強制で止めました"
            if left:
                detail += f"(残っていたもの: {'、'.join(left)})"
            if saved:
                detail += f"。保存していない図は控えに残しました: {'、'.join(saved)}"
            self._finish(STATE_FORCED, detail)
            return
        self.state = STATE_WAITING if left else STATE_NOTICE
        self.detail = "、".join(left)
        self._beat(force=True)

    def _left(self) -> list[str]:
        names = list((self._unsaved() or {}).values())
        names += [str(job) for job in (self._busy() or [])]
        return names

    def _save_before_force(self) -> list[str]:
        if self._before_force is None:
            return []
        try:
            return list(self._before_force() or [])
        except Exception:                     # noqa: BLE001 - 控えに失敗しても止める(期限は守る)
            log.exception("保存していない図の控えを残せませんでした")
            return []

    def _finish(self, state: str, detail: str) -> None:
        self.state, self.detail = state, detail
        self.stopping = True
        self._beat(force=True)
        log.warning("管理者以外停止: %s", detail, extra={"keep": True, "kind": "管理者以外停止"})

    # --- 画面へ --------------------------------------------------------
    def screen_state(self) -> Optional[dict]:
        """帯に出すもの。お願いが無ければ None。"""
        req = self.request
        if req is None:
            return None
        return {
            "id": req.get("id", ""),
            "text": describe_request(req),
            "by": f"{req.get('by_pc') or '?'}({req.get('by_login') or '?'})",
            "reason": req.get("reason") or "更新のため",
            "deadline_ms": int(self.deadline * 1000),
            "now_ms": int(self._clock() * 1000),
            "left": self.detail.split("、") if self.detail else [],
            "state": self.state,
        }


_watcher: Optional[Watcher] = None
_notes: list[str] = []
_notes_lock = threading.Lock()


def install(stop: Callable[[], None], unsaved: Callable[[], dict], busy: Callable[[], list], *,
            kind: str = "browser", mode: str = "",
            before_force: Optional[Callable[[], list[str]]] = None,
            on_leave: Optional[Callable[[], list[str]]] = None) -> Watcher:
    global _watcher
    if _watcher is not None:
        return _watcher
    _watcher = Watcher(stop, unsaved, busy, kind=kind, mode=mode, before_force=before_force,
                       on_leave=on_leave)
    _watcher.start()
    return _watcher


def get() -> Optional[Watcher]:
    return _watcher


def leave() -> None:
    if _watcher is not None:
        _watcher.leave()


def screen_state() -> Optional[dict]:
    return _watcher.screen_state() if _watcher is not None else None


def user_note(message: str) -> None:
    """画面に一度だけ出す一言(次の心拍で渡す)。"""
    with _notes_lock:
        _notes.append(message)


def take_notes() -> list[str]:
    with _notes_lock:
        out = list(_notes)
        _notes.clear()
    return out


def reset() -> None:
    """試験用。"""
    global _watcher
    if _watcher is not None:
        _watcher._halt.set()
    _watcher = None
    take_notes()
