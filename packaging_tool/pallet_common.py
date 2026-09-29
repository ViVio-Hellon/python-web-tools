"""パレット選定の共通の言葉 ── 型と、「その行を出してよいか」の判断

**3つの一覧(全件・直接検索・製品サイズ適合)と自動選定が、同じ判断を
使う。** 書き写していたせいで、製品サイズを入れた後の一覧だけ単位の
規則が抜け落ちていたことがある ── 判断は1か所に置いて、どの経路からも
そこを見る。

置いてあるもの:

  型       … Palette / ProductSize / SelectedBoard / PalletSizeRow ほか
  出す判断 … 単位、マトリックス行の重複抑制、適合範囲、現物に載るか、
             5×10の板厚、2山積の可否
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from . import config, db, material_service
from . import special_packaging as spk
from .logging_utils import get_logger
from .user_log import RejectLog, UserLog, tag_area

log = get_logger("board_selection.pallet_common")

# 業界カテゴリの判定(元VBA `isSpecialInd` 等)
_SPECIAL_INDUSTRIES = ("1×2", "4×8", "4×10", "5×10", "3×6", "5×8")

# 候補ゼロのパスで「惜しかった」レコードを何件までログに出すか(VBA `showMax`)
MAX_NEAR_MISS_LOGS = 3

log = get_logger("board_selection_service")

# VBA版はbtnApplyPalette_Click/LoadSinglePatternに `1.1` を直接ハードコードして
# いた(名前付き定数は無かった)。Python版では明示的な定数として抽出する。
PALETTE_OVERHANG_RATIO = 1.10


def _is_numeric(text: str) -> bool:
    """**寸法として使える数値か。**

    呼ぶ側はこれが True なら `int(float(text))` してよい、という約束で
    書かれています。ところが `float()` は `inf` / `nan` / `1e400` も
    受けるので、以前はそこを通り抜けて

        OverflowError: cannot convert float infinity to integer

    で 500 になっていました。断りの文言は用意してあるのに、そこへ
    辿り着く前に落ちるので、画面には「通信に失敗しました」としか
    出ません ── **直しようのない案内**になります。

    無限大も非数も寸法ではないので、ここで数値でないものとして扱います。
    """
    try:
        value = float(text)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value)


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

# 手動追加1回あたりの枚数(VBA `spnBoardCount` の `.Min` / `.Max`)
COUNT_MIN = 1
COUNT_MAX = 100


@dataclass
class ApplyResult:
    ok: bool
    message: str = ""
    reason: str = ""             # 断ったときだけ入る(上の2種)

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
    # --- どう当たった行なのか(製品サイズで絞ったときだけ入る) ---
    #
    # **当たり方が違えば、同じ「候補」でも意味が違う。** 製品を回して
    # 載せる前提の行と、そのままの向きで載る行は現場での扱いが別物だし、
    # 適合範囲にぴったり入った行と ±5mm の許容でようやく入った行も同じ
    # ではない。判定そのものは前からしていたのに画面へ出していなかった
    # ため、押した人には見分けようが無かった(現場の声)。
    rotated: bool = False        # 製品を回して(幅と丈を入れ替えて)当てた
    exact: bool = False          # 許容差なしで適合範囲に収まった
    tolerance: int = 0           # 許容差で当てたときの、その許容量(mm)
    # 2山積で当てたときだけ、その積み方(`幅2山` / `丈2山`)。通常は空
    two_stack: str = ""


def _row_to_pallet_size_row(row: sqlite3.Row) -> PalletSizeRow:
    return PalletSizeRow(
        id=row["管理番号"], width=row["幅"], length=row["丈"],
        w_min=row["巾適合min"], w_max=row["巾適合max"],
        l_min=row["丈適合min"], l_max=row["丈適合max"],
        industry=row["業界"] or "", symbol=(row["記号"] or "").strip(),
        leg_count=row["脚数"], keta=row["桁数"],
        code=row["コード"] or "", unit=row["単位"] or "",
    )


# 単価表のマトリックス行は、同じ幅・丈で実コードを持つ行が別にあるなら
# 重複表示になるので載せない(VBA `BuildMatrixSuppressMap`/`IsSuppressedMatrix`)
CODE_MATRIX = "単価表のマトリックス"


def build_matrix_suppress_map(rows) -> set[tuple[str, str]]:
    """VBA `BuildMatrixSuppressMap` の移植。

    幅|丈が同じで実コード(マトリックス以外)を持つ行が存在する
    組み合わせ(幅, 丈)の集合を作る。
    """
    suppress: set[tuple[str, str]] = set()
    for row in rows:
        code = (row["コード"] or "").strip()
        if code and code != CODE_MATRIX:
            suppress.add((str(row["幅"] or ""), str(row["丈"] or "")))
    return suppress


def is_suppressed_matrix(
    suppress_map: set[tuple[str, str]], width, length, code: str,
) -> bool:
    """VBA `IsSuppressedMatrix` の移植。True ならこの行は載せない。"""
    if (code or "").strip() != CODE_MATRIX:
        return False
    return (str(width or ""), str(length or "")) in suppress_map


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


# パレット一覧に出す単位。**判断はここ1か所**(VBA `FilterPalletList`)。
#
# 既定は「台」だけ ── パレットとして数えるものがそれだから。
# **保護材が確定しているとき**(上下共用。上蓋など、アングル以外に決まっている)は
# 「枚」も出す。上蓋は台で数えないものがあり、既定のままだと現物が一覧に
# 出てこない(現場の指示)。
#
# 以前は「組」も出していた。VBA は経路ごとに「台・組」「台・枚」とずれており、
# 更新後の VBA(`IsPalletUnitAllowed`)で「台・枚」の1つにそろえたので、それに
# 合わせる。自動検索の候補選び(`pallet_auto_select`)も同じ規則を使う。
#
# 3つの一覧(全件・直接検索・製品サイズ適合)が同じ規則を使う。
# **書き写していたせいで、製品サイズを入れた後の一覧だけこの規則が
# 抜け落ちていた** ── サイズで当たる行が単位に関わらず全部出ていた
# (現場の声)。関数に切り出して、次に増えても抜けないようにする
UNITS_DEFAULT = ("台",)
UNITS_WITH_HOSOZAI = ("台", "枚")


def unit_allowed(unit: str, last_hosozai: str) -> bool:
    """この単位の行を一覧に出してよいか。"""
    allowed = (UNITS_WITH_HOSOZAI
               if last_hosozai and last_hosozai not in (
                   material_service.HOSOZAI_ANGLE, "一致なし")
               else UNITS_DEFAULT)
    return (unit or "").strip() in allowed

@dataclass
class BoardRow:
    id: int
    width: int
    length: int
    board_type: str
    stock_low: bool = False

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


# 2山積の種別。一覧の絞り込み(`list_pallets_for_product`)が
# `_keta_ok` / `_category_ok` と同じ条件を掛けるために使う
KIND_WIDTH2 = "幅2山"
KIND_LEN2 = "丈2山"


def _two_stack_ok(kind: str, industry: str, keta: int, leg: int) -> bool:
    """その業界・桁数・脚数で、その2山の積み方ができるか。

    条件そのものは `_category_ok` / `_keta_ok`(自動選定のパス定義)が
    持っているものと同じ。**同じ条件を2つの言い方で書かない**ため、
    ここは種別から引き直すだけにしてある。
    """
    if kind == KIND_WIDTH2:
        return industry == "スカシ" or (keta > 0 and keta % 2 == 1)
    if kind == KIND_LEN2:
        return industry in ("スカシ", "タイト") and leg >= 3
    return True                                   # 2山積ではない(通常/回転)


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
    # 2山積で決まったときの2山の向き(VBA `m_2YamaDir`)。
    # "幅" = 幅方向に2山(Pass1〜16と強制入替え) / "丈" = 丈方向に2山(Pass17〜20)
    # / "" = 2山積でない。「セット」で製品サイズのこの向きを2倍する
    stack_dir: str = ""
