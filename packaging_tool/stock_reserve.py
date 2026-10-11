"""発注に在庫を充てる(引当)と、置き場から持ち出したこと(持出)の記録

【何をするか】
倉庫連携で送る発注のパレットと同じ幅×丈の在庫が簡易在庫にあれば、
「在庫分を発注数から引きますか?」と聞く。引くと

    1. 発注数を在庫の分だけ減らして送る
    2. どの置き場から何台使うかを、この表に「引当」として1行ずつ残す
    3. 簡易在庫の図のその置き場に「持出」の印が付く
    4. その置き場でそのサイズを払い出すと「持出」を足し、印が減る

【在庫は幅×丈だけで見る】
受け入れ・払い出しが持っているのはサイズ(と置き場)だけで、記号や発注コードは
在庫の行に入らない(現場の確認)。記号違いで発注コードが違うパレットでも、
幅×丈が同じなら同じ在庫として充てる。

【表は足すだけ】
状態を書き直さない(切断依頼の状態と同じ形)。共有へは書き戻しで送り、取り込みで
全端末に配る。同じ在庫を2台の端末が同時に払い出しても、足した行を数え直すだけで
済む(負の数にはしない)。

【取り消した発注は数えない】
発注を取り消すと、その発注の引当は印から消える。取り消しの印は発注の表にあるので、
こちらには何も書かない(書き忘れで印が残ることがない)。

【PalletMaster には何も足さない】
在庫の行は 0 台になると消え、取り込みのたびに総入れ替えになる。同じ置き場の在庫を
2つのロットが別々に引くこともあるので、在庫の行に印の列を持たせると失われる。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config, db
from .logging_utils import get_logger

log = get_logger("stock_reserve")

TABLE = config.TBL_STOCK_RESERVE
ORDER_TABLE = config.TBL_WAREHOUSE_ORDER

KIND_RESERVE = "引当"
KIND_TAKEN = "持出"


def place_key(name: Any) -> str:
    """置き場の突き合わせ用。前後の空白と大小文字を無視する(簡易在庫の図と同じ)。"""
    return str(name or "").strip().upper()


def _size(value: Any) -> Any:
    """寸法を数として比べる形にする(2.5 は 2.5 のまま、1100.0 は 1100)。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


# ------------------------------------------------------------------
# 引当の残り
# ------------------------------------------------------------------
def _cancelled_keys(conn: sqlite3.Connection) -> set[str]:
    rows = db.fetch_all(
        conn,
        f"SELECT 発注キー FROM [{ORDER_TABLE}]"
        " WHERE TRIM(CAST(COALESCE(取り消し済, '') AS TEXT)) = '1'"
        "   AND TRIM(COALESCE(発注キー, '')) <> ''",
        caller_name="stock_reserve.cancelled") or []
    return {r["発注キー"].strip() for r in rows}


@dataclass
class Waiting:
    """1つの発注が1つの置き場で、まだ持ち出していない台数。"""

    order_key: str
    lot_no: str
    width: Any
    length: Any
    position: str
    count: int
    since: str = ""          # いちばん古い引当の日時(持出はこの順に充てる)


def waiting(conn: sqlite3.Connection) -> list[Waiting]:
    """まだ持ち出していない引当。発注ごと・置き場ごと。古い順。

    引当 − 持出 が 0 以下のものは出さない(2台が同時に払い出して持出が
    多すぎても、負の数にはしない)。取り消した発注は数えない。
    """
    rows = db.fetch_all(
        conn,
        f"SELECT 発注キー, LotNo, 幅, 丈, 位置, 区分, 台数, 日時 FROM [{TABLE}]"
        " ORDER BY 日時, 管理番号",
        caller_name="stock_reserve.waiting") or []
    cancelled = _cancelled_keys(conn)
    found: dict[tuple, Waiting] = {}
    for r in rows:
        key = (r["発注キー"] or "").strip()
        if not key or key in cancelled:
            continue
        place = (key, _size(r["幅"]), _size(r["丈"]), place_key(r["位置"]))
        item = found.get(place)
        if item is None:
            item = found[place] = Waiting(
                order_key=key, lot_no=(r["LotNo"] or "").strip(),
                width=_size(r["幅"]), length=_size(r["丈"]),
                position=(r["位置"] or "").strip(), count=0)
        count = int(r["台数"] or 0)
        if r["区分"] == KIND_RESERVE:
            item.count += count
            if not item.since:
                item.since = r["日時"] or ""
        elif r["区分"] == KIND_TAKEN:
            item.count -= count
    items = [w for w in found.values() if w.count > 0]
    items.sort(key=lambda w: (w.since, w.order_key, place_key(w.position)))
    return items


def waiting_by_place(conn: sqlite3.Connection) -> dict[tuple, int]:
    """(幅, 丈, 置き場) ごとの持ち出し待ちの台数(発注をまたいだ合計)。"""
    totals: dict[tuple, int] = {}
    for w in waiting(conn):
        place = (w.width, w.length, place_key(w.position))
        totals[place] = totals.get(place, 0) + w.count
    return totals


def waiting_by_position(conn: sqlite3.Connection) -> dict[str, int]:
    """置き場ごとの持ち出し待ちの台数(サイズをまたいだ合計)。図の印に使う。"""
    totals: dict[str, int] = {}
    for w in waiting(conn):
        totals[place_key(w.position)] = totals.get(place_key(w.position), 0) + w.count
    return totals


# ------------------------------------------------------------------
# 使える在庫
# ------------------------------------------------------------------
@dataclass
class Place:
    """1つの置き場にある、そのサイズの在庫。"""

    position: str
    stock: int               # 在庫数
    waiting: int             # そのうち、ほかの発注が持ち出し待ちにしている台数
    free: int                # 今回使える台数(在庫数 − 持ち出し待ち。0 未満にしない)


@dataclass
class Offer:
    """そのサイズで使える在庫。**少ない置き場から**並べる(置き場を空けて散らばりを減らす)。"""

    width: Any
    length: Any
    places: list[Place] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(p.free for p in self.places)

    def to_dict(self) -> dict[str, Any]:
        return {"width": self.width, "length": self.length, "total": self.total,
                "places": [{"position": p.position, "stock": p.stock,
                            "waiting": p.waiting, "free": p.free} for p in self.places]}


def offer(conn: sqlite3.Connection, width: Any, length: Any) -> Offer:
    """そのサイズで今回使える在庫(置き場ごと)。使える台数が0の置き場は出さない。"""
    from . import pallet_service
    width, length = _size(width), _size(length)
    rows = db.fetch_all(
        conn,
        "SELECT 位置, 在庫数 FROM PalletMaster"
        f" WHERE TRIM(位置) <> '' AND 幅 = ? AND 丈 = ? AND {pallet_service.IN_STOCK}",
        (width, length), caller_name="stock_reserve.offer") or []
    stock: dict[str, list] = {}
    for r in rows:
        key = place_key(r["位置"])
        name, count = stock.get(key, [(r["位置"] or "").strip(), 0])
        stock[key] = [name, count + int(r["在庫数"] or 0)]
    held = waiting_by_place(conn)
    places = []
    for key, (name, count) in stock.items():
        busy = held.get((width, length, key), 0)
        free = max(count - busy, 0)
        if free > 0:
            places.append(Place(position=name, stock=count, waiting=busy, free=free))
    places.sort(key=lambda p: (p.free, place_key(p.position)))
    return Offer(width=width, length=length, places=places)


@dataclass
class Take:
    """1つの置き場から使う台数。"""

    position: str
    count: int


def plan(found: Offer, need: int) -> list[Take]:
    """`need` 台を、少ない置き場から使い切る順に割り当てる。足りなければあるだけ。"""
    takes: list[Take] = []
    left = max(int(need or 0), 0)
    for place in found.places:
        if left <= 0:
            break
        count = min(place.free, left)
        takes.append(Take(position=place.position, count=count))
        left -= count
    return takes


def describe(takes: list[Take]) -> str:
    """「A1 2台・S1 1台」。"""
    return "・".join(f"{t.position} {t.count}台" for t in takes)


def comment_text(width: Any, length: Any, takes: list[Take], *, before: int,
                 after: int) -> str:
    """倉庫へ自動で付けるコメント。"""
    used = sum(t.count for t in takes)
    return (f"在庫から {used}台 使います({_size(width)}×{_size(length)}: {describe(takes)})。"
            f"発注数 {before} → {after}")


# ------------------------------------------------------------------
# 書く(呼び出し側のトランザクションの中で。ここでは確定しない)
# ------------------------------------------------------------------
def write_reserve(conn: sqlite3.Connection, *, order_key: str, lot_no: str,
                  width: Any, length: Any, takes: list[Take], terminal: str,
                  now: Optional[str] = None) -> None:
    """引当を置き場ごとに1行足す。発注と同じトランザクションで呼ぶ。"""
    now = now or db.now_db_string()
    for take in takes:
        if take.count <= 0:
            continue
        conn.execute(
            f"INSERT INTO [{TABLE}] (引当ID, 発注キー, LotNo, 幅, 丈, 位置, 区分, 台数, 端末, 日時)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, order_key, lot_no, _size(width), _size(length),
             take.position, KIND_RESERVE, take.count, terminal, now))
    log.info("在庫を引き当てました: Lot %s %s×%s %s", lot_no, width, length, describe(takes))


@dataclass
class Consumed:
    """払い出しで済みにした持ち出し待ち。"""

    lot_no: str
    count: int


def consume(conn: sqlite3.Connection, *, width: Any, length: Any, position: str,
            qty: int, terminal: str, now: Optional[str] = None) -> list[Consumed]:
    """その置き場でそのサイズを払い出した。**古い引当から順に**持ち出したことにする。

    払い出しと同じトランザクションで呼ぶ。持ち出し待ちが無ければ何もしない
    (ふつうの払い出し)。払い出した台数が持ち出し待ちより多くても、充てるのは
    待っている分だけ。
    """
    now = now or db.now_db_string()
    width, length, key = _size(width), _size(length), place_key(position)
    left = max(int(qty or 0), 0)
    done: list[Consumed] = []
    for item in waiting(conn):
        if left <= 0:
            break
        if (item.width, item.length, place_key(item.position)) != (width, length, key):
            continue
        count = min(item.count, left)
        conn.execute(
            f"INSERT INTO [{TABLE}] (引当ID, 発注キー, LotNo, 幅, 丈, 位置, 区分, 台数, 端末, 日時)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, item.order_key, item.lot_no, width, length,
             item.position, KIND_TAKEN, count, terminal, now))
        done.append(Consumed(lot_no=item.lot_no, count=count))
        left -= count
    if done:
        log.info("持ち出し待ちを済みにしました: %s×%s 位置%s %s", width, length, position,
                 "・".join(f"Lot {d.lot_no} {d.count}台" for d in done))
    return done


def consumed_note(done: list[Consumed]) -> str:
    """払い出しの知らせに添える一言。"""
    if not done:
        return ""
    return ("持ち出し待ちを済みにしました("
            + "・".join(f"Lot {d.lot_no or '---'} {d.count}台" for d in done) + ")。")
