"""倉庫連携(現場↔倉庫)の業務ロジック

VBA `frmSendConfirm`(倉庫に発注データを送る確認ダイアログ)と
`frmWarehouseOrder`(倉庫側の一覧・確認画面)、および実際のINSERT処理を
行っていた `SendWarehouseRow` の移植。対象テーブルは `資材パレット注文管理`。

対応関係:
    VBA                                  -> Python
    ------------------------------------------------------------
    SendWarehouseRow                       -> create_order
    frmWarehouseOrder.LoadOrders/RefreshList -> list_orders
    frmWarehouseOrder.btnConfirm_Click        -> confirm_order
    frmSendConfirm.btnDelete_Click             -> cancel_order

【移植で変更した点】
    - VBA版は「取り消し済/確認済み」を文字列リテラル'1'として書き込み、
      「対象カラムが存在するか(AddConfirmColumns未実行でないか)」を
      毎回チェックしていた。SQLite版はスキーマに最初から4カラムとも
      定義済みなので、その存在チェックは不要になる。
    - `frmWarehouseOrder.btnConfirm_Click`はUPDATEの影響行数を確認せず
      「確認済みにしました」と表示してしまう(他端末が同時に取り消した
      場合に矛盾したメッセージになりうる)という弱点があったが、
      Python版は`cancel_order`と同様に影響行数を確認し、0件なら
      「他の端末で状態が変更されています」と正しく伝える(意図的な改善)。
    - 管理番号の採番はSQLiteのAUTOINCREMENTに任せる(VBA版は
      オートナンバー未設定の場合にMAX+1を手計算する分岐を持っていたが、
      SQLite版のテーブルは常にAUTOINCREMENTなので不要)。
    - 一覧のキーワード/期間フィルタはVBA版はメモリ上の全件配列に対して
      行っていたが、Python版はSQLiteのWHERE句で行う
      (登録日時をISO8601で保存しているため `date()` 関数がそのまま使える)。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import db, outbox_sync
from .logging_utils import get_logger

log = get_logger("warehouse_service")

TABLE = "資材パレット注文管理"

STATUS_CANCELLED = "取消済"
STATUS_CONFIRMED = "確認済み"
STATUS_PENDING = "未確認"


@dataclass
class OrderResult:
    ok: bool
    message: str
    mgr_no: Optional[int] = None


@dataclass
class ActionResult:
    ok: bool
    message: str


def create_order(
    conn: sqlite3.Connection,
    *,
    lot_no: str,
    hinmei: str,
    hatchu_code: str,
    tani: str,
    zaisitu: str = "",
    choshitu: str = "",
    atu,
    haba,
    take,
    nounyusaki: str = "",
    yoto_code: str = "",
    hatchu_suu,
    is_ex_order: bool = False,
) -> OrderResult:
    """VBA `SendWarehouseRow`(+ `frmSendConfirm.btnSend_Click`の数量検証)の移植。

    【EX受注】`is_ex_order` の行は、発注コード・単位・発注数を**空のまま**
    登録する。EXの実データは別の職場から別途届き、そちらが優先されるため、
    こちらからは「EXである」と分かる合図(品名 `EX`)だけを送る ──
    中途半端な値を入れると、届いた側の突き合わせで混乱する。
    どのロットのEXかは LotNo と寸法で分かるので、そこは通常どおり入れる。
    """
    lot_no = db.sanitize_for_db(lot_no)
    hinmei = db.sanitize_for_db(hinmei)
    hatchu_code = db.sanitize_for_db(hatchu_code)
    if not lot_no or not hinmei:
        return OrderResult(ok=False, message="LotNo・品名は必須です。")
    if not hatchu_code and not is_ex_order:
        return OrderResult(ok=False, message="LotNo・品名・発注コードは必須です。")

    try:
        # 呼び出し側はVBA同様の書式済み文字列("1122.0"等)を渡してくることがある。
        # Access版は列型に合わせて暗黙変換していたので、floatを経由して受ける
        atu_v, haba_v, take_v = float(atu), int(float(haba)), int(float(take))
    except (TypeError, ValueError, OverflowError):
        return OrderResult(ok=False, message="厚・幅・丈は数値で入力してください。")

    if is_ex_order:
        qty = None                       # 列は NULL 可。**0 で埋めない**
    else:
        try:
            qty = int(hatchu_suu)
        except (TypeError, ValueError):
            return OrderResult(ok=False, message="発注数を入力してください。")
        if qty <= 0:
            return OrderResult(ok=False, message="発注数は1以上を入力してください。")

    now = db.now_db_string()
    result = db.insert_record(
        conn, TABLE,
        {
            "登録日時": now,
            "LotNo": lot_no,
            "品名": hinmei,
            "発注コード": hatchu_code,
            "単位": db.sanitize_for_db(tani),
            "材質": db.sanitize_for_db(zaisitu),
            "調質": db.sanitize_for_db(choshitu),
            "厚": atu_v,
            "幅": haba_v,
            "丈": take_v,
            "用途コード": db.sanitize_for_db(yoto_code),
            "納入先": db.sanitize_for_db(nounyusaki),
            "発注数": qty,
        },
        caller_name="create_order",
    )
    if not result.ok:
        return OrderResult(ok=False, message=f"発注登録に失敗しました。({result.error})")

    return OrderResult(ok=True, message="倉庫へ発注を送信しました。", mgr_no=result.lastrowid)


def _status_of(row: sqlite3.Row) -> str:
    if (row["取り消し済"] or "") == "1":
        return STATUS_CANCELLED
    if (row["確認済み"] or "") == "1":
        return STATUS_CONFIRMED
    return STATUS_PENDING


def list_orders(
    conn: sqlite3.Connection,
    *,
    keyword: str = "",
    date_filter: Optional[str] = None,  # None / "today" / "week" / "month"
    include_cancelled: bool = False,
) -> list[dict]:
    """VBA `frmWarehouseOrder.LoadOrders`+`RefreshList` の移植。

    `include_cancelled=False` で `frmWarehouseOrder` 相当(取消済は非表示)、
    `True` で `frmSendConfirm` の履歴パネル相当(取消済も★付きで表示)。
    """
    where = ["1=1"]
    params: list = []

    if not include_cancelled:
        where.append("(取り消し済 IS NULL OR 取り消し済 <> '1')")

    if keyword:
        where.append(
            "(LotNo LIKE ? OR 品名 LIKE ? OR 発注コード LIKE ? OR 用途コード LIKE ? OR 納入先 LIKE ?)"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like, like])

    if date_filter == "today":
        where.append("date(登録日時) = date('now', 'localtime')")
    elif date_filter == "week":
        where.append("date(登録日時) >= date('now', 'localtime', '-6 days')")
    elif date_filter == "month":
        where.append("date(登録日時) >= date('now', 'localtime', '-29 days')")

    sql = f"SELECT * FROM {TABLE} WHERE {' AND '.join(where)} ORDER BY 登録日時 DESC"
    rows = db.fetch_all(conn, sql, params, caller_name="list_orders") or []

    result = []
    for row in rows:
        d = dict(row)
        d["状態"] = _status_of(row)
        result.append(d)
    return result


def confirm_order(conn: sqlite3.Connection, mgr_no: int) -> ActionResult:
    """VBA `frmWarehouseOrder.btnConfirm_Click` の移植(確認済みにする)。

    元VBAはUPDATEのWHERE句で「取消済でないこと」しかチェックしておらず、
    「既に確認済みか」はUI側のキャッシュ値でのみ判定していた(他端末が
    確認した直後に自分も確認ボタンを押すと、SQL上は無条件で
    再UPDATEが成功してしまい、確認日時が意図せず上書きされる弱点が
    あった)。Python版はWHERE句にも「未確認であること」を追加し、
    二重確認を確実に防ぐ(意図的な改善)。
    """
    result = db.update_record(
        conn, TABLE, "管理番号", mgr_no,
        {"確認済み": "1", "確認日時": db.now_db_string()},
        where_clause="管理番号 = ? AND (取り消し済 IS NULL OR 取り消し済 <> '1') AND (確認済み IS NULL OR 確認済み <> '1')",
        where_params=(mgr_no,),
    )
    if result.reason == "not_found":
        return ActionResult(ok=False, message=_refusal(conn, mgr_no, "確認済みにできません"))
    if not result.ok:
        return ActionResult(ok=False, message=f"更新に失敗しました。({result.error})")
    _mark_unsent(conn, mgr_no)
    return ActionResult(ok=True, message="確認済みにしました。")


def cancel_order(conn: sqlite3.Connection, mgr_no: int) -> ActionResult:
    """VBA `frmSendConfirm.btnDelete_Click` の移植(取り消し)。

    倉庫が確認済みの注文は現場から取り消せない(元VBA仕様を踏襲)。
    """
    result = db.update_record(
        conn, TABLE, "管理番号", mgr_no,
        {"取り消し済": "1", "取り消し日時": db.now_db_string()},
        where_clause="管理番号 = ? AND (確認済み IS NULL OR 確認済み <> '1') AND (取り消し済 IS NULL OR 取り消し済 <> '1')",
        where_params=(mgr_no,),
    )
    if result.reason == "not_found":
        return ActionResult(ok=False, message=_refusal(conn, mgr_no, "取り消せません"))
    if not result.ok:
        return ActionResult(ok=False, message=f"更新に失敗しました。({result.error})")
    _mark_unsent(conn, mgr_no)
    return ActionResult(ok=True, message="発注を取り消しました。")


def _refusal(conn: sqlite3.Connection, mgr_no: int, cannot: str) -> str:
    """断った理由を、**いまの状態から**言う。

    「見つからないか、確認済みか、取り消し済み」とまとめて言うと、押した人は
    どれなのか分からない。ほかの端末が先に動かしたときは特に、何が起きた
    かを伝えないと、倉庫が取り消し済みの発注を用意しかねない。
    """
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE 管理番号 = ?", (mgr_no,)).fetchone()
    if row is None:
        return "この発注は一覧にありません(共有で消えたか、入れ替わりました)。画面を更新してください。"
    status = _status_of(row)
    if status == STATUS_CANCELLED:
        return f"この発注は取り消し済みのため、{cannot}。"
    if status == STATUS_CONFIRMED:
        if cannot.startswith("確認"):
            return "この発注はもう確認済みです(ほかの端末で確認されています)。"
        return f"この発注は倉庫が確認済みのため、{cannot}。"
    return f"{cannot}でした。画面を更新してください。"


_IDENTITY = ("取込元管理番号", "登録日時", "LotNo", "品名")


def identity(conn: sqlite3.Connection, mgr_no: int) -> Optional[dict]:
    """取り込み直しても変わらない、その発注の見分け方。

    手元の管理番号は取り込み(総入れ替え)のたびに振り直されるので、
    取り込みをまたいで同じ行を指すには使えない。
    """
    row = conn.execute(
        f"SELECT {', '.join(_IDENTITY)} FROM {TABLE} WHERE 管理番号 = ?",
        (mgr_no,)).fetchone()
    return dict(zip(_IDENTITY, row)) if row else None


def find_again(conn: sqlite3.Connection, mgr_no: int,
               before: Optional[dict]) -> int:
    """取り込み直したあとの、同じ発注の管理番号。見つからなければ元の番号。

    元の番号で返せば、呼び手の更新が「見つかりません」と正しく断る。
    """
    if before is None or identity(conn, mgr_no) == before:
        return mgr_no
    if before["取込元管理番号"] not in (None, ""):
        row = conn.execute(
            f"SELECT 管理番号 FROM {TABLE} WHERE 取込元管理番号 = ?",
            (before["取込元管理番号"],)).fetchone()
        if row:
            return int(row[0])
    # 手元で作って送った行は、取り込むまで共有の行番号を知らない
    row = conn.execute(
        f"SELECT 管理番号 FROM {TABLE}"
        " WHERE 登録日時 = ? AND LotNo = ? AND 品名 = ? ORDER BY 管理番号 LIMIT 1",
        (before["登録日時"], before["LotNo"], before["品名"])).fetchone()
    return int(row[0]) if row else mgr_no


def _mark_unsent(conn: sqlite3.Connection, mgr_no: int) -> None:
    """付けた印を「まだ共有へ送っていない」と覚える。

    **印は手元のUPDATEなので、放っておくと共有へ届きません。**
    届かないと、相手側(現場/資材)から状況が見えず、次の取り込みの
    総入れ替えで消えます。ここで目印を立てておけば、書き戻しが拾って
    共有の同じ行へ書き、送るまでは取り込みが見送られます。
    """
    from . import data_sync
    for spec in data_sync.WRITEBACK_SPECS:
        if spec.sqlite_table == TABLE:
            outbox_sync.mark_pending(conn, spec, mgr_no)
            conn.commit()
            return
