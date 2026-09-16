"""いま使っている画面は1つだけ

【なぜ要るのか】
このツールは**作業状態をプロセスに1つ**持っています(`work_context` /
`selection_session`)。1台のPCを1人が使う前提で、画面が覚えていたことを
プロセス側に置いてあるからです。

ところがブラウザは同じアドレスを**いくつでも開けます**。2枚開くと、
どちらも同じ状態を触ります。

    画面A: ロット 1234567 を引いて、パレットを決める
    画面B: ロット 9999999 を引く          ← 状態が入れ替わる
    画面A: そのまま「倉庫送信」を押す      ← **Bのロットで送られる**

画面Aには 1234567 が出たままなので、押した人は気づけません。発注は
取り消しに人手が要るので、これは静かに間違うたぐいの中でも重いほうです。

【どう塞ぐか】
**いちばん最後に開いた画面だけが操作できます。** 画面を開くたびに
新しい番号を振って、この番号を「いま使っている画面」として覚えます。
古い画面から要求が来たら断り、画面の側は「別の画面で開いています」と
出して、そこから**取り戻せる**ようにします(閉じ忘れただけのことも
あるので、行き止まりにはしません)。

**番号を送ってこない相手は素通しします。** 守りたいのはブラウザの
画面どうしの取り合いで、`curl` や試験を巻き込む理由はありません。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

from .logging_utils import get_logger

log = get_logger("screen_lock")

# ブラウザが要求に付ける見出し。画面を開いたときに配った番号を返す
HEADER = "X-Screen-Id"

# 断りの種別。画面側はこれを見て「別の画面で開いています」を出す
REFUSE_TAKEN = "screen_taken"

# 断りの文言。**サーバが持つ**(設計書 §1)
REFUSE_MESSAGE = ("このタブは、あとから開いたタブに操作を譲りました。"
                  "続きはそちらで行ってください"
                  "(このタブで続けるなら、読み込み直してください)。")

_lock = threading.Lock()
_active: Optional[str] = None
_claimed_at: float = 0.0


def new_id() -> str:
    """画面1枚ぶんの番号。"""
    return uuid.uuid4().hex


def claim(screen_id: str) -> str:
    """この画面を「いま使っている画面」にする。

    画面を**開くたび**に呼びます ── 最後に開いたものが使う画面、が
    いちばん素直な決め方で、利用者は自分が今見ているものを操作できます。
    """
    global _active, _claimed_at
    with _lock:
        before = _active
        _active = screen_id
        _claimed_at = time.time()
    if before and before != screen_id:
        log.info("使う画面が替わりました: %s → %s", before[:8], screen_id[:8])
    return screen_id


def for_request(sent: str) -> str:
    """画面を組み立てるときに呼ぶ。その画面が名乗る番号を返す。

    見分け方は1つだけです:

        番号を**送ってきた**  … 同じタブの中の移動(`nav.js` が中身だけ
                                差し替える)。**同じ番号を使い続ける**
        送ってこない          … ブラウザがアドレスを開いた ── 新しい
                                タブか、読み込み直し。**新しい番号を振る**

    どちらの場合も「いま使っている画面」にします。**画面を開いた人が、
    その画面を操作できる**のがいちばん素直で、閉じ忘れた古いタブに
    握られたままにはなりません。読み込み直しが取り戻す手にもなります。
    """
    return claim(sent.strip() or new_id())


def active() -> Optional[str]:
    with _lock:
        return _active


def is_active(screen_id: str) -> bool:
    """その画面が操作してよいか。

    **番号が無ければ通します**(`curl`・試験・まだ配る前の要求)。
    まだ1枚も開いていないときも通します ── 誰とも取り合っていません。
    """
    if not screen_id:
        return True
    with _lock:
        return _active is None or _active == screen_id


def release(screen_id: str) -> None:
    """その画面が閉じた。自分が使っている画面だったときだけ手放す。"""
    global _active
    with _lock:
        if _active == screen_id:
            _active = None
            log.info("使っていた画面が閉じました: %s", screen_id[:8])


def reset() -> None:
    """試験用。"""
    global _active, _claimed_at
    with _lock:
        _active, _claimed_at = None, 0.0
