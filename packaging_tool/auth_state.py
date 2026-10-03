"""管理者認証の状態が**変わったこと**を、開いている画面へ知らせるための番号

現場の声:「パスワード認証してもマスタ編集がすぐできない。タブを切り替えて
戻ってもまだ無い。何度も言っているが、認証系はすぐにどこにでも反映させないと
分からないので良くない」。

認証の状態そのものは `selection_session` の `admin` が持つ(プロセスに1つ)。
ここが持つのは**変わった回数**(世代)だけ。画面は開いたときの世代を覚えておき
(`window.APP.authEpoch`)、違う世代を見たら、認証に関わるところを描き直す:

- 認証した画面: 応答で新しい世代を受け取り、その場で知らせる
- ほかの画面・窓: `/api/health` の見張り(数秒おき)で気づく
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_epoch = 0


def epoch() -> int:
    with _lock:
        return _epoch


def changed() -> int:
    """認証の状態が変わった。新しい世代を返す。"""
    global _epoch
    with _lock:
        _epoch += 1
        return _epoch


def admin_now() -> bool:
    """いま管理者として認証してあるか(セッションが無ければ False。作らない)。"""
    from . import selection_session
    session = selection_session._session
    return bool(session is not None and session.admin)


def snapshot() -> dict:
    """画面へ渡す形。"""
    return {"admin": admin_now(), "epoch": epoch()}
