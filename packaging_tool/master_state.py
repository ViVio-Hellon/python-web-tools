"""マスタが**変わったこと**を、開いている画面へ知らせるための番号(VER4.8.94)

現場の声:「作業長でない端末でも送れるままですよ ── と思ったらタブ変えたら送れなくなった。
毎回いうけどマスタ編集したらリアルタイムで読み直してください」

マスタ(アクセス権限など)を直すと手元の表はその場で入れ替わる(`master_common._follow`)。
しかし**開いている画面は、出したときの答えのまま**だった(倉庫送信のボタン・作業長かどうか)。
ここが持つのは**変わった回数**(世代)だけ。管理者認証の世代(`auth_state`)と同じ形で:

- 画面は開いたときの世代を覚えておき(`window.APP.dataEpoch`)
- `/api/health` の見張り(数秒おき・タブや窓に戻ったとき)で違う世代を見たら
- マスタで決まるところ(倉庫送信・作業長・作業ライン・蓋を待つか)をサーバから引き直して描き直す

進めるのは: マスタ管理で直した・アクセス権限だけ読み直した・マスタをまとめて取り込んだ とき。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_epoch = 0
# 表ごとの変わった回数(VER4.8.95)。**手元の表が入れ替わった・書き換わった**ときに進める
# (取り込み・適合範囲の計算し直し)。覚えている行の写しを持つところ(資材選択の選んだパレット)が、
# 自分の表が変わったときだけ引き直すのに使う
_table_rev: dict[str, int] = {}


def epoch() -> int:
    with _lock:
        return _epoch


def table_rev(table: str) -> int:
    """その表が変わった回数(まだ変わっていなければ 0)。"""
    with _lock:
        return _table_rev.get(table, 0)


def touch(tables) -> None:
    """手元の表が入れ替わった・書き換わった(取り込み・計算し直し)。表ごとの回数を進める。

    マスタ管理で直せる表(`master_common.MANAGED`)なら、画面へ知らせる世代も進める ──
    倉庫連携の見張りがパレットを取り込み直したときにも、開いている画面が読み直すように。
    """
    global _epoch
    names = [t for t in tables if t]
    if not names:
        return
    try:
        from .master_common import MANAGED
        masters = {m.table for m in MANAGED}
    except Exception:                         # noqa: BLE001 - 数えるのを止めない
        masters = set()
    with _lock:
        for table in names:
            _table_rev[table] = _table_rev.get(table, 0) + 1
        if any(t in masters for t in names):
            _epoch += 1


def changed() -> int:
    """マスタが変わった。新しい世代を返す。"""
    global _epoch
    with _lock:
        _epoch += 1
        return _epoch
