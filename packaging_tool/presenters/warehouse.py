"""倉庫連携画面の表示内容

UIツールキットに依存しない。tkinter からも Flask からも同じものを呼ぶ。

【権限の分離がこの画面の要点】
VBA版は現場が使う送信側(`frmSendConfirm`)と、倉庫が使う受け側
(`frmWarehouseOrder`)が**別のフォーム**だった。
現場のアプリから「確認済みにする」ができてしまうと、倉庫が実際に
受け取ったかどうかに関わらず現場側の都合で確認済みにでき、
**確認という工程そのものが意味を失う**。

tkinter版は「現場アプリにはボタンをそもそも作らない」ことでこれを保った。
Web版は同じ保証をHTTPの層で作る ── 倉庫用のエンドポイントを
現場モードのサーバに**登録しない**(隠すのではなく、存在しない)。
この層は表示の形だけを持ち、どちらに出すかは `app/__init__.py` が決める。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import warehouse_service as svc

# 一覧の列。VBA `frmWarehouseOrder` の並びをそのまま引き継ぐ。
#   (見出し, 数値か, 幅を広めに取るか)
ORDER_COLUMNS: tuple[tuple[str, bool, bool], ...] = (
    ("管理番号", True, False),
    ("登録日時", False, True),
    ("LotNo", False, False),
    ("品名", False, True),
    ("発注コード", False, False),
    ("発注数", True, False),
    ("単位", False, False),
    ("材質", False, False),
    ("調質", False, False),
    ("厚", True, False),
    ("幅", True, False),
    ("丈", True, False),
    ("用途コード", False, False),
    ("納入先", False, True),
    ("状態", False, False),
)


@dataclass(frozen=True)
class ViewColumn:
    """一覧の1列の**見せ方**。事実そのものは `OrderRow.values` にしか無い。

    ここが持つのは「どれを主に、どれを従に、どんな幅で置くか」だけで、
    画面はこの指図どおりに `values` から拾って並べる。
    """

    label: str                    # 見出し
    primary: str                  # 主に出す列(`ORDER_COLUMNS` の見出し)
    sub: tuple[str, ...] = ()     # 従に小さく添える列。複数なら `sep` で繋ぐ
    sep: str = " "
    numeric: bool = False
    width: str = ""               # 固定割付(`table-layout:fixed`)での幅
    kind: str = "text"            # "text" / "status"


# 一覧の並べ方。**15列を横に並べると1列 40px しか無く、品名も納入先も
# 読めない。** 主(太字)と従(小さい灰色)の2行に組めば、事実を1つも
# 減らさずに列を8つに畳める ── 取り込み元の列定義(`ORDER_COLUMNS`)は
# そのままなので、倉庫側の帳票と食い違うこともない。
#
# 畳む相手は**必ず同じものの言い換えか付帯情報**にする。
# 「LotNo と品名」は同じ1本の材の呼び名だが、「LotNo と納入先」は
# 別々の事実なので畳まない ── 見た目の都合で意味を混ぜない。
#
# 幅の合計は 83%。残りは操作の列 ── ボタンは畳めない(切れたら押せない)
# ので、**先に確保してから**残りを読み物に配る。
ORDER_VIEW: tuple[ViewColumn, ...] = (
    ViewColumn("管理番号 / 登録日時", "管理番号", ("登録日時",),
               numeric=True, width="10%"),
    ViewColumn("LotNo / 品名", "LotNo", ("品名",), width="16%"),
    ViewColumn("発注コード / 用途", "発注コード", ("用途コード",), width="11%"),
    ViewColumn("発注数 / 単位", "発注数", ("単位",), numeric=True, width="7%"),
    ViewColumn("材質 / 調質", "材質", ("調質",), width="9%"),
    ViewColumn("厚 / 幅×丈", "厚", ("幅", "丈"), sep=" × ",
               numeric=True, width="10%"),
    ViewColumn("納入先", "納入先", width="12%"),
    ViewColumn("状態", "状態", kind="status", width="8%"),
)
# 操作の列の幅。ボタン2つ(確認・取り消し)が並んでも切れない幅
ORDER_ACTION_WIDTH = "17%"

# 新規発注フォームの項目。VBA の入力欄の並びに合わせる。
#   (見出し, キー, 必須か, 数値か)
ORDER_FIELDS: tuple[tuple[str, str, bool, bool], ...] = (
    ("LotNo", "lot_no", True, False),
    ("品名", "hinmei", True, False),
    ("発注コード", "hatchu_code", True, False),
    ("単位", "tani", False, False),
    ("材質", "zaisitu", False, False),
    ("調質", "choshitu", False, False),
    ("厚", "atu", False, True),
    ("幅", "haba", False, True),
    ("丈", "take", False, True),
    ("用途コード", "yoto_code", False, False),
    ("納入先", "nounyusaki", False, False),
    ("発注数", "hatchu_suu", True, True),
)
REQUIRED_KEYS = tuple(key for _, key, required, _ in ORDER_FIELDS if required)
NUMERIC_KEYS = tuple(key for _, key, _, numeric in ORDER_FIELDS if numeric)

# 絞り込み。VBA `frmWarehouseOrder` の期間ボタンに対応する
DATE_FILTERS: tuple[tuple[str, str], ...] = (
    ("", "すべて"),
    ("today", "今日"),
    ("week", "1週間"),
    ("month", "1か月"),
)
DATE_FILTER_KEYS = frozenset(key for key, _ in DATE_FILTERS)

# 状態ごとの見た目。**色だけで伝えない**ので文言も持つ(§3.8)
# 状態ピルの種別。**共通の語彙**(components.css の `.st--*`)に合わせる。
# 画面ごとに別の名前を付けると、同じ状態が画面ごとに違う色になる
STATUS_KIND = {
    svc.STATUS_PENDING: "warn",      # 未確認 ── 倉庫がまだ受けていない
    svc.STATUS_CONFIRMED: "ok",      # 確認済
    svc.STATUS_CANCELLED: "off",     # 取消済 ── 中立 + 打ち消し線
}


@dataclass
class OrderRow:
    """一覧の1行。"""

    mgr_no: int
    values: dict[str, Any] = field(default_factory=dict)
    status: str = svc.STATUS_PENDING
    # 資材モードでその行に何ができるか。**判断はサーバが持つ**
    can_confirm: bool = False
    can_cancel: bool = False

    @property
    def status_kind(self) -> str:
        return STATUS_KIND.get(self.status, "todo")


@dataclass
class WarehouseViewModel:
    rows: list[OrderRow] = field(default_factory=list)
    keyword: str = ""
    date_filter: str = ""
    include_cancelled: bool = False
    message: str = ""
    # 未確認の件数。資材モードのレールに出す(行く前に分かる)
    pending: int = 0

    @property
    def found(self) -> int:
        return len(self.rows)


def build(conn: sqlite3.Connection, *, mode: str,
          keyword: str = "", date_filter: str = "",
          include_cancelled: bool = False) -> WarehouseViewModel:
    """一覧を組み立てる。

    `mode` で**できること**が変わる。現場は自分が送った
    ものを見るだけで、確認・取消は資材だけ(VBA のフォーム分けを
    そのまま引き継ぐ)。
    """
    from .. import modes

    if date_filter not in DATE_FILTER_KEYS:
        date_filter = ""
    is_material = modes.normalize(mode) == modes.MATERIAL

    raw = svc.list_orders(
        conn, keyword=keyword.strip(),
        date_filter=date_filter or None,
        # 現場は自分が取り消したものも見えたほうがよい(送った履歴なので)。
        # 資材は取り消し済みを既定では出さない(処理する対象ではない)
        include_cancelled=include_cancelled or not is_material)

    rows = [_row(item, is_material=is_material) for item in raw]
    pending = sum(1 for r in rows if r.status == svc.STATUS_PENDING)
    return WarehouseViewModel(
        rows=rows, keyword=keyword, date_filter=date_filter,
        include_cancelled=include_cancelled,
        pending=pending,
        message=("" if rows else "該当する発注はありません。"),
    )


def _row(item: dict, *, is_material: bool) -> OrderRow:
    status = item.get("状態", svc.STATUS_PENDING)
    return OrderRow(
        mgr_no=item.get("管理番号", 0),
        values={label: _cell(item.get(label)) for label, _, _ in ORDER_COLUMNS},
        status=status,
        # **確認は倉庫だけ。** 現場からできてしまうと、倉庫が実際に
        # 受け取ったかに関わらず現場の都合で確認済みにでき、確認という
        # 工程が意味を失う
        can_confirm=is_material and status == svc.STATUS_PENDING,
        # **取り消しは現場でもできる。** VBAでも取り消しは現場の送信確認
        # ダイアログ(`frmSendConfirm.btnDelete_Click`)の操作で、資材側の
        # `frmWarehouseOrder` にあったのは確認だけだった。移植のときに
        # 確認とまとめて資材専用にしてしまい、送った本人が間違いに
        # 気づいても引っ込められなくなっていた(現場の声)。
        # 止めるのは状態だけ ── 倉庫が確認したあとは誰も取り消せない
        can_cancel=status == svc.STATUS_PENDING,
    )


def _cell(value: Any) -> Any:
    return "" if value is None else value


# ------------------------------------------------------------------
# 新規発注
# ------------------------------------------------------------------
# EX受注の行で**空のまま通す**欄。
#
# EXの実データは別の職場から別途届き、そちらが優先される。こちらから
# 中身のある値を送ると突き合わせで混乱するため、意図的に空で送る
# (組み立ては `presenters/outputs.build_orders`)。
EX_BLANK_KEYS = ("hatchu_code", "tani", "hatchu_suu")


def is_ex_order(body: dict) -> bool:
    """EX受注の行か。**品名の文字から推し量らない。**

    `hinmei == "EX"` で判定すると、たまたま品名が EX の通常発注まで
    検査を素通りする。旗は組み立てた側が立てる。
    """
    return bool(body.get("is_ex_order"))


def validate(body: dict) -> tuple[dict[str, Any], Optional[tuple[str, str]]]:
    """入力を整える。おかしければ (欄, 理由) を返す。

    **通してよいかを決めるのはサーバ**。画面側の検査は打ち間違いを
    早く知らせるためのもの。
    """
    values = {key: str(body.get(key, "")).strip()
              for _, key, _, _ in ORDER_FIELDS}
    ex = is_ex_order(body)

    for label, key, required, _ in ORDER_FIELDS:
        if ex and key in EX_BLANK_KEYS:
            continue                    # EXは空が正しい。必須にしない
        if required and not values[key]:
            return values, (key, f"{label}を入れてください")

    for label, key, required, numeric in ORDER_FIELDS:
        if not numeric or not values[key]:
            continue
        try:
            float(values[key])
        except ValueError:
            return values, (key, f"{label}は数字で入れてください")

    # 数値の欄は空でも通す(VBA も空欄を許して 0 として扱っていた)。
    # ただし発注数だけは必須なので、上の検査で捕まる
    for key in NUMERIC_KEYS:
        if ex and key in EX_BLANK_KEYS:
            continue                    # **0 で埋めない。** 空のまま送る
        if not values[key]:
            values[key] = "0"
    values["is_ex_order"] = ex
    return values, None


# ------------------------------------------------------------------
# JSON
# ------------------------------------------------------------------
def to_dict(view: WarehouseViewModel) -> dict[str, Any]:
    return {
        # 事実は `rows[].values` にしか無い。ここは並べ方の指図だけを渡す
        "columns": [{"label": c.label, "primary": c.primary,
                     "sub": list(c.sub), "sep": c.sep,
                     "numeric": c.numeric, "width": c.width, "kind": c.kind}
                    for c in ORDER_VIEW],
        "rows": [row_dict(r) for r in view.rows],
        "keyword": view.keyword,
        "date_filter": view.date_filter,
        "include_cancelled": view.include_cancelled,
        "message": view.message,
        "pending": view.pending,
        "found": view.found,
        # EX受注の行で空のまま送る欄。**画面側で決めない**(どの欄が
        # 空でよいかを知っているのはここだけ。2か所で持つとずれる)
        "ex_blank_keys": list(EX_BLANK_KEYS),
    }


def row_dict(row: OrderRow) -> dict[str, Any]:
    return {
        "mgr_no": row.mgr_no,
        "values": row.values,
        "status": row.status,
        "status_kind": row.status_kind,
        "can_confirm": row.can_confirm,
        "can_cancel": row.can_cancel,
    }


def action_dict(result: svc.ActionResult) -> dict[str, Any]:
    return {"ok": result.ok, "message": result.message}


def order_dict(result: svc.OrderResult) -> dict[str, Any]:
    return {"ok": result.ok, "message": result.message, "mgr_no": result.mgr_no}
