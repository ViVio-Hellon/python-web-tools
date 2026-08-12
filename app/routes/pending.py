"""まだ作っていない画面

レールには7画面が並ぶが、移行の途中なので中身があるのは一部だけ。
残りをそのままにしておくと、押した人には **404** が返る。

まだ無いことと壊れていることは違うので、**無いなら無いと言う画面**を
出す。あわせて「いつ作るか」と「それまで何を使うか」まで書く。
黙って何も起きない状態を作らない、という基盤仕様書と同じ考え方。

ここに業務の中身は一切入れない。実装できたら `shell.READY_SCREENS` に
key を足すだけで、この画面は自動的に出なくなる。
"""
from __future__ import annotations

from flask import Blueprint, render_template

from packaging_tool.logging_utils import get_logger

from ..shell import PENDING_SCREENS, pending_items, shell_context

log = get_logger("app.routes.pending")

bp = Blueprint("pending", __name__)


def register(app) -> None:
    """このroleでまだ作っていない画面ぶんの経路を作る。

    `shell.py` の並びから作るので、レールに出ているのに開けない項目は
    生まれない(片方だけ直して食い違う、が起きない)。
    """
    for item in pending_items(app.config["MODE"]):
        phase, summary, meanwhile = PENDING_SCREENS.get(
            item.key, ("", "", ""))
        app.add_url_rule(
            item.url,
            endpoint=f"pending_{item.key}",
            view_func=_make_view(item.key, item.label, phase, summary, meanwhile),
            methods=["GET"],
        )
    log.info("準備中の画面: %s",
             ", ".join(i.label for i in pending_items(app.config["MODE"])) or "なし")


def _make_view(key: str, label: str, phase: str, summary: str, meanwhile: str):
    def view():
        return render_template(
            "pending.html",
            screen_label=label, phase=phase, summary=summary,
            meanwhile=meanwhile,
            # 資材展開で渡ってきた値。**受け取れていること**を見せる。
            # 画面が無いのと、渡せていないのは別の話で、
            # ここで何も出さないと「渡らなかった」と読まれる
            handoff=_handoff(key),
            **shell_context(key),
        )
    view.__name__ = f"pending_{key}"
    return view


def _handoff(key: str):
    """その画面が受け取っている作業。無ければ None。"""
    from packaging_tool import work_context

    if key != "selection":
        return None
    context = work_context.get_context()
    if not context.expanded:
        return None
    return {
        "lot_no": context.lot_no,
        "product": f"{context.product_width}×{context.product_length}",
        "is_ex": context.is_ex,
        "packaging_spec": context.packaging_spec,
    }
