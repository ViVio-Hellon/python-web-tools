"""作業ラインの判定 ── 実績ダッシュボードの条件表で、ロットがどのラインの作業になるかを決める(VER4.8.144)

現場の声:
    「actualsdashboard_条件表.sqlite3: テーブル 条件表 にライン作業条件があります。
     LVC と HVC と SVC は VC の設備です」
    「設定タブに作業ラインタブの作成。どのラインで作業になるかのテストを行えるように」
    「倉庫連携時に、仕掛データと照らし合わせて条件に合うもの(作業ラインに入っているもの)を
     作業予定ラインにセットし、倉庫へ送信を押した段階で作業予定ラインに入っていなければ自動で出力」

【条件表】実績ダッシュボード(actuals-dashboard)が「条件表の出力先」に書く .sqlite3。**このツールは読むだけ**。
置き場所は梱包資材マスタと同じフォルダ(`config.master_db_dir()`)。1行1グループで、決まり方は表「情報」の
「決まり方」のとおり(実績ダッシュボードの `actuals/engine.py classify` と同じ):

    - 見る順番の順に見て、最初に当てはまったグループに入る
    - VC 付きのロット(仕掛の VC が 01)は、VC優先=1 のグループに当てはまればそちらを優先(そこで決まる)
    - 除外を使う=1 のグループは、除外_ の条件に全部当てはまるロットを入れない(その行だけ外して次の行へ)
    - 空(NULL)は条件なし。MIN・MAX は両端を含む(MINより大・MAX未満は含まない)
    - 材質はカンマ区切り・末尾 * で前方一致
    - 寸法は「寸法の基準」(ｵｰﾀﾞｰ / 製造)の値。製造材質は製造材質、単重は製造寸法×比重で計算した値で比べる
    - 板でない(形状 30 かつ製造板丈 0)ロットは、どのグループにも入らない

【グループ → ライン】グループ名をラインの正規の呼び名(`line_names.OFFICIAL`)に読み替える。全角半角をそろえ、
後ろの「(…)」と「ライン」を外して当たれば、そのライン(ﾄｯﾄ(3SR) → トット、ﾊﾞﾗﾝｻｰﾗｲﾝ → バランサー)。
当たらないグループ(丸徳ﾗｲﾝ・生地その他・VCその他 など)は作業ラインではない ── 作業予定ラインには入れない。

**読めないときは決めない**(条件表・仕掛を読めなければ「分かりません」と言い、作業予定ラインに勝手に入れない)。
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config
from .logging_utils import get_logger

log = get_logger("work_lines")

FILE_NAME = "actualsdashboard_条件表.sqlite3"
TABLE = "条件表"
INFO_TABLE = "情報"
LOT_TABLE = "仕掛ロット"

# 比べる項目(条件表の列の頭)。寸法は「寸法の基準」の列で、材質は「材質」(寸法の基準の材質)で比べる
_DIMS = ("板厚", "板幅", "板丈")
_BASIS = {"ｵｰﾀﾞｰ": {"材質": "オーダー材質", "板厚": "オーダー板厚", "板幅": "オーダー板幅", "板丈": "オーダー板丈"},
          "製造": {"材質": "製造材質", "板厚": "製造板厚", "板幅": "製造板幅", "板丈": "製造板丈"}}


@dataclass
class Group:
    rank: int
    number: int
    name: str
    vc_first: bool
    use_not: bool
    cond: dict[str, Any]
    not_cond: dict[str, Any]
    line: str = ""            # 当たるラインの正規の呼び名(作業ラインでなければ空)


@dataclass
class Table:
    path: Path
    groups: list[Group]
    info: dict[str, str]

    @property
    def basis(self) -> str:
        return _basis_name(self.info.get("寸法の基準"))

    @property
    def basis_not(self) -> str:
        return _basis_name(self.info.get("除外の寸法の基準"))


@dataclass
class Decision:
    """ロット1件の判定。`line` が空なら作業予定ラインには入れない(`why` に理由)。"""

    lot_no: str
    line: str = ""
    group: str = ""
    why: str = ""
    known: bool = False                       # 条件表と仕掛を読めて、判定できた
    values: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"lot_no": self.lot_no, "line": self.line, "group": self.group, "why": self.why,
                "known": self.known, "values": self.values, "steps": self.steps}


class Unreadable(Exception):
    """条件表を読めない(無い・壊れている)。"""


def _basis_name(text: Any) -> str:
    return "製造" if unicodedata.normalize("NFKC", str(text or "")).strip() == "製造" else "ｵｰﾀﾞｰ"


def group_line(name: Any) -> str:
    """グループ名 → ラインの正規の呼び名。作業ラインでなければ空。"""
    from . import line_names
    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    text = re.sub(r"[((][^))]*[))]\s*$", "", text).strip()
    for candidate in (text, re.sub(r"ライン$", "", text).strip()):
        hit = line_names.official(candidate)
        if hit:
            return hit
    return ""


# ------------------------------------------------------------------
# 条件表を読む
# ------------------------------------------------------------------
def find() -> Optional[Path]:
    """条件表のファイル(梱包資材マスタと同じフォルダ)。無ければ None。"""
    from . import source_db
    return source_db.find(Path(config.master_db_dir()), FILE_NAME)


# 読んだ条件表の控え {パス: (ファイルの姿, 表)}。倉庫連携が開くたびに共有から写し直さない
_cache: dict[str, tuple[tuple[int, int], "Table"]] = {}


def load(path: Optional[Path] = None) -> Table:
    """条件表を読む。**読めなければ `Unreadable`**(空の表として扱わない)。ファイルが変わっていなければ控えを返す。"""
    from . import sync_sources
    path = path or find()
    if path is None:
        raise Unreadable(f"{FILE_NAME} が梱包資材マスタのフォルダ({config.master_db_dir()})にありません。"
                         "実績ダッシュボードの「条件表の出力先」をこのフォルダにして、条件表を保存してください。")
    stamp = sync_sources.source_stamp(path)
    held = _cache.get(str(path))
    if stamp is not None and held is not None and held[0] == stamp:
        return held[1]
    table = _read(path)
    if stamp is not None:
        _cache[str(path)] = (stamp, table)
    return table


def _read(path: Path) -> Table:
    from . import sync_sources
    try:
        rows = sync_sources.read_table(path, TABLE)
    except sync_sources.SyncError as exc:
        raise Unreadable(f"{path.name} の {TABLE} を読めません: {exc}") from exc
    info: dict[str, str] = {}
    try:
        info = {str(r.get("項目") or ""): str(r.get("値") or "") for r in sync_sources.read_table(path, INFO_TABLE)}
    except sync_sources.SyncError as exc:
        log.warning("%s の %s を読めません(寸法の基準は ｵｰﾀﾞｰ として続けます): %s", path.name, INFO_TABLE, exc)
    groups: list[Group] = []
    for index, row in enumerate(rows, start=1):
        name = str(row.get("グループ名") or "").strip()
        if not name:
            continue
        cond = {k: row.get(k) for k in row if not str(k).startswith("除外_")}
        not_cond = {k[3:]: row.get(k) for k in row if str(k).startswith("除外_")}
        groups.append(Group(rank=_int(row.get("見る順番"), index), number=_int(row.get("番号"), index), name=name,
                            vc_first=_int(row.get("VC優先"), 0) == 1, use_not=_int(row.get("除外を使う"), 0) == 1,
                            cond=cond, not_cond=not_cond, line=group_line(name)))
    groups.sort(key=lambda g: g.rank)
    return Table(path=path, groups=groups, info=info)


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------
# 比べる
# ------------------------------------------------------------------
def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = unicodedata.normalize("NFKC", str(value)).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return unicodedata.normalize("NFKC", str(value)).strip()


def mat_match(cond: Any, value: Any) -> bool:
    """材質の条件。カンマ区切りの並び・末尾 * で前方一致。数に見えるものは数として比べる(5083 = "5083")。"""
    if _blank(cond):
        return True
    text = _text(value)
    for part in _text(cond).replace("、", ",").split(","):
        p = part.strip()
        if not p:
            continue
        if p == "*":
            return True
        if p.endswith("*"):
            if text.startswith(p[:-1]):
                return True
            continue
        a, b = _num(p), _num(text)
        if (a is not None and b is not None and a == b) or p == text:
            return True
    return False


def _range(cond: dict[str, Any], head: str, value: Optional[float]) -> bool:
    """MIN≦ ≦MAX、MINより大(<)、MAX未満(<)。空は条件なし。値が読めなければ 0 として比べる(実績ダッシュボードと同じ)。"""
    v = value if value is not None else 0.0
    checks = ((f"{head}MIN", lambda c: c <= v), (f"{head}MAX", lambda c: v <= c),
              (f"{head}MINより大", lambda c: c < v), (f"{head}MAX未満", lambda c: v < c))
    for key, ok in checks:
        c = _num(cond.get(key))
        if c is not None and not ok(c):
            return False
    return True


def _checks(cond: dict[str, Any], lot: dict[str, Any], basis: str) -> list[tuple[str, bool]]:
    cols = _BASIS[basis]
    out = [("材質", mat_match(cond.get("材質"), lot.get(cols["材質"])))]
    if not _blank(cond.get("製造材質")):
        out.append(("製造材質", mat_match(cond.get("製造材質"), lot.get("製造材質"))))
    for head in _DIMS:
        out.append((head, _range(cond, head, _num(lot.get(cols[head])))))
    out.append(("単重", _range(cond, "単重", unit_weight(lot))))
    return out


def unit_weight(lot: dict[str, Any]) -> float:
    """計算単重 = 製造板厚 × 製造板幅 × 製造板丈 × 比重 ÷ 10^6(小数2桁。実績ダッシュボードと同じ)。"""
    product = 1.0
    for key in ("製造板厚", "製造板幅", "製造板丈", "比重"):
        product *= _num(lot.get(key)) or 0.0
    return round(product / 10 ** 6 + 0.0, 2)


def vc_on(lot: dict[str, Any]) -> bool:
    return _num(lot.get("VC")) == 1


def is_plate(lot: dict[str, Any]) -> bool:
    """板か(形状が 30 でない、または製造板丈がある)。板でなければどのグループにも入らない。"""
    return _num(lot.get("形状")) != 30 or (_num(lot.get("製造板丈")) or 0) > 0


def classify(table: Table, lot: dict[str, Any]) -> tuple[Optional[Group], list[dict[str, Any]]]:
    """ロット(仕掛の1行)がどのグループに入るか。入らなければ None。道筋(画面のテストに出す)も返す。"""
    steps: list[dict[str, Any]] = []
    chosen: Optional[Group] = None
    vc = vc_on(lot)
    for group in table.groups:
        step: dict[str, Any] = {"rank": group.rank, "name": group.name, "line": group.line,
                                "vc_first": group.vc_first}
        if group.use_not:
            nc = _checks(group.not_cond, lot, table.basis_not)
            if all(ok for _, ok in nc):
                step.update(result="除外に当たったので外す", ok=False,
                            failed=[])
                steps.append(step)
                continue
        if not is_plate(lot):
            step.update(result="板ではないので対象外", ok=False, failed=[])
            steps.append(step)
            continue
        checks = _checks(group.cond, lot, table.basis)
        failed = [name for name, ok in checks if not ok]
        if failed:
            step.update(result="外れ", ok=False, failed=failed)
            steps.append(step)
            continue
        if chosen is None:
            chosen = group
            step.update(result="最初に当たった", ok=True, failed=[])
        else:
            step.update(result="当たり(先の行で決まり済み)", ok=True, failed=[])
        if group.vc_first and vc:
            chosen = group
            step["result"] = "VC 付きのロットが VC優先のグループに当たったので、ここで決まり"
            steps.append(step)
            break
        steps.append(step)
    return chosen, steps


# ------------------------------------------------------------------
# ロットを判定する
# ------------------------------------------------------------------
_LOT_COLUMNS = ("ロット番号", "オーダー材質", "製造材質", "オーダー板厚", "オーダー板幅", "オーダー板丈",
                "製造板厚", "製造板幅", "製造板丈", "比重", "形状", "VC")


def lot_values(conn: sqlite3.Connection, lot_no: str) -> Optional[dict[str, Any]]:
    """仕掛(手元の仕掛ロット)のそのロットの先頭行。無ければ None。読めなければ例外。"""
    from .lot_plans import normalize_lot
    cols = ", ".join(f'"{c}"' for c in _LOT_COLUMNS)
    row = conn.execute(f'SELECT {cols} FROM "{LOT_TABLE}" WHERE UPPER(TRIM(ロット番号)) = ?'
                       " ORDER BY 管理番号 LIMIT 1", (normalize_lot(lot_no),)).fetchone()
    return dict(zip(_LOT_COLUMNS, tuple(row))) if row is not None else None


def decide(conn: sqlite3.Connection, lot_no: str, *, table: Optional[Table] = None,
           lot: Optional[dict[str, Any]] = None) -> Decision:
    """そのロットがどの作業ラインに入るか。決められなければ `line` が空で `why` に理由。"""
    from .lot_plans import normalize_lot
    lot_no = normalize_lot(lot_no)
    result = Decision(lot_no=lot_no)
    if not lot_no:
        result.why = "ロットNOがありません。"
        return result
    try:
        table = table or load()
    except Unreadable as exc:
        result.why = str(exc)
        return result
    if lot is None:
        try:
            lot = lot_values(conn, lot_no)
        except sqlite3.Error as exc:
            result.why = f"仕掛を読めません: {exc}"
            return result
        if lot is None:
            result.why = f"Lot {lot_no} は仕掛にありません(仕掛台帳を取り込み直すと出ることがあります)。"
            return result
    result.values = {**{k: lot.get(k) for k in _LOT_COLUMNS if k != "ロット番号"},
                     "計算単重": unit_weight(lot), "寸法の基準": table.basis}
    group, result.steps = classify(table, lot)
    result.known = True
    if group is None:
        result.why = "条件表のどのグループにも当たりません。"
        return result
    result.group = group.name
    if not group.line:
        result.why = f"グループ「{group.name}」は作業ラインではないので、作業予定ラインには入れません。"
        return result
    result.line = group.line
    return result
