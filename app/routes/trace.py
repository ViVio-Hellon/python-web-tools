"""ログ (`/api/trace/*`, `/api/client-error`)

設定画面「ログ」の面と、画面(ブラウザ)で起きたエラーの受け口。
中身は `packaging_tool/trace_log.py`。**どのモードでも登録する** ──
後から追う手段は、どの画面で困ったときにも要る。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from packaging_tool import logging_utils, trace_log
from packaging_tool.logging_utils import get_logger

log = get_logger("app.routes.trace")

bp = Blueprint("trace", __name__)

# 画面から届くエラーの文の長さの上限(壊れた画面が巨大な文を送ってきても
# ログを埋めない)
CLIENT_TEXT_LIMIT = 4000


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


@bp.get("/api/trace/state")
def state():
    return jsonify({"log_dir": trace_log.state()})


@bp.get("/api/trace/errors")
def errors():
    everyone = request.args.get("everyone") == "1"
    items = trace_log.list_errors(everyone=everyone,
                                  query=request.args.get("q", ""))
    return jsonify({"items": [i.to_dict() for i in items],
                    "everyone": everyone, "log_dir": trace_log.state()})


@bp.get("/api/trace/errors/<ref>")
def error_detail(ref: str):
    found = trace_log.find_error(ref)
    if found is None:
        return jsonify(_error("not_found", f"エラー番号 {ref} の記録が見つかりません"
                                           "(ほかの端末か、もう消えた古い記録です)。")), 404
    found.pop("_folder", None)
    found["why_why"] = trace_log.why_why_text(found)
    return jsonify(found)


@bp.post("/api/trace/log-dir")
def set_log_dir():
    body = request.get_json(silent=True) or {}
    result = trace_log.set_log_dir(str(body.get("path", "")), str(body.get("password", "")))
    if not result.ok:
        status = 403 if result.reason == trace_log.REFUSE_NEED_PASSWORD else 400
        return jsonify(_error(result.reason, result.message)), status
    return jsonify({"message": result.message, "log_dir": trace_log.state()})


@bp.post("/api/client-error")
def client_error():
    """画面(ブラウザ)の中で起きたエラー。**サーバのログに入れて番号を返す。**

    画面のエラーはブラウザの中で消えてしまい、後から何も追えなかった
    (「押しても何も起きない」の正体がこれのことがある)。
    """
    body = request.get_json(silent=True) or {}

    def text(key: str) -> str:
        return str(body.get(key, "") or "")[:CLIENT_TEXT_LIMIT]

    # 一覧で「どの画面で」が分かるように、操作に画面を書き足す
    # (この要求の経路 /api/client-error では何も分からない)
    op = logging_utils.current_operation()
    if op is not None:
        op["client"] = True
        op["page"] = text("page")
        op.pop("input", None)
    ref = logging_utils.new_error_ref()
    where = text("source")
    if body.get("line"):
        where += f":{body.get('line')}:{body.get('column', '')}"
    log.error("画面のエラー: %s (画面 %s / %s)", text("message"), text("page"), where,
              extra={"ref": ref, "detail": text("stack")})
    return jsonify({"ref": ref})
