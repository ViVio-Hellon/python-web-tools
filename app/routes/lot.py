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

from flask import Blueprint, jsonify, render_template, request

from packaging_tool import (lot_browse_session, lot_query, lot_service,
                            selection_session, spec_sheet, work_context)
from packaging_tool.presenters import lot as lot_presenter
from packaging_tool.presenters import lot_list as list_presenter

from .. import get_db
from ..shell import shell_context

bp = Blueprint("lot", __name__)


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
        lot_url=lot_presenter.URL_LOT_DISPLAY,
        # 図面が取れないときに、いままでどおり閲覧システムを開けるように
        url_hoso=lot_presenter.URL_HOSO_SHIYOSHO,
        **shell_context("lot"),
    )


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


def _list_state(message: str = "", status: int = 200, *, auto: bool = True):
    """一覧を返す。**1件に絞れていたらその場で確定する。**

    「絞り込んだ結果が1つになったら、それが答え」という扱い。番号を
    打つ専用の欄を無くしたぶん、ここが速い道を引き受ける。
    確定すると詳細(`detail`)も一緒に返るので、画面は追加の往復なしに
    そのまま開ける。

    `auto` を切るのは、既に1件検索を済ませた直後だけ ── そこで
    もう一度引くと同じ問い合わせを2回することになる。
    """
    conn = get_db()
    available = lot_presenter.has_data(conn)
    detail = None
    if available and auto:
        # **規則はこれだけ: 1件になったら開く。** 同じロットかどうかで
        # 分けたくなるが、そうすると「打ったのに開かないことがある」に
        # なり、利用者が規則を言葉にできなくなる
        only = lot_browse_session.get_session().only_lot(conn)
        if only:
            detail = _select(conn, only)

    body = _list_view(conn, available)
    body["message"] = message
    if detail is not None:
        body["detail"] = detail
        body["ribbon"] = work_context.get_context().ribbon()
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


def _select(conn, lot_no: str) -> dict:
    """1件引いて、**作業中のロットとして覚える**。

    リボンと、このあとの資材展開がこれを見る(tkinter版が
    `SelectionPresenter` に入れていたのと同じ役割)。
    """
    result = lot_service.search_lot(conn, lot_presenter.normalize(lot_no))
    context = work_context.get_context()
    context.apply_lot(result)

    # 資材選択の状態機械にも通す(VBA `SearchAndDisplay` 末尾)。
    # EX受注・1P0113・プロテックはここで決まる ── 資材選択の画面を
    # 開いたときに引き直すのでは、**開くまでモードが分からない**
    if result.found:
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
    body = lot_presenter.build_odr_switch(odr, lot)
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
    """
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

