"""画面の外枠に渡す値

左のレールの並びと、テンプレートが必要とする共通の値をここで作る。
**並びは作業の順序**で、tkinter版のタブ順(さらに遡ると VBA の
`mpMain.Pages`)と同一。番号は装飾ではなく、順序が実在するから振っている。
詳細は docs/UIUX設計指針.md §2.1。

資材モードでは出す画面が変わる。資材課は発注を受けて確認するだけで、
資材の選定はしない。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from flask import current_app, request

from packaging_tool import (access_control, app_config, idle_exit, modes,
                            screen_lock, user_settings, work_context)


@dataclass
class NavItem:
    key: str
    label: str
    url: str
    note: str = ""                  # 名前の下に出す一行。何をする画面か
    badge: str = ""
    badge_kind: str = "todo"        # "todo" | "done"
    ready: bool = True              # まだ作っていない画面は False


# 現場モードの7画面。左から作業する順。
#   引く → 決める → 出す
#
# 【なぜ説明を1行添えるのか】
# 「倉庫連携」「棚検索」は、名前だけでは何をする画面なのかが押すまで
# 分からない。行き先に何があるかを先に示すと、探して回る必要がなくなる
# (情報の匂い、設計指針 §2.4)。文言はサーバが持つ ── 画面が言葉を
# 組み立てると、同じことを2か所で決めることになる。
FIELD_NAV: tuple[tuple[str, str, str, str], ...] = (
    ("lot", "ロット検索", "/lot", "仕掛から引く"),
    ("selection", "資材選択", "/selection", "パレットとボードを決める"),
    ("inventory", "簡易在庫", "/inventory", "保管位置と受け払い"),
    ("warehouse", "倉庫連携", "/warehouse", "発注を送る・確認する"),
    ("layout", "棚検索", "/layout", "最寄りの置き場を探す"),
    ("log", "選定ログ", "/log", "何が起きたかを追う"),
    ("settings", "設定", "/settings", "取り込み元と端末"),
)

# 資材モード。受け取って確認するのが主だが、**届いた発注のLotを
# 確かめられる**必要がある。番号を目で読んで別の端末で引き直すしか
# ない状態だった(現場の指摘:「倉庫モードの時に送られてきたデータに
# 添付しているLOT情報を現場モードのように展開できる必要があります」)。
#
# 見られるのは**ロット情報・引当情報・受注情報・包装仕様書の図面**まで。
# 資材展開(製品サイズを入れて資材選択へ移る)はここには出しません ──
# あれは「これから資材を決めて発注する」の一歩目で、受けて確認する側の
# 操作ではないため(現場の指摘:「倉庫モードは資材展開して資材選択へが
# 出ていてはダメです」)。資材選択をレールに置かないのも同じ理由で、
# 辿り着く道が無い画面を並べると押した人には壊れて見えます。
#
# 簡易在庫も置く(現場の声:「倉庫モードに簡易在庫ページも追加したい」)。
# 在庫を見る・受け入れ・払い出しは資材モードでもできる。**配置編集だけは
# 現場モード**で、編集した配置は同じファイルなので資材モードにもそのまま
# 出る(`routes/inventory._map_edit_field_only`)。
#
# **発注を出せるようにもなりません。** 出すのは現場だけという決まりは
# `warehouse.field_only` が要求のたびに守っていて、ここは変えていない。
MATERIAL_NAV: tuple[tuple[str, str, str, str], ...] = (
    ("warehouse", "発注一覧", "/warehouse", "現場から届いた発注"),
    ("lot", "ロット検索", "/lot", "発注のLotを確かめる"),
    ("inventory", "簡易在庫", "/inventory", "保管位置と受け払い"),
    ("settings", "設定", "/settings", "取り込み元と端末"),
)


def _nav_source(mode: str) -> tuple[tuple[str, str, str, str], ...]:
    """そのモードのレール。**呼ぶたびに上の定数を読む。**

    表を一度だけ作って持つと、定数を差し替えても効かなくなる
    (画面を1つ足したときの試験がそれを踏んだ)。
    """
    return MATERIAL_NAV if modes.normalize(mode) == modes.MATERIAL else FIELD_NAV

# もう作った画面。ここに挙がっていないものは「準備中」ページを出す。
#
# 【なぜ空振りさせないのか】
# レールに7つ並べておいて4つが404になると、押した人には
# 「壊れている」としか見えない。まだ無いことと壊れていることは違うので、
# **無いなら無いと言う画面**を出す(基盤仕様書の考え方と同じで、
# 黙って何も起きない状態を作らない)。
READY_SCREENS = frozenset({"lot", "selection", "inventory", "warehouse",
                           "layout", "log", "settings"})

# 準備中の画面の説明。「いつ」と「それまでどうするか」まで書く。
#   key: (作る予定, 何ができるようになるか, いま使う画面)
#
# Phase 7 で7画面すべてが揃ったので、いまは空。まだ無い画面ができたら
# ここへ足す ── `READY_SCREENS` に挙げていない画面は自動でこの案内になる
PENDING_SCREENS: dict[str, tuple[str, str, str]] = {}


def nav_items(mode: str, badges: Optional[dict[str, tuple[str, str]]] = None) -> list[NavItem]:
    """レールの項目。`badges` は `{key: (文言, 種別)}`。

    バッジは「その先に何があるか」を行く前に示すためのもの
    (情報の匂い)。未読の選定ログ件数など。
    まだ作っていない画面には、行く前に分かるよう「準備中」を出す。
    """
    source = _nav_source(mode)
    badges = badges or {}
    items = []
    for key, label, url, note in source:
        ready = key in READY_SCREENS
        badge, kind = badges.get(key, ("", "todo"))
        if not ready and not badge:
            badge, kind = "準備中", "todo"
        items.append(NavItem(key=key, label=label, url=url, note=note,
                             badge=badge, badge_kind=kind, ready=ready))
    return items


def pending_items(mode: str) -> list[NavItem]:
    """まだ作っていない画面。準備中ページの登録に使う。"""
    return [item for item in nav_items(mode) if not item.ready]


# タブのアイコン。現場は青、資材は橙。文字は「現」「資」。
# 2つのモードを同時に開いていても、タブの並びで取り違えないようにする
# (タブの表題は幅が足りず途中で切れる)。**色と文字はモードが持つ**
_FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="6" fill="{color}"/>'
            '<text x="16" y="23" font-size="20" font-family="sans-serif"'
            ' text-anchor="middle" fill="#ffffff">{mark}</text></svg>')


def favicon(mode: str) -> str:
    """タブのアイコン(SVGそのもの)。`data:` URL に埋めて使う。"""
    try:
        item = modes.get(modes.normalize(mode))
    except ValueError:
        item = modes.get(modes.DEFAULT)
    return _FAVICON.format(color=item.color, mark=item.mark)


# 移行の途中はまだ作っていない画面がある。どれも開けないときの行き先。
# 「データ」はどちらのモードにもあり、取り込みの状態が分かるので
# 迷ったときに一番役に立つ
FALLBACK_URL = "/settings"


def home_url(mode: str) -> str:
    """`/` を開いたときに入る画面。

    レールの先頭(=作業の出発点)にする。ただし権限によっては現場の
    画面が登録されていないので、**実際に居るURL**から選ぶ。
    起動した先が404では、アプリが立ち上がったのかどうか分からない。
    """
    registered = {rule.rule for rule in current_app.url_map.iter_rules()}
    for item in nav_items(mode):
        if item.ready and item.url in registered:
            return item.url
    return FALLBACK_URL


def shell_context(active: str, *,
                  badges: Optional[dict[str, tuple[str, str]]] = None) -> dict[str, Any]:
    """`base.html` が必要とする値一式。

    各画面のルートは `render_template("...", **shell_context("lot"))` で使う。
    ここに集めておくことで、画面ごとに渡し忘れが起きない。
    """
    from . import current_grant

    config = current_app.config
    mode = config["MODE"]
    grant = current_grant()
    allowed = grant.allowed_modes()
    return {
        "active": active,
        "nav": nav_items(mode, badges),
        "mode": mode,
        "mode_label": modes.label(mode),
        # 切替に出す選択肢。**持っている権限のものだけ**。1つしか無ければ
        # 画面側は切替そのものを出さない(押せない選択肢を並べない)
        "mode_options": [{"key": m.key, "label": m.label, "detail": m.detail,
                          "current": m.key == mode}
                         for m in modes.ALL if m.key in allowed],
        # 権限で開けないモードがあることを、押す前に伝えるための一言。
        # **理由の有無によらず**、誰が・どこで・何を足せば増えるかまで
        # 具体的に出す(帯を見るだけで済ませたい、という現場の声への対応)
        "mode_note": (f"{config['MODE_NOTE']} " if config.get("MODE_NOTE") else "")
                     + access_control.explain_grant(grant),
        "display_name": config["DISPLAY_NAME"],
        # 帯に常時出す版。**どれが入っている端末か**を聞かれたときに、
        # 画面を見れば答えられるようにする(出どころは config/app.json)
        "version_label": app_config.version_label(),
        "favicon": favicon(mode),
        "token": config["TOKEN"],
        # このタブの番号。**開いた時点で「いま使っている画面」になる**
        # (`packaging_tool/screen_lock.py`)。同じアドレスを2枚開くと
        # どちらも同じ作業状態を触ってしまうので、操作できるのは
        # 最後に開いた1枚だけにする
        "screen_id": screen_lock.for_request(
            request.headers.get(screen_lock.HEADER, "")),
        "base_point": user_settings.get_position_label(),
        # いま何が決まっているか。画面をまたいで持つ(`work_context`)
        "ribbon": work_context.get_context().ribbon(),
        "health_poll_ms": app_config.health_poll_seconds() * 1000,
        "log_poll_ms": max(500, app_config.job_poll_ms() * 3),
        # 心拍の間隔。**間隔の出どころは `idle_exit` ただ1つ** ──
        # 画面とサーバで別に決めると、片方を直しただけで自動終了が誤る
        "alive_poll_ms": idle_exit.HEARTBEAT_MS,
    }
