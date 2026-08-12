"""入力文字の正規化 (VBA `ToNarrowAlphaNumeric` / `NormalizeKeyLocal` の移植)

現場では全角で入力されたり小文字で入力されたりするので、検索の前に
文字種を揃える。VBAは `StrConv(vbNarrow)` / `StrConv(vbUpperCase)` を
使っていたが、Pythonの標準ライブラリだけで同じことをする。
"""
from __future__ import annotations

# 全角→半角の対応(数字・英大文字・英小文字)。
# 全角0(U+FF10)〜9, A(U+FF21)〜Z, a(U+FF41)〜z が半角に連続で対応する
_FULLWIDTH_BASES = ((0xFF10, ord("0"), 10),
                    (0xFF21, ord("A"), 26),
                    (0xFF41, ord("A"), 26))   # 小文字は大文字に寄せる

# 掛け算記号。「4x8」「4X8」「4×8」を1つに揃える(VBA `NormalizeKeyLocal`)
MULTIPLY_SIGN = "×"
_MULTIPLY_SOURCES = {"x", "X", "Ｘ", "ｘ", MULTIPLY_SIGN}


def _to_halfwidth_upper(char: str) -> str:
    """1文字を半角の大文字に寄せる。対象外はそのまま返す。"""
    code = ord(char)
    for base, target, size in _FULLWIDTH_BASES:
        if base <= code < base + size:
            return chr(code - base + target)
    if "a" <= char <= "z":
        return char.upper()
    return char


def to_narrow_alnum(text: str) -> str:
    """VBA `ToNarrowAlphaNumeric` の移植。

    半角化 → 大文字化 したうえで、**英数字だけを残す**。
    ロット番号の入力欄に使う(記号や空白が混ざっても検索が通るように)。

        "ｒ４０８５ｑ０" -> "R4085Q0"
        "r-4085 q0"     -> "R4085Q0"
    """
    return "".join(c for c in (_to_halfwidth_upper(ch) for ch in text or "")
                   if c.isascii() and c.isalnum())


def normalize_key(text: str) -> str:
    """VBA `NormalizeKeyLocal` の移植。

    半角化・大文字化はするが**英数字以外も残す**点が
    `to_narrow_alnum` と違う。業界・記号の突き合わせに使うので、
    「4x8」「４×８」「4X8」がすべて同じキーになる必要がある。
    """
    result = []
    for char in (text or "").strip():
        if char in _MULTIPLY_SOURCES:
            result.append(MULTIPLY_SIGN)
        else:
            result.append(_to_halfwidth_upper(char))
    return "".join(result)


def is_allowed_lot_char(char: str) -> bool:
    """ロット番号の入力欄で受け付ける文字か(VBA `TXT_KeyPress` 相当)。

    VBAは英数字以外のキー入力をその場で捨てていた。
    全角で入ってくる場合もあるので、半角化した結果で判定する。
    """
    return bool(char) and to_narrow_alnum(char) != ""
