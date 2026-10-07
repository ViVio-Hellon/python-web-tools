"""操作説明書 (`/manual`)

帯の「説明書」から**別の窓**で開く、写真入りの操作説明書(`templates/manual.html`)。
写真は `static/manual/img/`。どのモードでも開ける(現場・倉庫どちらの使い方も載っている)。
"""
from __future__ import annotations

from flask import Blueprint, current_app, render_template

from packaging_tool import app_config, user_settings

bp = Blueprint("manual", __name__)


@bp.get("/manual")
def manual():
    return render_template(
        "manual.html",
        display_name=current_app.config["DISPLAY_NAME"],
        version_label=app_config.version_label(),
        # アプリと同じ色(ライト / ダーク)で出す
        theme=user_settings.get_theme())
