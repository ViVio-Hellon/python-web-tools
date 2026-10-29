"""裏で動いている長い処理の**いまの進み具合**(VER4.8.121)。

現場の声:「長い処理がある事はある程度許容できるが何故長いのかがわからないことが問題」
「長ければ進捗としてプログレス等だす」。

前は、共有が変わるたびの取り込み直し(発注まわり・実績)が裏で何十秒動いても、画面には何も出なかった。
画面は待たされ、なぜ待っているのか分からなかった(「検索一つに15分」)。

ここに「いま何をしていて、どこまで進んだか」を置き、画面の心拍(`/api/health`)と `/api/busy` が返す。
画面は、しばらく続いている処理を帯に出す(`static/js/health.js`)。

使い方:
    with work_status.work("発注まわりの取り込み直し"):
        work_status.step("PalletMaster を入れ直しています", done=3, total=9)
        ...
**入れ子にしない。** すでに同じスレッドで処理が動いていれば、内側の `work` は外側の「いまの段」を
書き換えるだけ(1つの操作が帯に2本出ない)。`step` は動いている処理が無ければ何もしない。
"""
from __future__ import annotations

import itertools
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

# この秒数より短い処理は帯に出さない(すぐ終わるものでちらつかせない)
SHOW_AFTER_SEC = 2.0


@dataclass
class _Work:
    id: int
    name: str
    started: float
    step: str = ""
    done: Optional[int] = None
    total: Optional[int] = None
    detail: str = ""                # 段の中のいまの手(「…を手元へ写しています(28.6MB)」など)
    step_started: float = field(default_factory=time.monotonic)


_lock = threading.Lock()
_works: dict[int, _Work] = {}
_ids = itertools.count(1)
_local = threading.local()


@contextmanager
def work(name: str) -> Iterator[None]:
    """長いかもしれない処理を1つ始める(終われば消える)。入れ子なら外側の段を書き換えるだけ。"""
    outer: Optional[_Work] = getattr(_local, "current", None)
    if outer is not None:
        step(name)
        yield
        return
    item = _Work(next(_ids), name, time.monotonic())
    with _lock:
        _works[item.id] = item
    _local.current = item
    try:
        yield
    finally:
        _local.current = None
        with _lock:
            _works.pop(item.id, None)


def step(text: str, done: Optional[int] = None, total: Optional[int] = None) -> None:
    """いま動いている処理(このスレッドの)の段を書く。動いていなければ何もしない。"""
    item: Optional[_Work] = getattr(_local, "current", None)
    if item is None:
        return
    with _lock:
        item.step = text
        item.done = done
        item.total = total
        item.detail = ""
        item.step_started = time.monotonic()


def detail(text: str) -> None:
    """いまの段の中の手を書く(段と数はそのまま)。動いている処理が無ければ何もしない。"""
    item: Optional[_Work] = getattr(_local, "current", None)
    if item is None:
        return
    with _lock:
        item.detail = text


def snapshot(min_sec: Optional[float] = None, *, now: Optional[float] = None) -> list[dict]:
    """`min_sec`(既定 `SHOW_AFTER_SEC`)秒以上続いている処理の様子(古い順)。画面の帯に出すもの。"""
    at = time.monotonic() if now is None else now
    min_sec = SHOW_AFTER_SEC if min_sec is None else min_sec
    with _lock:
        items = sorted(_works.values(), key=lambda w: w.started)
        out = []
        for w in items:
            elapsed = at - w.started
            if elapsed < min_sec:
                continue
            out.append({"name": w.name, "step": w.step, "done": w.done, "total": w.total,
                        "detail": w.detail,
                        "elapsed_sec": round(elapsed, 1),
                        "step_sec": round(at - w.step_started, 1)})
    return out


def describe(item: dict) -> str:
    """帯・記録に出す1行(画面の JS も同じ書き方)。"""
    text = item["name"]
    if item.get("step"):
        text += f" ── {item['step']}"
        if item.get("total"):
            text += f"({item.get('done') or 0}/{item['total']})"
    if item.get("detail"):
        text += f" … {item['detail']}"
    return f"{text}・{int(item.get('elapsed_sec') or 0)}秒"
