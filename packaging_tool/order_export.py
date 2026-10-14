"""倉庫連携の発注を、期間を決めてCSVに出す

2種類あります。

    集計     … パレットのサイズ × 発注コード(× 単位)ごとの発注数の合計。
               同じ組み合わせが何件あっても足す
    発注データ … 期間内の発注の行を、そのまま

【サイズはどこから取るか】
発注の表の「幅・丈」は**ロットの板の寸法**で、パレットのサイズではありません。
パレットのサイズは品名の末尾(`タイト　SP-18　1550x3100` の `1550x3100`)にあります
(`lot_service.build_hinmei`)。品名にサイズが無い行(1P0113 の角材・松板など)は、
品名をそのままサイズの欄に入れて、品名ごとに数えます。

【集計は倉庫が確認したものだけ】(現場の指示)
倉庫が「確認済み」にした発注だけを数えます。まだ確認していない発注は数えません。
そのうえで数えないもの:
    取り消した発注 … 発注として成り立っていない
    発注数が 0 か空の行 … EX受注(本番では 0 で入っている)・在庫で全部まかなえた発注など。
                          足しても台数は変わらず、「EX 0台」のような行ができるだけなので入れない
発注データには**どちらも入れます**(そのまま出すのが役目なので、取り消しの印の列も出す)。

【丸めない】
発注数・幅・丈は持っている値のまま出します(2502.5 は 2502.5)。

【Excel で開いても頭の0を消さない】(現場の声:「発注コードは文字列扱い? 頭に0が付いてると数値扱いで消えるよね」
「Excel で開くと思います」)
発注コードの 058901 は、CSV をダブルクリックで Excel に開くと数とみなされて 58901 と出て、
保存し直すとファイルからも0が消える。**コード・番号の列は `="058901"` の形で書く**(`EXCEL_TEXT_COLUMNS`)。
Excel は式として読み、文字の 058901 をそのまま出す。数の列(発注数・厚・幅・丈)は数のまま
(足し算できるように)。

【期間】
登録日時の**日付**で、開始日〜終了日(両端を含む)。登録日時は書き方が揃っていない
ことがある(`2026/09/24 14:23:54` と `2026-09-24 14:23:54`)ので、取り込みと同じ読み方
(`import_specs.to_datetime_text`)で日付にしてから比べます。読めない行は入れずに数だけ知らせます。
"""
from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from . import config, import_specs
from .logging_utils import get_logger

log = get_logger("order_export")

TABLE = config.TBL_WAREHOUSE_ORDER

# 文字の入れ方。ボード人気度の書き出しと同じ(UTF-8 BOM付き。Excel でそのまま開ける)
CSV_ENCODING = "utf-8-sig"

KIND_SUMMARY = "summary"
KIND_ROWS = "rows"
# ファイル名。**出した日時を入れて上書きしない**(前に出したものと見比べられるように)
FILE_NAMES = {
    KIND_SUMMARY: "発注集計_{start}-{end}_{now}.csv",
    KIND_ROWS: "発注データ_{start}-{end}_{now}.csv",
}

# 発注データで出す列(表の並びのまま)。**手元だけの帳簿の列は出さない** ──
# 管理番号は端末ごとに振り直され、取込元管理番号・印未反映・作成元・送信ID は
# 同期のための印で、発注の中身ではない
ROW_COLUMNS: tuple[str, ...] = (
    "登録日時", "LotNo", "品名", "発注コード", "単位", "材質", "調質", "厚", "幅", "丈",
    "用途コード", "納入先", "発注数", "取り消し済", "取り消し日時", "確認済み", "確認日時",
    "送信端末", "発注キー", "受注番号", "ロットの受注",
)

# Excel で開いたとき**文字のまま**見せる列(コード・番号。頭の0・長い数字・英字まじりを崩さない)
EXCEL_TEXT_COLUMNS: tuple[str, ...] = (
    "LotNo", "発注コード", "材質", "調質", "用途コード", "発注キー", "受注番号", "ロットの受注",
)

SUMMARY_COLUMNS: tuple[str, ...] = (
    "サイズ", "パレット幅", "パレット丈", "発注コード", "単位", "発注数の合計", "件数", "品名(例)",
)

# 品名の末尾のパレット寸法(`1550x3100`。全角の ｘ・× でも読む)
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*[xXｘＸ×]\s*(\d+(?:\.\d+)?)\s*$")


def pallet_size(hinmei: Any) -> Optional[tuple[Any, Any]]:
    """品名の末尾のパレット寸法 (幅, 丈)。無ければ None。"""
    found = _SIZE.search(str(hinmei or "").strip())
    if not found:
        return None
    return import_specs.to_number(found.group(1)), import_specs.to_number(found.group(2))


def _day(value: Any) -> Optional[date]:
    """登録日時の日付。読めなければ None。"""
    text = import_specs.to_datetime_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _cell(value: Any) -> str:
    """CSVの1セル。**値はそのまま**(None は空)。"""
    return "" if value is None else str(value)


def excel_text(value: Any) -> str:
    """Excel が文字のまま出すセル(`="058901"`)。空は空のまま。

    Excel の式の中の文字は `"` で囲み、中の `"` は2つ重ねる。
    """
    text = _cell(value)
    if not text:
        return ""
    return '="' + text.replace('"', '""') + '"'


@dataclass
class Picked:
    """期間で拾った行と、拾えなかった行の数。"""

    rows: list[sqlite3.Row] = field(default_factory=list)
    unreadable: int = 0            # 登録日時が読めず、期間に入るか決められなかった


def pick(conn: sqlite3.Connection, start: date, end: date) -> Picked:
    """登録日時の日付が start〜end(両端を含む)の発注。登録日時の古い順。"""
    got = Picked()
    rows = conn.execute(f"SELECT * FROM [{TABLE}] ORDER BY 登録日時, 管理番号").fetchall()
    for row in rows:
        day = _day(row["登録日時"])
        if day is None:
            got.unreadable += 1
            continue
        if start <= day <= end:
            got.rows.append(row)
    return got


def _cancelled(row: sqlite3.Row) -> bool:
    return str(row["取り消し済"] or "").strip() == "1"


@dataclass
class Summary:
    lines: list[list[str]] = field(default_factory=list)   # 見出しを除く
    counted: int = 0           # 数えた発注の数
    cancelled: int = 0         # 取り消したので数えなかった
    unconfirmed: int = 0       # 倉庫がまだ確認していないので数えなかった
    no_qty: int = 0            # 発注数が 0 か空(EX など)で数えなかった


def summarize(rows: list[sqlite3.Row]) -> Summary:
    """サイズ × 発注コード × 単位 ごとの発注数の合計。**倉庫が確認したものだけ**。同じ組み合わせは足す。"""
    got = Summary()
    groups: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        if _cancelled(row):
            got.cancelled += 1
            continue
        if str(row["確認済み"] or "").strip() != "1":
            got.unconfirmed += 1
            continue
        qty = import_specs.to_number(row["発注数"])
        if qty is None or qty == 0:
            got.no_qty += 1
            continue
        hinmei = str(row["品名"] or "").strip()
        size = pallet_size(hinmei)
        label = f"{size[0]}x{size[1]}" if size else hinmei
        code = str(row["発注コード"] or "").strip()
        unit = str(row["単位"] or "").strip()
        key = (label, code, unit)
        item = groups.setdefault(key, {"size": size, "total": Decimal(0), "count": 0,
                                       "example": hinmei})
        # 小数でも誤差を出さない(浮動小数で足すと 0.1 + 0.2 が 0.30000000000000004 になる)
        item["total"] += Decimal(str(qty))
        item["count"] += 1
        got.counted += 1

    def order(entry):
        (label, code, unit), item = entry
        size = item["size"]
        # 寸法のあるものを幅・丈の順に先へ、無いもの(品名で数えたもの)は後ろ
        return (size is None, size or (0, 0), label, code, unit)

    for (label, code, unit), item in sorted(groups.items(), key=order):
        size = item["size"]
        total = item["total"]
        got.lines.append([
            label,
            _cell(size[0]) if size else "",
            _cell(size[1]) if size else "",
            code, unit,
            _cell(int(total) if total == total.to_integral_value() else total.normalize()),
            str(item["count"]),
            item["example"],
        ])
    return got


@dataclass
class ExportResult:
    ok: bool
    message: str
    path: Optional[Path] = None


def parse_day(text: Any) -> Optional[date]:
    """画面の日付(YYYY-MM-DD)。読めなければ None。"""
    try:
        return datetime.strptime(str(text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def export(conn: sqlite3.Connection, kind: str, start: date, end: date,
           directory: Optional[Path] = None) -> ExportResult:
    """CSVを書く。戻り値の `path` は**書いた場所**(どこに出たか分からないのがいちばん困る)。"""
    if kind not in FILE_NAMES:
        return ExportResult(False, "出すものを選んでください。")
    if start > end:
        return ExportResult(False, "開始日が終了日より後になっています。")
    picked = pick(conn, start, end)
    period = f"{start:%Y/%m/%d}〜{end:%Y/%m/%d}"

    if kind == KIND_SUMMARY:
        summary = summarize(picked.rows)
        code = SUMMARY_COLUMNS.index("発注コード")
        lines = [list(SUMMARY_COLUMNS),
                 *[[excel_text(v) if i == code else v for i, v in enumerate(line)]
                   for line in summary.lines]]
        notes = [f"{period} の倉庫が確認した発注 {summary.counted}件を、{len(summary.lines)}行にまとめました"]
        if summary.unconfirmed:
            notes.append(f"倉庫がまだ確認していない {summary.unconfirmed}件は数えていません")
        if summary.cancelled:
            notes.append(f"取り消した {summary.cancelled}件は数えていません")
        if summary.no_qty:
            notes.append(f"発注数が 0 か空(EX など)の {summary.no_qty}件は数えていません")
    else:
        lines = [list(ROW_COLUMNS),
                 *[[excel_text(row[c]) if c in EXCEL_TEXT_COLUMNS else _cell(row[c])
                    for c in ROW_COLUMNS] for row in picked.rows]]
        notes = [f"{period} の発注 {len(picked.rows)}件を出しました(取り消したものも含みます)"]
    if picked.unreadable:
        notes.append(f"登録日時が読めない {picked.unreadable}件は入っていません")

    folder = Path(directory) if directory is not None else config.order_csv_dir()
    name = FILE_NAMES[kind].format(start=f"{start:%Y%m%d}", end=f"{end:%Y%m%d}",
                                   now=datetime.now().strftime("%Y%m%d_%H%M%S"))
    path = folder / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding=CSV_ENCODING, newline="") as out:
            csv.writer(out).writerows(lines)
    except (OSError, ValueError) as exc:
        # **どこへ書こうとして駄目だったかを言う。** 道が違うのか、権限が無いのかを見分けられるように
        log.warning("発注のCSVを書けませんでした(%s): %s", folder, exc)
        return ExportResult(False, f"{folder} に書き出せませんでした({exc})。"
                                   "設定タブの「発注CSVの出力先」を確かめてください。")
    log.info("発注のCSVを書きました: %s (%s行)", path, len(lines) - 1)
    return ExportResult(True, "。".join(notes) + f"。\n出力先: {path}", path)
