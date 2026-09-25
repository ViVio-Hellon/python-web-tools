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
    master_admin.REFUSE_NOT_CREATABLE: 422,
    # もうある = 誰かに先を越された。`REFUSE_NO_ROW` と同じ種類の話
    master_admin.REFUSE_ALREADY: 409,
}


@bp.get("/api/master/browse")
def browse():
    """表の一覧と、選んだ表の中身。"""
    view = master_presenter.browse(
        get_db(),
        table=request.args.get("table", ""),
        query=request.args.get("q", ""),
        sort=request.args.get("sort", ""),
        sort_dir=request.args.get("sort_dir", "asc"))
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


@bp.post("/api/master/table/create")
def create_table():
    """取り込み元にその表を作る。`{"table":…}`

    作れるのは、このツールが後から足した表だけ
    (`master_admin.creatable_tables`)。
    """
    body = request.get_json(silent=True) or {}
    return _write(master_admin.create_table(get_db(), _table(body)), body)


@bp.post("/api/master/table/rebuild")
def rebuild_table():
    """列名が想定と違う表を、正しい列名で作り直す。`{"table":…}`

    取り込み元にその表**はある**が、`ログインID` / `PC名` / `権限` などの
    列名が1つも一致せず、打ち込める欄が無いときに使う(`can_rebuild` が
    その状態かどうかを見て、画面はボタンの表示・非表示だけ決める)。
    元の表は消さず `{table}_旧_日時` へ退避してから作り直す
    (`master_admin.rebuild_table`)。
    """
    body = request.get_json(silent=True) or {}
    return _write(master_admin.rebuild_table(get_db(), _table(body)), body)


@bp.post("/api/master/table/drop")
def drop_table():
    """取り込み元から表を消す。`{"table":…, "confirm":…}`

    消せるのはこのツールが使わない表だけ(`master_admin.drop_why`)。戻せないので
    管理者認証と、表の名前をそのまま打った確かめ(`confirm`)を通し、消す前に控えを取る。
    """
    body = request.get_json(silent=True) or {}
    result = master_admin.drop_table(get_db(), _table(body),
                                     confirm=str(body.get("confirm", "")))
    # 消えた表はもう選べない。一覧の先頭を開き直す
    return _write(result, {**body, "table": "" if result.ok else _table(body)})


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

    絞り込み(`q`)と同じ理由で、いま押していた並び替え(`sort`/`sort_dir`)
    も送り返してもらって保つ ── 行を直すたびに並びが既定へ戻ると、
    並べ替えて探した続きの行を、また並べ替え直すことになる。
    """
    view = master_presenter.browse(
        get_db(), table=_table(body), query=str(body.get("q", "")),
        sort=str(body.get("sort", "")),
        sort_dir=str(body.get("sort_dir", "asc")),
        message=result.message if result.ok else "")
    payload = master_presenter.to_dict(view)
    if result.ok:
        return jsonify(payload)
    payload["error"] = {"code": result.reason, "message": result.message}
    return jsonify(payload), STATUS.get(result.reason, 422)
