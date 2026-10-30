"""画面の記録(設定の「ログ」の面・エラー・注意の記録)に**何を出すか**の決まり。VER4.8.120

【決まり】
- その日のログ(ファイル)には**全部**書く(あとから追う材料。減らさない)
- 画面の記録には、**人が何かしないといけないもの**だけ出す
    1. エラー
    2. 断った操作
    3. ツールが自分の判断で共有のデータを変えたとき(送り直した・二重を止めた・写しを消した) … `once`
    4. **同じ失敗が続いているとき**(共有を読めない・送れない・手元へ写せない) … `failed` / `ok`
       1回きりで次に直るものは出さない。続いたら1件だけ(回数・最初に起きた時刻つき)。直ったら「直った」も1件
    5. 時間のかかった操作・返らない操作(`app._footprint`・`stall_watch`)

現場の声:「いやログ出力は必要だから全部画面に出せばいいってもんでもないよ」。前は、共有を読めない回が
何百回も続いても、その日のログに同じ注意が1行ずつ並ぶだけで、画面には何も出なかった(実績の写しが
37,152件になるまで誰も気づけなかった)。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# 「続いている」とみなすまで: この秒数たっても直らず、2回以上しくじったとき
CONTINUE_SEC = 300
CONTINUE_COUNT = 2
# 続いたままのときに、もう一度だけ言う間隔(1日に数回まで)
REMIND_SEC = 6 * 3600
# ツールが自分で共有を変えたことを、同じ種類で言い直さない間隔(そのあいだの回数は次に添える)
ONCE_EVERY_SEC = 3600

KIND_UNREADABLE = "共有を読めない"
KIND_UNSENDABLE = "送れない"
KIND_NO_COPY = "手元へ写せない"
KIND_RECOVERED = "直った"
KIND_RESENT = "実績の送り直し"
KIND_STOPPED_DUPLICATE = "二重を止めた"
KIND_CLEANED = "重複を消した"
KIND_GUARD_OFF = "二重登録の防止が効いていない"
KIND_BROKEN_SETTINGS = "設定ファイルが壊れている"
KIND_SLOW_IMPORT = "時間のかかった取り込み"

# 取り込み(裏の取り込み直しも含む)がこの秒数を超えたら、画面の記録に出す(VER4.8.121)。
# 取り込みのあいだは手元のDBを書いているので、長いと画面の読み取りが待たされる
SLOW_IMPORT_SEC = 10.0


def keep(kind: str) -> dict[str, Any]:
    """画面の記録にも残す行の印(`logging_utils._ErrorRecordHandler`)。"""
    return {"keep": True, "kind": kind}


@dataclass
class _Streak:
    kind: str
    what: str
    first: float
    first_at: str
    count: int = 0
    noted_at: Optional[float] = None
    reason: str = ""


_lock = threading.Lock()
_streaks: dict[str, _Streak] = {}
_once: dict[str, tuple[float, int]] = {}


def _now_text(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%m/%d %H:%M")


def failed(log: logging.Logger, key: str, kind: str, what: str, reason: Any,
           *, now: Optional[float] = None) -> bool:
    """しくじった。**続いているとき**だけ画面の記録に1件出す(出したら True)。

    1回ごとの行はその日のログに書く(呼び手の注意の行がそれ)。`key` は同じ失敗を見分ける名前
    (ファイルや表ごと)。`what` は人が読む「何が」(例: 「共有の梱包資材マスタの実績の取り込み」)。
    """
    at = time.time() if now is None else now
    with _lock:
        streak = _streaks.get(key)
        if streak is None:
            streak = _streaks[key] = _Streak(kind, what, at, _now_text(at))
        streak.count += 1
        streak.reason = str(reason)
        if streak.noted_at is None:
            due = streak.count >= CONTINUE_COUNT and at - streak.first >= CONTINUE_SEC
        else:
            due = at - streak.noted_at >= REMIND_SEC
        if not due:
            return False
        again = streak.noted_at is not None
        streak.noted_at = at
        minutes = int((at - streak.first) // 60)
        count, first_at, last = streak.count, streak.first_at, streak.reason
    log.warning("%s が%s続いています: %s回・%s から(%s分)。最後の理由: %s",
                what, "まだ" if again else "", count, first_at, minutes, last, extra=keep(kind))
    return True


def ok(log: logging.Logger, key: str, *, now: Optional[float] = None) -> bool:
    """うまくいった。続いていると画面に出していた失敗なら「直った」を1件出す(出したら True)。"""
    at = time.time() if now is None else now
    with _lock:
        streak = _streaks.pop(key, None)
    if streak is None or streak.noted_at is None:
        return False
    minutes = int((at - streak.first) // 60)
    log.warning("直りました: %s(%s から %s分・%s回しくじっていました)",
                streak.what, streak.first_at, minutes, streak.count, extra=keep(KIND_RECOVERED))
    return True


def once(log: logging.Logger, key: str, kind: str, message: str, *args: Any,
         now: Optional[float] = None, every: float = ONCE_EVERY_SEC) -> bool:
    """ツールが自分の判断で共有を変えたことを言う。同じ `key` は `every` 秒に1件だけ画面に出す
    (あいだの分はその日のログにだけ書き、次に出すときに回数を添える)。画面に出したら True。"""
    at = time.time() if now is None else now
    with _lock:
        last, skipped = _once.get(key, (0.0, 0))
        show = not last or at - last >= every
        _once[key] = (at, 0) if show else (last, skipped + 1)
    if show:
        tail = f"(前に記録してから {skipped}回)" if skipped else ""
        log.warning(message + tail, *args, extra=keep(kind))
    else:
        log.warning(message, *args)
    return show


def slow_import(log: logging.Logger, what: str, seconds: float,
                parts: dict[str, float] | None = None, *, now: Optional[float] = None) -> bool:
    """取り込みに `SLOW_IMPORT_SEC` 秒以上かかったら、画面の記録に出す(同じ取り込みは1時間に1件。
    あいだの回数は次に添える)。`parts` は表ごとの秒数(長い順に5つまで添える)。出したら True。

    前は、裏の取り込みに何秒かかったかをどこにも出していなかったので、発注などが増えて少しずつ
    長くなっても、遅くなってから気づくしかなかった(VER4.8.121)。
    """
    if seconds < SLOW_IMPORT_SEC:
        return False
    top = sorted((parts or {}).items(), key=lambda kv: -kv[1])[:5]
    detail = "、".join(f"{name} {sec:.1f}秒" for name, sec in top if sec >= 0.05)
    return once(log, f"時間のかかった取り込み|{what}", KIND_SLOW_IMPORT,
                "%s に %.1f秒 かかりました(取り込みのあいだ、画面の読み取りが待たされることがあります)%s",
                what, seconds, f"。長かった表: {detail}" if detail else "", now=now)


def forget() -> None:
    """覚えている続き具合を消す(試験用)。"""
    with _lock:
        _streaks.clear()
        _once.clear()
