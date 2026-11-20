"""メモページ(VER4.9.0)── Excel 風のメモ。保存は `packaging_tool/memo_store.py`(この PC の手元の DB)

画面: `/memo`(現場モードのレール)。印刷: `/memo/print`(別の窓。帳票と同じく窓の印刷を使う)。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from packaging_tool import memo_store
from packaging_tool.logging_utils import get_logger

from .. import get_db
from ..shell import shell_context

log = get_logger("app.routes.memo")

bp = Blueprint("memo", __name__)


@bp.before_request
def _tables():
    """表が無ければ作る(起動のときに作るが、前の版の手元 DB をそのまま開いたときも困らないように。冪等)。"""
    conn = get_db()
    memo_store.ensure_tables(conn)
    conn.commit()


def _refuse(exc: memo_store.MemoError):
    status = {"not_found": 404, "conflict": 409}.get(exc.code, 400)
    body = {"error": {"code": exc.code, "message": str(exc)}}
    if isinstance(exc, memo_store.Conflict):
        body["cells"] = exc.cells
        body["rev"] = exc.rev
    return jsonify(body), status


def _sheets(conn) -> list[dict]:
    return [s.to_dict(with_cells=False) for s in memo_store.list_sheets(conn)]


@bp.get("/memo")
def page():
    conn = get_db()
    return render_template("memo.html", sheets=_sheets(conn), limits={
        "rows": memo_store.MAX_ROWS, "cols": memo_store.MAX_COLS, "text": memo_store.MAX_TEXT,
        "name": memo_store.NAME_MAX, "trash_days": memo_store.TRASH_DAYS},
        **shell_context("memo"))


@bp.get("/api/memo/sheets")
def sheets():
    conn = get_db()
    return jsonify({"sheets": _sheets(conn), "trash": memo_store.trashed(conn)})


@bp.get("/api/memo/sheets/<int:sheet_id>")
def sheet(sheet_id: int):
    try:
        return jsonify(memo_store.get_sheet(get_db(), sheet_id).to_dict())
    except memo_store.MemoError as exc:
        return _refuse(exc)


@bp.post("/api/memo/sheets")
def create():
    body = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        made = memo_store.create_sheet(conn, str(body.get("name") or ""))
    except memo_store.MemoError as exc:
        return _refuse(exc)
    return jsonify({"sheet": made.to_dict(), "sheets": _sheets(conn)})


@bp.post("/api/memo/sheets/<int:sheet_id>/<action>")
def act(sheet_id: int, action: str):
    body = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        if action == "save":
            ops = body.get("ops")
            if not isinstance(ops, list):
                return _refuse(memo_store.MemoError("保存する内容がありません。"))
            result = memo_store.save(conn, sheet_id, int(body.get("rev") or 0), ops)
            return jsonify(result)
        if action == "rename":
            done = memo_store.rename_sheet(conn, sheet_id, str(body.get("name") or ""))
            return jsonify({"sheet": done.to_dict(with_cells=False), "sheets": _sheets(conn)})
        if action == "duplicate":
            done = memo_store.duplicate_sheet(conn, sheet_id)
            return jsonify({"sheet": done.to_dict(), "sheets": _sheets(conn)})
        if action == "delete":
            memo_store.delete_sheet(conn, sheet_id)
            return jsonify({"sheets": _sheets(conn), "trash": memo_store.trashed(conn)})
        if action == "restore":
            done = memo_store.restore_sheet(conn, sheet_id)
            return jsonify({"sheet": done.to_dict(), "sheets": _sheets(conn), "trash": memo_store.trashed(conn)})
        if action == "move":
            memo_store.move_sheet(conn, sheet_id, int(body.get("to") or 0))
            return jsonify({"sheets": _sheets(conn)})
    except memo_store.MemoError as exc:
        return _refuse(exc)
    except (TypeError, ValueError) as exc:
        return _refuse(memo_store.MemoError(f"送られた内容が読めません({exc})。"))
    return _refuse(memo_store.MemoError("知らない操作です。", "not_found"))


@bp.get("/memo/print")
def print_page():
    """印刷(別の窓)。`?sheet=<ID>|all` と、範囲 `&range=r0,c0,r1,c1`(選んだ範囲を印刷するとき)。"""
    conn = get_db()
    which = request.args.get("sheet", "")
    try:
        if which == "all":
            data = [memo_store.get_sheet(conn, s.id).to_dict() for s in memo_store.list_sheets(conn)]
        else:
            data = [memo_store.get_sheet(conn, int(which)).to_dict()]
    except (memo_store.MemoError, ValueError):
        return render_template("memo_print.html", data=[], rng=None,
                               why="印刷するシートが見つかりません。メモの画面から開き直してください。")
    rng = None
    if request.args.get("range"):
        try:
            r0, c0, r1, c1 = (int(x) for x in request.args["range"].split(","))
            rng = [min(r0, r1), min(c0, c1), max(r0, r1), max(c0, c1)]
        except ValueError:
            rng = None
    return render_template("memo_print.html", data=data, rng=rng, why="")
