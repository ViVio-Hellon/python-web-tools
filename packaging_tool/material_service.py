"""資材選択の支援ロジック: 保護材選定・看板在庫(在庫薄)判定

VBA `PalletHistoryModule_v2` 内の以下の関数群の移植(実ソースを逐語で
確認して移植した):
    GetUpperPartMaterial / CheckCondition / CheckThickness / CheckWidth /
    FindInList                                   -> 保護材選定
    BoardKanbanTables / ParseKanbanSize /
    MakeBoardKey / BuildBoardStockMap / IsBoardLow -> 看板在庫薄判定
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from . import config, db
from .logging_utils import get_logger

log = get_logger("material_service")

# 保護材自動選定の対象となる包装仕様書No一覧(VBA `C_TARGET_HOSONOS`)。
# 【仮運用】既定では下の `HOSOZAI_APPLY_ALL_PACKS` によりこの一覧での
# 絞り込みは効かない(全包装仕様が対象)。一覧そのものは、旧動作へ
# 戻すときのために残してある。
TARGET_HOSONOS = (
    "1P0001", "1P0104", "1P0110", "1P0115", "1P0116", "1P0118", "1P0119", "1P0120",
)

# 【仮運用】TARGET_HOSONOS(VBA由来、対象8件のみ)に含まれない包装仕様でも
# 梱包保護材テーブルを参照するか(現場の要望:「全包装仕様に対応できるか
# 確認したい」)。既定で有効。うまくいかなければ環境変数
# `PACKAGING_TOOL_HOSOZAI_ALL_PACKS=0` で TARGET_HOSONOS 限定の旧動作へ
# コード変更なしで戻せる(現場の要望:「一旦停止という扱いでTARGET_HOSONOS
# は残してください」)。
HOSOZAI_APPLY_ALL_PACKS = os.environ.get(
    "PACKAGING_TOOL_HOSOZAI_ALL_PACKS", "1") != "0"

# 保護材選定結果が「アングルを使う」ことを意味する値(VBA `C_HOSOZAI_ANGLE`)
HOSOZAI_ANGLE = "アングル"


# ------------------------------------------------------------------
# 文字列正規化・リストマッチング (FindInList / CheckCondition)
# ------------------------------------------------------------------
def _zenkaku_ascii_to_hankaku(s: str) -> str:
    """全角英数記号(U+FF01-FF5E)を半角に変換する。VBA `StrConv(s, vbNarrow)` の近似移植。

    対象フィールド(材質/調質/用途コード等)は英数記号コードのみを想定しており、
    全角カタカナの半角化(NFKCとは逆方向の変換が必要)は対象外としている。
    """
    out_chars = []
    for c in s:
        code = ord(c)
        if 0xFF01 <= code <= 0xFF5E:
            out_chars.append(chr(code - 0xFEE0))
        elif code == 0x3000:  # 全角スペース
            out_chars.append(" ")
        else:
            out_chars.append(c)
    return "".join(out_chars)


def find_in_list(tmp: str, criteria: str, *, blank_flag: str = "") -> bool:
    """VBA `FindInList` の移植。

    `tmp` = 探される側(マスタ行の条件文字列。カンマ区切りで複数指定・
    `≠`による除外・`*`によるワイルドカードをサポート)、
    `criteria` = 探す側(実際の値)。

    肯定条件(≠なし)は「後勝ちOR評価」: 一致しても即終了せず、後続に
    `≠`条件があればそちらが優先してFalseに上書きされる。
    """
    if blank_flag == "" and tmp == "":
        return True

    result = False
    for raw_item in tmp.split(","):
        item = raw_item.strip()
        if not item:
            continue

        if item.startswith("≠"):
            compare = item[1:]
            if compare != criteria:
                result = True
            else:
                return False

            if compare.startswith("*") and compare.endswith("*") and len(compare) >= 2:
                mid = compare[1:-1]
                if mid in criteria:
                    return False
            elif compare.startswith("*"):
                mid = compare[1:]
                if criteria.endswith(mid):
                    return False
            elif compare.endswith("*"):
                left = compare[:-1]
                if criteria.startswith(left):
                    return False
            else:
                if compare == criteria:
                    return False

        elif item.startswith("*") and item.endswith("*") and len(item) >= 2:
            mid = item[1:-1]
            if mid in criteria:
                result = True
        elif item.startswith("*"):
            mid = item[1:]
            if criteria.endswith(mid):
                result = True
        elif item.endswith("*"):
            left = item[:-1]
            if criteria.startswith(left):
                result = True
        else:
            if item == criteria:
                result = True

    return result


def check_condition(cell_value: Optional[str], criteria: str, *, blank_flag: str = "") -> bool:
    """VBA `CheckCondition` の移植。マスタ行の値を正規化してFindInListへ渡す。"""
    cell_value = cell_value or ""
    normalized = _zenkaku_ascii_to_hankaku(cell_value) if cell_value else ""
    normalized = normalized.replace("ｰ", "-")  # 半角カナ長音記号の誤入力対策(元VBA踏襲)
    return find_in_list(normalized, criteria, blank_flag=blank_flag)


def _check_range(lower, upper, middle, value) -> bool:
    """VBA `CheckThickness`/`CheckWidth` 共通ロジック。

    下限・上限・中央列(表示用、比較には使わない)が3つとも空欄なら
    無条件でTrue(範囲制限なし)。それ以外は下限<=value<=上限か判定する。
    """
    if lower is None and upper is None and middle is None:
        return True
    lo = float(lower) if lower is not None else 0.0
    hi = float(upper) if upper is not None else 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    return lo <= v <= hi


# ------------------------------------------------------------------
# 保護材選定 (GetUpperPartMaterial)
# ------------------------------------------------------------------
def get_upper_part_material(
    conn: sqlite3.Connection,
    *,
    pack: str,
    zai: str,
    tyo: str,
    you: str,
    atu,
    hab,
    tak,
) -> str:
    """VBA `GetUpperPartMaterial` の移植。

    包装仕様書Noが空欄の場合は空文字列を返す(対象外、"一致なし"とは
    区別される)。【仮運用】`HOSOZAI_APPLY_ALL_PACKS` が無効化されている
    ときだけ、`TARGET_HOSONOS`(VBA由来、対象8件のみ)に含まれない
    包装仕様も同様に対象外とする。対象内であれば、
    Pass1(包装仕様書が指定されている行を優先)→Pass2(包装仕様書が
    空欄の汎用行)の順で材質・調質・用途コード・板厚/板幅/板丈の
    範囲条件に一致する最初の行の `使用保護材` を返す。
    どちらのパスでも一致しなければ "一致なし" を返す。
    """
    pack = (pack or "").strip()
    if not pack:
        log.debug("get_upper_part_material: 包装仕様書Noが空のため対象外")
        return ""
    if not HOSOZAI_APPLY_ALL_PACKS and pack not in TARGET_HOSONOS:
        log.debug("get_upper_part_material: 対象外 pack=%s", pack)
        return ""

    def _matches(row: sqlite3.Row, *, check_pack: bool) -> bool:
        if check_pack and not check_condition(row["包装仕様書"], pack, blank_flag="NoBlank"):
            return False
        return (
            check_condition(row["材質"], zai)
            and check_condition(row["調質"], tyo)
            and check_condition(row["用途コード"], you)
            and _check_range(row["板厚下"], row["板厚上"], row["板厚"], atu)
            and _check_range(row["板幅下"], row["板幅上"], row["板幅"], hab)
            and _check_range(row["板丈下"], row["板丈上"], row["板丈"], tak)
        )

    pass1_rows = db.fetch_all(
        conn,
        "SELECT * FROM 梱包保護材 WHERE 包装仕様書 IS NOT NULL AND 包装仕様書 <> '' ORDER BY 管理番号",
        caller_name="get_upper_part_material.pass1",
    ) or []
    for row in pass1_rows:
        if _matches(row, check_pack=True):
            result = row["使用保護材"] or ""
            log.debug("get_upper_part_material Pass1ヒット 使用保護材=%s", result)
            return result

    pass2_rows = db.fetch_all(
        conn,
        "SELECT * FROM 梱包保護材 WHERE 包装仕様書 IS NULL OR 包装仕様書 = '' ORDER BY 管理番号",
        caller_name="get_upper_part_material.pass2",
    ) or []
    for row in pass2_rows:
        if _matches(row, check_pack=False):
            result = row["使用保護材"] or ""
            log.debug("get_upper_part_material Pass2ヒット 使用保護材=%s", result)
            return result

    log.debug("get_upper_part_material: 一致なし pack=%s", pack)
    return "一致なし"


# ------------------------------------------------------------------
# 看板在庫薄判定 (BuildBoardStockMap / IsBoardLow)
# ------------------------------------------------------------------
def parse_kanban_size(s: Optional[str]) -> Optional[tuple[int, int]]:
    """VBA `ParseKanbanSize` の移植。

    "660 × 1050 : 100枚" のような自由形式の文字列から、区切り文字に
    依存せず数字の並びを2つ(幅・丈)拾う。":" 以降(数量)は無視する。
    数字が2つ取れなければ `None` を返す。
    """
    s = (s or "").strip()
    if not s:
        return None
    colon_idx = s.find(":")
    dim_part = s[:colon_idx] if colon_idx >= 0 else s
    dim_part = _zenkaku_ascii_to_hankaku(dim_part)  # 全角数字を半角化

    nums: list[int] = []
    buf = ""
    for ch in dim_part:
        if "0" <= ch <= "9":
            buf += ch
        elif buf:
            nums.append(int(buf))
            buf = ""
    if buf:
        nums.append(int(buf))

    if len(nums) < 2:
        return None
    return nums[0], nums[1]


def make_board_key(board_type: Optional[str], w: int, l: int) -> str:
    """VBA `MakeBoardKey` の移植。複合キー: タイプ|幅|丈。"""
    return f"{(board_type or '').strip()}|{w}|{l}"


def build_board_stock_map(conn: sqlite3.Connection) -> dict[str, str]:
    """VBA `BuildBoardStockMap` の移植。

    `config.BOARD_KANBAN_TABLES` (6テーブル)を横断し、ボードキーごとに
    在庫状態("有"=在庫あり/"低"=在庫薄)のマップを作る。
    集約ルール: どこかのテーブルで 不='〇'(在庫あり)なら"有"を最優先で
    確定する(後から他テーブルで"低"と判定されても上書きされない)。
    "有"が一度も無く 欲='〇'(欲しい=品薄)なら"低"とする。
    """
    stock_map: dict[str, str] = {}
    for table in config.BOARD_KANBAN_TABLES:
        rows = db.fetch_all(
            conn, f"SELECT 資材, サイズ, 欲, 不 FROM [{table}]",
            caller_name=f"build_board_stock_map:{table}",
        ) or []
        for row in rows:
            parsed = parse_kanban_size(row["サイズ"])
            if parsed is None:
                continue
            w, l = parsed
            key = make_board_key(row["資材"], w, l)
            in_stock = (row["不"] or "").strip() == "〇"
            is_out = (row["欲"] or "").strip() == "〇"
            if in_stock:
                stock_map[key] = "有"
            elif is_out and key not in stock_map:
                stock_map[key] = "低"
    return stock_map


def is_board_low(stock_map: Optional[dict[str, str]], board_type: str, w: int, l: int) -> bool:
    """VBA `IsBoardLow` の移植。未登録は False(=在庫薄と扱わず、選定を妨げない)。"""
    if not stock_map:
        return False
    key = make_board_key(board_type, w, l)
    return stock_map.get(key) == "低"
