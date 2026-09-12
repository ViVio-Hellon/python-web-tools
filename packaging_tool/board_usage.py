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

【被覆率(パレットをどれだけ覆えたか)とは別の話】
ここが答えるのは「**どのサイズを多く持っておけばよいか**」で、
何回ぶんも積み上がって初めて意味を持ちます。
1回の配置がうまく覆えたかどうかは、その場で見るべきことなので、
配置の段に「使用率」「はみ出し」として出ています
(`placement_render.usage_ratio_by_category`)。同じ語で別のものを
2か所に置くと、どちらの話をしているのか読めなくなります。
"""
from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
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
class PopularityRow:
    """ボード一覧1行ぶんの人気度。**使っていない行も出す。**

    一度も使っていないサイズが分かることに意味があります ── 一覧に
    載っているのに使われていないなら、持たなくてよいかもしれません。
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
class Popularity:
    """どのボードがよく使われているか。

    **パレットをどれだけ覆えたか(被覆率)とは別の話です。** そちらは
    1回の配置ごとの出来ばえで、配置の段に「使用率」「はみ出し」として
    出ています。ここで答えるのは「どのサイズを多く持っておけばよいか」
    ── 何回ぶんも積み上がって初めて意味を持つ数です。
    """

    rows: list[PopularityRow]
    total_sheets: int = 0        # 使った枚数の合計
    unlisted: list[PopularityRow] = None  # 一覧に無いのに使われた寸法

    def __post_init__(self) -> None:
        if self.unlisted is None:
            self.unlisted = []

    @property
    def top(self) -> Optional[PopularityRow]:
        """いちばん使われている寸法。まだ1枚も無ければ None。"""
        return self.rows[0] if self.rows and self.rows[0].used else None


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


def popularity(conn: sqlite3.Connection,
               board_type: str = "") -> Popularity:
    """**ボード一覧を軸にした**人気度。設定画面に出す。

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
        args, caller_name="board_usage.popularity.listed") or []

    used = db.fetch_all(
        conn,
        "SELECT ボード幅, ボード丈, ボードタイプ, "
        "       SUM(枚数) AS 枚数, COUNT(*) AS 回数, "
        "       MAX(使用日時) AS 最終使用日時 "
        f"FROM {TABLE} {where} "
        "GROUP BY ボード幅, ボード丈, ボードタイプ",
        args, caller_name="board_usage.popularity.used") or []

    by_key = {(r["ボード幅"], r["ボード丈"], r["ボードタイプ"]): r for r in used}
    total_sheets = sum((r["枚数"] or 0) for r in used)

    def share(sheets: int) -> float:
        return round(sheets * 100 / total_sheets, 1) if total_sheets else 0.0

    rows: list[PopularityRow] = []
    seen: set[tuple] = set()
    for row in listed:
        key = (row["ボード幅"], row["ボード丈"], row["ボードタイプ"])
        if key in seen:
            continue                              # マスタの重複行は1つに
        seen.add(key)
        hit = by_key.get(key)
        sheets = (hit["枚数"] or 0) if hit else 0
        rows.append(PopularityRow(
            width=key[0], length=key[1], board_type=key[2],
            sheets=sheets, times=(hit["回数"] if hit else 0),
            share=share(sheets),
            last_used_at=(hit["最終使用日時"] or "") if hit else ""))

    # 使った順に並べる。**0回のものは下にまとめて置く**(探すのは
    # 「よく使うもの」が先で、「使っていないもの」は眺めるだけ)
    rows.sort(key=lambda r: (-r.sheets, r.board_type, r.width, r.length))

    unlisted = [
        PopularityRow(width=k[0], length=k[1], board_type=k[2],
                      sheets=(r["枚数"] or 0), times=r["回数"],
                      share=share(r["枚数"] or 0),
                      last_used_at=r["最終使用日時"] or "")
        for k, r in by_key.items() if k not in seen]
    unlisted.sort(key=lambda r: -r.sheets)

    return Popularity(rows=rows, total_sheets=total_sheets, unlisted=unlisted)


def board_types(conn: sqlite3.Connection) -> list[str]:
    """ボード一覧にある種別。設定画面の絞り込みに出す。"""
    rows = db.fetch_all(
        conn,
        "SELECT DISTINCT ボードタイプ FROM BoardMaster "
        "WHERE ボードタイプ <> '' ORDER BY ボードタイプ",
        caller_name="board_usage.board_types") or []
    return [r["ボードタイプ"] for r in rows]


# ==================================================================
# CSVに書き出す
# ==================================================================
# 書き出しの列。**画面の表と同じ並び**にする ── 画面で見たものと
# CSVで開いたものが違う並びだと、どちらが正しいのか確かめる手間が増える
CSV_COLUMNS: tuple[str, ...] = (
    "区分", "種別", "幅", "丈", "枚数", "回数", "割合(%)", "最終使用",
)

# 一覧に載っているか。**一覧に無いのに使われた寸法も同じ表に入れる** ──
# 別ファイルにすると、片方だけ配られて「使っていない」と読まれる
KIND_LISTED = "一覧"
KIND_UNLISTED = "一覧に無い"

# 文字の入れ方。**UTF-8 に BOM を付ける(utf-8-sig)。**
#
# Excel は BOM を見て UTF-8 と判断するので、日本語がそのまま開けます。
# Shift-JIS にしないのは、書けない文字が来たときに**黙って化けるか
# 例外で止まるか**のどちらかになるためです ── ボード種別は上流の
# マスタから来る文字で、こちらでは何が入るか決められません。
CSV_ENCODING = "utf-8-sig"

# ファイル名。**日時を入れて上書きしない** ── 上書きすると、前に出した
# ものと見比べられなくなる(増えたのか減ったのかが分からない)
CSV_NAME = "ボード人気度_%Y%m%d_%H%M%S.csv"


def csv_rows(got: Popularity) -> list[list[str]]:
    """CSVの中身(見出しを含む)。画面の表と同じ並び・同じ値。"""
    out: list[list[str]] = [list(CSV_COLUMNS)]
    for kind, rows in ((KIND_LISTED, got.rows), (KIND_UNLISTED, got.unlisted)):
        for row in rows:
            out.append([
                kind, row.board_type, str(row.width), str(row.length),
                str(row.sheets), str(row.times), f"{row.share}",
                row.last_used_at,
            ])
    return out


def write_csv(conn: sqlite3.Connection, directory: Optional[Path] = None,
              *, board_type: str = "") -> Path:
    """ボード人気度をCSVにして書き出す。戻り値は**書いた場所**。

    戻り値を「成功しました」ではなく道そのものにしてあるのは、
    書き出しでいちばん困るのが「書けたのに、どこにあるか分からない」
    だからです。

    フォルダが無ければ作ります ── 既定の書き出し先はこのツールの
    フォルダの下で、初回は存在しません。ここで作らないと、何も
    していないのに1回目だけ必ず失敗します。
    """
    from datetime import datetime
    from . import config

    folder = Path(directory) if directory is not None else config.export_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / datetime.now().strftime(CSV_NAME)

    rows = csv_rows(popularity(conn, board_type))
    with path.open("w", encoding=CSV_ENCODING, newline="") as out:
        csv.writer(out).writerows(rows)

    log.info("ボード人気度を書き出しました: %s (%s行)", path, len(rows) - 1)
    return path
