"""ロット検索の業務ロジック (VBA `CreatePage1` のページ = `mpMain`の1ページ目)

ロット番号(7桁)から、ロット情報 / 引当情報 / 受注情報 の3ブロックを引く。

対応関係:
    SearchAndDisplay   -> search_lot
    SearchLotInfo       -> _load_lot_info
    SearchHikiAndOdr     -> _load_hiki
    SearchOdrInfo         -> _load_odr
    ClearLot / ClearOdr    -> LotSearchResult.found が False のとき

【データの出どころ】
社内ネットワーク共有
    \\\\nlmsrvngy03\\Read\\【New】仕掛\\台帳\\
にある3ファイル(`SIKALOT` / `SIKAHIKI` / `SIKAODR`、いずれも
テーブル名は「仕掛」)。梱包資材マスタとは別のDBで、ホスト系から出力される
参照専用データ。共有フォルダを直接読まず、マスタ類と同じく事前に手元の
SQLiteへ取り込む(設定画面、または `scripts/import_source.py --lot-only`)。
手元でのテーブル名は `仕掛ロット` / `仕掛引当` / `仕掛受注`。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from . import db, import_specs
from .logging_utils import get_logger

log = get_logger("lot_service")

# ロット番号の桁数(VBA `txtLotNo.maxLength = 7` / `If Len(lotNo) = 7`)
LOT_NO_LENGTH = 7

# 試験指示票の表示。要否を表すものなので OK/NG ではない
TEST_SLIP_NEEDED = "要"
TEST_SLIP_NOT_NEEDED = "不要"
TEST_SLIP_UNKNOWN = "---"

# 品質グレード_表面処理がこの2つのどちらかなら「先行データ」は不要
# (VBA `AdvanceCheck` の flag(2): 表面 <> "S" And 表面 <> "T")
ADVANCE_CHECK_EXEMPT_SURFACES = ("S", "T")
# 用途コードがこの接頭辞のいずれかなら flag(3) = True
ADVANCE_CHECK_YOTO_PREFIXES = ("T", "D1", "D2")
# flag(1): 製造板厚がこの値以下
ADVANCE_CHECK_THICKNESS_LIMIT = 3.0

# BOX実績寸法に切り替える設計_設備コース(VBA `G_COURSES`)
# 増やすときはここに追記するだけ、というVBA側のコメントを踏襲する
BOX_COURSES = ("GFS", "GCT", "GSS")

# 全フラグ成立時に書き換える包装仕様NO(VBA `lblOdr(1).caption = "1P0122"`)
PACKAGING_SPEC_OVERRIDE = "1P0122"

# フラグ4の判定に使う製造板厚(VBA `Val(...) = 6.75`)
FLAG_THICKNESS = 6.75

# フラグ3の判定に使う用途コード
FLAG_YOTO_CODES = ("K434", "K435")

# kg梱包で「1梱包に入れてよい重量」の余裕率(VBA `konVal * 1.05`)
PACK_WEIGHT_TOLERANCE = 1.05

# 品名に載せないパレット記号(VBA `FilterPalSym`)
# マスタとしては残すが帳票・発注には出さない運用の記号
HIDDEN_PALLET_SYMBOLS = ("G1", "G2", "A2", "A3", "A4", "A5", "A6", "A7", "A8")

# 引当行の種別(VBA `typeFlag`)
HIKI_ADJUSTED = "adj"   # 引当調整NOあり → 梱包数の計算不可
HIKI_QUANTITY = "qty"   # 引当数量あり
HIKI_ALL = "zen"        # 全量


def filter_pallet_symbol(symbol: str) -> str:
    """VBA `FilterPalSym` の移植。表に出さない記号を空文字にする。"""
    return "" if symbol in HIDDEN_PALLET_SYMBOLS else symbol


def format_thickness(value: float) -> str:
    """板厚の表示書式(VBA `Format(..., "0.000")`)。"""
    return f"{value:.3f}"


def format_dimension(value: float) -> str:
    """板幅・板丈の表示書式(VBA `Format(..., "0.0")`)。"""
    return f"{value:.1f}"


# BOX最終実績_設備名 がこれのロットは、BOX最終実績の寸法が入っていても使わない
# (現場: HOT の BOX最終実績寸法は使うに値しない)。寸法が空のときと同じく、
# 2つ目の仕掛台帳の同じロットの行(BOX設計寸法)から選ぶ。比べるときは前後の空白を除いて大文字で
BOX_FINAL_UNUSABLE_EQUIPMENT = ("HOT",)
BOX_FINAL_BLANK = "空"       # `LotInfo.box_final_problem`: 寸法のどれかが空


@dataclass
class BoxChoice:
    """BOX寸法の候補1つ(仕掛台帳の2つ目の置き場所の行の BOX設計寸法。`仕掛ロット_2つ目`)。

    1つ目の SIKALOT で BOX最終実績の寸法が空か、BOX最終実績_設備名が HOT のロットだけに付く。
    ロット情報の画面で「BOX実績寸法」の横に BOX設計_設備名 で並べ、選んだものの寸法を使う
    (現場の依頼)。
    """

    key: str = ""            # 選んだものを覚える鍵(BOX番号|設備名|寸法)。取り込み直しても変わらない
    equipment: str = ""      # BOX設計_設備名
    box_no: str = ""         # BOX番号
    thickness: float = 0.0
    width: float = 0.0
    length: float = 0.0
    source: str = ""         # 寸法の出どころ(BOX設計)

    @property
    def label(self) -> str:
        name = self.equipment or "(設備名なし)"
        box = f"・BOX{self.box_no}" if self.box_no else ""
        return (f"{name}({format_thickness(self.thickness)} × {format_dimension(self.width)}"
                f" × {format_dimension(self.length)}{box})")


@dataclass
class LotInfo:
    """ロット情報ブロック(VBA `lblLot(0..11)`)。"""

    lot_no: str = ""
    yoto_code: str = ""          # lblLot(0) 用途コード
    yoto_name: str = ""          # lblLot(1) 用途名
    zaishitsu: str = ""          # lblLot(2) 製造材質
    choshitsu: str = ""          # lblLot(3) 製造調質
    thickness: float = 0.0       # lblLot(4) 製造板厚 または BOX実績_板厚
    width: float = 0.0           # lblLot(5) 製造板幅 または BOX実績_板幅
    length: float = 0.0          # lblLot(6) 製造板丈 または BOX実績_板丈
    order_thickness: float = 0.0  # lblLot(7) オーダー板厚
    order_width: float = 0.0     # lblLot(8) オーダー板幅
    order_length: float = 0.0    # lblLot(9) オーダー板丈
    design_course: str = ""      # lblLot(10) 設計_設備コース
    actual_course: str = ""      # lblLot(11) 実績_設備コース
    is_box: bool = False         # 設計_設備コースがBOX_COURSESを含む
    box_course: str = ""         # 一致したコース名(VBA `m_gCourse`)
    # 梱包数の見積りが食い潰す「いま何枚あるか」。
    #
    # **BOX最終実績_枚本数(そのロットの最終工程の実績)を使う。**
    # 以前は `BOX実績_枚本数` を読んでいたが、1件検索が読むのは
    # ロットの**先頭行**で、そこはたいてい1工程目(HOT)── 鋳塊1本の
    # 「1」だった。実データ1,319ロットのうち448件で食い違い、
    #
    #     L8061E0  BOX実績 = 1 (HOT) → 最終実績 = 2874 (KEN)
    #
    # のように桁が変わる。梱包数はここから積み上げるので、発注数が
    # そのぶん小さく出ていた(10倍以上ずれるロットが181件)。
    # 最終実績が0のとき(最終工程がまだ記録されていない2件)は
    # 今までどおり BOX実績 を使う ── 悪くしないため
    final_process_count: int = 0
    final_process_text: str = ""   # 見せる値(元の値のまま。2.9 なら "2.9")
    quality_surface: str = ""    # 品質グレード_表面処理(試験指示票の要否判定に使う)
    # 包装仕様NOの書き換え判定(flag4)、試験指示票の要否判定
    # (AdvanceCheck flag1)のどちらも BOX実績に差し替える前の「製造板厚」を
    # 見るため、表示用の thickness とは別に保持する
    manufactured_thickness: float = 0.0
    # BOX最終実績の寸法が空か HOT のとき、2つ目の置き場所から控えた候補(`BoxChoice`)。
    # BOX実績寸法のロットのときだけ付く。`box_pick` は選ばれた候補の鍵(空 = 1つ目のまま)
    box_choices: list = field(default_factory=list)
    box_pick: str = ""
    # BOX実績寸法のロットで、1つ目の BOX最終実績寸法が使えない理由(候補が無くても理由を書くため)。
    # 空 = 使える / "空" = どれかが空 / 設備名(HOT) = その設備の値は使わない(`_box_final_problem`)
    box_final_problem: str = ""
    # 設計_設備ｺｰｽ の無いファイル(2つ目の圧縮版 SIKALOT)から入ったロット。BOX かどうか
    # 決められないので製造寸法を出し、画面で断る(`sync_import.COURSE_UNKNOWN_TABLE`)
    course_unknown: bool = False

    @property
    def box_final_missing(self) -> bool:
        return bool(self.box_final_problem)

    @property
    def box_final_hot(self) -> bool:
        """空だからではなく、設備名(HOT)のせいで使えないのか。"""
        return self.box_final_problem not in ("", BOX_FINAL_BLANK)

    @property
    def picked_choice(self) -> Optional["BoxChoice"]:
        return next((c for c in self.box_choices if c.key == self.box_pick), None)

    @property
    def dimension_source(self) -> str:
        """4〜6行目がどちらの寸法か(見出しに出す)。"""
        return "BOX実績" if self.is_box else "製造"

    @property
    def needs_test_slip(self) -> bool:
        """試験指示票(先行データ)が要るか(VBA `AdvanceCheck` の移植)。

        別クエリ(品質グレード_表面処理・製造板厚・用途コードだけを見る)
        の判定で、`SearchLotInfo` が読む列とは独立している。
        以前は「試験NOが'0000'なら不要」という誤った前提で実装しており、
        そもそも試験NOはこの判定に使われていなかった(VBAの実クエリには
        試験NO列自体が含まれない)。

            flag1: 製造板厚が3mm以下
            flag2: 品質グレード_表面処理が"S"でも"T"でもない
            flag3: 用途コードの先頭が"T","D1","D2"のいずれか
            要否  = flag2 And (flag1 Or flag3)
        """
        surface = (self.quality_surface or "").strip()
        yoto = self.yoto_code or ""
        flag1 = self.manufactured_thickness <= ADVANCE_CHECK_THICKNESS_LIMIT
        flag2 = surface not in ADVANCE_CHECK_EXEMPT_SURFACES
        flag3 = any(yoto.startswith(prefix) for prefix in ADVANCE_CHECK_YOTO_PREFIXES)
        return flag2 and (flag1 or flag3)

    @property
    def test_slip_text(self) -> str:
        """画面に出す文字。OK/NGで表すものではない。"""
        return TEST_SLIP_NEEDED if self.needs_test_slip else TEST_SLIP_NOT_NEEDED

    def test_slip_checks(self) -> list[tuple[str, bool, str]]:
        """要否の**判定根拠**。(条件, 成立したか, 実際の値)。

        「要」とだけ出しても、現場は正しいかどうかを確かめられません。
        この判定は過去に**誤った前提で実装されていた**ことがあり
        (試験NOで判定していたが、VBAの実クエリに試験NO列は無かった)、
        根拠が見えていれば早く気づけたはずのものです。

        判定式は `needs_test_slip` と同じ並びにしてあります。
        片方だけ直すと食い違うので、値の出どころは1つにしてあります。
        """
        surface = (self.quality_surface or "").strip()
        yoto = self.yoto_code or ""
        return [
            (f"製造板厚が {ADVANCE_CHECK_THICKNESS_LIMIT}mm 以下",
             self.manufactured_thickness <= ADVANCE_CHECK_THICKNESS_LIMIT,
             f"{self.manufactured_thickness:g}mm"),
            (f"品質グレード_表面処理が {'・'.join(ADVANCE_CHECK_EXEMPT_SURFACES)} 以外",
             surface not in ADVANCE_CHECK_EXEMPT_SURFACES,
             surface or "(空)"),
            (f"用途コードが {'・'.join(ADVANCE_CHECK_YOTO_PREFIXES)} で始まる",
             any(yoto.startswith(p) for p in ADVANCE_CHECK_YOTO_PREFIXES),
             yoto or "(空)"),
        ]


@dataclass
class HikiRow:
    """引当情報の1行(VBA `lstHiki`)。"""

    order_no: str = ""       # 受注番号
    quantity: float = 0.0    # 引当数量
    adjust_no: str = ""      # 引当調整NO
    # 引当番号は8桁の番号。数値にすると 6.0717e+07 と指数表記になるため文字列。
    # 全件8桁固定なので、文字列のままでも昇順は数値順と一致する
    hiki_no: str = ""        # 引当番号(この順で昇順ソートする)
    type_flag: str = ""      # adj / qty / zen (VBA `typeFlag`)
    display_value: str = ""  # 画面表示値(VBA `dispVal`)。"全量" もありうる

    @property
    def is_all(self) -> bool:
        return self.type_flag == HIKI_ALL

    @property
    def quantity_text(self) -> str:
        """引当数量の表示。全量指定のときだけ「全量」と出す。

        全量の行は 引当数量 列に 0 が入っている。そのまま数字を出すと
        「引当が無い」と読めてしまう(VBA も `dispVal` に "全量" を
        入れて区別していた)。tkinter版とWeb版で同じものを出すため、
        判断はここに置く。
        """
        return self.display_value if self.is_all else f"{self.quantity:g}"


@dataclass
class OdrInfo:
    """受注情報ブロック(VBA `lblOdr(0..7)` + 比重・梱包単位)。"""

    order_no: str = ""          # いま表示している受注番号(引当行との対応付けに使う)
    delivery_name: str = ""     # lblOdr(0) 納入先名称
    packaging_spec: str = ""    # lblOdr(1) 包装仕様NO
    customer_name: str = ""     # lblOdr(2) 取引先名称
    ship_to_name: str = ""      # lblOdr(3) 送り先名称
    delivery_comment: str = ""  # lblOdr(4) 納送用コメント
    factory_comment: str = ""   # lblOdr(5) 工場用コメント
    vc_front: str = ""          # lblOdr(6) VC_表
    vc_back: str = ""           # lblOdr(7) VC_裏
    is_ex: bool = False         # EX_輸出区分が空でない
    specific_gravity: float = 0.0   # 材質_比重(VBA `m_hizyuu`)
    pack_unit_weight: dict[str, float] = field(default_factory=dict)  # 受注番号→梱包単位_重量
    pack_unit_count: dict[str, float] = field(default_factory=dict)   # 受注番号→梱包単位_枚数
    delivery_names: dict[str, str] = field(default_factory=dict)      # 受注番号→納入先名称(全引当分)


@dataclass
class LotSearchResult:
    found: bool = False
    message: str = ""
    lot: LotInfo = field(default_factory=LotInfo)
    hiki: list[HikiRow] = field(default_factory=list)
    odr: OdrInfo = field(default_factory=OdrInfo)

    @property
    def lot_header(self) -> str:
        """ロット情報フレームの見出し(VBA `fraLotInfo.caption`)。"""
        base = "ロット情報 (SIKALOT)"
        if self.lot.is_box:
            base += "---【BOX実績寸法】---"
        else:
            base += "-----------"
        return f"{base}最終実績数: {self.lot.final_process_text or self.lot.final_process_count}枚"

    @property
    def odr_header(self) -> str:
        """受注情報フレームの見出し(VBA `fraOdrInfo.caption`)。"""
        base = "受注情報 (SIKAODR)"
        suffix = f" {self.lot.box_course}" if self.lot.box_course else ""
        if self.odr.is_ex:
            return f"{base}-----------EX{suffix}"
        if suffix:
            return f"{base}-----------{suffix.strip()}"
        return base


def _detect_box_course(design_course: str) -> str:
    """設計_設備コースからBOXコースを判定する(VBA `m_gCourse` の決定)。

    部分一致で、`BOX_COURSES` の並び順に最初に見つかったものを採用する。
    """
    for course in BOX_COURSES:
        if course in design_course:
            return course
    return ""


def _needs_spec_override(lot: LotInfo) -> bool:
    """包装仕様NOを1P0122に書き換えるか(VBA の flag(1)〜flag(5) の全AND)。

        flag1: 製造材質が "*75S" で終わる
        flag2: 製造調質が "T6" で始まる
        flag3: 用途コードが K434 か K435
        flag4: 製造板厚(BOX実績ではなく製造側)が 6.75
        flag5: BOXコース
    """
    flag1 = lot.zaishitsu.endswith("75S")
    flag2 = lot.choshitsu.startswith("T6")
    flag3 = lot.yoto_code in FLAG_YOTO_CODES
    flag4 = lot.manufactured_thickness == FLAG_THICKNESS
    flag5 = lot.is_box
    return flag1 and flag2 and flag3 and flag4 and flag5


def _final_count(row: Any) -> Any:
    """BOX最終実績_枚本数。空(0)なら BOX実績_枚本数(古い形式の写し)。"""
    return row["BOX最終実績_枚本数"] or row["BOX実績_枚本数"] or 0


def _count_text(value: Any) -> str:
    """枚本数の見せ方。整数ならそのまま、小数は小数のまま(VBA は値を文字のまま出す)。"""
    number = import_specs.to_number(value)
    if number is None:
        return "0"
    return str(number)


def _box_final(row: Any, name: str) -> Any:
    """BOX最終実績_{name}。空(古い形式の写しで列が無い)なら BOX実績_{name}。"""
    return row[f"BOX最終実績_{name}"] or row[f"BOX実績_{name}"]


def _course_unknown(conn: sqlite3.Connection, lot_no: str) -> bool:
    """設計_設備ｺｰｽ の無いファイルから入ったロットか(`仕掛ロット_コース不明`)。"""
    try:
        return conn.execute("SELECT 1 FROM 仕掛ロット_コース不明 WHERE ロット番号 = ?",
                            (lot_no,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False                  # 表がまだ無い(古い手元DB)


def _load_box_choices(conn: sqlite3.Connection, lot_no: str) -> list[BoxChoice]:
    """そのロットの BOX最終実績寸法の候補(`仕掛ロット_2つ目`)。無ければ空。"""
    try:
        rows = conn.execute(
            "SELECT BOX番号, BOX設計_設備名, 板厚, 板幅, 板丈, 出どころ FROM 仕掛ロット_2つ目"
            # 工程の順(BOX番号の小さい順)に並べる
            " WHERE ロット番号 = ? ORDER BY CAST(BOX番号 AS INTEGER), 管理番号", (lot_no,)).fetchall()
    except sqlite3.OperationalError:
        return []                     # 表がまだ無い(古い手元DB)
    out = []
    for box_no, equipment, thickness, width, length, source in rows:
        key = f"{box_no}|{equipment}|{thickness:g}|{width:g}|{length:g}"
        out.append(BoxChoice(key=key, equipment=equipment or "", box_no=box_no or "",
                             thickness=float(thickness or 0), width=float(width or 0),
                             length=float(length or 0), source=source or ""))
    return out


def _box_final_problem(row: Any) -> str:
    """1つ目の BOX最終実績寸法が使えない理由。使えるなら空。

    - BOX最終実績_設備名 が `BOX_FINAL_UNUSABLE_EQUIPMENT`(HOT)… その設備名を返す。
      寸法が入っていても使わない
    - BOX最終実績_板厚・板幅・板丈 のどれかが空(0)… `BOX_FINAL_BLANK`
    """
    equipment = box_final_equipment_unusable(row["BOX最終実績_設備名"])
    if equipment:
        return equipment
    if any(not (row[f"BOX最終実績_{name}"] or 0) for name in ("板厚", "板幅", "板丈")):
        return BOX_FINAL_BLANK
    return ""


def box_final_equipment_unusable(equipment: Any) -> str:
    """BOX最終実績_設備名 が、寸法を使わない設備(HOT)なら、その名前(大文字)。違えば空。"""
    name = str(equipment or "").strip().upper()
    return name if name in BOX_FINAL_UNUSABLE_EQUIPMENT else ""


def _load_lot_info(conn: sqlite3.Connection, lot_no: str,
                   box_pick: str = "") -> Optional[LotInfo]:
    """VBA `SearchLotInfo` の移植。

    `box_pick` は BOX最終実績寸法の候補の鍵(`BoxChoice.key`)。BOX実績寸法のロットで、
    1つ目の BOX最終実績寸法が空か HOT のときだけ効く。候補に無い鍵なら無視する(1つ目のまま)。
    """
    # ロット番号はBOX工程ごとに複数行ありうる。VBAは `rs.EOF` 判定で
    # 先頭レコードだけを見るので、取り込み順(=Accessの物理順)の先頭を採る
    row = db.fetch_one(
        conn, "SELECT * FROM 仕掛ロット WHERE ロット番号 = ? ORDER BY 管理番号 LIMIT 1",
        (lot_no,),
        caller_name="lot_service._load_lot_info",
    )
    if row is None:
        return None

    course = row["設計_設備コース"] or ""
    box_course = _detect_box_course(course)
    is_box = bool(box_course)

    lot = LotInfo(
        lot_no=lot_no,
        yoto_code=row["用途コード"] or "",
        yoto_name=row["用途名"] or "",
        zaishitsu=row["製造材質"] or "",
        choshitsu=row["製造調質"] or "",
        # BOXコース(設計_設備コースに GFS / GCT / GSS を含む)のときは
        # 寸法を実績に差し替える(VBA踏襲)。
        #
        # **差し替え先は BOX最終実績_*。** 枚本数を最終実績に替えたのと
        # 同じ理由で、読むのはロットの先頭行だからそろえておく。
        # 実データでは寸法3つは BOX実績_* と全8,056行で一致していたので、
        # 見える値は変わらない ── 変わるのは「どの工程の寸法か」という
        # 意味のほうで、今後ずれたときに正しいほうを指す
        #
        # **BOX最終実績_* が空なら BOX実績_* を使う。** 仕掛台帳の写しが
        # 古い形式(BOX最終実績_* の列が無い)だと、BOXコースのロットが
        # 0×0×0 になって使えなくなっていた(枚本数と同じ落とし方)
        thickness=(_box_final(row, "板厚") if is_box else row["製造板厚"]),
        width=(_box_final(row, "板幅") if is_box else row["製造板幅"]),
        length=(_box_final(row, "板丈") if is_box else row["製造板丈"]),
        order_thickness=row["オーダー板厚"],
        order_width=row["オーダー板幅"],
        order_length=row["オーダー板丈"],
        design_course=course,
        actual_course=row["実績_設備コース"] or "",
        is_box=is_box,
        box_course=box_course,
        # 枚本数は小数のことがある(2.9 など)。**数えるときは VBA と同じ `CLng`**
        # (いちばん近い整数)、**見せるときは元の値のまま**(VBA は文字のまま出す)。
        # 以前は取り込みで切り捨てていた(2.9 → 2)
        final_process_count=import_specs.clng(_final_count(row)),
        final_process_text=_count_text(_final_count(row)),
        quality_surface=str(row["品質グレード_表面処理"] or ""),
        manufactured_thickness=row["製造板厚"],
    )
    if not course:
        lot.course_unknown = _course_unknown(conn, lot_no)
    # 1つ目で BOX最終実績寸法が空、または HOT の値 → 2つ目の同じロットの行から選べるようにする
    # (現場の依頼)。**BOX実績寸法のときだけ**(製造寸法を出しているロットでは使わない)
    problem = _box_final_problem(row) if is_box else ""
    if problem:
        lot.box_final_problem = problem
        if problem != BOX_FINAL_BLANK:
            # **HOT の寸法は使わない**(使うに値しない、と現場)。選ぶまでは空のまま ──
            # 値を残すと、資材展開でそのまま使われてしまう(画面の説明は資材選択まで付いていかない)
            lot.thickness = lot.width = lot.length = 0.0
        lot.box_choices = _load_box_choices(conn, lot_no)
        picked = next((c for c in lot.box_choices if c.key == box_pick), None)
        if picked is not None:
            lot.box_pick = picked.key
            lot.thickness, lot.width, lot.length = picked.thickness, picked.width, picked.length
    return lot


def _load_hiki(conn: sqlite3.Connection, lot_no: str) -> list[HikiRow]:
    """VBA `SearchHikiAndOdr` 前半の移植。引当番号の昇順に並べる。"""
    rows = db.fetch_all(
        conn,
        "SELECT 受注番号, 引当数量, 引当調整NO, 引当番号 FROM 仕掛引当 "
        "WHERE ロット番号 = ? ORDER BY 引当番号",
        (lot_no,), caller_name="lot_service._load_hiki",
    ) or []
    result: list[HikiRow] = []
    for r in rows:
        adjust_no = (r["引当調整NO"] or "").strip()
        quantity = r["引当数量"] or 0.0
        # VBA は 引当調整NO → 引当数量 → 全量 の優先順で表示値と種別を決める
        if adjust_no not in ("", "0"):
            type_flag, display_value = HIKI_ADJUSTED, adjust_no
        elif quantity:
            type_flag, display_value = HIKI_QUANTITY, f"{quantity:g}"
        else:
            type_flag, display_value = HIKI_ALL, "全量"
        result.append(HikiRow(
            order_no=r["受注番号"] or "",
            quantity=quantity,
            adjust_no=adjust_no,
            hiki_no=str(r["引当番号"] or ""),
            type_flag=type_flag,
            display_value=display_value,
        ))
    return result


def _load_odr(conn: sqlite3.Connection, order_nos: list[str]) -> OdrInfo:
    """VBA `SearchOdrInfo` の移植。

    受注情報そのものは先頭1件だけを表示に使い(VBA `SELECT TOP 1`)、
    梱包単位は該当する全受注番号ぶんを辞書で持つ。
    """
    if not order_nos:
        return OdrInfo()

    marks = ", ".join("?" for _ in order_nos)
    row = db.fetch_one(
        conn, f"SELECT * FROM 仕掛受注 WHERE 受注番号 IN ({marks})", tuple(order_nos),
        caller_name="lot_service._load_odr",
    )
    if row is None:
        return OdrInfo()

    odr = OdrInfo(
        order_no=row["受注番号"] or "",
        delivery_name=row["納入先名称"] or "",
        packaging_spec=row["包装仕様NO"] or "",
        customer_name=row["取引先名称"] or "",
        ship_to_name=row["送り先名称"] or "",
        delivery_comment=row["納送用コメント"] or "",
        factory_comment=row["工場用コメント"] or "",
        vc_front=row["VC_表"] or "",
        vc_back=row["VC_裏"] or "",
        is_ex=bool((row["EX_輸出区分"] or "").strip()),
        specific_gravity=row["材質_比重"] or 0.0,
    )

    unit_rows = db.fetch_all(
        conn,
        f"SELECT 受注番号, 梱包単位_重量, 梱包単位_枚数, 納入先名称 FROM 仕掛受注"
        f" WHERE 受注番号 IN ({marks})",
        tuple(order_nos), caller_name="lot_service._load_odr.units",
    ) or []
    for r in unit_rows:
        odr.pack_unit_weight[r["受注番号"]] = r["梱包単位_重量"] or 0.0
        odr.pack_unit_count[r["受注番号"]] = r["梱包単位_枚数"] or 0.0
        # 受注番号ごとの納入先。**納入先の違う引当が混ざっているか**を見るのに使う
        # (VBA `m_nouDict`。受注情報の欄は先頭の1件しか出さないので、ここで全件持つ)
        odr.delivery_names.setdefault(r["受注番号"], (r["納入先名称"] or "").strip())
    return odr


def hiki_delivery_names(result: "LotSearchResult") -> list[str]:
    """引当一覧に含まれる納入先(重複なし・引当の並び順・空は数えない)。

    VBA `GetHikiNounyusakiCount` / `GetHikiNounyusakiList`。2つ以上なら
    「納入先の違う引当が混ざっている」。倉庫へ送る納入先は1つだけ、発注数は
    ロット全体なので、送る前にそのことを知らせる(`outputs.draft_notice`)。
    """
    names: list[str] = []
    for row in result.hiki:
        name = result.odr.delivery_names.get(row.order_no, "").strip()
        if name and name not in names:
            names.append(name)
    return names


def pack_unit_of(odr: OdrInfo, order_no: str) -> tuple[str, float]:
    """受注番号の梱包単位を (種別, 値) で返す(VBA `m_konKDict`/`m_konDict`)。

    重量が入っていれば kg、無ければ枚数で mai、どちらも無ければ種別なし。
    """
    weight = odr.pack_unit_weight.get(order_no, 0.0)
    if weight:
        return "kg", weight
    count = odr.pack_unit_count.get(order_no, 0.0)
    if count:
        return "mai", count
    return "", 0.0


def search_lot(conn: sqlite3.Connection, lot_no: str, box_pick: str = "") -> LotSearchResult:
    """VBA `SearchAndDisplay` の移植。ロット番号1本で3ブロックまとめて引く。

    VBA版は7桁入力された時点で自動検索していた(`Page1_OnTxtLotNoChanged`)。
    桁数チェックは呼び出し側(UI)の責務とし、ここでは空文字だけ弾く。
    """
    lot_no = (lot_no or "").strip()
    if not lot_no:
        return LotSearchResult(found=False, message="ロット番号を入力してください。")

    lot = _load_lot_info(conn, lot_no, box_pick)
    if lot is None:
        log.debug("search_lot: ロット未発見 [%s]", lot_no)
        return LotSearchResult(found=False, message=f"未発見: ロット {lot_no}")

    hiki = _load_hiki(conn, lot_no)
    # 受注番号は重複しうるので初出順で一意化する(VBA の odrDict 相当)
    order_nos: list[str] = []
    for row in hiki:
        if row.order_no and row.order_no not in order_nos:
            order_nos.append(row.order_no)
    odr = _load_odr(conn, order_nos)
    _apply_spec_override(odr, lot)

    return LotSearchResult(found=True, message=f"ロット {lot_no}", lot=lot, hiki=hiki, odr=odr)


def load_odr_for_hiki_row(
    conn: sqlite3.Connection, lot_no: str, hiki_no: str,
) -> Optional[tuple[OdrInfo, list[HikiRow], LotInfo]]:
    """引当行を1件クリックしたときの、その行の受注情報(VBA `Page1_OnLstHikiClick`)。

    データの流れ: SIKALOT から LOTNO で1行 → SIKAHIKI で同じ
    LOTNO の行の中から**引当NO**で1行(=引当NOとオーダーNOが決まる)
    → SIKAODR で同じオーダーNOの行を展開。

    **主語は「引当行」であって「受注番号」ではない。** 受注番号
    だけで照合すると、1ロット内で受注番号が重複する状況が万一あった
    場合に、押した行とは別の行(たまたま同じ受注番号を持つ行)の
    データを表示してしまいうる。引当NOで行そのものを特定してから
    受注番号を取り出すことで、押した行と表示される内容が必ず一致する。

    VBA版は受注情報を**先頭1件だけ**表示に使い(`_load_odr` のdocstring
    参照)、複数の受注番号にまたがるロットでは `lstHiki`(引当一覧)の
    行をクリックすると、その行が指す受注番号の情報に表示を差し替えて
    いた。Python版はこのクリック時の差し替え経路が無く、常に先頭1件
    しか出せなかった(現場の声:「引当情報をクリックしても受注内容が
    切り替わっているように見えない」)。

    ロットが見つからない、または指定の引当NOがそのロットに無ければ
    断る。別ロットの受注情報が紛れ込むと、資材選定や1P0113判定の
    元になる情報が誤って差し替わる。

    戻り値は `(受注情報, 引当一覧, ロット情報)`。呼び出し側(画面のAPI)が
    引当一覧の選択状態を塗り直し、見出しの断り書きにロット情報を使うため、
    ここでまとめて返す(同じ行を何度も読み直させない)。
    """
    hiki_no = (hiki_no or "").strip()
    if not hiki_no:
        return None
    lot = _load_lot_info(conn, lot_no)
    if lot is None:
        return None
    hiki = _load_hiki(conn, lot_no)
    # **引当行そのもの**(引当NO)で特定する。受注番号ではなく、
    # 押された行がどれかをまず決めてから、その行の受注番号を引く
    picked_row = next((row for row in hiki if row.hiki_no == hiki_no), None)
    if picked_row is None or not picked_row.order_no:
        log.warning("load_odr_for_hiki_row: 引当NO %s はロット %s にありません",
                   hiki_no, lot_no)
        return None

    odr = _load_odr(conn, [picked_row.order_no])
    _apply_spec_override(odr, lot)
    return odr, hiki, lot


def _apply_spec_override(odr: OdrInfo, lot: LotInfo) -> None:
    """1P0113判定用に包装仕様NOを上書きする(該当ロットのみ)。

    `search_lot` と `load_odr_for_order` の両方で同じ判断をするので、
    ここに1つだけ置く(**同じ事実を2か所で判断すると、片方だけ
    直し忘れる**)。
    """
    if _needs_spec_override(lot):
        log.debug("_apply_spec_override: 包装仕様NO → %s に書き換え",
                 PACKAGING_SPEC_OVERRIDE)
        odr.packaging_spec = PACKAGING_SPEC_OVERRIDE


def has_lot_data(conn: sqlite3.Connection) -> bool:
    """仕掛台帳が取り込まれているか(未取り込みならUIで案内を出す)。"""
    row = db.fetch_one(conn, "SELECT COUNT(*) AS c FROM 仕掛ロット",
                       caller_name="lot_service.has_lot_data")
    return bool(row and row["c"])


# ------------------------------------------------------------------
# 梱包数の計算 (VBA `CalcTotalPackages`)
# ------------------------------------------------------------------
# 計算不可を表す戻り値(VBA も -1 を返す)
PACKAGES_UNKNOWN = -1

# 計算不可になった理由。**現場が次にどこを確認すればよいかが変わる**ので、
# 「調整NO混在」の一種類にまとめない(以前は理由を持ち帰らず、原因が
# 「比重・寸法未設定」でも案内は常に「調整NO混在」と出ていて、
# 現場が引当調整NOを探しに行っても見つからない、という誤診断を招いていた)
REASON_ADJUSTED = "引当調整NO混在"
REASON_WEIGHT_UNKNOWN = "比重または寸法が未設定"


def weight_per_sheet(lot: LotInfo, specific_gravity: float) -> float:
    """1枚あたりの重量(kg) = 厚 × 幅 × 丈 × 比重 / 1,000,000。

    寸法は画面に出ている値(BOXコースならBOX実績)を使う。
    """
    if specific_gravity <= 0:
        return 0.0
    return lot.thickness * lot.width * lot.length * specific_gravity / 1_000_000


def _sheets_per_package(unit_kind: str, unit_value: float, per_sheet: float) -> int:
    """1梱包に入る枚数。kgなら重量から逆算し、maiならそのまま枚数。"""
    if unit_kind == "kg":
        max_weight = unit_value * PACK_WEIGHT_TOLERANCE
        return max(int(max_weight / per_sheet), 1)
    return max(int(unit_value), 1)


def calc_total_packages(result: LotSearchResult) -> int:
    """VBA `CalcTotalPackages` の移植。発注数(総梱包数)を見積もる。

    計算不可になった理由まで要るときは `calc_total_packages_reason` を使う。
    """
    return _calc_total_packages(result)[0]


def calc_total_packages_reason(result: LotSearchResult) -> str:
    """計算不可(`PACKAGES_UNKNOWN`)になった理由。計算できたときは空文字。

    原因は1種類ではない ── 調整NOが混ざっているときと、比重や寸法が
    無くて1枚重量が出せないときの両方が同じ -1 を返す。呼び出し側が
    案内の文言をどちらか一方に決め打ちすると、実際の原因と違う案内を
    出してしまう(現場が引当調整NOを探しに行っても見つからない、
    という誤診断が起きていた)。
    """
    return _calc_total_packages(result)[1]


def _calc_total_packages(result: LotSearchResult) -> tuple[int, str]:
    """`calc_total_packages` / `calc_total_packages_reason` の共通実装。

    引当行を引当番号順に処理し、最終実績数(BOX最終実績_枚本数)を
    上から食い潰しながら梱包数を積み上げる。
    次の場合は計算不可として `(PACKAGES_UNKNOWN, 理由)` を返す:
        - 引当調整NOを持つ行が1件でも混ざる
        - 1枚重量が出せない(比重や寸法が無い)のに kg 梱包の行がある
          → 過少カウントを避けるため
    """
    if any(row.type_flag == HIKI_ADJUSTED for row in result.hiki):
        log.debug("calc_total_packages: 引当調整NO混在のため計算不可")
        return PACKAGES_UNKNOWN, REASON_ADJUSTED

    per_sheet = weight_per_sheet(result.lot, result.odr.specific_gravity)
    if per_sheet <= 0:
        for row in result.hiki:
            if pack_unit_of(result.odr, row.order_no)[0] == "kg":
                log.debug("calc_total_packages: 1枚重量0かつkg種別混在のため計算不可")
                return PACKAGES_UNKNOWN, REASON_WEIGHT_UNKNOWN

    remaining = result.lot.final_process_count
    total = 0
    for row in result.hiki:
        if remaining <= 0:
            break

        unit_kind, unit_value = pack_unit_of(result.odr, row.order_no)

        # 表示値から単位表記を落として数量を取り出す(VBA の Replace 群)
        text = row.display_value.replace("Kg", "").replace("枚", "")
        text = text.replace("全量", "0").replace("全", "0")
        try:
            qty = int(float(text))
        except (ValueError, OverflowError):
            qty = 0
        if qty <= 0:
            qty = remaining          # 全量指定は残り全部
        actual = min(qty, remaining)

        if unit_kind in ("kg", "mai") and unit_value > 0 and (
                unit_kind == "mai" or per_sheet > 0):
            per_package = _sheets_per_package(unit_kind, unit_value, per_sheet)
            if row.is_all:
                # 全量は「残りが尽きるまで1梱包ずつ」数える
                packages = -(-remaining // per_package)
                actual = remaining
            else:
                packages = -(-actual // per_package)
        else:
            packages = 1             # 梱包単位が分からない行は1梱包とみなす

        total += packages
        remaining -= actual
        log.debug("calc_total_packages: 受注=%s 数量=%s 梱包=%s 累計=%s 残=%s",
                  row.order_no, actual, packages, total, remaining)

    log.debug("calc_total_packages 完了: 総梱包数=%s", total)
    return total, ""


def build_hinmei(industry: str, symbol: str, width: int, length: int) -> str:
    """倉庫送信の品名を組み立てる(VBA `btnSendToWarehouse_Click` 通常系)。

        業界　記号　幅x丈
    記号は `filter_pallet_symbol` で表に出さないものを除いてある。
    """
    return f"{industry}　{filter_pallet_symbol(symbol)}　{width}x{length}"
