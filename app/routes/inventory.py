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

from packaging_tool import (data_sync, pallet_map_session, pallet_service)
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import inventory as inv

from .. import get_db
from ..shell import shell_context

log = get_logger("app.routes.inventory")

bp = Blueprint("inventory", __name__)


def _db():
    """手元のDB。**共有が変わっていれば、先に取り込み直してから渡す。**

    在庫数は受入・払出の履歴と一緒に共有へ届く(`sync_writeback.apply_stock`)。
    ほかの端末が動かした数は取り込むまで手元に無いので、見る前・動かす前に
    追いつく。変わっていなければファイルの姿を見るだけで帰る。
    届かない端末では何もせず、手元の値で続ける。
    """
    conn = get_db()
    try:
        data_sync.refresh_orders(conn, only_if_changed=True)
    except Exception:                               # noqa: BLE001 - 画面は止めない
        log.exception("在庫を見る前の取り込み直しに失敗(手元の値で続けます)")
    return conn


@bp.get("/inventory")
def page():
    view = inv.initial(_db())
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
    return jsonify(inv.to_dict(inv.search(_db(), width, length, mode)))


@bp.get("/api/inventory/position/<name>")
def at_position(name: str):
    return jsonify(inv.to_dict(inv.at_position(_db(), name)))


@bp.get("/api/inventory/map/background")
def background():
    """図の背景画像。設定されていなければ 404。

    `data:` URL をそのまま返す。**要求でパスを受け取らない**ので、
    任意のファイルを読ませる余地が無い(`routes/layout.background` と同じ)。

    以前はここが `background.path` を開こうとしていた。`Background` に
    `path` は無い(あるのは `image`)ので、背景を設定しても必ず404で、
    簡易在庫の図に写真が出たことは一度も無かった。
    """
    import base64
    import binascii

    from flask import Response

    image = _map_session().plan.background.image
    if not image or not image.startswith("data:"):
        return jsonify(_error("no_background", "背景画像は設定されていません")), 404
    try:
        header, encoded = image.split(",", 1)
        mimetype = header[len("data:"):].split(";")[0]
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        log.warning("背景画像を読めません(壊れた data: URL)")
        return jsonify(_error("no_background", "背景画像を読めませんでした")), 404
    return Response(raw, mimetype=mimetype or "application/octet-stream")


# ------------------------------------------------------------------
# 配置編集
#
# 棚検索(`routes/layout.py`)と同じ約束にそろえてある。覚え直しを
# 増やさないため、URLの形も断りの種類も同じ。
# ------------------------------------------------------------------
# 背景画像の上限と形式。**棚検索と同じ**(片方だけ通る画像を作らない)
MAX_BACKGROUND_BYTES = 4 * 1024 * 1024
ALLOWED_IMAGE_PREFIXES = ("data:image/png;base64,", "data:image/jpeg;base64,",
                          "data:image/gif;base64,", "data:image/webp;base64,")


def _map_session():
    return pallet_map_session.get_session()


def _map_state(result=None):
    """図を触ったあとの**画面ぜんぶ**。どの操作の後も同じものを返す。"""
    view = inv.initial(_db())
    body = inv.to_dict(view)
    if result is not None:
        body["message"] = result.message
    return body


def _map_apply(result):
    """操作1回の結果を応答にする(`routes/layout._apply` と同じ約束)。

    断ったときも**画面ぜんぶを返す**。押した拍子に図が消えると、
    断られたのか壊れたのか区別できない。
    """
    if result.ok:
        return jsonify(_map_state(result))
    status = _MAP_STATUS.get(result.reason, 400)
    if status == 400:
        return jsonify(_error(result.reason, result.message)), status
    return jsonify({**_map_state(result),
                    "error": {"code": result.reason,
                              "message": result.message}}), status


# **棚検索(`routes/layout._STATUS_BY_REASON`)と同じ写し方。**
# 同じ断りに違う番号を返すと、画面側が図ごとに書き分けることになる
_MAP_STATUS = {
    pallet_map_session.REFUSE_BAD_INPUT: 400,
    pallet_map_session.REFUSE_NOT_LISTED: 400,
    pallet_map_session.REFUSE_FAILED: 500,
}


# 図を**変える**要求の頭。背景画像を読むのは GET なので含まれない
_MAP_EDIT_PREFIX = "/api/inventory/map/"
# 払い出し。現場の声:「受け入れは必要かなぁ 払い出しは現場だね」
_ISSUE_PATH = "/api/inventory/issue"


@bp.before_request
def _field_only_writes():
    """配置編集と払い出しは現場モードだけ。資材モードは見る・受け入れまで。

    **モードで分ける**(権限では分けない)のは `warehouse.field_only` と
    同じ理由 ── `mode:field` は誰でも持つ既定の権限なので、権限で断っても
    誰も断れない。編集した図は同じファイル(`pallet_map.json`)なので、
    資材モードでもそのまま同じ配置が出る。
    """
    if request.method != "POST":
        return None
    if request.path.startswith(_MAP_EDIT_PREFIX):
        what = "配置編集"
    elif request.path == _ISSUE_PATH:
        what = "払い出し"
    else:
        return None
    from .. import current_mode
    from packaging_tool import modes
    if current_mode() == modes.FIELD:
        return None
    log.info("現場モードではないため%sを断りました: %s", what, request.path)
    return jsonify(_error(
        "wrong_mode",
        f"{what}は現場モードでのみ行えます。右上でモードを切り替えてください。")), 403


@bp.post("/api/inventory/map/edit")
def set_map_editing():
    """配置編集の入り切り。

    通常は動かせない。図はよく押す(その位置の在庫を見る)ので、
    常時ドラッグできると見るつもりの操作で位置がずれる。
    """
    body = request.get_json(silent=True) or {}
    return _map_apply(_map_session().set_editing(bool(body.get("on"))))


@bp.post("/api/inventory/map/move")
def move_position():
    """保管位置を動かす。座標は**図の論理座標**(SVGの viewBox と同じ)。"""
    body = request.get_json(silent=True) or {}
    x, y = _to_float(body.get("x")), _to_float(body.get("y"))
    if x is None or y is None:
        return jsonify(_error("bad_point", "移動先を指定してください。")), 400
    return _map_apply(_map_session().move(str(body.get("name", "")), x, y))


@bp.post("/api/inventory/map/arrange")
def arrange_positions():
    """選んだ保管位置をそろえる/詰める。`{"names": [...], "op": "top"}`。"""
    body = request.get_json(silent=True) or {}
    names = body.get("names")
    # **名前は文字の一覧でだけ受ける。** 一覧でないもの(文字1つ・数)を
    # 受けると、1文字ずつ名前として読まれて「A は図にありません」になる
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return jsonify(_error("bad_names", "並べる保管位置を選んでください。")), 400
    return _map_apply(_map_session().arrange(names, str(body.get("op", ""))))


@bp.post("/api/inventory/map/resize")
def resize_position():
    """保管位置の大きさを変える(背景の写真に合わせこむため)。"""
    body = request.get_json(silent=True) or {}
    w, h = _to_float(body.get("w")), _to_float(body.get("h"))
    if w is None or h is None:
        return jsonify(_error("bad_size", "大きさを指定してください。")), 400
    return _map_apply(_map_session().resize(str(body.get("name", "")), w, h))


@bp.post("/api/inventory/map/add")
def add_position():
    body = request.get_json(silent=True) or {}
    return _map_apply(_map_session().add(str(body.get("name", ""))))


@bp.post("/api/inventory/map/remove")
def remove_position():
    body = request.get_json(silent=True) or {}
    return _map_apply(_map_session().remove(str(body.get("name", ""))))


@bp.post("/api/inventory/map/background")
def set_map_background():
    """背景画像を差し替える。**中身を受け取る**(棚検索と同じ)。"""
    body = request.get_json(silent=True) or {}
    image = str(body.get("image", ""))
    if image:
        if not image.startswith(ALLOWED_IMAGE_PREFIXES):
            return jsonify(_error(
                "bad_image",
                "画像は PNG / JPEG / GIF / WebP を選んでください。")), 400
        if len(image) > MAX_BACKGROUND_BYTES:
            return jsonify(_error(
                "too_large",
                f"画像が大きすぎます({MAX_BACKGROUND_BYTES // (1024 * 1024)}MBまで)。"
                "縮小してから選んでください。")), 400
    return _map_apply(_map_session().set_background(image))


@bp.post("/api/inventory/map/background/place")
def place_map_background():
    """背景の写真をずらす・拡げ縮めする。**箱は動かさない。**"""
    body = request.get_json(silent=True) or {}
    x, y = _to_float(body.get("x")), _to_float(body.get("y"))
    scale = _to_float(body.get("scale"))
    if x is None or y is None or scale is None:
        return jsonify(_error("bad_point", "背景の位置と倍率を指定してください。")), 400
    return _map_apply(_map_session().place_background(x, y, scale))


@bp.post("/api/inventory/map/save")
def save_map():
    return _map_apply(_map_session().save())


@bp.post("/api/inventory/map/reset")
def reset_map():
    """出荷時の配置に戻す。編集した内容は消える。"""
    return _map_apply(_map_session().reset())


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        _db(),
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
        _db(),
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
