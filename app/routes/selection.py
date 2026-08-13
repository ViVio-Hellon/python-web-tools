"""資材選択 — パレット・製品サイズ・ボード選定 (旧 `CreatePage2`)

ロットで決まった製品を、どのパレットに、どの向きで載せるか。
それが決まったら、どのボードを何枚使うかを決める。

【VBAから引き継ぐ挙動】
- 一覧は**単位フィルタ**が既定(単位=台。保護材が確定していれば台・組)。
  「EXまで表示」で解除、「EXオンリー」は**EX受注のときだけ**効く
- 「パレット検索」は製品サイズが入っていれば自動選定、
  空なら入力したパレット寸法の ±50mm 検索(直接検索モード)
- 自動選定で回転が起きたら、製品サイズの幅・丈を**入れ替えて返す**
- 「セット」はパレットに収まるかを確かめる。収まらず回転すれば収まる
  場合は自動で入れ替える
- 「セットクリア」でパレット・製品サイズを両方とも未設定に戻す
- 「疲労度優先」「在庫考慮」は押しっぱなしのモードで、ロットを
  切り替えても解除しない
- 保護材がアングル以外に確定していれば**上用の欄とアングルの欄は無い**
  (上下共用モード)

【HTTPの使い分け(設計書 §6.1)】
- 400 … 数字でない・一覧に無いものを指した ── **入力の形**の誤り
- 422 … 形は正しいが**業務として断る**(サイズが未確定、候補が無い)
- 500 … 選定そのものが落ちた(理由を出して画面は保つ)

どのエンドポイントも**更新後のビューモデル一式**を返す(設計書 §3.3)。
差分を返すと、画面が「押した結果どうなったか」を組み立て直すことになる。
"""
from __future__ import annotations

from typing import Optional

from flask import Blueprint, Response, jsonify, render_template, request

from packaging_tool import board_selection_service as svc
from packaging_tool import printing, selection_session, work_context
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import outputs
from packaging_tool.presenters import selection as presenter

from .. import get_db
from ..shell import shell_context

log = get_logger("app.routes.selection")

bp = Blueprint("selection", __name__)


def _session():
    return selection_session.get_session(get_db())


def _state(session, **extra):
    """いまの画面ぜんぶ。**どの操作の後も同じものを返す**。

    「押した結果どうなったか」を画面が組み立て直さずに済む。
    tkinter版はウィジェットを個別に触っていたが、その方式は
    触り忘れた欄が古い値のまま残る(実際に何度か起きた)。
    """
    view = presenter.build_view(session, available=presenter.has_data(get_db()))
    body = presenter.to_dict(view)
    body["ribbon"] = work_context.get_context().ribbon()
    body.update(extra)
    return jsonify(body)


@bp.get("/selection")
def page():
    session = _session()
    view = presenter.build_view(session, available=presenter.has_data(get_db()))
    return render_template(
        "selection.html",
        view=view,
        columns=presenter.PALLET_COLUMNS,
        board_columns=presenter.BOARD_COLUMNS,
        selected_columns=presenter.SELECTED_COLUMNS,
        pending=presenter.PENDING_PARTS,
        # 初回の描画をJSに任せない。開いた瞬間に読める
        state=presenter.to_dict(view),
        **shell_context("selection"),
    )


@bp.get("/api/selection/state")
def state():
    return _state(_session())


# ------------------------------------------------------------------
# パレット
# ------------------------------------------------------------------
@bp.post("/api/selection/pallet/apply")
def apply_pallet():
    """「適用」(VBA `btnApplyPalette_Click`)。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    result = session.apply_pallet(str(body.get("width", "")),
                                  str(body.get("length", "")))
    if not result.ok:
        # 数値でない・0以下 ── どちらも入力の形の誤り
        return jsonify(_error("bad_size", result.message)), 400
    log.info("パレット適用: %s×%s", session.palette.width, session.palette.length)
    return _state(session, message=result.message)


@bp.post("/api/selection/pallet/pick")
def pick_pallet():
    """一覧の行に決める(VBA `_on_pallet_row_select` + `btnApplyPalette_Click`)。

    **押した時点で決まる。** 以前は「行を選ぶ」「適用」「セット」の3回で、
    しかも順番を間違えると断られていた。行を押すこと自体が
    「このパレットにする」という意思なので、そこまで済ませる。

    行そのものも覚える(VBA と同じ)── 発注コードと単位は行にしか無く、
    同じ寸法でも記号違いで別のコードになるので、倉庫送信と帳票が見る。
    """
    body = request.get_json(silent=True) or {}
    session = _session()
    width, length = _parse_int(body.get("width")), _parse_int(body.get("length"))
    if width is None or length is None:
        return jsonify(_error("bad_size", "一覧から行を選んでください。")), 400

    applied = session.apply_pallet(str(width), str(length))
    if not applied.ok:
        return jsonify(_error("bad_size", applied.message)), 400
    picked = session.pick_pallet_row(width, length, str(body.get("symbol", "")))
    if not picked.ok:
        return _apply(session, picked)

    # 製品サイズが分かっているなら、載るかどうかまで確かめて確定する。
    # **確かめる規則は変えていない**(`apply_product_size`)。押す回数を
    # 減らしただけで、載らないパレットを選べば今までどおり断られる
    note = _settle_product(session, body)
    return _state(session, message=f"{applied.message}{note}")


def _settle_product(session, body: dict) -> str:
    """パレットが決まった直後に、製品サイズを確定させる。

    製品サイズはロットから入っている(`資材展開`)ので、人が打ち直す
    ものではない。**パレットが決まって初めて「載るか」を確かめられる**
    ので、決まったこの場で確かめる。

    **画面が送ってきた値を先に見る。** 打ち直した直後は、まだこちらが
    知らない値が欄に入っている ── 覚えているほうだけを見ると、
    打ってから行を押した人だけ確定しない、という食い違いが出る。

    戻り値は画面に添える一言。確定できなかったとき(載らない・
    まだロットを引いていない)は空文字 ── パレットを選べたこと自体は
    成り立っているので、断りにはしない。理由は製品サイズの段が出す。
    """
    context = work_context.get_context()
    width = str(body.get("product_width", "")).strip()
    length = str(body.get("product_length", "")).strip()
    if not (width and length):
        width = str(context.product_width or "")
        length = str(context.product_length or "")
    if not (width and length):
        return ""
    result, rotated = session.apply_product(width, length)
    if not result.ok:
        return ""
    # 回転して載せると決まったら、覚えている向きもそろえる。
    # 二か所で別々に入れ替えると、どちらが本当か分からなくなる
    context.product_width = session.product.width
    context.product_length = session.product.length
    return f" / 製品 {session.product.width}×{session.product.length} を確定" + (
        "(回転)" if rotated else "")


@bp.post("/api/selection/pallet/list")
def list_pallets_live():
    """製品 幅・丈を**入力するたびに**呼ばれる、確定しない一覧の絞り込み。

    「パレット決定」を押すまでは何も確定しない ── ここでは
    `session.list_mode`/`live_product_*` を更新して一覧を引き直すだけで、
    パレットも製品サイズも一切セットしない(セットは行を選んでからの
    「パレット決定」の役目)。
    """
    body = request.get_json(silent=True) or {}
    session = _session()
    session.list_mode = selection_session.LIST_PRODUCT_LIVE
    session.live_product_width = str(body.get("product_width", "")).strip()
    session.live_product_length = str(body.get("product_length", "")).strip()
    return _state(session)


@bp.post("/api/selection/pallet/search")
def search_pallet():
    """「パレット検索」(VBA `btnAutoSelectPallet_Click`)。

    製品サイズが入っていれば自動選定、空ならパレット寸法の直接検索。
    **どちらの結果になったかは画面に出す** ── 同じボタンで別のことが
    起きるので、何が起きたか言わないと分からない。
    """
    body = request.get_json(silent=True) or {}
    session = _session()
    conn = get_db()
    context = work_context.get_context()

    product_width = str(body.get("product_width", "")).strip()
    product_length = str(body.get("product_length", "")).strip()

    if not product_width or not product_length:
        # 直接検索モード。条件だけ覚えて、一覧はビューモデルが引き直す
        session.list_mode = selection_session.LIST_DIRECT
        session.direct_width = str(body.get("pallet_width", "")).strip()
        session.direct_length = str(body.get("pallet_length", "")).strip()
        return _state(session, direct=True,
                      message="製品サイズが空なので、パレット寸法で直接検索しました")

    result = svc.auto_select_pallet(
        conn, product_width_text=product_width, product_length_text=product_length,
        two_stack=session.two_stack, show_all=session.show_all,
        ex_only=session.ex_only, is_ex_order=session.presenter.is_ex_order,
        last_hosozai=session.presenter.last_hosozai,
        manufactured_thickness=session.presenter.manufactured_thickness,
        user_log=session.presenter.user_log)

    # 一覧は**失敗しても**この製品サイズの候補だけに絞る。
    # 検索前の一覧が残ると「検索したのに何も変わらない」ように見える
    if _to_int(product_width) and _to_int(product_length):
        context.product_width = _to_int(product_width)
        context.product_length = _to_int(product_length)
        session.list_mode = selection_session.LIST_PRODUCT

    if not result.ok:
        # 形は正しいが、載るパレットが無い ── 業務としての断り
        return _state(session, message=result.message, found=False), 422

    session.apply_pallet(str(result.width), str(result.length))
    # 決まった行を**サーバにも覚えさせる**(tkinter版 `_select_pallet_row`)。
    # 発注コードと単位は一覧の行にしか無いので、覚えないと画面上は行が
    # 光っているのに倉庫送信が「行を選んでください」と断り、Lot印刷は
    # 業界・記号・発注コードが空欄で刷られる
    picked = session.pick_pallet_row(result.width, result.length, result.symbol)
    if not picked.ok:
        log.warning("自動選定した %s×%s %s を一覧から引けませんでした: %s",
                    result.width, result.length, result.symbol, picked.message)

    # **ここまで来たら製品サイズも確定させる。** 「載るパレットを探して
    # 見つかった」のだから、載ることはもう確かめてある。もう一度
    # 「セット」を押させるのは、同じ確認を2回やらせているだけ
    note = _settle_product(session, body)

    log.info("パレット自動選定: %s×%s (%s)",
             result.width, result.length, result.pass_label)
    return _state(
        session, found=True,
        message=f"{result.message}: {result.width}×{result.length}{note}",
        rotated=result.rotated,
        selected={"width": result.width, "length": result.length},
        thickness_warning=result.needs_thickness_warning)


# ------------------------------------------------------------------
# 製品サイズ
# ------------------------------------------------------------------
@bp.post("/api/selection/product/apply")
def apply_product():
    """「セット」(VBA `btnApplyProductSize_Click`)。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    width, length = str(body.get("width", "")), str(body.get("length", ""))

    result, rotated = session.apply_product(width, length)
    if not result.ok:
        # 断りの種類はサービスが付ける(`ApplyResult.reason`)。
        # 文言から推し量ると、文言を直した日に区別が壊れる
        bad_input = result.reason == svc.REFUSE_BAD_INPUT
        return jsonify(_error("bad_size" if bad_input else "cannot_set",
                              result.message)), 400 if bad_input else 422

    # 1P0113 中は製品サイズが資材そのものを決めるので引き直す
    # (VBA `btnApplyProductSize_Click` の 1P0113 分岐)
    if session.presenter.mode_1p0113:
        session.reload_1p0113_materials()

    log.info("製品サイズ確定: %s×%s (回転=%s)",
             session.product.width, session.product.length, rotated)
    return _state(session, message=result.message, rotated=rotated)


@bp.post("/api/selection/clear")
def clear_sizes():
    """「セットクリア」(VBA `btnClearPalProd_Click`)。"""
    session = _session()
    session.clear_sizes()
    work_context.get_context().clear()
    log.info("パレット・製品サイズをクリアしました")
    return _state(session, message="パレット・製品サイズを未設定に戻しました")


# ------------------------------------------------------------------
# 押しっぱなしのモード
# ------------------------------------------------------------------
@bp.post("/api/selection/toggle/<name>")
def toggle(name: str):
    """「EXまで表示」「EXオンリー」「2山積」。"""
    session = _session()
    try:
        now = session.toggle(name)
    except KeyError:
        return jsonify(_error("bad_toggle", f"知らない切り替えです: {name}")), 400

    # EXオンリーはEX受注のときだけ効く。効かない状態でONにされても、
    # 一覧は変わらない ── その旨を返して、押した意味が無いことを伝える
    if name == "ex_only" and now and not session.presenter.is_ex_order:
        return _state(session, message="EXオンリーはEX受注のロットでのみ効きます")
    return _state(session)


# ------------------------------------------------------------------
# 1P0113 裸梱包
# ------------------------------------------------------------------
@bp.post("/api/selection/1p0113/force")
def toggle_force_1p0113():
    """強制1P0113(VBA `lblForce1P0113_Click`)。

    包装仕様NOに関わらず裸梱包として扱う。**ロットを切り替えると
    解除される**ので、押しっぱなしのモード群とは別の窓口にしてある。
    """
    session = _session()
    return _apply(session, session.toggle_force_1p0113())


@bp.post("/api/selection/1p0113/qty")
def set_qty_1p0113():
    """乗数(VBA `spn1P0113Qty`)。角材本数・松板枚数にそのまま掛かる。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    qty = _parse_int(body.get("qty"))
    if qty is None:
        return jsonify(_error("bad_qty", "数量は数字で入力してください。",
                              field="qty")), 400
    return _apply(session, session.set_qty_1p0113(qty))


# ------------------------------------------------------------------
# ボード選定
# ------------------------------------------------------------------
@bp.post("/api/selection/board-type")
def set_board_type():
    """ボード種別の切り替え(VBA `cboBoardType`)。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    return _apply(session, session.set_board_type(str(body.get("board_type", ""))))


@bp.post("/api/selection/boards/auto-select")
def auto_select_boards():
    """「ボード選定」(VBA `btnAutoSelect_Click`)。"""
    session = _session()
    return _apply(session, session.auto_select_boards())


@bp.post("/api/selection/boards/add")
def add_board():
    """手動追加(VBA `btnAddBoardUpper/Lower_Click`)。

    枚数だけはここで数に直す。数でない文字は**入力の形**の誤りなので、
    業務としての断り(422)と混ぜない。
    """
    body = request.get_json(silent=True) or {}
    session = _session()
    count = _parse_int(body.get("count"))
    if count is None:
        return jsonify(_error("bad_count", "枚数は数字で入力してください。",
                              field="count")), 400
    width, length = _parse_int(body.get("width")), _parse_int(body.get("length"))
    if width is None or length is None:
        return jsonify(_error("bad_size", "ボードを一覧から選んでください。")), 400

    return _apply(session, session.add_board(
        str(body.get("category", "")), width, length, count))


@bp.post("/api/selection/boards/remove")
def remove_board():
    """選定済みから1行外す。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    index = _parse_int(body.get("index"))
    if index is None:
        return jsonify(_error("bad_index", "外す行を選んでください。")), 400
    return _apply(session, session.remove_board(str(body.get("category", "")), index))


@bp.post("/api/selection/boards/place")
def place_boards():
    """「ボード配置」(VBA `btnAutoPlace_Click`)。

    返すのは**描画計画**(矩形・色・キャプション)で、SVGそのものではない。
    描くのはブラウザで、拡大縮小は `viewBox` に任せる(設計書 §7.1)。
    """
    session = _session()
    return _apply(session, session.place_boards())


@bp.post("/api/selection/boards/clear")
def clear_boards():
    """「クリア」(VBA `btnClearAll_Click`)。"""
    session = _session()
    return _apply(session, session.clear_boards())


# ------------------------------------------------------------------
# アングル
# ------------------------------------------------------------------
@bp.post("/api/selection/angles/auto")
def auto_select_angles():
    """「アングル自動」(VBA `btnAngleAuto_Click`)。"""
    session = _session()
    return _apply(session, session.auto_select_angles())


@bp.post("/api/selection/angles/add")
def add_angle():
    """「アングル追加」(VBA `btnAngleAdd_Click`)。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    length = _parse_int(body.get("length"))
    if length is None:
        return jsonify(_error("bad_length", "追加するアングルを選んでください。")), 400
    return _apply(session, session.add_angle(length))


@bp.post("/api/selection/angles/remove")
def remove_angle():
    """「アングル削除」(VBA `btnAngleRemove_Click`)。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    index = _parse_int(body.get("index"))
    if index is None:
        return jsonify(_error("bad_index", "外すアングルを選んでください。")), 400
    return _apply(session, session.remove_angle(index))


@bp.post("/api/selection/angles/draw")
def draw_angles():
    """「アングル配置」(VBA `btnAngleDraw_Click`)。図を描くだけ。"""
    session = _session()
    return _apply(session, session.draw_angles())


# ------------------------------------------------------------------
# 管理者と実績パターン
# ------------------------------------------------------------------
@bp.post("/api/selection/auth")
def authenticate():
    """管理者認証(VBA `btnAuth_Click`)。

    **パスワードは応答に載せない。** 照合はサーバでしか行わず、
    画面へ返すのは「認証したか」の1ビットだけ(設計書 §3.6)。
    """
    body = request.get_json(silent=True) or {}
    session = _session()
    return _apply(session, session.authenticate(str(body.get("password", ""))))


@bp.post("/api/selection/pattern/save")
def save_pattern():
    """実績パターンの保存(VBA `btnSavePattern_Click`)。認証が要る。"""
    session = _session()
    return _apply(session, session.save_pattern())


@bp.post("/api/selection/pattern/load")
def load_pattern():
    """実績パターンの読み込み(VBA `LoadSinglePattern`)。"""
    body = request.get_json(silent=True) or {}
    session = _session()
    pattern_id = _parse_int(body.get("id"))
    if pattern_id is None:
        return jsonify(_error("bad_id", "読み込むパターンを選んでください。")), 400
    return _apply(session, session.load_pattern(pattern_id))


# ------------------------------------------------------------------
# 出す ── 帳票と倉庫送信
# ------------------------------------------------------------------
@bp.get("/report/<name>")
def report(name: str):
    """帳票を**HTMLのまま返す**(設計書 §6.3)。

    `reports.py` は完成したHTMLを作るので、そのまま返せる。
    tkinter版がやっていた「一時ファイルに書いてブラウザで開き、
    あとで消す」という後始末が丸ごと不要になった。

    画面は `window.open()` して `onload` で `window.print()` を呼ぶ。
    """
    session = _session()
    if name == outputs.REPORT_LABEL:
        refusal = outputs.label_refusal(session)
        if refusal is not None:
            return _report_problem(refusal)
        built = outputs.build_label(session)
    elif name == outputs.REPORT_CUT:
        refusal = outputs.cut_request_refusal(session)
        if refusal is not None:
            return _report_problem(refusal)
        built, refusal = outputs.build_cut_request(session)
        if built is None:
            return _report_problem(refusal)
    else:
        return _report_problem(outputs.Refusal(
            f"知らない帳票です: {name}", outputs.NOT_FOUND), status=404)

    session.presenter.user_log.log(
        f"{built.title} を出力しました", emphasis=True)
    return Response(printing.render_html(built), mimetype="text/html")


def _report_problem(refusal, status: int = 422):
    """帳票が出せないときも**HTMLを返す**。

    別窓で開いた先が真っ白だったりJSONの生文字列だったりすると、
    壊れているようにしか見えない。理由と、閉じる手立てを出す。
    """
    body = (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        "<title>帳票を出せません</title><style>"
        "body{font-family:'Meiryo UI',sans-serif;margin:40px;line-height:1.8}"
        "p{font-size:15px}button{font:inherit;padding:8px 16px;margin-top:16px}"
        "</style></head><body>"
        f"<h1>帳票を出せません</h1><p>{printing.escape(refusal.message)}</p>"
        '<button type="button" onclick="window.close()">閉じる</button>'
        "</body></html>")
    return Response(body, status=status, mimetype="text/html")


@bp.post("/api/selection/map")
def show_on_map():
    """選定した資材の置き場を、棚検索でまとめて見る(VBA `btnMap_Click`)。

    **ここでは渡すだけ。** どこに置いてあるか・どれだけ歩くかは棚検索が
    決める(`layout_session.show_selection`)── 同じ判断を2か所に置かない。
    """
    session = _session()
    items = session.map_items()
    if not items:
        return jsonify(_error("no_selection",
                              "先にボードかアングルを決めてください。")), 422
    work_context.get_context().set_map_items(items)
    return jsonify({"ok": True, "count": len(items), "next": "/layout"})


@bp.post("/api/selection/stock")
def show_on_stock():
    """いま決まっているパレット寸法で、簡易在庫を見る(VBA `btnUFMAP_Click`)。

    **ここでは引かない。** 在庫が有るかどうかは簡易在庫が答える
    (`presenters/inventory`)── 同じ問い合わせを2か所に置かない。
    """
    session = _session()
    size = session.stock_size()
    if not size:
        return jsonify(_error("no_pallet",
                              "先にパレットを決めてください。")), 422
    work_context.get_context().set_stock_size(size["width"], size["length"])
    return jsonify({"ok": True, **size, "next": "/inventory"})


@bp.post("/api/selection/send")
def send_to_warehouse():
    """倉庫送信(VBA `btnSendToWarehouse_Click`)。

    **ここでは登録しない。** 組み立てた行を倉庫連携画面へ渡し、
    そこで内容を見てから登録させる。tkinter版も `frmSendConfirm` に
    切り替えて確認させていた ── 発注は取り消しに人手が要るので、
    送る前に画面で見せる。
    """
    session = _session()
    refusal = outputs.send_refusal(session)
    if refusal is not None:
        return _state(session, message=refusal.message), 422

    orders = outputs.build_orders(session)
    work_context.get_context().set_pending_orders(orders)
    log.info("倉庫送信の下書きを %s 行 渡しました", len(orders))
    return _state(session, message=f"倉庫連携へ {len(orders)} 行 渡しました",
                  sent=len(orders), next_url="/warehouse")


# ------------------------------------------------------------------
# 共通
# ------------------------------------------------------------------
# 断りの種類 → HTTPステータス。**文言から推し量らない**(§6.1)。
# ここが1か所にあるので、新しい断りを足したときに割り当てを決め忘れない
_STATUS_BY_REASON = {
    selection_session.REFUSE_BAD_INPUT: 400,
    selection_session.REFUSE_NOT_LISTED: 400,
    selection_session.REFUSE_NEEDS_SIZES: 422,
    selection_session.REFUSE_NO_CANDIDATES: 422,
    selection_session.REFUSE_NOT_FOUND: 422,
    # 認証していないのに保存しようとした。入力の誤りでも業務の断りでもない
    selection_session.REFUSE_DENIED: 403,
    selection_session.REFUSE_FAILED: 500,
}


def _apply(session, result):
    """操作1回の結果を応答にする。

    断られた場合も**画面ぜんぶを返す**(422 / 500)。押した拍子に
    一覧が消えると、断られたのか壊れたのか区別できない。
    入力の形の誤り(400)だけはエラー封筒で返す ── 状態は動いていないので、
    画面はその欄に戻せばよい。
    """
    if not result.ok:
        status = _STATUS_BY_REASON.get(result.reason, 400)
        if status == 400:
            return jsonify(_error(result.reason, result.message)), status
        # 最上位の `message` に理由を置く。画面はここから読む
        # (`api.js` の `ApiError`)。入れ忘れると「通信に失敗しました」と
        # 出て、通信は成功しているのに直しようがなくなる
        return _state(session, message=result.message,
                      notes=result.notes), status
    return _state(session, message=result.message, notes=result.notes)


def _parse_int(value) -> Optional[int]:
    """数に直せなければ `None`。0 と「読めなかった」を混ぜない。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_int(text: str) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _error(code: str, message: str, field: str = "") -> dict:
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    return {"error": body}
