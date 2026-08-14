"""アングル配置図の描画計画 (VBA `DrawAngleBoards` の移植)

パレットを真横から見た断面として、
    アングルバー(上) / パレット面 / 脚(下)
の3段を1本の帯に描く図。製品の左右端を赤い縦線で示し、
アングルが製品丈に足りない・被る・カットが要る、といった状況を可視化する。

`placement_render` と同じ方針で、tkinterに依存しない座標計算だけを持つ。

対応関係:
    DrawAngleBoards -> build_angle_plan
    (バーのX座標決定部) -> compute_bar_positions
    (バーの段組み決定部) -> compute_bar_rows
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

# VBA の Private Const をそのまま
ANGLE_BAR_H = 12.0        # バー高さ(pt)
ANGLE_BAR_GAP = 2.0       # 上下段間隔(pt)
ANGLE_OLAP_THR = 50       # 被りなし判定閾値(mm) ※パレット丈が不明なときだけ使う

PALLET_BAR_H = 5.0        # パレット面の厚み(pt)
LEG_SIZE = 10.0           # 脚は正方形(pt)
CANVAS_FILL_RATIO = 0.88  # キャンバス幅に対する描画幅の割合

# 色(VBAのRGB値をそのまま16進に変換)
BAR_COLORS = (
    "#4682b4",  # RGB(70,130,180)
    "#64b464",  # RGB(100,180,100)
    "#b48246",  # RGB(180,130,70)
    "#8246b4",  # RGB(130,70,180)
    "#46b482",  # RGB(70,180,130)
    "#b44646",  # RGB(180,70,70)
)
COLOR_PALLET = "#b4783c"       # RGB(180,120,60)
COLOR_LEG = "#8c8c8c"          # RGB(140,140,140)
COLOR_LEG_BORDER = "#646464"   # RGB(100,100,100)
COLOR_PRODUCT_EDGE = "#c80000"  # RGB(200,0,0)
COLOR_WASTE = "#a0a0a0"        # RGB(160,160,160)
COLOR_WASTE_BORDER = "#646464"
COLOR_WASTE_TEXT = "#3c3c3c"   # RGB(60,60,60)
COLOR_CUT_LINE = "#dc0000"     # RGB(220,0,0)
COLOR_BAR_BORDER = "#323232"   # RGB(50,50,50)
COLOR_BAR_TEXT = "#ffffff"
COLOR_OVERLAP_TEXT = "#ff0000"


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
    text_color: str = "#000000"
    bold: bool = False
    # 未指定なら描画側(`svgplan.js`)が 7 を当てる。それでは実機で
    # 読めないほど小さかった(`placement_render.build_caption` と同じ
    # 不具合。現場の声:「アングルがどのサイズか配置後わからない
    # （文字サイズ）」)ので、キャプションを持つ矩形には明示的に渡す
    font_size: int = 0


@dataclass
class TextItem:
    """枠を持たない文字だけの要素。`x` は中央、`y` は上端。"""

    x: float
    y: float
    text: str
    color: str = "#000000"
    bold: bool = False


@dataclass
class AnglePlan:
    scale: float = 0.0
    pallet_bar: Optional[Rect] = None
    legs: list[Rect] = field(default_factory=list)
    product_edges: list[Rect] = field(default_factory=list)
    bars: list[Rect] = field(default_factory=list)      # 描画順どおり
    cut_marks: list[Rect] = field(default_factory=list)
    notes: list[TextItem] = field(default_factory=list)


def _no_overlap_2(angle_lens: Sequence[int], pallet_len: int, product_len: int) -> bool:
    """2本のとき「被りなし」とみなすか(VBA `noOlap2` / `isOverlap2` の判定)。

    パレット丈が分かっていれば「2本の合計がパレットに収まるか」で判定し、
    分からないときだけ従来の閾値(製品丈+50mm)で判定する。
    """
    total = angle_lens[0] + angle_lens[1]
    if pallet_len > 0:
        return total <= pallet_len
    return total - product_len <= ANGLE_OLAP_THR


def compute_bar_positions(
    angle_lens: Sequence[int], prod_left: float, prod_screen_w: float,
    scale: float, pallet_len: int, product_len: int,
) -> list[float]:
    """各アングルバーの左端X座標(px)を決める(VBA の対称配置ロジック)。

        1本   : 製品左端に置く
        2本   : 収まるなら中央揃えで突き合わせ、被るなら左端+右端
        3本以上: 左から順・右から逆順に並べ、奇数本なら中央の1本を
                 左右の内側の端の中点に置く
    """
    n = len(angle_lens)
    if n == 0:
        return []
    if n == 1:
        return [prod_left]

    if n == 2:
        if _no_overlap_2(angle_lens, pallet_len, product_len):
            total = angle_lens[0] + angle_lens[1]
            offset = ((total - product_len) / 2) * scale
            return [prod_left - offset, prod_left - offset + angle_lens[0] * scale]
        return [prod_left, prod_left + prod_screen_w - angle_lens[1] * scale]

    lefts = [0.0] * n
    left_half = (n - 1) // 2 if n % 2 == 1 else n // 2

    # 左サイド: 製品左端から順に詰める
    x_mm = 0.0
    for i in range(left_half):
        lefts[i] = prod_left + x_mm * scale
        x_mm += angle_lens[i]

    # 右サイド: 製品右端から逆順に詰める
    x_mm = 0.0
    for i in range(n - 1, n - left_half - 1, -1):
        lefts[i] = prod_left + prod_screen_w - (x_mm + angle_lens[i]) * scale
        x_mm += angle_lens[i]

    # センター(奇数本のみ): 左右サイドの内端の中点
    if n % 2 == 1:
        c = left_half
        left_edge = lefts[left_half - 1] + angle_lens[left_half - 1] * scale
        right_edge = lefts[n - left_half]
        lefts[c] = (left_edge + right_edge) / 2 - angle_lens[c] * scale / 2
    return lefts


def compute_bar_rows(
    angle_lens: Sequence[int], y_angle: float, pallet_len: int, product_len: int,
) -> tuple[list[float], list[int]]:
    """各バーのY座標と描画順を決める。

    戻り値は (各バーのtop, 描画順のインデックス列)。
    重なりが見えるように、2本で被るときは上下段に分け、
    奇数3本以上はサイドを下段・センターを上段にして最後に描く(=前面)。
    """
    n = len(angle_lens)
    bar_h = ANGLE_BAR_H
    tops = [y_angle] * n

    if n == 2 and not _no_overlap_2(angle_lens, pallet_len, product_len):
        tops[1] = y_angle + bar_h + ANGLE_BAR_GAP
        return tops, [0, 1]

    is_odd_multi = n >= 3 and n % 2 == 1
    if not is_odd_multi:
        return tops, list(range(n))

    center = (n - 1) // 2
    order = [i for i in range(n) if i != center] + [center]
    for i in range(n):
        tops[i] = y_angle if i == center else y_angle + bar_h + 2
    return tops, order


def build_angle_plan(
    angle_lens: Sequence[int], product_len: int, pallet_len: int, leg_count: int,
    canvas_w: float, *, need_cut: bool = False,
) -> AnglePlan:
    """アングル配置図の描画計画を組み立てる(VBA `DrawAngleBoards`)。

    `pallet_len` が0(パレット未設定)なら製品丈を基準寸法にする。
    `need_cut` は `angle_service.select_angles` の `need_cut` をそのまま渡す。
    """
    plan = AnglePlan()
    if product_len <= 0:
        return plan

    base_len = pallet_len if pallet_len > 0 else product_len
    if base_len <= 0 or canvas_w <= 0:
        return plan

    scale = (canvas_w * CANVAS_FILL_RATIO) / base_len
    plan.scale = scale
    pal_screen_w = base_len * scale
    pal_left = (canvas_w - pal_screen_w) / 2

    y_angle = 3.0
    y_pallet = y_angle + ANGLE_BAR_H * 2 + 6
    y_leg = y_pallet + PALLET_BAR_H + 1

    # パレット面
    plan.pallet_bar = Rect(x=pal_left, y=y_pallet, width=pal_screen_w,
                           height=PALLET_BAR_H, fill=COLOR_PALLET)

    # 脚(正方形)。脚数が取れないときは2本として描く
    legs = max(leg_count, 2)
    leg_spacing = base_len // (legs + 1)
    for i in range(1, legs + 1):
        leg_x = pal_left + (leg_spacing * i) * scale - LEG_SIZE / 2
        plan.legs.append(Rect(x=leg_x, y=y_leg, width=LEG_SIZE, height=LEG_SIZE,
                              fill=COLOR_LEG, outline=COLOR_LEG_BORDER))

    # 製品の左右端(赤い縦線)
    side_margin = (base_len - product_len) / 2
    prod_left = pal_left + side_margin * scale
    prod_screen_w = product_len * scale
    for x in (prod_left, prod_left + prod_screen_w):
        plan.product_edges.append(Rect(x=x, y=y_pallet - 2, width=1,
                                       height=PALLET_BAR_H + 4, fill=COLOR_PRODUCT_EDGE))

    lens = [a for a in angle_lens if a > 0]
    if not lens:
        return plan

    lefts = compute_bar_positions(lens, prod_left, prod_screen_w, scale, pallet_len, product_len)
    tops, order = compute_bar_rows(lens, y_angle, pallet_len, product_len)

    for idx in order:
        plan.bars.append(Rect(
            x=lefts[idx], y=tops[idx], width=lens[idx] * scale, height=ANGLE_BAR_H,
            fill=BAR_COLORS[idx % len(BAR_COLORS)], outline=COLOR_BAR_BORDER,
            caption=f"{lens[idx]}mm", text_color=COLOR_BAR_TEXT, bold=True,
            font_size=9,
        ))

    # 1本をカットして使う場合は、切り落とす側を廃材として見せる
    if need_cut and len(lens) == 1:
        cut_x = lefts[0] + product_len * scale
        waste_w = (lens[0] - product_len) * scale
        cut_amount = lens[0] - product_len
        if waste_w > 0:
            plan.cut_marks.append(Rect(
                x=cut_x, y=tops[0], width=waste_w, height=ANGLE_BAR_H,
                fill=COLOR_WASTE, outline=COLOR_WASTE_BORDER,
                caption=f"廃材 {cut_amount}mm", text_color=COLOR_WASTE_TEXT,
                font_size=9))
        plan.cut_marks.append(Rect(
            x=cut_x - 1, y=tops[0] - 4, width=2, height=ANGLE_BAR_H + 8, fill=COLOR_CUT_LINE))
        plan.notes.append(TextItem(
            x=cut_x + 10, y=tops[0] - 16,
            text=f"カット {lens[0]}->{product_len}mm", color=COLOR_CUT_LINE, bold=True))

    # 被り量(2本で「被りなし」と判定された場合は出さない)
    overlap = sum(lens) - product_len
    is_no_overlap = len(lens) == 2 and _no_overlap_2(lens, pallet_len, product_len)
    if overlap > 0 and len(lens) >= 2 and not is_no_overlap:
        plan.notes.append(TextItem(
            x=prod_left + prod_screen_w / 2, y=48,
            text=f"ｱﾝｸﾞﾙ被り{overlap}mm", color=COLOR_OVERLAP_TEXT, bold=True))

    return plan
