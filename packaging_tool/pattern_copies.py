"""実績の**写し**(同じ1件の実績が2件以上入ったもの)を見分ける。VER4.8.119

現場の共有の梱包資材マスタで、実績 37,161件のうち 37,152件が、2026-09-23 に覚えた2件
(1350×1450・ロット H7085M0 の「別案A」と「通常」)の写しだった。

【なぜ増えたか】
実績の取り込み(`pattern_sync.import_from`)は、共有に実績の表が無ければ「共有を作り直した」と
見て、手元の実績を「未送信」に戻して送り直す。ところが表の一覧を引く `source_db.list_tables`
は**読めないときに空を返していた**ので、共有を読めなかっただけの回も「表が無い」になり、
そのたびに手元の実績を送り直していた。送り直しの二重は送信IDで防いでいたが、その2件は送信ID が
できる前の実績で送信ID を持っていない。だから送るたびに1件ずつ増えた(送信ID のある実績は
1件も増えていない)。直したのは:
  - 実績の取り込みは表の一覧を `strict=True` で引く(読めなければ送り直さない)
  - 送るとき、送信ID の無い実績には送信ID を付けてから送る
  - 送る前に、共有に**同じ中身の実績**(登録日時・ヘッダ・明細が同じ)があれば、足さずにそれとつなぐ

【何を写しとみなすか(ここ1か所)】
ヘッダの列のうち 送信ID・使用回数・更新日時 を除いた全部(**登録日時の秒まで**)が同じで、
明細3つ(選定・配置・カット)の行も全部同じもの。組の中で**いちばん小さい実績ID**を残す
(先に入ったほうが元)。使用回数は組の中の大きいほうを残す。
登録日時が秒まで同じで中身も同じ実績を、人が2回覚えることはできないので、別の実績を消すことはない。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import config, source_db
from . import pattern_store as store

H = config.TBL_PT_HEADER
# 比べないヘッダの列。送るたびに振る印と、読むたびに変わるもの
SKIP = ("送信ID", "使用回数", "更新日時")
_HEADER_TYPES = dict(store.HEADER_COLUMNS)
KEY_NAMES: tuple[str, ...] = tuple(n for n, _ in store.HEADER_COLUMNS if n not in SKIP)
# 明細を1件ずつ引くか、表をまとめて読むかの境目
FEW = 50


def _q(name: str) -> str:
    return source_db.quote_identifier(name)


def norm(value: Any, kind: str = "TEXT") -> Any:
    """比べる形にそろえる。Access から来た型の無い列(数が文字で入る)でも同じになるように。"""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if kind.upper().startswith("INTEGER"):
            try:
                number = float(value)
            except ValueError:
                return value
            return int(number) if number.is_integer() else number
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def header_key(row: dict[str, Any]) -> tuple:
    return tuple(norm(row.get(n), _HEADER_TYPES[n]) for n in KEY_NAMES)


def _row_hash(table: str, values: tuple) -> int:
    digest = hashlib.blake2b(repr((table, values)).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def detail_signatures(conn: sqlite3.Connection, ids: Iterable[int]) -> dict[int, str]:
    """実績ごとの明細の中身の印。行の並びは見ない(同じ行が同じ数あれば同じ)。

    少ないときは実績ごとに引き、多いときは表を1度だけ流し読みする(写しが何万件あっても
    明細をまるごと覚えない。1行ごとの小さな数だけ持つ)。
    """
    wanted = {int(i) for i in ids}
    rows: dict[int, list[int]] = {pid: [] for pid in wanted}
    for table, columns, _order in store.DETAIL_TABLES:
        try:
            if len(wanted) <= FEW:
                cursors = [conn.execute(f"SELECT * FROM {_q(table)} WHERE 実績ID IN (?, ?)",
                                        (pid, str(pid))) for pid in sorted(wanted)]
            else:
                cursors = [conn.execute(f"SELECT * FROM {_q(table)}")]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                continue                              # その明細の表がまだ無い
            raise
        for cursor in cursors:
            names = [d[0] for d in cursor.description or []]
            at_id = names.index("実績ID")
            cols = [(names.index(n), kind) for n, kind in columns if n in names]
            for raw in cursor:
                pid = store.pt_long(raw[at_id])
                if pid in rows:
                    rows[pid].append(_row_hash(table, tuple(norm(raw[i], k) for i, k in cols)))
    return {pid: hashlib.blake2b(repr(sorted(found)).encode("ascii"), digest_size=16).hexdigest()
            for pid, found in rows.items()}


@dataclass
class Group:
    keep: int                     # 残す実績ID
    copies: list[int]             # 消す実績ID
    label: str
    usage: int                    # 残す実績に入れる使用回数(組の中の大きいほう)


@dataclass
class Copies:
    total: int = 0                # 実績ぜんぶの件数
    groups: list[Group] = field(default_factory=list)

    @property
    def drop(self) -> list[int]:
        return sorted(pid for g in self.groups for pid in g.copies)

    def describe(self) -> list[str]:
        return [f"{g.label}: 写し {len(g.copies)}件(実績ID {g.keep} を残す)" for g in self.groups]


def label(row: dict[str, Any]) -> str:
    return (f"{store.nv_str(row.get('登録日時'))} パレット{store.pt_long(row.get('パレット幅'))}"
            f"×{store.pt_long(row.get('パレット丈'))} 製品{store.pt_long(row.get('製品幅'))}"
            f"×{store.pt_long(row.get('製品丈'))} ロット{store.nv_str(row.get('LotNo'))}"
            f" {store.nv_str(row.get('配置方式'))}")


def find(conn: sqlite3.Connection) -> Copies:
    """接続(共有の写し・共有への書き込みの鍵の中・手元の作業用DB)の実績の写しを数える。

    実績の表が無ければ空(数えるものが無い)。
    """
    try:
        cursor = conn.execute(f"SELECT * FROM {_q(H)}")
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return Copies()
        raise
    names = [d[0] for d in cursor.description or []]
    groups: dict[tuple, list[tuple[int, int, dict[str, Any]]]] = {}
    total = 0
    for raw in cursor:
        row = dict(zip(names, raw))
        pid = store.pt_long(row.get("実績ID"))
        if pid <= 0:
            continue
        total += 1
        groups.setdefault(header_key(row), []).append((pid, store.pt_long(row.get("使用回数")), row))
    many = [g for g in groups.values() if len(g) > 1]
    out = Copies(total=total)
    if not many:
        return out
    signs = detail_signatures(conn, [pid for g in many for pid, _u, _r in g])
    for members in many:
        by_sign: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
        for member in members:
            by_sign.setdefault(signs.get(member[0], ""), []).append(member)
        for same in by_sign.values():
            if len(same) < 2:
                continue
            same.sort(key=lambda m: m[0])
            out.groups.append(Group(keep=same[0][0], copies=[m[0] for m in same[1:]],
                                    label=label(same[0][2]), usage=max(m[1] for m in same)))
    out.groups.sort(key=lambda g: g.keep)
    return out


def remove(conn: sqlite3.Connection, copies: Copies) -> dict[str, int]:
    """写しを消す(呼び手のトランザクションの中で)。表ごとに消した行数を返す。

    消す番号は手元の一時の表に入れて、明細の表は1度ずつ流すだけにする(何万件でも往復しない)。
    """
    drop = copies.drop
    removed: dict[str, int] = {}
    if not drop:
        return removed
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS __pt_drop (id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM temp.__pt_drop")
    conn.executemany("INSERT OR IGNORE INTO temp.__pt_drop (id) VALUES (?)", [(pid,) for pid in drop])
    try:
        for table, _cols, _order in store.DETAIL_TABLES:
            try:
                n = conn.execute(f"DELETE FROM {_q(table)} WHERE CAST(実績ID AS INTEGER)"
                                 " IN (SELECT id FROM temp.__pt_drop)").rowcount
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    continue
                raise
            if n:
                removed[table] = n
        removed[H] = conn.execute(f"DELETE FROM {_q(H)} WHERE CAST(実績ID AS INTEGER)"
                                  " IN (SELECT id FROM temp.__pt_drop)").rowcount
        for group in copies.groups:
            conn.execute(f"UPDATE {_q(H)} SET 使用回数 = ? WHERE 実績ID IN (?, ?)"
                         " AND COALESCE(CAST(使用回数 AS INTEGER), 0) < ?",
                         (group.usage, group.keep, str(group.keep), group.usage))
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.__pt_drop")
    return removed


def same_in(conn: sqlite3.Connection, header: dict[str, Any], sign: str) -> Optional[int]:
    """`conn`(共有への書き込みの鍵の中)に、`header` と同じ中身の実績があればその実績ID。

    登録日時で絞ってから、ヘッダと明細を比べる。いちばん小さい番号を返す(写しがあるときは元)。
    """
    when = header.get("登録日時")
    if when is None or str(when).strip() == "":
        return None
    try:
        cursor = conn.execute(f"SELECT * FROM {_q(H)} WHERE 登録日時 = ?", (when,))
    except sqlite3.OperationalError:
        return None
    names = [d[0] for d in cursor.description or []]
    key = header_key(header)
    candidates = sorted(store.pt_long(r.get("実績ID")) for r in (dict(zip(names, raw)) for raw in cursor)
                        if header_key(r) == key)
    for pid in candidates:
        if pid > 0 and detail_signatures(conn, [pid]).get(pid) == sign:
            return pid
    return None


# 手元の数え(設定の「いまの状態」)。実績が変わったときだけ数え直す
_local_cache: dict[str, tuple[tuple, int]] = {}


def local_count(conn: sqlite3.Connection) -> int:
    """手元の作業用DBの実績の写しの件数。実績の件数・最大の番号が同じあいだは前の数を使う。"""
    try:
        stamp = tuple(conn.execute(f"SELECT COUNT(*), MAX(実績ID) FROM {_q(H)}").fetchone())
    except sqlite3.Error:
        return 0
    known = _local_cache.get("local")
    if known is not None and known[0] == stamp:
        return known[1]
    try:
        count = len(find(conn).drop)
    except sqlite3.Error:
        return 0
    _local_cache["local"] = (stamp, count)
    return count
