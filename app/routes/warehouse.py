"""発注のやりとり (旧 `frmSendConfirm` + `frmWarehouseOrder`)

**この画面はモードで中身が変わります。**

    現場モード … 発注を**出す**。自分が送ったものを見て、まだ倉庫が
                 受けていないものは**取り消せる**(確認はできない)
    資材モード … 受け取った発注を**確認する**(出すことも取り消すことも
                 しない)

**役割は3つに分かれます。**

    出せるのは現場だけ
    確認できるのは倉庫(資材)だけ
    取り消せるのは現場だけ、かつ倉庫が確認する前だけ

VBA版は現場用(`frmSendConfirm`)と資材用(`frmWarehouseOrder`)が別の
フォームで、**確認ボタンは資材のフォームにしかなく、取り消しは現場の
フォームにしかありませんでした**。移植のときに確認と取り消しを
まとめて資材専用にしてしまい、取り消しが**逆**になっていました。

現場から確認できてはいけないのは、資材が実際に受け取ったかに関わらず
確認済みにでき、**確認という工程そのものが意味を失う**からです。
資材から取り消せてはいけないのは、取り消しが「出した側が自分の依頼を
取り下げる」操作だからです。

Web版は確認の保証を2段で作ります。

1. `mode:material` の権限が無い端末には、確認の操作を断ります(404)。
   守るのは「誰か」です
2. 権限があっても、いま現場モードで見ていれば断ります(403)。
   こちらは誤操作の防止で、1 とは目的が違うので両方置いています

**両方とも、要求のたびに確かめます。** 以前は 1 を「起動時の権限で
エンドポイントを登録するかどうか」で分けていましたが、これだと
マスタ管理でアクセス権限に行を足しても、**サーバプロセスを終了して
起動し直すまで反映されません**でした(ページの読み込み直しでは
Pythonのプロセスは再起動しないため)。権限を足した理由が
「いま資材モードで使いたいから」であることを踏まえ、`master.py` の
マスタ管理と同じ形(常に登録し、要求のたびに `master_admin.can_edit`
で見る)にそろえています。
"""
from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, render_template, request

from packaging_tool import (access_control, config, data_sync, modes, order_comments,
                            stock_reserve, warehouse_service as svc, work_context)
from packaging_tool.logging_utils import get_logger
from packaging_tool.presenters import warehouse as presenter

from .. import current_grant, current_mode, get_db
from ..shell import shell_context

log = get_logger("app.routes.warehouse")

# 発注一覧から開いたロットの詳細で、資材展開のボタンがあった場所に出す一文。
# **ここは確かめる場所。** 発注を見て「どんなロットだっけ?」を確かめる
# ためだけに開くので、資材を決める操作は置かない(現場モードでも同じ)
LOT_PEEK_WHY = ("この発注のロット情報です。"
                "資材展開はロット検索から行います。")

# どのモードにもある部分(一覧・検索)
bp = Blueprint("warehouse", __name__)
# 現場モードで見ているときだけ使える部分(発注・取消)。
#
# **権限では分けない。** `mode:field` は誰でも持つ既定の権限
# (`access_control` は権限行にモードが1つも無ければ現場モードを足す)
# なので、権限で断っても誰も断れない。分かれ目は「いまどちらのモードで
# 見ているか」だけ ── 資材課の人でも、現場モードに切り替えれば
# 発注も取り消しもできる
field_only = Blueprint("field_only", __name__)
# 資材モードの権限がある端末でだけ使える部分(確認)。
# **常に登録する**(起動時の権限では決めない)。誰が使えるかは
# `_require_material_mode` が要求のたびに確かめる
material_only = Blueprint("material_only", __name__)


@material_only.before_request
def _require_material_mode():
    """資材モードの権限があり、かついま資材モードで見ているかを断る。

    2段のうち、こちらが両方を受け持つ(以前は1段目を起動時の登録で
    分けていた)。

    1. 権限そのものが無い ── **そもそもこの端末では使えない操作**。
       起動時の権限では決めず、要求のたびに `access_control.resolve`
       で引き直す。取り込み元は共有フォルダの1ファイルで、書く場所は
       ここだけとは限らないため、断る前に一度だけ読み直す
       (`access_control.resync` と同じ考え方)
    2. 権限はあるが、いま現場モードで見ている ── 資材課の人が現場
       モードで作業している最中に、手が滑って確認済みにできて
       しまうのを防ぐ
    """
    grant = current_grant()
    if not grant.allows_mode(modes.MATERIAL):
        # 手元がまだ古い可能性がある。断る前に取り込み元から
        # アクセス権限だけ読み直し、それでも無ければ本当に無い
        if access_control.resync(get_db()):
            from flask import g
            g.pop("grant", None)
            grant = current_grant()
        if not grant.allows_mode(modes.MATERIAL):
            log.info("資材モードの権限が無いため断りました: %s", request.path)
            return jsonify({"error": {
                "code": "not_found",
                "message": "その操作はこの端末では使えません。"
                          "設定画面でアクセス権限の登録状況を確認してください。"
                }}), 404
    if current_mode() != modes.MATERIAL:
        log.info("資材モードではないため断りました: %s", request.path)
        return jsonify({"error": {
            "code": "wrong_mode",
            "message": "この操作は資材モードでのみ行えます。"
                       "右上でモードを切り替えてください。"}}), 403
    return None


@field_only.before_request
def _require_field_mode():
    """いま現場モードで見ているかだけを断る。

    **権限は見ない。** `mode:field` は誰でも持つ既定の権限なので、
    権限で断っても誰も断れない(`field_only` の宣言を参照)。
    資材モードで見ているあいだだけ断る ── 資材課の人が受け側の画面を
    開いたまま、手が滑って発注を出したり取り消したりするのを防ぐ。
    現場モードに切り替えれば使える。
    """
    if current_mode() != modes.FIELD:
        log.info("現場モードではないため断りました: %s", request.path)
        return jsonify({"error": {
            "code": "wrong_mode",
            "message": "この操作は現場モードでのみ行えます。"
                       "右上でモードを切り替えてください。"}}), 403
    return None


def _mode() -> str:
    return current_mode()


@bp.get("/warehouse")
def page():
    mode = _mode()
    view = presenter.build(get_db(), mode=mode)
    # 資材選択の「倉庫送信」が組み立てた下書き。**見るだけで手放さない**
    # (`WorkContext.peek_pending_orders`)。手放すのは、送ったとき・
    # 「入力を消す」で捨てたとき・ロットが変わったとき
    drafts = work_context.get_context().peek_pending_orders()
    if drafts:
        log.info("倉庫送信の下書きを %s 行 出します", len(drafts))
    if drafts and mode == modes.FIELD:
        _apply_orders(drafts)
    if len(drafts) == 1 and mode == modes.FIELD:
        # 同じ幅×丈の在庫があれば「在庫分を引きますか?」を出す(現場だけ。出すのは現場)
        drafts[0]["stock"] = presenter.stock_view(
            get_db(), drafts[0], drafts[0].get("hatchu_suu"))
    return render_template(
        "warehouse.html",
        state=presenter.to_dict(view),
        columns=presenter.ORDER_VIEW,
        action_width=presenter.ORDER_ACTION_WIDTH,
        fields=presenter.ORDER_FIELDS,
        date_filters=presenter.DATE_FILTERS,
        drafts=drafts,
        is_material=mode == modes.MATERIAL,
        # 「Lotを開く」で出すモーダルは、ここでは**確かめるだけ**。
        # 資材展開のボタンは出さないので、その場所に出す一文を渡す
        can_expand_here=False,
        lot_peek_why=LOT_PEEK_WHY,
        expand_absent_why=LOT_PEEK_WHY,
        cut_state=_cut_list(get_db()),
        # 作業予定ライン(新規発注の下。VER4.8.85)。いまのロット(下書きのロットが先)の予定と、選べるライン
        lot_plan=(_lot_plan_state(get_db(), (drafts[0].get("lot_no") if drafts else "")
                                  or work_context.get_context().lot_no)
                  if mode == modes.FIELD else None),
        # CSVで出す: 期間の初めの値(今月の1日〜今日)と出る場所(設定タブで変えられる)
        export_start=date.today().replace(day=1).isoformat(),
        export_end=date.today().isoformat(),
        export_dir=str(config.order_csv_dir()),
        **shell_context("warehouse", badges=_rail_badge(view, _cut_unread(mode))),
    )


def _order_choice(lot_no: str):
    """いまのロットのオーダー(`outputs.order_choice`)。資材選択のロットと違えば None。"""
    from packaging_tool import selection_session
    from packaging_tool.presenters import outputs
    session = selection_session.get_session(get_db())
    result = session.presenter.lot_result
    if result is None or not getattr(result, "found", False) or result.lot.lot_no != lot_no:
        return None
    return outputs.order_choice(session)


def _apply_orders(drafts: list[dict]) -> None:
    """下書きを、**いま選んでいるオーダー**に合わせ直す(納入先・含めるオーダー・発注数)。

    下書きはパレットを決めたときに作るので、そのあとロット検索でオーダーを選び直すと古くなる。
    開くたびに作り直す。EX は中身を送らないので触らない。
    """
    from packaging_tool.presenters import outputs
    if drafts[0].get("is_ex_order"):
        return
    choice = _order_choice(drafts[0].get("lot_no", ""))
    if not choice:
        return
    delivery = outputs.delivery_of(choice)
    lot_wide = _lot_wide(drafts)
    for draft in drafts:
        if delivery is not None:
            draft["nounyusaki"] = delivery
        # 2行(1P0113 の角材・松板)はロット全体の数なので、全オーダーを含める
        every = [o["order_no"] for o in choice["orders"]]
        fields = outputs.order_fields(choice, every if lot_wide else None)
        draft["juchu_no"], draft["lot_orders"] = fields["juchu_no"], fields["lot_orders"]
    # 発注数を梱包数で決め直すのは1行目(パレット・EXなら台)だけ。EXの枚は人が入れる
    if not lot_wide and drafts[0].get("pallet_width"):
        included = outputs.order_fields(choice)["included"]
        qty = outputs.packages_for(choice, included)
        if qty is not None:
            drafts[0]["hatchu_suu"] = str(qty) if qty > 0 else ""
        block = presenter.orders_block(choice, drafts[0].get("hatchu_suu", ""),
                                       drafts[0].get("lot_no", ""))
        if block:
            drafts[0]["orders"] = block


def _lot_wide(drafts: list[dict]) -> bool:
    """ロット全体の数で送る2行(1P0113 の角材・松板)か。

    パレット(台)と蓋(枚)(`outputs.lid_row`。EX・上下共用)も2行だが、どちらもパレット一覧の行(寸法を持つ)で、
    オーダーの選び方は1行のときと同じ(含めるオーダーは画面のチェック)。
    """
    return len(drafts) > 1 and not all(d.get("pallet_width") for d in drafts)


def _cut_unread(mode: str) -> int:
    """レールに出す、倉庫がまだ開いていない切断依頼の数(資材モードだけ)。"""
    if mode != modes.MATERIAL:
        return 0
    from packaging_tool import cut_requests
    return cut_requests.unread_count(get_db())


def _rail_badge(view, cut_unread: int = 0):
    """レールの印。未確認の件数と、相手が書いた未読コメントの数と、
    倉庫がまだ開いていない切断依頼の数(資材モードだけ)。"""
    parts = []
    if view.pending:
        parts.append(str(view.pending))
    if view.unread_comments:
        parts.append(f"新{view.unread_comments}")
    if cut_unread:
        parts.append(f"切{cut_unread}")
    return {"warehouse": ("・".join(parts), "todo")} if parts else None


@bp.get("/api/warehouse/orders")
def orders():
    """`?q=&period=&cancelled=`。"""
    view = presenter.build(
        get_db(), mode=_mode(),
        keyword=request.args.get("q", ""),
        date_filter=request.args.get("period", ""),
        include_cancelled=request.args.get("cancelled") == "1")
    return jsonify(presenter.to_dict(view))


@bp.post("/api/warehouse/refresh")
def refresh():
    """取り込み元を見に行ってから一覧を返す。

    **どちらのモードからも押せる。** 出す側も受ける側も、相手が何を
    したかを見るのに要る。

    2通りの呼ばれ方をする。

        押されたとき     … `only_if_changed` 無し。必ず見に行く。
                           変わっていなければ「新しいものはありません」
        見張りのとき     … `only_if_changed=1`。取り込み元の姿だけ見て、
                           変わっていなければ**開かずに帰る**

    どちらも一覧を一緒に返す。取り込んだのに古い一覧のままでは、
    押した意味が画面に出ない。
    """
    only_if_changed = request.args.get("only_if_changed") == "1"
    conn = get_db()
    got = data_sync.refresh_orders(conn, only_if_changed=only_if_changed)
    if got.errors:
        for message in got.errors:
            log.warning("発注の取り込み直し: %s", message)
    view = presenter.build(
        conn, mode=_mode(),
        keyword=request.args.get("q", ""),
        date_filter=request.args.get("period", ""),
        include_cancelled=request.args.get("cancelled") == "1")
    return jsonify({**presenter.to_dict(view),
                    "refresh": {"updated": got.updated,
                                "looked": got.looked,
                                # 取り込めなかった表と理由(送れていない分があって見送った等)。
                                # 見張りでも画面に残す(`showReach`)
                                "errors": list(got.errors),
                                "message": got.message()}})


@field_only.post("/api/warehouse/send")
def send():
    """発注を出す(VBA `btnSendToWarehouse_Click` 〜 `WriteWarehouseRows`)。**出せるのは現場だけ。**

    `rows` に複数行を渡すと、**全行を1回で登録する**(1P0113 の角材と松板)。
    片方だけが倉庫に届くことが無いように、1行でもおかしければ1行も登録しない
    (`warehouse_service.create_orders`)。1行のときは本文そのものが1行。

    下書き(資材選択の「倉庫送信」から届いたもの)は、**発注数のほかを
    打ち直せない**。原文の `frmSendConfirm.BuildRowUI` も発注数だけを
    入力欄にして、ほかはラベルで出していた ── 送るのは道具が組み立てた
    ものだから。

    画面で打てなくするだけにしない(守りは1枚ではない)。ここでは
    **LotNo がいま作業中のロットと同じか**を確かめる。作業中のロットが
    無い(見つからないロット番号を開いた・まだ開いていない)ときも断る ──
    前のロットのまま送れてしまうため(VBA で直した不具合)。
    手入力(`from_draft` が無い)には掛けない ── 別のロットの分を手で
    起こすことがあるため。

    **同じロットをもう送ってあれば、続けるかを聞く**(VBA `ConfirmNotAlreadySent`)。
    `confirm_duplicate` が無ければ 409 と、送信済みの一覧を返す。警告であって止めはしない
    (追加の注文もある)。ほかの端末の発注も数えるよう、確かめる前に共有から取り込み直す。
    """
    body = request.get_json(silent=True) or {}
    raw_rows = body.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raw_rows = [body]
    orders = []
    for index, raw in enumerate(raw_rows):
        values, problem = presenter.validate(raw if isinstance(raw, dict) else {})
        if problem:
            field, message = problem
            where = f"{index + 1}行目: " if len(raw_rows) > 1 else ""
            return jsonify({"error": {"code": "invalid", "message": where + message,
                                      "field": field, "row": index}}), 400
        orders.append(values)

    # ロット検索なしの下書き(`outputs.no_lot_draft`)は LotNo を打ち込むので、作業中のロットと照らさない。
    # **どちらの下書きかは、サーバが預かっている下書きで決める**(画面から届いた印は信じない)
    held = work_context.get_context().peek_pending_orders()
    no_lot = bool(body.get("from_draft")) and bool(held) and bool(held[0].get("no_lot"))
    if body.get("from_draft") and not no_lot:
        working = (work_context.get_context().lot_no or "").strip()
        if not working:
            return jsonify({"error": {
                "code": "no_lot", "field": "lot_no",
                "message": ("作業中のロットがありません。ロット検索でロットを開き直してから、"
                            "資材選択でパレットを決め直してください。")}}), 400
        wrong = [o["lot_no"] for o in orders if o["lot_no"] != working]
        if wrong:
            log.warning("下書きのLotNoが作業中と違います: %s ≠ %s", wrong[0], working)
            return jsonify({"error": {
                "code": "lot_mismatch", "field": "lot_no",
                "message": (f"下書きのLotNoが作業中のロット({working})と"
                            "違います。画面を開き直してください。")}}), 400

    note = str(body.get("comment") or "").strip()
    if len(note) > order_comments.MAX_LENGTH:
        return jsonify({"error": {
            "code": "invalid", "field": "comment",
            "message": f"コメントは{order_comments.MAX_LENGTH}文字までです(いま{len(note)}文字)。"}}), 400

    conn = get_db()
    # **送る直前に、共有で変わっていれば取り込み直す(1回だけ)。** ほかの端末が出した発注
    # (同じロットの確かめ)・使った在庫(「在庫分を引く」の確かめ)は、取り込むまで手元に無い
    _refresh_before("送る前の確かめ", conn)
    if not body.get("confirm_duplicate"):
        # 同じロットで発注していれば訊く(現場の声:「ロットNoが重複していたら、同じロットで
        # 発注していると警告を。追加注文もあるので、あくまで警告でいい」)
        for lot_no in dict.fromkeys(o["lot_no"] for o in orders):
            sent = svc.already_sent(conn, lot_no)
            if sent:
                log.info("同じロットの送信済みがあるので確かめます: LotNo=%s %s件", lot_no, len(sent))
                return jsonify({"error": {"code": "already_sent",
                                          "message": presenter.same_lot_text(lot_no, sent)},
                                "ask": presenter.already_sent_ask(lot_no, sent)}), 409

    # **オーダー。** 下書きなら、含めたオーダー(画面のチェック)と納入先をサーバで決め直す
    if body.get("from_draft"):
        refusal = _set_orders(orders, body.get("orders"))
        if refusal is not None:
            return refusal

    # 「在庫分を引く」を押して送った。**送る直前に共有の最新で確かめ直す**
    use = _to_int(body.get("use_stock"))
    reserve = None
    if use:
        # **丸めない。** 発注数は整数だけ(10.5 を 10 にして元の数を作り違えない)
        if len(orders) != 1 or presenter._qty(orders[0]["hatchu_suu"]) is None:
            return jsonify({"error": {"code": "invalid", "field": "hatchu_suu",
                                      "message": "発注数は整数で入れてください。"}}), 400
        reserve, refusal = _stock_to_use(conn, orders, use, from_draft=bool(body.get("from_draft")))
        if refusal is not None:
            return refusal
        orders[0]["stock_used"] = use

    terminal = svc.this_terminal()
    after = None
    if reserve is not None:
        def after(tx, rows, _made):
            # 発注と同じトランザクションで引当を残す(片方だけ残さない)
            stock_reserve.write_reserve(
                tx, order_key=rows[0]["発注キー"], lot_no=rows[0]["LotNo"],
                width=reserve["size"][0], length=reserve["size"][1],
                takes=reserve["takes"], terminal=terminal)
    result = svc.create_orders(conn, orders, terminal=terminal, after=after)
    if not result.ok:
        return jsonify({"ok": False, "message": result.message,
                        "error": {"code": "rejected", "message": result.message}}), 422
    log.info("発注を登録しました: 管理番号=%s", result.mgr_nos)
    if body.get("from_draft"):
        # 送った下書きは手放す。開き直しても、送ったものはもう出さない
        work_context.get_context().drop_pending_orders()
    if note:
        # 送るときに添えたコメント。発注と一緒に取り込み元へ届く
        # (まとめて送ったときは1行目に付ける。同じ文を行の数だけ積まない)
        order_comments.add(conn, result.mgr_nos[0], note,
                           terminal=terminal, side=_side())
    stock_note = ""
    if reserve is not None:
        # **在庫を使ったことを倉庫へ知らせる。** 発注数が減った理由と元の数が分かるように
        qty = presenter._qty(orders[0]["hatchu_suu"])
        order_comments.add(conn, result.mgr_nos[0], stock_reserve.comment_text(
            reserve["size"][0], reserve["size"][1], reserve["takes"],
            before=qty + use, after=qty), terminal=terminal, side=_side())
        stock_note = (f" 在庫から {use}台 使います({stock_reserve.describe(reserve['takes'])})。"
                      "簡易在庫の図に「持出」の印を付けました。")
    # 倉庫へ届けるのが仕事なので、押した直後に送りにいく。
    # **画面には何も出さない** ── 手元の登録はもう終わっており、
    # 届かなくても次の「取り込み元へ反映」でまとめて送られる
    data_sync.write_back_in_background()
    # **共有フォルダが見えないときは「送信しました」と言わない。** 手元に預かっただけで、
    # 倉庫にはまだ届いていない(通し点検: 見えない間も「送信しました」と出て、現場は
    # 届いたと思っていた)。つながれば心拍のついでに自動で送る(`retry_unsent_in_background`)
    from packaging_tool import sync_sources
    queued = sync_sources.find_material_db() is None
    message = result.message
    if queued:
        rows = f"{len(result.mgr_nos)}行まとめて" if len(result.mgr_nos) > 1 else ""
        message = (f"発注を{rows}この端末に登録しましたが、共有フォルダに届かないため、まだ倉庫には"
                   "届いていません。つながると自動で送ります(一覧に「未送信」と出ている間は届いていません)。")
        log.warning("共有フォルダが見えないため、発注を手元に預かりました: 管理番号=%s", result.mgr_nos)
    message += stock_note
    return jsonify({"ok": True, "message": message, "queued": queued,
                    "mgr_no": result.mgr_nos[0], "mgr_nos": result.mgr_nos})


def _set_orders(orders: list[dict], picked_list):
    """発注の行に、含めたオーダー・ロットの全オーダー・代表のオーダーの納入先を入れる。

    含めるオーダーは画面のチェック(`orders`)。**このロットに無いオーダーは断る**。
    代表(納入先のオーダー)は必ず含める。2行(1P0113)はロット全体なので全オーダー。
    """
    from packaging_tool.presenters import outputs
    if not orders or orders[0].get("is_ex_order"):
        return None
    choice = _order_choice(orders[0]["lot_no"])
    if not choice:
        return None
    every = [o["order_no"] for o in choice["orders"]]
    included = None
    # どちらの2行かは、サーバが預かっている下書きで決める(画面から届いた値は寸法を持たない)
    if len(orders) > 1 and _lot_wide(work_context.get_context().peek_pending_orders() or orders):
        included = every
    elif isinstance(picked_list, list):
        stray = [str(o) for o in picked_list if str(o) not in every]
        if stray:
            return jsonify({"error": {"code": "invalid", "field": "orders",
                                      "message": f"このロットに無いオーダーです: {'・'.join(stray)}。"
                                                 "画面を開き直してください。"}}), 400
        included = [str(o) for o in picked_list]
    fields = outputs.order_fields(choice, included)
    delivery = outputs.delivery_of(choice)
    for order in orders:
        order["juchu_no"], order["lot_orders"] = fields["juchu_no"], fields["lot_orders"]
        if delivery is not None:
            order["nounyusaki"] = delivery
    return None


def _to_int(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


# 在庫を充てられない送り方のときの断り
STOCK_ONLY_DRAFT = "在庫を充てられるのは、資材選択から届いた1行の発注だけです。"


def _stock_to_use(conn, orders: list[dict], use: int, *, from_draft: bool):
    """「在庫分を引く」で引いた台数を、いまの在庫で確かめる。

    戻り値は (使う置き場, None) か (None, 断りの応答)。
    **送る直前に共有の最新を取り込んでから**確かめる(コメントを書く前と同じ)。
    ほかの端末が先に使った・払い出したなら送らずに、いま使える台数を返す。
    寸法は画面から受け取らない ── 下書き(サーバが預かっているもの)から取る。
    """
    drafts = work_context.get_context().peek_pending_orders()
    size = presenter.draft_pallet(drafts[0]) if len(drafts) == 1 else None
    if not from_draft or len(orders) != 1 or orders[0].get("is_ex_order") or size is None:
        return None, (jsonify({"error": {"code": "bad_stock", "field": "hatchu_suu",
                                         "message": STOCK_ONLY_DRAFT}}), 400)
    # 共有の最新は `send` が取り込み直してある(`_refresh_before`)
    found = stock_reserve.offer(conn, *size)
    if found.total < use:
        qty = presenter._qty(orders[0]["hatchu_suu"])
        log.info("在庫が足りなくなっていたので送りませんでした: %s×%s 引く%s台 / 使える%s台",
                 size[0], size[1], use, found.total)
        return None, (jsonify({
            "error": {"code": "stock_changed", "field": "hatchu_suu",
                      "message": (f"在庫が変わりました(ほかの端末が先に使った・払い出した)。"
                                  f"いま使える在庫は {found.total}台 です。発注数を {qty + use} に"
                                  "戻したので、もう一度確かめてから送ってください。")},
            "before": qty + use,
            "stock": presenter.stock_view(conn, drafts[0], qty + use)}), 409)
    return {"size": size, "takes": stock_reserve.plan(found, use)}, None


def _refresh_before(label: str, conn) -> None:
    """共有で変わっていれば取り込み直す。失敗しても止めない(手元で確かめて続ける)。"""
    try:
        data_sync.refresh_orders(conn, only_if_changed=True)
    except Exception:                               # noqa: BLE001 - 送る操作は止めない
        log.exception("%s の前の取り込み直しに失敗(手元の発注で確かめます)", label)


@field_only.get("/api/warehouse/same-lot")
def same_lot():
    """このLotNoで、もう発注しているか(取り消していないもの)。**送る前から**欄の下に一言出す。

    止めはしない ── 追加の注文もある(現場の声)。送るときにも同じ一覧を見せて訊く(`send`)。
    ほかの端末が出した分も数えるよう、共有で変わっていれば取り込み直してから数える。
    """
    lot_no = (request.args.get("lot_no") or "").strip()
    if not lot_no:
        return jsonify({"lot_no": "", "count": 0, "message": ""})
    conn = get_db()
    _refresh_before("同じロットの一言", conn)
    sent = svc.already_sent(conn, lot_no)
    return jsonify({"lot_no": lot_no, "count": len(sent),
                    "message": presenter.same_lot_note(lot_no, sent)})


@field_only.post("/api/warehouse/stock/plan")
def stock_plan():
    """「在庫分を引く」を押した。いまの発注数で、何台をどの置き場から使うかを返す。

    **まだ何も書かない。** 決まるのは「倉庫へ送信」を押したとき(`send`)。
    """
    body = request.get_json(silent=True) or {}
    drafts = work_context.get_context().peek_pending_orders()
    if len(drafts) != 1 or presenter.draft_pallet(drafts[0]) is None:
        return jsonify({"error": {"code": "bad_stock", "message": STOCK_ONLY_DRAFT}}), 400
    view = presenter.stock_view(get_db(), drafts[0], body.get("qty"))
    if view is None:
        return jsonify({"stock": None,
                        "message": "使える在庫がありません(ほかの発注が持ち出し待ちにしている分は使えません)。"})
    return jsonify({"stock": view})


@bp.post("/api/warehouse/export")
def export_csv():
    """発注を期間で絞ってCSVに出す(集計 / 発注データ)。**どちらのモードからも出せる。**

    出す先は設定タブの「発注CSVの出力先」。**ブラウザへ渡さず、その場所へ書く**
    (現場の決まり: ダウンロードはしない)。書いた場所を返す。
    出す前に共有の変化を取り込む ── ほかの端末が送った発注も入れるため。
    """
    from packaging_tool import order_export
    body = request.get_json(silent=True) or {}
    start = order_export.parse_day(body.get("start"))
    end = order_export.parse_day(body.get("end"))
    if start is None or end is None:
        field = "start" if start is None else "end"
        return jsonify({"error": {"code": "invalid", "field": field,
                                  "message": "開始日と終了日を入れてください。"}}), 400
    conn = get_db()
    try:
        data_sync.refresh_orders(conn, only_if_changed=True)
    except Exception:                               # noqa: BLE001 - 手元の発注で出す
        log.exception("CSVの前の取り込み直しに失敗(手元の発注で出します)")
    result = order_export.export(conn, str(body.get("kind") or ""), start, end)
    if not result.ok:
        return jsonify({"error": {"code": "export_failed", "message": result.message}}), 422
    return jsonify({"ok": True, "message": result.message, "path": str(result.path)})


@field_only.post("/api/warehouse/drafts/discard")
def discard_drafts():
    """下書きを捨てる(「入力を消す」)。手入力に切り替えるときの口でもある。

    捨てないと、開き直したときにまた下書きが出てくる(下書きは送るまで預かる)。
    """
    work_context.get_context().drop_pending_orders()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# コメント(`order_comments`)。**どちらのモードからも**読み書きできる
# ------------------------------------------------------------------
def _side() -> str:
    return (order_comments.SIDE_MATERIAL if _mode() == modes.MATERIAL
            else order_comments.SIDE_FIELD)


def _comments_body(conn, mgr_no: int, **extra):
    row = conn.execute(f"SELECT * FROM {svc.TABLE} WHERE 管理番号 = ?",
                       (mgr_no,)).fetchone()
    comments = order_comments.comments_for(conn, mgr_no, terminal=svc.this_terminal(),
                                           side=_side())
    return {"mgr_no": mgr_no,
            "comments": [order_comments.to_dict(c) for c in comments],
            # 書けないなら理由(未確認のあいだだけ書ける)。**画面で決めない**
            "write_why": (order_comments.closed_why(row) if row is not None
                          else "この発注は一覧にありません。画面を更新してください。"),
            "max_length": order_comments.MAX_LENGTH,
            **extra}


@bp.get("/api/warehouse/comments")
def comments():
    """その発注のコメント。**開いたら読んだことにする**(この端末だけ)。

    返す一覧の「未読」は、開く前の状態のまま ── どれが新しく届いたものかを
    開いた画面で見分けられるように、印を付けてから既読にする。
    """
    try:
        mgr_no = int(request.args.get("mgr_no", ""))
    except ValueError:
        return jsonify({"error": {"code": "bad_mgr_no",
                                  "message": "対象が指定されていません"}}), 400
    conn = get_db()
    body = _comments_body(conn, mgr_no)
    # 相手のコメントを初めて開いたなら「見た」を残し、**すぐ共有へ送る**
    # (書いた人の画面に「見ました」が出るまでを短くする)
    if order_comments.mark_read(conn, mgr_no, terminal=svc.this_terminal(),
                                side=_side()):
        data_sync.write_back_in_background()
    return jsonify(body)


@bp.post("/api/warehouse/comments/read-all")
def comments_read_all():
    """新しいコメントを**まとめて既読**にする(この端末・この側だけ)。

    相手には「見た」と伝えない(共有へは何も送らない)。消えるのはこの端末の「新」の印だけ。
    """
    count = order_comments.mark_all_read(get_db(), terminal=svc.this_terminal(), side=_side())
    message = (f"新しいコメント {count}件を既読にしました(相手には「見た」と伝えていません)。"
               if count else "新しいコメントはありません。")
    return jsonify({"ok": True, "count": count, "message": message})


@bp.post("/api/warehouse/comment")
def comment():
    """コメントを書く。**未確認の発注にだけ**(確認・取り消しで固定)。

    押す前に共有の変化を取り込む(確認・取消と同じ)── 相手が先に確認・
    取り消していたら、書けない理由を言う。
    """
    body = request.get_json(silent=True) or {}
    try:
        mgr_no = int(body.get("mgr_no"))
    except (TypeError, ValueError):
        return jsonify({"error": {"code": "bad_mgr_no",
                                  "message": "対象が指定されていません"}}), 400
    conn = get_db()
    before = svc.identity(conn, mgr_no)
    try:
        data_sync.refresh_orders(conn, only_if_changed=True)
    except Exception:                               # noqa: BLE001 - 書く操作は止めない
        log.exception("コメントの前の取り込み直しに失敗(手元の状態で続けます)")
    mgr_no = svc.find_again(conn, mgr_no, before)
    result = order_comments.add(conn, mgr_no, str(body.get("text") or ""),
                                terminal=svc.this_terminal(), side=_side())
    if not result.ok:
        return jsonify(_comments_body(conn, mgr_no, error={
            "code": "rejected", "message": result.message})), 422
    # 返事を書いた = 相手のコメントも見ている
    order_comments.mark_read(conn, mgr_no, terminal=svc.this_terminal(), side=_side())
    # 相手に届けるのが目的なので、書いた直後に送りにいく(画面には出さない)
    data_sync.write_back_in_background()
    return jsonify(_comments_body(conn, mgr_no, message=result.message + data_sync.queued_note()))


@field_only.post("/api/warehouse/cancel")
def cancel():
    """取り消し。**出した本人が引っ込める操作**なので現場だけ。

    止めるものが2つあります。

        モード … 現場だけ(`field_only` の `before_request`)。資材は
                 受けて確認する側で、消す立場ではない
        状態  … 倉庫が確認する前だけ(`cancel_order` のWHERE句)。
                 受け取ったことを確認したものを消すと、現物と帳簿が
                 合わなくなる

    VBAでも取り消しは現場の送信確認ダイアログ
    `frmSendConfirm.btnDelete_Click` にあった操作で、資材側の
    `frmWarehouseOrder` にあったのは「確認済みにする」だけでした
    (`warehouse_service` の対応表を参照)。移植のときに確認と取り消しを
    まとめて資材専用にしてしまい、**送った本人が間違いに気づいても
    引っ込められない**状態になっていました(現場の声)。

    **誰が送ったかは記録していません。** 元Accessスキーマにその列は
    無く、足してもいません ── 送れるのは現場だけなので、一覧に出て
    いるものはすべて現場が出したものになり、区別する相手がいない。

    (取り込み元のテーブルに `送信ID` 列がありますが、これは人では
     ありません。書き戻しエンジンが二重登録を防ぐために自分で足す
     送信操作の一意IDです ── `outbox_sync.DEFAULT_OP_ID_COLUMN`)
    """
    return _action(svc.cancel_order, "取消")


@material_only.post("/api/warehouse/confirm")
def confirm():
    """確認済みにする(VBA `frmWarehouseOrder.btnConfirm_Click`)。

    **資材モードの権限がある端末にしか登録されません。** 権限が無ければ
    404、権限があっても現場モードで見ていれば 403。
    """
    return _action(svc.confirm_order, "確認")


@material_only.post("/api/warehouse/unconfirm")
def unconfirm():
    """確認を外す(未確認に戻す)。**資材モードだけ**(確認と同じ。どの資材端末からでも)。

    現場の「取り消し」とは別の操作(`warehouse_service.unconfirm_order`)。
    外すと、送った端末はまた取り消せるようになる。現場が気づけるよう
    その発注にコメントを自動で残す。
    """
    return _action(svc.unconfirm_order, "確認を外す")


def _action(func, label: str):
    body = request.get_json(silent=True) or {}
    try:
        mgr_no = int(body.get("mgr_no"))
    except (TypeError, ValueError):
        return jsonify({"error": {"code": "bad_mgr_no",
                                  "message": "対象が指定されていません"}}), 400

    conn = get_db()
    before = svc.identity(conn, mgr_no)
    # **押す前に、共有で変わっていれば取り込み直す。** 相手の端末が先に
    # 取り消した(確認した)ことは、取り込むまで手元に無い。古いまま押すと
    # 取り消し済の発注を「確認済みにしました」と言ってしまう
    # (送るときにも止めるが、それでは押した人に伝わらない)。
    # 変わっていなければファイルの姿を見るだけで帰る
    try:
        data_sync.refresh_orders(conn, only_if_changed=True)
    except Exception:                               # noqa: BLE001 - 押す操作は止めない
        log.exception("%s の前の取り込み直しに失敗(手元の状態で続けます)", label)
    # 取り込み直すと手元の管理番号は振り直される。画面が持っている番号の
    # 行を、共有の行番号(なければ登録日時・LotNo・品名)で探し直す
    mgr_no = svc.find_again(conn, mgr_no, before)
    result = func(conn, mgr_no)
    if not result.ok:
        # 「既に確認済み/取消済み」は、他の端末が先に動かした結果でもある。
        # 画面を取り直せば正しい状態が見えるので 409 で返す
        log.info("%s できませんでした: 管理番号=%s %s", label, mgr_no, result.message)
        return jsonify({**presenter.action_dict(result),
                        "error": {"code": "conflict",
                                  "message": result.message}}), 409
    log.info("%s しました: 管理番号=%s", label, mgr_no)
    # **印も共有へ届けにいく。** 確認・取消は手元の書き換えなので、
    # 送りにいかないと相手側から状況が見えず、次の取り込みで消える。
    # 発注を出したときと同じで、画面には何も出さない(手元の更新は
    # もう終わっており、届かなくても次の反映でまとめて送られる)
    data_sync.write_back_in_background()
    body = presenter.action_dict(result)
    if body.get("message"):
        body["message"] += data_sync.queued_note()
    return jsonify(body)


# ------------------------------------------------------------------
# 切断依頼(`cut_requests`)。現場が資材選択の切断依頼のプレビューから送り、
# 倉庫はここで一覧・開いて印刷・「切った」。現場はここで状態を見て、
# 倉庫が受け取る前なら取り消せる
# ------------------------------------------------------------------
def _cut_list(conn) -> dict:
    from packaging_tool import cut_requests
    material = _mode() == modes.MATERIAL
    terminal = svc.this_terminal()
    unsent = cut_requests.unsent_ids(conn)
    items = [cut_requests.to_dict(r, terminal=terminal, material=material, unsent=unsent)
             for r in cut_requests.recent(conn)]
    return {"items": items, "material": material,
            # 倉庫がまだ開いていない数(倉庫では「新」、現場では「未読」)
            "unread": sum(1 for i in items if i["state"] == cut_requests.SENT
                          and not i["replaced"])}


@bp.get("/api/cut-requests")
def cut_request_list():
    """送った / 届いた切断依頼(手元から。共有は見張りが取り込む)。"""
    return jsonify(_cut_list(get_db()))


def _cut_mark(state: str):
    from packaging_tool import cut_requests
    body = request.get_json(silent=True) or {}
    conn = get_db()
    # 押す前に共有の変化を取り込む(相手が先に受け取った・取り消した、を知るため)
    try:
        data_sync.refresh_orders(conn, only_if_changed=True)
    except Exception:                               # noqa: BLE001 - 押す操作は止めない
        log.exception("切断依頼の前の取り込み直しに失敗(手元の状態で続けます)")
    side = (cut_requests.SIDE_MATERIAL if _mode() == modes.MATERIAL
            else cut_requests.SIDE_FIELD)
    result = cut_requests.mark(conn, str(body.get("id") or ""), state,
                               terminal=svc.this_terminal(), side=side)
    payload = _cut_list(conn)
    if not result.ok:
        payload["error"] = {"code": result.reason, "message": result.message}
        return jsonify(payload), (404 if result.reason == cut_requests.REFUSE_NOT_FOUND
                                  else 409)
    data_sync.write_back_in_background()
    payload["message"] = result.message + data_sync.queued_note()
    return jsonify(payload)


@field_only.post("/api/cut-requests/cancel")
def cut_request_cancel():
    """取り消す。**送った端末だけ、倉庫が受け取る前だけ**。`{"id":…}`"""
    from packaging_tool import cut_requests
    return _cut_mark(cut_requests.CANCELLED)


@material_only.post("/api/cut-requests/cut")
def cut_request_cut():
    """切った。`{"id":…}`(まだ開いていなければ、受け取ったことにもなる)"""
    from packaging_tool import cut_requests
    return _cut_mark(cut_requests.CUT)


@bp.get("/report/cut-sent/<request_id>")
def cut_request_view(request_id: str):
    """送った切断依頼の紙面(読むだけ)。**倉庫(資材モード)が開いたら「受け取った」**。

    印刷はこの窓の「印刷する」から。現場も、送ったあと(別のロットに移ったあとでも)
    ここから開き直して印刷できる。
    """
    from flask import Response
    from packaging_tool import cut_requests, printing
    conn = get_db()
    report = cut_requests.report_of(conn, request_id)
    req = cut_requests.get(conn, request_id)
    if report is None or req is None:
        return Response(
            '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            "<title>切断依頼が見つかりません</title></head><body>"
            "<p>この切断依頼は見つかりません。倉庫連携の画面を更新してください。</p>"
            "</body></html>", status=404, mimetype="text/html")
    if _mode() == modes.MATERIAL and req.state == cut_requests.SENT:
        got = cut_requests.mark(conn, request_id, cut_requests.RECEIVED,
                                terminal=svc.this_terminal(), side=cut_requests.SIDE_MATERIAL)
        if got.ok:
            # 現場の画面に「受け取った」が早く出るよう、すぐ送る
            data_sync.write_back_in_background()
            req = cut_requests.get(conn, request_id) or req
    note = (f"Lot {req.lot_no} の切断依頼({req.terminal} が {req.sent_at} に送ったもの)。"
            f"いまの状態: {cut_requests.state_text(req)}。")
    if req.state == cut_requests.CANCELLED:
        note = "⚠ この切断依頼は取り消されています。" + note
    elif req.replaced_by:
        note = "⚠ この切断依頼は送り直されています(新しいほうを使ってください)。" + note
    return Response(printing.render_html(report, note=note), mimetype="text/html")


# ------------------------------------------------------------------
# 作業予定ライン(VER4.8.85)── 長がロットを作業ラインに入れる(現場)
#
# 現場の声:「新規発注フレームの下余白に作業予定ライン選択を設けてください」
# 入れたロットは、そのライン(アクセス権限のライン名)の端末のロット検索に知らせが出る(`lot_plans`)
# ------------------------------------------------------------------
def _lot_plan_state(conn, lot_no: str) -> dict:
    """そのロットのいまの予定と、選べるライン(拠点。既定の L1 / HVC / LVC とマスタで足した拠点)。"""
    from packaging_tool import location_service, lot_plans
    lot = lot_plans.normalize_lot(lot_no)
    plan = lot_plans.current(conn, lot) if lot else None
    unsent = lot_plans.unsent_ids(conn)
    lines = location_service.base_point_names(conn)
    return {"lot_no": lot,
            "plan": plan.to_dict() if plan else None,
            "lines": lines,
            # 選択肢のうち、いまの予定のライン(「L-1」で入っていても「L1」を選んだ形で出す)
            "selected_line": _choice_of(plan.line, lines) if plan else "",
            # この端末で入れた・外したが、まだ共有へ届いていない
            "unsent": bool(plan and plan.plan_id in unsent)}


def _choice_of(line: str, choices: list) -> str:
    """そのラインに当たる選択肢(全角半角・ハイフンは見ない)。無ければ空。"""
    from packaging_tool import lot_plans
    key = lot_plans.line_key(line)
    return next((c for c in choices if lot_plans.line_key(c) == key), "")


def _lot_plan_summary(lot_no: str) -> str:
    """予定に添える一言(いま開いているロットなら製品寸法)。"""
    from packaging_tool import lot_plans
    context = work_context.get_context()
    if lot_plans.normalize_lot(context.lot_no) != lot_plans.normalize_lot(lot_no):
        return ""
    product = context.ribbon().get("product", "")
    return f"製品 {product}" if product and product != work_context.UNSET else ""


@field_only.get("/api/warehouse/lot-plan")
def lot_plan_get():
    """そのロット(`?lot_no=`。無ければいまのロット)の予定。"""
    lot_no = request.args.get("lot_no", "") or work_context.get_context().lot_no
    return jsonify(_lot_plan_state(get_db(), lot_no))


@field_only.post("/api/warehouse/lot-plan")
def lot_plan_send():
    """ロットを作業ラインに入れる(手元に足し、共有へ送る)。"""
    from packaging_tool import lot_plans
    body = request.get_json(silent=True) or {}
    lot_no = str(body.get("lot_no") or "") or work_context.get_context().lot_no
    line = str(body.get("line") or "")
    conn = get_db()
    from packaging_tool import location_service
    choices = location_service.base_point_names(conn)
    if line and not _choice_of(line, choices):
        return jsonify({"error": {"code": lot_plans.REFUSE_NO_LINE,
                                  "message": f"{line} は選べるラインにありません"
                                             f"({' / '.join(choices)})。"}}), 400
    line = _choice_of(line, choices) if line else line      # 選択肢の名前で残す
    result = lot_plans.send(conn, lot_no, line, terminal=svc.this_terminal(),
                            sender=access_control.current_identity().login_id,
                            summary=_lot_plan_summary(lot_no))
    if not result.ok:
        return jsonify({"error": {"code": result.reason, "message": result.message}}), 400
    if result.changed:
        data_sync.write_back_in_background()
    return jsonify({**_lot_plan_state(conn, lot_no), "message": result.message})


@field_only.post("/api/warehouse/lot-plan/cancel")
def lot_plan_cancel():
    """ロットを予定から外す。"""
    from packaging_tool import lot_plans
    body = request.get_json(silent=True) or {}
    lot_no = str(body.get("lot_no") or "") or work_context.get_context().lot_no
    conn = get_db()
    result = lot_plans.cancel(conn, lot_no, terminal=svc.this_terminal(),
                              sender=access_control.current_identity().login_id)
    if not result.ok:
        return jsonify({"error": {"code": result.reason, "message": result.message}}), 409
    data_sync.write_back_in_background()
    return jsonify({**_lot_plan_state(conn, lot_no), "message": result.message})

