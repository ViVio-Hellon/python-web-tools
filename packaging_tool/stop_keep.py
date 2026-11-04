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

**倉庫送信の下書き**(資材選択の「倉庫送信」が作り、倉庫連携でまだ送っていない発注。VER4.8.130)も同じ扱い。
プロセスが預かっているだけなので、止めると消え、倉庫に何も届かない。控えに書き、次の起動で戻す
(倉庫連携の画面にまた出る。資材選択の作業状態からは作り直さない: `WorkContext.pending_restored`)。

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
ORDERS = "orders"
ORDERS_LABEL = "倉庫連携の送っていない発注の下書き"


def pending_orders_left() -> dict[str, str]:
    """送っていない倉庫送信の下書き {鍵: 呼び名}。**作業状態を作らない**(無ければ空)。"""
    from . import work_context
    context = work_context._context
    held = context.peek_pending_orders() if context is not None else []
    if not held:
        return {}
    lot = str(held[0].get("lot_no") or "").strip()
    return {ORDERS: f"{ORDERS_LABEL}({len(held)}行{'・Lot ' + lot if lot else ''})"}


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
    saved += save_orders(stamp)
    return saved


def save_orders(stamp: str = "") -> list[str]:
    """送っていない倉庫送信の下書きを控えに書く。書けたら呼び名を返す。

    強制で止める前のほか、**お願いが出ているあいだに利用者が自分で終えた**ときにも呼ぶ(終了は下書きを
    訊かないので、黙って消えないように)。
    """
    left = pending_orders_left()
    if not left:
        return []
    from . import work_context
    data = {"kind": ORDERS, "label": left[ORDERS], "saved_at": time.time(),
            "orders": work_context._context.peek_pending_orders()}
    if not _write(keep_dir() / f"{ORDERS}-{stamp or time.strftime('%Y%m%d-%H%M%S')}.json", data):
        return []
    log.warning("送っていない発注の下書きを控えに残しました(管理者以外停止): %s",
                left[ORDERS], extra={"keep": True, "kind": "管理者以外停止"})
    return [left[ORDERS]]


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
    notes += _restore_orders()
    return notes


def _restore_orders() -> list[str]:
    from . import work_context
    path = _latest(ORDERS)
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        orders = [dict(o) for o in data["orders"]]
        if not orders or not all(isinstance(o, dict) for o in orders):
            raise ValueError("下書きが空です")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("送っていない発注の下書きの控えを読めないので戻しませんでした(控えは残しています: %s): %s",
                    path, exc, extra={"keep": True, "kind": "管理者以外停止"})
        return []
    context = work_context.get_context()
    if context.peek_pending_orders():
        log.warning("送っていない発注の下書きの控えがありますが、いま別の下書きがあるので戻しません(控えは残しています): %s",
                    path, extra={"keep": True, "kind": "管理者以外停止"})
        return []
    context.set_pending_orders(orders)
    context.pending_restored = True
    when = time.strftime("%m/%d %H:%M", time.localtime(float(data.get("saved_at") or 0)))
    note = (f"{when} に管理者の停止で送れなかった{data.get('label') or ORDERS_LABEL}を戻しました(まだ送っていません)。"
            "倉庫連携の画面で確かめて送ってください。要らなければ「入力を消す」で捨ててください")
    log.warning("%s", note, extra={"keep": True, "kind": "管理者以外停止"})
    _done(path)
    return [note]
