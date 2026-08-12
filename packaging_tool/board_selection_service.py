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
    - VBA版はボード選定アルゴリズム未着手の段階のため、選定リストへの
      手動追加(`btnAddBoardUpper/Lower_Click`)の詳細な検証条件は
      未確認(別モジュールとして次フェーズで詳細を確認する)。
      現時点の`add_selected_board`は数量>0の確認のみを行う簡易版。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from . import config, db, material_service
from .logging_utils import get_logger
from .user_log import UserLog

log = get_logger("board_selection_service")

# VBA版はbtnApplyPalette_Click/LoadSinglePatternに `1.1` を直接ハードコードして
# いた(名前付き定数は無かった)。Python版では明示的な定数として抽出する。
PALETTE_OVERHANG_RATIO = 1.10


def _is_numeric(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------
# パレットサイズ
# ------------------------------------------------------------------
@dataclass
class Palette:
    width: int = 0
    length: int = 0
    overhang_ratio: float = 0.0
    max_width: float = 0.0
    max_length: float = 0.0

    @property
    def is_set(self) -> bool:
        return self.width > 0 and self.length > 0


@dataclass
class ProductSize:
    width: int = 0
    length: int = 0

    @property
    def is_set(self) -> bool:
        return self.width > 0 and self.length > 0


# 断りの種類。**入力の形の誤り**と**業務としての断り**を区別する。
# Web版はこれで HTTP の 400 と 422 を選ぶ(設計書 §6.1)。
# 文言から推し量ると、文言を直した日に区別が壊れる
REFUSE_BAD_INPUT = "bad_input"   # 数値でない・0以下
REFUSE_BUSINESS = "business"     # 形は正しいが業務として通せない


@dataclass
class ApplyResult:
    ok: bool
    message: str = ""
    reason: str = ""             # 断ったときだけ入る(上の2種)


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


@dataclass
class PalletSizeRow:
    id: int
    width: int
    length: int
    w_min: int
    w_max: int
    l_min: int
    l_max: int
    industry: str
    symbol: str
    leg_count: int
    keta: int
    code: str
    unit: str


def _row_to_pallet_size_row(row: sqlite3.Row) -> PalletSizeRow:
    return PalletSizeRow(
        id=row["管理番号"], width=row["幅"], length=row["丈"],
        w_min=row["巾適合min"], w_max=row["巾適合max"],
        l_min=row["丈適合min"], l_max=row["丈適合max"],
        industry=row["業界"] or "", symbol=(row["記号"] or "").strip(),
        leg_count=row["脚数"], keta=row["桁数"],
        code=row["コード"] or "", unit=row["単位"] or "",
    )


def find_pallet_row(conn: sqlite3.Connection, width: int, length: int, symbol: str) -> Optional[PalletSizeRow]:
    """幅・丈・記号からPalletMasterの1行を引く(コード・単位を知りたいだけの単純参照)。

    一覧表示側のフィルタ(単位/EX/製品サイズ適合)には関係なく、画面に
    表示されている行の実体をそのまま引けるようにする(クリック時の
    単位・コード表示 = VBA `DynamicTip` 用)。
    """
    rows = db.fetch_all(
        conn, "SELECT * FROM PalletMaster WHERE 幅 = ? AND 丈 = ?",
        (width, length), caller_name="find_pallet_row") or []
    for row in rows:
        if (row["記号"] or "").strip() == symbol:
            return _row_to_pallet_size_row(row)
    return None


def list_pallet_sizes(
    conn: sqlite3.Connection,
    *,
    show_all: bool = False,
    ex_only: bool = False,
    is_ex_order: bool = False,
    last_hosozai: str = "",
) -> list[PalletSizeRow]:
    """VBA `InitializePalletSizeList`/`FilterPalletList` の移植(統合版)。

    フィルタ規則(元VBAと同一):
        - 幅・丈が0の行は常に除外
        - EXオンリーモード(is_ex_order かつ ex_only)は 記号にEXを含む行のみ
        - show_all=Trueなら(EXオンリーでない限り)フィルタ無しで全件
        - それ以外は「単位フィルタ」と「EX除外」を両方適用:
            単位フィルタ: last_hosozaiが設定されていて、かつ
              アングル/一致なし のいずれでもなければ 単位∈{台,組}、
              それ以外は 単位=台 のみ許可
            EX除外: 記号に"EX"を含む行(大文字小文字問わず)は除外
    """
    rows = db.fetch_all(conn, "SELECT * FROM PalletMaster ORDER BY 幅", caller_name="list_pallet_sizes") or []
    ex_only_mode = is_ex_order and ex_only

    result: list[PalletSizeRow] = []
    for row in rows:
        w, l = row["幅"], row["丈"]
        if not w or not l:
            continue
        symbol = (row["記号"] or "").strip()
        is_ex = "EX" in symbol.upper()

        if ex_only_mode:
            if not is_ex:
                continue
        elif not show_all:
            unit = (row["単位"] or "").strip()
            if last_hosozai and last_hosozai not in (material_service.HOSOZAI_ANGLE, "一致なし"):
                unit_ok = unit in ("台", "組")
            else:
                unit_ok = unit == "台"
            if not unit_ok or is_ex:
                continue

        result.append(_row_to_pallet_size_row(row))
    return result


# ------------------------------------------------------------------
# ボード一覧
# ------------------------------------------------------------------
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


@dataclass
class BoardRow:
    id: int
    width: int
    length: int
    board_type: str
    stock_low: bool = False


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


# ------------------------------------------------------------------
# 選定済みボードリスト(手動追加/削除。自動選定アルゴリズムは次フェーズ)
# ------------------------------------------------------------------
@dataclass
class SelectedBoard:
    width: int
    length: int
    count: int
    tag: str = ""  # '主' / '幅補填' / '丈補填' / 'カット前提' 等(自動選定フェーズで使用予定)


@dataclass
class SelectedBoards:
    lower: list[SelectedBoard] = field(default_factory=list)
    upper: list[SelectedBoard] = field(default_factory=list)


def add_selected_board(target: list[SelectedBoard], width: int, length: int, count: int) -> ApplyResult:
    """手動でのボード追加(簡易版)。

    VBA `btnAddBoardUpper/Lower_Click` は「幅が対象サイズ以下か」等の
    検証を行っていたが、その詳細な条件は今回の調査範囲に含まれて
    いなかったため未移植(次フェーズで実ソースを確認して精緻化する)。
    現時点では数量が1以上であることのみ確認する。
    """
    if count <= 0:
        return ApplyResult(False, "枚数は1以上を入力してください。", REFUSE_BAD_INPUT)
    target.append(SelectedBoard(width=width, length=length, count=count))
    return ApplyResult(True, "追加しました。")


# ------------------------------------------------------------------
# パレット自動選定 (VBA `btnAutoSelectPallet_Click`、728行の移植)
#
# 元VBAは「16/20パス探索」(業界カテゴリ×厳密/±5×通常/回転の総当たり、
# 優先順位が高いパスから順に試し、最初に候補が見つかったパスの中で
# 面積最小のものを採用)と、製品サイズ未入力時の「直接検索モード」の
# 2つの動作を1つの巨大な関数で兼ねていた。Python版では役割ごとに
# search_pallet_direct / auto_select_pallet の2関数に分けている。
# ------------------------------------------------------------------

# 業界カテゴリの判定(元VBA `isSpecialInd` 等)
_SPECIAL_INDUSTRIES = ("1×2", "4×8", "4×10", "5×10", "3×6", "5×8")

# 候補ゼロのパスで「惜しかった」レコードを何件までログに出すか(VBA `showMax`)
MAX_NEAR_MISS_LOGS = 3


def _industry_category(industry: str) -> str:
    if industry in _SPECIAL_INDUSTRIES:
        return "特定業界"
    if industry == "タイト":
        return "タイト"
    if industry == "全面":
        return "全面"
    return "一般"  # "スカシ"はここに含まれる(元VBA仕様、意図的)


@dataclass
class _PassDef:
    number: int
    tolerance: int
    category: str
    orientation: str  # "normal" / "rotated" / "normal_len2" / "rotated_len2"
    label: str


def _build_pass_defs(max_pass: int) -> list[_PassDef]:
    defs: list[_PassDef] = []
    n = 1
    for category in ("特定業界", "一般", "タイト", "全面"):
        for tol in (0, 5):
            for orient, orient_label in (("normal", "通常"), ("rotated", "回転")):
                tol_label = "厳密" if tol == 0 else "±5"
                defs.append(_PassDef(n, tol, category, orient, f"{orient_label}({category}/{tol_label})"))
                n += 1
    if max_pass > 16:
        for tol in (0, 5):
            for orient, orient_label in (("normal_len2", "通常"), ("rotated_len2", "回転")):
                tol_label = "厳密" if tol == 0 else "±5"
                defs.append(_PassDef(n, tol, "丈2山", orient, f"{orient_label}(丈2山/スカシ・タイト/{tol_label})"))
                n += 1
    return defs


def _search_dims(orientation: str, pw: int, pl: int, two_stack: bool) -> tuple[int, int]:
    if orientation == "normal":
        return (pw * 2, pl) if two_stack else (pw, pl)
    if orientation == "rotated":
        return (pl * 2, pw) if two_stack else (pl, pw)
    if orientation == "normal_len2":
        return (pw, pl * 2)
    if orientation == "rotated_len2":
        return (pl, pw * 2)
    raise ValueError(orientation)


def _category_ok(pass_def: _PassDef, industry: str) -> bool:
    if pass_def.category == "丈2山":
        return industry in ("スカシ", "タイト")
    return _industry_category(industry) == pass_def.category


def _keta_ok(pass_def: _PassDef, two_stack: bool, industry: str, keta: int, leg: int) -> bool:
    if not two_stack:
        return True
    if pass_def.orientation in ("normal", "rotated"):  # 幅2山
        if industry == "スカシ":
            return True
        return keta > 0 and keta % 2 == 1
    if pass_def.orientation in ("normal_len2", "rotated_len2"):  # 丈2山
        return leg >= 3
    return True


def _pass_tag(pass_def: _PassDef, two_stack: bool) -> str:
    """ログに付ける2山モードの種別タグ(VBA `passTag`)。"""
    if pass_def.number <= 16:
        return "【幅2山】" if two_stack else ""
    return "【丈2山】"


def _reject_reason(
    row: sqlite3.Row, pass_def: _PassDef, two_stack: bool, industry: str,
    *, size_ok: bool, type_ok: bool, keta_ok: bool,
    fits: bool = True, search_w: int = 0, search_l: int = 0,
) -> str:
    """却下理由の文字列を組み立てる(VBA `reason` の移植)。

    「なぜこのパレットが検索に乗らないのか」を現場が自分で追えるよう、
    どの条件で落ちたかを"+"で連結して並べる。
    """
    parts: list[str] = []
    if not size_ok:
        # マスタの適合範囲がmin>maxで逆転している行は何を検索しても絶対に
        # ヒットしない。単に「範囲外」と出すと原因がマスタ側にあることが
        # 分からないため、区別して表示する(VBAからの改善)
        if _fit_range_inverted(row):
            parts.append(
                f"適合範囲がマスタ側で逆転(W:{row['巾適合min']}~{row['巾適合max']} "
                f"L:{row['丈適合min']}~{row['丈適合max']})")
        else:
            parts.append(
                f"範囲外(W:{row['巾適合min']}~{row['巾適合max']} "
                f"L:{row['丈適合min']}~{row['丈適合max']})")
    if not fits:
        parts.append(f"現物に載らない(製品{search_w}x{search_l} > "
                     f"パレット{row['幅']}x{row['丈']})")
    if not type_ok:
        parts.append(f"属性({industry})")
    if two_stack and not keta_ok:
        if pass_def.number <= 16:
            parts.append(f"桁数偶数({row['桁数']})")
        else:
            parts.append(f"脚数不足({row['脚数']}本)")
    return "+".join(parts)


def _fit_range_ok(row: sqlite3.Row) -> bool:
    return bool(row["巾適合min"]) and bool(row["巾適合max"]) and bool(row["丈適合min"]) and bool(row["丈適合max"])


def _fit_range_inverted(row: sqlite3.Row) -> bool:
    """適合範囲のmin>maxが逆転しているか(マスタの入力ミス)。

    実データにも70件ほど存在し、この行は何を検索しても絶対にヒットしない。
    """
    return row["巾適合max"] < row["巾適合min"] or row["丈適合max"] < row["丈適合min"]


def _size_ok(row: sqlite3.Row, search_w: int, search_l: int, tol: int) -> bool:
    return (
        row["巾適合min"] - tol <= search_w <= row["巾適合max"] + tol
        and row["丈適合min"] - tol <= search_l <= row["丈適合max"] + tol
    )


# 製品がパレットからはみ出してよい量(mm)。
#
# 実マスタでは適合max = 現物サイズ - 10(一部 -20)で入っており、
# 「製品はパレットより小さい」が守られている。つまり現物からのはみ出しは
# 本来ゼロ。ただし丸めや実測差でぴったりの行があり得るので、
# 少しだけ余裕を持たせて「明らかに載らない」ものだけを弾く。
PHYSICAL_MARGIN = 5


def _physically_fits(row: sqlite3.Row, search_w: int, search_l: int) -> bool:
    """製品がそのパレットに現物として載るか。

    【VBAからの変更】VBA版は適合範囲(巾適合min〜max / 丈適合min〜max)だけを
    見ており、パレットの現物サイズ(幅・丈)とは突き合わせていなかった。
    マスタが正しい限りは適合max < 現物サイズなので問題にならないが、
    マスタの1行が壊れると**製品より小さいパレットが選定されてしまう**。
    実マスタには適合範囲が逆転している行が70件あり、マスタが壊れうることは
    実証済みなので、現物サイズとの突き合わせを最後の砦として必ず行う。

    「1490x2970のパレットに1528x3053の製品」のような選定は、
    適合範囲が何と書いてあってもここで止まる。
    """
    width = row["幅"]
    length = row["丈"]
    if not width or not length:          # 現物サイズ未入力の行は判定できない
        return True
    return (search_w <= width + PHYSICAL_MARGIN
            and search_l <= length + PHYSICAL_MARGIN)


def _thickness_5x10_ok(symbol: str, manufactured_thickness: Optional[float]) -> tuple[bool, bool]:
    """5×10業界専用の板厚フィルタ。戻り値は (合格か, 板厚不明で警告フラグを立てるか)。"""
    if manufactured_thickness is None:
        return True, True
    if manufactured_thickness <= 13.0:
        return symbol == "強度UP", False
    return symbol != "強度UP", False


@dataclass
class AutoSelectPalletResult:
    ok: bool
    message: str = ""
    width: int = 0
    length: int = 0
    industry: str = ""
    symbol: str = ""
    pass_label: str = ""
    rotated: bool = False
    needs_thickness_warning: bool = False
    # 実際に「この寸法が載るか」を探した値(回転・2山を反映済み)。
    # 選定結果が現物に載るかを呼び出し側でも確かめられるようにする
    search_width: int = 0
    search_length: int = 0


def search_pallet_direct(
    conn: sqlite3.Connection,
    *,
    pallet_width_text: str = "",
    pallet_length_text: str = "",
    show_all: bool = False,
    last_hosozai: str = "",
) -> list[PalletSizeRow]:
    """VBA `btnAutoSelectPallet_Click`の「直接検索モード」の移植。

    製品サイズが未入力のときに使う、パレット自身の幅・丈を対象とした
    ±50mmの単純検索。EXオンリーモードは(元VBA仕様どおり)考慮しない。
    """
    pw = int(float(pallet_width_text)) if _is_numeric(pallet_width_text) else 0
    pl = int(float(pallet_length_text)) if _is_numeric(pallet_length_text) else 0

    rows = db.fetch_all(conn, "SELECT * FROM PalletMaster ORDER BY 管理番号", caller_name="search_pallet_direct") or []
    result: list[PalletSizeRow] = []
    for row in rows:
        w, l = row["幅"], row["丈"]
        if not w or not l:
            continue
        if pw and abs(w - pw) > config.SEARCH_RANGE_TOLERANCE:
            continue
        if pl and abs(l - pl) > config.SEARCH_RANGE_TOLERANCE:
            continue

        symbol = (row["記号"] or "").strip()
        is_ex = "EX" in symbol.upper()
        if not show_all:
            unit = (row["単位"] or "").strip()
            if last_hosozai and last_hosozai not in (material_service.HOSOZAI_ANGLE, "一致なし"):
                unit_ok = unit in ("台", "組")
            else:
                unit_ok = unit == "台"
            if not unit_ok or is_ex:
                continue

        result.append(_row_to_pallet_size_row(row))
    return result


def list_pallets_for_product(
    conn: sqlite3.Connection,
    *,
    product_width: int,
    product_length: int,
    show_all: bool = False,
    ex_only: bool = False,
    is_ex_order: bool = False,
) -> list[PalletSizeRow]:
    """製品サイズが適合範囲に収まるパレットだけを返す(検索結果リスト表示用)。

    `auto_select_pallet` は最終的に1件を決めるだけで、リスト表示用の
    候補集合は返さない(VBAの「決定値でリストを選択し直す」処理が
    不安定だったため、意図的に持たせていない)。ただし検索結果として
    「どれが候補なのか」を画面のリストに映すこと自体は別の話なので、
    ここで独立に候補集合を作る(通常向き・回転の両方をあたる)。

    `ex_only`/`is_ex_order` は `list_pallet_sizes` と同じ意味づけ
    (EXオンリーはEX受注のときだけ効く)。この2つを見ていなかったため、
    EX受注でEXオンリーを付けたままパレット検索すると、絞り込み結果に
    EX以外の行が混ざってしまっていた。
    """
    # auto_select_pallet の各パスは厳密/±5mmの許容差を使う。ここも同じ
    # 5mmにして、決定されたパレットが検索結果から漏れないようにする
    tol = 5
    rows = db.fetch_all(conn, "SELECT * FROM PalletMaster ORDER BY 管理番号",
                        caller_name="list_pallets_for_product") or []
    ex_only_mode = is_ex_order and ex_only
    result: list[PalletSizeRow] = []
    for row in rows:
        w, l = row["幅"], row["丈"]
        if not w or not l or not _fit_range_ok(row) or _fit_range_inverted(row):
            continue
        fits = (_size_ok(row, product_width, product_length, tol)
                or _size_ok(row, product_length, product_width, tol))
        if not fits:
            continue
        if not (_physically_fits(row, product_width, product_length)
                or _physically_fits(row, product_length, product_width)):
            continue

        symbol = (row["記号"] or "").strip()
        is_ex = "EX" in symbol.upper()
        if ex_only_mode:
            if not is_ex:
                continue
        elif not show_all and is_ex:
            continue

        result.append(_row_to_pallet_size_row(row))
    return result


def auto_select_pallet(
    conn: sqlite3.Connection,
    *,
    product_width_text: str,
    product_length_text: str,
    two_stack: bool = False,
    show_all: bool = False,
    ex_only: bool = False,
    is_ex_order: bool = False,
    last_hosozai: str = "",
    manufactured_thickness: Optional[float] = None,
    user_log: Optional[UserLog] = None,
) -> AutoSelectPalletResult:
    """VBA `btnAutoSelectPallet_Click`(製品サイズ入力済み時)の移植。

    業界カテゴリ(特定業界/一般/タイト/全面 [/2山積時は丈2山])×
    許容差(厳密/±5mm)×向き(通常/回転)の16パス(2山積モード時は+4パスの
    計20パス)を優先順位順に試し、最初に候補が1件以上見つかったパスの
    中で面積最小のものを採用する。どのパスも該当しなければ、フィルタを
    大幅に緩めた最終フォールバック探索を1回だけ試みる。

    【移植で変更した点】
        - 元VBAは「決定した組み合わせを再度リスト用に緩い許容差で
          再検索し、リストボックスの該当行を選択する」という追加処理を
          行っていたが、これはリストUIに行を事前選択させるためだけの
          処理であり、稀に決定値とリスト再検索結果が食い違う既知の
          不具合があった。Python版はリストボックスへの事前選択という
          概念自体が無い(呼び出し側が決定値をそのまま使う)ため、この
          処理ごと不要になり、不具合も自然に解消されている。
        - フォールバック探索時のパス名表示は、元VBAでは直前に失敗した
          パスの名前が誤って使い回されるバグがあったが、Python版では
          素直に「強制フォールバック」と表示する(選定結果そのものは
          元VBAのフォールバック探索ロジックを忠実に再現している)。
    """
    ulog = user_log if user_log is not None else UserLog()  # 未指定なら捨てバッファ

    if not (_is_numeric(product_width_text) and _is_numeric(product_length_text)):
        return AutoSelectPalletResult(False, "製品サイズが不正です")
    pw, pl = int(float(product_width_text)), int(float(product_length_text))
    if pw == 0 and pl == 0:
        return AutoSelectPalletResult(False, "製品サイズが不正です")

    ulog.clear()
    ulog.log("パレット検索を開始します..." + ("【2山モード】" if two_stack else ""), emphasis=True)
    ulog.log(f"製品サイズ: {pw} x {pl}")
    if two_stack:
        ulog.log(f"幅2山: 幅×2={pw * 2} (通常) / 丈×2={pl * 2} (回転) ※桁数奇数")
        ulog.log(f"丈2山: 丈×2={pl * 2} (通常) / 幅×2={pw * 2} (回転) ※スカシ限定")
    if manufactured_thickness is not None:
        ulog.log(f"製造板厚: {manufactured_thickness}")
    else:
        ulog.log("製造板厚: 未取得（Lot未検索）")

    rows = db.fetch_all(conn, "SELECT * FROM PalletMaster ORDER BY 管理番号", caller_name="auto_select_pallet") or []
    candidate_rows = [r for r in rows if r["幅"] and r["丈"] and _fit_range_ok(r)]

    # 探索に入る前に落ちている行を先に知らせる(「なんで検索に乗らないの?」対策)。
    # ここはVBAには無い追加ログ。
    skipped = len(rows) - len(candidate_rows)
    ulog.log(f"パレットマスタ: {len(rows)}件中 {len(candidate_rows)}件を探索対象にします"
             + (f" (幅/丈または適合範囲が未設定の{skipped}件を除外)" if skipped else ""))
    inverted = sum(1 for r in candidate_rows if _fit_range_inverted(r))
    if inverted:
        ulog.log(f"  ※適合範囲がマスタ側で逆転している行が{inverted}件あります"
                 f"(この行は何を検索してもヒットしません。マスタ修正が必要)")

    ex_only_mode = is_ex_order and ex_only
    max_pass = 20 if two_stack else 16
    # 適合範囲と現物サイズが食い違う行。1回の検索で1サイズにつき1度だけ知らせる
    reported_bad_master: set[tuple] = set()

    for pass_def in _build_pass_defs(max_pass):
        search_w, search_l = _search_dims(pass_def.orientation, pw, pl, two_stack)
        tag = _pass_tag(pass_def, two_stack)
        ulog.log(f"Pass {pass_def.number}: {pass_def.label}{tag} "
                 f"(W={search_w} L={search_l} tol=±{pass_def.tolerance})")

        candidates: list[tuple[sqlite3.Row, bool]] = []
        rejects: list[tuple[int, str]] = []       # (惜しさ, 表示文字列)
        keta_rejects: list[str] = []              # 桁数偶数だけで落ちたもの(全件出す)

        for row in candidate_rows:
            industry = row["業界"] or "一般"
            symbol = (row["記号"] or "").strip()
            is_ex = "EX" in symbol.upper()

            # EXフィルタで落ちた分はVBA同様ログに出さない(件数が多すぎるため)
            if ex_only_mode:
                if not is_ex:
                    continue
            elif not show_all and is_ex:
                continue

            size_ok = _size_ok(row, search_w, search_l, pass_def.tolerance)
            type_ok = _category_ok(pass_def, industry)
            keta_ok = _keta_ok(pass_def, two_stack, industry, row["桁数"], row["脚数"])
            # 適合範囲が何と書いてあっても、現物に載らないものは候補にしない
            fits = _physically_fits(row, search_w, search_l)

            if size_ok and not fits:
                # 適合範囲が現物サイズより広い行。黙って落とすと
                # 「なぜ出ないのか」が分からないので理由を出す。
                # 同じサイズは何パスで当たっても1回だけ出す(ログが埋まるため)
                key = (row["幅"], row["丈"])
                if key not in reported_bad_master:
                    reported_bad_master.add(key)
                    ulog.log(f"  ×除外: {row['幅']}x{row['丈']} "
                             f"(製品{search_w}x{search_l}が現物からはみ出す"
                             f"→適合範囲が現物より広い。"
                             f"設定画面の「適合範囲を再計算」で直ります)")

            if not (size_ok and type_ok and keta_ok and fits):
                diff = abs(row["幅"] - search_w) + abs(row["丈"] - search_l)
                reason = _reject_reason(
                    row, pass_def, two_stack, industry,
                    size_ok=size_ok, type_ok=type_ok, keta_ok=keta_ok,
                    fits=fits, search_w=search_w, search_l=search_l)
                rejects.append((diff, f"{row['幅']}x{row['丈']} → {reason}"))
                # サイズ・属性はOKで桁数偶数だけが原因のものは別枠で全件出す
                # (「惜しい」上位3件に埋もれて見えなくなるのを防ぐ)
                if two_stack and pass_def.number <= 16 and size_ok and type_ok:
                    keta_rejects.append(
                        f"{row['幅']}x{row['丈']} (桁数={row['桁数']}/偶数のため2山不可)")
                continue

            needs_warning = False
            if industry == "5×10":
                ok, needs_warning = _thickness_5x10_ok(symbol, manufactured_thickness)
                if not ok:
                    if manufactured_thickness is not None and manufactured_thickness <= 13.0:
                        ulog.log(f"  △スキップ: {row['幅']}x{row['丈']} "
                                 f"(5×10/板厚{manufactured_thickness}≦13.0 記号={symbol} "
                                 f"→ 強度UP以外は除外)")
                    else:
                        ulog.log(f"  △スキップ: {row['幅']}x{row['丈']} "
                                 f"(5×10/板厚{manufactured_thickness}>13.0 "
                                 f"→ 強度UPは薄板専用のため除外)")
                    continue

            candidates.append((row, needs_warning))
            ulog.log(f"  候補: {row['幅']}x{row['丈']} ({industry})")

        # 候補ゼロのときだけ「惜しかった」上位3件を出す
        if not candidates and rejects:
            for _, text in sorted(rejects, key=lambda r: r[0])[:MAX_NEAR_MISS_LOGS]:
                ulog.log(f"  ×惜しい: {text}")

        # 桁数偶数だけで落ちた分は候補の有無に関わらず全件出す
        if keta_rejects:
            ulog.log(f"  △桁数偶数のため2山除外({len(keta_rejects)}件):")
            for text in keta_rejects:
                ulog.log(f"    ×{text}")

        if not candidates:
            ulog.log("  → 該当なし")
            continue

        best_row, best_warning = min(candidates, key=lambda c: c[0]["幅"] * c[0]["丈"])
        industry = best_row["業界"] or ""
        detail = ""
        if two_stack and pass_def.number <= 16:
            detail = f" / 桁数={best_row['桁数']}"
        suffix = f" ← {len(candidates)}件中最小" if len(candidates) > 1 else ""
        ulog.log(f"  決定(最小面積): {best_row['幅']}x{best_row['丈']} "
                 f"({industry}{detail}){suffix}", emphasis=True)
        if best_warning and industry == "5×10":
            ulog.log("板厚13.0mmに注意（板厚情報なし）")
        rotated = pass_def.number % 2 == 0
        ulog.log(f"Pass {pass_def.number} で決定" + ("（製品回転）" if rotated else ""),
                 emphasis=True)

        return AutoSelectPalletResult(
            ok=True,
            message=f"パレットを自動選定しました({pass_def.label})",
            width=best_row["幅"], length=best_row["丈"],
            industry=industry, symbol=(best_row["記号"] or "").strip(),
            pass_label=pass_def.label, rotated=rotated,
            needs_thickness_warning=best_warning,
            search_width=search_w, search_length=search_l,
        )

    # 強制フォールバック(元VBA仕様: フィルタ大幅緩和・1回のみ・回転フラグは立てない)
    ulog.log("適合なし → 強制入替えサイズ検索中...", emphasis=True)
    fb_w, fb_l = (pw * 2, pl) if two_stack else (pl, pw)
    fb_candidates = []
    for row in candidate_rows:
        industry = row["業界"] or "一般"
        if not (row["巾適合min"] <= fb_w <= row["巾適合max"] and row["丈適合min"] <= fb_l <= row["丈適合max"]):
            continue
        # 最終フォールバックでも現物に載らないものは出さない
        if not _physically_fits(row, fb_w, fb_l):
            continue
        if two_stack and industry != "スカシ":
            keta = row["桁数"]
            if not (keta > 0 and keta % 2 == 1):
                continue
        fb_candidates.append(row)

    if fb_candidates:
        best_row = min(fb_candidates, key=lambda r: r["幅"] * r["丈"])
        ulog.log(f"【決定】強制入替えで適合: {best_row['幅']} x {best_row['丈']}", emphasis=True)
        return AutoSelectPalletResult(
            ok=True, message="パレットを自動選定しました(強制フォールバック)",
            width=best_row["幅"], length=best_row["丈"],
            industry=best_row["業界"] or "", symbol=(best_row["記号"] or "").strip(),
            pass_label="強制フォールバック", rotated=False,
            search_width=fb_w, search_length=fb_l,
        )

    ulog.log("適合パレットなし", emphasis=True)
    return AutoSelectPalletResult(False, "適合するパレットが見つかりませんでした")
