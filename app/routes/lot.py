"""ロット検索 (旧 `CreatePage1` = `mpMain` の1ページ目)

作業の出発点。**入口は検索欄1つ**で、絞り込んだ一覧から行を開くと
ロット情報・引当情報・受注情報がモーダルに出る。

【入口を1つにした理由】
以前は「一覧を検索する欄」と「ロット番号を打つ欄」が別にあった。
番号を知っているときにどちらへ打てばよいかを利用者が判断しなければ
ならず、しかも番号の欄は一覧の**下**にあって、打つ場所と結果が
上下に離れていた。いまは検索欄に打ち、**1件に絞れた時点で自動で
確定する** ── 7桁を打てば必ず1件になるので、速い道は塞いでいない。

【結果はモーダルに集める】
以前は結果が一覧の下へ縦に伸びていて、打つ→スクロールして読む→
戻って打ち直す、を繰り返していた。入力(上)と結果(下)を行き来
させないため、結果は前面に重ねて出し、閉じれば元の一覧に戻る。

【VBAから引き継ぐ挙動】
- 入力は半角・大文字にそろえる(全角や小文字で打たれても通るように)
- 表示の並びは ロット情報 → 引当情報 → 受注情報(フレーム順)
- BOXコースのときは板厚・板幅・板丈が**BOX実績の値に差し替わる**ので、
  見出しに「【BOX実績寸法】」を出し、該当項目を赤くする
- 引当情報は引当番号の昇順
- EX受注なら見出しに `EX` を付ける
"""
from __future__ import annotations

from typing import Optional

from flask import Blueprint, jsonify, render_template, request

from packaging_tool import (lot_browse_session, lot_query, lot_service, modes,
                            selection_session, spec_sheet, work_context)
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import lot as lot_presenter
from packaging_tool.presenters import lot_list as list_presenter

from .. import current_mode, get_db
from ..shell import shell_context

bp = Blueprint("lot", __name__)

log = get_logger("app.routes.lot")

# 資材モードで資材展開を出さない理由。**画面にも応答にも同じ文を使う**
# ── 2か所で言葉が育つと、断りの理由と画面の説明が食い違う
EXPAND_ABSENT_WHY = (
    "資材展開は現場モードの操作です。"
    "ここでは届いた発注のロット情報を確かめられます。")


@bp.get("/lot")
def page():
    """ロット検索の画面。

    `?lot=1234567` を付けて開くと、その行を**開いた状態で**出す。
    発注一覧から「このLotを開く」で来るときに使う ── 番号を目で読んで
    打ち直させると、7桁の打ち間違いがそのまま別のロットを開く。
    """
    conn = get_db()
    available = lot_presenter.has_data(conn)
    return render_template(
        "lot.html",
        available=available,
        # 開いた直後に開く行。空なら何も開かない
        open_lot=request.args.get("lot", "").strip(),
        # 初回描画のぶんはサーバが埋める。空の表が一瞬出ると
        # 「データが無い」ように見える
        list_view=_list_view(conn, available),
        columns=lot_query.COLUMNS,
        # 未取り込みなら、空の画面を出す前に理由を伝える
        unavailable_message=(
            "" if available else
            "仕掛台帳が未取り込みです。設定画面から取り込んでください。"),
        # **資材展開は現場の操作。** 資材モードには出さない
        # (`EXPAND_ABSENT_WHY` に理由がある)
        can_expand_here=current_mode() == modes.FIELD,
        expand_absent_why=EXPAND_ABSENT_WHY,
        lot_url=lot_presenter.URL_LOT_DISPLAY,
        # 図面が取れないときに、いままでどおり閲覧システムを開けるように
        url_hoso=lot_presenter.URL_HOSO_SHIYOSHO,
        # 予定ロット(この端末のラインに入ったロット。VER4.8.85)。初回はサーバが埋める
        plans=_plans_state(conn),
        **shell_context("lot"),
    )


# ==================================================================
# 予定ロット(VER4.8.85)── 長が作業ラインに入れたロットの知らせ
# ==================================================================
def _plans_state(conn, *, refresh: bool = False) -> dict:
    """この端末のライン(アクセス権限の権限の列に書いたライン)に入った予定ロット。未確認を先に。

    `refresh` なら、共有が変わっていれば予定ロットだけ取り込み直してから出す(見張り)。
    """
    from packaging_tool import access_control, lot_plans
    if refresh:
        try:
            lot_plans.refresh_if_changed(conn)
        except Exception as exc:                  # noqa: BLE001 - 見張りで画面を落とさない
            log.warning("予定ロットを取り込み直せませんでした: %s", exc)
    identity = access_control.current_identity()
    state = access_control.line_state(conn, identity)
    lines = list(state.lines)
    items = [p.to_dict() for p in lot_plans.for_lines(conn, lines)]
    if lines:
        why = ""
    elif state.leader:
        # 作業長の端末(ラインなし)。「未登録」と出すと、読んでいないように見える(VER4.8.90)
        why = ("作業長の端末です(ラインは持っていません)。作業予定ラインで入れたロットは、"
               "そのラインの端末に届きます。この端末にも届けたいときは、この端末の行の「権限」に"
               "ラインを書きます(すべてなら Administrator)。")
    else:
        why = (f"この端末({identity.label()})の作業ラインが登録されていません。"
               f"{access_control.TABLE} のこの端末の行の「権限」にラインを正規の呼び名で書くと"
               "(L-1 / LVC / HVC / 機側 / AIM / コイル / トット / バランサー / NS1。L1・LS は読みません)、"
               "そのラインに入った予定ロットがここに届きます。" + state.near_text())
    return {
        "lines": lines,
        # いまの状態(強いもの優先: Administrator > 作業長 > ライン > 未登録。VER4.8.90)
        "label": state.label(),
        "role": state.role,
        "items": items,
        "unseen": sum(1 for i in items if not i["seen"]),
        "days": lot_plans.LIST_DAYS,
        # ラインが無いときに出す一文(どこに何を書けば届くか・当たっていない似た行)
        "why": why,
    }


@bp.get("/api/lot/plans")
def plans():
    """予定ロット。`?refresh=1` なら共有が変わっていれば取り込み直す(ロット検索の見張り)。"""
    return jsonify(_plans_state(get_db(), refresh=request.args.get("refresh") == "1"))


@bp.post("/api/lot/plans/<plan_id>/seen")
def plan_seen(plan_id: str):
    """知らせを確認した(消す)。`{"opened": true}` なら開いて確認した。"""
    from packaging_tool import lot_plans
    body = request.get_json(silent=True) or {}
    conn = get_db()
    if not lot_plans.mark_seen(conn, plan_id, opened=bool(body.get("opened"))):
        return jsonify({"error": {"code": "not_found",
                                  "message": "その予定ロットは見つかりません。画面を更新してください。"}}), 404
    return jsonify(_plans_state(conn))


@bp.post("/api/lot/plans/<plan_id>/done")
def plan_done(plan_id: str):
    """作業済みにする(VER4.8.109)。共有へ送るので、そのラインのほかの端末の予定欄からも消える。"""
    from packaging_tool import access_control, data_sync, lot_plans, warehouse_service
    conn = get_db()
    identity = access_control.current_identity()
    state = access_control.line_state(conn, identity)
    result = lot_plans.finish(conn, plan_id, lines=state.lines, leader=state.leader,
                              terminal=warehouse_service.this_terminal(), sender=identity.login_id)
    if not result.ok:
        code = 403 if result.reason == lot_plans.REFUSE_NOT_MINE else 409
        return jsonify({"error": {"code": result.reason, "message": result.message}}), code
    if result.changed:
        data_sync.write_back_in_background()
    return jsonify({**_plans_state(conn), "message": result.message})


#: 予定ロットを開く手順(画面は1つずつ呼んで進み具合を出す。VER4.8.93)
PLAN_STEPS = ("open", "orders", "pallet")


@bp.post("/api/lot/plans/start")
def plan_start():
    """予定ロットを「開く」(VER4.8.92)。**発注したパレットを決めて、ボード選定へ**。

    現場の声:「予定に届いたロットを開いた場合に、倉庫連携で発注したパレットサイズを検索して
    パレットが決定した状態でボード選定まで移行させてください」

    `step` で1つずつ進める(画面が進み具合を出すため。VER4.8.93)。省くと全部を1回で。

        open   … ロットを開く(作業中のロットになる)・資材展開(製造板幅・板丈 → 製品サイズ)
        orders … 共有から発注を取り込み直す(作業長の発注を見るため)
        pallet … このロットに発注したパレットを引き(`lot_plans.ordered_pallet`)、パレット決定と
                 同じ手順で決める(`selection.decide_pallet`)→ 資材選択へ(ボードの段が開く)

    移れないとき(ロットが無い・資材モード・板の寸法が無い・発注したパレットが無い)は、
    今までどおり**ロット情報を開く**(`next` が空。ロット情報を返す)。理由は `message` に出す。
    途中の手順は `{"next_step": 次の手順}` を返す。
    """
    body = request.get_json(silent=True) or {}
    lot_no = lot_presenter.normalize(str(body.get("lot_no", "")))
    step = str(body.get("step") or "all")
    if step not in PLAN_STEPS and step != "all":
        return jsonify({"error": {"code": "bad_step", "message": f"知らない手順です: {step}"}}), 400
    conn = get_db()
    if step in ("all", "open"):
        done = _plan_open(conn, lot_no)
        if done is not None:
            return done
        if step == "open":
            return jsonify({"ok": True, "next_step": "orders"})
    if step in ("all", "orders"):
        _plan_refresh_orders(conn)
        if step == "orders":
            return jsonify({"ok": True, "next_step": "pallet"})
    # 手順を分けて呼ばれたときは、作業中のロットが違っていれば開き直す(ほかの画面で別のロットを開いた等)
    if work_context.get_context().lot_no != lot_no or not work_context.get_context().expanded:
        done = _plan_open(conn, lot_no)
        if done is not None:
            return done
    return _plan_pallet(conn, lot_no)


def _plan_stay(conn, lot_no: str, message: str = "", detail: Optional[dict] = None):
    """移らずにロット情報を開く応答(今までの「開く」と同じ中身)。引いてあればそれを使う。"""
    detail = detail if detail is not None else _select(conn, lot_no)
    detail["ribbon"] = work_context.get_context().ribbon()
    detail["list"] = _list_view(conn, True)
    return jsonify({**detail, "next": "", "message": message})


def _plan_open(conn, lot_no: str):
    """ロットを開いて資材展開する。ここで終わるとき(移らない・1P0113)は応答、続けるなら None。"""
    detail = _select(conn, lot_no)
    if current_mode() != modes.FIELD or not detail.get("found"):
        return _plan_stay(conn, lot_no, detail=detail)
    context = work_context.get_context()
    if not context.expand_materials():
        return _plan_stay(conn, lot_no, "製造板幅・板丈が取得できていないので、資材選択へは移りません"
                                        "(ロット情報を開きました)。", detail=detail)
    session = selection_session.get_session(conn)
    if session.presenter.mode_1p0113:
        # 裸梱包はパレットを使わない(展開したら資材を引き直す。資材展開と同じ)
        session.reload_1p0113_materials()
        return _go(f"資材展開: Lot {context.lot_no}(裸梱包 1P0113 なのでパレットは使いません)", context)
    return None


def _plan_refresh_orders(conn) -> None:
    """ほかの端末(作業長)の発注を見るので、共有から取り込み直す。読めなくても手元の分で続ける。"""
    from packaging_tool import data_sync
    try:
        got = data_sync.refresh_orders(conn)
        for message in got.errors:
            log.warning("予定ロットを開く前の発注の取り込み直し: %s", message)
    except Exception as exc:                      # noqa: BLE001 - 手元の分で続ける
        log.warning("予定ロットを開く前に発注を取り込み直せませんでした: %s", exc)


def _plan_pallet(conn, lot_no: str):
    """発注したパレットで決めて資材選択へ。無ければロット情報を開く。"""
    from packaging_tool import lot_plans
    from . import selection as selection_routes
    context = work_context.get_context()
    session = selection_session.get_session(conn)
    pallet, why = lot_plans.ordered_pallet(conn, lot_no)
    if pallet is None:
        return _plan_stay(conn, lot_no, f"{why} ロット情報を開きました(資材展開してパレットを選んでください)。")

    if pallet.size_only:
        decided = selection_routes.decide_pallet_size(session, pallet.width, pallet.length, {})
    else:
        decided = selection_routes.decide_pallet(
            session, pallet.width, pallet.length, pallet.symbol, pallet.row_id, {})
    source = (f"発注したパレット {pallet.hinmei or f'{pallet.width}x{pallet.length}'}"
              + (f"(発注コード {pallet.code})" if pallet.code and not pallet.size_only else ""))
    if not decided.ok:
        return _go(f"{source} を決められませんでした: {decided.message}", context, warn=True)
    notes = []
    if pallet.size_only:
        notes.append("パレットマスタに無い寸法なので、寸法だけ決めました(Lot印刷・倉庫送信には一覧から行を選んでください)")
    elif pallet.by_name:
        notes.append("発注コードでは引けなかったので、品名の寸法で選びました")
    if not decided.product_set:
        notes.append("製品サイズは載るか確かめられなかったので、資材選択で確かめてください")
    message = f"{source} で決めました: {decided.message}" + "".join(f" / {n}" for n in notes)
    log.info("予定ロットを開いた: Lot %s %s", context.lot_no, message)
    return _go(message, context, warn=bool(notes))


def _go(message: str, context, *, warn: bool = False):
    """資材選択へ移る応答(資材展開と同じ形)。"""
    return jsonify({"ok": True, "next": "/selection", "message": message, "warn": warn,
                    "ribbon": context.ribbon()})


# ==================================================================
# 一覧(絞り込み)
# ==================================================================
def _list_view(conn, available: bool) -> dict:
    """一覧の画面ぜんぶ。**どの操作でもこれを返す**(設計書 §1 の2番)。"""
    session = lot_browse_session.get_session()
    view = list_presenter.build(
        session, conn, available=available,
        current_lot=work_context.get_context().lot_no)
    return view.to_dict()


def _list_state(message: str = "", status: int = 200):
    """一覧を返す。**1件に絞れても、ここでは開かない(作業中のロットにもしない)。**

    以前は1件になった時点で確定して詳細まで返していた。7桁目を打つ前に
    詳細へ移ってしまい、「有るかどうかだけ見たい」ができなかった(現場の声:
    「どんどん絞り込むのはいいが勝手に詳細に移行しないように」)。しかも開くと
    作業中のロットが替わり、前のロットの製品サイズや倉庫送信の下書きが消える。
    開くのは行のダブルクリック / Enter と、検索欄で Enter(1件のとき)だけ。
    """
    conn = get_db()
    available = lot_presenter.has_data(conn)
    body = _list_view(conn, available)
    body["message"] = message
    return jsonify(body), status


def _list_apply(result):
    """操作1回の結果を応答にする(`routes/selection.py` と同じ約束)。

    断られた場合も**一覧ぜんぶを返す**(422)。押した拍子に表が消えると、
    断られたのか壊れたのか区別できない。400 は入力の形が違うときだけで、
    そのときサーバの状態は動いていない。
    """
    if result.ok:
        return _list_state(result.message)
    if result.reason == lot_query.REFUSE_BAD_INPUT:
        return jsonify({"error": {"code": result.reason,
                                  "message": result.message}}), 400
    if result.reason == lot_query.REFUSE_NOT_LISTED:
        return jsonify({"error": {"code": result.reason,
                                  "message": result.message}}), 400
    return _list_state(result.message, 422)


@bp.get("/api/lot/list")
def lot_list():
    return _list_state()


@bp.post("/api/lot/list/filter/add")
def lot_list_add():
    body = request.get_json(silent=True) or {}
    return _list_apply(lot_browse_session.get_session().add(
        str(body.get("column", "")), str(body.get("op", "")),
        body.get("value", "")))


@bp.post("/api/lot/list/filter/remove")
def lot_list_remove():
    body = request.get_json(silent=True) or {}
    try:
        index = int(body.get("index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": {"code": lot_query.REFUSE_BAD_INPUT,
                                  "message": "外す条件を指定してください。"}}), 400
    return _list_apply(lot_browse_session.get_session().remove(index))


@bp.post("/api/lot/list/filter/quick")
def lot_list_quick():
    """すぐ絞るボタン(板・コイル / 実績あり・なし)。押すたびに入る・外れる。"""
    body = request.get_json(silent=True) or {}
    return _list_apply(lot_browse_session.get_session().toggle_quick(
        str(body.get("name", ""))))


@bp.post("/api/lot/list/filter/clear")
def lot_list_clear():
    return _list_apply(lot_browse_session.get_session().clear())


@bp.post("/api/lot/list/search")
def lot_list_search():
    body = request.get_json(silent=True) or {}
    return _list_apply(
        lot_browse_session.get_session().set_text(str(body.get("text", ""))))


@bp.post("/api/lot/list/sort")
def lot_list_sort():
    body = request.get_json(silent=True) or {}
    return _list_apply(
        lot_browse_session.get_session().set_sort(str(body.get("column", ""))))


@bp.post("/api/lot/list/page-size")
def lot_list_page_size():
    body = request.get_json(silent=True) or {}
    return _list_apply(
        lot_browse_session.get_session().set_page_size(body.get("size")))


@bp.get("/api/lot/list/suggest")
def lot_list_suggest():
    """「検索して条件を追加」の候補。

    **候補は実データから作る** ── 0件になる条件を勧めても意味がない。
    ここは状態を変えないので GET。
    """
    session = lot_browse_session.get_session()
    items = lot_query.suggest(get_db(), request.args.get("q", ""),
                              session.saved())
    return jsonify({"items": [i.to_dict() for i in items]})


@bp.post("/api/lot/list/saved/save")
def lot_list_saved_save():
    body = request.get_json(silent=True) or {}
    return _list_apply(
        lot_browse_session.get_session().save_current(str(body.get("name", ""))))


@bp.post("/api/lot/list/saved/load")
def lot_list_saved_load():
    body = request.get_json(silent=True) or {}
    return _list_apply(
        lot_browse_session.get_session().load_saved(str(body.get("name", ""))))


@bp.post("/api/lot/list/saved/delete")
def lot_list_saved_delete():
    body = request.get_json(silent=True) or {}
    return _list_apply(
        lot_browse_session.get_session().delete_saved(str(body.get("name", ""))))


def _box_pick(lot_no: str, *, remember: bool) -> str:
    """BOX最終実績寸法の候補のどれを使うか。

    画面が `box_pick` を付けてきたらそれ(空なら「1つ目のまま」に戻す)。付けていなければ、
    そのロットで前に選んだもの。`remember` のときだけ覚える(見るだけの画面では覚えない)。
    """
    picks = work_context.get_context().box_picks
    if "box_pick" not in request.args:
        return picks.get(lot_no, "")
    pick = request.args.get("box_pick", "")
    if remember:
        if pick:
            picks[lot_no] = pick
        else:
            picks.pop(lot_no, None)
    return pick


def _select(conn, lot_no: str) -> dict:
    """1件引いて、**作業中のロットとして覚える**。

    リボンと、このあとの資材展開がこれを見る(tkinter版が
    `SelectionPresenter` に入れていたのと同じ役割)。
    """
    lot_no = lot_presenter.normalize(lot_no)
    context = work_context.get_context()
    # 開き直したロットで前に選んだオーダーがあれば、受注情報も資材選択の特殊モードも
    # **そのオーダーで**決める(別のロットなら選んだオーダーは外れる)
    picked = context.picked_order if context.lot_no == lot_no else ""
    result = lot_service.search_lot(conn, lot_no, _box_pick(lot_no, remember=True),
                                    order_pick=picked)
    context.apply_lot(result)

    # 資材選択の状態機械にも通す(VBA `SearchAndDisplay` 末尾)。
    # EX受注・1P0113・プロテックはここで決まる ── 資材選択の画面を
    # 開いたときに引き直すのでは、**開くまでモードが分からない**
    #
    # **現場モードのときだけ。** 資材モードは届いた発注を確かめるだけで、
    # 資材選択へ移る道がない(資材展開は現場の操作)。行き先の無い準備を
    # しておく理由がないので、帯に出すところ(`apply_lot`)で止める
    if not result.found and current_mode() == modes.FIELD:
        # **見つからなければ、前のロットで決めたことも外す**(`WorkContext.forget_lot`)
        selection_session.get_session(conn).forget_lot()
    if result.found and current_mode() == modes.FIELD:
        session = selection_session.get_session(conn)
        # セッション側を通すのは、プロテック判定の結果として
        # **ボード種別を実際に切り替える**ため(種別の一覧を持つのは
        # セッション)。順序依存の判断そのものはプレゼンタが持つ
        session.apply_lot(
            result,
            # 状態機械が見るのは**入力欄の値**(`presenters/selection.py` 冒頭)
            product_width=context.product_width,
            product_length=context.product_length)

    view = lot_presenter.build(result)
    # 包装仕様書の図面を**この時点で**取りに行く。画面が「見たい」と
    # 言ってから始めると、そのぶん待たせることになる。
    # 取得は別スレッドなので、この応答は待たない
    if view.packaging_spec and spec_sheet.is_valid_no(view.packaging_spec):
        spec_sheet.get_fetcher().prefetch(view.packaging_spec)
    return lot_presenter.to_dict(view)


@bp.get("/api/lot/<lot_no>")
def search(lot_no: str):
    """1件開く(行のダブルクリック / Enter)。

    見つからない場合も 200 で返し、`found: false` と理由を載せる。
    「その番号が無い」は通信の失敗ではなく、正しい検索結果なので。
    """
    conn = get_db()
    body = _select(conn, lot_no)
    # 画面を移らずにリボンを直せるよう、一緒に返す
    body["ribbon"] = work_context.get_context().ribbon()
    # **どの行が作業中かはサーバが決める。** 画面が別に覚えると、
    # 別のロットを引いたときに前の行が光ったまま残る
    body["list"] = _list_view(conn, True)
    return jsonify(body)


@bp.get("/api/lot/<lot_no>/peek")
def peek(lot_no: str):
    """**見るだけ**。1件引いて返すが、作業中のロットにはしない。

    発注一覧の「Lotを開く」が使う。そこでやりたいのは「この発注は
    どんなロットだったか」を確かめることだけで、いま組み立てている
    作業を横取りしたいわけではない(現場の指摘:「発注情報を見て
    どんなロットだっけ? だけの話」)。

    `/api/lot/<lot_no>` との違いはそこ1点 ── あちらは開くことが
    「選ぶ」ことでもあるので、帯も資材選択の状態機械も動く。
    """
    lot_no = lot_presenter.normalize(lot_no)
    result = lot_service.search_lot(get_db(), lot_no, _box_pick(lot_no, remember=False))
    view = lot_presenter.build(result)
    # 図面だけは先に取りに行く。見に行くのは読むだけの操作で、
    # 待たせる理由が無い(`_select` と同じ理由)
    if view.packaging_spec and spec_sheet.is_valid_no(view.packaging_spec):
        spec_sheet.get_fetcher().prefetch(view.packaging_spec)
    return jsonify(lot_presenter.to_dict(view))


def _apply_picked_order(conn, lot_no: str, order_no: str) -> None:
    """選んだオーダーで、資材選択の特殊モード(EX・1P0113・プロテック・1P1185・包装材)を決め直す。

    VBA `Page1_OnLstHikiClick` は引当行のクリックでモードを決め直していた。以前の Python 版は
    受注情報の表示だけを差し替えていたので、1ロットに複数オーダーがあって特殊オーダーを
    選んでも、資材選択は先頭のオーダー(受注番号のいちばん小さいもの)のモードのまま開いた。

    **別のオーダーに選び直したら、決めたパレット・蓋・倉庫連携の下書きを外す**(VER4.8.37)。
    同じロットでもオーダーごとに資材が違うことがあり、前のオーダーで決めたパレットのまま次のオーダーを
    発注してしまう(EXの台が普通のオーダーで送られる、など)。製品サイズの入力値は残す。
    同じオーダーを押し直しただけなら外さない。
    """
    result = lot_service.search_lot(conn, lot_no, _box_pick(lot_no, remember=False),
                                    order_pick=order_no)
    if not result.found:
        return
    context = work_context.get_context()
    session = selection_session.get_session(conn)
    before = session.presenter.lot_result
    changed = (before is not None and before.lot.lot_no == lot_no
               and before.odr.order_no != result.odr.order_no)
    context.apply_lot(result)
    session.apply_lot(
        result, product_width=context.product_width, product_length=context.product_length)
    if changed:
        session.forget_pallet(f"オーダーを {before.odr.order_no} から {result.odr.order_no} に選び直した")
        context.drop_pending_orders()


@bp.get("/api/lot/<lot_no>/hiki/<hiki_no>")
def odr_for_hiki_row(lot_no: str, hiki_no: str):
    """引当情報の行をクリックしたら、受注情報をその行のものに差し替える

    (VBA `Page1_OnLstHikiClick`)。

    現場の声:「ロット情報画面の引当情報をクリックしても受注内容が
    切り替わっているように見えない、できていないのではないか」→
    「引当情報クリックでオーダー情報切り替えですよ」。

    データの流れ: SIKALOT から LOTNO で1行 → SIKAHIKI で同じ
    LOTNO の行の中から**引当NO**で1行(=引当NOとオーダーNOが決まる)
    → SIKAODR で同じオーダーNOの行を展開。URLも引当NOで引く
    (受注番号ではない) ── 主語は押された「引当行」そのもの。

    1ロットに複数の受注番号がまたがることがあり、受注情報欄は
    そのうち1件しか出せない(`lot_service._load_odr` のdocstring
    参照)。以前はこの切り替え経路自体が無く、常に先頭1件のままだった。

    **ロット情報・引当一覧そのもの・図面には触らない。** そこまで
    作り直すと重いだけでなく、入力中の値が再描画で消える。差し替える
    のは受注情報の断片だけ。
    """
    conn = get_db()
    lot_no = lot_presenter.normalize(lot_no)
    found = lot_service.load_odr_for_hiki_row(conn, lot_no, hiki_no)
    if found is None:
        return jsonify({
            "error": {"code": "not_found",
                     "message": f"引当NO {hiki_no} はロット {lot_no} に"
                                "ありません。"}}), 404

    odr, hiki, lot = found
    # **ロット検索の画面で押したときだけ**、選んだオーダーとして覚える(倉庫連携の納入先と、
    # 発注に含めるオーダーの既定になる)。発注一覧の「Lotを開く」(見るだけ)では覚えない
    context = work_context.get_context()
    picked = (request.args.get("pick") == "1" and current_mode() == modes.FIELD
              and context.pick_order(lot_no, odr.order_no))
    body = lot_presenter.build_odr_switch(odr, lot)
    body["picked"] = bool(picked)
    if picked:
        _apply_picked_order(conn, lot_no, odr.order_no)
        body["ribbon"] = context.ribbon()
    # 引当一覧のどの行が選ばれているかも塗り直す(サーバが決める。
    # 画面が別に覚えると、別のロットを引いたときに前の選択が残る)
    body["hiki"] = [
        {"order_no": row.order_no, "hiki_no": row.hiki_no,
         "selected": row.hiki_no == hiki_no}
        for row in hiki
    ]
    return jsonify(body)


@bp.post("/api/lot/expand")
def expand():
    """資材展開(VBA `Page1_OnBtnHBClick`)。

    製造板幅・板丈を製品サイズとして覚え、資材選択へ送る。
    **「セット」までは押さない**のがVBAの挙動で、パレットに収まるかの
    検証(回転の要否を含む)は利用者が自分で行う。

    **現場モードのときだけ。** 画面にも出していないが、要求そのものも
    断る ── 守りは1枚ではない(倉庫連携の確認・取消と同じ形)。
    """
    if current_mode() != modes.FIELD:
        log.info("現場モードではないため断りました: %s", request.path)
        return jsonify({"error": {
            "code": "wrong_mode", "message": EXPAND_ABSENT_WHY}}), 403

    context = work_context.get_context()
    if not context.expand_materials():
        return jsonify({"error": {
            "code": "cannot_expand",
            "message": ("先にロットを検索してください"
                        if not context.lot_no
                        else "製造板幅・板丈が取得できていません")}}), 422

    # 1P0113(裸梱包)は製品サイズが**資材そのもの**を決めるので、
    # 展開したらその場で引き直す(tkinter版 `expand_materials` 末尾の
    # `if self.mode_1p0113: self.load_1p0113_materials()`)。
    #
    # 呼ばないと行き止まりになります ── 1P0113 中はパレットカードごと
    # 隠れていて「セット」が押せないため、展開した製品サイズを資材へ
    # 反映させる操作が画面上に1つも無くなり、角材・松板が
    # 「製品ｻｲｽﾞ未設定」のまま倉庫送信まで断られ続けます
    session = selection_session.get_session(get_db())
    if session.presenter.mode_1p0113:
        session.reload_1p0113_materials()

    return jsonify({
        "ok": True,
        "next": "/selection",
        "message": (f"資材展開: Lot {context.lot_no} 製品サイズ "
                    f"{context.product_width}×{context.product_length}"),
        "ribbon": context.ribbon(),
    })

