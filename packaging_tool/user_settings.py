"""利用者ごとの設定の永続化 (VBA `GetSetting`/`SaveSetting` の代替)

VBA版は Windows レジストリに保存していた。

    GetSetting("梱包資材管理", "Config", "Position", "L1")
    SaveSetting "梱包資材管理", "Config", "Position", "HVC"

Python版はレジストリに依存しないよう、JSONファイル(`config.USER_CONFIG_PATH`)
に保存する。読み書きのたびにファイルを開くので、外部から書き換えても
次回の読み取りで反映される(VBAのレジストリと同じ感覚で使える)。

**拠点(Position)は複数の機能が同じ値を参照する共有設定**である点が重要:
    - 疲労度マップの構築(`BuildFatigueMap3`)
    - アングル選定の疲労度(`CalcAngleFatigue`)
    - 棚検索(`frmLayout`)
VBAでは1つのレジストリ値をこれら全部が読んでいたので、Python版でも
画面ごとに別々の値を持たせず、この設定を唯一の出どころにする。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from . import config
from .logging_utils import get_logger

log = get_logger("user_settings")

# 拠点(VBA `Config\Position`)。値は `floor_plan.BASE_POINTS` と揃える
KEY_POSITION = "position"
DEFAULT_POSITION = "L-1"

# 拠点が未登録であることを表す表示用の文字列(VBA `GetSetting(..., "未登録")`)
UNSET_LABEL = "未登録"


class SettingsUnreadable(RuntimeError):
    """設定ファイルは**あるのに読めない**(ほかが掴んでいる・書きかけ・壊れている)。

    「無い」(初めての端末)とは分ける。読めないのに空とみなして保存すると、保存した1つ以外の
    設定がぜんぶ消える(別ツールで起きた: 保存するたびにほかの設定が消え、配布設定で PC の値が
    上書きされた)。**読めないときは書かない・消さない・覚えない**(VER4.8.122)。
    """


_lock = threading.RLock()
# 最後に**読めた**中身。読めないあいだの読み取りはこれで答える(書くときには使わない)
_last_good: dict[str, Any] = {}


def _read() -> dict[str, Any]:
    """設定ファイル全体を読む。**無ければ空**、あるのに読めなければ `SettingsUnreadable`。

    無いかどうかは開いてみて決める(`exists()` は、届かない・権限が無いときも False を返すので使わない)。
    メモ帳で保存すると付く BOM も読む。
    """
    path = config.USER_CONFIG_PATH
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsUnreadable(f"{path} を読めません: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SettingsUnreadable(f"{path} の中身を読めません(書きかけか壊れています): {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsUnreadable(f"{path} の中身が設定の形ではありません")
    return data


def load_all_strict() -> dict[str, Any]:
    """設定ファイル全体。**読めなければ `SettingsUnreadable`**(読んだ中身で何かを決めて書く呼び手用)。"""
    with _lock:
        data = _read()
        _last_good.clear()
        _last_good.update(data)
        return dict(data)


def load_all() -> dict[str, Any]:
    """設定ファイル全体を読む(画面に出す・既定値で続けてよい呼び手用)。

    読めないときは、前に読めた中身で答える(一度も読めていなければ空 = 既定値)。**この答えを
    もとに書いてはいけない**(書くのは `save`。`save` は自分で読み直し、読めなければ書かない)。
    """
    try:
        return load_all_strict()
    except SettingsUnreadable as exc:
        _note_unreadable(exc)
        with _lock:
            return dict(_last_good)


def readable() -> tuple[bool, str]:
    """いま設定ファイルを読めるか(読めなければ理由)。"""
    try:
        load_all_strict()
    except SettingsUnreadable as exc:
        return False, str(exc)
    return True, ""


def _note_unreadable(exc: Exception) -> None:
    from . import trouble
    trouble.failed(log, "設定ファイル", trouble.KIND_UNREADABLE,
                   "この端末の設定ファイル(user_config.json)の読み取り", exc)
    log.warning("設定ファイルを読めません(前に読めた値で続けます。保存はしません): %s", exc)


def get(key: str, default: Any = None) -> Any:
    """VBA `GetSetting` 相当。"""
    return load_all().get(key, default)


# ログに値を出さない鍵。伏せるだけで、保存はふつうに行う
HIDDEN_IN_LOG = ("admin_password",)


def _write(data: dict[str, Any]) -> None:
    """**丸ごと書いてから入れ替える**(書きかけのファイルを残さない・読む側に半分の姿を見せない)。"""
    path = config.USER_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                   # Windows: 誰かが一瞬開いている
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.1 * (attempt + 1))


def save(key: str, value: Any) -> bool:
    """VBA `SaveSetting` 相当。書き込めなければ False を返す。

    **読み直してから1つだけ変えて書く。** 設定ファイルがあるのに読めないときは書かない
    (空とみなして書くと、ほかの設定がぜんぶ消える)。
    """
    with _lock:
        try:
            data = _read()
        except SettingsUnreadable as exc:
            _note_unreadable(exc)
            log.warning("設定を保存しませんでした(設定ファイルを読めないため。ほかの設定を消さないように): %s",
                        key, extra={"keep": True, "kind": "設定を保存できない"})
            return False
        data[key] = value
        try:
            _write(data)
        except OSError as exc:
            log.warning("設定ファイルに書けませんでした: %s", exc)
            return False
        _last_good.clear()
        _last_good.update(data)
    from . import trouble
    trouble.ok(log, "設定ファイル")
    # **値を出さない鍵がある。** 管理者パスワードは撹拌済みだが、
    # ログに残す理由が無い(残せば持ち出せる)
    log.info("設定を保存しました: %s=%s", key,
             "(伏せます)" if key in HIDDEN_IN_LOG else value)
    return True


# ------------------------------------------------------------------
# 拠点 (VBA `Config\Position`)
# ------------------------------------------------------------------
def _canonical_position(value: str) -> str:
    """前の版で保存した「L1」を「L-1」として読む(VER4.8.87。拠点の表示を L-1 にそろえた)。"""
    from .floor_plan import DEFAULT_BASE_POINTS, _base_key
    key = _base_key(value)
    return next((d for d in DEFAULT_BASE_POINTS if _base_key(d) == key), value)


def get_position(default: str = DEFAULT_POSITION) -> str:
    """登録済みの拠点を返す。未登録なら `default`。"""
    value = get(KEY_POSITION)
    return _canonical_position(value) if isinstance(value, str) and value else default


def get_position_label() -> str:
    """画面に出す用。未登録なら「未登録」(VBA `btnPositionSetting_Click` 踏襲)。"""
    value = get(KEY_POSITION)
    return _canonical_position(value) if isinstance(value, str) and value else UNSET_LABEL


# 画面の色(ライト / ダーク)。**空なら OS の設定に合わせる**(`tokens.css` の3つの状態)
KEY_THEME = "theme"
THEMES = ("light", "dark")


def get_theme() -> str:
    """選んだ画面の色。"light" / "dark"、選んでいなければ ""(OS に合わせる)。"""
    value = get(KEY_THEME)
    return value if value in THEMES else ""


def set_theme(value: str) -> bool:
    """画面の色を選ぶ。"light" / "dark" 以外は「OS に合わせる」(空)。"""
    return save(KEY_THEME, value if value in THEMES else "")


def set_position(value: str) -> bool:
    """拠点を登録する(VBA `mBtnPosition_Click` の `SaveSetting`)。"""
    return save(KEY_POSITION, value)


def is_position_set() -> bool:
    return bool(get(KEY_POSITION))
