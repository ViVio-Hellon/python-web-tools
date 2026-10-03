"""設定画面

**この端末の設定をここに集める。** 取り込み元の置き場所・起動時の
自動取り込み・拠点・包装仕様書のURL・ロット一覧のよく使う条件。
取り込みと書き戻しの実行もここから行う。

以前は置き場所が「データ」画面、拠点が資材選択と棚検索の中にあった。
どこで何が変えられるのかを探すこと自体が手間で、変えたつもりで
変わっていない事故も起きる。

【進捗はプロセス側が持つ】
**ブラウザを閉じても取り込みは続く**(基盤仕様書 2.9)ので、進捗は
`packaging_tool/jobs.py` が持ち、画面は見に行くだけ。開き直しても、
別のタブから見ても、同じものが見える。

【フォルダの「参照...」について】
ブラウザのファイル選択ダイアログは**クライアント側**のパスしか返さない
ので、サーバ(=このPC)から見たフォルダは選べない。代わりに
`GET /api/fs/list` がサーバ側のフォルダを一覧する。直接書いてもよく、
保存すると即座に「そのフォルダで何が見つかったか」を出す。
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, render_template, request

from packaging_tool import data_sync, db, jobs, modes, source_db, trace_log
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import settings as settings_presenter
from packaging_tool.presenters import fs_browse
from packaging_tool.presenters import master as master_presenter

from .. import get_db
from ..shell import shell_context

log = get_logger("app.routes.settings")

bp = Blueprint("settings", __name__)


def _caught_up_db():
    """手元のDB。**共有が変わっていれば、先に取り込み直してから渡す。**

    ボード人気度は全端末の「使用する」を合計した数で、ほかの端末の分は
    取り込むまで手元に無い。見る前・書き出す前に追いつく。変わって
    いなければファイルの姿を見るだけで帰り、届かなければ手元の数で出す。
    """
    conn = get_db()
    try:
        from packaging_tool import data_sync
        data_sync.refresh_orders(conn, only_if_changed=True)
    except Exception:                               # noqa: BLE001 - 画面は止めない
        log.exception("設定画面の前の取り込み直しに失敗(手元の数で出します)")
    return conn


@bp.get("/settings")
def page():
    view = settings_presenter.build(_caught_up_db(), _startup_modes())
    registry = jobs.get_registry()
    # `?tab=` で面を名指しできる(帯のモード表示から「この端末の権限」
    # へ直接連れて行くため)。JS を待たずに最初の描画から正しい面が
    # 開くよう、サーバ側でも同じ既定を選ぶ(`tabs.js` と揃える)
    requested_tab = request.args.get("tab", "")
    valid_tabs = {key for key, _ in settings_presenter.TABS}
    default_tab = requested_tab if requested_tab in valid_tabs else settings_presenter.DEFAULT_TAB
    return render_template(
        "settings.html",
        view=view,
        # マスタ管理の面。**ここでは共有フォルダに触らない** ── 設定を
        # 1つ変えたいだけの人まで、19テーブルを数え終わるのを待つことに
        # なる。中身はその面を開いたときに読む(`/api/master/browse`)
        master=master_presenter.to_dict(master_presenter.frame(get_db())),
        targets=settings_presenter.TARGETS,
        suffixes=" / ".join(source_db.SUFFIXES),
        level_label=settings_presenter.LEVEL_LABEL,
        tabs=settings_presenter.TABS,
        default_tab=default_tab,
        # 保存場所の面(このPCだけ / 全PC共通)。いまの設定から引く
        storage=settings_presenter.storage_places(),
        # ログの面(出力先)。エラー記録は面を開いたときに読む
        logs=trace_log.state(),
        # **面の印も初回から出す。** 開いていない面の問題を、JSが動くのを
        # 待たずに読めるようにする
        tab_badges=settings_presenter.tab_badges(view),
        # 初回の描画をJSに任せない。開いた瞬間に読める
        state=settings_presenter.to_dict(view),
        job_state=settings_presenter.jobs_dict(registry),
        **shell_context("settings",
                        badges={"settings": _badge(view)}),
    )


def _badge(view) -> tuple[str, str]:
    """レールに出す印。行く前に「その先に何があるか」を示す。"""
    if view.level == settings_presenter.NG:
        return ("!", "todo")
    if view.level == settings_presenter.WARN:
        return ("?", "todo")
    return ("", "done")


@bp.get("/api/settings/state")
def state():
    return jsonify(settings_presenter.to_dict(
        settings_presenter.build(get_db(), _startup_modes())))


def _startup_modes() -> tuple[str, ...]:
    """**起動したときに使えた、開き直しが要るモード。**

    画面(登録するURL)を起動時の権限で決めているのは
    `app.GATED_MODES` のモードだけ。それ以外は要求のたびに権限を見るので、
    あとから足してもその場で使える ── 全部のモードを返すと、開き直しが
    要らないモードにまで「開き直してください」と出る(現場の指摘:
    「なぜ開きなおしが要るんですか 面倒ですよ」)。

    ここで **GATED_MODES に無いモードは「起動時にもあった」ことにする**。
    そうすれば `_access_section` の突き合わせに引っかからない。
    """
    from flask import current_app
    from .. import GATED_MODES
    grant = current_app.config.get("STARTUP_GRANT")
    startup = set(grant.allowed_modes()) if grant is not None else set()
    # 開き直しが要らないモードは、いま使えるなら起動時にもあった扱い
    startup |= {m.key for m in modes.ALL if m.key not in GATED_MODES}
    return tuple(m.key for m in modes.ALL if m.key in startup)


@bp.post("/api/settings/save")
def save():
    """設定を保存する。**本文に入っている項目だけ**を触る。

    一部だけ送られてきたときに、送っていない項目を消さないため。
    保存しただけで終わらせず、その設定で何が見つかるかまで返す ──
    「保存しました」だけでは、直ったのかどうかが分からない。
    """
    body = request.get_json(silent=True) or {}

    def pick(key):
        return str(body[key]) if key in body else None

    result = settings_presenter.save(
        master_dir=pick("master_dir"),
        lot_dir=pick("lot_dir"),
        kanban_dir=pick("kanban_dir"),
        threshold_dir=pick("threshold_dir"),
        auto_import=bool(body["auto_import"]) if "auto_import" in body else None,
        spec_url=pick("spec_sheet_url"),
        position=pick("position"),
        # 置き場所を**変えるとき**だけ要る。値は保存も応答もしない
        password=pick("password"))
    if not result.ok:
        # 断りの種別を HTTP に写す(設計.md §1)。
        #   400 … 入力の形が違う。**サーバの状態は動いていない**
        #   403 … 管理者パスワードが要る / 合っていない
        status = (403 if result.reason == settings_presenter.REFUSE_NEED_PASSWORD
                  else 400)
        return jsonify(_error(result.reason, result.message)), status

    log.info("設定を保存しました: %s", ", ".join(sorted(body)))
    state = settings_presenter.to_dict(
        settings_presenter.build(get_db(), _startup_modes()))
    state["message"] = result.message
    return jsonify(state)


@bp.post("/api/settings/admin-password")
def change_admin_password():
    """管理者パスワードを変える。**値は保存も応答もしない**(撹拌して持つ)。

    実績パターンの保存ボタンを誤って押されないためのUIガードで、
    誰がその端末を使えるかは `アクセス権限` マスタが決める
    (`packaging_tool/admin_password.py`)。
    """
    from packaging_tool import admin_password

    body = request.get_json(silent=True) or {}
    if body.get("reset"):
        result = admin_password.reset(str(body.get("current", "")))
    else:
        result = admin_password.change(str(body.get("current", "")),
                                       str(body.get("new", "")),
                                       str(body.get("confirm", "")))
    if not result.ok:
        # 入力の形の誤り。**サーバの状態は動いていない**
        return jsonify(_error(result.reason, result.message)), 400
    state = settings_presenter.to_dict(
        settings_presenter.build(get_db(), _startup_modes()))
    state["message"] = result.message
    return jsonify(state)


# ------------------------------------------------------------------
# 配布設定(`packaging_tool/distribution.py`)。どれも管理者パスワードが要る
# ------------------------------------------------------------------
def _distribution_reply(result):
    from packaging_tool import distribution
    if not result.ok:
        status = 403 if result.reason == distribution.REFUSE_NEED_PASSWORD else 400
        if result.reason == distribution.REFUSE_FAILED:
            status = 500
        return jsonify(_error(result.reason, result.message)), status
    state = settings_presenter.to_dict(
        settings_presenter.build(get_db(), _startup_modes()))
    state["message"] = result.message
    return jsonify(state)


@bp.post("/api/settings/distribution/export")
def export_distribution():
    """この端末のいまの設定を、配布設定として書き出す。"""
    from packaging_tool import distribution
    body = request.get_json(silent=True) or {}
    items, maps = body.get("items"), body.get("maps")
    if not isinstance(items, list) or not isinstance(maps, list) \
            or not all(isinstance(x, str) for x in items + maps):
        return jsonify(_error("bad_input", "入れる項目の形が違います。")), 400
    return _distribution_reply(distribution.export(
        str(body.get("password", "")), items, maps))


@bp.post("/api/settings/distribution/remove")
def remove_distribution():
    from packaging_tool import distribution
    body = request.get_json(silent=True) or {}
    return _distribution_reply(distribution.remove(str(body.get("password", ""))))


@bp.post("/api/settings/distribution/reapply")
def reapply_distribution():
    """置いてある配布設定を読み込み直す(配置図も上書き)。"""
    from packaging_tool import distribution
    body = request.get_json(silent=True) or {}
    return _distribution_reply(distribution.reapply(str(body.get("password", ""))))


@bp.post("/api/settings/lot-filter/delete")
def delete_lot_filter():
    """ロット一覧の「よく使う条件」を消す。"""
    name = str((request.get_json(silent=True) or {}).get("name", ""))
    result = settings_presenter.delete_lot_filter(name)
    if not result.ok:
        return jsonify(_error(result.reason, result.message)), 400
    state = settings_presenter.to_dict(
        settings_presenter.build(get_db(), _startup_modes()))
    state["message"] = result.message
    return jsonify(state)


@bp.post("/api/settings/board-usage/export")
def export_board_usage():
    """ボード人気度をCSVに書き出す。`{"dir": "..."}`(省略可)。

    渡した書き出し先はそのまま設定として覚えるので、次からは空で
    押せば同じ場所に出る。**書いた場所を必ず返す** ── 書き出しで
    いちばん困るのは「書けたのに、どこにあるか分からない」。
    """
    raw = (request.get_json(silent=True) or {}).get("dir", "")
    # **文字列でなければ断る。** 以前は何でも `str()` していたので、
    # 一覧 `[1, 2]` や `None` がそのまま**フォルダ名になって作られて**
    # いた(相対の道はアプリのフォルダ基準なので、アプリの隣に
    # `[1, 2]` `None` `True` … が並ぶ)。画面は必ず文字列を送るので、
    # それ以外が来たら誤りとして返す
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        return jsonify(_error("bad_dir",
                              "書き出し先はフォルダの道(文字)で指定してください")), 400
    result = settings_presenter.export_board_usage(_caught_up_db(), raw)
    if not result.ok:
        return jsonify(_error(result.reason, result.message)), 400
    state = settings_presenter.to_dict(
        settings_presenter.build(get_db(), _startup_modes()))
    state["message"] = result.message
    return jsonify(state)


# ------------------------------------------------------------------
# 長時間処理
# ------------------------------------------------------------------
@bp.get("/api/fs/list")
def fs_list():
    """サーバから見えるフォルダの一覧(§7.2)。

    返すのは**フォルダの名前**と、そこにある **取り込み元ファイルの名前**だけ。
    ファイルの中身は返さない ── 読み取り口をここに作らない。

    一覧そのものが目的なので「上のフォルダへ行かせない」たぐいの制限は
    付けない(それでは置き場所を探せない)。守りは `app/__init__.py` の
    127.0.0.1 バインド + 起動トークン + Host検証 + 同一オリジン確認。
    """
    view = fs_browse.browse(request.args.get("path", ""),
                            access=request.args.get("access") == "1")
    return jsonify(fs_browse.to_dict(view))


@bp.post("/api/settings/import")
def start_import():
    """`{"target": "all" | "master" | "lot"}`。"""
    # **文字列にしてから照らす。** 一覧・辞書が来ると `in` が
    # `TypeError: unhashable type` で落ち、断りの 400 まで届かない
    target = str((request.get_json(silent=True) or {}).get("target", "all"))
    if target not in settings_presenter.TARGET_KEYS:
        return jsonify(_error("bad_target", "取り込む対象が正しくありません")), 400

    label = next(name for key, name, _ in settings_presenter.TARGETS if key == target)
    work = {
        "all": lambda c, p: data_sync.import_all(c, progress=p),
        "master": lambda c, p: data_sync.import_master(c, progress=p),
        "lot": lambda c, p: data_sync.import_lot_ledger(c, progress=p),
    }[target]
    return _start("import", label, lambda p: _with_conn(lambda c: work(c, p)))


@bp.get("/report/import-diag")
def import_diag_report():
    """取り込みの記録(`import_diag`)を別窓で見る。`?save=1` なら保存させる。

    ログフォルダは Windows では隠しフォルダ(`%LOCALAPPDATA%`)の中にあり、
    現場からは探せない。**送ってもらうための道**を画面に置く。
    `/report/` の下なので起動トークンが要る(`app/__init__.py`)。
    """
    from packaging_tool import import_diag
    path = import_diag.latest_path()
    if path is None:
        body = ("まだ取り込みの記録がありません。\n"
                "取り込みを1回実行すると作られます(起動時の自動取り込みでも作られます)。\n")
        return Response(body, mimetype="text/plain; charset=utf-8")
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Response(f"記録を読めません: {path}\n{exc}\n", status=500,
                        mimetype="text/plain; charset=utf-8")
    response = Response(body, mimetype="text/plain; charset=utf-8")
    if request.args.get("save") == "1":
        from urllib.parse import quote
        response.headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(path.name)}")
    return response


@bp.get("/api/settings/table-bring/plan")
def table_bring_plan():
    """Access で作った表を持ってくる ── まず中を見る(書かない)。`?path=`"""
    from packaging_tool import table_bring
    # 変換ツールの場所(Access のまま読むとき)。送られてきたら覚える
    if "converter" in request.args:
        why = table_bring.set_converter_dir(request.args.get("converter", ""))
        if why:
            return jsonify(table_bring.plan_dict(table_bring.Plan(
                source=request.args.get("path", ""), message=why)))
    return jsonify(table_bring.plan_dict(
        table_bring.plan(request.args.get("path", ""))))


@bp.post("/api/settings/table-bring/upload")
def table_bring_upload():
    """ドラッグ&ドロップされたファイルを受け取る。返すのは置いた場所。

    ブラウザはファイルの**場所**を教えてくれない(中身だけ渡す)。このツールは
    同じPCで動いているので、手元の作業フォルダへ置いて、そこから読む。
    """
    from packaging_tool import table_bring
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(_error("no_file", "ファイルが届きませんでした")), 400
    saved, why = table_bring.save_upload(upload.filename, upload.stream)
    if saved is None:
        return jsonify(_error("bad_file", why)), 400
    return jsonify({"path": str(saved)})


@bp.post("/api/settings/table-bring")
def table_bring_run():
    """選んだ表を梱包資材マスタへ足す。`{"path":…, "tables":[…]}`

    共有のマスタに書くので、マスタ管理と同じ関門(`master_admin.can_edit`
    ── 管理者パスワード、権限があれば資材モードも)を通す。
    """
    from packaging_tool import master_admin, selection_session, table_bring
    conn = get_db()
    # **パスワードは必ず。** `can_edit` はアクセス権限がまだ空のとき誰でも
    # 通す(最初の1行を入れるための逃げ道)が、表を足すのはその用途ではない
    if not selection_session.get_session(conn).admin:
        return jsonify(_error("not_allowed", "管理者認証が必要です。"
                              "「パスワード」の面で認証してください。")), 403
    allowed, why = master_admin.can_edit(conn, "")
    if not allowed:
        return jsonify(_error("not_allowed", why)), 403
    body = request.get_json(silent=True) or {}
    tables = body.get("tables")
    if not isinstance(tables, list):
        return jsonify(_error("bad_tables", "持ってくる表の指定が正しくありません")), 400
    result = table_bring.bring(str(body.get("path", "")), [str(t) for t in tables])
    payload = {"ok": result.ok, "message": result.message,
               "brought": [{"name": n, "rows": r} for n, r in result.brought],
               "backup": result.backup,
               "plan": table_bring.plan_dict(table_bring.plan(str(body.get("path", ""))))}
    if result.ok:
        return jsonify(payload)
    payload["error"] = {"code": result.reason, "message": result.message}
    status = {table_bring.REFUSE_EXISTS: 409,
              table_bring.REFUSE_NOTHING: 400}.get(result.reason, 422)
    return jsonify(payload), status


@bp.post("/api/settings/admin-auth")
def admin_auth():
    """管理者認証(マスタ編集・表を持ってくる・中身を入れ替える の関門)。

    以前は資材選択の口(`/api/selection/auth`)を使っていた。資材選択は
    **現場モードの権限がある端末にしか登録されない**ので、資材モードだけの
    端末(倉庫)では 404 になり、マスタを直す役目の端末がパスワードを通せ
    なかった(通しの試験で見つけた)。設定画面はどのモードにもあるので、
    ここに口を置く。状態は同じもの(`selection_session` の admin)を使う。
    """
    from packaging_tool import selection_session
    body = request.get_json(silent=True) or {}
    session = selection_session.get_session(get_db())
    result = session.authenticate(str(body.get("password", "")))
    from packaging_tool import auth_state
    payload = {"admin": {"authenticated": bool(session.admin)}, "message": result.message,
               # 画面はこれを見て、認証に関わるところをその場で描き直す
               "auth": auth_state.snapshot()}
    if result.ok:
        return jsonify(payload)
    return jsonify({**payload, "error": {"code": "denied", "message": result.message}}), 422


@bp.post("/api/settings/table-refresh")
def table_refresh_run():
    """もうある表を Access の最新にする(入れ替える・作り直す)。`{"path":…, "tables":[…]}`

    Access をまるごと変換して差し替えると、このツールが共有に足した表・列・
    行が消える。選んだ表の中身だけを入れ替える(`table_bring.refresh`)。
    関門は「表を持ってくる」と同じ(管理者パスワード + マスタを直せる権限)。
    """
    from packaging_tool import master_admin, selection_session, table_bring
    conn = get_db()
    if not selection_session.get_session(conn).admin:
        return jsonify(_error("not_allowed", "管理者認証が必要です。"
                              "「パスワード」の面で認証してください。")), 403
    allowed, why = master_admin.can_edit(conn, "")
    if not allowed:
        return jsonify(_error("not_allowed", why)), 403
    body = request.get_json(silent=True) or {}
    tables = body.get("tables")
    if not isinstance(tables, list):
        return jsonify(_error("bad_tables", "入れ替える表の指定が正しくありません")), 400
    result = table_bring.refresh(conn, str(body.get("path", "")), [str(t) for t in tables])
    payload = {"ok": result.ok, "message": result.message,
               "refreshed": [{"name": n, "before": b, "after": a}
                             for n, b, a in result.refreshed],
               "kept_old": [{"name": n, "old": o} for n, o in result.kept_old],
               "backup": result.backup,
               "plan": table_bring.plan_dict(table_bring.plan(str(body.get("path", ""))))}
    if result.ok:
        return jsonify(payload)
    payload["error"] = {"code": result.reason, "message": result.message}
    status = {table_bring.REFUSE_NOTHING: 400}.get(result.reason, 422)
    return jsonify(payload), status


@bp.post("/api/settings/write-back")
def start_write_back():
    return _start("write_back", "取り込み元へ反映",
                  lambda _p: _with_conn(data_sync.write_back))


@bp.post("/api/settings/recompute")
def start_recompute():
    """パレットの適合範囲を基準表から計算し直す(VBA `RunUpdatePalletAll`)。"""
    from packaging_tool import pallet_service
    return _start("recompute", "適合範囲の再計算",
                  lambda _p: _with_conn(pallet_service.recompute_fit_ranges))


def _start(kind: str, label: str, work):
    try:
        job = jobs.get_registry().start(kind, label, work)
    except jobs.JobBusy as busy:
        # 409。同時に走らせると同じDBを2か所から書くことになる
        return jsonify({
            "error": {"code": "busy",
                      "message": f"{busy.running.label}を実行中です。"
                                 "終わってからもう一度押してください"},
            "job": settings_presenter.job_dict(busy.running),
        }), 409
    return jsonify({"job": settings_presenter.job_dict(job)}), 202


def _with_conn(func):
    """作業スレッド用に別接続を開いて処理を回す。

    `sqlite3` の接続はスレッドをまたげない(`check_same_thread=True`)。
    リクエストの接続(`get_db()`)はこのスレッドのものではないので、
    ここで開き直す ── tkinter版 `DataView._with_conn` と同じ理由。
    """
    with db.connect() as conn:
        db.apply_schema(conn)
        return func(conn)


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
