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
にある3ファイル(`SIKALOTNOW` / `SIKAHIKINOW` / `SIKAODRNOW`、いずれも
テーブル名は「仕掛」)。梱包資材マスタとは別のDBで、ホスト系から出力される
参照専用データ。共有フォルダを直接読まず、マスタ類と同じく事前に手元の
SQLiteへ取り込む(設定画面、または `scripts/import_source.py --lot-only`)。
手元でのテーブル名は `仕掛ロット` / `仕掛引当` / `仕掛受注`。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from . import db
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
    prev_process_count: int = 0  # BOX実績_枚本数(前工程実績数)
    quality_surface: str = ""    # 品質グレード_表面処理(試験指示票の要否判定に使う)
    # 包装仕様NOの書き換え判定(flag4)、試験指示票の要否判定
    # (AdvanceCheck flag1)のどちらも BOX実績に差し替える前の「製造板厚」を
    # 見るため、表示用の thickness とは別に保持する
    manufactured_thickness: float = 0.0

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
        base = "ロット情報 (SIKALOTNOW)"
        if self.lot.is_box:
            base += "---【BOX実績寸法】---"
        else:
            base += "-----------"
        return f"{base}前工程実績数: {self.lot.prev_process_count}枚"

    @property
    def odr_header(self) -> str:
        """受注情報フレームの見出し(VBA `fraOdrInfo.caption`)。"""
        base = "受注情報 (SIKAODRNOW)"
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


def _load_lot_info(conn: sqlite3.Connection, lot_no: str) -> Optional[LotInfo]:
    """VBA `SearchLotInfo` の移植。"""
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
        # BOXコースのときは寸法をBOX実績に差し替える(VBA踏襲)
        thickness=row["BOX実績_板厚"] if is_box else row["製造板厚"],
        width=row["BOX実績_板幅"] if is_box else row["製造板幅"],
        length=row["BOX実績_板丈"] if is_box else row["製造板丈"],
        order_thickness=row["オーダー板厚"],
        order_width=row["オーダー板幅"],
        order_length=row["オーダー板丈"],
        design_course=course,
        actual_course=row["実績_設備コース"] or "",
        is_box=is_box,
        box_course=box_course,
        prev_process_count=int(row["BOX実績_枚本数"] or 0),
        quality_surface=str(row["品質グレード_表面処理"] or ""),
        manufactured_thickness=row["製造板厚"],
    )
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
        f"SELECT 受注番号, 梱包単位_重量, 梱包単位_枚数 FROM 仕掛受注 WHERE 受注番号 IN ({marks})",
        tuple(order_nos), caller_name="lot_service._load_odr.units",
    ) or []
    for r in unit_rows:
        odr.pack_unit_weight[r["受注番号"]] = r["梱包単位_重量"] or 0.0
        odr.pack_unit_count[r["受注番号"]] = r["梱包単位_枚数"] or 0.0
    return odr


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


def search_lot(conn: sqlite3.Connection, lot_no: str) -> LotSearchResult:
    """VBA `SearchAndDisplay` の移植。ロット番号1本で3ブロックまとめて引く。

    VBA版は7桁入力された時点で自動検索していた(`Page1_OnTxtLotNoChanged`)。
    桁数チェックは呼び出し側(UI)の責務とし、ここでは空文字だけ弾く。
    """
    lot_no = (lot_no or "").strip()
    if not lot_no:
        return LotSearchResult(found=False, message="ロット番号を入力してください。")

    lot = _load_lot_info(conn, lot_no)
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

    if _needs_spec_override(lot):
        log.debug("search_lot: 包装仕様NO → %s に書き換え", PACKAGING_SPEC_OVERRIDE)
        odr.packaging_spec = PACKAGING_SPEC_OVERRIDE

    return LotSearchResult(found=True, message=f"ロット {lot_no}", lot=lot, hiki=hiki, odr=odr)


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

    引当行を引当番号順に処理し、前工程実績数(BOX実績_枚本数)を
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

    remaining = result.lot.prev_process_count
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
        except ValueError:
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
