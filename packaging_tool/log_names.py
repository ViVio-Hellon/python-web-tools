"""ログの置き場所と名前の決まり(日報複合ツールに合わせる。VER4.8.111〜)

現場の決まり:

1. 置き場所: ``<ログの出力パス>\\<ツールID>\\`` フォルダを作り、その中に書く。出力パスの直下に
   ファイルを直接置かない。ツールIDは半角英小文字(このツールは ``packagingtool``)
2. ファイル名: 必ず ``<ツールID>_`` で始める。続けて 種類(必要なら)・PC名・日付。
   **PC名は必ず入れる**(共有フォルダで何台もが同じファイルに書かないため)
       packagingtool\\packagingtool_<PC>_20261024.log            動作ログ
       packagingtool\\packagingtool_エラー記録_<PC>_202610.jsonl   エラー記録
       packagingtool\\packagingtool_取り込み診断_<PC>_20261024.log 取り込み診断
3. 出力パスを決めていない・届かないときは、この端末のローカルの logs の下に同じ形で書く
   (``logs\\packagingtool\\packagingtool_…``)。ログが書けないことで止めない
4. 前の場所・前の名前のログは、移さない・消さない・もう書かない。読む・探す・古いものを
   片付ける処理は、前の場所の自分のログも読む(`trace_log.sources`)
5. ほかのツールのファイル・フォルダは読まない・消さない。古いログを消すのは、自分のツールの、
   この端末のものだけ

ここは**名前だけ**を持つ(どのフォルダを見るかは `trace_log`)。ほかのモジュールを読み込まない
(`config` と `logging_utils` がここを使うので、循環しないように)。
"""
from __future__ import annotations

import platform
import re
from datetime import date, datetime
from typing import Optional

#: ツールID(半角英小文字)。フォルダ名とファイル名の頭
TOOL_ID = "packagingtool"

#: 種類
KIND_ERROR = "エラー記録"
KIND_DIAG = "取り込み診断"

#: 前の版のツール名(VER4.8.107〜4.8.110 のフォルダ名・ファイル名の頭)
LEGACY_TOOL = "packaging_tool"

# --- いまの名前 ---------------------------------------------------------
NEW_ERROR = re.compile(rf"^{TOOL_ID}_{KIND_ERROR}_(?P<pc>.+)_(?P<ym>\d{{6}})\.jsonl$")
NEW_DIAG = re.compile(rf"^{TOOL_ID}_{KIND_DIAG}_(?P<pc>.+)_(?P<d>\d{{8}})\.log$")
NEW_DAY = re.compile(rf"^{TOOL_ID}_(?!{KIND_DIAG}_|{KIND_ERROR}_)(?P<pc>.+)_(?P<d>\d{{8}})\.log$")

# --- 前の名前(PC名が無い。どの端末のものかはフォルダで決まる) -----------------
OLD_DAY = re.compile(rf"^{LEGACY_TOOL}_(?P<d>\d{{8}})\.log$")
OLD_DIAG = re.compile(rf"^(?:{LEGACY_TOOL}_)?{KIND_DIAG}_(?P<d>\d{{8}})\.log$")
OLD_ERROR = re.compile(rf"^(?:{LEGACY_TOOL}_)?{KIND_ERROR}_(?P<ym>\d{{6}})\.jsonl$")

_pc_cache: Optional[str] = None
_finding = False


def safe(name: str) -> str:
    """ファイル名・フォルダ名に使えない字を置き換える。"""
    return re.sub(r'[\\/:*?"<>|]', "_", name or "").strip() or "この端末"


def pc_name() -> str:
    """この端末の名前(ファイル名に入れる形)。1度引いたら覚えておく(ログを書くたびに引かない)。"""
    global _pc_cache, _finding
    if _pc_cache:
        return _pc_cache
    name = ""
    if not _finding:                       # 引く途中でログを書いても、ここへ戻ってこない
        _finding = True
        try:
            from . import access_control
            name = access_control.current_identity().pc_name or ""
        except Exception:                  # noqa: BLE001 - 名前が引けなくてもログは書く
            name = ""
        finally:
            _finding = False
    if not name:
        name = platform.node() or ""
    _pc_cache = safe(name)
    return _pc_cache


def forget_pc_name() -> None:
    """試験用。次に引き直す。"""
    global _pc_cache
    _pc_cache = None


def day_log(day: date, pc: Optional[str] = None) -> str:
    return f"{TOOL_ID}_{pc or pc_name()}_{day:%Y%m%d}.log"


def error_log(when: datetime, pc: Optional[str] = None) -> str:
    return f"{TOOL_ID}_{KIND_ERROR}_{pc or pc_name()}_{when:%Y%m}.jsonl"


def diag_log(day: date, pc: Optional[str] = None) -> str:
    return f"{TOOL_ID}_{KIND_DIAG}_{pc or pc_name()}_{day:%Y%m%d}.log"


def old_day_log(day: date) -> str:
    """前の版の動作ログの名前(前の場所の自分のログを読むため)。"""
    return f"{LEGACY_TOOL}_{day:%Y%m%d}.log"
