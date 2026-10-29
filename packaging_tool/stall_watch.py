"""返らない要求を、**どこで止まっているか**ごとログに残す(VER4.8.115)

現場の声:「資材と現場を切り替えると時々フリーズする。設定も開けなくなるのでログすらみれない」。

フリーズは現場の端末でしか起きないことが多く、こちらでは再現できない。起きたときに
「どの要求が・何を待って止まっているか」が残っていないと、直しようがない。

要求が `STALL_SEC` を超えても返らなければ、その要求のスレッドがいま何をしているか
(呼び出しの並び)を**1度だけ**ログに書く。止まっている要求が `CROWD` 件を超えたら、
全部がふさがる前にそう書く。ログは別のスレッドが書く(`logging_utils._AsyncWriter`)ので、
ここが書くことでさらに止まることは無い。
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Optional

from .logging_utils import get_logger

log = get_logger("stall_watch")

# これを超えて返らない要求を「止まっている」と見る(秒)
STALL_SEC = 20.0
# 見て回る間(秒)
CHECK_SEC = 5.0
# 止まっている要求がこの数を超えたら、まとめて知らせる(受付は8本)
CROWD = 4

_lock = threading.Lock()
# スレッド番号 → [何の要求か, 始まった時刻, もう書いたか]
_active: dict[int, list] = {}
_thread: Optional[threading.Thread] = None
_crowd_reported = False


def begin(label: str) -> None:
    """このスレッドで要求が始まった。"""
    from .logging_utils import current_operation
    # どの操作か(操作番号・画面・入力)。止まったときの記録に付けて、同じ操作の行を集められるようにする
    operation = dict(current_operation() or {})
    with _lock:
        _active[threading.get_ident()] = [label, time.monotonic(), False, operation]
    _ensure_thread()


def end() -> None:
    """このスレッドの要求が終わった。"""
    with _lock:
        _active.pop(threading.get_ident(), None)


def stalled(now: Optional[float] = None) -> list[tuple[str, float]]:
    """いま止まっている要求(何の要求か, 秒)。"""
    moment = time.monotonic() if now is None else now
    with _lock:
        return [(entry[0], moment - entry[1]) for entry in _active.values()
                if moment - entry[1] >= STALL_SEC]


def check(now: Optional[float] = None) -> int:
    """1回見て回る。書いた件数を返す(試験からも呼ぶ)。"""
    global _crowd_reported
    moment = time.monotonic() if now is None else now
    frames = sys._current_frames()                       # noqa: SLF001 - 調べるための標準の口
    report: list[tuple[str, float, str, dict]] = []
    with _lock:
        for ident, entry in _active.items():
            label, began, done = entry[0], entry[1], entry[2]
            if done or moment - began < STALL_SEC:
                continue
            entry[2] = True
            frame = frames.get(ident)
            stack = "".join(traceback.format_stack(frame)) if frame is not None else "(呼び出しの並びが取れません)"
            report.append((label, moment - began, stack, entry[3] if len(entry) > 3 else {}))
        crowd = sum(1 for entry in _active.values() if moment - entry[1] >= STALL_SEC)
    for label, took, stack, operation in report:
        # **設定の「ログ」の面に出す**(VER4.8.118。前はその日のログファイルにだけあった)。
        # 立ち会えなくても、あとから「何が・どこで待っていたか」を開ける
        log.warning("要求が %.0f秒 返っていません: %s\nいまここにいます:\n%s", took, label, stack,
                    extra={"keep": True, "kind": "返らない操作", "operation": operation,
                           "detail": f"いまここにいます(呼び出しの並び。いちばん下が今いるところ):\n{stack}"})
    if crowd > CROWD and not _crowd_reported:
        _crowd_reported = True
        log.warning("返らない要求が %s件 あります(受付がふさがりかけています): %s",
                    crowd, " / ".join(label for label, _ in stalled(moment)))
    elif crowd <= CROWD:
        _crowd_reported = False
    return len(report)


def _ensure_thread() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="stall-watch", daemon=True)
        _thread.start()


def _loop() -> None:
    while True:
        time.sleep(CHECK_SEC)
        try:
            check()
        except Exception:                                # noqa: BLE001 - 見張りは止めない
            pass


def forget() -> None:
    """試験用。"""
    global _crowd_reported
    with _lock:
        _active.clear()
    _crowd_reported = False
