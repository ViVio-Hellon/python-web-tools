"""資材選択(パレット/製品サイズ設定・候補ボード一覧)の業務ロジック

VBA `MaterialMasterForm` の初期化・一覧構築・サイズ確定ハンドラの移植
(実ソースを1行ずつ確認して移植した)。対象:
    InitializePalletSizeList / FilterPalletList  -> list_pallet_sizes
    InitializeAvailableBoards                     -> list_available_boards
    (cboBoardTypeの初期候補)                        -> list_board_types
    btnApplyPalette_Click                           -> apply_pallet_size
    btnApplyProductSize_Click                        -> apply_product_size
    btnAutoSelectPallet_Click(製品未入力時)            -> search_pallet_direct
    btnAutoSelectPallet_Click(通常時、16/20パス探索)     -> auto_select_pallet

【移植で変更した点】
    - VBA版はAccessへの往復を避けるため、PalletMaster全件を一度だけ
      メモリ配列にキャッシュし(`mPalletRecords`)、EX表示切替や
      検索条件変更のたびにその配列を再フィルタしていた
      (`InitializePalletSizeList`と`FilterPalletList`は実質同じ
      フィルタロジックの重複だった)。SQLite版は毎回問い合わせても
      十分高速なため、この2つを`list_pallet_sizes`1つに統合した。
    - 手動追加(`btnAddBoardUpper/Lower_Click`)の「幅が製品幅以下か」の
      検証は、**原文でもコメントアウトされていた**(意図的に外されて
      いる)ので入れていない。枚数の範囲は原文のスピンボタンから。

【段ごとに分けてある】
ここは**外向けの1枚窓**で、中身は段ごとのモジュールが持つ。

    pallet_common      型と「その行を出してよいか」の判断
    pallet_list        一覧の絞り込み(全件・直接検索・製品サイズ適合)
    pallet_auto_select 16/20パスの自動選定
    ここ               寸法の確定と、ボードの一覧・手動追加

**下線で始まる名前(段の内側)はここから公開しない** ── 使うときは
持ち主のモジュールから引く。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import db, material_service
from .logging_utils import get_logger

# ------------------------------------------------------------------
# **ここは外向けの1枚窓。** 中身は段ごとのモジュールが持ち、呼ぶ側は
# このモジュールだけを見ればよいようにする。
#
#     pallet_common      型と「その行を出してよいか」の判断
#     pallet_list        一覧の絞り込み(全件・直接検索・製品サイズ適合)
#     pallet_auto_select 16/20パスの自動選定
#     ここ               寸法の確定と、ボードの一覧・手動追加
# ------------------------------------------------------------------
from .pallet_common import (  # noqa: F401
    ApplyResult, AutoSelectPalletResult, BoardRow, KIND_LEN2, KIND_WIDTH2,
    MAX_NEAR_MISS_LOGS, Palette, PalletSizeRow, ProductSize, SelectedBoard,
    SelectedBoards, UNITS_DEFAULT, UNITS_WITH_HOSOZAI, _is_numeric,
    _row_to_pallet_size_row, build_matrix_suppress_map, is_suppressed_matrix,
    unit_allowed)
from .pallet_list import (  # noqa: F401
    find_pallet_row, list_pallet_sizes, list_pallets_by_product_dims,
    list_pallets_for_product, search_pallet_direct)
from .pallet_auto_select import auto_select_pallet  # noqa: F401

log = get_logger("board_selection_service")

# 手動追加1回あたりの枚数(VBA `spnBoardCount` の `.Min` / `.Max`)
COUNT_MIN = 1
COUNT_MAX = 100

# 断りの種別。Web版はこれで HTTP の 400 と 422 を選ぶ(設計書 §6.1)。
# 文言から推し量ると、文言を直した日に区別が壊れる
REFUSE_BAD_INPUT = "bad_input"   # 数値でない・0以下
REFUSE_BUSINESS = "business"     # 形は正しいが業務として通せない

# パレットのはみ出し許容係数(VBA `PALETTE_OVERHANG_RATIO`)
PALETTE_OVERHANG_RATIO = 1.10


def apply_pallet_size(width_text: str, length_text: str) -> tuple[ApplyResult, Palette]:
    """VBA `btnApplyPalette_Click` の移植。10%の食み出し許容(overhang)を設定する。"""
    if not (_is_numeric(width_text) and _is_numeric(length_text)):
        return ApplyResult(False, "パレットサイズには数値を入力してください。", REFUSE_BAD_INPUT), Palette()

    width, length = int(float(width_text)), int(float(length_text))
    if width <= 0 or length <= 0:
        return ApplyResult(False, "パレットサイズには正の数を入力してください。", REFUSE_BAD_INPUT), Palette()

    palette = Palette(
        width=width, length=length, overhang_ratio=PALETTE_OVERHANG_RATIO,
        max_width=width * PALETTE_OVERHANG_RATIO, max_length=length * PALETTE_OVERHANG_RATIO,
    )
    return ApplyResult(True, f"パレット: {width} × {length} (設定済)"), palette


def apply_product_size(
    width_text: str, length_text: str, palette: Palette,
) -> tuple[ApplyResult, ProductSize, bool]:
    """VBA `btnApplyProductSize_Click` の移植。

    製品サイズがパレットに収まるか確認し、通常向きで収まらず回転すれば
    収まる場合は自動的に幅・丈を入れ替える。戻り値は
    (結果, 製品サイズ, 回転したか)。
    """
    if not (_is_numeric(width_text) and _is_numeric(length_text)):
        return ApplyResult(False, "製品サイズには数値を入力してください。", REFUSE_BAD_INPUT), ProductSize(), False

    width, length = int(float(width_text)), int(float(length_text))
    if width <= 0 or length <= 0:
        return ApplyResult(False, "製品サイズには正の数を入力してください。", REFUSE_BAD_INPUT), ProductSize(), False

    if not palette.is_set:
        return ApplyResult(False, "先にパレットサイズを設定してください。", REFUSE_BUSINESS), ProductSize(), False

    normal_fit = width <= palette.width and length <= palette.length
    rotated_fit = width <= palette.length and length <= palette.width
    if not normal_fit and not rotated_fit:
        message = (
            f"製品サイズがパレットに収まりません。\n"
            f"パレット: {palette.width} × {palette.length}\n"
            f"製品: {width} × {length}"
        )
        return ApplyResult(False, message, REFUSE_BUSINESS), ProductSize(), False

    rotated = False
    if not normal_fit and rotated_fit:
        width, length = length, width
        rotated = True

    note = " ※回転済(幅丈入替)" if rotated else ""
    return ApplyResult(True, f"製品: {width} × {length} (設定済){note}"), ProductSize(width=width, length=length), rotated

def list_board_types(conn: sqlite3.Connection) -> list[str]:
    """VBA `InitializeAvailableBoards` 内の `cboBoardType` 初期候補構築の移植。

    ボード幅昇順で読み込みながら、初出順の重複無しリストを作る
    (アルファベット順ソートではない、元VBA仕様)。
    """
    rows = db.fetch_all(
        conn, "SELECT ボードタイプ FROM BoardMaster ORDER BY ボード幅", caller_name="list_board_types",
    ) or []
    seen: list[str] = []
    for row in rows:
        t = row["ボードタイプ"]
        if t and t not in seen:
            seen.append(t)
    return seen

def list_available_boards(
    conn: sqlite3.Connection, board_type: str = "", *, stock_map: Optional[dict[str, str]] = None,
) -> list[BoardRow]:
    """VBA `InitializeAvailableBoards` の移植。`board_type` 指定時のみ絞り込む。"""
    sql = "SELECT * FROM BoardMaster"
    params: tuple = ()
    if board_type:
        sql += " WHERE ボードタイプ = ?"
        params = (board_type,)
    sql += " ORDER BY ボード幅"
    rows = db.fetch_all(conn, sql, params, caller_name="list_available_boards") or []

    result = []
    for row in rows:
        low = False
        if stock_map:
            low = material_service.is_board_low(stock_map, row["ボードタイプ"], row["ボード幅"], row["ボード丈"])
        result.append(BoardRow(
            id=row["管理番号"], width=row["ボード幅"], length=row["ボード丈"],
            board_type=row["ボードタイプ"], stock_low=low,
        ))
    return result

def add_selected_board(target: list[SelectedBoard], width: int, length: int, count: int) -> ApplyResult:
    """手動でのボード追加(VBA `btnAddBoardUpper/Lower_Click`)。

    **原文を確認済み。** 「幅が製品幅以下か」の検証は原文でも
    **コメントアウトされていた**(意図的に外されている)ので、
    こちらにも入れない。手で足す以上、一覧に無い寸法でなければ
    通す ── 収まるかどうかは配置が答える。

    枚数の範囲は原文のスピンボタン(`spnBoardCount` の `.Min = 1`
    `.Max = 100`)から。原文はテキスト欄に直接打てば素通りしたが、
    ここでは**打った値も同じ範囲で見る**。上限が無いと、配置が
    現実に無い枚数を並べようとして黙って時間を使う。
    """
    if not COUNT_MIN <= count <= COUNT_MAX:
        return ApplyResult(
            False, f"枚数は{COUNT_MIN}〜{COUNT_MAX}で入力してください。",
            REFUSE_BAD_INPUT)
    target.append(SelectedBoard(width=width, length=length, count=count))
    return ApplyResult(True, "追加しました。")
