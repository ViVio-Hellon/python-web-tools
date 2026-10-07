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
from typing import Any

from . import config
from .logging_utils import get_logger

log = get_logger("user_settings")

# 拠点(VBA `Config\Position`)。値は `floor_plan.BASE_POINTS` と揃える
KEY_POSITION = "position"
DEFAULT_POSITION = "L1"

# 拠点が未登録であることを表す表示用の文字列(VBA `GetSetting(..., "未登録")`)
UNSET_LABEL = "未登録"


def load_all() -> dict[str, Any]:
    """設定ファイル全体を読む。壊れていても例外にせず空扱いにする。"""
    path = config.USER_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("設定ファイルを読めませんでした(既定値で続行): %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str, default: Any = None) -> Any:
    """VBA `GetSetting` 相当。"""
    return load_all().get(key, default)


# ログに値を出さない鍵。伏せるだけで、保存はふつうに行う
HIDDEN_IN_LOG = ("admin_password",)


def save(key: str, value: Any) -> bool:
    """VBA `SaveSetting` 相当。書き込めなければ False を返す。"""
    data = load_all()
    data[key] = value
    try:
        config.USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.USER_CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("設定ファイルに書けませんでした: %s", exc)
        return False
    # **値を出さない鍵がある。** 管理者パスワードは撹拌済みだが、
    # ログに残す理由が無い(残せば持ち出せる)
    log.info("設定を保存しました: %s=%s", key,
             "(伏せます)" if key in HIDDEN_IN_LOG else value)
    return True


# ------------------------------------------------------------------
# 拠点 (VBA `Config\Position`)
# ------------------------------------------------------------------
def get_position(default: str = DEFAULT_POSITION) -> str:
    """登録済みの拠点を返す。未登録なら `default`。"""
    value = get(KEY_POSITION)
    return value if isinstance(value, str) and value else default


def get_position_label() -> str:
    """画面に出す用。未登録なら「未登録」(VBA `btnPositionSetting_Click` 踏襲)。"""
    value = get(KEY_POSITION)
    return value if isinstance(value, str) and value else UNSET_LABEL


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
