"""部品カタログ (`/__catalog`)

デザインシステムの見本帳。**画面を作る前にここで部品を確かめる**ため、
そして色やコントラストを変えたときに何が変わるかを一望するために置く。

利用者向けの画面ではないので、URLに `__` を付けて業務のURL空間と分けてある。
基盤仕様書に定めのある機能ではなく、開発を進めるための道具。

コントラスト比は `scripts/check_contrast.py` が機械的に検査する。
ここは「実際に並べたときにどう見えるか」を目で確かめる場所。
"""
from __future__ import annotations

import sys
from pathlib import Path

from flask import Blueprint, render_template

from packaging_tool.presenters import render_json

bp = Blueprint("catalog", __name__)

# `scripts/` は パッケージではないので、パスを足して読み込む
_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"


@bp.get("/__catalog")
def catalog():
    """部品と色の見本。"""
    return render_template("catalog.html", contrast=_contrast_summary(),
                           sample_plan=_sample_plan())


def _contrast_summary() -> dict:
    """検査結果を画面にも出す。数字を見ながら色を選べるようにするため。"""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    try:
        import check_contrast as cc
    except ImportError:                       # pragma: no cover - 通常は起きない
        return {"available": False}

    try:
        themes = cc.parse_tokens(cc.TOKENS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):             # pragma: no cover
        return {"available": False}

    findings = cc.check(themes)
    light = [f for f in findings if f.theme == cc.THEME_LIGHT]
    return {
        "available": True,
        "total": len(findings),
        "failed": sum(1 for f in findings if not f.ok),
        "min_text": cc.MIN_TEXT,
        "min_ui": cc.MIN_UI,
        "themes": len(themes),
        "rows": [
            {"label": f.label, "bg": f.bg, "fg": f.fg,
             "ratio": f"{f.ratio:.2f}" if f.ratio else "—",
             "ok": f.ok, "required": f.required}
            for f in light
        ],
        "exceptions": [
            {"label": label, "reason": reason}
            for _bg, _fg, label, reason in cc.DOCUMENTED_EXCEPTIONS
        ],
    }


def _sample_plan() -> dict:
    """配置図の見本。実データ(パレット1150×2650)の一部を使う。

    SVGの描き方が tokens の色を正しく拾っているかを、
    実際の描画計画で確かめる。
    """
    from packaging_tool import placement_render
    from packaging_tool.models import PlacedBoardModel

    placed = [
        PlacedBoardModel(width=1130, length=750, x=x, y=0,
                         board_category=placement_render.CATEGORY_LOWER,
                         original_width=750, original_length=1130,
                         instance_id=f"b{i}")
        for i, x in enumerate((0, 750, 1500))
    ]
    # 4枚目は回転して置いた板。元寸と実寸が入れ替わるだけでカットは無い
    # (元寸を実寸と食い違わせると、見本に意味のないカット注記が出てしまう)
    placed.append(PlacedBoardModel(
        width=1080, length=400, x=2250, y=0,
        board_category=placement_render.CATEGORY_LOWER,
        original_width=400, original_length=1080, instance_id="b4"))

    plan = render_json.build_render_plan_dict(
        placed, placement_render.CATEGORY_LOWER, 1150, 2650)
    plan["view_box"] = render_json.view_box()
    return plan
