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


# ------------------------------------------------------------------
# 共有の側の守り: 「同じ実績は1件だけ」(VER4.8.120)
# ------------------------------------------------------------------
# VER4.8.1 で発注・コメントなど7つの表に付けた「同じ番号は1つだけ」と同じ考え。**共有のファイルそのもの**に
# 一意索引を付けるので、古い版の端末から送られても・ツールを通さずに書かれても、同じ実績の2件目は入らない。
# 実績には一生変わらない番号が無い(送信ID は送るたびに振る印)ので、**中身**で決める:
# 写しの決まり(`KEY_NAMES`。登録日時の秒・ヘッダの中身)が同じなら同じ実績。空欄は '' に寄せる
# (一意索引は NULL どうしを別物として扱うので、寄せないと空欄のある実績が素通りする)。
# 登録日時が空の古い行は対象外(その行どうしは比べない)。明細は索引に入らないが、登録日時が秒まで
# 同じで中身が同じ実績を人が2回覚えることはできない。
GUARD_INDEX = "UX_パレット実績ヘッダ_同じ実績"
_GUARD_WHERE = "登録日時 IS NOT NULL AND 登録日時 <> ''"
_guard_cache: dict[str, tuple[Any, bool]] = {}


def _guard_exprs() -> str:
    return ", ".join(f"COALESCE({_q(n)}, '')" for n in KEY_NAMES)


def guard_duplicates(source: Any) -> int:
    """共有で、同じ実績(索引の決まりで)が2件以上ある組の数。これがあると索引を付けられない。"""
    found = source.query(f"SELECT COUNT(*) AS 組 FROM (SELECT 1 FROM {_q(H)} WHERE {_GUARD_WHERE}"
                         f" GROUP BY {_guard_exprs()} HAVING COUNT(*) > 1)")
    return int(found[0]["組"]) if found else 0


def guard_state(source: Any) -> Optional[str]:
    """守りが効いているか。**読むだけ**。効いていれば ""、効いていなければ理由、実績の表がまだ無ければ None。"""
    try:
        if H not in source.table_names():
            return None
        if any(r.get("name") == GUARD_INDEX for r in source.query(f"PRAGMA index_list({_q(H)})")):
            return ""
        have = {r.get("name") for r in source.query(f"PRAGMA table_info({_q(H)})")}
        lacking = [n for n in KEY_NAMES if n not in have]
        if lacking:
            return f"{H} に列がありません({', '.join(lacking)})"
        n = guard_duplicates(source)
    except source_db.SourceError as exc:
        return f"確かめられません({exc})"
    if n:
        return (f"{H} に同じ実績が {n}組 あるので「同じ実績は1件だけ」を付けられません。"
                "「取り込み」の面の「--fix を実行(重複を消す)」で消すと付きます")
    return f"{H} に「同じ実績は1件だけ」がまだ付いていません(次に取り込むか送るときに付けます)"


def ensure_guard(source: Any) -> bool:
    """共有の実績の表に「同じ実績は1件だけ」の一意索引を付ける。付いていれば True。

    付けられないのは、もう同じ実績が2件以上あるとき(写しの残り)か、列が足りないとき。そのときも
    送ることは止めない(画面の「いまの状態」と、画面の記録に1日1件出す)。ファイルの姿ごとに覚えて、
    毎回は試さない。
    """
    from . import outbox_sync, trouble
    from .logging_utils import get_logger
    log = get_logger("pattern_copies")
    key = str(source.path)
    stamp = outbox_sync._file_stamp(source.path)
    cached = _guard_cache.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        if H not in source.table_names():
            return False
        if any(r.get("name") == GUARD_INDEX for r in source.query(f"PRAGMA index_list({_q(H)})")):
            _guard_cache[key] = (stamp, True)
            return True
        have = {r.get("name") for r in source.query(f"PRAGMA table_info({_q(H)})")}
    except source_db.SourceError as exc:
        log.debug("実績の守りを確かめられません: %s", exc)
        return False
    lacking = [n for n in KEY_NAMES if n not in have]
    if lacking:
        trouble.once(log, f"実績の守り|{key}", trouble.KIND_GUARD_OFF,
                     "共有の実績に「同じ実績は1件だけ」を付けられません(列がありません: %s)",
                     ", ".join(lacking), every=86400)
        _guard_cache[key] = (stamp, False)
        return False
    try:
        source.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_q(GUARD_INDEX)} ON {_q(H)}"
                       f" ({_guard_exprs()}) WHERE {_GUARD_WHERE}")
    except source_db.SourceError as exc:
        if source_db.is_lock_error(str(exc)):
            log.debug("実績の守り: ロック競合のため今回は見送ります(次回再試行)")
            return False
        try:
            n = guard_duplicates(source)
        except source_db.SourceError:
            n = 0
        trouble.once(log, f"実績の守り|{key}", trouble.KIND_GUARD_OFF,
                     "共有の実績に「同じ実績は1件だけ」を付けられません(同じ実績が %s組 あります。"
                     "設定の「--fix を実行(重複を消す)」で消すと付きます): %s", n, exc, every=86400)
        _guard_cache[key] = (stamp, False)
        return False
    log.info("共有の実績に「同じ実績は1件だけ」を付けました(%s)", source.path)
    _guard_cache[key] = (outbox_sync._file_stamp(source.path), True)
    return True


def same_header_in(conn: sqlite3.Connection, header: dict[str, Any]) -> Optional[int]:
    """守りの決まり(ヘッダの中身)で同じ実績があればその実績ID(いちばん小さい番号)。"""
    when = header.get("登録日時")
    if when is None or str(when).strip() == "":
        return None
    cursor = conn.execute(f"SELECT * FROM {_q(H)} WHERE 登録日時 = ?", (when,))
    names = [d[0] for d in cursor.description or []]
    key = header_key(header)
    found = sorted(store.pt_long(r.get("実績ID")) for r in (dict(zip(names, raw)) for raw in cursor)
                   if header_key(r) == key)
    return next((pid for pid in found if pid > 0), None)


def forget_guard() -> None:
    """覚えている守りの様子を消す(試験用・片付けのあと)。"""
    _guard_cache.clear()
