"""倉庫連携画面の表示内容

UIツールキットに依存しない。tkinter からも Flask からも同じものを呼ぶ。

【権限の分離がこの画面の要点】
VBA版は現場が使う送信側(`frmSendConfirm`)と、倉庫が使う受け側
(`frmWarehouseOrder`)が**別のフォーム**だった。役割は3つに分かれる:

    出せるのは現場だけ
    確認できるのは倉庫だけ
    取り消せるのは現場だけ、かつ倉庫が確認する前だけ

現場のアプリから「確認済みにする」ができてしまうと、倉庫が実際に
受け取ったかどうかに関わらず現場側の都合で確認済みにでき、
**確認という工程そのものが意味を失う**。逆に倉庫から取り消せると、
出した覚えのない側が依頼を消せてしまう。

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
# **発注コードと用途コードは畳まない。** 用途は製品が何に使われるかで、
# 発注コードはパレットの品番。**互いに何の関係も無い**ので、上下に
# 組むと「このコードの用途」と読めてしまう(現場の声:「発注コード /
# 用途は違和感。用途は製品の用途であってコードとは何ら関係がない」)。
# 残りの組(発注数/単位・材質/調質・厚/幅×丈)は、どれも同じものの
# 言い換えか単位なのでそのまま。
#
# 幅の合計は 83%。残りは操作の列 ── ボタンは畳めない(切れたら押せない)
# ので、**先に確保してから**残りを読み物に配る。
ORDER_VIEW: tuple[ViewColumn, ...] = (
    ViewColumn("管理番号 / 登録日時", "管理番号", ("登録日時",),
               numeric=True, width="10%"),
    ViewColumn("LotNo / 品名", "LotNo", ("品名",), width="14%"),
    ViewColumn("発注コード", "発注コード", width="8%"),
    ViewColumn("用途", "用途コード", width="6%"),
    ViewColumn("発注数 / 単位", "発注数", ("単位",), numeric=True, width="7%"),
    ViewColumn("材質 / 調質", "材質", ("調質",), width="8%"),
    ViewColumn("厚 / 幅×丈", "厚", ("幅", "丈"), sep=" × ",
               numeric=True, width="10%"),
    ViewColumn("納入先", "納入先", width="10%"),
    ViewColumn("状態", "状態", kind="status", width="7%"),
)
# 1行を開いたときに、大きく出す項目の並び。
#
# **一覧は詰めて並べるので小さい。** 現場からは「選択したものを大きく
# 表示してほしい」「発注コードの自動コピーが効いていない」と言われた。
# 開いて確かめる場所を作り、そこで発注コードを控えておく。
#
# 並びは「どの発注か」→「何を頼んだか」→「どこへ」の順。
#   (見出し, 列名, 大きく出すか)
ORDER_DETAIL: tuple[tuple[str, str, bool], ...] = (
    ("発注コード", "発注コード", True),
    ("LotNo", "LotNo", True),
    ("品名", "品名", False),
    ("発注数", "発注数", True),
    ("単位", "単位", False),
    ("材質", "材質", False),
    ("調質", "調質", False),
    ("厚", "厚", False),
    ("幅", "幅", False),
    ("丈", "丈", False),
    ("用途コード", "用途コード", False),
    ("納入先", "納入先", False),
    ("管理番号", "管理番号", False),
    ("登録日時", "登録日時", False),
)

# 行を開いたときに自動で控える列。**発注コードは打ち写す値**で、
# 8桁前後を目で読んで別のシステムへ入れ直すことになる
ORDER_COPY_COLUMN = "発注コード"

# 操作の列の幅。ボタンが3つ(Lotを開く・確認・取り消し)並んでも
# 切れない幅。足りないと列幅を分け合って互いに重なる
ORDER_ACTION_WIDTH = "20%"

# 新規発注フォームの項目。VBA の入力欄の並びに合わせる。
#   (見出し, キー, 必須か, 数値か, 横に広げるか)
#
# **広げる欄がある。** 品名(「5×10 1550x3100」)と納入先
# (「ﾊｸﾄﾞｳ(ｶ)ｶﾅｶﾞﾜｼﾞｷﾞｮｳ」)は他より倍近く長く、他の欄と同じ幅だと
# 入れた値の後ろが切れて読めない(現場の声:「品名が見切れて見えない」)。
# 入力欄は打った内容を**確かめる**ためのものなので、読めないのは
# 入っていないのと同じ
ORDER_FIELDS: tuple[tuple[str, str, bool, bool, bool], ...] = (
    ("LotNo", "lot_no", True, False, False),
    ("品名", "hinmei", True, False, True),
    ("発注コード", "hatchu_code", True, False, False),
    ("単位", "tani", False, False, False),
    ("材質", "zaisitu", False, False, False),
    ("調質", "choshitu", False, False, False),
    ("厚", "atu", False, True, False),
    ("幅", "haba", False, True, False),
    ("丈", "take", False, True, False),
    ("用途コード", "yoto_code", False, False, False),
    ("納入先", "nounyusaki", False, False, True),
    ("発注数", "hatchu_suu", True, True, False),
)
REQUIRED_KEYS = tuple(key for _, key, required, _, _ in ORDER_FIELDS if required)
NUMERIC_KEYS = tuple(key for _, key, _, numeric, _ in ORDER_FIELDS if numeric)

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
    # そのモードでその行に何ができるか。**判断はサーバが持つ**
    # (確認=資材だけ / 取消=現場だけ、どちらも未確認のときだけ)
    can_confirm: bool = False
    can_cancel: bool = False
    # その発注がどのLotのものか。**押すとロット検索でそのLotが開く。**
    # 空なら出さない(Lotが入っていない発注は辿れない)
    lot_no: str = ""

    @property
    def lot_url(self) -> str:
        """そのLotを開いた状態のロット検索。

        番号を目で読んで打ち直させない ── 7桁の打ち間違いは、そのまま
        別のロットを開いてしまい、しかも開けてしまうので気づけない。
        """
        from urllib.parse import quote
        return f"/lot?lot={quote(self.lot_no)}" if self.lot_no else ""

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

    `mode` で**できること**が変わる(VBA のフォーム分けをそのまま
    引き継ぐ)。現場は出して取り消す、資材は受けて確認する。
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
        # **取り消しは現場だけ。** 出した側が引っ込める操作で、資材は
        # 受けて確認する側なので消す立場ではない。VBAでも取り消しは
        # 現場の送信確認ダイアログ(`frmSendConfirm.btnDelete_Click`)に
        # あり、資材側の `frmWarehouseOrder` にあったのは確認だけだった。
        # 移植のときに確認とまとめて資材専用にしてしまい、**逆**に
        # なっていた(現場の声:「送った発注を取り消せない」)。
        # 現場であっても、倉庫が確認したあとは取り消せない
        can_cancel=not is_material and status == svc.STATUS_PENDING,
        # **どのモードでも出す。** 現場にとっても「この発注は何のLotか」は
        # 確かめたい事実で、資材だけのものではない
        lot_no=str(item.get("LotNo") or "").strip(),
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
              for _, key, _, _, _ in ORDER_FIELDS}
    ex = is_ex_order(body)

    for label, key, required, _, _ in ORDER_FIELDS:
        if ex and key in EX_BLANK_KEYS:
            continue                    # EXは空が正しい。必須にしない
        if required and not values[key]:
            return values, (key, f"{label}を入れてください")

    for label, key, required, numeric, _ in ORDER_FIELDS:
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
        # 1行を開いたときの中身。**見せ方だけ**で、事実は `rows` にある
        "detail_fields": [{"label": label, "key": key, "big": big}
                          for label, key, big in ORDER_DETAIL],
        "copy_column": ORDER_COPY_COLUMN,
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
        "lot_no": row.lot_no,
        "lot_url": row.lot_url,
    }


def action_dict(result: svc.ActionResult) -> dict[str, Any]:
    return {"ok": result.ok, "message": result.message}


def order_dict(result: svc.OrderResult) -> dict[str, Any]:
    return {"ok": result.ok, "message": result.message, "mgr_no": result.mgr_no}
