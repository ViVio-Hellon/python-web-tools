"""この端末がつながっている共有マスタ(取り違え防止)

手元の作業用DBは、**どれか1つの共有マスタ**の写し + この端末で作ってまだ送っていない行で
できている。置き場所の設定を別の共有マスタ(本番 ⇔ Test環境 など)へ変えたとき、前の
共有向けに作った行を新しい共有へ送ると、2つの共有の中身が混ざる。現場の端末でも、
Test環境 には無い表(発注コメント閲覧)の行を「済」で持っていた ── 別の共有にも
つながっていた形跡がある。

ここでは「手元の中身がどの共有マスタのものか」を1つ覚える。

    同じ共有             … そのまま
    違う共有・送る物なし … 新しい共有に付け替える(取り込みで中身も入れ替わる)
    違う共有・送る物あり … **送らない**。前の共有に作った行を新しい共有へ送らない。
                            理由を画面に出す(前の置き場所に戻して送れば付け替えられる)

覚えるのは場所の文字列だけ(書き方の揺れはそろえて比べる)。PC を替えたときは、
新しい端末が最初に取り込んだ共有を覚える。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .logging_utils import get_logger

log = get_logger("shared_link")

TABLE = "つながっている共有"


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS [{TABLE}] ("
        " 番号 INTEGER PRIMARY KEY CHECK (番号 = 1),"
        " 場所 TEXT NOT NULL,"
        " 覚えた日時 TEXT NOT NULL DEFAULT (datetime('now','localtime')))")


def same_place(a: object, b: object) -> bool:
    """同じ共有マスタか(区切り記号・大文字小文字の書き方の揺れはそろえて比べる)。"""
    def norm(p: object) -> str:
        return os.path.normcase(os.path.normpath(str(p))).replace("\\", "/").lower()
    return norm(a) == norm(b)


def remembered(conn: sqlite3.Connection) -> Optional[str]:
    """覚えている共有マスタの場所。まだ無ければ None。"""
    _ensure(conn)
    row = conn.execute(f"SELECT 場所 FROM [{TABLE}] WHERE 番号 = 1").fetchone()
    return str(row[0]) if row else None


def remember(conn: sqlite3.Connection, path: Path) -> None:
    _ensure(conn)
    with conn:
        conn.execute(f"INSERT OR REPLACE INTO [{TABLE}] (番号, 場所) VALUES (1, ?)", (str(path),))


@dataclass
class Link:
    """いまの共有マスタと、覚えている共有マスタの関係。"""

    current: str
    previous: Optional[str] = None
    pending: int = 0              # 前の共有向けに作って、まだ送っていない行の数
    switched: bool = False        # 付け替えた(送る物が無かったので)

    @property
    def blocked(self) -> bool:
        return bool(self.previous) and not same_place(self.previous, self.current) \
            and self.pending > 0

    def why(self) -> str:
        """送れない理由(送れるなら空)。"""
        if not self.blocked:
            return ""
        return (f"共有マスタの置き場所が変わりました(前: {self.previous} → いま: {self.current})。"
                f"前の共有マスタ向けに作って、まだ送っていないものが {self.pending}件 あるので、"
                "新しい共有マスタへは送りません(2つの共有の中身が混ざるため)。"
                "設定の置き場所を前に戻すと送れます。送り終われば、新しい置き場所に切り替えられます。")


def bind(conn: sqlite3.Connection, path: Path) -> Link:
    """送る前・取り込む前に呼ぶ。同じなら何もしない。違えば、送る物が無いときだけ付け替える。"""
    from . import sync_writeback
    previous = remembered(conn)
    link = Link(current=str(path), previous=previous)
    if previous is None:
        remember(conn, path)          # はじめて(新しい端末・この版にしたばかり)
        return link
    if same_place(previous, path):
        return link
    link.pending = sum(sync_writeback._unsent_writeback_tables(conn).values())
    if link.pending:
        log.warning("%s", link.why())
        return link
    remember(conn, path)
    link.switched = True
    log.info("つながっている共有マスタを付け替えました: %s → %s", previous, path)
    return link
