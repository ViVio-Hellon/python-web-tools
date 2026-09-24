"""配布設定 ── 1台で決めた設定を、配った先の端末でそのまま使う

【なぜ要るのか】
設定(取り込み元の置き場所・図面URL・自動取り込み・管理者パスワード)は
端末ごとの `data\\user_config.json` に入ります。配った先で1台ずつ設定画面を
開いて打ち直すのは手間で、打ち間違えると**別のファイルを読み書きする**
端末ができてしまいます(置き場所は取り込みと書き戻しの相手そのもの)。

【流れ】
    1. 1台で起動して設定し、設定画面の「配布設定」で書き出す
    2. ツールのフォルダの直下に `配布設定\\` ができ、配るものが全部そこに入る
    3. フォルダごと配る(`scripts\\make_dist.bat` を使うと `data\\` などが紛れない)
    4. 配った先は起動したとき `配布設定\\` を見つけて読み込む

【`配布設定\\` の中身】**配布先に関わるものはここだけ**に置きます。

    配布設定\\
      設定.json            置き場所・図面URL・自動取り込み・書き出し先・
                           管理者パスワード(撹拌した値)・拠点(選んだときだけ)
      配置図\\
        floor_plan.json    棚検索の配置図(選んだときだけ)
        pallet_map.json    簡易在庫の保管位置マップ(選んだときだけ)
      はじめに読む.txt     何が入っているか・配った先で何が起きるか

配置図は普通のファイルなので、差し替えたいときは置き換えるだけで済みます。
`data\\` は端末ごとの中身で、ここには混ぜません。出荷時の既定の配置図は
プログラム側(`packaging_tool\\*_default.json`)にあり、配布設定に配置図が
無い端末はそれを使います。

【読み込むときの決まり】**その端末にすでにあるものは読み込みません。**

    設定     … その端末で値が入っている項目はそのまま。無い項目だけ埋める
    配置図   … その端末で配置を保存してあれば(`data\\*.json`)そのまま

起動のたびに見に行きますが、埋まった項目は次から「すでにある」ので、
端末で直した値が戻されることはありません。狙って揃えたいときは、
設定画面の「配布設定を読み込み直す」(管理者パスワード)で上書きします。

【パスワード】書き出す・消す・読み込み直すには管理者パスワードが要ります。
起動時の読み込みには要りません ── `配布設定\\` を置いたのは、フォルダを
配った管理者本人だからです。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import admin_password, config, db, user_settings
from .logging_utils import get_logger

log = get_logger("distribution")

# `配布設定\\` の置き場所(ツールのフォルダの直下)
DIR = Path(os.environ.get("PACKAGING_TOOL_DISTRIBUTION_DIR",
                          str(config.BASE_DIR / "配布設定")))
SETTINGS_NAME = "設定.json"
MAPS_DIRNAME = "配置図"
README_NAME = "はじめに読む.txt"

FORMAT = 1

# この端末が最後に読み込んだとき(設定画面に出すだけ)
KEY_APPLIED = "distribution_applied"

# 入れられるもの: (鍵, 画面の名前, 既定で入れるか)
#
# 鍵は `user_config.json` の鍵そのもの。**拠点だけは既定で外す**(端末ごとに違う)
ITEMS: tuple[tuple[str, str, bool], ...] = (
    (config.KEY_MASTER_DB_DIR, "梱包資材マスタの置き場所", True),
    (config.KEY_LOT_DB_DIR, "仕掛台帳の置き場所", True),
    (config.KEY_KANBAN_DB_DIR, "看板マスタの置き場所", True),
    (config.KEY_THRESHOLD_DB_DIR, "パレット閾値マスタの置き場所", True),
    (config.KEY_SPEC_SHEET_URL, "包装仕様書の図面URL", True),
    (config.KEY_AUTO_IMPORT, "起動時の自動取り込み", True),
    (config.KEY_EXPORT_DIR, "書き出し先", True),
    (admin_password.KEY, "管理者パスワード", True),
    (user_settings.KEY_POSITION, "拠点(ラインごとに違う)", False),
)
ITEM_KEYS = frozenset(key for key, _, _ in ITEMS)
ITEM_LABELS = {key: label for key, label, _ in ITEMS}

# 配置図: (鍵 = ファイル名の本体, 画面の名前)
MAPS: tuple[tuple[str, str], ...] = (
    ("floor_plan", "棚検索の配置図"),
    ("pallet_map", "簡易在庫の保管位置マップ"),
)
MAP_KEYS = frozenset(key for key, _ in MAPS)
MAP_LABELS = dict(MAPS)

REFUSE_NEED_PASSWORD = "need_password"
REFUSE_BAD_INPUT = "bad_input"
REFUSE_FAILED = "failed"


def settings_path(base: Optional[Path] = None) -> Path:
    return (base or DIR) / SETTINGS_NAME


def map_file(key: str, base: Optional[Path] = None) -> Path:
    return (base or DIR) / MAPS_DIRNAME / f"{key}.json"


def _terminal_map(key: str) -> Path:
    """その端末で保存した配置図(`data\\`)。"""
    from . import floor_plan, pallet_map
    return floor_plan.USER_PATH if key == "floor_plan" else pallet_map.USER_PATH


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""
    applied: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)   # すでにあったので読まなかったもの


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
@dataclass
class Bundle:
    """置いてある `配布設定\\` の中身。"""

    settings: dict[str, Any]
    maps: dict[str, Path]              # 鍵 → 配置図のファイル
    created_at: str = ""
    created_on: str = ""


def read() -> Optional[Bundle]:
    """置いてある配布設定。無い・読めない・形が違うなら None。

    `設定.json` が無くても、配置図だけ置いてあれば読む(配置図だけ配りたい、
    に応える)。
    """
    if not DIR.is_dir():
        return None
    settings: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    path = settings_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.warning("配布設定を読めませんでした: %s", exc)
            return None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            log.warning("配布設定の形が違うため読みません: %s", path)
            return None
        raw = data.get("settings") or {}
        settings = {k: v for k, v in raw.items() if k in ITEM_KEYS}   # 知らない鍵は捨てる
        meta = data
    maps = {key: map_file(key) for key in MAP_KEYS if map_file(key).is_file()}
    if not settings and not maps:
        return None
    return Bundle(settings=settings, maps=maps,
                  created_at=str(meta.get("created_at", "")),
                  created_on=str(meta.get("created_on", "")))


def summary() -> dict[str, Any]:
    """設定画面に出す、配布設定のいま。**パスワードの値は出さない。**"""
    bundle = read()
    applied = user_settings.get(KEY_APPLIED)
    out: dict[str, Any] = {
        "exists": bundle is not None,
        "path": str(DIR),
        "items": [{"key": k, "label": label, "default": default}
                  for k, label, default in ITEMS],
        "maps": [{"key": k, "label": label} for k, label in MAPS],
        "applied_at": applied.get("at", "") if isinstance(applied, dict) else "",
        "contents": [],
        "created_at": "",
        "created_on": "",
    }
    if bundle is None:
        return out
    contents = [{"label": ITEM_LABELS[key],
                 "value": "(設定済み)" if key == admin_password.KEY else _show(value)}
                for key, value in bundle.settings.items()]
    contents += [{"label": MAP_LABELS[key], "value": f"{MAPS_DIRNAME}\\{path.name}"}
                 for key, path in bundle.maps.items()]
    out.update(contents=contents, created_at=bundle.created_at,
               created_on=bundle.created_on)
    return out


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "する" if value else "しない"
    text = str(value)
    return text if text else "(既定)"


# ------------------------------------------------------------------
# 書き出す(配る側)
# ------------------------------------------------------------------
def export(password: str, items: list[str], maps: list[str]) -> Result:
    """**この端末のいまの設定**を `配布設定\\` に書き出す(前の中身は置き換える)。

    この端末で一度も変えていない項目は入れない ── 配った先も同じ既定で
    動くので要らない(空で上書きしないためにも入れない)。配置図は、この端末で
    保存したものがあればそれを、無ければ**出荷時の既定**を入れる。
    """
    if not admin_password.verify(str(password or "")):
        return Result(False, "配布設定を書き出すには管理者パスワードが要ります。",
                      REFUSE_NEED_PASSWORD)
    unknown = [k for k in list(items) + list(maps)
               if k not in ITEM_KEYS and k not in MAP_KEYS]
    if unknown:
        return Result(False, f"知らない項目です: {', '.join(unknown)}",
                      REFUSE_BAD_INPUT)
    if not items and not maps:
        return Result(False, "入れる項目を1つ以上選んでください。", REFUSE_BAD_INPUT)

    current = user_settings.load_all()
    settings = {k: current[k] for k in items if k in current}
    defaults = [ITEM_LABELS[k] for k in items if k not in current]
    map_sources = {key: _map_source(key) for key in maps}
    if not settings and not map_sources:
        return Result(False, "書き出せる設定がありません(" + "・".join(defaults)
                      + " はこの端末で変えていないため既定のままです)。",
                      REFUSE_BAD_INPUT)

    # **作ってから入れ替える。** 途中で失敗して、半分だけ新しい配布設定を
    # 残さない(配った先がそれを読んでしまう)
    staging = DIR.with_name(DIR.name + ".作成中")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        (staging / MAPS_DIRNAME).mkdir(parents=True)
        meta = {"format": FORMAT, "created_at": db.now_db_string(),
                "created_on": platform.node(), "settings": settings}
        settings_path(staging).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        for key, src in map_sources.items():
            shutil.copyfile(src, map_file(key, staging))
        (staging / README_NAME).write_text(_readme(meta, list(map_sources)),
                                           encoding="utf-8-sig")
        if DIR.exists():
            shutil.rmtree(DIR)
        staging.rename(DIR)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return Result(False, f"{DIR} に書けませんでした: {exc}", REFUSE_FAILED)
    _mark_applied()

    names = [ITEM_LABELS[k] for k in settings] + [MAP_LABELS[k] for k in map_sources]
    message = (f"配布設定を書き出しました({len(names)}項目)。ツールの直下の「{DIR.name}」"
               "フォルダに入っています。配るときは scripts\\make_dist.bat で"
               "配布用フォルダを作ってください(このフォルダも入ります)。")
    if defaults:
        message += (" 既定のままなので入れていないもの(配った先も既定で動きます): "
                    + "・".join(defaults) + "。")
    log.info("配布設定を書き出しました: %s", ", ".join(names))
    return Result(True, message, applied=names)


def _map_source(key: str) -> Path:
    """配る配置図の元。この端末で保存したもの、無ければ出荷時の既定。"""
    from . import floor_plan, pallet_map
    own = _terminal_map(key)
    if own.exists():
        return own
    return floor_plan.DEFAULT_PATH if key == "floor_plan" else pallet_map.DEFAULT_PATH


def _readme(meta: dict[str, Any], maps: list[str]) -> str:
    lines = [
        "梱包資材総合ツール 配布設定",
        "",
        f"作成: {meta['created_at']}({meta['created_on']})",
        "",
        "このフォルダに入っているもの",
    ]
    for key, value in meta["settings"].items():
        shown = "(設定済み)" if key == admin_password.KEY else _show(value)
        lines.append(f"  {ITEM_LABELS[key]}: {shown}")
    for key in maps:
        lines.append(f"  {MAP_LABELS[key]}: {MAPS_DIRNAME}\\{key}.json")
    lines += [
        "",
        "配った先で起きること",
        "  起動したときにこのフォルダを読み込みます。",
        "  その端末にすでにある設定・保存してある配置図は、読み込みません(上書きしない)。",
        "  揃えたいときは、設定画面の「配布設定」→「配布設定を読み込み直す」。",
        "",
        "配置図を差し替えたいとき",
        f"  {MAPS_DIRNAME} の中の .json を置き換えてください(配置編集で「配置を保存」した",
        "  端末の data\\floor_plan.json / pallet_map.json と同じ形です)。",
        "",
    ]
    return "\n".join(lines)


def remove(password: str) -> Result:
    if not admin_password.verify(str(password or "")):
        return Result(False, "配布設定を消すには管理者パスワードが要ります。",
                      REFUSE_NEED_PASSWORD)
    try:
        if DIR.exists():
            shutil.rmtree(DIR)
    except OSError as exc:
        return Result(False, f"消せませんでした: {exc}", REFUSE_FAILED)
    log.info("配布設定を消しました")
    return Result(True, "配布設定を消しました。この端末の設定はそのままです。")


# ------------------------------------------------------------------
# 読み込む(配られた側)
# ------------------------------------------------------------------
def _mark_applied() -> None:
    user_settings.save(KEY_APPLIED, {"at": db.now_db_string()})


def _apply(bundle: Bundle, *, overwrite: bool) -> Result:
    result = Result()
    current = user_settings.load_all()
    for key, value in bundle.settings.items():
        if key in current and not overwrite:
            result.kept.append(ITEM_LABELS[key])        # すでにある
            continue
        if user_settings.save(key, value):
            result.applied.append(ITEM_LABELS[key])
    from . import map_data
    for key, src in bundle.maps.items():
        dest = _terminal_map(key)
        if dest.exists() and not overwrite:
            result.kept.append(MAP_LABELS[key])
            continue
        plan = map_data.read_json(src)
        if not isinstance(plan, dict):
            log.warning("配布設定の配置図を読めません: %s", src)
            continue
        if map_data.write_json(dest, plan):
            result.applied.append(MAP_LABELS[key])
    if result.applied:
        _mark_applied()
    return result


def apply_on_start() -> Result:
    """起動時に呼ぶ。`配布設定\\` があれば、**その端末に無いものだけ**読む。"""
    bundle = read()
    if bundle is None:
        return Result(True, "")
    result = _apply(bundle, overwrite=False)
    if result.applied:
        log.info("配布設定を読み込みました: %s(すでにあったので読まなかったもの: %s)",
                 ", ".join(result.applied), ", ".join(result.kept) or "なし")
        result.message = "配布設定を読み込みました"
    return result


def reapply(password: str) -> Result:
    """設定画面から。**すでにあるものも上書きして**読み込み直す。"""
    if not admin_password.verify(str(password or "")):
        return Result(False, "配布設定を読み込み直すには管理者パスワードが要ります。",
                      REFUSE_NEED_PASSWORD)
    bundle = read()
    if bundle is None:
        return Result(False, "配布設定が置かれていません。", REFUSE_BAD_INPUT)
    result = _apply(bundle, overwrite=True)
    _reload_map_sessions()
    result.message = f"配布設定を読み込みました({len(result.applied)}項目)"
    return result


def _reload_map_sessions() -> None:
    """配置図を入れ替えたら、開いている図の作業状態を作り直す。"""
    from . import layout_session, pallet_map_session
    layout_session.reset_session()
    pallet_map_session.reset_session()
