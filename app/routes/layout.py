"""棚検索 (旧 `frmLayout`)

置き場の配置図から資材を探す。あわせて、**置き場が変わったら現場で
図を直せる**ようにする(配置編集)。

【HTTPの使い分け(設計書 §6.1)】
- 400 … サイズが数字でない・編集モードでないのに動かした
- 422 … 形は正しいが**業務として断る**(そのサイズの置き場が無い)

【配置編集は保存するまでファイルに触らない】
ドラッグするたびに書き込むと、間違えて動かしたものを戻せません。
tkinter版も「保存」で初めて書き出していました。
"""
from __future__ import annotations

from typing import Optional

from flask import Blueprint, jsonify, render_template, request

from packaging_tool import layout_session
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import layout as presenter

from .. import get_db
from ..shell import shell_context

log = get_logger("app.routes.layout")

bp = Blueprint("layout", __name__)

# 背景画像の上限。図の下敷きに使うだけなので、大きい写真は要らない。
# 上限が無いと、`floor_plan.json` が数十MBになって起動のたびに読まれる
MAX_BACKGROUND_BYTES = 4 * 1024 * 1024

# 受け付ける画像の形式。`tk.PhotoImage` の PNG/GIF 制約が無くなったので
# **JPEG も使える**(§7.2)
ALLOWED_IMAGE_PREFIXES = ("data:image/png;base64,", "data:image/jpeg;base64,",
                          "data:image/gif;base64,", "data:image/webp;base64,")


def _session():
    return layout_session.get_session()


def _state(session, **extra):
    """いまの画面ぜんぶ。**どの操作の後も同じものを返す**(設計書 §3.3)。"""
    view = presenter.build(get_db(), session)
    body = presenter.to_dict(view)
    body.update(extra)
    return jsonify(body)


@bp.get("/layout")
def page():
    session = _session()
    view = presenter.build(get_db(), session)
    return render_template(
        "layout.html",
        view=view,
        state=presenter.to_dict(view),
        **shell_context("layout"),
    )


@bp.get("/api/layout/state")
def state():
    return _state(_session())


# ------------------------------------------------------------------
# 検索
# ------------------------------------------------------------------
@bp.post("/api/layout/search")
def search():
    """最寄りの置き場を探す。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    return _apply(session, session.search(
        get_db(), str(body.get("kind", presenter.KIND_BOARD)),
        str(body.get("width", "")), str(body.get("length", ""))))


@bp.post("/api/layout/from-selection")
def from_selection():
    """資材選択が渡した資材の置き場を、まとめて光らせる。

    渡ってくるのは `work_context`(画面をまたぐ受け渡しの唯一の口)。
    画面から品目を送らせないのは、**同じ事実を2か所に持たない**ため。
    """
    from packaging_tool import work_context

    session = _session()
    return _apply(session, session.show_selection(
        get_db(), work_context.get_context().map_items))


@bp.post("/api/layout/select")
def select():
    """置き場を押す。そこに何があるかを出す(VBA版に無い機能)。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    return _apply(session, session.select(str(body.get("name", ""))))


@bp.post("/api/layout/base-point")
def base_point():
    """拠点の切替。**資材選択と共有する設定**なので疲労度にも効く。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    return _apply(session, session.set_base_point(str(body.get("name", ""))))


# ------------------------------------------------------------------
# 配置編集
# ------------------------------------------------------------------
@bp.post("/api/layout/edit")
def set_editing():
    """配置編集の入り切り。

    通常は動かせない。図はよく押す(中身を見る)ので、常時ドラッグ
    できると見るつもりの操作で置き場がずれる。
    """
    body = request.get_json(silent=True) or {}
    session = _session()
    return _apply(session, session.set_editing(bool(body.get("on"))))


@bp.post("/api/layout/move")
def move():
    """置き場を動かす。座標は**図の論理座標**(SVGの viewBox と同じ)。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    x, y = _to_float(body.get("x")), _to_float(body.get("y"))
    if x is None or y is None:
        return jsonify(_error("bad_point", "移動先を指定してください。")), 400
    return _apply(session, session.move(str(body.get("name", "")), x, y))


@bp.post("/api/layout/resize")
def resize():
    """置き場の大きさを変える(背景の写真に合わせこむため)。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    w, h = _to_float(body.get("w")), _to_float(body.get("h"))
    if w is None or h is None:
        return jsonify(_error("bad_size", "大きさを指定してください。")), 400
    return _apply(session, session.resize(str(body.get("name", "")), w, h))


@bp.post("/api/layout/background/place")
def place_background():
    """背景の写真をずらす・拡げ縮めする。**箱は動かさない。**"""
    body = request.get_json(silent=True) or {}
    session = _session()
    x, y = _to_float(body.get("x")), _to_float(body.get("y"))
    scale = _to_float(body.get("scale"))
    if x is None or y is None or scale is None:
        return jsonify(_error("bad_point", "背景の位置と倍率を指定してください。")), 400
    return _apply(session, session.place_background(x, y, scale))


@bp.post("/api/layout/add")
def add():
    body = request.get_json(silent=True) or {}
    session = _session()
    return _apply(session, session.add(str(body.get("name", ""))))


@bp.post("/api/layout/remove")
def remove():
    body = request.get_json(silent=True) or {}
    session = _session()
    return _apply(session, session.remove(str(body.get("name", ""))))


@bp.post("/api/layout/background")
def set_background():
    """背景画像を差し替える(§7.2)。

    **中身を受け取る。** ブラウザのファイル選択はクライアント側のパスしか
    返さないので、サーバがそのパスを開けるとは限らない(共有フォルダから
    選ばれることもある)。`data:` URL のまま `floor_plan.json` に持つ。
    """
    body = request.get_json(silent=True) or {}
    session = _session()
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
    return _apply(session, session.set_background(image))


@bp.get("/api/layout/map/background")
def background():
    """図の背景画像。設定されていなければ 404。

    `data:` URL をそのまま返す。**要求でパスを受け取らない**ので、
    任意のファイルを読ませる余地が無い。
    """
    import base64
    import binascii

    from flask import Response

    image = _session().plan.background.image
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
# 保存
# ------------------------------------------------------------------
@bp.post("/api/layout/save")
def save():
    session = _session()
    return _apply(session, session.save())


@bp.post("/api/layout/reset")
def reset():
    """出荷時の配置に戻す。編集した内容は消える。"""
    session = _session()
    return _apply(session, session.reset())


# ------------------------------------------------------------------
# 共通
# ------------------------------------------------------------------
_STATUS_BY_REASON = {
    layout_session.REFUSE_BAD_INPUT: 400,
    layout_session.REFUSE_NOT_LISTED: 400,
    layout_session.REFUSE_NOT_FOUND: 422,
    layout_session.REFUSE_NO_HANDOFF: 422,
    layout_session.REFUSE_FAILED: 500,
}


def _apply(session, result):
    """操作1回の結果を応答にする(`routes/selection.py` と同じ約束)。

    断られた場合も**画面ぜんぶを返す**(422 / 500)。押した拍子に図が
    消えると、断られたのか壊れたのか区別できない。
    """
    if not result.ok:
        status = _STATUS_BY_REASON.get(result.reason, 400)
        if status == 400:
            return jsonify(_error(result.reason, result.message)), status
        return _state(session, message=result.message), status
    return _state(session, message=result.message)


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
