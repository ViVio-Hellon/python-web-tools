"""PalletMaster の発注コードの重複を見つける。

現場の声:「パレットマスターに発注コードが重複しているものがあった」「重複チェックは必要」。
調べると 4,133行のうち 30種(60行)あり、Access の中にも前からあった(取り込みで混ざったものではない)。

【なぜ要るか】
発注は押した行(管理番号)のコードを送るので、ツールが別の行のコードを送ることはない。
けれど**同じコードが2つの品物を指している**と、受け取った倉庫はどちらを用意するか決められない
(例: 057417 が 850×2150 台 と 950×2050 台 の両方に付いている)。

【分け方】(組ごと。上ほど危ない)
    寸法が違う       … 幅・丈の違う行が同じコード。別の品物なのにコードが同じ
    単位が違う       … 寸法は同じで、台・枚・組 が同じコード
    記号・備考だけ違う … 寸法も単位も同じ。古い行と足した行が並んでいることが多い
    全く同じ          … 記号・備考まで同じ行が2つ以上

在庫の行(位置のある行)は数えない ── 在庫はツールが型録の行の写しとして足すもので、
コードは空にしてある。空のコード・「---」(コードが無い印)も数えない。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

KIND_SIZE = "寸法が違う"
KIND_UNIT = "単位が違う"
KIND_NOTE = "記号・備考だけ違う"
KIND_SAME = "全く同じ"
KINDS = (KIND_SIZE, KIND_UNIT, KIND_NOTE, KIND_SAME)

# コードが無いことを表す字(`warehouse_service.NO_CODE`)
NO_CODE = "---"

COLUMNS = ("管理番号", "幅", "丈", "単位", "記号", "備考", "コード", "位置")


@dataclass
class DupGroup:
    """同じ発注コードの行の組。"""

    code: str
    kind: str
    rows: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> str:
        """「057417(850×2150 台 / 950×2050 台)」の形。"""
        return f"{self.code}(" + " / ".join(row_text(r) for r in self.rows) + ")"


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _size(value: Any) -> str:
    """寸法をそろえる(1100 と 1100.0 と '1100' を同じに)。"""
    text = _text(value)
    try:
        number = float(text)
    except ValueError:
        return text
    return str(int(number)) if number.is_integer() else str(number)


def row_text(row: Mapping[str, Any]) -> str:
    """画面に出す1行。「850×2150 台」。"""
    return f"{_size(row.get('幅'))}×{_size(row.get('丈'))} {_text(row.get('単位')) or '(単位なし)'}"


def _kind(rows: list[Mapping[str, Any]]) -> str:
    if len({(_size(r.get("幅")), _size(r.get("丈"))) for r in rows}) > 1:
        return KIND_SIZE
    if len({_text(r.get("単位")) for r in rows}) > 1:
        return KIND_UNIT
    if len({(_text(r.get("記号")), _text(r.get("備考"))) for r in rows}) > 1:
        return KIND_NOTE
    return KIND_SAME


def find(rows: Iterable[Mapping[str, Any]]) -> list[DupGroup]:
    """重複している発注コードの組。危ない種類から、同じ種類はコードの順。"""
    by_code: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = _text(row.get("コード"))
        if not code or code == NO_CODE or _text(row.get("位置")):
            continue
        by_code.setdefault(code, []).append(dict(row))
    groups = [DupGroup(code, _kind(found), found)
              for code, found in by_code.items() if len(found) > 1]
    groups.sort(key=lambda g: (KINDS.index(g.kind), g.code))
    return groups


def summary(groups: list[DupGroup]) -> str:
    """「30種(60行)。寸法が違う 14・単位が違う 9・記号・備考だけ違う 6・全く同じ 1」。空なら空。"""
    if not groups:
        return ""
    counts = [(k, sum(1 for g in groups if g.kind == k)) for k in KINDS]
    detail = "・".join(f"{k} {n}" for k, n in counts if n)
    return f"{len(groups)}種({sum(len(g.rows) for g in groups)}行)。{detail}"


def _select(names: Iterable[str]) -> str:
    have = set(names)
    return ", ".join(f'"{c}"' if c in have else f'NULL AS "{c}"' for c in COLUMNS)


def from_conn(conn: sqlite3.Connection) -> list[DupGroup]:
    """手元のDBの PalletMaster から。表が無ければ空。"""
    try:
        names = [r[1] for r in conn.execute("PRAGMA table_info(PalletMaster)")]
        if "コード" not in names:
            return []
        cursor = conn.execute(f"SELECT {_select(names)} FROM PalletMaster")
        keys = [d[0] for d in cursor.description]
        return find(dict(zip(keys, row)) for row in cursor.fetchall())
    except sqlite3.Error:
        return []


def from_path(path: Path) -> list[DupGroup]:
    """共有(取り込み元)の PalletMaster から。読めなければ空。"""
    from . import source_db
    try:
        names = source_db.columns(path, "PalletMaster")
        if "コード" not in names:
            return []
        return find(source_db.read_query(path, f"SELECT {_select(names)} FROM PalletMaster"))
    except source_db.SourceError:
        return []


def rowids(path: Path) -> list[int]:
    """共有の PalletMaster で、重複している行の rowid(マスタ管理の「重複だけ」)。コードの順。"""
    from . import source_db
    try:
        names = source_db.columns(path, "PalletMaster")
        if "コード" not in names:
            return []
        rows = source_db.read_query(
            path, f'SELECT rowid AS "_rid", {_select(names)} FROM PalletMaster')
    except source_db.SourceError:
        return []
    groups = find(rows)
    return [int(r["_rid"]) for g in sorted(groups, key=lambda g: g.code) for r in g.rows]
