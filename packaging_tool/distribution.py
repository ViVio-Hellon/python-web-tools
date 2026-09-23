"""配布設定 ── 1台で決めた設定を、配った先の端末でそのまま使う

【なぜ要るのか】
設定(取り込み元の置き場所・図面URL・自動取り込み・管理者パスワード)は
端末ごとの `data\\user_config.json` に入ります。配った先で1台ずつ設定画面を
開いて打ち直すのは手間で、打ち間違えると**別のファイルを読み書きする**
端末ができてしまいます(置き場所は取り込みと書き戻しの相手そのもの)。

そこで、設定を決めた端末で「配布設定」を書き出し、ツールのフォルダごと
配ります。配った先は起動したときにそれを見つけて読み込みます。

【どこに置くか】
`config\\distribution.json`(ツールのフォルダの中)。**`data\\` には置きません**
── `data\\` は端末ごとの中身(手元のDB・この端末の設定)で、配るものでは
ないからです。フォルダごと配れば一緒に届きます。

【何を入れるか】
画面で選んだものだけ(`ITEMS`)。**拠点は既定で入れません** ── ラインごとに
違うので、入れたまま配ると全端末が同じ拠点になります(疲労度・棚検索の
距離がずれる)。配置図(棚検索・簡易在庫)も選べば入れられます。

【いつ読むか】
起動したとき、**まだ読んでいない配布設定**があれば読みます(中身の指紋で
見分ける)。同じ配布設定は2度読みません ── 読んだあとに端末で直した値を、
次の起動で配布設定が上書きしてしまわないためです。新しい配布設定を置けば、
次の起動で読みます。

配置図は、**その端末で編集した図が無いときだけ**入れます(端末で直した配置を
黙って消さない)。設定画面の「配布設定を読み込み直す」なら上書きします。

【パスワード】書き出す・消す・読み込み直すには管理者パスワードが要ります
(設定画面の置き場所の変更と同じ関門)。起動時の読み込みには要りません
── 配布設定を置いたのは、フォルダを配った管理者本人だからです。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import admin_password, config, db, user_settings
from .logging_utils import get_logger

log = get_logger("distribution")

PATH = Path(os.environ.get("PACKAGING_TOOL_DISTRIBUTION_PATH",
                           str(config.BASE_DIR / "config" / "distribution.json")))

FORMAT = 1

# この端末の設定に「どの配布設定を読んだか」を控える鍵
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

# 配置図: (鍵, 画面の名前)
MAPS: tuple[tuple[str, str], ...] = (
    ("floor_plan", "棚検索の配置図"),
    ("pallet_map", "簡易在庫の保管位置マップ"),
)
MAP_KEYS = frozenset(key for key, _ in MAPS)

REFUSE_NEED_PASSWORD = "need_password"
REFUSE_BAD_INPUT = "bad_input"
REFUSE_FAILED = "failed"


def _map_path(key: str) -> Path:
    from . import floor_plan, pallet_map
    return floor_plan.USER_PATH if key == "floor_plan" else pallet_map.USER_PATH


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""
    applied: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
def read() -> Optional[dict[str, Any]]:
    """置いてある配布設定。無い・読めない・形が違うなら None。"""
    if not PATH.exists():
        return None
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("配布設定を読めませんでした: %s", exc)
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        log.warning("配布設定の形が違うため読みません: %s", PATH)
        return None
    return data


def _stamp(data: dict[str, Any]) -> str:
    """中身の指紋。**同じ配布設定を2度読まない**ための見分け。"""
    body = {"settings": data.get("settings", {}), "maps": data.get("maps", {})}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def applied_stamp() -> str:
    value = user_settings.get(KEY_APPLIED)
    return value.get("stamp", "") if isinstance(value, dict) else ""


def summary() -> dict[str, Any]:
    """設定画面に出す、配布設定のいま。**パスワードの値は出さない。**"""
    data = read()
    applied = user_settings.get(KEY_APPLIED)
    out: dict[str, Any] = {
        "exists": data is not None,
        "path": str(PATH),
        "items": [{"key": k, "label": label, "default": default}
                  for k, label, default in ITEMS],
        "maps": [{"key": k, "label": label} for k, label in MAPS],
        "applied_at": applied.get("at", "") if isinstance(applied, dict) else "",
        "applied_here": False,
        "contents": [],
        "created_at": "",
        "created_on": "",
    }
    if data is None:
        return out
    labels = dict((k, label) for k, label, _ in ITEMS)
    settings = data.get("settings", {}) or {}
    contents = []
    for key, value in settings.items():
        if key not in labels:
            continue
        shown = "(設定済み)" if key == admin_password.KEY else _show(value)
        contents.append({"label": labels[key], "value": shown})
    for key, label in MAPS:
        if key in (data.get("maps") or {}):
            contents.append({"label": label, "value": "入っています"})
    out.update(contents=contents,
               created_at=str(data.get("created_at", "")),
               created_on=str(data.get("created_on", "")),
               applied_here=applied_stamp() == _stamp(data))
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
    """**この端末のいまの設定**を配布設定として書き出す。

    入っていない項目(この端末で一度も設定していない)は入れない ──
    配った先の既定値を「空」で上書きしないため。
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
    # この端末で一度も変えていない項目は**既定のまま**。配った先も同じ既定で
    # 動くので、入れる必要が無い(空で上書きしないためにも入れない)
    defaults = [dict((k, l) for k, l, _ in ITEMS)[k] for k in items if k not in current]
    missing: list[str] = []
    map_data: dict[str, Any] = {}
    for key in maps:
        path = _map_path(key)
        if not path.exists():
            missing.append(dict(MAPS)[key] + "(この端末で編集していません)")
            continue
        try:
            map_data[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return Result(False, f"{dict(MAPS)[key]}を読めませんでした: {exc}",
                          REFUSE_FAILED)
    if not settings and not map_data:
        return Result(False, "書き出せる設定がありません(" + "・".join(missing + defaults)
                      + " はこの端末で変えていないため既定のままです)。",
                      REFUSE_BAD_INPUT)

    data = {"format": FORMAT, "created_at": db.now_db_string(),
            "created_on": platform.node(), "settings": settings, "maps": map_data}
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PATH)
    except OSError as exc:
        return Result(False, f"{PATH} に書けませんでした: {exc}", REFUSE_FAILED)
    # 書き出した端末は、その中身をもう使っている
    _mark_applied(data)
    names = [dict((k, l) for k, l, _ in ITEMS)[k] for k in settings] + \
            [dict(MAPS)[k] for k in map_data]
    message = f"配布設定を書き出しました({len(names)}項目)。ツールのフォルダごと配ってください。"
    if defaults:
        message += (" 既定のままなので入れていないもの(配った先も既定で動きます): "
                    + "・".join(defaults) + "。")
    if missing:
        message += " 入れられなかったもの: " + "・".join(missing) + "。"
    log.info("配布設定を書き出しました: %s", ", ".join(names))
    return Result(True, message, applied=names)


def remove(password: str) -> Result:
    if not admin_password.verify(str(password or "")):
        return Result(False, "配布設定を消すには管理者パスワードが要ります。",
                      REFUSE_NEED_PASSWORD)
    try:
        PATH.unlink(missing_ok=True)
    except OSError as exc:
        return Result(False, f"消せませんでした: {exc}", REFUSE_FAILED)
    log.info("配布設定を消しました")
    return Result(True, "配布設定を消しました。この端末の設定はそのままです。")


# ------------------------------------------------------------------
# 読み込む(配られた側)
# ------------------------------------------------------------------
def _mark_applied(data: dict[str, Any]) -> None:
    user_settings.save(KEY_APPLIED, {"stamp": _stamp(data),
                                     "at": db.now_db_string()})


def _apply(data: dict[str, Any], *, overwrite_maps: bool) -> list[str]:
    labels = dict((k, label) for k, label, _ in ITEMS)
    done = []
    for key, value in (data.get("settings") or {}).items():
        if key not in labels:                 # 知らない鍵は入れない
            continue
        if user_settings.save(key, value):
            done.append(labels[key])
    for key, plan in (data.get("maps") or {}).items():
        if key not in MAP_KEYS or not isinstance(plan, dict):
            continue
        path = _map_path(key)
        if path.exists() and not overwrite_maps:
            log.info("%s はこの端末で編集済みのため、配布設定で上書きしません", key)
            continue
        from . import map_data
        if map_data.write_json(path, plan):
            done.append(dict(MAPS)[key])
    _mark_applied(data)
    return done


def apply_on_start() -> Result:
    """起動時に呼ぶ。まだ読んでいない配布設定があれば読む。"""
    data = read()
    if data is None:
        return Result(True, "")
    if applied_stamp() == _stamp(data):
        return Result(True, "")
    done = _apply(data, overwrite_maps=False)
    log.info("配布設定を読み込みました: %s", ", ".join(done) or "(変更なし)")
    return Result(True, "配布設定を読み込みました", applied=done)


def reapply(password: str) -> Result:
    """設定画面から。**配置図も上書きして**読み込み直す。"""
    if not admin_password.verify(str(password or "")):
        return Result(False, "配布設定を読み込み直すには管理者パスワードが要ります。",
                      REFUSE_NEED_PASSWORD)
    data = read()
    if data is None:
        return Result(False, "配布設定が置かれていません。", REFUSE_BAD_INPUT)
    done = _apply(data, overwrite_maps=True)
    _reload_map_sessions()
    return Result(True, f"配布設定を読み込みました({len(done)}項目)", applied=done)


def _reload_map_sessions() -> None:
    """配置図を入れ替えたら、開いている図の作業状態を作り直す。"""
    from . import layout_session, pallet_map_session
    layout_session.reset_session()
    pallet_map_session.reset_session()
