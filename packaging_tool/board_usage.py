"""ボードの使用実績 ── 実際に使ったものだけを記録する

【何をもって「使用」とするか】
**配置してあり、かつ「使用する」を押したとき**だけ積みます。

以前は配置図を印刷したときに積んでいました。ですが印刷は
「確かめるために出す」ことも「出さずに使う」こともあり、押した人の
意図と一致しません(現場の指摘:「何をもって使用なのか決めていない」)。
数える入口は**1つだけ**にして、押した人が「これを使う」と言った
ときに積みます。

【何を残すか】
ボードの寸法だけでは、なぜそのサイズが多いのかを説明できません。
**製品とパレットの寸法も一緒に**残すので、「この製品・このパレットの
ときに何を使ったか」が後から辿れます。

    1行 = 「使用する」を1回押した、そのときの1寸法

集計はこの表を数えて出します ── 同じ事実を「明細」と「集計」の2か所に
持つと、片方だけ直ったときにどちらが本当か分からなくなります。

集計単位は 幅×丈×ボードタイプ。上用/下用は物理的には同じ板なので
分けません(現場の指示)。
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from . import db
from .logging_utils import get_logger
from .models import PlacedBoardModel

log = get_logger("board_usage")

TABLE = "ボード使用実績"


@dataclass
class UsageRow:
    """1寸法ぶんの集計。使用実績の一覧に出す。"""

    width: int
    length: int
    board_type: str
    usage_count: int             # 累計の枚数
    last_used_at: str


@dataclass
class RateRow:
    """ボード一覧1行ぶんの使用率。**使っていない行も出す。**

    使われていないサイズが分かることに意味があります ── 一覧に載って
    いるのに一度も使われていないなら、持たなくてよいかもしれません。
    出さなければ「無い」のか「0回」なのか区別できません。
    """

    width: int
    length: int
    board_type: str
    sheets: int = 0              # 使った枚数(累計)
    times: int = 0              # 「使用する」を押した回数
    share: float = 0.0           # 全使用枚数に占める割合(0〜100)
    last_used_at: str = ""

    @property
    def used(self) -> bool:
        return self.sheets > 0


@dataclass
class RateSummary:
    """ボード一覧ぜんぶに対する使用の広がり。

    「登録55件のうち12件が使われています」という一文が、一覧を全部
    読まなくても現状を言い当てます。
    """

    rows: list[RateRow]
    listed: int = 0              # ボード一覧に載っている寸法の数
    used: int = 0               # そのうち一度でも使われた数
    total_sheets: int = 0        # 使った枚数の合計
    unlisted: list[RateRow] = None  # 一覧に無いのに使われた寸法

    def __post_init__(self) -> None:
        if self.unlisted is None:
            self.unlisted = []

    @property
    def coverage(self) -> float:
        """一覧のうち何%が使われているか。"""
        return round(self.used * 100 / self.listed, 1) if self.listed else 0.0


def _listed_sizes(conn: sqlite3.Connection, board_type: str) -> set[tuple[int, int]]:
    """ボード一覧に載っている寸法(向きそのまま)。"""
    rows = db.fetch_all(
        conn,
        "SELECT ボード幅, ボード丈 FROM BoardMaster WHERE ボードタイプ = ?",
        (board_type,), caller_name="board_usage._listed_sizes") or []
    return {(r["ボード幅"], r["ボード丈"]) for r in rows}


def _as_listed(size: tuple[int, int], listed: set[tuple[int, int]]) -> tuple[int, int]:
    """寸法の向きを、ボード一覧の書き方にそろえる。

    配置は板を回して置くので、同じ板でも 900×1800 と 1800×900 の
    両方が出てきます。そのまま積むと**同じ板が2行に割れて**、
    どちらも一覧と突き合わないことがあります。

    ひっくり返した側だけが一覧にあるならそちらを採ります。**両方ある
    ときは触りません** ── その2つは一覧が別物として持っている寸法で、
    こちらで勝手に寄せると、載せたつもりのない行に数が積まれます。
    """
    flipped = (size[1], size[0])
    if size in listed:
        return size
    return flipped if flipped in listed else size


def record_usage(conn: sqlite3.Connection, placed: list[PlacedBoardModel],
                 board_type: str, *,
                 product: tuple[int, int] = (0, 0),
                 palette: tuple[int, int] = (0, 0),
                 lot: str = "") -> int:
    """「使用する」1回ぶんを記録する。戻り値は積んだ枚数(通知用)。

    `placed` は物理的な板1枚につき1件(`PlacedBoardModel`)なので、
    同じ幅×丈が複数あればそのまま複数枚として数えます。

    【積むのは「棚から取った板」の寸法】
    カットして使った板も、消費したのは**カット前の1枚**です
    (`original_width`/`original_length`)。カット後の寸法で積むと、
    ボード一覧に無い寸法ばかりが並び、何を何枚持っておけばよいのかが
    読めなくなります。カット後の寸法は別に添えます。

    製品・パレットの寸法は分からなければ0で構いません(0は
    「分からない」の意味)。**押せた以上は記録する** ── 寸法が
    揃っていないことを理由に黙って捨てると、押した人には積まれた
    ように見えて数だけが合わなくなります。
    """
    if not placed:
        return 0

    listed = _listed_sizes(conn, board_type)
    counts: Counter = Counter()
    for board in placed:
        # 元寸法が入っていない古い経路のために、無ければ実寸で代える
        taken = (board.original_width or board.width,
                 board.original_length or board.length)
        cut = ((board.width, board.length)
               if (board.width, board.length) != taken else (0, 0))
        counts[(_as_listed(taken, listed), cut)] += 1

    now = db.now_db_string()
    for (taken, cut), qty in counts.items():
        db.insert_record(
            conn, TABLE,
            {"ボード幅": taken[0], "ボード丈": taken[1], "ボードタイプ": board_type,
             "枚数": qty,
             "切断後幅": cut[0], "切断後丈": cut[1],
             "製品幅": product[0], "製品丈": product[1],
             "パレット幅": palette[0], "パレット丈": palette[1],
             "ロット番号": lot, "使用日時": now},
            caller_name="board_usage.record_usage")

    conn.commit()
    total = sum(counts.values())
    log.info("board_usage.record_usage: %s種類 計%s枚 (タイプ=%s 製品=%s×%s "
             "パレット=%s×%s ロット=%s)",
             len(counts), total, board_type,
             product[0], product[1], palette[0], palette[1], lot or "—")
    return total


def list_usage(conn: sqlite3.Connection) -> list[UsageRow]:
    """使用枚数の多い順。選定画面の実績一覧に出す。"""
    rows = db.fetch_all(
        conn,
        "SELECT ボード幅, ボード丈, ボードタイプ, "
        "       SUM(枚数) AS 枚数, MAX(使用日時) AS 最終使用日時 "
        f"FROM {TABLE} "
        "GROUP BY ボード幅, ボード丈, ボードタイプ "
        "ORDER BY 枚数 DESC, 最終使用日時 DESC",
        caller_name="board_usage.list_usage") or []
    return [UsageRow(width=r["ボード幅"], length=r["ボード丈"],
                     board_type=r["ボードタイプ"],
                     usage_count=r["枚数"] or 0,
                     last_used_at=r["最終使用日時"] or "")
            for r in rows]


def usage_rates(conn: sqlite3.Connection,
                board_type: str = "") -> RateSummary:
    """**ボード一覧を軸にした**使用率。設定画面に出す。

    分母はボードマスタに載っている寸法ぜんぶです。使われていない行も
    0%で並べます ── 「一覧に載っているのに使っていないサイズ」を
    見つけるのがこの表の目的なので、そこを隠すと意味がなくなります。

    一覧に**無いのに使われた**寸法は別に添えます。マスタに足し忘れて
    いるか、寸法を打ち間違えているかのどちらかで、どちらも直すべき
    事実です(黙って落とすと気づけません)。
    """
    where = "WHERE ボードタイプ = ?" if board_type else ""
    args: tuple = (board_type,) if board_type else ()

    listed = db.fetch_all(
        conn,
        "SELECT ボード幅, ボード丈, ボードタイプ FROM BoardMaster "
        + ("WHERE ボードタイプ = ?" if board_type else "")
        + " ORDER BY ボードタイプ, ボード幅, ボード丈",
        args, caller_name="board_usage.usage_rates.listed") or []

    used = db.fetch_all(
        conn,
        "SELECT ボード幅, ボード丈, ボードタイプ, "
        "       SUM(枚数) AS 枚数, COUNT(*) AS 回数, "
        "       MAX(使用日時) AS 最終使用日時 "
        f"FROM {TABLE} {where} "
        "GROUP BY ボード幅, ボード丈, ボードタイプ",
        args, caller_name="board_usage.usage_rates.used") or []

    by_key = {(r["ボード幅"], r["ボード丈"], r["ボードタイプ"]): r for r in used}
    total_sheets = sum((r["枚数"] or 0) for r in used)

    def share(sheets: int) -> float:
        return round(sheets * 100 / total_sheets, 1) if total_sheets else 0.0

    rows: list[RateRow] = []
    seen: set[tuple] = set()
    for row in listed:
        key = (row["ボード幅"], row["ボード丈"], row["ボードタイプ"])
        if key in seen:
            continue                              # マスタの重複行は1つに
        seen.add(key)
        hit = by_key.get(key)
        sheets = (hit["枚数"] or 0) if hit else 0
        rows.append(RateRow(
            width=key[0], length=key[1], board_type=key[2],
            sheets=sheets, times=(hit["回数"] if hit else 0),
            share=share(sheets),
            last_used_at=(hit["最終使用日時"] or "") if hit else ""))

    # 使った順に並べる。**0回のものは下にまとめて置く**(探すのは
    # 「よく使うもの」が先で、「使っていないもの」は眺めるだけ)
    rows.sort(key=lambda r: (-r.sheets, r.board_type, r.width, r.length))

    unlisted = [
        RateRow(width=k[0], length=k[1], board_type=k[2],
                sheets=(r["枚数"] or 0), times=r["回数"],
                share=share(r["枚数"] or 0),
                last_used_at=r["最終使用日時"] or "")
        for k, r in by_key.items() if k not in seen]
    unlisted.sort(key=lambda r: -r.sheets)

    return RateSummary(rows=rows, listed=len(rows),
                       used=sum(1 for r in rows if r.used),
                       total_sheets=total_sheets, unlisted=unlisted)


def board_types(conn: sqlite3.Connection) -> list[str]:
    """ボード一覧にある種別。設定画面の絞り込みに出す。"""
    rows = db.fetch_all(
        conn,
        "SELECT DISTINCT ボードタイプ FROM BoardMaster "
        "WHERE ボードタイプ <> '' ORDER BY ボードタイプ",
        caller_name="board_usage.board_types") or []
    return [r["ボードタイプ"] for r in rows]
