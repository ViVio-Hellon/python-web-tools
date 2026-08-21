"""発注のやりとり (旧 `frmSendConfirm` + `frmWarehouseOrder`)

**この画面はモードで中身が変わります。**

    現場モード … 発注を出す。自分が送ったものを見る(状態は動かせない)
    資材モード … 受け取った発注を確認する。取り消しもできる

VBA版は現場用と資材用が別のフォームで、確認ボタンは資材のフォームに
しかありませんでした。現場のアプリから「確認済みにする」ができると、
資材が実際に受け取ったかに関わらず現場の都合で確認済みにでき、
**確認という工程そのものが意味を失います**。

Web版は同じ保証を2段で作ります。

1. `mode:material` の権限が無い端末には、確認・取消の操作を断ります(404)。
   守るのは「誰か」です
2. 権限があっても、いま現場モードで見ていれば断ります(403)。
   こちらは誤操作の防止で、1 とは目的が違うので両方置いています

**両方とも、要求のたびに確かめます。** 以前は 1 を「起動時の権限で
エンドポイントを登録するかどうか」で分けていましたが、これだと
マスタ管理でアクセス権限に行を足しても、**サーバプロセスを終了して
起動し直すまで反映されません**でした(ページの読み込み直しでは
Pythonのプロセスは再起動しないため)。権限を足した理由が
「いま資材モードで使いたいから」であることを踏まえ、`master.py` の
マスタ管理と同じ形(常に登録し、要求のたびに `master_admin.can_edit`
で見る)にそろえています。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from packaging_tool import (access_control, data_sync, modes,
                            warehouse_service as svc, work_context)
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import warehouse as presenter

from .. import current_grant, current_mode, get_db
from ..shell import shell_context

log = get_logger("app.routes.warehouse")

# どのモードにもある部分(一覧・検索・発注)
bp = Blueprint("warehouse", __name__)
# 資材モードの権限がある端末でだけ使える部分(確認・取消)。
# **常に登録する**(起動時の権限では決めない)。誰が使えるかは
# `_require_material_mode` が要求のたびに確かめる
material_only = Blueprint("material_only", __name__)


@material_only.before_request
def _require_material_mode():
    """資材モードの権限があり、かついま資材モードで見ているかを断る。

    2段のうち、こちらが両方を受け持つ(以前は1段目を起動時の登録で
    分けていた)。

    1. 権限そのものが無い ── **そもそもこの端末では使えない操作**。
       起動時の権限では決めず、要求のたびに `access_control.resolve`
       で引き直す。取り込み元は共有フォルダの1ファイルで、書く場所は
       ここだけとは限らないため、断る前に一度だけ読み直す
       (`access_control.resync` と同じ考え方)
    2. 権限はあるが、いま現場モードで見ている ── 資材課の人が現場
       モードで作業している最中に、手が滑って確認済みにできて
       しまうのを防ぐ
    """
    grant = current_grant()
    if not grant.allows_mode(modes.MATERIAL):
        # 手元がまだ古い可能性がある。断る前に取り込み元から
        # アクセス権限だけ読み直し、それでも無ければ本当に無い
        if access_control.resync(get_db()):
            from flask import g
            g.pop("grant", None)
            grant = current_grant()
        if not grant.allows_mode(modes.MATERIAL):
            log.info("資材モードの権限が無いため断りました: %s", request.path)
            return jsonify({"error": {
                "code": "not_found",
                "message": "その操作はこの端末では使えません。"
                          "設定画面でアクセス権限の登録状況を確認してください。"
                }}), 404
    if current_mode() != modes.MATERIAL:
        log.info("資材モードではないため断りました: %s", request.path)
        return jsonify({"error": {
            "code": "wrong_mode",
            "message": "この操作は資材モードでのみ行えます。"
                       "右上でモードを切り替えてください。"}}), 403
    return None


def _mode() -> str:
    return current_mode()


@bp.get("/warehouse")
def page():
    mode = _mode()
    view = presenter.build(get_db(), mode=mode)
    # 資材選択の「倉庫送信」が組み立てた下書き。**受け取ったら手放す**
    # ので、開き直しても同じ発注が二重に出てくることはない
    drafts = work_context.get_context().take_pending_orders()
    if drafts:
        log.info("倉庫送信の下書きを %s 行 受け取りました", len(drafts))
    return render_template(
        "warehouse.html",
        state=presenter.to_dict(view),
        columns=presenter.ORDER_VIEW,
        action_width=presenter.ORDER_ACTION_WIDTH,
        fields=presenter.ORDER_FIELDS,
        date_filters=presenter.DATE_FILTERS,
        drafts=drafts,
        is_material=mode == modes.MATERIAL,
        **shell_context("warehouse",
                        badges={"warehouse": (str(view.pending), "todo")}
                        if view.pending else None),
    )


@bp.get("/api/warehouse/orders")
def orders():
    """`?q=&period=&cancelled=`。"""
    view = presenter.build(
        get_db(), mode=_mode(),
        keyword=request.args.get("q", ""),
        date_filter=request.args.get("period", ""),
        include_cancelled=request.args.get("cancelled") == "1")
    return jsonify(presenter.to_dict(view))


@bp.post("/api/warehouse/send")
def send():
    """発注を出す(VBA `SendWarehouseRow`)。"""
    body = request.get_json(silent=True) or {}
    values, problem = presenter.validate(body)
    if problem:
        field, message = problem
        return jsonify({"error": {"code": "invalid", "message": message,
                                  "field": field}}), 400

    result = svc.create_order(get_db(), **values)
    if not result.ok:
        return jsonify({**presenter.order_dict(result),
                        "error": {"code": "rejected",
                                  "message": result.message}}), 422
    log.info("発注を登録しました: 管理番号=%s", result.mgr_no)
    # 倉庫へ届けるのが仕事なので、押した直後に送りにいく。
    # **画面には何も出さない** ── 手元の登録はもう終わっており、
    # 届かなくても次の「取り込み元へ反映」でまとめて送られる
    data_sync.write_back_in_background()
    return jsonify(presenter.order_dict(result))


@material_only.post("/api/warehouse/cancel")
def cancel():
    """取り消し。

    **確認と同じく、資材モードの権限がある端末にしか登録されません。**
    VBA `frmWarehouseOrder` も確認・取り消しの両方を資材側だけに
    置いていました。現場から状態を動かせると、資材が実際に受け取ったかに
    関わらず現場の都合で消せてしまいます。

    資材が確認したあとは資材からも取り消せません ── 受け取ったことを
    確認したものを消すと、現物と帳簿が合わなくなります。
    """
    return _action(svc.cancel_order, "取消")


@material_only.post("/api/warehouse/confirm")
def confirm():
    """確認済みにする(VBA `frmWarehouseOrder.btnConfirm_Click`)。

    **資材モードの権限がある端末にしか登録されません。** 権限が無ければ
    404、権限があっても現場モードで見ていれば 403。
    """
    return _action(svc.confirm_order, "確認")


def _action(func, label: str):
    body = request.get_json(silent=True) or {}
    try:
        mgr_no = int(body.get("mgr_no"))
    except (TypeError, ValueError):
        return jsonify({"error": {"code": "bad_mgr_no",
                                  "message": "対象が指定されていません"}}), 400

    result = func(get_db(), mgr_no)
    if not result.ok:
        # 「既に確認済み/取消済み」は、他の端末が先に動かした結果でもある。
        # 画面を取り直せば正しい状態が見えるので 409 で返す
        log.info("%s できませんでした: 管理番号=%s %s", label, mgr_no, result.message)
        return jsonify({**presenter.action_dict(result),
                        "error": {"code": "conflict",
                                  "message": result.message}}), 409
    log.info("%s しました: 管理番号=%s", label, mgr_no)
    return jsonify(presenter.action_dict(result))
