"""強制で止める前に、保存していない図を控えに残し、次の起動で戻す(VER4.8.129。管理者以外停止モード)

現場の声:「保存していない図・処理中の PC は重要な内容と考えている。止まらなければ5分たったら強制で止める。
保存の持ち方は考えてください」。

【持ち方】
- **図のファイル(`floor_plan.json`・`pallet_map.json`)には書かない。** 「保存を押すまで書かない。間違えても
  開き直せば元に戻る」という約束を守る(勝手に保存すると、捨てるつもりだった編集が入ってしまう)
- 代わりに、この端末の手元(`%LOCALAPPDATA%\\PackagingTool\\data\\停止控え\\`)へ**控え**として書く
- 次の起動で控えがあれば、その図を**編集中・保存していない**状態で戻す。画面に「戻しました。確かめて保存して
  ください」と出す。保存していないので、閉じるときはいつもどおり「保存していない変更があります」と訊く
- 戻した控えは `停止控え\\戻した\\` へ移して残す(もう一度戻さない。念のため消さない)

打ちかけ(送る・登録する前の入力)は、もともと打つたびに画面の側(ブラウザ・窓)に置いてあるので、強制で
止めても残る(docs/ランチャー連携.md の 9)。ここでは扱わない。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from . import config
from .logging_utils import get_logger

log = get_logger("stop_keep")

DIR_NAME = "停止控え"
DONE_NAME = "戻した"
LAYOUT = "floor_plan"
INVENTORY = "pallet_map"


def keep_dir() -> Path:
    return config.DB_PATH.parent / DIR_NAME


def _write(path: Path, data: dict) -> bool:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        log.error("保存していない図の控えを書けませんでした(%s): %s", path, exc)
        return False


def save_unsaved() -> list[str]:
    """保存していない図を控えに書く。書けたものの呼び名を返す。**作業状態を作らない**(無ければ何もしない)。"""
    from . import layout_session, pallet_map_session
    saved: list[str] = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    targets = (
        (LAYOUT, layout_session._session, layout_session.UNSAVED_LABEL),
        (INVENTORY, pallet_map_session._session, pallet_map_session.UNSAVED_LABEL),
    )
    for key, session, label in targets:
        if session is None or not session.dirty:
            continue
        data = {"kind": key, "label": label, "saved_at": time.time(), "plan": session.plan.to_dict()}
        if _write(keep_dir() / f"{key}-{stamp}.json", data):
            log.warning("保存していない図を控えに残しました(管理者以外停止で強制で止める前): %s", label,
                        extra={"keep": True, "kind": "管理者以外停止"})
            saved.append(label)
    return saved


def _latest(key: str) -> Optional[Path]:
    try:
        found = sorted(keep_dir().glob(f"{key}-*.json"))
    except OSError:
        return None
    return found[-1] if found else None


def _done(path: Path) -> None:
    """戻した控えを `戻した\\` へ移す(もう一度戻さない)。同じ種類の古い控えも一緒に移す。"""
    target = keep_dir() / DONE_NAME
    try:
        target.mkdir(parents=True, exist_ok=True)
        for old in keep_dir().glob(f"{path.name.split('-', 1)[0]}-*.json"):
            os.replace(old, target / old.name)
    except OSError as exc:
        log.warning("戻した控えを移せませんでした(次の起動でもう一度戻ります): %s", exc)


def restore() -> list[str]:
    """控えがあれば、図を編集中・保存していない状態で戻す。戻したものの一言を返す。

    **控えを読めなければ戻さず、控えも残す**(壊れていれば記録に出す)。
    """
    from . import floor_plan, layout_session, pallet_map, pallet_map_session
    notes: list[str] = []
    plan_types = (
        (LAYOUT, layout_session, floor_plan.FloorPlan, "棚検索"),
        (INVENTORY, pallet_map_session, pallet_map.PalletMap, "簡易在庫"),
    )
    for key, module, plan_type, screen in plan_types:
        path = _latest(key)
        if path is None:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            plan = plan_type.from_dict(data["plan"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("保存していない図の控えを読めないので戻しませんでした(控えは残しています: %s): %s",
                        path, exc, extra={"keep": True, "kind": "管理者以外停止"})
            continue
        session = module.get_session()
        session.plan = plan
        session.editing = True
        session.dirty = True
        when = time.strftime("%m/%d %H:%M", time.localtime(float(data.get("saved_at") or 0)))
        label = data.get("label") or module.UNSAVED_LABEL
        note = (f"{when} に管理者の停止で保存できなかった{label}を戻しました(まだ保存していません)。"
                f"{screen}の画面で確かめて「配置を保存」を押してください。要らなければ保存せずに閉じてください")
        log.warning("%s", note, extra={"keep": True, "kind": "管理者以外停止"})
        notes.append(note)
        _done(path)
    return notes
