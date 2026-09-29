"""配置図の描画計画 (VBA `MaterialMasterForm` の描画系の移植)

`placement_algorithm` が決めた配置(mm単位の論理座標)を、図の上の
座標・色・キャプションに変換する。**描き方は知らない**純粋な計算なので、
画面なしで単体テストできる。実際に描くのはブラウザ
(`app/static/js/svgplan.js` が SVG を組み立てる)。

対応関係:
    DetectBoardCut             -> detect_board_cut
    GetMainBoardsBoundingBox    -> main_boards_bounding_box
    GetNarrowPaletteBoundingBox  -> narrow_bounding_box
    GetBoardColor                 -> board_color
    DrawPaletteBorder              -> RenderPlan.border
    DrawSingleBoardOnCanvas         -> build_render_plan
    DrawFillBoardLegend              -> RenderPlan.legend
    GetCutSummaryText                 -> cut_summary_text
    GetCoverageDeficitText              -> coverage_deficit_text
    DisplayInfo_Normal/_Kyoyo の配色判定  -> cut_status_color
    CalculateUsageRatioByCategory        -> usage_ratio_by_category
    CalculateOverhangRatio                -> overhang_ratio

【InfoDisplay1(使用率/はみ出し率)の表示位置についての注記】
    VBA原文では InfoDisplay1(使用率/はみ出し率の行)と InfoDisplay3
    (カット+不足の行)が `DisplayInfo_Normal` 内で全く同じ座標
    (`PaletteCanvasLower.top + PaletteCanvasLower.height + 16`)に
    配置されており、後から追加されて幅も広い InfoDisplay3 の下に
    InfoDisplay1 が隠れて実機では見えていなかったとみられる
    (`DisplayInfo_Kyoyo` 側は +2 / +18 と正しく段組みされており、
    Normalだけがこの座標指定漏れになっている)。Python版では
    意図どおり両方とも見える形で表示する。

【座標系の変換】
    配置側は X=丈方向 / Y=幅方向 で、`PlacedBoardModel.length` がX方向、
    `width` がY方向の寸法。画面では丈を横(左→右)、幅を縦(上→下)に描くため、
        画面X = offset_x + board.x * scale     幅(px) = board.length * scale
        画面Y = offset_y + board.y * scale     高さ(px) = board.width * scale
    となる(VBA版と同じ)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import PlacedBoardModel

CATEGORY_LOWER = "下用"
CATEGORY_UPPER = "上用"

# キャンバスに対する描画領域の割合(VBA: canvasW * 0.9)
CANVAS_FILL_RATIO = 0.9

# 主ボードのバウンディングボックスに含める最小短辺(VBA: pbMinSide >= 100)
MAIN_BOARD_MIN_SIDE = 100

# カット判定の許容差(VBA `DetectBoardCut` の -3 / > 3)
CUT_TOLERANCE = 3

# キャプションを出さずに色分けだけにする閾値(px)。
# 【現場の声:「全画面でも文字が小さくて読み取れない」】フォントサイズを
# 2倍に上げた(`build_caption`)のに合わせて、この閾値も2倍にする。
# 論理キャンバス(980×460)自体は変えない ── フォントが同じキャンバスに
# 対してより大きな比率を占めるようにするのが目的なので、キャンバスまで
# 一緒に拡大すると比率が変わらず何も改善しない
THIN_LABEL_LIMIT = 30

# 凡例に載せる「細すぎるボード」の閾値(px)。THIN_LABEL_LIMIT と同じ理由で2倍
LEGEND_THIN_LIMIT = 30

# 文字1つが要る横幅(フォントサイズに対する比)。寸法の文字列は
# 「幅」「丈」の全角と数字の半角が混ざるので、**分けて数える** ──
# ひとまとめに全角で見ると、入るはずのボードまで文字をやめてしまう
CAPTION_WIDE_EM = 1.0      # 全角(幅・丈・カット など)
CAPTION_NARROW_EM = 0.6    # 半角(数字・記号)

# 色(VBAのRGB値をそのまま16進に変換)
COLOR_LOWER = "#c8ffc8"        # RGB(200,255,200)
COLOR_UPPER = "#ffc8c8"        # RGB(255,200,200)
COLOR_BORDER = "#000000"
COLOR_BOARD_OUTLINE = "#646464"  # RGB(100,100,100)
COLOR_CUT_LINE = "#dc2800"     # RGB(220,40,0)
COLOR_CUT_ZONE = "#ff8c1e"     # RGB(255,140,30)
COLOR_THIN_30 = "#3c78c8"      # RGB(60,120,200)
COLOR_THIN_50 = "#50a050"      # RGB(80,160,80)
COLOR_THIN_100 = "#783ca0"     # RGB(120,60,160)
COLOR_THIN_TINY = "#c83c3c"    # RGB(200,60,60)   高さ6px未満
COLOR_THIN_SMALL = "#e67850"   # RGB(230,120,80)  高さ12px未満
COLOR_THIN_OTHER = "#3c8c8c"   # RGB(60,140,140)

# 【VBA由来ではない、現場の声への対応】補填ボード(幅補填/丈補填)は
# 主ボードと同じ塗り(上用=薄赤/下用=薄緑)だったため、キャプションの
# 数字を読んで比べない限り見分けが付かなかった(現場の声:「はみ出しも
# 補填ボードもあまりにも解かりにくい」)。キャプションが入る大きさの
# ボードにも、主/補填を問わず常に見分けが付くよう専用の色を割り当てる
# (「細すぎて文字が入らない」ときの色分け=THIN_* とは別物)
COLOR_FILL = "#e0c264"        # 補填ボードの塗り
COLOR_FILL_OUTLINE = "#8a6d1f"  # 補填ボードの輪郭

# 【VBA由来ではない】パレット/製品の枠を超えて配置された(はみ出した)
# ボードは、以前は枠内のボードと見分けが付かなかった(現場の声:同上)。
# 塗りは変えず、警告色の輪郭だけを太くして示す
# (`--state-warn` と同じ値。render_json.py でそのトークンに対応付ける)
COLOR_OVERHANG_LINE = "#883c02"


@dataclass
class CutInfo:
    """VBA `DetectBoardCut` の4つのByRef出力。"""

    cut_width: bool = False      # 幅(Y)方向のカットあり
    cut_length: bool = False     # 丈(X)方向のカットあり
    amount_width: int = 0
    amount_length: int = 0

    @property
    def note(self) -> str:
        """キャプションに添えるカット注記(VBA `cutNote`)。"""
        note = ""
        if self.cut_length:
            note += f"←{self.amount_length}カット"
        if self.cut_width:
            note += f"↑{self.amount_width}カット"
        return note


@dataclass
class BoundingBox:
    min_x: int = 0
    min_y: int = 0
    max_x: int = 0
    max_y: int = 0


@dataclass
class Rect:
    """キャンバス上の矩形(px)。"""

    x: float
    y: float
    width: float
    height: float
    fill: str
    outline: str = ""
    caption: str = ""
    font_size: int = 14
    tooltip: str = ""
    # はみ出したボードの輪郭を太くして警告色と合わせて目立たせる
    # (`outline` を `COLOR_OVERHANG_LINE` にするのとセットで使う)
    outline_width: float = 1.0
    # 文字を置く中心。`None` なら矩形の中心(ふつうはこちら)。
    #
    # **枠からはみ出したボードで要る。** はみ出した分には切り落としの
    # 帯が重なるので、矩形の中心に置くと寸法が帯の下に隠れる
    # (現場の声:「カット描写まではよくなったがボードサイズが
    # 見えなくなってしまった」)。**切ったあとに残る側**へ寄せる ──
    # 現物として残るのはそちらで、寸法はその板の呼び名だから
    caption_cx: Optional[float] = None
    caption_cy: Optional[float] = None


@dataclass
class RenderPlan:
    """1つのキャンバスに描くもの一式。"""

    scale: float = 0.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    border: Optional[Rect] = None
    boards: list[Rect] = field(default_factory=list)
    cut_marks: list[Rect] = field(default_factory=list)
    legend: list[tuple[int, str]] = field(default_factory=list)


# ------------------------------------------------------------------
# カット検出
# ------------------------------------------------------------------
def detect_board_cut(orig_w: int, orig_l: int, placed_w: int, placed_l: int) -> CutInfo:
    """VBA `DetectBoardCut` の移植。

    元寸法(`original_width`/`original_length`)と実配置寸法を比べ、
    どちらの向きで解釈するとカット量が小さいかを判定してから
    幅/丈それぞれのカット有無と量を返す。
    `CUT_TOLERANCE`(3mm)以内の差はカットとみなさない。
    """
    info = CutInfo()
    if orig_w <= 0 or orig_l <= 0:
        return info

    # A: 無回転 origW→placedW, origL→placedL
    cut_y_a = orig_w - placed_w
    cut_x_a = orig_l - placed_l
    # B: 回転   origL→placedW, origW→placedL
    cut_y_b = orig_l - placed_w
    cut_x_b = orig_w - placed_l

    valid_a = cut_y_a >= -CUT_TOLERANCE and cut_x_a >= -CUT_TOLERANCE
    valid_b = cut_y_b >= -CUT_TOLERANCE and cut_x_b >= -CUT_TOLERANCE

    if valid_a and valid_b:
        # 同点ならB(回転側)を優先するのがVBAの挙動
        use_b = abs(cut_y_b) + abs(cut_x_b) <= abs(cut_y_a) + abs(cut_x_a)
    else:
        use_b = valid_b

    final_x, final_y = (cut_x_b, cut_y_b) if use_b else (cut_x_a, cut_y_a)

    if final_x > CUT_TOLERANCE:
        info.cut_length = True
        info.amount_length = final_x
    if final_y > CUT_TOLERANCE:
        info.cut_width = True
        info.amount_width = final_y
    return info


# ------------------------------------------------------------------
# 配置後サマリ (VBA `DisplayInfo` 系。キャンバス下の「はみ出し/カット」表示)
# ------------------------------------------------------------------
def usage_ratio_by_category(placed: list[PlacedBoardModel], category: str,
                            palette_w: int, palette_l: int) -> float:
    """VBA `CalculateUsageRatioByCategory` の移植。パレット面積に対する使用率(%)。

    上用/下用どちらのカテゴリでも基準は常に**パレット**のサイズ
    (VBA原文どおり。上用の使用率も製品サイズではなくパレット面積で割る)。
    パレットからはみ出す部分は面積に数えない(effective_l/effective_wで
    クリップしてから面積を足す)。
    """
    if not placed:
        return 0.0

    total_board_area = 0.0
    for pb in placed:
        if pb.board_category != category:
            continue
        effective_l = pb.length
        effective_w = pb.width
        if pb.x + effective_l > palette_l:
            effective_l = max(palette_l - pb.x, 0)
        if pb.y + effective_w > palette_w:
            effective_w = max(palette_w - pb.y, 0)
        if effective_l > 0 and effective_w > 0:
            total_board_area += effective_l * effective_w

    palette_area = float(palette_w) * float(palette_l)
    if palette_area <= 0:
        return 0.0
    return (total_board_area / palette_area) * 100


def overhang_ratio(placed: list[PlacedBoardModel], category: str,
                   palette_w: int, palette_l: int) -> float:
    """VBA `CalculateOverhangRatio` の移植。パレット面積に対するはみ出し面積の割合(%)。"""
    if palette_w == 0 or palette_l == 0:
        return 0.0

    total_overhang = 0.0
    for pb in placed:
        if pb.board_category != category:
            continue
        right_over = (pb.x + pb.length) - palette_l
        bottom_over = (pb.y + pb.width) - palette_w
        if right_over > 0:
            total_overhang += right_over * pb.width
        if bottom_over > 0:
            total_overhang += bottom_over * pb.length
        # コーナー(right_over * bottom_over)が二重計上されるため減算する
        if right_over > 0 and bottom_over > 0:
            total_overhang -= float(right_over) * float(bottom_over)

    palette_area = float(palette_w) * float(palette_l)
    return (total_overhang / palette_area) * 100


# 使用率/はみ出し率ラインの背景色(VBA固定値、条件分岐なし)
COLOR_INFO_USAGE_BG = "#ffffdc"     # RGB(255,255,220)


def cut_summary_text(placed: list[PlacedBoardModel], category: str,
                     base_w: int, base_l: int) -> str:
    """VBA `GetCutSummaryText` の移植。

    `base_w`/`base_l` は基準枠(下用=パレット、上用=製品)の幅・丈で、
    `build_render_plan` に渡すものと同じ。基準枠から実際にどれだけ
    はみ出しているか(はみ出しカット)と、ボード自体がカットされて
    いるか(`detect_board_cut`)の両方を、そのカテゴリの最大値でまとめる。
    """
    max_right_over = 0
    max_bottom_over = 0
    max_board_cut_l = 0
    max_board_cut_w = 0

    for pb in placed:
        if pb.board_category != category:
            continue
        right_over = (pb.x + pb.length) - base_l
        bottom_over = (pb.y + pb.width) - base_w
        max_right_over = max(max_right_over, right_over)
        max_bottom_over = max(max_bottom_over, bottom_over)

        cut = detect_board_cut(pb.original_width, pb.original_length, pb.width, pb.length)
        max_board_cut_l = max(max_board_cut_l, cut.amount_length)
        max_board_cut_w = max(max_board_cut_w, cut.amount_width)

    parts: list[str] = []
    if max_right_over > 0:
        parts.append(f"はみ出し丈カット: 約{max_right_over}mm")
    if max_bottom_over > 0:
        parts.append(f"はみ出し幅カット: 約{max_bottom_over}mm")
    if max_board_cut_l > CUT_TOLERANCE:
        parts.append(f"ボード丈カット: {max_board_cut_l}mm")
    if max_board_cut_w > CUT_TOLERANCE:
        parts.append(f"ボード幅カット: {max_board_cut_w}mm")
    return "  ".join(parts) if parts else "カットなし"


def coverage_deficit_text(placed: list[PlacedBoardModel], category: str,
                          product_w: int, product_l: int) -> str:
    """VBA `GetCoverageDeficitText` の移植。

    基準は下用/上用どちらのカテゴリでも常に**製品サイズ**
    (VBA原文どおり。下用でもパレットサイズではなく製品サイズと比較する)。
    """
    max_y = 0
    max_x = 0
    for pb in placed:
        if pb.board_category != category:
            continue
        max_y = max(max_y, pb.y + pb.width)
        max_x = max(max_x, pb.x + pb.length)

    deficit_w = product_w - max_y
    deficit_l = product_l - max_x

    if deficit_w > CUT_TOLERANCE:
        result_w = f"幅-{deficit_w}mm"
    elif deficit_w >= -CUT_TOLERANCE:
        result_w = "幅OK"
    else:
        result_w = f"幅+{abs(deficit_w)}mm超"

    if deficit_l > CUT_TOLERANCE:
        result_l = f"丈-{deficit_l}mm"
    elif deficit_l >= -CUT_TOLERANCE:
        result_l = "丈OK"
    else:
        result_l = f"丈+{abs(deficit_l)}mm超"

    return f"{result_w}  {result_l}"


# 配置サマリの背景色/文字色(VBA `DisplayInfo_Normal`/`_Kyoyo` の3分岐)
COLOR_INFO_DEFICIT_BG = "#fff0b4"   # RGB(255,240,180) 不足あり
COLOR_INFO_DEFICIT_FG = "#965000"   # RGB(150,80,0)
COLOR_INFO_CUT_BG = "#ffe6cd"       # RGB(255,230,205) カットあり
COLOR_INFO_CUT_FG = "#b42800"       # RGB(180,40,0)
COLOR_INFO_OK_BG = "#d7ffd7"        # RGB(215,255,215) 問題なし
COLOR_INFO_OK_FG = "#006400"        # RGB(0,100,0)


def cut_status_color(cut_text: str, deficit_text: str) -> tuple[str, str]:
    """`cut_summary_text`/`coverage_deficit_text` の組み合わせから背景色・文字色を判定する。

    不足(deficitに"-"が含まれる、つまり「幅-Xmm」等) > カットあり > 問題なし
    の優先順でVBAと同じ3段階に塗り分ける。
    """
    if "-" in deficit_text:
        return COLOR_INFO_DEFICIT_BG, COLOR_INFO_DEFICIT_FG
    if cut_text != "カットなし":
        return COLOR_INFO_CUT_BG, COLOR_INFO_CUT_FG
    return COLOR_INFO_OK_BG, COLOR_INFO_OK_FG


# ------------------------------------------------------------------
# スケール・バウンディングボックス
# ------------------------------------------------------------------
def compute_scale(canvas_w: float, canvas_h: float, base_w: int, base_l: int) -> float:
    """VBA の `scaleVal` 計算。丈(X)と幅(Y)で小さい方の倍率に合わせる。"""
    if base_w <= 0 or base_l <= 0:
        return 0.0
    scale_x = (canvas_w * CANVAS_FILL_RATIO) / base_l
    scale_y = (canvas_h * CANVAS_FILL_RATIO) / base_w
    return min(scale_x, scale_y)


def _bounding_box(
    placed: list[PlacedBoardModel], category: str, base_l: int, base_w: int,
    *, min_side: int,
) -> BoundingBox:
    """`GetMainBoardsBoundingBox` / `GetNarrowPaletteBoundingBox` の共通実装。

    両者の違いは「主ボードとみなす短辺の下限」だけ(通常版は100mm以上、
    狭幅版は下限なし)。補填ボードは、主ボードの端に接するものだけを
    ボックスに含める(離れた補填で中央寄せが過剰にずれるのを防ぐ)。
    """
    main_max_x = main_max_y = 0
    main_min_x = main_min_y = 2147483647

    for pb in placed:
        if pb.board_category != category or pb.is_fill_board:
            continue
        if min(pb.width, pb.length) < min_side:
            continue
        main_max_x = max(main_max_x, pb.x + pb.length)
        main_max_y = max(main_max_y, pb.y + pb.width)
        main_min_x = min(main_min_x, pb.x)
        main_min_y = min(main_min_y, pb.y)

    if main_max_x == 0 and main_max_y == 0:
        return BoundingBox(min_x=0, min_y=0, max_x=base_l, max_y=base_w)

    box = BoundingBox(
        min_x=0 if main_min_x == 2147483647 else main_min_x,
        min_y=0 if main_min_y == 2147483647 else main_min_y,
        max_x=main_max_x,
        max_y=main_max_y,
    )

    # 判定基準は固定値で持つ(ループ中にbox.min_yを書き換えるため、
    # 条件式にbox.min_yを直接使うと2本目以降の基準がズレる。
    # 下端側がbox.max_yではなくmain_max_yを基準にしているのと同じ理由)
    main_top_y = box.min_y

    # 主ボードのX端/Y端に直接接する補填ボードのみ取り込む
    for pb in placed:
        if pb.board_category != category or not pb.is_fill_board:
            continue
        if pb.y >= main_max_y:
            box.max_y = max(box.max_y, pb.y + pb.width)
        # 主ボード上端に接する幅補填(上側)をmin_yに含める(幅補填を
        # 主ボードの上下に振り分ける配置に対応)。丈補填はy=main_top_y
        # 付近に置かれるため pb.y + pb.width > main_top_y となり、
        # この条件から自然に外れる。
        if pb.y + pb.width <= main_top_y:
            box.min_y = min(box.min_y, pb.y)
        if pb.x >= main_max_x:
            box.max_x = max(box.max_x, pb.x + pb.length)
    return box


def main_boards_bounding_box(
    placed: list[PlacedBoardModel], category: str, base_l: int, base_w: int,
) -> BoundingBox:
    """VBA `GetMainBoardsBoundingBox` の移植(短辺100mm未満は主ボード扱いしない)。"""
    return _bounding_box(placed, category, base_l, base_w, min_side=MAIN_BOARD_MIN_SIDE)


def narrow_bounding_box(
    placed: list[PlacedBoardModel], category: str, base_l: int, base_w: int,
) -> BoundingBox:
    """VBA `GetNarrowPaletteBoundingBox` の移植(短辺の下限なし)。"""
    return _bounding_box(placed, category, base_l, base_w, min_side=0)


# ------------------------------------------------------------------
# 色・キャプション
# ------------------------------------------------------------------
def board_color(category: str) -> str:
    """VBA `GetBoardColor` の移植。"""
    return COLOR_UPPER if category == CATEGORY_UPPER else COLOR_LOWER


# 厚みで色が決まっている補填の3種。**この色が図に出たら、凡例も出す。**
# 30/50/100 は「何ミリの補填か」を色で言っているので、色だけ出して
# 意味を言わないと読めない(現場の指摘:「補填用ボードの30,50,100が
# 出るときと出ないときがある(色での案内)」)
FIXED_THIN_SIDES = (30, 50, 100)

# 補填の色で塗る板の、短い辺の上限。これより太い板は補填に使っても
# 主ボードと同じ色にする(`build_render_plan`)
FILL_STRIP_MAX = 100


def fill_board_color(thin_side: int) -> str:
    """補填ボードの塗り。**厚みで決める(30/50/100)。**

    以前は補填ならどれも1色(`COLOR_FILL`)でした。ところが細くて
    キャプションが入らない補填は `thin_board_color` を通り、そちらは
    30/50/100 で色が分かれます。つまり**同じ補填材が、文字が入るか
    どうかで色が変わって**いました(現場の指摘:「補填3種は30,50,100で
    色を統一」)。

    どちらの道でも同じ色になるよう、補填はここ1つで決めます。
    30/50/100 以外の厚みは補填の地色のまま(そこは種類ではなく端数)。
    """
    if thin_side == 30:
        return COLOR_THIN_30
    if thin_side == 50:
        return COLOR_THIN_50
    if thin_side == 100:
        return COLOR_THIN_100
    return COLOR_FILL


def thin_board_color(thin_side: int, screen_h: float) -> str:
    """細くてキャプションが入らないボードの色分け(VBA `DrawSingleBoardOnCanvas`)。

    30/50/100mmは固定色。それ以外は画面上の高さで3段階に分ける。
    """
    if thin_side == 30:
        return COLOR_THIN_30
    if thin_side == 50:
        return COLOR_THIN_50
    if thin_side == 100:
        return COLOR_THIN_100
    if screen_h < 6:
        return COLOR_THIN_TINY
    if screen_h < 12:
        return COLOR_THIN_SMALL
    return COLOR_THIN_OTHER


def build_caption(board: PlacedBoardModel, cut: CutInfo, screen_h: float) -> tuple[str, int]:
    """ボード上に載せる文字列とフォントサイズを決める(VBA踏襲)。

    高さに応じて 2行 / 1行 / 幅のみ の3段階に切り替える。

    【VBAからの変更】フォントサイズはVBAの値のままだと、配置後の図で
    「何を選んだのか読めない」という声があった(現場の声)。行数と
    切り替えの閾値(screen_h)はVBAのまま、サイズだけ底上げする。

    一度 8 まで上げたが、それでも「配置ボードのサイズが見えません」と
    再度声が出た。`viewBox` はキャンバス(980×460)に対する論理値で、
    実際の画面では枠の大きさぶん縮んで描かれる ── 8 は縮む前の数字で、
    縮んだあとは記号にしか見えない大きさだった。実機の見え方(実測
    143px四方のボードで文字の高さが約7.6px)で確かめたうえで底上げする。

    【さらに2倍】全画面表示・配置図印刷のどちらでも「文字サイズが
    小さくて読み取れない」という声が再度出たため、フォントサイズを
    この時点の2倍(12→24 / 11→22)にする(一度3倍にしたが、今度は
    「大きすぎる」と声が出たため2倍に落ち着けた)。行数の切り替え閾値
    (`screen_h`)も、文字が大きくなった分だけボードの高さに余裕が
    要るため同じ2倍(30→60 / 14→28)にする ── 閾値だけそのままだと、
    以前は2行キャプションが入っていた高さのボードで、大きくなった
    文字がボード枠からはみ出す
    """
    cap_w = f"幅{board.width}"
    cap_l = f"丈{board.length}"
    note = cut.note

    if screen_h >= 60:
        caption = f"{cap_w}\n{cap_l}"
        if note:
            caption += f"\n{note}"
        return caption, 24
    if screen_h >= 28:
        caption = f"{cap_w} {cap_l}"
        if note:
            caption += f" {note}"
        return caption, 24
    return cap_w, 22


def caption_fits(caption: str, font_size: int, screen_w: float) -> bool:
    """その文字が、そのボードの横幅に収まるか。

    **高さだけ見ていると、細長い補填で読めない文字が出る。**
    `build_caption` は縦(screen_h)を見て行数を決めるが、横がどれだけ
    あるかは見ていない。丈補填のような縦長の帯は縦に余裕がある一方、
    横は板厚ぶんしかない ── 厚み100mmの帯は画面で38px、そこへ24ptで
    「幅812」と書こうとしていた。

    現場の指摘:「50補填は50mmと左下に出て分かったが100は出ていなかった」
    ── 50は細いので凡例に回り、100は凡例に載らず、文字は入らない。
    どちらでもない状態になっていた。
    """
    if not caption:
        return True
    widest = max(caption_line_em(line) for line in caption.split("\n"))
    return widest * font_size <= screen_w


def caption_line_em(line: str) -> float:
    """1行の横幅(フォントサイズの何倍か)。"""
    return sum(CAPTION_NARROW_EM if ch.isascii() else CAPTION_WIDE_EM
               for ch in line)


def _is_overhanging(board: PlacedBoardModel, base_w: int, base_l: int) -> bool:
    """ボードが基準枠(パレット/製品)を超えて配置されているか。

    枠の外へ出た部分は既に描画上そのまま見せているが(補正しない仕様)、
    どのボードがはみ出しているかは塗り色だけでは分からなかった
    (現場の声)。ここで判定し、輪郭の色分けに使う。
    """
    return (board.x < 0 or board.y < 0
            or board.x + board.length > base_l
            or board.y + board.width > base_w)


# ------------------------------------------------------------------
# 描画計画の組み立て
# ------------------------------------------------------------------
def build_render_plan(
    placed: list[PlacedBoardModel], category: str,
    base_w: int, base_l: int, canvas_w: float, canvas_h: float,
    *, narrow: bool = False,
) -> RenderPlan:
    """1カテゴリ分の描画計画を組み立てる。

    `base_w`/`base_l` は基準枠(下用=パレット、上用=製品)の幅と丈。
    主ボードのバウンディングボックスを使って中央寄せ補正をかけるのは
    VBAと同じで、枠からはみ出しているときは補正しない(はみ出しを
    そのまま見せる)。
    """
    plan = RenderPlan()
    scale = compute_scale(canvas_w, canvas_h, base_w, base_l)
    if scale <= 0:
        return plan

    offset_x = (canvas_w - base_l * scale) / 2
    offset_y = (canvas_h - base_w * scale) / 2

    plan.border = Rect(
        x=offset_x, y=offset_y,
        width=base_l * scale, height=base_w * scale,
        fill="", outline=COLOR_BORDER,
    )

    box = (narrow_bounding_box if narrow else main_boards_bounding_box)(
        placed, category, base_l, base_w)

    # はみ出しあり(max > base)のときは負値になるのでシフトしない
    if box.max_x <= base_l:
        offset_x += (base_l - box.max_x - box.min_x) / 2 * scale
    if box.max_y <= base_w:
        offset_y += (base_w - box.max_y - box.min_y) / 2 * scale

    plan.scale = scale
    plan.offset_x = offset_x
    plan.offset_y = offset_y

    legend_seen: dict[int, str] = {}

    for board in placed:
        if board.board_category != category:
            continue

        px = offset_x + board.x * scale
        py = offset_y + board.y * scale
        screen_w = board.length * scale
        screen_h = board.width * scale
        thin_side = min(board.width, board.length)

        cut = detect_board_cut(
            board.original_width, board.original_length, board.width, board.length)
        tooltip = f"幅{board.width} × 丈{board.length}"

        # **文字が入るかどうかと、色は別の話。** 以前はこの2つを1つの
        # 分岐で決めていたため、同じ補填材が幅によって色を変えていた
        thin = screen_w < THIN_LABEL_LIMIT or screen_h < THIN_LABEL_LIMIT
        if thin:
            caption, font_size = "", 14
        else:
            caption, font_size = build_caption(board, cut, screen_h)
            # **書けない文字は書かない。** 入らない文字を書くと、読めない
            # うえに隣のボードへかぶる。文字をやめたぶんは凡例が引き受ける
            if not caption_fits(caption, font_size, screen_w):
                caption, font_size, thin = "", 14, True

        # 補填の色は**細い補填材(短い辺が FILL_STRIP_MAX 以下)だけ**。普通の
        # 大きさの板を丈の継ぎ足しに使ったときは主ボードと同じ色にする ──
        # 板全体が別の色になると、切る部分の印と見分けがつかない(現場の声:
        # 「カットする部分の色を変えるのはよいが、ボード全部の色を変えて
        # しまってよくわからない」)。切る部分は斜線の帯(`cut_marks`)が示す
        strip = board.is_fill_board and thin_side <= FILL_STRIP_MAX
        if strip:
            fill = fill_board_color(thin_side)
        elif thin:
            fill = thin_board_color(thin_side, screen_h)
        else:
            fill = board_color(board.board_category)

        if board.is_fill_board and cut.note:
            tooltip += f" / カット:{cut.note}"

        if _is_overhanging(board, base_w, base_l):
            outline, outline_width = COLOR_OVERHANG_LINE, 2.5
            tooltip += " / はみ出し"
        elif strip:
            outline, outline_width = COLOR_FILL_OUTLINE, 1.0
        else:
            outline, outline_width = COLOR_BOARD_OUTLINE, 1.0

        # 枠からはみ出した分には切り落としの帯が重なる。寸法が帯の下に
        # 隠れないよう、**切ったあとに残る側**の真ん中へ寄せる
        keep_right = min(px + screen_w, offset_x + base_l * scale)
        keep_bottom = min(py + screen_h, offset_y + base_w * scale)
        plan.boards.append(Rect(
            x=px, y=py, width=screen_w, height=screen_h,
            fill=fill, outline=outline, outline_width=outline_width,
            caption=caption, font_size=font_size, tooltip=tooltip,
            caption_cx=(px + keep_right) / 2 if keep_right < px + screen_w else None,
            caption_cy=(py + keep_bottom) / 2 if keep_bottom < py + screen_h else None,
        ))

        # 凡例に出すのは次の2つ。どちらも**色だけ出して意味を言わない**
        # 状態を作らないため。
        #
        #   1. 文字を出さなかったもの ── ほかに名乗る手段が無い
        #   2. 厚みで色が決まっている補填(30/50/100)── 文字が入っても、
        #      その色が何ミリを指すのかは凡例にしか書いていない。
        #      以前は 1 だけを見ていたので、同じ100の補填が、帯が太くて
        #      文字が入る図では凡例から消えていた
        show_in_legend = thin or (strip and thin_side in FIXED_THIN_SIDES)
        if show_in_legend and thin_side not in legend_seen:
            legend_seen[thin_side] = fill  # 図に出ている色をそのまま出す

        # 補填ボードはカットが無ければカット表示を出さない(VBA踏襲)
        if board.is_fill_board and not cut.cut_length and not cut.cut_width:
            continue

        # 切り落とす部分は**枠(下用=パレット・上用=製品)の中にだけ**描く。
        # 板の先に切り落とし分をそのまま足すと、枠の外へ長く伸びて
        # はみ出しに見えていた(30×2500 を 500 に切ると、捨てる 2000mm が
        # 枠の外まで描かれた。現場の声:「はみ出していないのにはみ出し」)
        frame_right = offset_x + base_l * scale
        frame_bottom = offset_y + base_w * scale
        if cut.cut_length:
            plan.cut_marks.append(Rect(
                x=px + screen_w - 1, y=py, width=3, height=screen_h, fill=COLOR_CUT_LINE))
            zone_w = (board.length + cut.amount_length) * scale - screen_w
            zone_w = min(zone_w, frame_right - (px + screen_w))
            if zone_w > 2:
                plan.cut_marks.append(Rect(
                    x=px + screen_w, y=py, width=zone_w, height=screen_h,
                    fill=COLOR_CUT_ZONE, caption="|||"))

        if cut.cut_width:
            plan.cut_marks.append(Rect(
                x=px, y=py + screen_h - 1, width=screen_w, height=3, fill=COLOR_CUT_LINE))
            zone_h = (board.width + cut.amount_width) * scale - screen_h
            zone_h = min(zone_h, frame_bottom - (py + screen_h))
            if zone_h > 2:
                plan.cut_marks.append(Rect(
                    x=px, y=py + screen_h, width=screen_w, height=zone_h,
                    fill=COLOR_CUT_ZONE, caption="==="))

    _add_overhang_cut_marks(plan, placed, category, base_w, base_l,
                            scale, offset_x, offset_y)
    plan.legend = sorted(legend_seen.items())
    return plan


def _add_overhang_cut_marks(
    plan: RenderPlan, placed: list[PlacedBoardModel], category: str,
    base_w: int, base_l: int, scale: float, offset_x: float, offset_y: float,
) -> None:
    """**枠からはみ出した分**のカット表示(VBA `DrawCutLinesOnCanvas`)。

    ボードそのものが切られている場合(在庫の実寸より小さく置いてある)は
    `detect_board_cut` が見つけますが、**フルサイズのまま置いて枠から
    はみ出している**ボードは実寸と一致するので、そちらでは見つかりません。
    現物ではその分を切るので、図にも出す必要があります。

    現場の声:「選定→配置ではカットがあっても描写なし。候補変更では出る」。
    敷き詰め方式(候補変更)は必ず寸法を指定して置くため実寸と食い違い、
    たまたま `detect_board_cut` で拾えていました ── **同じカットが
    経路によって出たり出なかったりする**状態だったので、はみ出しを
    見る側をここに足します。

    はみ出し量そのものは図の下の行(`cut_summary_text`)にも出ます。
    """
    boards = [pb for pb in placed if pb.board_category == category]
    right = [pb for pb in boards if pb.x + pb.length > base_l]
    bottom = [pb for pb in boards if pb.y + pb.width > base_w]

    for pb in right:
        over = (pb.x + pb.length) - base_l
        plan.cut_marks.append(Rect(
            x=offset_x + base_l * scale, y=offset_y + pb.y * scale,
            width=over * scale, height=pb.width * scale,
            fill=COLOR_CUT_ZONE, caption="|||"))
    for pb in bottom:
        over = (pb.y + pb.width) - base_w
        plan.cut_marks.append(Rect(
            x=offset_x + pb.x * scale, y=offset_y + base_w * scale,
            width=pb.length * scale, height=over * scale,
            fill=COLOR_CUT_ZONE, caption="==="))

    # 切る線は**はみ出しているボードのぶんだけ**引く。枠の端いっぱいに
    # 引くと、はみ出していないボードまで切るように見える
    if right:
        min_y = min(pb.y for pb in right)
        max_y = max(pb.y + pb.width for pb in right)
        plan.cut_marks.append(Rect(
            x=offset_x + base_l * scale - 1, y=offset_y + min_y * scale,
            width=3, height=(max_y - min_y) * scale, fill=COLOR_CUT_LINE))
    if bottom:
        min_x = min(pb.x for pb in bottom)
        max_x = max(pb.x + pb.length for pb in bottom)
        plan.cut_marks.append(Rect(
            x=offset_x + min_x * scale, y=offset_y + base_w * scale - 1,
            width=(max_x - min_x) * scale, height=3, fill=COLOR_CUT_LINE))
