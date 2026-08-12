"""描画計画を JSON に変換する

`placement_render.RenderPlan`(ボード配置図)と `angle_render.AnglePlan`
(アングル配置図)を、そのまま JSON にできる辞書へ落とす。

【なぜこの層が要るか】
tkinter版は `RenderPlan` を受け取って `Canvas.create_rectangle()` を呼ぶ。
Web版は同じ `RenderPlan` を JSON で受け取って SVG の `<rect>` を書く。
**描画計算(スケール・中央寄せ・カット検出・色分け)はどちらも同じ**
`placement_render` / `angle_render` が行うので、図の内容は一致する。

つまり Canvas から SVG への描き替えは「同じ計画を別の書き方で出す」だけで、
計算の移植ではない。ここが今回の移行でいちばん危険度の低い部分になっている。

【論理キャンバス】
サーバは固定寸法(`LOGICAL_CANVAS`)で計画を作り、実際の拡大縮小は
ブラウザの `<svg viewBox>` に任せる。ウィンドウサイズが変わるたびに
サーバへ問い合わせる必要がない(tkinter版は `<Configure>` のたびに
再計算していた)。詳細は docs/設計.md の §5。

【丸め】
浮動小数はそのまま出すと環境ごとの最下位ビット差でJSONが揺れる。
比較(ゴールデン)にも使うので、ここで丸めてから出す。
"""
from __future__ import annotations

from typing import Any, Optional

from .. import angle_render, placement_render

# ボード配置図(上用・下用)の論理キャンバス寸法(px)
LOGICAL_CANVAS_W = 980
LOGICAL_CANVAS_H = 460
LOGICAL_CANVAS = (LOGICAL_CANVAS_W, LOGICAL_CANVAS_H)

# アングル配置図は薄い帯なので低い
LOGICAL_ANGLE_W = 980
LOGICAL_ANGLE_H = 136
LOGICAL_ANGLE_CANVAS = (LOGICAL_ANGLE_W, LOGICAL_ANGLE_H)

# 座標の丸め桁
ROUND = 3


def round_px(value: float) -> float:
    """丸めた座標。`-0.0` は `0.0` に寄せてJSON上の見た目を安定させる。"""
    result = round(float(value), ROUND)
    return 0.0 if result == 0 else result


# ------------------------------------------------------------------
# ボード配置図
# ------------------------------------------------------------------
def rect_to_dict(rect: Optional[placement_render.Rect]) -> Optional[dict[str, Any]]:
    """`placement_render.Rect` → dict。

    `caption` は改行を含みうる(高さ30px以上のとき「幅…\\n丈…\\nカット…」)。
    SVG側は `<tspan>` に展開する。ここでは文字列のまま渡す。
    """
    if rect is None:
        return None
    return {
        "x": round_px(rect.x), "y": round_px(rect.y),
        "width": round_px(rect.width), "height": round_px(rect.height),
        "fill": rect.fill, "outline": rect.outline,
        "caption": rect.caption, "font_size": rect.font_size,
    }


def render_plan_to_dict(plan: placement_render.RenderPlan) -> dict[str, Any]:
    """`RenderPlan` → dict。"""
    return {
        "scale": round_px(plan.scale),
        "offset_x": round_px(plan.offset_x),
        "offset_y": round_px(plan.offset_y),
        "border": rect_to_dict(plan.border),
        "boards": [rect_to_dict(r) for r in plan.boards],
        "cut_marks": [rect_to_dict(r) for r in plan.cut_marks],
        # 細くて文字が入らないボードの色凡例 (幅mm, 色)
        "legend": [[int(width), str(color)] for width, color in plan.legend],
    }


def build_render_plan_dict(
    placed: list, category: str, base_w: int, base_l: int, *,
    narrow: bool = False,
    canvas: tuple[int, int] = LOGICAL_CANVAS,
) -> dict[str, Any]:
    """配置済みボードから、論理キャンバス上の描画計画を作って dict にする。

    `base_w`/`base_l` は基準枠の幅と丈(下用=パレット、上用=製品)。
    """
    plan = placement_render.build_render_plan(
        placed, category, base_w, base_l, canvas[0], canvas[1], narrow=narrow)
    return render_plan_to_dict(plan)


# ------------------------------------------------------------------
# アングル配置図
# ------------------------------------------------------------------
def angle_rect_to_dict(rect: Optional[angle_render.Rect]) -> Optional[dict[str, Any]]:
    if rect is None:
        return None
    return {
        "x": round_px(rect.x), "y": round_px(rect.y),
        "width": round_px(rect.width), "height": round_px(rect.height),
        "fill": rect.fill, "outline": rect.outline,
        "caption": rect.caption,
        "text_color": rect.text_color, "bold": bool(rect.bold),
    }


def text_item_to_dict(item: angle_render.TextItem) -> dict[str, Any]:
    """`x` は中央、`y` は上端(SVGでは `text-anchor="middle"` に対応)。"""
    return {
        "x": round_px(item.x), "y": round_px(item.y),
        "text": item.text, "color": item.color, "bold": bool(item.bold),
    }


def angle_plan_to_dict(plan: angle_render.AnglePlan) -> dict[str, Any]:
    """`AnglePlan` → dict。`bars` は描画順(中央のバーが最後=前面)。"""
    return {
        "scale": round_px(plan.scale),
        "pallet_bar": angle_rect_to_dict(plan.pallet_bar),
        "legs": [angle_rect_to_dict(r) for r in plan.legs],
        "product_edges": [angle_rect_to_dict(r) for r in plan.product_edges],
        "bars": [angle_rect_to_dict(r) for r in plan.bars],
        "cut_marks": [angle_rect_to_dict(r) for r in plan.cut_marks],
        "notes": [text_item_to_dict(t) for t in plan.notes],
    }


def build_angle_plan_dict(
    angle_lens, product_len: int, pallet_len: int, leg_count: int, *,
    need_cut: bool = False,
    canvas: tuple[int, int] = LOGICAL_ANGLE_CANVAS,
) -> dict[str, Any]:
    plan = angle_render.build_angle_plan(
        angle_lens, product_len, pallet_len, leg_count, canvas[0],
        need_cut=need_cut)
    return angle_plan_to_dict(plan)


# ------------------------------------------------------------------
# SVG の viewBox
# ------------------------------------------------------------------
def view_box(canvas: tuple[int, int] = LOGICAL_CANVAS) -> str:
    """`<svg viewBox="...">` に入れる文字列。"""
    return f"0 0 {canvas[0]} {canvas[1]}"


# ==================================================================
# 色 → デザイントークン
#
# 計画が持つ色は **VBA の RGB をそのまま16進にしたもの**で、tkinter版が
# 描くのに使う値。ゴールデン(41通り)もこの値で固定されている。
#
# Web版はこれをそのまま描かない。3つのテーマ(既定・OSダーク・明示ダーク)で
# 読める色は `tokens.css` が唯一の出どころで、コントラストは
# `scripts/check_contrast.py` が機械検査している(Phase 3)。
# 淡いペールトーンをダークの地に置くと図が読めなくなる。
#
# **変換はここに置く。** JS側に色の対応表を持たせると、計画が新しい色を
# 出すようになった日に、気づかないまま既定色で描かれる。
# ==================================================================
_UNKNOWN_TOKEN = "--ink"

# 塗り。キーは `placement_render` / `angle_render` の定数そのもの
FILL_TOKENS: dict[str, str] = {
    placement_render.COLOR_LOWER: "--mat-lower-fill",
    placement_render.COLOR_UPPER: "--mat-upper-fill",
    placement_render.COLOR_CUT_ZONE: "--cut-zone",
    placement_render.COLOR_CUT_LINE: "--cut-line",
    # 細くて文字が入らないボードは色分けで示す(凡例に幅mmを出す)
    placement_render.COLOR_THIN_30: "--coded-30",
    placement_render.COLOR_THIN_50: "--coded-50",
    placement_render.COLOR_THIN_100: "--coded-100",
    placement_render.COLOR_THIN_TINY: "--coded-tiny",
    placement_render.COLOR_THIN_SMALL: "--coded-small",
    placement_render.COLOR_THIN_OTHER: "--coded-other",
    # アングル図
    angle_render.COLOR_PALLET: "--angle-pallet",
    angle_render.COLOR_LEG: "--faint",
    angle_render.COLOR_PRODUCT_EDGE: "--angle-edge",
    angle_render.COLOR_WASTE: "--angle-waste",
    angle_render.COLOR_CUT_LINE: "--cut-line",
    angle_render.COLOR_OVERLAP_TEXT: "--state-error",
    angle_render.COLOR_WASTE_TEXT: "--angle-waste-fg",
    angle_render.COLOR_BAR_TEXT: "--angle-bar-fg",
}

# 輪郭。塗りと別の表を持つのは、同じ灰色でも「ボードの輪郭」と
# 「脚の塗り」では要る濃さが違うため(1.4.11 は接する相手ごとに見る)
LINE_TOKENS: dict[str, str] = {
    placement_render.COLOR_BORDER: "--ink",
    angle_render.COLOR_LEG_BORDER: "--muted",
    angle_render.COLOR_WASTE_BORDER: "--muted",
    angle_render.COLOR_BAR_BORDER: "--ink",
}

# ボードの輪郭は上用/下用で色が違う。計画は同じ灰色1つで出すが、
# **輪郭が接する相手が違う**(上用の淡い赤 / 下用の淡い緑)ので、
# 検査もトークンも category ごとに分ける(Phase 3 で塗りを淡くしたぶん、
# 隣り合うボードの境目は輪郭が担っている)
BOARD_LINE_TOKENS = {
    placement_render.CATEGORY_UPPER: "--mat-upper-line",
    placement_render.CATEGORY_LOWER: "--mat-lower-line",
}

# アングルのバーは**種類ではなく並び順**を示す色(6色の巡回)。
# 意味を持たないので、地と互いに見分けられることだけを要件にする
ANGLE_BAR_TOKENS: tuple[str, ...] = tuple(
    f"--angle-bar-{i + 1}" for i in range(len(angle_render.BAR_COLORS)))
_BAR_TOKEN_BY_COLOR = {
    color: ANGLE_BAR_TOKENS[i] for i, color in enumerate(angle_render.BAR_COLORS)}


def fill_token(color: str) -> str:
    """塗りの色 → トークン名。未知の色は `--ink`(見えなくなるよりよい)。"""
    if not color:
        return ""
    return (FILL_TOKENS.get(color) or _BAR_TOKEN_BY_COLOR.get(color)
            or _UNKNOWN_TOKEN)


def line_token(color: str, category: str = "") -> str:
    """輪郭の色 → トークン名。

    ボードの輪郭(`COLOR_BOARD_OUTLINE`)だけは上用/下用で行き先が違う。
    """
    if not color:
        return ""
    if color == placement_render.COLOR_BOARD_OUTLINE:
        return BOARD_LINE_TOKENS.get(category, "--mat-lower-line")
    return LINE_TOKENS.get(color) or _UNKNOWN_TOKEN


def with_tokens(plan: dict[str, Any], category: str = "") -> dict[str, Any]:
    """計画の各矩形に `fill_token` / `line_token` を足して返す(破壊しない)。

    `fill` / `outline` はそのまま残す。ゴールデンが見ているのはそちらで、
    **tkinter版とWeb版が同じ計画から描いていること**の証明に使う。
    """
    def convert(rect: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if rect is None:
            return None
        out = dict(rect)
        out["fill_token"] = fill_token(rect.get("fill", ""))
        out["line_token"] = line_token(rect.get("outline", ""), category)
        if "text_color" in rect:
            out["text_token"] = fill_token(rect["text_color"])
        return out

    out = dict(plan)
    for key in ("boards", "cut_marks", "legs", "product_edges", "bars"):
        if key in out:
            out[key] = [convert(r) for r in out[key]]
    for key in ("border", "pallet_bar"):
        if key in out:
            out[key] = convert(out[key])
    if "notes" in out:
        out["notes"] = [dict(n, color_token=fill_token(n.get("color", "")))
                        for n in out["notes"]]
    if "legend" in out:
        # 凡例は (幅mm, 色) の対。色はそのままトークンに置き換える
        out["legend"] = [[width, fill_token(color)]
                         for width, color in out["legend"]]
    return out
