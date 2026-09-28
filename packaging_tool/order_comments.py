"""発注ごとの現場⇔倉庫のやり取り(コメント)

【形】
発注1件につき、書き込みを何件でも積む(書き直さない)。誰が(端末名と
現場/倉庫)・いつ・何を書いたかを残すので、往復の経緯が後から読める。

【どの発注のコメントか ── 発注キー】
手元の管理番号は取り込みのたびに振り直され、共有の管理番号は送るまで
決まらない。どちらも鍵にならないので、発注を作るときに `発注キー` を
振る(`warehouse_service.create_order`)。この列を足す前の発注は
`#共有の管理番号` を鍵にする(共有の行番号は変わらない)。

【いつ書けるか】
**発注が未確認のあいだだけ。** 倉庫が確認したか、現場が取り消した発注は
記録として固定する(現場の判断)。

【未読】
相手(自分の端末以外)が書いたもので、この端末でまだ開いていないもの。
どれを読んだかは端末ごとの表(`発注コメント既読`)に `コメントID` で残す。
共有へは送らない ── 読んだかどうかは人ごと(端末ごと)の話。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import config, db
from .logging_utils import get_logger

log = get_logger("order_comments")

TABLE = config.TBL_ORDER_COMMENT
READ_TABLE = config.TBL_ORDER_COMMENT_READ
ORDER_TABLE = config.TBL_WAREHOUSE_ORDER

# 1件の長さの上限。**やり取りのための欄**で、長い文を置く場所ではない
MAX_LENGTH = 300

SIDE_FIELD = "現場"
SIDE_MATERIAL = "倉庫"


@dataclass
class Comment:
    comment_id: str
    terminal: str
    side: str
    text: str
    written_at: str
    mine: bool = False
    unread: bool = False


@dataclass
class Summary:
    """一覧の1行に添える、その発注のコメントのまとめ。"""

    count: int = 0
    unread: int = 0
    latest: str = ""


@dataclass
class CommentResult:
    ok: bool
    message: str
    comments: list[Comment] = field(default_factory=list)


def _get(row, name: str):
    """列が無い行(古い手元DB・一部の列だけの行)でも落ちない読み方。"""
    return row[name] if name in row.keys() else None


def order_key(row: sqlite3.Row | dict) -> str:
    """その発注のコメントを結ぶ鍵。無ければ空(まだコメントを付けられない)。"""
    key = str(_get(row, "発注キー") or "").strip()
    if key:
        return key
    source = _get(row, "取込元管理番号")
    if source not in (None, "", 0, "0"):
        return f"#{int(source)}"
    return ""


def _order(conn: sqlite3.Connection, mgr_no: int) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(f"SELECT * FROM {ORDER_TABLE} WHERE 管理番号 = ?",
                        (mgr_no,)).fetchone()


def closed_why(row: sqlite3.Row) -> str:
    """その発注にもう書けない理由。書けるなら空。"""
    if (_get(row, "取り消し済") or "") == "1":
        return "取り消し済みの発注には書けません(記録として残しています)。"
    if (_get(row, "確認済み") or "") == "1":
        return "倉庫が確認済みの発注には書けません(記録として残しています)。"
    if not order_key(row):
        return "この発注は、取り込み元へ届いてからコメントを付けられます。"
    return ""


def comments_for(conn: sqlite3.Connection, mgr_no: int, *,
                 terminal: str) -> list[Comment]:
    """その発注のコメント(古い順)。**読んだことにはしない**(`mark_read`)。"""
    row = _order(conn, mgr_no)
    if row is None:
        return []
    return _comments(conn, order_key(row), terminal)


def _comments(conn: sqlite3.Connection, key: str, terminal: str) -> list[Comment]:
    if not key:
        return []
    read = _read_ids(conn)
    rows = conn.execute(
        f"SELECT コメントID, 書いた端末, 書いた側, 本文, 書いた日時 FROM {TABLE}"
        " WHERE 発注キー = ? ORDER BY 書いた日時, 管理番号", (key,)).fetchall()
    out = []
    for cid, who, side, text, at in rows:
        mine = _same(who, terminal)
        out.append(Comment(comment_id=cid, terminal=who, side=side, text=text,
                           written_at=at, mine=mine,
                           unread=not mine and cid not in read))
    return out


def mark_read(conn: sqlite3.Connection, mgr_no: int) -> None:
    """その発注のコメントを、この端末で読んだことにする。"""
    row = _order(conn, mgr_no)
    if row is None or not order_key(row):
        return
    with conn:
        conn.execute(
            f"INSERT OR IGNORE INTO {READ_TABLE} (コメントID)"
            f" SELECT コメントID FROM {TABLE} WHERE 発注キー = ? AND コメントID <> ''",
            (order_key(row),))


def add(conn: sqlite3.Connection, mgr_no: int, text: str, *,
        terminal: str, side: str) -> CommentResult:
    """書く。**未確認の発注にだけ**。書いたものは自分では既読。"""
    text = db.sanitize_for_db(text or "").strip()
    if not text:
        return CommentResult(False, "コメントを入れてください。")
    if len(text) > MAX_LENGTH:
        return CommentResult(False, f"コメントは{MAX_LENGTH}文字までです(いま{len(text)}文字)。")
    row = _order(conn, mgr_no)
    if row is None:
        return CommentResult(False, "この発注は一覧にありません。画面を更新してください。")
    why = closed_why(row)
    if why:
        return CommentResult(False, why, _comments(conn, order_key(row), terminal))
    cid = uuid.uuid4().hex
    with conn:
        conn.execute(
            f"INSERT INTO {TABLE} (発注キー, コメントID, 書いた端末, 書いた側, 本文, 書いた日時)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (order_key(row), cid, terminal, side, text, db.now_db_string()))
        conn.execute(f"INSERT OR IGNORE INTO {READ_TABLE} (コメントID) VALUES (?)", (cid,))
    log.info("コメントを書きました: 発注キー=%s 端末=%s(%s)", order_key(row), terminal, side)
    return CommentResult(True, "コメントを書きました。",
                         _comments(conn, order_key(row), terminal))


def summaries(conn: sqlite3.Connection, *, terminal: str) -> dict[str, Summary]:
    """発注キーごとのまとめ(件数・未読・いちばん新しい1件)。一覧に添える。"""
    read = _read_ids(conn)
    out: dict[str, Summary] = {}
    try:
        rows = conn.execute(
            f"SELECT 発注キー, コメントID, 書いた端末, 本文 FROM {TABLE}"
            " ORDER BY 書いた日時, 管理番号").fetchall()
    except sqlite3.Error:
        return out                     # 表がまだ無い(古い手元DB)
    for key, cid, who, text in rows:
        got = out.setdefault(key, Summary())
        got.count += 1
        got.latest = text
        if not _same(who, terminal) and cid not in read:
            got.unread += 1
    return out


def _read_ids(conn: sqlite3.Connection) -> set[str]:
    try:
        return {r[0] for r in conn.execute(f"SELECT コメントID FROM {READ_TABLE}")}
    except sqlite3.Error:
        return set()                   # 表がまだ無い(古い手元DB)


def _same(a: str, b: str) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def to_dict(comment: Comment) -> dict:
    return {"id": comment.comment_id, "terminal": comment.terminal,
            "side": comment.side,
            # 画面が色を分けるための鍵(字の「現場」「倉庫」で比べさせない)
            "side_key": "material" if comment.side == SIDE_MATERIAL else "field",
            "text": comment.text,
            "written_at": comment.written_at, "mine": comment.mine,
            "unread": comment.unread}
