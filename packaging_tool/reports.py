"""帳票の中身 (VBA `CreateLabelLayout` / `CreateCuttingRequestForm` の移植)

VBA版は Excel のシートにセル単位で値・結合・罫線を置き、`PrintOut` していた。
Python版は同じ紙面を `printing.py` のHTML基盤で組む。
このモジュールはHTML文字列を返すだけでtkinterには依存しないので、
GUIなしで内容を検証できる。

【セル結合をHTMLに写すときの方針】
Excelの結合(`Merge`)は `colspan`/`rowspan` にそのまま対応する。
列幅・行高も元の値(文字数・ポイント)から比率・mmに換算して指定するので、
紙面の見た目は元のシートとほぼ一致する。ただし
「結合してから内側の罫線を消して2段組みにする」という
VBAの組み方(切断依頼書の切断サイズ欄)だけは、HTMLでは入れ子の
ブロックで組んだほうが素直なのでそうしている(見た目は同じ)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional, Sequence

from . import lot_service, printing
from .board_selection_algorithm import ProtecCutResult
from .logging_utils import get_logger

log = get_logger("reports")

# Excelのポイント → mm (1pt = 1/72inch = 0.352777...mm)
PT_MM = 25.4 / 72.0

# ==================================================================
# 共通の書式
# ==================================================================
def format_thickness(value: float) -> str:
    """板厚。VBA `Format(vA, "0.00")`。"""
    return f"{value:.2f}"


def format_side(value: float) -> str:
    """幅・丈。VBA `IIf(v = Int(v), Format(v,"0"), Format(v,"0.0"))`。

    整数なら小数点を出さず、端数があるときだけ小数第1位まで出す。
    """
    return f"{value:.0f}" if float(value) == int(value) else f"{value:.1f}"


def product_size_text(thickness: float, width: float, length: float) -> str:
    """「50.00 × 1252 × 2502.5」形式(両帳票で共通)。"""
    return (f"{format_thickness(thickness)} × "
            f"{format_side(width)} × {format_side(length)}")


def _nv(caption: str) -> str:
    """VBA `NvCaption`。未取得を表す "---" は空文字にする。"""
    return "" if caption in ("---", None) else caption


def _lines(*parts: object) -> str:
    """複数行のセル内容(Excelの `vbCrLf`)をHTMLの改行にする。"""
    return "<br>".join(printing.escape(p) for p in parts)


# ==================================================================
# Lot貼付用 (VBA `CreateLabelLayout`)
# ==================================================================
# 元シートの行高(pt)。1行目だけ低く、6行目は少し低い
LABEL_ROW_HEIGHTS_PT = (37, 91, 91, 91, 91, 55, 91)

# 元シートの列幅から求めた各ブロックの幅比率。
# A(9.625) + B:J(6.25x9) = 65.875 が左半分、K(6.25)が仕切り、
# L(9.625) + M:O(6.25x3) + P(6.25) + Q:U(6.25x5) = 65.875 が右半分。
# (VBAは最後に `ws.Columns("K").Insert` で仕切り列を1本足している)
LABEL_W_LEFT = 47.74
LABEL_W_GAP = 4.53
LABEL_W_CAP1 = 6.97
LABEL_W_VAL1 = 13.59
LABEL_W_CAP2 = 4.53
LABEL_W_VAL2 = 22.64

LABEL_CSS = """
table.label { width: 100%; height: 100%; border-collapse: collapse;
              table-layout: fixed; }
table.label td { padding: 0 2px; overflow: hidden; }
td.lmain { text-align: center; vertical-align: middle; font-size: 38pt; }
td.gap { border-right: 1px solid #000; }
td.cap { font-size: 8pt; color: #505050; text-align: right;
         vertical-align: middle; white-space: pre-line; }
td.val { font-size: 12pt; font-weight: bold; text-align: left;
         vertical-align: middle; }
.vc  { font-size: 48pt; }
.wvc { text-align: right; }
.dim { font-size: 32pt; }
.pal { font-size: 24pt; text-align: right; }
.bare { font-size: 20pt; text-align: right; }
.pkg { font-size: 28pt; }
.lot { font-size: 50pt; }
.hiki { vertical-align: top; }
.addr { font-size: 8pt; font-weight: normal; }
.matcell { font-size: 12pt; font-weight: normal; }
.deliv { font-size: 11pt; color: #b40000; }
.factory { font-size: 11pt; color: #0000b4; }
.unit { font-size: 11pt; }
.code { font-size: 10pt; }
/* VC表が無いときの「K」丸印(VBA DrawCircleK: 枠線3pt・文字は円の80%) */
.circle-k { border: 3px solid #000; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-weight: bold; margin: 0 auto; }
"""


@dataclass
class LabelData:
    """Lot貼付用に載せる値一式(画面から集めたもの)。"""

    lot_no: str = ""
    # 左半分
    vc_front: str = ""          # lblOdr(6) VC_表
    vc_back: str = ""           # lblOdr(7) VC_裏
    thickness: float = 0.0      # lblLot(4)
    width: float = 0.0          # lblLot(5)
    length: float = 0.0         # lblLot(6)
    pallet_width: str = ""      # txtPaletteWidth
    pallet_length: str = ""     # txtPaletteLength
    pallet_industry: str = ""   # 一覧の選択行(業界)
    pallet_symbol: str = ""     # 一覧の選択行(記号。FilterPalSym適用後)
    total_packages: int = 0     # CalcTotalPackages
    pallet_unit: str = "台"     # 一覧の選択行(単位)。既定は「台」
    pallet_code: str = ""       # 一覧の選択行(発注コード)
    # 右半分
    yoto_code: str = ""         # lblLot(0)
    yoto_name: str = ""         # lblLot(1)
    zaishitsu: str = ""         # lblLot(2)
    choshitsu: str = ""         # lblLot(3)
    order_thickness: str = ""   # lblLot(7)
    order_width: str = ""       # lblLot(8)
    order_length: str = ""      # lblLot(9)
    delivery_name: str = ""     # lblOdr(0)
    customer_name: str = ""     # lblOdr(2)
    ship_to_name: str = ""      # lblOdr(3)
    delivery_comment: str = ""  # lblOdr(4)
    factory_comment: str = ""   # lblOdr(5)
    hiki_header: str = "引当数量/調整NO"   # hdrHiki1 の動的キャプション
    hiki_rows: list[tuple[str, str, str]] = field(default_factory=list)
    # 1P0113(裸梱包)
    is_1p0113: bool = False
    kakuzai_count_caption: str = ""   # lbl1P_KakCount
    matsuita_count_caption: str = ""  # lbl1P_MatCount
    kakuzai_size: str = ""            # lbl1P_KakuzaiDB
    matsuita_size: str = ""           # lbl1P_MatsutaDB
    kakuzai_cell: str = ""            # P1P0113KakCellText
    matsuita_cell: str = ""           # P1P0113MatCellText
    # 紙面で直した内容(`data-edit` の名前 → 文字)。別のロットを
    # 検索するまで残る(`selection_session.report_edits`)
    edits: dict[str, str] = field(default_factory=dict)


def hiki_header_for(type_flag: str) -> str:
    """引当欄の見出し(VBA `Page1_OnLstHikiClick` の `hdrHiki1.caption`)。

    調整NOの行なら「引当調整NO」、数量/全量なら「引当数量」、
    判別できなければ両方を示す既定文言にする。
    """
    if type_flag == lot_service.HIKI_ADJUSTED:
        return "引当調整NO"
    if type_flag in (lot_service.HIKI_QUANTITY, lot_service.HIKI_ALL):
        return "引当数量"
    return "引当数量/調整NO"


def build_hiki_text(header: str, rows: Sequence[tuple[str, str, str]]) -> str:
    """引当欄の本文(VBA `hikiText`)。1件ごとに3行、件の間は空行。"""
    if not rows:
        return "(引当なし)"
    blocks = [f"{header}：{disp}\n引当番号：{hiki_no}\n受注番号：{odr_no}"
              for disp, hiki_no, odr_no in rows]
    return "\n\n".join(blocks)


def _label_left_cells(data: LabelData) -> list[str]:
    """左半分(A:J)の7行分。各行は1セル(VBAは行ごとにA:Jを結合)。"""
    vc_front, vc_back = _nv(data.vc_front), _nv(data.vc_back)
    cells: list[str] = ["" for _ in range(7)]

    # 1行目は空。2〜3行目はVC表示、無ければ「K」の丸印
    if vc_front:
        cells[1] = f'<span class="vc">{printing.escape(vc_front)}</span>'
        if vc_front == vc_back:
            cells[2] = f'<div class="wvc">{printing.escape("WVC")}</div>'
    else:
        # VBA `DrawCircleK`: A2:J3 の高さの95%の円に、その80%の文字
        h = (LABEL_ROW_HEIGHTS_PT[1] + LABEL_ROW_HEIGHTS_PT[2]) * PT_MM
        size = h * 0.95
        cells[1] = (f'<div class="circle-k" style="width:{size:.1f}mm;'
                    f'height:{size:.1f}mm;font-size:{size * 0.8:.1f}mm">K</div>')

    cells[3] = (f'<span class="dim">'
                f'{printing.escape(product_size_text(data.thickness, data.width, data.length))}'
                f'</span>')

    if data.is_1p0113:
        # 裸梱包は角材・松板の本数/枚数を出す(パレット欄の代わり)
        cells[4] = (
            '<div class="bare">'
            + _lines("裸梱包",
                     f"{data.kakuzai_count_caption} ({data.kakuzai_size})",
                     f"{data.matsuita_count_caption} ({data.matsuita_size})")
            + "</div>")
        # 1P0113では梱包数(6行目)は出さない
    else:
        if data.pallet_width:
            prefix = " ".join(p for p in (data.pallet_industry, data.pallet_symbol) if p)
            size = f"{data.pallet_width} × {data.pallet_length}"
            body = _lines(prefix, f"  {size}") if prefix else printing.escape(size)
            cells[4] = f'<div class="pal">{body}</div>'
        # **梱包数は直せる。** 台数が確定できないまま貼ることがある
        cells[5] = (
            f'<span class="pkg">'
            f'{printing.editable("pkg", data.edits.get("pkg", str(data.total_packages or "")), placeholder="—")}'
            f'{printing.escape(data.pallet_unit)}</span>')

    cells[6] = f'<span class="lot">{printing.escape(data.lot_no)}</span>'
    return cells


def build_label_sheet(data: LabelData) -> str:
    """Lot貼付用の紙面1枚分のHTML。

    右半分の値と梱包数は**紙面で直せる**(現場の声:「Lot貼付け用も
    同様に」)。直した内容は別のロットを検索するまで残る。
    """
    def ed(key: str, value: object, *, placeholder: str = "") -> str:
        return printing.editable(key, data.edits.get(key, str(value)),
                                 placeholder=placeholder)

    def ed_lines(key: str, *parts: object) -> str:
        """複数行の値。1行ずつ別の欄にする(まとめると改行が壊れる)。"""
        return "<br>".join(ed(f"{key}{i}", part)
                           for i, part in enumerate(parts))

    left = _label_left_cells(data)
    h = [f"{pt * PT_MM:.1f}mm" for pt in LABEL_ROW_HEIGHTS_PT]
    # VC無しのときは丸印が2〜3行にまたがるので左セルを縦結合する
    circle = not _nv(data.vc_front)

    hiki_text = build_hiki_text(data.hiki_header, data.hiki_rows)
    # 引当が3件以上だと入りきらないので小さくする(VBA: >2 なら7pt)
    hiki_size = 7 if len(data.hiki_rows) > 2 else 9

    def left_td(row: int, extra: str = "") -> str:
        return (f'<td class="lmain" style="height:{h[row]}"{extra}>'
                f'{left[row]}</td>')

    rows: list[str] = []

    # --- 1〜3行目: 右は 用途/材質/オーダーサイズ + 縦結合の引当欄 ---
    rows.append(
        "<tr>"
        + left_td(0)
        + '<td class="gap" rowspan="7"></td>'
        + f'<td class="cap">{_lines("用途ｺｰﾄ:ﾞ", "用途:")}</td>'
        + f'<td class="val" colspan="3">{ed_lines("yoto", data.yoto_code, data.yoto_name)}</td>'
        + f'<td class="cap" rowspan="3">{printing.escape("引当等:")}</td>'
        + '<td class="val hiki" colspan="5" rowspan="3"'
        f' style="font-size:{hiki_size}pt">'
        + "<br>".join(printing.escape(line) for line in hiki_text.split("\n"))
        + "</td></tr>")
    rows.append(
        "<tr>" + left_td(1, ' rowspan="2"' if circle else "")
        + f'<td class="cap">{_lines("材質:", "調質:")}</td>'
        + f'<td class="val" colspan="3">{ed_lines("zai", data.zaishitsu, data.choshitsu)}</td>'
        + "</tr>")
    rows.append(
        "<tr>" + ("" if circle else left_td(2))
        + f'<td class="cap">{printing.escape("ｵｰﾀﾞｰｻｲｽﾞ:")}</td>'
        + '<td class="val" colspan="3">'
        + ed_lines("odr", data.order_thickness, data.order_width, data.order_length)
        + "</td></tr>")

    # --- 4行目: 宛先情報(右側全幅) ---
    rows.append(
        "<tr>" + left_td(3)
        + f'<td class="cap">{printing.escape("宛先情報:")}</td>'
        + '<td class="val addr" colspan="9">'
        + ed_lines("addr", f"納入：{data.delivery_name}",
                   f"取引：{data.customer_name}",
                   f"送り：{data.ship_to_name}")
        + "</td></tr>")

    # --- 5行目: 通常は単位/コード、1P0113は角材・松板の明細を半々に ---
    if data.is_1p0113:
        right5 = ('<td class="val matcell" colspan="4">'
                  + "<br>".join(printing.escape(x)
                                for x in data.kakuzai_cell.split("\n"))
                  + '</td><td class="val matcell" colspan="6">'
                  + "<br>".join(printing.escape(x)
                                for x in data.matsuita_cell.split("\n"))
                  + "</td>")
    else:
        right5 = (f'<td class="cap">{printing.escape("単位:")}</td>'
                  f'<td class="val unit" colspan="3">'
                  f'{ed("unit", data.pallet_unit)}</td>'
                  f'<td class="cap">{printing.escape("ｺｰﾄﾞ:")}</td>'
                  f'<td class="val code" colspan="5">'
                  f'{ed("code", data.pallet_code)}</td>')
    rows.append("<tr>" + left_td(4) + right5 + "</tr>")

    # --- 6・7行目: 納送コメント(赤) / 工場コメント(青) ---
    rows.append(
        "<tr>" + left_td(5)
        + f'<td class="cap">{printing.escape("納送ｺﾒﾝﾄ:")}</td>'
        + f'<td class="val deliv" colspan="9">'
        f'{ed("deliv", data.delivery_comment)}</td></tr>')
    rows.append(
        "<tr>" + left_td(6)
        + f'<td class="cap">{printing.escape("工場ｺﾒﾝﾄ:")}</td>'
        + f'<td class="val factory" colspan="9">'
        f'{ed("factory", data.factory_comment)}</td></tr>')

    widths = ([f"{LABEL_W_LEFT}%", f"{LABEL_W_GAP}%",
               f"{LABEL_W_CAP1}%"] + [f"{LABEL_W_VAL1 / 3:.2f}%"] * 3
              + [f"{LABEL_W_CAP2}%"] + [f"{LABEL_W_VAL2 / 5:.2f}%"] * 5)
    colgroup = "<colgroup>" + "".join(
        f'<col style="width:{w}">' for w in widths) + "</colgroup>"
    return f'<table class="label">{colgroup}{"".join(rows)}</table>'


def build_label_report(data: LabelData) -> printing.Report:
    """Lot貼付用の帳票(A4横)。

    VBA は余白0だったが、プリンターは紙の縁から約4mmに印刷できないので、
    縁の罫線や文字が欠けていた。**余白は `printing.SAFE_MARGIN_MM`(6mm)。**
    行の高さの合計(193mm)は余白を取っても紙(210 − 12mm)に収まる。
    """
    report = printing.Report(
        title=f"Lot貼付用 {data.lot_no}".strip(),
        setup=printing.PageSetup(orientation=printing.LANDSCAPE,
                                 margin_mm=printing.SAFE_MARGIN_MM,
                                 extra_css=LABEL_CSS),
    )
    report.add_sheet(build_label_sheet(data))
    return report


# ==================================================================
# 切断依頼書 (VBA `CreateCuttingRequestForm`)
# ==================================================================
# 丈方向のはみ出しがこれ以下なら丈カット不要とみなす(VBA `CUT_OVERL_THRESHOLD`)
CUT_OVERL_THRESHOLD = 100
# プロテックの切断許容は `board_selection_algorithm` の
# `PROTEC_1P1216_TOLERANCE` / `PROTEC_OTHER_TOLERANCE` が定義元(SSOT)。
# ここでは重複して持たない ── 選定側とカット依頼書側で別の値を
# 持つと、片方だけ直し忘れて選定結果と帳票が食い違う原因になる
# (現場の声で報告された不具合の一因)。`protec_cut_size_info` は
# `ProtecCutResult`(選定の確定値)をそのまま表示するだけなので、
# この関数自体はもう許容値を直接使わない。

CUT_CSS = """
table.cut { width: 100%; border-collapse: collapse; table-layout: fixed;
            font-size: 18pt; }
table.cut td { border: 1px solid #000; padding: 1mm 2mm;
               vertical-align: middle; }
td.title { font-size: 20pt; font-weight: bold; text-align: center;
           border: none; }
td.head { font-size: 18pt; }
td.side { text-align: center; font-size: 18pt; }
.value28 { font-size: 28pt; }
.center { text-align: center; }
/* 「切断サイズ」欄は上下2段。VBAは結合+内側罫線消し、こちらは入れ子で組む */
.block { display: flex; flex-direction: column; height: 100%; }
.block > div { flex: 1; display: flex; align-items: center; padding: 1mm 0; }
.block > div:first-child { border-bottom: 1px dotted #000; }
td.pack { padding: 0; }
.cutline { width: 100%; display: flex; align-items: baseline; }
.cutsize { font-size: 24pt; }
.orig { font-size: 11pt; color: #787878; margin-left: 2mm; }
.per { font-size: 20pt; margin-left: auto; padding-right: 2mm; }
.total { justify-content: center; font-size: 28pt; }
/* 切断サイズ見出しの左上セルは斜線(VBA `Borders(xlDiagonalDown)`) */
td.diag { background: linear-gradient(to bottom right,
          transparent calc(50% - 0.5px), #000 calc(50% - 0.5px),
          #000 calc(50% + 0.5px), transparent calc(50% + 0.5px)); }
"""


@dataclass
class CutSizeInfo:
    """VBA `GetCutSizeInfo` の出力(幅カットのみ / 幅+丈カット)。"""

    size_width_only: str = ""
    count_width_only: int = 0
    size_both: str = ""
    count_both: int = 0
    orig_width_only: str = ""
    orig_both: str = ""


TAG_CUT_PREMISE = "カット前提"


def sku_key_min_max(a: int, b: int) -> str:
    """向きに関係なく同じサイズを同じキーにする(短辺x長辺。VBA `SkuKeyMinMax`)。"""
    return f"{a}x{b}" if a <= b else f"{b}x{a}"


def cut_premise_skus(boards: Sequence[object]) -> dict[str, str]:
    """選定リストで「カット前提」の付いたサイズ(キー → リスト上の表記 `幅×丈`)。"""
    out: dict[str, str] = {}
    for b in boards:
        if (getattr(b, "tag", "") or "").strip() == TAG_CUT_PREMISE:
            out.setdefault(sku_key_min_max(b.width, b.length), f"{b.width}×{b.length}")
    return out


def get_cut_size_info(
    boards: Sequence[object], placed: Sequence[object], category: str,
    target_w: int, target_l: int, *, use_len_cut: bool = True,
    warn: Optional[Callable[[str], None]] = None,
) -> CutSizeInfo:
    """切断依頼の集計(プロテック以外。VBA `GetCutSizeInfo` の全面差し替え版)。

    【切断依頼の定義】
        対象 … 選定リストで「カット前提」の付いたサイズだけ(補填ボードは対象外)
        枚数 … 実際の配置で**切られている板だけ**を数える
               (別案では同じサイズでも、切るのは各行の最後の1枚だけのため)
        幅   … 配置された板の実際の幅(配置側が定義の幅で切っている。
               上用 = 製品幅 − 20 / 下用 = パレット幅ぴったり)
        丈   … 基準の丈(上用 = 製品丈 / 下用 = パレット丈)を超える分を切る。
               `use_len_cut=False` なら丈は切らない
        出力 … 「幅カットのみ」「丈カットあり」の2枠(依頼書のレイアウト)

    以前は選定のカット辞書と、旧ロジックの向き判定で寸法を計算し直していた
    ので、別案で配置すると**直前の通常選定のカットが載る・寸法が配置と合わない**
    ことがあった。

    1枠1サイズなので、枠に別サイズが来たら `warn` で知らせる(2つ目は載らない)。
    """
    from .placement_render import detect_board_cut

    out = CutSizeInfo()
    cut_sku = cut_premise_skus(boards)
    log.debug("get_cut_size_info[%s]: カット前提サイズ=%s種 基準=%sx%s 丈カット=%s",
              category, len(cut_sku), target_w, target_l, use_len_cut)
    if not cut_sku:
        return out

    for pb in placed:
        if pb.board_category != category or pb.is_fill_board:
            continue
        key = sku_key_min_max(pb.original_width, pb.original_length)
        if key not in cut_sku:
            continue
        # 幅: 配置で元寸法より細く切られているか
        cut = detect_board_cut(pb.original_width, pb.original_length, pb.width, pb.length)
        # 丈: 基準の丈をはみ出しているか
        len_over = max(0, (pb.x + pb.length) - target_l)
        has_w = cut.cut_width
        has_l = use_len_cut and (len_over > 0 or cut.cut_length)
        if not has_w and not has_l:
            continue                        # 同じサイズでも切らない板
        final_l = pb.length - len_over if has_l else pb.length
        # 切断後の幅は**配置された板の実際の幅**。別案では1行に複数枚並び、
        # 切るのは最後の1枚なので、その幅は「定義の幅 − 同じ行の他の板の幅」
        size = f"{pb.width}x{final_l}"
        if has_l:
            _add_cut_slot(out, "both", size, cut_sku[key], category, "丈カットあり", warn)
        else:
            _add_cut_slot(out, "width_only", size, cut_sku[key], category, "幅カットのみ", warn)

    log.debug("get_cut_size_info[%s] 結果: 幅カットのみ=[%s]x%s 元=[%s] 丈カットあり=[%s]x%s 元=[%s]",
              category, out.size_width_only, out.count_width_only, out.orig_width_only,
              out.size_both, out.count_both, out.orig_both)
    return out


def _add_cut_slot(out: CutSizeInfo, slot: str, size: str, orig: str,
                  category: str, slot_name: str,
                  warn: Optional[Callable[[str], None]]) -> None:
    """切断依頼の1枠に1枚加える(VBA `AddCutSlot`)。

    依頼書は1枠1サイズなので、枠に別サイズが来たら知らせる(2つ目は載らない)。
    """
    size_attr, count_attr, orig_attr = f"size_{slot}", f"count_{slot}", f"orig_{slot}"
    current = getattr(out, size_attr)
    if not current:
        setattr(out, size_attr, size)
        setattr(out, orig_attr, orig)
        setattr(out, count_attr, 1)
    elif current == size:
        setattr(out, count_attr, getattr(out, count_attr) + 1)
    else:
        message = (f"[切断依頼][{category}][警告] {slot_name}に別サイズの板があります"
                   f"({current} と {size})。依頼書には {current} だけ載ります")
        log.warning(message)
        if warn is not None:
            warn(message)


@dataclass
class CutTargets:
    """切断依頼の対象と対象外(プロテック以外。VBA `CheckCutTargets`)。"""

    count: int = 0                         # 対象の枚数(実際に切るカット前提の板)
    others: list[str] = field(default_factory=list)   # 対象外(選定ログに出す行)


def check_cut_targets(boards: Sequence[object], placed: Sequence[object],
                      category: str, base_w: int, base_l: int) -> CutTargets:
    """対象の枚数と、対象外(はみ出し・配置上のカット)を数える。

        対象   … カット前提のサイズで、実際に切られている板(`get_cut_size_info` と同じ判定)
        対象外 … カット前提ではない板で、はみ出し・配置上のカットがあるもの
                 (PASS3 で20%まで許したはみ出し、最後の1枚の丈はみ出し等)
    補填ボードはどちらにも含めない。
    """
    from .placement_render import detect_board_cut

    cut_sku = cut_premise_skus(boards)
    result = CutTargets()
    others: dict[tuple[str, str], int] = {}
    for pb in placed:
        if pb.board_category != category or pb.is_fill_board:
            continue
        key = sku_key_min_max(pb.original_width, pb.original_length)
        cut = detect_board_cut(pb.original_width, pb.original_length, pb.width, pb.length)
        over_w = max(0, (pb.y + pb.width) - base_w)
        over_l = max(0, (pb.x + pb.length) - base_l)
        if key in cut_sku:
            if cut.cut_width or cut.cut_length or over_l > 0:
                result.count += 1
            continue
        reason = ""
        if over_w > 0:
            reason += f" 幅はみ出し{over_w}mm"
        if over_l > 0:
            reason += f" 丈はみ出し{over_l}mm"
        if cut.cut_width:
            reason += f" 幅カット{cut.amount_width}mm"
        if cut.cut_length:
            reason += f" 丈カット{cut.amount_length}mm"
        if reason:
            k = (f"{pb.original_width}x{pb.original_length}", reason)
            others[k] = others.get(k, 0) + 1
    result.others = [f"[切断依頼対象外] {category} {size} {n}枚:{reason}"
                     "(カット前提ではないため依頼しません)"
                     for (size, reason), n in others.items()]
    log.debug("check_cut_targets[%s]: 対象=%s枚 対象外=%s種", category,
              result.count, len(others))
    return result


# プロテックは**上用=下用と同サイズを強制コピー**する仕様なので、
# 現物は上下2セット切り出すことになる。`ProtecCutResult` が持っている
# のは1セット分の枚数なので、依頼書に出す枚数はここで2倍する。
# **依頼書だけの話**で、選定・配置の枚数(1セット分)は変えない
PROTEC_SETS = 2


def _len_split(pr: ProtecCutResult) -> tuple[int, int]:
    """(丈カットしない枚数, 丈カットする枚数)。

    確定値が内訳(`len_normal_cnt`/`len_cut_cnt`)を持っていればそれを
    使い、**持っていなければ枚数から割り出す。** 内訳を入れるのは
    `compute_protec_length_cut` を通った確定値だけなので、そこを
    通らずに組み立てられた確定値(上用のコピーなど)でも枚数が0に
    ならないようにしておく ── 依頼書の枚数が黙って0になるのは、
    **カットが要らないのと見分けが付かない**いちばん危ない壊れ方
    """
    if pr.len_normal_cnt or pr.len_cut_cnt:
        return pr.len_normal_cnt, pr.len_cut_cnt
    cut_cnt = 1 if pr.need_length_cut else 0
    return max(0, pr.count - cut_cnt), cut_cnt


def protec_cut_size_info(
    protec_result: ProtecCutResult, *, use_len_cut: bool = True,
) -> CutSizeInfo:
    """プロテックモードの切断サイズ(VBA `CreateCuttingRequestForm` の分岐)。

    【全面書き換え】以前はここで `placedBoards`(配置座標)から寸法を
    逆算し、`GetBestOrientation` で幅カット・丈カットの要否を
    独自に再計算していた。選定(`select_protec_lower_boards` /
    `select_upper_boards`)が既に `ProtecCutResult` として確定させた
    値があるのに、ここでもう一度判定をやり直すと、丸め方や境界条件の
    違いで選定結果と食い違う帳票が出てしまう(現場の声で報告された、
    カット依頼が正しく出力されない不具合の一因)。

    いまは再計算をせず、`ProtecCutResult` の値をそのまま表示用の形
    (`CutSizeInfo`)に写すだけ。`get_cut_size_info`(通常モード)と
    同じ「幅カットのみ/幅+丈カット」の2枠構成に合わせる:

        丈カットも要る → 最後の1枚は `size_both`(丈カット後の短い方の
        丈で仕上がる)。残りの枚数はカット済みの通常サイズなので
        `size_width_only` に入れる(幅カットが無ければ`size_width_only`
        自体は出さない ── 幅は元のサイズのままで良いため)
        丈カットが要らない → 幅カットの有無だけで `size_width_only`
        に全枚数をまとめる

    `use_len_cut`(既定True)がFalseのとき、丈カットが要るケースでも
    丈カットせず、その1枚も幅カットのみの枠(通常の実効丈)へ合算する
    (`get_cut_size_info` の同名パラメータと揃えた挙動)。
    """
    out = CutSizeInfo()
    if not protec_result.valid:
        return out

    pr = protec_result
    if pr.len_cut_optional and use_len_cut:
        # 「切る」を選んだ。**確定値のほうは切らないまま**にしておく ──
        # 配置図は切らない姿で描かれており、依頼書だけをカットありに
        # 切り替える(切るかどうかは現場が選ぶことで、図の前提ではない)
        need_length_cut = True
        cnt_width_only = pr.len_opt_normal_cnt * PROTEC_SETS
        cnt_both = pr.len_opt_cut_cnt * PROTEC_SETS
        cut_after = pr.len_opt_cut_eff
        log.debug("protec_cut_size_info[任意カット採用]: 通常%s枚+カット%s枚→%smm",
                  cnt_width_only, cnt_both, cut_after)
    else:
        need_length_cut = pr.need_length_cut
        normal_cnt, cut_cnt = _len_split(pr)
        cnt_width_only = normal_cnt * PROTEC_SETS
        cnt_both = cut_cnt * PROTEC_SETS
        cut_after = pr.length_cut_eff

    # 丈カットなし指定なら丈カット分を通常カットへ合算する
    if not use_len_cut and need_length_cut:
        cnt_width_only = cnt_width_only + cnt_both if pr.need_cut else 0
        need_length_cut = False
        cnt_both = 0
        log.debug("protec_cut_size_info[丈カットなし] 合算後 countWidthOnly=%s", cnt_width_only)

    if need_length_cut:
        out.size_both = f"{pr.cut_eff_width}x{cut_after}"
        out.count_both = cnt_both
        out.orig_both = f"{pr.orig_width}×{pr.orig_length}"

    if pr.need_cut and cnt_width_only > 0:
        out.size_width_only = f"{pr.cut_eff_width}x{pr.eff_length}"
        out.count_width_only = cnt_width_only
        out.orig_width_only = f"{pr.orig_width}×{pr.orig_length}"

    log.debug("protec_cut_size_info: cutEffW=%s effL=%s needCut=%s "
             "needLengthCut=%s lengthCutEff=%s count=%s optional=%s",
             pr.cut_eff_width, pr.eff_length, pr.need_cut,
             pr.need_length_cut, pr.length_cut_eff, pr.count, pr.len_cut_optional)
    return out


@dataclass
class CutRequestData:
    """切断依頼書に載せる値一式。"""

    lot_no: str = ""
    position: str = ""          # 拠点(L1 は "L-1" に整形して使う)
    board_type: str = ""        # cboBoardType。空なら「ボード」
    thickness: float = 0.0
    width: float = 0.0
    length: float = 0.0
    pallet_width: str = ""
    pallet_length: str = ""
    total_packages: int = 0
    is_protec: bool = False
    upper: CutSizeInfo = field(default_factory=CutSizeInfo)
    lower: CutSizeInfo = field(default_factory=CutSizeInfo)
    request_date: Optional[date] = None
    # 見出しの頭に足す拠点名。**既定は空**(現場の指示で拠点名を
    # 付けるのをやめた)。要るときだけ紙面で入れる
    title_prefix: str = ""
    # 紙面で直した内容(`data-edit` の名前 → 文字)。別のロットを
    # 検索するまで残る(`selection_session.report_edits`)
    edits: dict[str, str] = field(default_factory=dict)


def format_position(position: str) -> str:
    """VBA `If posName = "L1" Then posName = "L-1"`。"""
    return "L-1" if position == "L1" else position


def cut_request_title(board_type: str) -> str:
    """「プロテックボード切断依頼書」。種別が取れなければ「ボード」。

    **拠点名は付けない。** 以前は先頭に付けていたが、出す拠点は決まって
    いないことがあり、要るときだけ人が頭に足すほうが正しい(現場の指示)。
    見出しの手前には直せる欄を置いてあるので、そこへ入れる。
    """
    return f"{board_type or 'ボード'}切断依頼書"


def _total_text(count: int, total_packages: int) -> str:
    """総枚数。梱包数が出ていれば掛ける(VBA踏襲)。"""
    if count <= 0:
        return ""
    return str(count * total_packages if total_packages > 0 else count)


def _cut_block(info: CutSizeInfo, total_packages: int, *, show_orig: bool,
               ed=None, prefix: str = "") -> tuple[str, str]:
    """切断サイズ欄(B:C)と総枚数欄(D)の2段組みを作る。

    `ed` を渡すと、寸法と枚数を**紙面で直せる**ようにする(現場の声:
    「サイズを微調整したい」)。空の行にも欄だけ出しておく ── 計算に
    出てこなかった切り出しを手で足せるようにするため。
    """
    def line(size: str, orig: str, count: int, name: str) -> str:
        cell = (printing.escape(size) if ed is None
                else ed(f"{prefix}{name}_size", size, placeholder="幅x丈"))
        if ed is None and not size:
            return "<div></div>"
        orig_html = (f'<span class="orig">（元: {printing.escape(orig)}）</span>'
                     if show_orig and orig else "")
        per = (f'<span class="per">1梱{count}枚</span>' if count > 0 else "")
        return (f'<div><span class="cutline">'
                f'<span class="cutsize">{cell}</span>'
                f"{orig_html}{per}</span></div>")

    def total(count: int, name: str) -> str:
        text = _total_text(count, total_packages)
        cell = (printing.escape(text) if ed is None
                else ed(f"{prefix}{name}_count", text))
        return f'<div class="total">{cell}</div>'

    sizes = ('<div class="block">'
             + line(info.size_width_only, info.orig_width_only,
                    info.count_width_only, "w")
             + line(info.size_both, info.orig_both, info.count_both, "b")
             + "</div>")
    totals = ('<div class="block">'
              + total(info.count_width_only, "w")
              + total(info.count_both, "b")
              + "</div>")
    return sizes, totals


def build_cut_request_sheet(data: CutRequestData) -> str:
    """切断依頼書の紙面1枚分のHTML。"""
    d = data.request_date or date.today()
    # 元シートの列幅 A=8 B=35 C=35 D=15 (合計93)
    colgroup = ("<colgroup>"
                '<col style="width:8.6%"><col style="width:37.6%">'
                '<col style="width:37.6%"><col style="width:16.2%">'
                "</colgroup>")
    esc = printing.escape
    rows: list[str] = []

    def ed(key: str, value: object, *, placeholder: str = "") -> str:
        """紙面で直せる欄。**直した内容があればそちらを出す。**"""
        return printing.editable(key, data.edits.get(key, str(value)),
                                 placeholder=placeholder)

    rows.append(f'<tr><td class="title" colspan="4" style="height:21.2mm">'
                f'{ed("title_prefix", data.title_prefix, placeholder="拠点")} '
                f'{esc(cut_request_title(data.board_type))}</td></tr>')
    rows.append('<tr><td class="head" colspan="3">ロットNO</td>'
                '<td class="head center">担当者</td></tr>')
    rows.append(f'<tr><td colspan="3" style="height:21.2mm">'
                f'<span class="value28">{esc(data.lot_no)}</span></td>'
                f'<td class="center">{ed("tantou", "", placeholder="—")}</td></tr>')
    rows.append('<tr><td class="head" colspan="4">製品サイズ</td></tr>')
    rows.append(f'<tr><td class="center" colspan="4" style="height:21.2mm">'
                f'<span class="value28">'
                f'{ed("product_size", product_size_text(data.thickness, data.width, data.length))}'
                f"</span></td></tr>")
    rows.append('<tr><td class="head" colspan="3">パレットサイズ</td>'
                '<td class="head center">台</td></tr>')
    pallet = (f"{data.pallet_width} × {data.pallet_length}"
              if data.pallet_width else "")
    rows.append(f'<tr><td class="center" colspan="3" style="height:21.2mm">'
                f'<span class="value28">{ed("pallet_size", pallet)}</span></td>'
                f'<td class="center"><span class="value28">'
                f'{ed("total_packages", data.total_packages)}</span></td></tr>')
    rows.append('<tr><td class="diag"></td>'
                '<td class="head" colspan="2">切断サイズ</td>'
                '<td class="head center">枚数</td></tr>')

    # 切断サイズ本体。プロテックは上下を分けないので見出しの「上」「下」を出さない
    if data.is_protec:
        sections = [("", data.upper), ("", CutSizeInfo())]
        show_orig = False
    else:
        sections = [("上", data.upper), ("下", data.lower)]
        show_orig = True
    for index, (side, info) in enumerate(sections):
        sizes, totals = _cut_block(info, data.total_packages, show_orig=show_orig,
                                   ed=ed, prefix=f"cut{index}_")
        rows.append(f'<tr><td class="side" style="height:31.8mm">{esc(side)}</td>'
                    f'<td class="pack" colspan="2">{sizes}</td>'
                    f'<td class="pack">{totals}</td></tr>')

    rows.append('<tr><td class="head" colspan="2">依頼日</td>'
                '<td class="head" colspan="2">期限日</td></tr>')
    rows.append(f'<tr><td class="center" colspan="2" style="height:21.2mm">'
                f'{ed("request_date", f"{d:%Y/%m/%d}")}</td>'
                f'<td class="center" colspan="2">'
                f'{ed("due_date", "", placeholder="年/月/日")}</td></tr>')

    return f'<table class="cut">{colgroup}{"".join(rows)}</table>'


def build_cut_request_report(data: CutRequestData) -> printing.Report:
    """切断依頼書の帳票(A4縦・余白1cm)。VBA `PageSetup` と同じ設定。"""
    report = printing.Report(
        title=f"切断依頼書 {data.lot_no}".strip(),
        setup=printing.PageSetup(orientation=printing.PORTRAIT, margin_mm=10.0,
                                 extra_css=CUT_CSS),
    )
    report.add_sheet(build_cut_request_sheet(data))
    return report


__all__ = [
    "LabelData", "build_label_report", "build_label_sheet", "build_hiki_text",
    "CutRequestData", "CutSizeInfo", "build_cut_request_report",
    "build_cut_request_sheet", "get_cut_size_info", "protec_cut_size_info",
    "check_cut_targets", "sku_key_min_max", "CutTargets",
    "cut_request_title", "format_position", "product_size_text",
    "format_thickness", "format_side", "hiki_header_for",
]
