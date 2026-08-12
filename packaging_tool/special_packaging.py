"""包装仕様NOで切り替わる特殊モード (VBA `MaterialMasterForm` の1P0113/プロテック)

受注の包装仕様NO(`lblOdr(1)`)によって、資材選択画面は3通りに分岐する。

1. **1P0113 = 裸梱包モード**
   パレットもボードも使わず、溝切角材と松板だけで組む。VBAは
   `Apply1P0113Mode` でパレットサイズのセクションごと隠し、代わりに
   溝切角材/松板の必要数を出す小さなパネルを動的生成していた。
   資材は `松板角材` テーブルから寸法帯で引く。

2. **1P1216 / 1P1125 / 1P1211 = プロテックモード**
   `CheckAndSetProtecMode` がボード種別を「プロテックボード」に切り替え、
   上用ボードは下用と同サイズを強制する(選定側は
   `board_selection_algorithm.select_upper_boards` に移植済み)。

3. それ以外は通常モード。

このモジュールはDB参照と計算だけを持ち、画面には依存しない。
表示の切り替えは `presenters/selection.py` がこの結果を見て決める。

【VBAとの対応】
    Apply1P0113Mode の判定部分      -> is_1p0113
    Load1P0113Materials の計算/検索 -> load_materials
    Update1P0113CountLabels         -> kakuzai_count_caption / matsuita_count_caption
    ExtractCount                    -> extract_count
    Parse1P0113Info                 -> parse_info
    CheckAndSetProtecMode           -> protec_state
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db
from .logging_utils import get_logger

log = get_logger("special_packaging")

# ==================================================================
# 1P0113 (裸梱包) 定数 — VBA の C_1P_* をそのまま
# ==================================================================
HOSOSIYO_1P0113 = "1P0113"

# 松板の丈(A値)を製品丈から決める閾値
A_THR1 = 949     # 1〜949      : A = 製品丈
A_THR2 = 1800    # 950〜1800   : A = 950
A_THR3 = 2400    # 1801〜2400  : A = 1600
A_VAL1 = 950
A_VAL2 = 1600
A_VAL3 = 2330    # 2401〜      : A = 2330

# 検索する資材の固定寸法
KAKUZAI_NAME = "溝切角材"
KAKUZAI_ATSU = 75
KAKUZAI_HABA = 75
MATSUITA_NAME = "松板"
MATSUITA_ATSU = 24
MATSUITA_HABA = 120

# 溝切角材の実際の丈 = 製品幅 + マージン
KAKUZAI_MARGIN = 10

# 必要数(製品丈の閾値ごと)
KAKUZAI_THR1 = 1800   # 〜1800: 2本
KAKUZAI_THR2 = 2400   # 〜2400: 3本 / 超: 4本
KAKUZAI_CNT1 = 2
KAKUZAI_CNT2 = 3
KAKUZAI_CNT3 = 4
MATSUITA_CNT = 2      # 松板は常に2枚

# 倉庫送信の品名に使う固定文字列(VBA `C_KAKUZAI_SIZE` / `C_MATSUZAI_SIZE`)。
# 全角スペース区切りなのもVBAのまま
KAKUZAI_SIZE_LABEL = "溝切角材　75x75"
MATSUITA_SIZE_LABEL = "松板　24x120"

TABLE_MATSUITA_KAKUZAI = "松板角材"

# DB未ヒット/エラー時にラベルへ出す文字列(VBAの文言をそのまま使う。
# 「寸法を出すか、この文字列をそのまま出すか」の判定にも使われる)
INFO_NOT_FOUND = "該当なし"
INFO_DB_ERROR = "DB接続エラー"
_ERROR_INFOS = (INFO_NOT_FOUND, INFO_DB_ERROR, "テーブル名エラー")

# ==================================================================
# プロテックモード 定数 — VBA `CheckAndSetProtecMode`
# ==================================================================
# VBA は納入先「ｶ)ﾏﾂｼﾀｼﾖｳﾃﾝ」判定も持っていたが `cond1 = False` で
# 明示的に廃止済み。包装仕様NOだけで判定する
PROTEC_SPECS = ("1P1216", "1P1125", "1P1211")
PROTEC_SPEC_1P1216 = "1P1216"   # ｺｰﾐ金属。上用の幅許容が -5mm になる

PROTEC_BOARD_TYPE = "プロテックボード"
DEFAULT_BOARD_TYPE = "ハードボード"


# ==================================================================
# モード判定
# ==================================================================
def is_1p0113(packaging_spec: str) -> bool:
    """VBA `Apply1P0113Mode` の `effectiveNo = HOSOSIYO_1P0113` 判定。"""
    return (packaging_spec or "").strip() == HOSOSIYO_1P0113


@dataclass
class ProtecState:
    """VBA `mIsProtecMode` / `mIsProtec1P1216` の組。"""

    is_protec: bool = False
    is_1p1216: bool = False

    @property
    def board_type(self) -> str:
        """プロテックなら「プロテックボード」、でなければ既定の「ハードボード」。"""
        return PROTEC_BOARD_TYPE if self.is_protec else DEFAULT_BOARD_TYPE


def protec_state(packaging_spec: str) -> ProtecState:
    """VBA `CheckAndSetProtecMode` の移植(判定部分だけ)。"""
    spec = (packaging_spec or "").strip()
    state = ProtecState(is_protec=spec in PROTEC_SPECS,
                        is_1p1216=(spec == PROTEC_SPEC_1P1216))
    log.debug("protec_state: spec=%s is_protec=%s is_1p1216=%s",
              spec, state.is_protec, state.is_1p1216)
    return state


# ==================================================================
# 1P0113: 寸法と必要数の計算
# ==================================================================
def calc_a_value(product_length: int) -> int:
    """松板の丈(A値)を製品丈から決める(VBA `Load1P0113Materials` のA算出)。"""
    if product_length <= A_THR1:
        return product_length
    if product_length <= A_THR2:
        return A_VAL1
    if product_length <= A_THR3:
        return A_VAL2
    return A_VAL3


def calc_kakuzai_count(product_length: int) -> int:
    """溝切角材の1セット本数(VBA `m1P0113BaseKak`)。"""
    if product_length <= KAKUZAI_THR1:
        return KAKUZAI_CNT1
    if product_length <= KAKUZAI_THR2:
        return KAKUZAI_CNT2
    return KAKUZAI_CNT3


def calc_kakuzai_length(product_width: int) -> int:
    """溝切角材の実際の丈(VBA `m1P0113KakActualLen`)。"""
    return product_width + KAKUZAI_MARGIN


# ==================================================================
# 1P0113: 資材マスタ検索
# ==================================================================
@dataclass
class MaterialHit:
    """`松板角材` から引いた1件。見つからなければ `found=False`。"""

    name: str                 # 品名(溝切角材 / 松板)
    found: bool = False
    info: str = INFO_NOT_FOUND  # "75x75 丈:1001-1999 (備考)" 形式
    code: str = ""
    unit: str = ""

    @property
    def is_ok(self) -> bool:
        """VBA の `isKakOk` / `isMatOk` 相当。"""
        return self.found and self.info not in _ERROR_INFOS

    @property
    def full_info(self) -> str:
        """クリック時のTip用フル情報(VBA `m1P0113KakFullInfo`)。

        倉庫送信の品名・発注コード・単位はこの文字列を
        `parse_info` で切り出して作るので、書式を変えてはいけない。
        """
        return (f"{self.name} {self.info}"
                f"  CD:{self.code or '---'}"
                f"  単位:{self.unit or '---'}")

    @property
    def cell_text(self) -> str:
        """一覧セル表示用(VBA `m1P0113KakCellText`。改行あり)。"""
        if not self.is_ok:
            return f"{self.name} {self.info}"
        return (f"{self.name}\n{self.info}\n"
                f"コード:{self.code or '---'}\n"
                f"単位:{self.unit or '---'}")


def find_material(
    conn: sqlite3.Connection, *, name: str, atsu: int, haba: int, target_length: int,
) -> MaterialHit:
    """`松板角材` から「品名・厚・幅が一致し、丈帯が`target_length`を含む」行を引く。

    VBA は `Val(厚)=75 AND Val(幅)=75 AND Val(丈min)<=X AND Val(丈max)>=X`
    という文字列列前提のSQLだったが、SQLite版は数値列なのでそのまま比較する。

    【VBAからの意図的な変更】VBA は `ORDER BY` を付けずに先頭行を採って
    いた。実データには丈帯が重複する行(例: 松板 4000-4999 が 355047 と
    355054 の2件)があり、Access の返す順に依存して発注コードが変わって
    しまう。Python版は `管理番号` 昇順で必ず先頭を採り、結果を再現可能に
    する(Accessも通常は主キー順に返すため、実質同じ行が選ばれる)。
    """
    rows = db.fetch_all(
        conn,
        f"SELECT 品名, 厚, 幅, 丈min, 丈max, コード, 単位, 備考 "
        f"FROM {TABLE_MATSUITA_KAKUZAI} "
        f"WHERE 品名 = ? AND 厚 = ? AND 幅 = ? AND 丈min <= ? AND 丈max >= ? "
        f"ORDER BY 管理番号",
        (name, atsu, haba, target_length, target_length),
        caller_name="find_material",
    ) or []

    if not rows:
        log.debug("find_material: 該当なし %s 厚=%s 幅=%s 丈=%s",
                  name, atsu, haba, target_length)
        return MaterialHit(name=name)

    row = rows[0]
    biko = (row["備考"] or "").strip()
    info = f"{_num(row['厚'])}x{_num(row['幅'])} 丈:{_num(row['丈min'])}-{_num(row['丈max'])}"
    if biko:
        info += f" ({biko})"
    hit = MaterialHit(name=name, found=True, info=info,
                      code=(row["コード"] or ""), unit=(row["単位"] or ""))
    log.debug("find_material: ヒット %s code=%s", info, hit.code)
    return hit


def _num(value) -> str:
    """VBA が `CStr(rs.fields(...))` で出していた見た目に合わせる。

    Access側は数値でも `75` のように整数表記だったので、
    小数点以下が無いREAL(75.0)は `75` と出す。
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ==================================================================
# 1P0113: まとめて読み込む (VBA `Load1P0113Materials`)
# ==================================================================
@dataclass
class Materials1P0113:
    """1P0113モードの表示に必要な値一式。"""

    product_width: int = 0
    product_length: int = 0
    a_value: int = 0
    kakuzai_length: int = 0        # 製品幅 + マージン
    base_kakuzai: int = 0          # 1セットの本数
    base_matsuita: int = MATSUITA_CNT
    kakuzai: MaterialHit = field(default_factory=lambda: MaterialHit(name=KAKUZAI_NAME))
    matsuita: MaterialHit = field(default_factory=lambda: MaterialHit(name=MATSUITA_NAME))

    @property
    def kakuzai_label(self) -> str:
        """VBA `lblKakuzaiDB.caption`。ヒット時は実際の丈、外れたら理由。"""
        return f"{self.kakuzai_length}mm" if self.kakuzai.is_ok else self.kakuzai.info

    @property
    def matsuita_label(self) -> str:
        """VBA `lblMatsutaDB.caption`。ヒット時はA値、外れたら理由。"""
        return f"{self.a_value}mm" if self.matsuita.is_ok else self.matsuita.info

    @property
    def tip_text(self) -> str:
        """VBA `DynamicTip_PalUnit.caption`。

        単位は松板側だけを出す(VBAのまま。角材と松板で単位が違う場合は
        角材側が見えないが、実データはどちらも本/枚で固定運用のため
        問題にならない)。
        """
        return (f"角材 CD:{self.kakuzai.code or '---'}"
                f"  松板 CD:{self.matsuita.code or '---'}"
                f"  単位:{self.matsuita.unit or '---'}")

    def kakuzai_count(self, qty: int) -> int:
        return self.base_kakuzai * qty

    def matsuita_count(self, qty: int) -> int:
        return self.base_matsuita * qty


def load_materials(
    conn: sqlite3.Connection, product_width: int, product_length: int,
) -> Materials1P0113:
    """VBA `Load1P0113Materials` の移植(ラベル更新を除いた計算・検索部分)。

    製品サイズが未設定(0以下)なら検索せず、ラベルに出す文言だけ
    「製品ｻｲｽﾞ未設定」にして返す(VBA踏襲)。
    """
    if product_width <= 0 or product_length <= 0:
        log.debug("load_materials: 製品サイズ未設定 w=%s l=%s", product_width, product_length)
        unset = "製品ｻｲｽﾞ未設定"
        return Materials1P0113(
            product_width=product_width, product_length=product_length,
            kakuzai=MaterialHit(name=KAKUZAI_NAME, info=unset),
            matsuita=MaterialHit(name=MATSUITA_NAME, info=unset),
        )

    a_value = calc_a_value(product_length)
    materials = Materials1P0113(
        product_width=product_width,
        product_length=product_length,
        a_value=a_value,
        kakuzai_length=calc_kakuzai_length(product_width),
        base_kakuzai=calc_kakuzai_count(product_length),
        # 溝切角材は製品「幅」の帯、松板はA値(製品「丈」由来)の帯で引く
        kakuzai=find_material(conn, name=KAKUZAI_NAME, atsu=KAKUZAI_ATSU,
                              haba=KAKUZAI_HABA, target_length=product_width),
        matsuita=find_material(conn, name=MATSUITA_NAME, atsu=MATSUITA_ATSU,
                               haba=MATSUITA_HABA, target_length=a_value),
    )
    log.debug("load_materials: A=%s 角材丈=%s 本数=%s 松板=%s枚",
              a_value, materials.kakuzai_length,
              materials.base_kakuzai, materials.base_matsuita)
    return materials


# ==================================================================
# 表示テキストの組み立てと読み戻し
# ==================================================================
def kakuzai_count_caption(count: int) -> str:
    """VBA `lblKakuzaiCount.caption`。"""
    return f"{KAKUZAI_NAME}: {count}本"


def matsuita_count_caption(count: int) -> str:
    """VBA `lblMatsutaCount.caption`。"""
    return f"{MATSUITA_NAME}: {count}枚"


def extract_count(caption: str) -> str:
    """VBA `ExtractCount` の移植。「溝切角材: 6本」→「6」。

    ": " で切って右側の末尾1文字(単位)を落とすだけ、というVBAの
    素朴な実装をそのまま踏襲する(倉庫送信の発注数がこれで決まる)。
    """
    pos = caption.find(": ")
    if pos < 0:
        return ""
    right = caption[pos + 2:].strip()
    return right[:-1].strip()


def parse_info(full_info: str, key: str) -> str:
    """VBA `Parse1P0113Info` の移植。

    改行をスペースに直してから空白で分割し、`key:` で始まるトークンの
    残りを返す。`full_info` は `MaterialHit.full_info` の書式が前提。

        parse_info("溝切角材 75x75 丈:1001-1999  CD:251023  単位:本", "CD")
        -> "251023"
    """
    normalized = (full_info or "").replace("\n", " ")
    prefix = f"{key}:"
    for token in normalized.split(" "):
        token = token.strip()
        if token.startswith(prefix):
            return token[len(prefix):].strip()
    return ""
