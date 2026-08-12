"""簡易在庫 (旧 `UFMAP` ユーザーフォーム)

保管位置の図から在庫を引き、受け入れと払い出しを登録する。

【この画面から書き込みが始まる】
ロット検索・選定ログ・データは読むだけでしたが、ここから在庫を動かします。
そのため、後の画面でも使う約束をここで決めています。

- 入力の検査は**サーバでやる**。画面側の検査は打ち間違いを早く知らせる
  ためのもので、通ってよいかを決めるのはこちら
- **同時更新は 409** で返す。`pallet_service` が
  `UPDATE ... WHERE 管理番号=? AND 更新日時=?` で弾いた場合で、
  「他の端末が先に動かした」ことが分かる唯一の場面
- 数量が合わない等の業務上の拒否は **422**。通信の失敗(5xx)や
  権限の話(4xx前半)と区別する
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from packaging_tool import data_sync, pallet_map, pallet_service
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import inventory as inv

from .. import get_db
from ..shell import shell_context

log = get_logger("app.routes.inventory")

bp = Blueprint("inventory", __name__)


@bp.get("/inventory")
def page():
    view = inv.initial(get_db())
    return render_template(
        "inventory.html",
        state=inv.to_dict(view),
        modes=inv.SEARCH_MODES,
        symbol_presets=inv.SYMBOL_PRESETS,
        industry_presets=inv.INDUSTRY_PRESETS,
        unit_presets=inv.UNIT_PRESETS,
        columns=inv.STOCK_COLUMNS,
        **shell_context("inventory"),
    )


# ------------------------------------------------------------------
# 参照
# ------------------------------------------------------------------
@bp.get("/api/inventory/search")
def search():
    """`?w=&l=&mode=`。幅・丈が数値でなければ 400。"""
    try:
        width = int(request.args.get("w", ""))
        length = int(request.args.get("l", ""))
    except ValueError:
        return jsonify(_error("bad_size", "幅と丈は数字で入れてください")), 400
    if width <= 0 or length <= 0:
        return jsonify(_error("bad_size", "幅と丈は1以上で入れてください")), 400

    mode = request.args.get("mode", "exact")
    return jsonify(inv.to_dict(inv.search(get_db(), width, length, mode)))


@bp.get("/api/inventory/position/<name>")
def at_position(name: str):
    return jsonify(inv.to_dict(inv.at_position(get_db(), name)))


@bp.get("/api/inventory/map/background")
def background():
    """図の背景画像。設定されていなければ 404。

    サーバのファイルをそのまま返すので、**設定に書かれた1枚だけ**を返す
    (パスを要求で受け取ると、任意のファイルを読ませることになる)。
    """
    from flask import send_file

    plan = pallet_map.load()
    path = getattr(plan.background, "path", "")
    if not path:
        return jsonify(_error("no_background", "背景画像は設定されていません")), 404
    try:
        return send_file(path)
    except OSError:
        log.warning("背景画像を読めません: %s", path)
        return jsonify(_error("no_background", "背景画像を読めませんでした")), 404


# ------------------------------------------------------------------
# 更新
# ------------------------------------------------------------------
@bp.post("/api/inventory/receive")
def receive():
    """受け入れ(VBA `btnReceive_Click`)。"""
    body = request.get_json(silent=True) or {}
    numbers, error = _numbers(body, ("width", "length", "qty"))
    if error:
        return error

    result = pallet_service.receive(
        get_db(),
        width=numbers["width"], length=numbers["length"], qty=numbers["qty"],
        position=str(body.get("position", "")).strip(),
        symbol=str(body.get("symbol", "")).strip(),
        industry=str(body.get("industry", "")).strip(),
        unit=str(body.get("unit", "")).strip(),
        note=str(body.get("note", "")).strip(),
    )
    return _transaction_response(result, "受入")


@bp.post("/api/inventory/issue")
def issue():
    """払い出し(VBA `btnDispatch_Click`)。"""
    body = request.get_json(silent=True) or {}
    numbers, error = _numbers(body, ("width", "length", "qty"))
    if error:
        return error

    result = pallet_service.issue(
        get_db(),
        width=numbers["width"], length=numbers["length"],
        position=str(body.get("position", "")).strip(),
        qty=numbers["qty"],
    )
    return _transaction_response(result, "払出")


def _numbers(body: dict, names: tuple[str, ...]):
    """必要な数値を取り出す。1つでも欠ければ 400 を返す。"""
    values = {}
    for name in names:
        try:
            values[name] = int(body.get(name))
        except (TypeError, ValueError):
            return None, (jsonify(_error(
                "bad_number", "数字で入れてください", field=name)), 400)
    return values, None


def _transaction_response(result, label: str):
    """在庫を動かした結果を返す。

    **失敗の種類でHTTPの返しを分ける**。画面はこれを見て、
    やり直せるのか・画面を取り直すのかを決める。
    """
    body = inv.result_dict(result)
    if result.ok:
        log.info("%s: %s", label, result.message)
        # 入出庫履歴を取り込み元へ送る。**画面には何も出さない**
        # (手元の登録はもう終わっており、届かなくても次の「取り込み元へ
        # 反映」でまとめて送られる)。設定画面が「登録のたびに自動でも
        # 送られる」と書いているのは、この呼び出しのこと
        data_sync.write_back_in_background()
        return jsonify(body)
    if result.conflict:
        # 他の端末が先に動かした。取り直してからでないと続けられない
        log.info("%s: 競合 %s", label, result.message)
        return jsonify({**body, "error": {"code": "conflict",
                                          "message": result.message}}), 409
    return jsonify({**body, "error": {"code": "rejected",
                                      "message": result.message}}), 422


def _error(code: str, message: str, field: str = "") -> dict:
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    return {"error": body}
