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

from flask import Blueprint, jsonify, render_template, request

from packaging_tool import data_sync, db, jobs, source_db
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import settings as settings_presenter
from packaging_tool.presenters import fs_browse
from packaging_tool.presenters import master as master_presenter

from .. import get_db
from ..shell import shell_context

log = get_logger("app.routes.settings")

bp = Blueprint("settings", __name__)


@bp.get("/settings")
def page():
    view = settings_presenter.build(get_db(), _startup_modes())
    registry = jobs.get_registry()
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
        default_tab=settings_presenter.DEFAULT_TAB,
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
    """**起動したときに**使えたモード。

    使える画面(登録するURL)は起動時の権限で決まる。あとから権限が
    増えても増えないので、食い違ったら設定画面がそう言う
    (`presenters/settings._access_section`)。
    """
    from flask import current_app
    grant = current_app.config.get("STARTUP_GRANT")
    return grant.allowed_modes() if grant is not None else ()


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
        auto_import=bool(body["auto_import"]) if "auto_import" in body else None,
        spec_url=pick("spec_sheet_url"),
        position=pick("position"))
    if not result.ok:
        # 入力の形の誤り。**サーバの状態は動いていない**
        return jsonify(_error(result.reason, result.message)), 400

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
    view = fs_browse.browse(request.args.get("path", ""))
    return jsonify(fs_browse.to_dict(view))


@bp.post("/api/settings/import")
def start_import():
    """`{"target": "all" | "master" | "lot"}`。"""
    target = (request.get_json(silent=True) or {}).get("target", "all")
    if target not in settings_presenter.TARGET_KEYS:
        return jsonify(_error("bad_target", "取り込む対象が正しくありません")), 400

    label = next(name for key, name, _ in settings_presenter.TARGETS if key == target)
    work = {
        "all": lambda c, p: data_sync.import_all(c, progress=p),
        "master": lambda c, p: data_sync.import_master(c, progress=p),
        "lot": lambda c, p: data_sync.import_lot_ledger(c, progress=p),
    }[target]
    return _start("import", label, lambda p: _with_conn(lambda c: work(c, p)))


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
