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
    ("送信端末", False, False),
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
# 幅の合計は 80%。残りは操作の列 ── ボタンは畳めない(切れたら押せない)
# ので、**先に確保してから**残りを読み物に配る。
ORDER_VIEW: tuple[ViewColumn, ...] = (
    ViewColumn("管理番号 / 登録日時", "管理番号", ("登録日時",),
               numeric=True, width="8%"),
    ViewColumn("LotNo / 品名", "LotNo", ("品名",), width="12%"),
    ViewColumn("発注コード", "発注コード", width="8%"),
    ViewColumn("用途", "用途コード", width="5%"),
    ViewColumn("発注数 / 単位", "発注数", ("単位",), numeric=True, width="7%"),
    ViewColumn("材質 / 調質", "材質", ("調質",), width="7%"),
    ViewColumn("厚 / 幅×丈", "厚", ("幅", "丈"), sep=" × ",
               numeric=True, width="8%"),
    ViewColumn("納入先", "納入先", width="8%"),
    ViewColumn("状態", "状態", kind="status", width="7%"),
    # どの現場が送ったか。**現場が複数台**なので、取り消せない行が
    # 「なぜ押せないのか」を一覧で読めるようにする
    ViewColumn("送った端末", "送信端末", width="10%"),
)
# 1行を開いたときに出す項目の並び。**VBA `frmWarehouseOrder` の
# `lstOrders_Click` が埋めていた13個のラベル**と同じ並びにしてある
# (登録日時→LotNo→品名→発注コード→発注数→単位→材質→調質→厚→幅→丈→
#  用途コード→納入先)。
#
# VBAはこれを一覧と同じフォームの中に並べていた。こちらは一覧が14列の
# 表なので、同じ場所に置くと表が読めなくなる ── 開いて出す形にした。
#
# 大きく出すのはVBAが目立たせていた2つ。
#   発注コード … 青字・下線・枠付きで「クリックでコピー」
#   発注数     … Font.Size 13・色を濃く・中央揃え
#   (見出し, 列名, 大きく出すか)
ORDER_DETAIL: tuple[tuple[str, str, bool], ...] = (
    ("登録日時", "登録日時", False),
    ("LotNo", "LotNo", False),
    ("品名", "品名", False),
    ("発注コード", "発注コード", True),
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
    ("送った端末", "送信端末", False),
)

# 押すと控えられる列(VBA `evtHatchu_Click`)。
#
# **自動では控えない。** VBAは発注コードのラベルを青字・下線・枠付きに
# して `ControlTipText = "クリックでコピー"` を付け、押されたときだけ
# クリップボードへ入れて「コピーしました」と出していた。開いただけで
# 控えると、利用者が別に写していたものを黙って消すことになる
ORDER_COPY_COLUMN = "発注コード"

# 押す前に見せる項目(VBA `btnConfirm_Click` / `btnDelete_Click`)。
#
# **何を動かすのかを見せてから訊く。** どちらも取り返しのつかない操作で、
# 一覧は14列を詰めて並べるので、押す行を1行ずれて選んでも気づけない。
# VBAはどちらの操作でも同じ3項目を出して Yes/No を訊いていた
ORDER_ASK_FIELDS: tuple[str, ...] = ("品名", "発注コード", "登録日時")

# 操作ごとの訊き方。**言葉もサーバが持つ**(設計書 §4)
ORDER_ASK: dict[str, dict[str, str]] = {
    "confirm": {
        "title": "確認済みにしますか？",
        "ok": "確認済みにする",
        # 資材が受け取ったことの記録。現場はこれ以降取り消せなくなる
        "why": "確認済みにすると、現場はこの発注を取り消せなくなります。",
    },
    "cancel": {
        "title": "この発注を取り消しますか？",
        "ok": "取り消す",
        "why": "取り消した発注は元に戻せません。",
    },
}

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
    # ボタンが1つも無いときに添える理由。**言葉はサーバが決める**
    why: str = ""
    # コメント(`order_comments`)。件数・この端末で未読の数・いちばん新しい1件
    comments: int = 0
    unread: int = 0
    # 自分が書いたのに、相手がまだ見ていないもの
    unseen_mine: int = 0
    # 自分が書いて、相手がもう見たもの(「既読」)
    seen_mine: int = 0
    # 相手の呼び名(現場なら「倉庫」、倉庫なら「現場」)。印の言葉に使う
    partner: str = ""
    latest_comment: str = ""
    # まだ書けるか(未確認のあいだだけ)。書けないなら理由
    comment_why: str = ""
    # **この端末で登録したが、まだ共有(倉庫)へ届いていない。** 共有フォルダが
    # 見えない間に送った発注は、画面上は送れたように見えて倉庫には無い
    unsent: bool = False

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
    # 相手が書いた、この端末で未読のコメントの数
    unread_comments: int = 0

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

    terminal = svc.this_terminal()
    from .. import order_comments
    unsent = _unsent_order_ids(conn)
    # 側(現場/倉庫)も渡す。1台で両方のモードを使う端末でも、自分の側で書いたものだけが
    # 「自分のコメント」になる(`order_comments._mine`)
    notes = order_comments.summaries(
        conn, terminal=terminal,
        side=order_comments.SIDE_MATERIAL if is_material else order_comments.SIDE_FIELD)
    rows = [_row(item, is_material=is_material, terminal=terminal, notes=notes,
                 unsent=unsent)
            for item in raw]
    pending = sum(1 for r in rows if r.status == svc.STATUS_PENDING)
    return WarehouseViewModel(
        rows=rows, keyword=keyword, date_filter=date_filter,
        include_cancelled=include_cancelled,
        pending=pending,
        unread_comments=sum(r.unread for r in rows),
        message=("" if rows else "該当する発注はありません。"),
    )


def _unsent_order_ids(conn: sqlite3.Connection) -> set[int]:
    """この端末で登録して、まだ共有へ送れていない発注(管理番号)。"""
    from .. import outbox_sync, sync_writeback
    spec = next((s for s in sync_writeback.WRITEBACK_SPECS
                 if s.sqlite_table == svc.TABLE), None)
    if spec is None:
        return set()
    try:
        return {int(r[spec.key_column]) for r in outbox_sync.pending_rows(conn, spec)}
    except sqlite3.Error:
        return set()


def _row(item: dict, *, is_material: bool, terminal: str = "",
         notes: Optional[dict] = None, unsent: Optional[set] = None) -> OrderRow:
    from .. import order_comments
    status = item.get("状態", svc.STATUS_PENDING)
    sender = str(item.get("送信端末") or "").strip()
    mine = svc.can_cancel_from(sender, terminal)
    note = (notes or {}).get(order_comments.order_key(item)) \
        if order_comments.order_key(item) else None
    return OrderRow(
        comments=note.count if note else 0,
        unread=note.unread if note else 0,
        unseen_mine=note.unseen_mine if note else 0,
        seen_mine=note.seen_mine if note else 0,
        partner=order_comments.SIDE_FIELD if is_material else order_comments.SIDE_MATERIAL,
        latest_comment=note.latest if note else "",
        comment_why=order_comments.closed_why(item),
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
        # **送った端末だけ。** 現場は複数台で使うので、ほかの現場の発注を
        # 取り消せると別のラインの発注を誤って引っ込めてしまう
        can_cancel=not is_material and status == svc.STATUS_PENDING and mine,
        why=_why(status, is_material=is_material, sender=sender, mine=mine,
                 cancelled_at=str(item.get("取り消し日時") or "").strip()),
        # **どのモードでも出す。** 現場にとっても「この発注は何のLotか」は
        # 確かめたい事実で、資材だけのものではない
        lot_no=str(item.get("LotNo") or "").strip(),
        unsent=int(item.get("管理番号") or 0) in (unsent or set()),
    )


def _why(status: str, *, is_material: bool, sender: str, mine: bool,
         cancelled_at: str = "") -> str:
    """ボタンが出ない行に添える理由。**押せない理由を空欄で示さない。**"""
    if status == svc.STATUS_CONFIRMED:
        return "確認済みです" if is_material else "倉庫が確認済みのため取り消せません"
    if status == svc.STATUS_CANCELLED:
        # **いつ取り消されたか**も言う。倉庫は印刷したあとに取り消されたのかを
        # これで判断する(VBA で足した「【取消済】…(取消日時: …)」)
        when = f"(取消日時: {cancelled_at})" if cancelled_at else ""
        return ("現場で取り消されています" + when) if is_material else ("取り消し済みです" + when)
    if not is_material and not mine:
        return f"{sender} が送った発注です(取り消しは送った端末から)"
    return "この画面からできる操作はありません"


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
        # 押す前に見せる項目と言葉。**画面では決めない**
        "ask_fields": list(ORDER_ASK_FIELDS),
        "ask": ORDER_ASK,
        "rows": [row_dict(r) for r in view.rows],
        "keyword": view.keyword,
        "date_filter": view.date_filter,
        "include_cancelled": view.include_cancelled,
        "message": view.message,
        "pending": view.pending,
        "unread_comments": view.unread_comments,
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
        "why": row.why,
        "unsent": row.unsent,
        "comments": row.comments,
        "unread": row.unread,
        "unseen_mine": row.unseen_mine,
        "seen_mine": row.seen_mine,
        "partner": row.partner,
        "latest_comment": row.latest_comment,
        "comment_why": row.comment_why,
        "lot_no": row.lot_no,
        "lot_url": row.lot_url,
    }


def action_dict(result: svc.ActionResult) -> dict[str, Any]:
    return {"ok": result.ok, "message": result.message}


def order_dict(result: svc.OrderResult) -> dict[str, Any]:
    return {"ok": result.ok, "message": result.message, "mgr_no": result.mgr_no}


# 二重送信の確認で見せる送信済みの件数(VBA は最新5件まで)
ALREADY_SENT_SHOWN = 5


def already_sent_ask(lot_no: str, sent: list[dict]) -> dict[str, Any]:
    """同じロットを送ってあるときに訊く中身(VBA `ConfirmNotAlreadySent`)。

    **何を送ってあるかを見せてから訊く。** 件数だけでは、前に送ったのが
    同じ内容なのか(二重発注)、追加で要る分なのかを判断できない。
    """
    fields = []
    for item in sent[:ALREADY_SENT_SHOWN]:
        value = f"{item['hinmei']}　発注数:{item['qty'] or '---'}"
        if item["confirmed"]:
            value += "　[倉庫確認済]"
        fields.append({"label": item["registered"], "value": value})
    if len(sent) > ALREADY_SENT_SHOWN:
        fields.append({"label": "ほか", "value": f"{len(sent) - ALREADY_SENT_SHOWN}件"})
    return {
        "title": "二重送信の確認",
        "lead": (f"このロット({lot_no})は、すでに倉庫へ送信されています。"
                 "もう一度送信すると、二重発注になります。"),
        "why": (f"送信済み(取り消していないもの){len(sent)}件。新しい順に"
                f"{min(len(sent), ALREADY_SENT_SHOWN)}件まで出しています。それでも送信しますか？"),
        "ok": "それでも送信する",
        # 何件かを並べるので、1件を大きく見せる並べ方にしない
        "compact": True,
        "fields": fields,
    }
