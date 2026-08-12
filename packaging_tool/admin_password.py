"""管理者パスワード ── 現場で変えられるようにする

【何のためのものか】
実績パターンの保存ボタンを**誤って押されない**ためのUIガードです
(VBA版 `MaterialMasterForm` の `ADMIN_PASSWORD` から引き継いだ役目)。
本来の意味でのアクセス制御ではありません ── 誰がその端末を使えるかは
`access_control`(アクセス権限マスタ)が決めます。

【なぜ変えられるようにするのか】
これまでは `config.py` の値か環境変数だけで、**運用では変えられません
でした。** 変えられないパスワードは、実質「変えない」と同じです。
人が入れ替わっても直せず、結局みんなが同じ値を知っている状態が続きます。

【平文で持たない】
配布はフォルダごとコピーなので、設定ファイルは**そのまま持ち出せます**。
UIガードとはいえ平文で置く理由が無いので、PBKDF2 で撹拌して持ちます。
これは「盗まれても困らない」ためではなく、**ついでに漏れない**ため。

【変えていないときは、これまでどおり】
一度も変えていない端末では `config.ADMIN_PASSWORD`(環境変数で上書き可)
がそのまま通ります。入れ替えただけで認証が通らなくなる、を作りません。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Optional

from . import config, user_settings
from .logging_utils import get_logger

log = get_logger("admin_password")

# 設定に入れる鍵。値は撹拌済みの文字列で、平文は入らない
KEY = "admin_password"

# 撹拌の仕様。`pbkdf2$<繰り返し>$<塩>$<結果>` の形で1つの文字列に畳む
SCHEME = "pbkdf2"
ITERATIONS = 200_000
SALT_BYTES = 16

# 短すぎるものは断る。**UIガードなので厳しくはしない** ── 長さの規則を
# 増やすほど、現場は紙に書いて画面に貼る
MIN_LENGTH = 4

# 断りの種類。**文言から推し量らない**(設計.md §1)
REFUSE_WRONG = "wrong_password"     # いまのパスワードが違う
REFUSE_TOO_SHORT = "too_short"      # 新しいパスワードが短い
REFUSE_MISMATCH = "mismatch"        # 確認用と一致しない
REFUSE_SAME = "same"                # 変わっていない


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""


# ==================================================================
# 撹拌
# ==================================================================
def _encode(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, iterations)
    return "$".join([SCHEME, str(iterations),
                     base64.b64encode(salt).decode("ascii"),
                     base64.b64encode(digest).decode("ascii")])


def _matches(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != SCHEME:
            return False
        expected = _encode(password, base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):               # 壊れた値
        return False
    return hmac.compare_digest(expected, stored)


# ==================================================================
# 使う
# ==================================================================
def _stored() -> Optional[str]:
    value = user_settings.get(KEY)
    return value if isinstance(value, str) and value.strip() else None


def is_custom() -> bool:
    """この端末で変えてあるか。設定画面に出す(値そのものは出さない)。"""
    return _stored() is not None


def verify(password: str) -> bool:
    """合っているか。**照合はここでしかしない。**"""
    stored = _stored()
    if stored is not None:
        return _matches(str(password), stored)
    # 一度も変えていない端末。これまでどおり `config` の値で通す
    return hmac.compare_digest(str(password).encode("utf-8"),
                               config.ADMIN_PASSWORD.encode("utf-8"))


def change(current: str, new: str, confirm: str) -> Result:
    """変える。**いまのパスワードを知っている人だけ。**

    肩越しに見ていた人が勝手に変えられる、を作らないための確認です。
    """
    if not verify(current):
        # **何が違うのかは言わない。** 「そのパスワードは存在しない」等を
        # 返すと、総当たりの手がかりになる
        log.warning("管理者パスワードの変更に失敗しました(いまの値が違う)")
        return Result(False, "いまのパスワードが違います。", REFUSE_WRONG)
    new = str(new)
    if len(new) < MIN_LENGTH:
        return Result(False, f"新しいパスワードは{MIN_LENGTH}文字以上にしてください。",
                      REFUSE_TOO_SHORT)
    if new != str(confirm):
        return Result(False, "確認用と一致しません。", REFUSE_MISMATCH)
    if verify(new):
        return Result(False, "いまと同じパスワードです。", REFUSE_SAME)

    user_settings.save(KEY, _encode(new, os.urandom(SALT_BYTES)))
    log.info("管理者パスワードを変更しました")
    return Result(True, "管理者パスワードを変えました。")


def reset(current: str) -> Result:
    """既定に戻す(`config.ADMIN_PASSWORD` / 環境変数)。

    忘れたときの逃げ道は**ファイルを消すこと**です ── 設定ファイルの
    `admin_password` の行を消せば既定に戻ります。画面から「忘れた」で
    戻せるようにすると、確認そのものが意味を失います。
    """
    if not verify(current):
        return Result(False, "いまのパスワードが違います。", REFUSE_WRONG)
    user_settings.save(KEY, "")
    log.info("管理者パスワードを既定に戻しました")
    return Result(True, "既定のパスワードに戻しました。")


def token() -> str:                              # pragma: no cover - 予備
    """検証用に使い捨ての値を作る。"""
    return secrets.token_hex(8)
