"""パレット適合設定 (`/pallet-fit`・`/api/pallet-fit/*`。VER4.8.116)

設定のマスタ管理・資材選択のパレット一覧から**別の窓**で開く専用ページ。中身は
`packaging_tool/fit_setting.py`(直す表と行を自動で決め、変わるパレットを見せてから共有へ書く)。

【どのモードでも登録する】
直せるかどうかは `fit_setting.can_edit`(資材モードの権限 + 管理者認証。マスタ管理と同じ関門)で
要求のたびに見る。見る・見本を作る・製品で確かめるは誰でもできる(書かないので)。

【画面の取り合いに入らない】
このページは「いま使っているタブ」を名乗らない(`window.APP.screenId` は空)。開いても、
作業中の資材選択のタブを取り上げない。書く口(反映・元に戻す)は書く錠を通る。
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request

from packaging_tool import app_config, fit_setting, idle_exit, master_state, user_settings
from packaging_tool.logging_utils import get_logger

from .. import _error, get_db

log = get_logger("app.routes.pallet_fit")

bp = Blueprint("pallet_fit", __name__)

# 断りの種別を HTTP に写す(`app/routes/master.py` と同じ考え方)
STATUS = {
    fit_setting.REFUSE_BAD_INPUT: 400,
    fit_setting.REFUSE_NOT_FOUND: 404,
    fit_setting.REFUSE_NOT_ALLOWED: 403,
    fit_setting.REFUSE_CHANGED: 409,
    fit_setting.REFUSE_EXPIRED: 409,
    fit_setting.REFUSE_NO_SOURCE: 422,
    fit_setting.REFUSE_UNSAFE: 422,
}

# 書かない POST(書く錠を通さない口。`app._NO_LOCK_PREFIXES` に入れてある)
READ_ONLY_PATHS = ("/api/pallet-fit/find", "/api/pallet-fit/plan", "/api/pallet-fit/check")


def _refuse(exc: fit_setting.FitError):
    return jsonify(_error(exc.reason, str(exc))), STATUS.get(exc.reason, 422)


def _body() -> dict:
    return request.get_json(silent=True) or {}


@bp.get("/pallet-fit")
def page():
    return render_template(
        "pallet_fit.html",
        display_name=current_app.config["DISPLAY_NAME"],
        version_label=app_config.version_label(),
        theme=user_settings.get_theme(),
        token=current_app.config["TOKEN"],
        alive_poll_ms=idle_exit.HEARTBEAT_MS,
        data_epoch=master_state.epoch(),
        # 資材選択のパレット一覧から開いたときは、その寸法から始める
        start_width=request.args.get("w", ""),
        start_length=request.args.get("l", ""))


@bp.get("/api/pallet-fit/state")
def state():
    """直せるか・どこに書くか・最近の変更。ページは数秒おきに聞き、マスタの世代が変われば読み直す。"""
    conn = get_db()
    allowed, why = fit_setting.can_edit(conn)
    # ほかの端末が直した閾値に気づく(背景の1本だけ。待たない)
    if current_app.config.get("READY", True):
        fit_setting.refresh_in_background()
    return jsonify(fit_setting.state(conn, allowed=allowed, why=why))


@bp.post("/api/pallet-fit/find")
def find():
    body = _body()
    try:
        return jsonify(fit_setting.find(get_db(), body.get("width"), body.get("length")))
    except fit_setting.FitError as exc:
        return _refuse(exc)


@bp.post("/api/pallet-fit/plan")
def plan():
    """見本を作る(**書かない**)。`kind`: fit(適合)/ size(サイズ)。"""
    body = _body()
    try:
        if body.get("kind") == fit_setting.KIND_SIZE:
            made = fit_setting.plan_size(get_db(), body)
        else:
            made = fit_setting.plan_fit(get_db(), body)
    except fit_setting.FitError as exc:
        return _refuse(exc)
    return jsonify({"ok": True, "plan": made.to_dict()})


@bp.post("/api/pallet-fit/check")
def check():
    body = _body()
    try:
        return jsonify(fit_setting.check_product(body.get("plan_id"), body.get("width"), body.get("length")))
    except fit_setting.FitError as exc:
        return _refuse(exc)


@bp.post("/api/pallet-fit/apply")
def apply():
    body = _body()
    result = fit_setting.apply(get_db(), str(body.get("plan_id") or ""))
    payload = {"ok": result.ok, "message": result.message, "log_no": result.log_no,
               "history": fit_setting.history()}
    if result.ok:
        return jsonify(payload)
    return jsonify({**payload, **_error(result.reason or "failed", result.message)}), \
        STATUS.get(result.reason, 422)


@bp.post("/api/pallet-fit/undo")
def undo():
    body = _body()
    result = fit_setting.undo(get_db(), body.get("log_no"))
    payload = {"ok": result.ok, "message": result.message, "history": fit_setting.history()}
    if result.ok:
        return jsonify(payload)
    return jsonify({**payload, **_error(result.reason or "failed", result.message)}), \
        STATUS.get(result.reason, 422)


@bp.get("/api/pallet-fit/history")
def history():
    return jsonify({"history": fit_setting.history()})
