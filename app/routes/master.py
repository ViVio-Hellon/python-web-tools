"""マスタ管理 (`/api/master/*`)

設定画面の「マスタ管理」の面が使う。画面そのものは `settings.html` の
中にあり、ここは中身の出し入れだけを持つ。

【書き先は取り込み元】
直すのは共有フォルダの sqlite3(梱包資材マスタ)で、手元のDBではない。
手元は総入れ替えで取り込まれるので、直しても次の取り込みで消える。
理由と手順は `packaging_tool/master_admin.py` に書いてある。

【なぜ権限で登録を分けないのか】
資材モードだけのエンドポイント(`warehouse.material_only`)は、権限を
持たない端末では**登録しない**(URL自体が無い)。ここは同じにできない。

    直せるかどうかは「アクセス権限マスタの中身」で決まり、その中身は
    **動いている最中に変わる**(この画面で最初の1行を入れられる)。
    起動時に決めて登録を分けると、入れた直後から食い違う。

なので登録は常にして、要求のたびに `master_admin.can_edit` で見る。
見るだけ(`browse`)はどのモードでも通す ── 中身を確かめられることと、
書き換えられることは別の話。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from packaging_tool import master_admin
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import master as master_presenter

from .. import get_db

log = get_logger("app.routes.master")

bp = Blueprint("master", __name__)

# 断りの種別を HTTP に写す(設計.md §1)。
#   400 … 入力の形が違う。**サーバの状態は動いていない**
#   403 … 許されていない
#   409 … 先を越された(見ていた行がもう無い)
#   422 … 業務としての断り
STATUS = {
    master_admin.REFUSE_BAD_VALUE: 400,
    master_admin.REFUSE_NOT_ALLOWED: 403,
    master_admin.REFUSE_NO_ROW: 409,
    master_admin.REFUSE_NOT_EDITABLE: 422,
    master_admin.REFUSE_NO_SOURCE: 422,
    master_admin.REFUSE_WRITE_FAILED: 422,
}


@bp.get("/api/master/browse")
def browse():
    """表の一覧と、選んだ表の中身。"""
    view = master_presenter.browse(
        get_db(),
        table=request.args.get("table", ""),
        query=request.args.get("q", ""))
    return jsonify(master_presenter.to_dict(view))


@bp.post("/api/master/row/save")
def save_row():
    """1行を書き換える。`{"table":…, "key":…, "values":{…}}`"""
    body = request.get_json(silent=True) or {}
    return _write(master_admin.save_row(
        get_db(), _table(body), body.get("key"), _values(body)), body)


@bp.post("/api/master/row/add")
def add_row():
    """1行足す。`{"table":…, "values":{…}}`"""
    body = request.get_json(silent=True) or {}
    return _write(master_admin.add_row(
        get_db(), _table(body), _values(body)), body)


@bp.post("/api/master/row/delete")
def delete_row():
    """1行消す。`{"table":…, "key":…}`"""
    body = request.get_json(silent=True) or {}
    return _write(master_admin.delete_row(
        get_db(), _table(body), body.get("key")), body)


def _table(body: dict) -> str:
    return str(body.get("table", ""))


def _values(body: dict) -> dict:
    values = body.get("values")
    return values if isinstance(values, dict) else {}


def _write(result: master_admin.Result, body: dict):
    """書いたあとは**まるごとの状態**を返す(設計.md §1)。

    断ったときも同じ形で返す ── 画面は「何が起きたか」と「いまどう
    なっているか」を1回で受け取れる。断りの理由は `reason` が運び、
    画面は文言から推し量らない。
    """
    view = master_presenter.browse(
        get_db(), table=_table(body), query=str(body.get("q", "")),
        message=result.message if result.ok else "")
    payload = master_presenter.to_dict(view)
    if result.ok:
        return jsonify(payload)
    payload["error"] = {"code": result.reason, "message": result.message}
    return jsonify(payload), STATUS.get(result.reason, 422)
