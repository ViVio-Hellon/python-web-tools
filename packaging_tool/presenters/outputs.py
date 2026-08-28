"""資材選択から「出す」もの ── 帳票と倉庫送信

VBA `btnPrint_Click` / `btnCutRequest_Click` /
`build_warehouse_orders` から**判断とデータ組み立てを抜き出した**もの。

【なぜ画面から抜くのか】
どれも「いまの選定内容から1件のデータを組み立てる」処理で、
ウィジェットには一切依存していない。tkinter版は入力欄やツリーの選択から
直接値を読んでいたが、その読み取りは**セッションが持っている値**に
置き換えられる。抜いておけば画面なしで検証できる(Phase 8 で `ui/` を
消したあとも残る)。

【文言はここが持つ】
「先にロット検索でロットを確定してください。」のような断りの文言は
サーバが持つ(設計書 §6.1)。tkinter版の `messagebox` の文言をそのまま
移してあるので、現場が覚えている言葉が変わらない。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .. import lot_service, printing, reports
from .. import special_packaging as spk
from ..logging_utils import get_logger

log = get_logger("presenters.outputs")

# 帳票の種類。URL は `app/routes/selection.py` が登録するもの
REPORT_LABEL = "label"
REPORT_CUT = "cut-request"
REPORT_PLAN = "plan"


@dataclass
class Refusal:
    """断り。`reason` は呼び出し側がHTTPステータスを決めるための種別。"""

    message: str
    reason: str


# 断りの種類(`selection_session` と同じ語を使う)
NEEDS_LOT = "needs_lot"
NEEDS_SIZES = "needs_sizes"
NEEDS_BOARDS = "no_candidates"
NOT_FOUND = "not_found"


# ==================================================================
# 押せるかどうか
# ==================================================================
def label_refusal(session: Any) -> Optional[Refusal]:
    """Lot印刷が出せない理由(VBA `btnPrint_Click` の冒頭)。"""
    if session.presenter.lot_result is None:
        return Refusal("先にロット検索でロットを確定してください。", NEEDS_LOT)
    return None


def cut_request_refusal(session: Any) -> Optional[Refusal]:
    """切断依頼が出せない理由(VBA `btnCutRequest_Click` の冒頭)。

    「カットが要らないので出せない」は**押してみるまで分からない**。
    選定結果にカットの記録があるかどうかは組み立ててみて初めて決まるので、
    ここでは前提だけを見る。
    """
    if session.presenter.lot_result is None:
        return Refusal("先にロット検索でロットを確定してください。", NEEDS_LOT)
    if not (session.selected.upper or session.selected.lower):
        return Refusal("先にボードを選定してください。", NEEDS_BOARDS)
    return None


def plan_refusal(session: Any) -> Optional[Refusal]:
    """配置図印刷が出せない理由。

    候補を選んだだけでは出せない ── **配置してあること**が前提。
    配置は何度でも試せる操作なので、印刷できる=実際に配置した、まで
    絞っておく(使用実績はこの印刷のタイミングで積む。`board_usage`)。
    """
    if session.presenter.lot_result is None:
        return Refusal("先にロット検索でロットを確定してください。", NEEDS_LOT)
    if session.placement is None or not session.placement.placed:
        return Refusal("先にボードを配置してください。", NEEDS_BOARDS)
    return None


def send_refusal(session: Any) -> Optional[Refusal]:
    """倉庫送信が出せない理由(VBA `btnSendToWarehouse_Click` の検証)。

    1P0113 はパレットを使わないのでパレット未設定でも送れる
    (VBA も `If Not Me.Is1P0113Mode Then` でパレット検証を飛ばす)。

    **EX受注も同じくパレット未設定で送れる。** EXの実データは別の職場から
    別途届き、そちらが優先される。こちらから送るのは「EXである」と
    分かる最小限の合図だけなので、パレットも発注コードも要らない
    (`build_orders` が中身を EX 用に差し替える)。
    """
    if session.presenter.lot_result is None:
        return Refusal("先にロット検索でロットを確定してください。", NEEDS_LOT)

    if session.presenter.is_ex_order:
        return None

    if session.presenter.mode_1p0113:
        m = session.presenter.materials_1p0113
        if not (m.kakuzai.is_ok and m.matsuita.is_ok):
            return Refusal(
                "角材・松板をマスタから引けていません。"
                "製品サイズを設定してから送信してください。", NEEDS_SIZES)
        return None

    if not session.palette.is_set:
        return Refusal("パレットを設定してください。", NEEDS_SIZES)
    if session.pallet_row is None or not session.pallet_row.code:
        # 発注コードはパレット一覧の行にしか無い。未選択だと登録できないので
        # 「必須項目が空です」より具体的に案内する
        return Refusal(
            "パレット一覧から該当行を選んでください。"
            "発注コードと単位は一覧の行から取ります。", NEEDS_SIZES)
    return None


# ==================================================================
# 倉庫送信
# ==================================================================
# EX受注のときに送る品名。**これだけが倉庫側への合図**になる
EX_HINMEI = "EX"


def build_orders(session: Any) -> list[dict[str, Any]]:
    """送信する行を組み立てる(VBA `g_SendRows`)。

    通常は1行、1P0113 は角材と松板の2行。前提が満たされていることは
    `send_refusal()` で先に確かめてある。
    """
    if session.presenter.is_ex_order:
        # **EX受注は中身を送らない。** 実データは別の職場から別途届き、
        # そちらが優先される。こちらから半端な値を送ると突き合わせで
        # 混乱するので、EXだと分かる最小限だけにする。
        #
        # **組み立てたあとに上から潰す**(VBA の `g_SendRows` 上書きと
        # 同じ形)。どの組み立てを通っても最後にここへ来るので、
        # **それぞれの組み立てには一切手を入れない**
        return [_as_ex_order(row) for row in _ex_source_rows(session)]
    return _build_rows(session)


def _build_rows(session: Any) -> list[dict[str, Any]]:
    if session.presenter.mode_1p0113:
        return _orders_1p0113(session)
    return [_order_normal(session)]


def _ex_source_rows(session: Any) -> list[dict[str, Any]]:
    """EX受注で土台にする行。中身は `_as_ex_order` が潰すので、器だけでよい。

    **パレットを決めずに送れるのがEXの要点**(`send_refusal`)なので、
    通常の組み立て(`_order_normal`)はそのままでは通らない ── あちらは
    品名と発注コードをパレット一覧の行から作るため、行が無ければ落ちる。
    パレットまで決めてあるならその組み立てを通し、決めていなければ
    ロットと寸法だけの器を作る。**どちらの組み立ても書き換えない。**
    """
    if session.presenter.mode_1p0113:
        return _orders_1p0113(session)
    if session.palette.is_set and session.pallet_row is not None:
        return [_order_normal(session)]
    return [{"lot_no": session.presenter.lot_result.lot.lot_no, **_common(session)}]


def _as_ex_order(row: dict[str, Any]) -> dict[str, Any]:
    """1行をEX受注の送信内容にする。

    発注コード・単位・発注数は空、品名は `EX` 固定。ロット番号と寸法は
    残す ── **どのロットのEXなのかが分からないと、届いた側で
    突き合わせられない。**
    """
    return {**row,
            "hinmei": EX_HINMEI,
            "hatchu_code": "",
            "tani": "",
            "hatchu_suu": "",
            # 受け側(倉庫連携の下書き)が発注数の必須検査を外すための印。
            # 文言から推し量らせない(`hinmei == "EX"` で判定しない)
            "is_ex_order": True}


def _common(session: Any) -> dict[str, Any]:
    """通常と1P0113で共通の欄。

    幅・丈は**ロットの板幅・板丈**(VBA `GetLblLotCaption(5)/(6)`)。
    製品サイズはパレットに載せる都合で回転していることがあり、
    それを送ると倉庫側に板の寸法が入れ替わって伝わる。
    """
    result = session.presenter.lot_result
    lot, odr = result.lot, result.odr
    return {
        "zaisitu": lot.zaishitsu,
        "choshitu": lot.choshitsu,
        "atu": lot_service.format_thickness(lot.thickness),
        "haba": lot_service.format_dimension(lot.width),
        "take": lot_service.format_dimension(lot.length),
        "nounyusaki": odr.delivery_name,
        "yoto_code": lot.yoto_code,
    }


def _order_normal(session: Any) -> dict[str, Any]:
    result = session.presenter.lot_result
    lot = result.lot
    row = session.pallet_row

    hinmei = lot_service.build_hinmei(
        row.industry, row.symbol, session.palette.width, session.palette.length)
    # 発注数は梱包数の見積り。計算不可(-1)や0のときは空欄にする(VBA踏襲)
    packages = lot_service.calc_total_packages(result)
    return {
        "lot_no": lot.lot_no,
        "hinmei": hinmei,
        "hatchu_code": row.code,
        "tani": row.unit,
        "hatchu_suu": str(packages) if packages > 0 else "",
        **_common(session),
    }


def _orders_1p0113(session: Any) -> list[dict[str, Any]]:
    """VBA `btnSendToWarehouse_Click` の1P0113分岐(2行)。

    品名・発注コード・単位は `MaterialHit.full_info` から切り出す。
    **品名に入る丈はマスタの丈帯**(例 "1001-1999")で、実際の切断長では
    ない。発注コードが帯ごとに分かれているため帯の表記で足りる、という
    VBA側の判断をそのまま引き継いでいる。
    """
    lot = session.presenter.lot_result.lot
    materials = session.presenter.materials_1p0113
    qty = session.qty_1p0113
    common = _common(session)

    rows = []
    for hit, size_label, count in (
        (materials.kakuzai, spk.KAKUZAI_SIZE_LABEL, materials.kakuzai_count(qty)),
        (materials.matsuita, spk.MATSUITA_SIZE_LABEL, materials.matsuita_count(qty)),
    ):
        info = hit.full_info
        rows.append({
            "lot_no": lot.lot_no,
            "hinmei": f"{size_label} 丈:{spk.parse_info(info, '丈')}",
            "hatchu_code": spk.parse_info(info, "CD"),
            "tani": spk.parse_info(info, "単位"),
            "hatchu_suu": str(count),
            **common,
        })
    return rows


# ==================================================================
# 帳票
# ==================================================================
def _pallet_info(session: Any) -> tuple[str, str, str, str]:
    """一覧の選択行から (業界, 記号, 発注コード, 単位) を採る。

    記号は `FilterPalSym` 相当のフィルタを通す(G1/G2/A2〜A8は落とす)。
    未選択なら全部空で、単位だけ既定の「台」にする(VBA踏襲)。
    """
    row = session.pallet_row
    if row is None:
        return "", "", "", "台"
    return (row.industry, lot_service.filter_pallet_symbol(row.symbol),
            row.code, row.unit or "台")


def _package_count(session: Any) -> int:
    """梱包数。計算不可(-1)は0扱いにする(VBA踏襲)。"""
    result = session.presenter.lot_result
    if result is None:
        return 0
    count = lot_service.calc_total_packages(result)
    if count < 0:
        reason = lot_service.calc_total_packages_reason(result)
        session.presenter.user_log.log(f"梱包数:計算不可({reason})")
        return 0
    return count


def build_label(session: Any) -> printing.Report:
    """Lot印刷(VBA `btnPrint_Click` → `CreateLabelLayout`)。"""
    presenter = session.presenter
    # 1P0113は印刷直前に資材を引き直す(VBA `CreateLabelLayout` の冒頭)
    if presenter.mode_1p0113:
        session.reload_1p0113_materials()

    result = presenter.lot_result
    lot, odr, hiki = result.lot, result.odr, result.hiki
    industry, symbol, code, unit = _pallet_info(session)
    materials = presenter.materials_1p0113
    qty = session.qty_1p0113

    data = reports.LabelData(
        lot_no=lot.lot_no,
        vc_front=odr.vc_front, vc_back=odr.vc_back,
        thickness=lot.thickness, width=lot.width, length=lot.length,
        pallet_width=str(session.palette.width) if session.palette.is_set else "",
        pallet_length=str(session.palette.length) if session.palette.is_set else "",
        pallet_industry=industry, pallet_symbol=symbol,
        total_packages=_package_count(session),
        pallet_unit=unit, pallet_code=code,
        yoto_code=lot.yoto_code, yoto_name=lot.yoto_name,
        zaishitsu=lot.zaishitsu, choshitsu=lot.choshitsu,
        order_thickness=lot_service.format_thickness(lot.order_thickness),
        order_width=lot_service.format_dimension(lot.order_width),
        order_length=lot_service.format_dimension(lot.order_length),
        delivery_name=odr.delivery_name, customer_name=odr.customer_name,
        ship_to_name=odr.ship_to_name,
        delivery_comment=odr.delivery_comment,
        factory_comment=odr.factory_comment,
        # 見出しは先頭行の種別で決まる(印刷は全行を同じ見出しで並べる)
        hiki_header=reports.hiki_header_for(hiki[0].type_flag if hiki else ""),
        hiki_rows=[(r.display_value, r.hiki_no, r.order_no) for r in hiki],
        is_1p0113=presenter.mode_1p0113,
        kakuzai_count_caption=spk.kakuzai_count_caption(materials.kakuzai_count(qty)),
        matsuita_count_caption=spk.matsuita_count_caption(materials.matsuita_count(qty)),
        kakuzai_size=materials.kakuzai_label,
        matsuita_size=materials.matsuita_label,
        kakuzai_cell=materials.kakuzai.cell_text,
        matsuita_cell=materials.matsuita.cell_text,
    )
    return reports.build_label_report(data)


def build_cut_request(session: Any, *, use_len_cut: bool = True) -> tuple[
        Optional[printing.Report], Optional[Refusal]]:
    """切断依頼(VBA `btnCutRequest_Click` → `CreateCuttingRequestForm`)。

    カットが1つも無ければ帳票にならない。**それは失敗ではない**ので、
    理由を添えて返す(押した人には「不要だった」と分かる必要がある)。

    切断依頼は配置を実行していなくても押せる(`cut_request_refusal` は
    配置済みを条件にしない)ため、`session.placement`(配置後にしか
    存在しない)には頼れない。`place_boards` と同じ手順
    (`select_result` があればそこから、手動で増減した後なら後付け
    適用)を、ここでも独立して行い、`ProtecCutResult` を用意する。

    `use_len_cut`(既定True)は、VBA側で追加された「丈カットを行いますか」
    という確認(押した人が選ぶ)に対応する。Falseなら丈カット分を
    幅カットのみへ合算して出す(`reports.get_cut_size_info`/
    `reports.protec_cut_size_info` の同名パラメータ参照)。
    """
    from .. import board_selection_algorithm as alg
    from .. import user_settings

    presenter = session.presenter
    lot = presenter.lot_result.lot
    result = session.select_result
    cut_info = result.cut_info if result else {}
    length_cut_info = result.length_cut_info if result else {}
    length_cut_count = result.length_cut_count if result else {}

    if presenter.protec.is_protec:
        # プロテックは上下を分けず、選定(または手動追加後の後付け)が
        # 確定させた `ProtecCutResult` をそのまま表示するだけ
        # (`reports.protec_cut_size_info` のdocstring参照)。
        if result is not None:
            protec_result = result.lower_result.protec_result
        else:
            protec_result = alg.apply_protec_rules_to_lower_list(
                session.selected.lower, session.product, session.palette.length,
                is_1p1216=presenter.protec.is_1p1216)
        upper = reports.protec_cut_size_info(protec_result, use_len_cut=use_len_cut)
        lower = reports.CutSizeInfo()
    else:
        upper = reports.get_cut_size_info(
            session.selected.upper, "上用",
            session.product.width, session.product.length,
            cut_info, length_cut_info, length_cut_count, use_len_cut=use_len_cut)
        lower = reports.get_cut_size_info(
            session.selected.lower, "下用",
            session.palette.width, session.palette.length,
            cut_info, length_cut_info, length_cut_count, use_len_cut=use_len_cut)

    if not any((upper.size_width_only, upper.size_both,
                lower.size_width_only, lower.size_both)):
        return None, Refusal(
            "カットが必要なボードはありません。"
            "(選定結果に幅カット・丈カットの記録がないため依頼は不要です)",
            NOT_FOUND)

    data = reports.CutRequestData(
        lot_no=lot.lot_no,
        position=user_settings.get_position(),
        board_type=session.board_type,
        thickness=lot.thickness, width=lot.width, length=lot.length,
        pallet_width=str(session.palette.width) if session.palette.is_set else "",
        pallet_length=str(session.palette.length) if session.palette.is_set else "",
        total_packages=_package_count(session),
        is_protec=presenter.protec.is_protec,
        upper=upper, lower=lower,
    )
    return reports.build_cut_request_report(data), None
