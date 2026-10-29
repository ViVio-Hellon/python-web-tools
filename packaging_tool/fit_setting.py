"""パレット適合設定(VER4.8.116)── 何を直したいかを聞き、直す表と行を**自動で**決めて、
変わるパレットを全部見せてから共有のマスタへ反映する

現場の声:「パレット適合、サイズ修正 → サイズですか？適合ですか？ → 入力 → 業界、記号があるのか？ →
入力 → 探して反映。極力この機能に関してはマスタをダイレクトに操作させないつくりにしてください。
影響範囲が大きいです」

【何を直すのか】
適合範囲(巾適合min/max・丈適合min/max)は PalletMaster に書いてある値ではなく、閾値マスタの
7表から**各端末が計算した値**(`pallet_service.recompute_fit_ranges`)。だから

    適合を直す … 閾値マスタ(PalletThresholdMaster)の表を直す
                  組合せ表 → 記号表 → 業界表 → 帯(丈・幅) のうち、そのパレットを決めている表
    サイズを直す … 梱包資材マスタの PalletMaster の幅・丈(と、古い寸法を指すパレット限定条件)

【流れ】
    探す(`find`)→ 見本を作る(`plan_fit` / `plan_size`。**書かない**)→ 反映(`apply`)
見本と反映は**同じ計算**を通る(`pallet_service.fit_row`)。見本は共有の閾値マスタを読んだ姿で
計算し、反映の直前にもう一度読んで、見本を作ったときと同じかを確かめる(同時に直した人がいれば止める)。

【帯は分ける】(現場の判断)
業界・記号の無いパレットで「この寸法だけ」変えるときは、帯を分ける(1001〜1050 → 1001〜1029 /
1030 / 1031〜1050)。分けた帯には業界を問わず同じ幅(丈)のパレットが乗る。それも見本に出す。

【戻せるように】
反映のたびに、変える前の共有ファイルを手元へ写し(`backup`)、閾値マスタに変更の記録を残す
(`LOG_TABLE`。変える前・変えたあとの値)。記録から「元に戻す」ができる。ほかの端末は、閾値
マスタが変わったことに心拍で気づいて取り込み直す(`refresh_in_background`)。
"""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import bounded, config, import_specs, pallet_service, pallet_threshold, source_db
from .logging_utils import get_logger
from .pallet_service import (RULE_BAND, RULE_COMBO, RULE_INDUSTRY, RULE_SYMBOL,
                             cap_to_pallet, normalize_key)
from .pallet_symbol import base_symbol

log = get_logger("fit_setting")

KIND_FIT = "fit"
KIND_SIZE = "size"

# 変更の記録(閾値マスタの中に置く。ほかの端末はこの表が増えたことでも気づく)
LOG_TABLE = "パレット適合の変更記録"
# 分けた帯の備考の頭。**この画面が分けた帯**を見分ける(元に戻す・つなぎ直すとき)
SPLIT_MARK = "パレット適合設定で分けた"
ADDED_MARK = "パレット適合設定で足した"

FILE_THRESHOLD = "threshold"
FILE_MASTER = "master"

FIT_COLUMNS = ("巾適合min", "巾適合max", "丈適合min", "丈適合max")
SHOWN_COLUMNS = FIT_COLUMNS + ("桁数", "脚数")

# 決まりの呼び名(画面に出す)
RULE_LABEL = {
    RULE_COMBO: "組合せ表(業界×記号)",
    RULE_SYMBOL: "記号表",
    RULE_INDUSTRY: "業界表",
    RULE_BAND: "帯(丈の帯・幅の帯)",
}
TABLE_LABEL = {
    "PalletDakeThreshold": "丈の帯",
    "PalletHabaThreshold": "幅の帯",
    "PalletAshiThreshold": "脚数の帯",
    "PalletKetaThreshold": "桁数の帯",
    "PalletSymbolMaster": "記号表",
    "PalletIndustryMaster": "業界表",
    "PalletComboMaster": "組合せ表",
    config.TBL_PALLET_MASTER: "パレット(PalletMaster)",
    import_specs.PALLET_LIMIT_TABLE: "パレット限定条件(1P1185)",
}

# 選択肢の名前
OPT_EDIT = "edit"              # いま決めている固定の表の行を直す
OPT_ADD_SYMBOL = "add_symbol"  # 記号表に足す(その記号のパレットだけ)
OPT_ADD_INDUSTRY = "add_industry"  # 業界表に足す(その業界のパレットだけ。寸法が1種類のときだけ)
OPT_SPLIT = "split"            # 帯を分ける(この寸法だけ)
OPT_BAND = "band"              # 帯ぜんぶを変える

# 製品で確かめるときの許容(選定の ±5 の段と同じ)
CHECK_TOLERANCE = 5
# 見本を覚えておく時間(秒)と数
PLAN_TTL_SEC = 30 * 60
PLAN_KEEP = 20
# 一覧に出す行の上限(数は必ず言う)
SHOW_LIMIT = 300

# 断りの種別
REFUSE_BAD_INPUT = "bad_input"
REFUSE_NOT_FOUND = "not_found"
REFUSE_NO_SOURCE = "no_source"
REFUSE_NOT_ALLOWED = "not_allowed"
REFUSE_CHANGED = "changed"          # 見本のあとに共有が変わった
REFUSE_EXPIRED = "expired"
REFUSE_UNSAFE = "unsafe"


class FitError(Exception):
    """見本を作れない・反映できない理由。`reason` は断りの種別。"""

    def __init__(self, message: str, reason: str = REFUSE_BAD_INPUT) -> None:
        super().__init__(message)
        self.reason = reason


# ==================================================================
# 読む
# ==================================================================
def _now() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S")


def _int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(float(str(value).strip() or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _threshold_path() -> Path:
    from . import data_sync
    path = data_sync.find_threshold_db()
    if path is None:
        raise FitError(f"パレット閾値マスタ({config.THRESHOLD_DB_NAME})が見つかりません。"
                       f"設定の「取り込み元」で {config.threshold_db_dir()} を確かめてください。",
                       REFUSE_NO_SOURCE)
    return path


def _master_path() -> Path:
    from . import data_sync, sync_sources
    path = data_sync.find_material_db()
    if path is None:
        raise FitError(sync_sources.material_db_missing_why(), REFUSE_NO_SOURCE)
    return path


def _convert(table: str, raw: dict[str, Any]) -> dict[str, Any]:
    """共有の1行を、取り込みと同じ形にそろえる(`import_specs` の変換)。`__行` は rowid。

    **`ID` は持たない。** 取り込みは共有の ID を写さず、読んだ順(= rowid の順)に手元で振り直す
    (`import_specs._THRESHOLD_TAIL`)。同じ並び順の行の勝ち負けを手元と同じにするため、
    `select_rows` には rowid(`__行`)で並べさせる。
    """
    out: dict[str, Any] = {"__行": raw.get("__行")}
    for local, source, conv in import_specs.IMPORT_SPECS.get(table, []):
        try:
            out[local] = conv(raw.get(source))
        except Exception:                          # noqa: BLE001 - 読めない値は空
            out[local] = None
    return out


@dataclass
class Shared:
    """共有の閾値マスタ7表を読んだ姿。`raw` は共有の値そのまま(反映前の確かめに使う)。"""

    path: Path
    raw: dict[str, list[dict[str, Any]]]
    conv: dict[str, list[dict[str, Any]]]


def read_shared(path: Optional[Path] = None) -> Shared:
    path = path or _threshold_path()
    raw: dict[str, list[dict[str, Any]]] = {}
    conv: dict[str, list[dict[str, Any]]] = {}
    q = source_db.quote_identifier
    for table in pallet_threshold.TABLES:
        try:
            rows = source_db.read_query(path, f"SELECT rowid AS __行, * FROM {q(table)}")
        except source_db.SourceError as exc:
            raise FitError(f"パレット閾値マスタの {table} を読めません: {exc}", REFUSE_NO_SOURCE) from exc
        raw[table] = rows
        conv[table] = [_convert(table, r) for r in rows]
    return Shared(path, raw, conv)


@dataclass
class Engine:
    thresholds: pallet_threshold.Thresholds
    table: pallet_service.PalletRangeTable


def _engine(conv: dict[str, list[dict[str, Any]]]) -> Engine:
    def get(table: str) -> list[Any]:
        return pallet_threshold.select_rows(table, conv.get(table, []))
    try:
        return Engine(pallet_threshold.load_from(get), pallet_service.build_range_table_from(get))
    except pallet_threshold.ThresholdError as exc:
        raise FitError(f"閾値の表が正しくありません: {exc}", REFUSE_UNSAFE) from exc


def _local_engine(conn: sqlite3.Connection) -> Optional[Engine]:
    try:
        return Engine(pallet_threshold.load(conn), pallet_service.build_pallet_range_table(conn))
    except pallet_threshold.ThresholdError:
        return None


def _pallets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = ("管理番号", "幅", "丈", "業界", "記号", "脚数", "桁数", "コード", "単位",
            "位置", "在庫数", "リスト管理") + FIT_COLUMNS
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM PalletMaster ORDER BY 管理番号").fetchall()
    out = []
    for r in rows:
        d = {c: r[i] for i, c in enumerate(cols)}
        d["幅"] = _int(d["幅"])
        d["丈"] = _int(d["丈"])
        out.append(d)
    return out


def _is_stock(row: dict[str, Any]) -> bool:
    """受入で増えた在庫の行(位置あり)。型録の行は位置が空。"""
    return bool(str(row.get("位置") or "").strip())


# 業界が空の行は、取り込むと「一般」になる(`import_specs` の PalletMaster)。共有の行と手元の行を
# 同じ組に数えるため、ここでも空は「一般」とみなす
BLANK_INDUSTRY = "一般"


def _industry(value: Any) -> str:
    return str(value or "").strip() or BLANK_INDUSTRY


def _group_key(row: dict[str, Any]) -> str:
    return f"{normalize_key(_industry(row.get('業界')))}|{normalize_key(base_symbol(row.get('記号')))}"


def _group_label(industry: str, symbol: str) -> str:
    return f"{industry or '(業界なし)'}・{symbol or '記号なし'}"


def _fit(row: dict[str, Any], engine: Engine) -> pallet_service.RowFit:
    return pallet_service.fit_row(row, engine.thresholds, engine.table)


def _effective(row: dict[str, Any], fit: pallet_service.RowFit) -> dict[str, int]:
    """計算したあとの値(計算が触らなかった列は今の値)。"""
    return {c: _int(fit.updates.get(c, row.get(c))) for c in SHOWN_COLUMNS}


def _row_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"no": row["管理番号"], "code": str(row.get("コード") or "").strip(),
            "unit": str(row.get("単位") or ""), "width": row["幅"], "length": row["丈"],
            "industry": str(row.get("業界") or ""), "symbol": str(row.get("記号") or ""),
            "position": str(row.get("位置") or "").strip(), "stock": _int(row.get("在庫数")),
            "kind": "stock" if _is_stock(row) else "catalog"}


# ==================================================================
# 探す
# ==================================================================
def find(conn: sqlite3.Connection, width: Any, length: Any) -> dict[str, Any]:
    """その寸法のパレットを、業界・記号ごとにまとめて返す(手元のマスタ。共有には触らない)。"""
    w, l = _int(width), _int(length)
    if w <= 0 or l <= 0:
        raise FitError("パレットの幅と丈を入れてください。")
    pallets = _pallets(conn)
    engine = _local_engine(conn)
    rows = [r for r in pallets if r["幅"] == w and r["丈"] == l]
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _group_key(row)
        g = groups.get(key)
        if g is None:
            fit = _fit(row, engine) if engine else None
            eff = _effective(row, fit) if fit else {c: _int(row.get(c)) for c in SHOWN_COLUMNS}
            industry = _industry(row.get("業界"))
            symbol = base_symbol(row.get("記号"))
            g = groups[key] = {
                "key": key, "industry": industry, "symbol": symbol,
                "label": _group_label(industry, symbol),
                "rule": fit.rule if fit else "", "rule_key": fit.key if fit else "",
                "rule_label": RULE_LABEL.get(fit.rule, "") if fit else "閾値が読めません",
                "fit": eff, "rows": []}
        g["rows"].append(_row_view(row))
    out = list(groups.values())
    for g in out:
        g["catalog"] = sum(1 for r in g["rows"] if r["kind"] == "catalog")
        g["stock_rows"] = sum(1 for r in g["rows"] if r["kind"] == "stock")
    nearby: list[str] = []
    if not out:
        same_w = sorted({r["丈"] for r in pallets if r["幅"] == w})
        same_l = sorted({r["幅"] for r in pallets if r["丈"] == l})
        if same_w:
            nearby.append(f"幅{w} のパレットの丈: " + "・".join(str(x) for x in same_w[:12])
                          + (" ほか" if len(same_w) > 12 else ""))
        if same_l:
            nearby.append(f"丈{l} のパレットの幅: " + "・".join(str(x) for x in same_l[:12])
                          + (" ほか" if len(same_l) > 12 else ""))
    return {"ok": True, "width": w, "length": l, "groups": out, "nearby": nearby,
            "message": "" if out else f"{w}×{l} のパレットはマスタにありません。"}


# ==================================================================
# 見本(書かない)
# ==================================================================
@dataclass
class Op:
    """共有のファイルへの書き込み1つ。`before` は共有のその行のいまの値(反映前の確かめに使う)。"""

    file: str
    table: str
    action: str                      # update / insert / delete
    rowid: Optional[int] = None
    before: dict[str, Any] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "table": self.table, "action": self.action, "rowid": self.rowid,
                "before": self.before, "values": self.values, "note": self.note}


@dataclass
class Plan:
    id: str
    kind: str
    created: float
    summary: str
    ops: list[Op]
    affected: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)
    option: str = ""
    shared_path: Optional[Path] = None
    master_path: Optional[Path] = None
    # 製品で確かめる(`check_product`)ための、全パレットの前・後の範囲
    before_fits: dict[int, dict[str, int]] = field(default_factory=dict)
    after_fits: dict[int, dict[str, int]] = field(default_factory=dict)
    sizes_before: dict[int, tuple[int, int]] = field(default_factory=dict)
    sizes_after: dict[int, tuple[int, int]] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        shown = self.affected[:SHOW_LIMIT]
        return {
            "id": self.id, "kind": self.kind, "summary": self.summary,
            "ops": [{"table": TABLE_LABEL.get(o.table, o.table), "action": o.action, "note": o.note}
                    for o in self.ops],
            "affected": shown, "affected_count": len(self.affected),
            "affected_more": max(0, len(self.affected) - len(shown)),
            "warnings": self.warnings, "notes": self.notes,
            "options": self.options, "option": self.option,
            "can_apply": bool(self.ops),
        }


_plans: dict[str, Plan] = {}
_plans_lock = threading.Lock()


def _keep(plan: Plan) -> Plan:
    with _plans_lock:
        now = time.time()
        for key in [k for k, p in _plans.items() if now - p.created > PLAN_TTL_SEC]:
            _plans.pop(key, None)
        while len(_plans) >= PLAN_KEEP:
            _plans.pop(min(_plans, key=lambda k: _plans[k].created), None)
        _plans[plan.id] = plan
    return plan


def get_plan(plan_id: str) -> Plan:
    with _plans_lock:
        plan = _plans.get(str(plan_id or ""))
    if plan is None or time.time() - plan.created > PLAN_TTL_SEC:
        raise FitError("見本が古くなりました。もう一度「変わるところを見る」を押してください。",
                       REFUSE_EXPIRED)
    return plan


def _fits_all(pallets: list[dict[str, Any]], engine: Engine) -> dict[int, dict[str, int]]:
    return {r["管理番号"]: _effective(r, _fit(r, engine)) for r in pallets}


def _diff(pallets: list[dict[str, Any]], before: dict[int, dict[str, int]],
          after: dict[int, dict[str, int]],
          sizes_after: Optional[dict[int, tuple[int, int]]] = None) -> list[dict[str, Any]]:
    out = []
    for row in pallets:
        n = row["管理番号"]
        b, a = before.get(n), after.get(n)
        size_after = (sizes_after or {}).get(n, (row["幅"], row["丈"]))
        if b == a and size_after == (row["幅"], row["丈"]):
            continue
        view = _row_view(row)
        view["before"] = b
        view["after"] = a
        view["width_after"], view["length_after"] = size_after
        view["changed"] = [c for c in SHOWN_COLUMNS if (b or {}).get(c) != (a or {}).get(c)]
        out.append(view)
    return out


def _range(req: dict[str, Any], prefix: str) -> tuple[int, int]:
    lo, hi = _int(req.get(f"{prefix}_min")), _int(req.get(f"{prefix}_max"))
    name = "巾" if prefix == "w" else "丈"
    if lo <= 0 or hi <= 0:
        raise FitError(f"{name}適合の最小・最大を入れてください(1以上の整数)。")
    if lo > hi:
        raise FitError(f"{name}適合の最小({lo})が最大({hi})より大きくなっています。")
    return lo, hi


def _targets(pallets: list[dict[str, Any]], width: int, length: int, group: str) -> list[dict[str, Any]]:
    rows = [r for r in pallets if r["幅"] == width and r["丈"] == length]
    if group:
        rows = [r for r in rows if _group_key(r) == group]
    return rows


def _fixed_source(conv: dict[str, list[dict[str, Any]]], rule: str,
                  key: str) -> Optional[tuple[str, dict[str, Any]]]:
    """その鍵の範囲を**実際に決めている**表と行。積む順は `build_range_table_from` と同じ。

    記号表と業界表は**同じ辞書**に積む(記号があと = 同じ鍵なら記号が勝つ)。だから
    「記号で当たった」鍵が業界表の行から来ていることも、その逆もある。鍵だけで探して、
    記号表で最後に当たる行 → 無ければ業界表で最後に当たる行、の順に決める。
    """
    def last(table: str, name: Callable[[dict[str, Any]], str]) -> Optional[dict[str, Any]]:
        hit = None
        for row in pallet_threshold.select_rows(table, conv.get(table, [])):
            if name(row) == key:
                hit = row
        return hit

    if rule == RULE_COMBO:
        row = last("PalletComboMaster",
                   lambda r: f"{normalize_key(str(r.get('業界') or ''))}|{normalize_key(str(r.get('記号') or ''))}")
        return ("PalletComboMaster", row) if row is not None else None
    for table, column in (("PalletSymbolMaster", "記号"), ("PalletIndustryMaster", "業界")):
        row = last(table, lambda r, c=column: normalize_key(str(r.get(c) or "")))
        if row is not None:
            return table, row
    return None


def _raw_of(shared: Shared, table: str, rowid: Any) -> dict[str, Any]:
    for row in shared.raw.get(table, []):
        if row.get("__行") == rowid:
            return {k: v for k, v in row.items() if k != "__行"}
    raise FitError(f"{table} の行(rowid {rowid})が共有にありません。", REFUSE_CHANGED)


def _apply_conv(shared: Shared, ops: list[Op]) -> dict[str, list[dict[str, Any]]]:
    """見本のために、7表の写しへ ops を当てる(共有には書かない)。"""
    conv = copy.deepcopy(shared.conv)
    # 足す行は、共有でも**いちばん後ろの rowid** になる(手元でも最後に取り込まれる)
    fake = 1 + max((_int(r.get("__行")) for rows in conv.values() for r in rows), default=0)
    for op in ops:
        if op.file != FILE_THRESHOLD:
            continue
        rows = conv.setdefault(op.table, [])
        if op.action == "update":
            for row in rows:
                if row.get("__行") == op.rowid:
                    row.update(_convert(op.table, {**op.before, **op.values}))
                    row["__行"] = op.rowid
        elif op.action == "insert":
            new = _convert(op.table, op.values)
            new["__行"] = fake
            fake += 1
            rows.append(new)
        elif op.action == "delete":
            conv[op.table] = [r for r in rows if r.get("__行") != op.rowid]
    return conv


def _band_ops(shared: Shared, table: str, size: int, lo: int, hi: int, *,
              split: bool, dim: str) -> list[Op]:
    """帯を直す。`split` なら**この寸法だけ**の帯を作る(帯を3つ/2つに分ける)。"""
    bands = pallet_threshold.select_rows(table, shared.conv[table])
    hit = next((b for b in bands if _int(b.get("入力最小値")) <= size <= _int(b.get("入力最大値"))), None)
    if hit is None:
        raise FitError(f"{TABLE_LABEL[table]}に {dim}{size} の帯がありません。", REFUSE_UNSAFE)
    rid = hit["__行"]
    before = _raw_of(shared, table, rid)
    b_min, b_max = _int(hit.get("入力最小値")), _int(hit.get("入力最大値"))
    stamp = _now()
    note_band = f"{TABLE_LABEL[table]} {b_min}〜{b_max}"
    if not split or b_min == b_max == size:
        return [Op(FILE_THRESHOLD, table, "update", rid, before,
                   {"適合最小値": lo, "適合最大値": hi, "更新日時": stamp},
                   f"{note_band} の適合を {_int(hit.get('適合最小値'))}〜{_int(hit.get('適合最大値'))}"
                   f" → {lo}〜{hi} にします")]
    base = {k: v for k, v in before.items() if k not in ("ID",)}
    mid_note = f"{SPLIT_MARK}({dim}{size} だけ。{datetime.now():%Y/%m/%d})"
    ops: list[Op] = []
    if size > b_min:
        ops.append(Op(FILE_THRESHOLD, table, "update", rid, before,
                      {"入力最大値": size - 1, "更新日時": stamp},
                      f"{note_band} を {b_min}〜{size - 1} に縮めます(適合はそのまま)"))
        ops.append(Op(FILE_THRESHOLD, table, "insert", None, {},
                      {**base, "入力最小値": size, "入力最大値": size, "適合最小値": lo,
                       "適合最大値": hi, "有効": 1, "備考": mid_note, "更新日時": stamp},
                      f"{dim}{size} だけの帯を足します(適合 {lo}〜{hi})"))
    else:
        ops.append(Op(FILE_THRESHOLD, table, "update", rid, before,
                      {"入力最大値": size, "適合最小値": lo, "適合最大値": hi,
                       "備考": mid_note, "更新日時": stamp},
                      f"{note_band} を {dim}{size} だけの帯にします(適合 {lo}〜{hi})"))
    if size < b_max:
        ops.append(Op(FILE_THRESHOLD, table, "insert", None, {},
                      {**base, "入力最小値": size + 1, "入力最大値": b_max, "有効": 1,
                       "更新日時": stamp},
                      f"残りの {size + 1}〜{b_max} の帯を足します(適合は元のまま"
                      f" {_int(hit.get('適合最小値'))}〜{_int(hit.get('適合最大値'))})"))
    return ops


def _consistent(values: Iterable[Any]) -> Optional[int]:
    seen = {_int(v) for v in values}
    return seen.pop() if len(seen) == 1 else None


def plan_fit(conn: sqlite3.Connection, req: dict[str, Any]) -> Plan:
    """適合を直す見本。`req`: width, length, group, w_min, w_max, l_min, l_max, option。"""
    width, length = _int(req.get("width")), _int(req.get("length"))
    group = str(req.get("group") or "")
    w_lo, w_hi = _range(req, "w")
    l_lo, l_hi = _range(req, "l")
    shared = read_shared()
    engine = _engine(shared.conv)
    pallets = _pallets(conn)
    targets = _targets(pallets, width, length, group)
    if not targets:
        raise FitError(f"{width}×{length} のパレットが見つかりません。", REFUSE_NOT_FOUND)
    row_fits = {r["管理番号"]: _fit(r, engine) for r in pallets}
    rules = {(row_fits[r["管理番号"]].rule, row_fits[r["管理番号"]].key) for r in targets}
    if len(rules) > 1:
        raise FitError("この寸法には、適合範囲の決まり方が違うパレットが混ざっています。"
                       "業界・記号を選んでから直してください。")
    rule, key = next(iter(rules))
    first = targets[0]
    current = _effective(first, row_fits[first["管理番号"]])
    changed_w = (w_lo, w_hi) != (current["巾適合min"], current["巾適合max"])
    changed_l = (l_lo, l_hi) != (current["丈適合min"], current["丈適合max"])
    if not changed_w and not changed_l:
        raise FitError("いまと同じ範囲です。変えたい値を入れてください。")

    before_all = {r["管理番号"]: _effective(r, row_fits[r["管理番号"]]) for r in pallets}
    source = _fixed_source(shared.conv, rule, key) if rule != RULE_BAND else None
    if rule != RULE_BAND and source is None:
        raise FitError("決めている行が共有の表に見つかりません。取り込み直してからもう一度。", REFUSE_CHANGED)
    options = _fit_options(rule, key, source, targets, pallets, row_fits)
    chosen = str(req.get("option") or "") or next(o["id"] for o in options if o.get("default"))
    if chosen not in {o["id"] for o in options if o.get("available", True)}:
        raise FitError("その直し方はこのパレットでは選べません。")
    for o in options:
        o["selected"] = o["id"] == chosen

    ops: list[Op] = []
    stamp = _now()
    notes: list[str] = []
    warnings: list[str] = []
    where = ""
    if chosen == OPT_EDIT:
        table, row = source
        values: dict[str, Any] = {"更新日時": stamp}
        if changed_w:
            values.update({"巾適合最小値": w_lo, "巾適合最大値": w_hi})
        if changed_l:
            values.update({"丈適合最小値": l_lo, "丈適合最大値": l_hi})
        where = f"{TABLE_LABEL[table]}「{_key_label(rule, key)}」の行を直します"
        ops.append(Op(FILE_THRESHOLD, table, "update", row["__行"], _raw_of(shared, table, row["__行"]),
                      values, where))
    elif chosen in (OPT_ADD_SYMBOL, OPT_ADD_INDUSTRY):
        if chosen == OPT_ADD_SYMBOL:
            table, column, name = "PalletSymbolMaster", "記号", base_symbol(first.get("記号"))
            keta = _consistent(r.get("桁数") for r in targets)
            ashi = _consistent(r.get("脚数") for r in targets)
            # 記号表は桁数・脚数も上書きする。空のまま足すと 0 になって2山の判定が崩れるので、今の値を写す
            extra = {"桁数": keta or 0, "脚数": ashi or 0}
            label = f"記号表に「{name}」を足します(桁数 {keta}・脚数 {ashi} は今の値)"
        else:
            table, column, name = "PalletIndustryMaster", "業界", _industry(first.get("業界"))
            extra = {}
            label = f"業界表に「{name}」を足します"
        values = {column: name,
                  "巾適合最小値": w_lo, "巾適合最大値": w_hi,
                  "丈適合最小値": l_lo, "丈適合最大値": l_hi,
                  **extra, "有効": 1, "並び順": 0,
                  "備考": f"{ADDED_MARK}({width}×{length})", "更新日時": stamp}
        where = f"{TABLE_LABEL[table]}に「{name}」を足します"
        # 外してある(有効 0)同じ鍵の行があれば、足さずにそれを直して戻す(記号は重ねられない)
        existing = next((r for r in shared.conv[table]
                         if normalize_key(str(r.get(column) or "")) == normalize_key(name)), None)
        if existing is not None:
            ops.append(Op(FILE_THRESHOLD, table, "update", existing["__行"],
                          _raw_of(shared, table, existing["__行"]), values,
                          f"{TABLE_LABEL[table]}の「{name}」の行(外してあった)を直して使います"))
        else:
            ops.append(Op(FILE_THRESHOLD, table, "insert", None, {}, values, label))
    else:
        split = chosen == OPT_SPLIT
        where = (f"帯を分けて、{'幅' + str(width) if changed_w else ''}{'・' if changed_w and changed_l else ''}"
                 f"{'丈' + str(length) if changed_l else ''} だけの帯を作ります" if split
                 else "帯ぜんぶの範囲を変えます")
        clearance = pallet_service.PALLET_CLEARANCE
        if changed_w:
            ops += _band_ops(shared, "PalletHabaThreshold", width, w_lo, w_hi, split=split, dim="幅")
            if w_hi > width - clearance:
                notes.append(f"幅の上限は現物−{clearance}mm の {cap_to_pallet(w_hi, width)} になります"
                             f"(帯で決まるパレットは、現物より {clearance}mm 小さい製品までしか載せません)。")
        if changed_l:
            ops += _band_ops(shared, "PalletDakeThreshold", length, l_lo, l_hi, split=split, dim="丈")
            if l_hi > length - clearance:
                notes.append(f"丈の上限は現物−{clearance}mm の {cap_to_pallet(l_hi, length)} になります"
                             f"(帯で決まるパレットは、現物より {clearance}mm 小さい製品までしか載せません)。")

    engine_after = _engine(_apply_conv(shared, ops))
    after_all = _fits_all(pallets, engine_after)
    affected = _diff(pallets, before_all, after_all)
    if not affected:
        warnings.append("この直し方では、変わるパレットがありません(上の決まりで上書きされています)。")
    _guard_physical(affected, warnings)
    _note_shared_values(affected)
    targets_no = {r["管理番号"] for r in targets}
    other = [a for a in affected if a["no"] not in targets_no]
    if other:
        notes.append(f"選んだパレットのほかに {len(other)}行 変わります(一覧の「ほか」)。")
    parts = []
    if changed_w:
        parts.append(f"巾適合 {current['巾適合min']}〜{current['巾適合max']} → {w_lo}〜{w_hi}")
    if changed_l:
        parts.append(f"丈適合 {current['丈適合min']}〜{current['丈適合max']} → {l_lo}〜{l_hi}")
    label = _group_label(*_split_group(first)) if group else ""
    summary = (f"{width}×{length}{(' ' + label) if label else ''}: {where}({' / '.join(parts)})。"
               f"変わるパレット {len(affected)}行。")
    plan = Plan(id=uuid.uuid4().hex, kind=KIND_FIT, created=time.time(), summary=summary, ops=ops,
                affected=_mark_targets(affected, targets_no), warnings=warnings, notes=notes,
                options=options, option=chosen, shared_path=shared.path,
                before_fits=before_all, after_fits=after_all,
                sizes_before={r["管理番号"]: (r["幅"], r["丈"]) for r in pallets},
                labels={r["管理番号"]: _pallet_label(r) for r in pallets}, request=dict(req))
    plan.sizes_after = dict(plan.sizes_before)
    return _keep(plan)


def _note_shared_values(affected: list[dict[str, Any]]) -> None:
    """共有の PalletMaster に**書いてある**適合の値が、計算した値と違えば並べて出す。

    適合の4列は各端末が計算し直して上書きするので、共有に書いてある値(Access 側の計算)が
    古いことがある(例: C1 の1行だけ前の範囲が入っていた)。読めなければ黙って何もしない。
    """
    if not affected:
        return
    try:
        from . import data_sync
        master = data_sync.find_material_db()
        if master is None:
            return
        rows = source_db.read_query(
            master, "SELECT TRIM(コード) AS コード, " + ", ".join(FIT_COLUMNS)
                    + f" FROM {source_db.quote_identifier(config.TBL_PALLET_MASTER)} WHERE TRIM(COALESCE(コード, '')) <> ''")
    except Exception:                              # noqa: BLE001 - 見せるだけの値
        return
    by_code = {str(r["コード"]): {c: _int(r.get(c)) for c in FIT_COLUMNS} for r in rows}
    for a in affected:
        now = by_code.get(a["code"])
        if now and a["before"] and any(now[c] != a["before"][c] for c in FIT_COLUMNS):
            a["shared_now"] = now


def _guard_physical(affected: list[dict[str, Any]], warnings: list[str]) -> None:
    """現物より大きい製品の範囲になるなら**止める**。余裕が10mmを切るなら言う。

    帯で決まるパレットは上限が現物−10mm で頭打ちになる(`cap_to_pallet`)が、記号・業界・組合せの
    固定の表は頭打ちにならない。**変わった列だけ**で見る(前から大きかった列で止めない)。
    """
    clearance = pallet_service.PALLET_CLEARANCE
    too_big, tight = [], []
    for a in affected:
        after, changed = a["after"] or {}, set(a["changed"])
        w, l = a["width_after"], a["length_after"]
        over = (("巾適合max" in changed and after.get("巾適合max", 0) > w)
                or ("丈適合max" in changed and after.get("丈適合max", 0) > l))
        near = (("巾適合max" in changed and after.get("巾適合max", 0) > w - clearance)
                or ("丈適合max" in changed and after.get("丈適合max", 0) > l - clearance))
        if over:
            too_big.append(a)
        elif near:
            tight.append(a)
    if too_big:
        a = too_big[0]
        raise FitError(f"現物より大きい製品まで載ることになるパレットがあります({len(too_big)}行。例 "
                       f"{a['width_after']}×{a['length_after']} {a['code'] or a['position']})。"
                       "範囲を見直してください。", REFUSE_UNSAFE)
    if tight:
        a = tight[0]
        warnings.append(f"現物との余裕が {clearance}mm より小さくなるパレットが {len(tight)}行 あります"
                        f"(例 {a['width_after']}×{a['length_after']} {a['code'] or a['position']})。")


def _mark_targets(affected: list[dict[str, Any]], targets: Iterable[Any]) -> list[dict[str, Any]]:
    chosen = set(targets)
    for a in affected:
        a["target"] = a["no"] in chosen
    affected.sort(key=lambda a: (not a["target"], a["width"], a["length"], a["code"]))
    return affected


def _pallet_label(row: dict[str, Any]) -> str:
    sym = base_symbol(row.get("記号"))
    return (f"{row['幅']}×{row['丈']} {row.get('業界') or ''}{(' ' + sym) if sym else ''}"
            f" {row.get('単位') or ''} {str(row.get('コード') or '').strip()}").strip()


def _key_label(rule: str, key: str) -> str:
    if rule == RULE_COMBO:
        industry, _, symbol = key.partition("|")
        return f"{industry} × {symbol}"
    return key


def _fit_options(rule: str, key: str, source: Optional[tuple[str, dict[str, Any]]],
                 targets: list[dict[str, Any]], pallets: list[dict[str, Any]],
                 row_fits: dict[int, pallet_service.RowFit]) -> list[dict[str, Any]]:
    """直し方の選択肢。**変わるパレットがいちばん少ないものを先に選んでおく。**"""
    width, length = targets[0]["幅"], targets[0]["丈"]
    if rule != RULE_BAND:
        table = source[0] if source else ""
        # 記号表と業界表は同じ鍵の辞書に積むので、鍵が同じなら「記号で当たった」も「業界で
        # 当たった」も同じ行で決まっている(`_fixed_source`)
        same_rule = {rule} if rule == RULE_COMBO else {RULE_SYMBOL, RULE_INDUSTRY}
        governed = [r for r in pallets
                    if row_fits[r["管理番号"]].rule in same_rule and row_fits[r["管理番号"]].key == key]
        sizes = {(r["幅"], r["丈"]) for r in governed}
        name = f"{TABLE_LABEL.get(table, RULE_LABEL[rule])}「{_key_label(rule, key)}」"
        return [{"id": OPT_EDIT, "label": f"{name}の行を直す",
                 "note": f"{name}で決まっている {len(governed)}行(寸法 {len(sizes)}種類)すべてに効きます。",
                 "affected": len(governed), "default": True, "available": True}]
    options: list[dict[str, Any]] = []
    shown = base_symbol(targets[0].get("記号"))
    symbol = normalize_key(shown)
    if symbol and all(normalize_key(base_symbol(r.get("記号"))) == symbol for r in targets):
        same = [r for r in pallets if normalize_key(base_symbol(r.get("記号"))) == symbol]
        sizes = {(r["幅"], r["丈"]) for r in same}
        keta = _consistent(r.get("桁数") for r in targets)
        ashi = _consistent(r.get("脚数") for r in targets)
        if len(sizes) == 1 and keta is not None and ashi is not None:
            options.append({"id": OPT_ADD_SYMBOL, "label": f"記号表に「{shown}」を足す",
                            "note": f"記号「{shown}」のパレット {len(same)}行 だけが変わります"
                                    "(桁数・脚数は今の値を写します)。",
                            "affected": len(same), "default": True, "available": True})
        else:
            why = (f"記号「{shown}」は {len(sizes)}種類の寸法にまたがるので、記号表には足せません"
                   "(1つの範囲で全部の寸法を決めてしまうため)。"
                   if len(sizes) > 1 else "桁数・脚数がそろっていないので、記号表には足せません。")
            options.append({"id": OPT_ADD_SYMBOL, "label": f"記号表に「{shown}」を足す", "note": why,
                            "affected": 0, "default": False, "available": False})
    industry = _industry(targets[0].get("業界"))
    key_i = normalize_key(industry)
    if (not symbol and industry != BLANK_INDUSTRY
            and all(not base_symbol(r.get("記号")) and normalize_key(_industry(r.get("業界"))) == key_i
                    for r in targets)):
        same = [r for r in pallets if normalize_key(_industry(r.get("業界"))) == key_i]
        sizes = {(r["幅"], r["丈"]) for r in same}
        if len(sizes) == 1:
            options.append({"id": OPT_ADD_INDUSTRY, "label": f"業界表に「{industry}」を足す",
                            "note": f"業界「{industry}」のパレット {len(same)}行 だけが変わります。",
                            "affected": len(same), "default": True, "available": True})
        else:
            options.append({"id": OPT_ADD_INDUSTRY, "label": f"業界表に「{industry}」を足す",
                            "note": f"業界「{industry}」は {len(sizes)}種類の寸法にまたがるので、業界表には"
                                    "足せません(1つの範囲で全部の寸法を決めてしまうため)。",
                            "affected": 0, "default": False, "available": False})
    band_w = sum(1 for r in pallets if r["幅"] == width and row_fits[r["管理番号"]].rule == RULE_BAND)
    band_l = sum(1 for r in pallets if r["丈"] == length and row_fits[r["管理番号"]].rule == RULE_BAND)
    options.append({"id": OPT_SPLIT, "label": "帯を分ける(この寸法だけ)",
                    "note": (f"直した向きの帯だけを分けます。巾適合を直すと幅{width} のパレット {band_w}行、"
                             f"丈適合を直すと丈{length} のパレット {band_l}行 に効きます(業界は問いません)。"),
                    "affected": None, "default": not any(o.get("default") for o in options),
                    "available": True})
    options.append({"id": OPT_BAND, "label": "帯ぜんぶを変える",
                    "note": "その寸法が入っている帯の、全部のパレットに効きます(数は見本で出します)。",
                    "affected": None, "default": False, "available": True})
    return options


# ------------------------------------------------------------------
# サイズを直す
# ------------------------------------------------------------------
STOCK_KEEP = "keep"     # 倉庫にある前のサイズの在庫は、前の寸法のまま残す
STOCK_MOVE = "move"     # 在庫の行も新しい寸法にする(マスタの書き間違いだったとき)


def plan_size(conn: sqlite3.Connection, req: dict[str, Any]) -> Plan:
    """サイズを直す見本。`req`: width, length, group, rows(直す行のコードの並び。空なら型録の行ぜんぶ),
    new_width, new_length, codes({前のコード: 新しいコード}。空ならコードは変えない),
    stock(keep/move), limit(move/keep), merge(bool)。"""
    width, length = _int(req.get("width")), _int(req.get("length"))
    new_w, new_l = _int(req.get("new_width")), _int(req.get("new_length"))
    group = str(req.get("group") or "")
    if new_w <= 0 or new_l <= 0:
        raise FitError("新しい幅と丈を入れてください。")
    if (new_w, new_l) == (width, length):
        raise FitError("いまと同じ寸法です。")
    if not group:
        raise FitError("業界・記号を選んでください(サイズはパレットごとに直します)。")
    pallets = _pallets(conn)
    rows = _targets(pallets, width, length, group)
    catalog = [r for r in rows if not _is_stock(r)]
    stock = [r for r in rows if _is_stock(r)]
    # 行はコードで選ぶ(手元の管理番号は取り込みのたびに振り直されるので、画面との間で食い違う)
    wanted = {str(c).strip() for c in (req.get("rows") or []) if str(c or "").strip()}
    chosen = [r for r in catalog if not wanted or str(r.get("コード") or "").strip() in wanted]
    if not chosen:
        raise FitError("直すパレットの行を選んでください。", REFUSE_NOT_FOUND)
    codes_in = {str(k).strip(): str(v).strip() for k, v in (req.get("codes") or {}).items()
                if str(v or "").strip()}
    change_code = bool(codes_in)
    stock_mode = str(req.get("stock") or STOCK_KEEP)
    limit_move = str(req.get("limit") or "move") == "move"

    # 重なり: 新しい寸法に、同じ業界・記号・単位の型録の行がもうある
    clash = [r for r in pallets if (r["幅"], r["丈"]) == (new_w, new_l) and not _is_stock(r)
             and _group_key(r) == group
             and any(str(r.get("単位") or "") == str(c.get("単位") or "") for c in chosen)]
    if clash:
        raise FitError(f"{new_w}×{new_l} には、同じ業界・記号・単位のパレットがもうあります"
                       f"({_pallet_label(clash[0])})。そちらを使うか、先に整理してください。")
    used_codes = {str(r.get("コード") or "").strip() for r in pallets}
    if change_code:
        for row in chosen:
            old = str(row.get("コード") or "").strip()
            new = codes_in.get(old, "")
            if not new:
                raise FitError(f"{row.get('単位') or ''}({old})の新しいコードを入れてください。")
            if new in used_codes:
                raise FitError(f"コード {new} はもうマスタにあります。")
        if len(set(codes_in.values())) != len(codes_in):
            raise FitError("新しいコードが重なっています。")

    master = _master_path()
    q = source_db.quote_identifier
    ops: list[Op] = []
    stamp = _now()
    shared_rows: dict[int, dict[str, Any]] = {}
    for row in chosen:
        code = str(row.get("コード") or "").strip()
        if not code:
            raise FitError(f"コードの無い行は選べません({_pallet_label(row)})。")
        try:
            found = source_db.read_query(
                master, f"SELECT rowid AS __行, * FROM {q('PalletMaster')} WHERE TRIM(コード) = ?", [code])
        except source_db.SourceError as exc:
            raise FitError(f"梱包資材マスタを読めません: {exc}", REFUSE_NO_SOURCE) from exc
        if len(found) != 1 or (_int(found[0].get("幅")), _int(found[0].get("丈"))) != (width, length):
            raise FitError(f"共有のパレット(コード {code})が {width}×{length} の1行として見つかりません。"
                           "取り込み直してからもう一度。", REFUSE_CHANGED)
        shared_rows[row["管理番号"]] = found[0]
    for row in chosen:
        raw = shared_rows[row["管理番号"]]
        rid = raw["__行"]
        before = {k: v for k, v in raw.items() if k != "__行"}
        old_code = str(row.get("コード") or "").strip()
        if change_code:
            new_values = {k: v for k, v in before.items() if k != "管理番号"}
            new_values.update({"幅": new_w, "丈": new_l, "コード": codes_in[old_code], "更新日時": stamp})
            for c in ("巾適合min", "巾適合max", "丈適合min", "丈適合max"):
                if c in new_values:
                    new_values[c] = 0
            ops.append(Op(FILE_MASTER, "PalletMaster", "insert", None, {}, new_values,
                          f"{row.get('単位') or ''} {codes_in[old_code]}({new_w}×{new_l})を足します"))
            ops.append(Op(FILE_MASTER, "PalletMaster", "delete", rid, before, {},
                          f"{row.get('単位') or ''} {old_code}({width}×{length})を消します"))
        else:
            ops.append(Op(FILE_MASTER, "PalletMaster", "update", rid, before,
                          {"幅": new_w, "丈": new_l, "更新日時": stamp},
                          f"{row.get('単位') or ''} {old_code} を {width}×{length} → {new_w}×{new_l} にします"))
    notes: list[str] = []
    warnings: list[str] = []
    moved_stock: list[dict[str, Any]] = []
    if stock:
        if stock_mode == STOCK_MOVE:
            # 在庫の行はコードを持たないので、寸法・位置・業界・記号で共有の行を探す。
            # 同じ位置に台と枚の2行があることもあるので、**同じ行を2度書かない**(rowid でまとめる)
            try:
                found = source_db.read_query(
                    master, f"SELECT rowid AS __行, * FROM {q('PalletMaster')}"
                            " WHERE CAST(幅 AS INTEGER) = ? AND CAST(丈 AS INTEGER) = ?"
                            " AND TRIM(COALESCE(位置, '')) <> ''", [width, length])
            except source_db.SourceError as exc:
                raise FitError(f"梱包資材マスタを読めません: {exc}", REFUSE_NO_SOURCE) from exc
            for raw in found:
                if _group_key(raw) != group:
                    continue
                ops.append(Op(FILE_MASTER, "PalletMaster", "update", raw["__行"],
                              {k: v for k, v in raw.items() if k != "__行"},
                              {"幅": new_w, "丈": new_l, "更新日時": stamp},
                              f"在庫の行(位置 {str(raw.get('位置') or '').strip()}・{raw.get('単位') or ''}"
                              f" {_int(raw.get('在庫数'))})も {new_w}×{new_l} にします"))
            moved_stock = list(stock)
        else:
            notes.append(f"倉庫にある前のサイズの在庫 {len(stock)}行(合計 {sum(_int(r.get('在庫数')) for r in stock)})"
                         "は前の寸法のまま残します。在庫がある間は前のサイズも候補に出て、"
                         "払い出しで0になると自動で消えます。")
    # パレット限定条件(1P1185)。前の寸法を決め打っている行は、既定で新しい寸法へ書き換える。
    # ただし**前の寸法にほかのパレットが残る**(業界が空の条件なら、どの業界でも)ときは、
    # その条件はそのパレットを指しているかもしれないので書き換えない
    chosen_no = {r["管理番号"] for r in chosen}
    remaining = [r for r in pallets if (r["幅"], r["丈"]) == (width, length)
                 and not _is_stock(r) and r["管理番号"] not in chosen_no]
    for raw in _limit_rows(master, width, length, chosen[0]):
        spec = str(raw.get("包装仕様NO") or "").strip()
        industry = normalize_key(str(raw.get("業界") or ""))
        still = [r for r in remaining if not industry or normalize_key(_industry(r.get("業界"))) == industry]
        if still:
            notes.append(f"パレット限定条件({spec})は前の寸法 {width}×{length} のまま残します"
                         f"(その寸法に {_pallet_label(still[0])} ほか {len(still)}行 が残るため)。")
        elif limit_move:
            ops.append(Op(FILE_MASTER, import_specs.PALLET_LIMIT_TABLE, "update", raw["__行"],
                          {k: v for k, v in raw.items() if k != "__行"},
                          {"パレット幅": new_w, "パレット丈": new_l, "更新日時": stamp},
                          f"パレット限定条件({spec})の決め打つ寸法を {new_w}×{new_l} にします"))
        else:
            warnings.append(f"パレット限定条件({spec})は前の寸法 {width}×{length} を決め打ったまま"
                            "になります(そのロットでは、在庫が尽きるとパレットが出なくなります)。")
    patterns = _pattern_count(conn, width, length)
    if patterns:
        notes.append(f"前の寸法 {width}×{length} の実績パターン {patterns}件は、そのまま残ります"
                     "(この版では新しい寸法へは写しません。新しい寸法で覚え直してください)。")

    # 見本: 新しい寸法で計算し直す(共有の閾値で)
    shared = read_shared()
    engine = _engine(shared.conv)
    moved = {r["管理番号"] for r in chosen} | {r["管理番号"] for r in moved_stock}
    before_all = _fits_all(pallets, engine)
    after_rows = [dict(r, 幅=new_w, 丈=new_l) if r["管理番号"] in moved else r for r in pallets]
    # 分けた帯が使われなくなるなら、つなぎ直す(見本の計算もつなぎ直したあとの帯で)
    if req.get("merge", True) is not False:
        ops_merge, merge_notes = _merge_ops(shared, after_rows, engine, width, length)
        ops += ops_merge
        notes += merge_notes
    engine_after = _engine(_apply_conv(shared, ops))
    after_all = _fits_all(after_rows, engine_after)
    sizes_after = {r["管理番号"]: (r["幅"], r["丈"]) for r in after_rows}
    affected = _diff(pallets, before_all, after_all, sizes_after)
    if change_code:
        for a in affected:
            if a["no"] in chosen_no:
                a["code_after"] = codes_in.get(a["code"], a["code"])
    _note_shared_values(affected)
    # 決まり方(固定の表なら寸法について来ない)
    fit_new = _fit(dict(chosen[0], 幅=new_w, 丈=new_l), engine_after)
    if fit_new.rule != RULE_BAND:
        eff = _effective(dict(chosen[0], 幅=new_w, 丈=new_l), fit_new)
        notes.append(f"適合は{RULE_LABEL[fit_new.rule]}「{_key_label(fit_new.rule, fit_new.key)}」で決まるので、"
                     f"寸法に合わせては変わりません(巾 {eff['巾適合min']}〜{eff['巾適合max']} /"
                     f" 丈 {eff['丈適合min']}〜{eff['丈適合max']})。合わなければ、続けて「適合」で直してください。")
        if eff["巾適合max"] > new_w or eff["丈適合max"] > new_l:
            warnings.append(f"いまの{RULE_LABEL[fit_new.rule]}の範囲は、新しい寸法 {new_w}×{new_l} より"
                            "大きい製品まで入っています。続けて「適合」で直してください。")
    summary = (f"{width}×{length} {_group_label(*_split_group(chosen[0]))} を {new_w}×{new_l} に"
               + ("入れ替えます(新しいコード)" if change_code else "直します(コードはそのまま)")
               + f"。変わる行 {len(affected)}行。")
    plan = Plan(id=uuid.uuid4().hex, kind=KIND_SIZE, created=time.time(), summary=summary, ops=ops,
                affected=_mark_targets(affected, moved), warnings=warnings, notes=notes,
                shared_path=shared.path, master_path=master, before_fits=before_all, after_fits=after_all,
                sizes_before={r["管理番号"]: (r["幅"], r["丈"]) for r in pallets}, sizes_after=sizes_after,
                labels={r["管理番号"]: _pallet_label(r) for r in pallets}, request=dict(req))
    return _keep(plan)


def _split_group(row: dict[str, Any]) -> tuple[str, str]:
    return (_industry(row.get("業界")), base_symbol(row.get("記号")))


def _limit_rows(master: Path, width: int, length: int, row: dict[str, Any]) -> list[dict[str, Any]]:
    """前の寸法を決め打っているパレット限定条件(業界が空か同じもの)。表が無ければ空。"""
    q = source_db.quote_identifier
    table = import_specs.PALLET_LIMIT_TABLE
    try:
        if table not in source_db.list_tables(master):
            return []
        found = source_db.read_query(
            master, f"SELECT rowid AS __行, * FROM {q(table)}"
                    " WHERE CAST(パレット幅 AS INTEGER) = ? AND CAST(パレット丈 AS INTEGER) = ?",
            [width, length])
    except source_db.SourceError:
        return []
    industry = normalize_key(_industry(row.get("業界")))
    return [r for r in found if not str(r.get("業界") or "").strip()
            or normalize_key(str(r.get("業界") or "")) == industry]


def _pattern_count(conn: sqlite3.Connection, width: int, length: int) -> int:
    total = 0
    for table in (config.TBL_PT_HEADER, config.TBL_PALLET_PATTERN):
        try:
            total += conn.execute(
                f"SELECT COUNT(*) FROM [{table}] WHERE CAST(パレット幅 AS INTEGER) = ?"
                " AND CAST(パレット丈 AS INTEGER) = ?", (width, length)).fetchone()[0]
        except sqlite3.Error:
            continue
    return total


def _merge_ops(shared: Shared, after_rows: list[dict[str, Any]], engine: Engine,
               width: int, length: int) -> tuple[list[Op], list[str]]:
    """この画面が分けた1寸法だけの帯が、**もうどのパレットにも使われない**ならつなぎ直す。

    つなげるのは、両隣の帯の適合が同じとき(分けたときと同じ形)だけ。違えば残して、そう言う。
    """
    ops: list[Op] = []
    notes: list[str] = []
    for table, size, dim, col in (("PalletHabaThreshold", width, "幅", "幅"),
                                  ("PalletDakeThreshold", length, "丈", "丈")):
        bands = pallet_threshold.select_rows(table, shared.conv[table])
        idx = next((i for i, b in enumerate(bands)
                    if _int(b.get("入力最小値")) == size == _int(b.get("入力最大値"))
                    and str(b.get("備考") or "").startswith(SPLIT_MARK)), None)
        if idx is None:
            continue
        users = [r for r in after_rows if r[col] == size and _fit(r, engine).rule == RULE_BAND]
        if users:
            notes.append(f"{dim}{size} だけの帯は、まだ {len(users)}行 が使っているので残します。")
            continue
        left = bands[idx - 1] if idx > 0 else None
        right = bands[idx + 1] if idx + 1 < len(bands) else None
        same = (left is not None and right is not None
                and (_int(left.get("適合最小値")), _int(left.get("適合最大値")))
                == (_int(right.get("適合最小値")), _int(right.get("適合最大値"))))
        if not same:
            notes.append(f"{dim}{size} だけの帯は使われなくなりますが、両隣の帯の範囲が違うのでそのまま残します。")
            continue
        stamp = _now()
        ops.append(Op(FILE_THRESHOLD, table, "update", left["__行"], _raw_of(shared, table, left["__行"]),
                      {"入力最大値": _int(right.get("入力最大値")), "更新日時": stamp},
                      f"{TABLE_LABEL[table]}を {_int(left.get('入力最小値'))}〜{_int(right.get('入力最大値'))}"
                      " につなぎ直します"))
        for band in (bands[idx], right):
            ops.append(Op(FILE_THRESHOLD, table, "delete", band["__行"], _raw_of(shared, table, band["__行"]),
                          {}, f"{TABLE_LABEL[table]} {_int(band.get('入力最小値'))}〜"
                              f"{_int(band.get('入力最大値'))} を消します(つなぎ直したので)"))
        notes.append(f"{dim}{size} だけの帯は使われなくなるので、元の帯につなぎ直します。")
    return ops, notes


# ==================================================================
# 製品で確かめる
# ==================================================================
def check_product(plan_id: str, width: Any, length: Any) -> dict[str, Any]:
    """その製品の候補になるパレットが、反映の前と後でどう変わるか。

    **適合範囲と現物の寸法だけ**で見る(±5mm・向きは両方)。業界の順や2山などの細かい決まりは
    資材選択で決まるので、ここでは言わない。
    """
    plan = get_plan(plan_id)
    w, l = _int(width), _int(length)
    if w <= 0 or l <= 0:
        raise FitError("製品の幅と丈を入れてください。")

    def hits(fits: dict[int, dict[str, int]], sizes: dict[int, tuple[int, int]]) -> set[int]:
        out = set()
        t = CHECK_TOLERANCE
        for n, f in fits.items():
            pw, pl = sizes.get(n, (0, 0))
            for a, b in ((w, l), (l, w)):
                if (f["巾適合min"] - t <= a <= f["巾適合max"] + t
                        and f["丈適合min"] - t <= b <= f["丈適合max"] + t
                        and a <= pw + t and b <= pl + t):
                    out.add(n)
                    break
        return out

    before = hits(plan.before_fits, plan.sizes_before)
    after = hits(plan.after_fits, plan.sizes_after)

    def names(ns: Iterable[int]) -> list[str]:
        return sorted(plan.labels.get(n, str(n)) for n in ns)[:30]
    return {"ok": True, "width": w, "length": l,
            "before": len(before), "after": len(after),
            "added": names(after - before), "removed": names(before - after),
            "kept": len(before & after),
            "note": "適合範囲と現物の寸法だけで見た候補です(±5mm・向きは両方)。業界の順や2山の決まりは資材選択で決まります。"}


# ==================================================================
# 反映
# ==================================================================
@dataclass
class ApplyResult:
    ok: bool
    message: str
    log_no: Optional[int] = None
    reason: str = ""


def _who() -> dict[str, str]:
    from . import access_control
    ident = access_control.current_identity()
    return {"pc": ident.pc_name, "login": ident.login_id}


def can_edit(conn: sqlite3.Connection) -> tuple[bool, str]:
    """この端末がパレット適合を直せるか(マスタ管理と同じ関門: 資材モード + 管理者認証)。"""
    from . import master_common
    return master_common.can_edit(conn, config.TBL_PALLET_MASTER)


def backup(path: Path, why: str = "パレット適合設定の前") -> Path:
    """書く前に共有のファイルを手元へ写す(`table_bring._backup` と同じ置き場所)。"""
    from . import app_config
    folder = app_config.local_dir("backup")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.stem}_{why}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}"
    shutil.copyfile(path, target)
    return target


def _same(now: Any, was: Any) -> bool:
    if now is None or was is None:
        return now is None and was is None
    try:
        return float(now) == float(was)
    except (TypeError, ValueError):
        return str(now) == str(was)


def _ensure_log(tx: Any) -> None:
    q = source_db.quote_identifier
    tx.execute(f"CREATE TABLE IF NOT EXISTS {q(LOG_TABLE)} ("
               " 番号 INTEGER PRIMARY KEY AUTOINCREMENT, 日時 TEXT NOT NULL DEFAULT '',"
               " 端末 TEXT NOT NULL DEFAULT '', 利用者 TEXT NOT NULL DEFAULT '',"
               " 種類 TEXT NOT NULL DEFAULT '', 内容 TEXT NOT NULL DEFAULT '',"
               " 行数 INTEGER NOT NULL DEFAULT 0, 操作 TEXT NOT NULL DEFAULT '',"
               " 戻した INTEGER NOT NULL DEFAULT 0, 戻した日時 TEXT NOT NULL DEFAULT '')")


def _like(sample: Any, value: Any) -> Any:
    """`value` を、その列に**いま入っている値と同じ形**で書く。

    現場の共有ファイルは Access から変換したもので、列に型が無く、数も文字('1030')で入っている。
    数で書くと同じ列に文字と数が混ざる(読む側は CAST でそろえているが、形はそろえておく)。
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(sample, str) and isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(sample, float) and isinstance(value, int):
        return float(value)
    return value


def _typed(tx: Any, table: str, values: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """書く値を、その行(無ければ表の1行目)の値の形にそろえる(`_like`)。"""
    sample = before
    if not sample:
        rows = tx.query(f"SELECT * FROM {source_db.quote_identifier(table)} LIMIT 1")
        sample = rows[0] if rows else {}
    return {k: (_like(sample.get(k), v) if k in sample else v) for k, v in values.items()}


def _run_ops(path: Path, ops: list[Op], *,
             finish: Optional[Callable[[Any, list[dict[str, Any]]], Any]] = None
             ) -> tuple[list[dict[str, Any]], Any]:
    """1つのファイルへ ops をまとめて書く(**全部か、何もしないか**)。反映前の確かめもここで。

    消す・直す行は、見本を作ったときの値(`Op.before`)と**いまの値が全部同じか**を同じ
    トランザクションの中で確かめる。違えば何も書かずに止める(ほかの端末が先に直した)。
    `finish(tx, done)` は同じトランザクションの最後に呼ぶ(変更の記録を書く)。
    返すのは、元に戻すための記録(書いた行の rowid・前・後)と、`finish` の返した値。
    """
    q = source_db.quote_identifier
    done: list[dict[str, Any]] = []
    extra = None
    with source_db.connect(path) as src:
        with src.transaction() as tx:
            next_no: dict[str, int] = {}
            for op in ops:
                if op.action in ("update", "delete"):
                    now = tx.query(f"SELECT * FROM {q(op.table)} WHERE rowid = ?", [op.rowid])
                    if not now or not all(_same(now[0].get(k), v) for k, v in op.before.items()):
                        raise FitError(f"{TABLE_LABEL.get(op.table, op.table)}が、見本を作ったあとに"
                                       "ほかで変わりました。もう一度「変わるところを見る」を押してください。",
                                       REFUSE_CHANGED)
                if op.action == "update":
                    values = _typed(tx, op.table, op.values, now[0])
                    sets = ", ".join(f"{q(k)} = ?" for k in values)
                    tx.execute(f"UPDATE {q(op.table)} SET {sets} WHERE rowid = ?",
                               [*values.values(), op.rowid])
                    done.append({**op.to_dict(), "after": values})
                elif op.action == "delete":
                    tx.execute(f"DELETE FROM {q(op.table)} WHERE rowid = ?", [op.rowid])
                    done.append(op.to_dict())
                elif op.action == "insert":
                    present = {r["name"] for r in tx.query(f"PRAGMA table_info({q(op.table)})")}
                    values = {k: v for k, v in op.values.items() if k in present}
                    # 番号の列は空にしない(Access から変換した表は自動で番号が入らない)。元に戻すときは元の番号
                    number = "管理番号" if op.table == config.TBL_PALLET_MASTER else "ID"
                    if number in present:
                        values[number] = _free_number(tx, op.table, number, values.get(number), next_no)
                    values = _typed(tx, op.table, values, {})
                    rid = tx.insert(op.table, values)
                    done.append({**op.to_dict(), "rowid": rid, "after": values})
            if finish is not None:
                extra = finish(tx, done)
    return done, extra


def _precheck(path: Path, ops: list[Op]) -> None:
    """書く前に、消す・直す行がまだ見本のときのままかを**読むだけで**確かめる。

    2つのファイルに書くとき、1つ目を書いてから2つ目で「変わっていた」と止まるのを減らす
    (本当の確かめは `_run_ops` が書くトランザクションの中でもう一度する)。
    """
    q = source_db.quote_identifier
    for op in ops:
        if op.action not in ("update", "delete"):
            continue
        now = source_db.read_query(path, f"SELECT * FROM {q(op.table)} WHERE rowid = ?", [op.rowid])
        if not now or not all(_same(now[0].get(k), v) for k, v in op.before.items()):
            raise FitError(f"{TABLE_LABEL.get(op.table, op.table)}が、見本を作ったあとにほかで変わりました。"
                           "もう一度「変わるところを見る」を押してください。", REFUSE_CHANGED)


def _free_number(tx: Any, table: str, column: str, wanted: Any, next_no: dict[str, int]) -> int:
    """足す行の番号(パレットの管理番号・閾値の表の ID)。元に戻すときは**元の番号**を使う(空いていれば)。
    無ければ最大+1。"""
    q = source_db.quote_identifier
    if wanted not in (None, ""):
        taken = tx.query(f"SELECT 1 FROM {q(table)} WHERE CAST({q(column)} AS INTEGER) = ?", [_int(wanted)])
        if not taken:
            return _int(wanted)
    if table not in next_no:
        top = tx.query(f"SELECT MAX(CAST({q(column)} AS INTEGER)) AS n FROM {q(table)}")
        next_no[table] = int(top[0]["n"] or 0) + 1
    number = next_no[table]
    next_no[table] += 1
    return number


def _log_writer(entry: dict[str, Any], prefix: list[dict[str, Any]]
                ) -> Callable[[Any, list[dict[str, Any]]], int]:
    """変更の記録を、閾値マスタへの書き込みと**同じトランザクション**で書く手。"""
    def write(tx: Any, done: list[dict[str, Any]]) -> int:
        _ensure_log(tx)
        return tx.insert(LOG_TABLE, {**entry, "操作": json.dumps(prefix + done, ensure_ascii=False,
                                                                 default=str)})
    return write


def apply(conn: sqlite3.Connection, plan_id: str) -> ApplyResult:
    """見本どおりに共有のマスタへ書いて、手元も追いつかせる。

    書く順は 梱包資材マスタ(パレットの行)→ 閾値マスタ(帯・表と変更の記録)。
    ファイルをまたいだ「全部か何もしないか」はできないので、パレットの行を書けたあとに
    閾値マスタで止まったときは、**記録だけでも残して**元に戻せるようにする。
    """
    try:
        plan = get_plan(plan_id)
    except FitError as exc:
        return ApplyResult(False, str(exc), reason=exc.reason)
    allowed, why = can_edit(conn)
    if not allowed:
        return ApplyResult(False, why, reason=REFUSE_NOT_ALLOWED)
    if not plan.ops:
        return ApplyResult(False, "変えるものがありません。", reason=REFUSE_BAD_INPUT)
    master_ops = [o for o in plan.ops if o.file == FILE_MASTER]
    threshold_ops = [o for o in plan.ops if o.file == FILE_THRESHOLD]
    try:
        threshold = _threshold_path()
        if plan.shared_path and Path(threshold) != Path(plan.shared_path):
            raise FitError("パレット閾値マスタの置き場所が変わりました。見本を作り直してください。", REFUSE_CHANGED)
        master = _master_path() if master_ops else None
        if master_ops and plan.master_path and Path(master) != Path(plan.master_path):
            raise FitError("梱包資材マスタの置き場所が変わりました。見本を作り直してください。", REFUSE_CHANGED)
    except FitError as exc:
        return ApplyResult(False, str(exc), reason=exc.reason)

    who = _who()
    entry = {"日時": _now(), "端末": who["pc"], "利用者": who["login"],
             "種類": "サイズ" if plan.kind == KIND_SIZE else "適合", "内容": plan.summary,
             "行数": len(plan.affected)}
    backups: list[str] = []
    done_master: list[dict[str, Any]] = []
    try:
        if master_ops:
            _precheck(threshold, threshold_ops)
            backups.append(str(backup(master)))
            done_master, _ = _run_ops(master, master_ops)
        backups.append(str(backup(threshold)))
        _, log_no = _run_ops(threshold, threshold_ops, finish=_log_writer(entry, done_master))
    except (FitError, source_db.SourceError, OSError) as exc:
        reason = exc.reason if isinstance(exc, FitError) else REFUSE_NO_SOURCE
        if not done_master:
            log.warning("パレット適合設定: 共有へ書けませんでした: %s", exc)
            text = str(exc) if isinstance(exc, FitError) else f"共有のマスタへ書けませんでした: {exc}"
            return ApplyResult(False, text, reason=reason)
        # パレットの行は書けた。帯のつなぎ直しはあきらめて、記録だけ残す(元に戻せるように)
        log.error("パレット適合設定: パレットは直したが閾値マスタで止まりました: %s", exc)
        log_no = None
        try:
            _, log_no = _run_ops(threshold, [], finish=_log_writer(
                {**entry, "内容": plan.summary + "(帯のつなぎ直しはできませんでした)"}, done_master))
        except (FitError, source_db.SourceError, OSError) as again:
            log.error("パレット適合設定: 変更の記録も書けませんでした: %s / 書いた行: %s", again,
                      json.dumps(done_master, ensure_ascii=False, default=str))
        follow = _follow(conn, threshold, master)
        return ApplyResult(False, f"パレットの行は直しましたが、閾値マスタで止まりました({exc})。"
                                  + ("変更の記録は残したので、元に戻せます。" if log_no else
                                     "変更の記録も残せませんでした(ログに書いた行を残しました)。")
                                  + follow, log_no=log_no, reason=reason)
    with _plans_lock:
        _plans.pop(plan.id, None)
    follow = _follow(conn, threshold, master)
    log.info("パレット適合設定を反映しました(記録 %s): %s 控え %s", log_no, plan.summary, " / ".join(backups))
    return ApplyResult(True, f"反映しました。{plan.summary}{follow}", log_no=log_no)


def _import(conn: sqlite3.Connection, path: Path, tables: Iterable[str]) -> Any:
    from . import data_sync
    return data_sync.import_tables(conn, path, {t: import_specs.IMPORT_SPECS[t] for t in tables},
                                   required=import_specs.REQUIRED_KEY_COLUMNS,
                                   blank_is_missing=import_specs.BLANK_IS_MISSING,
                                   optional=import_specs.OPTIONAL_TABLES,
                                   fallbacks=import_specs.NULL_FALLBACKS)


MASTER_TABLES = (config.TBL_PALLET_MASTER, import_specs.PALLET_LIMIT_TABLE)


def _follow(conn: sqlite3.Connection, threshold: Path, master: Optional[Path]) -> str:
    """書いた表を手元へ取り込み直し、適合範囲を計算し直す。ほかの端末は心拍で気づく
    (`refresh_in_background`)。"""
    from . import master_state, sync_auto
    notes = []
    try:
        _import(conn, threshold, import_specs.THRESHOLD_TABLES)
        sync_auto.mark_imported(conn, threshold)
    except Exception as exc:                       # noqa: BLE001 - 書けたことは言う
        log.warning("閾値マスタを取り込み直せませんでした: %s", exc)
        notes.append("ただし閾値を手元へ取り込み直せませんでした(設定の「取り込み直す」を押してください)")
    if master is not None:
        try:
            got = _import(conn, master, MASTER_TABLES)
            if config.TBL_PALLET_MASTER not in got.imported:
                notes.append("ただしパレットを手元へ取り込み直せませんでした("
                             + (" / ".join(got.errors) or "理由不明") + ")")
        except Exception as exc:                   # noqa: BLE001
            log.warning("パレットを取り込み直せませんでした: %s", exc)
            notes.append("ただしパレットを手元へ取り込み直せませんでした")
    summary = pallet_service.recompute_fit_ranges(conn)
    if not summary.ok:
        notes.append(f"ただし適合範囲の再計算に失敗しました({summary.error})")
    master_state.changed()
    _note_seen_latest(threshold)
    tail = " この端末にはもう効いています。ほかの端末には30秒ほどで届きます(開き直しは要りません)。"
    return tail + ("" if not notes else " " + "。".join(notes) + "。")


# ==================================================================
# 記録・元に戻す
# ==================================================================
HISTORY_COLUMNS = ("番号", "日時", "端末", "利用者", "種類", "内容", "行数", "戻した", "戻した日時")


def history(limit: int = 30) -> list[dict[str, Any]]:
    try:
        path = _threshold_path()
        if LOG_TABLE not in source_db.list_tables(path):
            return []
        return source_db.read_query(
            path, f"SELECT {', '.join(HISTORY_COLUMNS)} FROM {source_db.quote_identifier(LOG_TABLE)}"
                  " ORDER BY 番号 DESC LIMIT ?", [int(limit)])
    except (FitError, source_db.SourceError):
        return []


KIND_UNDO = "元に戻す"


def undo(conn: sqlite3.Connection, log_no: Any) -> ApplyResult:
    """記録1つぶんを元に戻す。**戻す前に、いまの値が記録の「後」と同じか確かめる**
    (あとで直した人がいれば止める。新しい記録から順に戻せば通る)。"""
    allowed, why = can_edit(conn)
    if not allowed:
        return ApplyResult(False, why, reason=REFUSE_NOT_ALLOWED)
    q = source_db.quote_identifier
    number = _int(log_no)
    try:
        threshold = _threshold_path()
        rows = (source_db.read_query(threshold, f"SELECT * FROM {q(LOG_TABLE)} WHERE 番号 = ?", [number])
                if LOG_TABLE in source_db.list_tables(threshold) else [])
    except (FitError, source_db.SourceError) as exc:
        return ApplyResult(False, f"記録を読めません: {exc}", reason=REFUSE_NO_SOURCE)
    if not rows:
        return ApplyResult(False, "その記録はありません。", reason=REFUSE_NOT_FOUND)
    entry = rows[0]
    if _int(entry.get("戻した")):
        return ApplyResult(False, "この記録はもう元に戻してあります。", reason=REFUSE_BAD_INPUT)
    if str(entry.get("種類") or "") == KIND_UNDO:
        return ApplyResult(False, "「元に戻した」記録は戻せません(戻したい変更をもう一度反映してください)。",
                           reason=REFUSE_BAD_INPUT)
    try:
        done = json.loads(entry.get("操作") or "[]")
    except ValueError:
        return ApplyResult(False, "記録を読めません(中身が壊れています)。", reason=REFUSE_UNSAFE)
    reverse: dict[str, list[Op]] = {FILE_MASTER: [], FILE_THRESHOLD: []}
    for d in reversed(done):
        after = d.get("after") or {}
        before = d.get("before") or {}
        if d["action"] == "update":
            back = {k: before.get(k) for k in after}
            reverse[d["file"]].append(Op(d["file"], d["table"], "update", d["rowid"], dict(after), back,
                                         f"{TABLE_LABEL.get(d['table'], d['table'])}の行を元の値へ"))
        elif d["action"] == "insert":
            reverse[d["file"]].append(Op(d["file"], d["table"], "delete", d["rowid"],
                                         {k: v for k, v in after.items() if k != "更新日時"}, {},
                                         f"{TABLE_LABEL.get(d['table'], d['table'])}の足した行を消す"))
        elif d["action"] == "delete":
            reverse[d["file"]].append(Op(d["file"], d["table"], "insert", None, {}, dict(before),
                                         f"{TABLE_LABEL.get(d['table'], d['table'])}の消した行を戻す"))
    who = _who()
    undo_entry = {"日時": _now(), "端末": who["pc"], "利用者": who["login"], "種類": KIND_UNDO,
                  "内容": f"記録 {number} を元に戻しました: {entry.get('内容') or ''}",
                  "行数": _int(entry.get("行数"))}

    def finish(tx: Any, done_now: list[dict[str, Any]]) -> int:
        changed = tx.execute(f"UPDATE {q(LOG_TABLE)} SET 戻した = 1, 戻した日時 = ?"
                             " WHERE 番号 = ? AND 戻した = 0", [_now(), number])
        if changed != 1:
            raise FitError("この記録は、ほかの端末がいま元に戻しました。", REFUSE_CHANGED)
        return _log_writer(undo_entry, [])(tx, [])

    master = None
    try:
        if reverse[FILE_MASTER]:
            _precheck(threshold, reverse[FILE_THRESHOLD])
            master = _master_path()
            backup(master, "パレット適合設定を戻す前")
            _run_ops(master, reverse[FILE_MASTER])
        backup(threshold, "パレット適合設定を戻す前")
        _run_ops(threshold, reverse[FILE_THRESHOLD], finish=finish)
    except FitError as exc:
        text = str(exc).replace("見本を作ったあとに", "反映したあとに")
        if exc.reason == REFUSE_CHANGED:
            text += "(あとの記録から順に戻してください)"
        return ApplyResult(False, text, reason=exc.reason)
    except (source_db.SourceError, OSError) as exc:
        return ApplyResult(False, f"共有のマスタへ書けませんでした: {exc}", reason=REFUSE_NO_SOURCE)
    follow = _follow(conn, threshold, master)
    log.info("パレット適合設定: 記録 %s を元に戻しました", number)
    return ApplyResult(True, f"記録 {number} を元に戻しました。{follow}", log_no=number)


# ==================================================================
# 画面の状態
# ==================================================================
def _where_work() -> dict[str, Any]:
    from . import data_sync
    threshold, master = data_sync.find_threshold_db(), data_sync.find_material_db()
    return {"threshold": str(threshold) if threshold else "", "master": str(master) if master else "",
            "history": history()}


# 共有の置き場所と記録は**背景の1本で**見に行き、画面は上限までしか待たない(VER4.8.115 の決まり)
_where = bounded.Flight("pallet-fit-where", _where_work)
WHERE_WAIT_SEC = bounded.wait_seconds(2.0)


def state(conn: sqlite3.Connection, *, allowed: bool, why: str) -> dict[str, Any]:
    """ページの帯に出すもの。直せるか・認証・書く先・最近の変更・マスタの世代。"""
    from . import master_state, selection_session
    got = _where.run(WHERE_WAIT_SEC)
    where = got.value if got.done and got.error is None else got.late
    return {
        "can_edit": allowed, "why": why,
        "admin": bool(selection_session.get_session(conn).admin),
        "epoch": master_state.epoch(),
        "where": where,
        "checking": where is None,
    }


# ==================================================================
# ほかの端末が気づく(心拍から)
# ==================================================================
WATCH_MIN_SEC = 30.0
_watch_at = float("-inf")
_watch_lock = threading.Lock()
_watch_running = False
# いちばん新しく見た変更の記録の番号。None = まだ見ていない(最初の1回は数えるだけ)
_seen_log: Optional[int] = None
# 記録のこの種類が増えたら、パレット(梱包資材マスタ)も取り込み直す
SIZE_KINDS = ("サイズ", KIND_UNDO)


def _latest_log(path: Path) -> tuple[int, list[dict[str, Any]]]:
    if LOG_TABLE not in source_db.list_tables(path):
        return 0, []
    rows = source_db.read_query(path, f"SELECT 番号, 種類 FROM {source_db.quote_identifier(LOG_TABLE)}"
                                      " ORDER BY 番号 DESC LIMIT 50")
    return (_int(rows[0]["番号"]) if rows else 0), rows


def _note_seen_latest(path: Optional[Path] = None) -> None:
    global _seen_log
    try:
        _seen_log = _latest_log(path or _threshold_path())[0]
    except Exception:                              # noqa: BLE001 - 見張りの目印なので握る
        pass


def refresh_if_changed(conn: sqlite3.Connection) -> bool:
    """閾値マスタが変わっていれば7表を取り込み直して計算し直す。サイズの直しがあればパレットも。

    パレット(梱包資材マスタ)は、ふだんは発注の見張り(`data_sync.catch_up`)が取り込み直すが、
    開いている画面によっては走らない。サイズを直した記録が増えたときは、ここでも取り込む
    (送っていない在庫の行があれば `import_tables` が入れ替えを見送る)。
    """
    from . import data_sync, master_state, sync_auto
    global _seen_log
    path = data_sync.find_threshold_db()
    if path is None or not sync_auto.needs_import(conn, path):
        return False
    result = _import(conn, path, import_specs.THRESHOLD_TABLES)
    sync_auto.mark_imported(conn, path)
    latest, rows = _latest_log(path)
    size_changed = _seen_log is not None and any(
        _int(r["番号"]) > _seen_log and str(r.get("種類") or "") in SIZE_KINDS for r in rows)
    _seen_log = latest
    if size_changed:
        master = data_sync.find_material_db()
        if master is not None:
            _import(conn, master, MASTER_TABLES)
    if result.imported or size_changed:
        pallet_service.recompute_fit_ranges(conn)
        master_state.changed()
        log.info("パレット適合の変更に気づいて取り込み直しました(閾値 %s表%s)",
                 len(result.imported), "・パレット" if size_changed else "")
        return True
    return False


def refresh_in_background(*, now: Optional[float] = None) -> bool:
    """心拍から。**間を空け、同時に2つは走らせない**(`lot_plans.refresh_in_background` と同じ形)。

    共有を見に行くのは背景の1本だけ(画面の要求は待たない。VER4.8.115 の決まり)。
    """
    global _watch_at, _watch_running
    moment = time.monotonic() if now is None else now
    with _watch_lock:
        if _watch_running or moment - _watch_at < WATCH_MIN_SEC:
            return False
        _watch_at = moment
        _watch_running = True

    def runner() -> None:
        global _watch_running
        from . import db
        try:
            with db.connect() as conn:
                if _seen_log is None:
                    _note_seen_latest()
                refresh_if_changed(conn)
        except Exception:                          # noqa: BLE001 - 背景処理なので握る
            log.warning("パレット適合の変更の確かめで例外(手元の値で続けます)", exc_info=True)
        finally:
            with _watch_lock:
                _watch_running = False

    threading.Thread(target=runner, daemon=True, name="fit-watch").start()
    return True


def forget_watch() -> None:
    """試験用。"""
    global _watch_at, _seen_log, _watch_running
    with _watch_lock:
        _watch_at = float("-inf")
        _seen_log = None
        _watch_running = False
