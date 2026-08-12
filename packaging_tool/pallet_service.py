"""簡易在庫(パレット位置別在庫)の業務ロジック

VBA `UFMAP` ユーザーフォーム + `PalletHistoryModule_v2`内 `InventoryManager`
セクションの一部(桁数/脚数/適合範囲の自動計算部分)の移植。

対応関係:
    VBA                              -> Python
    ------------------------------------------------------------
    NormalizeKey                       -> normalize_key
    IsEmpty2                            -> is_blank
    CalcDakeMin / CalcDakeMax            -> calc_dake_min / calc_dake_max
    CalcHabaMin / CalcHabaMax             -> calc_haba_min / calc_haba_max
    CalcAshi / CalcKeta                    -> calc_ashi / calc_keta
    BuildFixedRangeTable / AddRange         -> build_pallet_range_table
    UpdatePalletAll / UpdateAll              -> recompute_fit_ranges
    (完了メッセージの内訳)                     -> RecomputeSummary.counts()
    SearchAndHighlight                          -> search_pallets
    ShowInventoryByLocation                       -> pallets_at_position
    btnReceive_Click                                -> receive
    btnDispatch_Click                                -> issue

【移植で変更した点】
    - VBA版は起動時に PalletMaster 全件をメモリ配列(`m_PalletData`)へ
      キャッシュし、検索/位置表示はその配列に対して行っていた。
      Python版はSQLiteに直接クエリする(小〜中規模テーブルであれば
      キャッシュより単純で、更新直後の再表示でも常に最新値を返せる)。
    - 受入/払出の「他端末が同時に更新していないか」の確認は、VBA版は
      「更新日時を読み直して比較 → 別のUPDATE文を発行」の2段階だったが、
      Python版は `UPDATE ... WHERE 管理番号=? AND 更新日時=?` の
      比較送信(compare-and-swap)を1文で行うことで同じ安全性を保ちつつ
      単純化している。
    - 受入時の「置き場が既知のボタンか」というチェックは、VBA版が
      Excelフォーム上に配置された物理ボタンのキャプション一覧に依存
      していたため(今回のエクスポートにはその一覧が含まれない)、
      Python版では「位置は空でなければ登録可」とし、位置一覧は
      PalletMasterに実在するデータから動的に生成する。
    - 主キー(管理番号)はVBA版が`MAX(管理番号)+1`を手計算していたが
      (複数端末同時実行での重複リスクがあった)、Python版はSQLiteの
      AUTOINCREMENTに任せる。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config, db
from .logging_utils import get_logger

log = get_logger("pallet_service")

# ------------------------------------------------------------------
# NormalizeKeyLocal の移植
# ------------------------------------------------------------------
_FULLWIDTH_DIGIT_START = 0xFF10
_FULLWIDTH_DIGIT_END = 0xFF19
_FULLWIDTH_UPPER_START = 0xFF21
_FULLWIDTH_UPPER_END = 0xFF3A
_FULLWIDTH_LOWER_START = 0xFF41
_FULLWIDTH_LOWER_END = 0xFF5A
_MULTIPLY_SIGN = "×"  # ×


# **かけるとしか読めない文字。** 位置に関わらず × にそろえる
#   × U+00D7 / ✕ U+2715 / ✖ U+2716 / ╳ U+2573
_MULTIPLY_MARKS = frozenset("×✕✖╳")

# **数字に挟まれたときだけ**「かける」と読む文字。
#   X x Ｘ ｘ * ＊ (「5x10」「5*10」と打つ人がいる)
#
# 位置を見るのが要点です。挟まれていないものまで × にすると、
# `EXﾀｲﾄ` が `E×ﾀｲﾄ`、`EX2方向` が `E×2方向` になります ──
# どちらも実マスタに合わせて100行あり、そこの X は EX受注 の X であって
# 「かける」ではありません。固定表の鍵は `1×2` `4×8` `5×10`
# `4×10` `3×6` `5×8` と**すべて数字に挟まれた形**なので、
# この規則で取りこぼしは出ません。
_MULTIPLY_IF_BETWEEN_DIGITS = frozenset("X*＊")

# 詰めて消す空白。全角の空白(U+3000)も含める
_SPACES = frozenset(" \t　 ")


def normalize_key(s: str) -> str:
    """固定適合表を引くための鍵をそろえる(VBA `NormalizeKey` の移植)。

    【元のVBAには取りこぼしがあった】
    `Select Case` の評価順の都合で、「全角英大文字」「全角英小文字」
    「半角a-z」の分岐が「×へ変換」の分岐より**先**に来ます。そのため
    全角のＸ/ｘと半角小文字のxは手前で捕まり、`×` ではなく半角大文字
    `X` になっていました。結果として

        "5X10" → "5×10"   (固定表に当たる)
        "5x10" → "5X10"   (**当たらない**)

    と、同じ寸法の書き方で結果が割れていました。マスタに小文字で
    書かれた行は固定適合を受け取れず、段階表からの計算に落ちます。

    ここでそろえるのは4つです。

      1. 前後と**中の空白**を詰める(「5 × 10」も同じ鍵にする)
      2. 全角の英数字を半角に、英字を大文字に
      3. かけるとしか読めない記号(× ✕ ✖ ╳)は × に
      4. **数字に挟まれた** X / x / Ｘ / ｘ / * / ＊ だけを × に

    4 で位置を見るのが要点です。挟まれていないものまで変えると
    `EXﾀｲﾄ` が `E×ﾀｲﾄ` になります(実マスタに100行あり、
    そこの X は EX受注 の X です)。

    **そろえたことは黙っていません。** `recompute_fit_ranges` が
    「書き方をそろえて拾った行」として管理番号と元の文字を報告するので、
    マスタ側を直す手がかりになります(直さなくても選定は通ります)。
    """
    # --- 1〜3: 文字ごとにそろえる ---
    chars: list[str] = []
    for c in s.strip():
        code = ord(c)
        if c in _SPACES:
            continue                      # 「5 × 10」のような中の空白も詰める
        if c in _MULTIPLY_MARKS:
            chars.append(_MULTIPLY_SIGN)
        elif _FULLWIDTH_DIGIT_START <= code <= _FULLWIDTH_DIGIT_END:
            chars.append(chr(code - _FULLWIDTH_DIGIT_START + ord("0")))
        elif _FULLWIDTH_UPPER_START <= code <= _FULLWIDTH_UPPER_END:
            chars.append(chr(code - _FULLWIDTH_UPPER_START + ord("A")))
        elif _FULLWIDTH_LOWER_START <= code <= _FULLWIDTH_LOWER_END:
            chars.append(chr(code - _FULLWIDTH_LOWER_START + ord("A")))
        elif "a" <= c <= "z":
            chars.append(c.upper())
        else:
            chars.append(c)

    # --- 4: 数字に挟まれた X / * だけを × にする ---
    #     ここまでで全角Ｘ/ｘと半角xは大文字 X にそろっている
    for i, c in enumerate(chars):
        if c not in _MULTIPLY_IF_BETWEEN_DIGITS:
            continue
        before = chars[i - 1] if i else ""
        after = chars[i + 1] if i + 1 < len(chars) else ""
        if before.isdigit() and after.isdigit():
            chars[i] = _MULTIPLY_SIGN
    return "".join(chars)


# ------------------------------------------------------------------
# 段階判定表(タイトパレット選定基準表)の移植
# 数値は変更しないこと(社内基準表準拠の固定値)
# ------------------------------------------------------------------
def calc_dake_min(dake: int) -> int:
    """丈適合min: PDF「タイトパレット選定基準表：丈方向」準拠。

    `calc_dake_max` と必ず対で使うこと(min <= max が保証される)。
    """
    table = (
        (200, 110), (300, 191), (400, 291), (500, 391), (600, 491),
        (700, 591), (800, 691), (900, 791), (1000, 891),
        (1100, 991), (1200, 1091), (1300, 1191), (1450, 1291), (1550, 1441),
        (1800, 1541), (2100, 1791), (2350, 2091), (2650, 2341), (3150, 2641),
        (3650, 3141), (4150, 3641), (4600, 4141), (5100, 4591), (5600, 5091),
        (6100, 5591),
    )
    for threshold, value in table:
        if dake <= threshold:
            return value
    return 5591  # 6100超


def calc_dake_max(dake: int) -> int:
    """丈適合max: `calc_dake_min` と対になる上限表。

    以前は「丈 - 10」で算出していたが、丈が小さい行(実データに幅2.5mmの
    ような異常値があった)では min を下回り、適合範囲が逆転して
    何を検索してもヒットしない行が生まれていた。
    min と同じ段階表から引くことで min <= max を常に満たす。
    """
    table = (
        (200, 190), (300, 290), (400, 390), (500, 490), (600, 590),
        (700, 690), (800, 790), (900, 890), (1000, 990),
        (1100, 1090), (1200, 1190), (1300, 1290), (1450, 1440), (1550, 1540),
        (1800, 1790), (2100, 2090), (2350, 2340), (2650, 2640), (3150, 3140),
        (3650, 3640), (4150, 4140), (4600, 4590), (5100, 5090), (5600, 5590),
        (6100, 6090),
    )
    for threshold, value in table:
        if dake <= threshold:
            return value
    return 99999  # 6100超は上限なし


# 製品とパレットの現物サイズの間に必ず取る余裕(mm)。
#
# 実マスタは 適合max = 現物サイズ - 10 で入っている(2584行中2533行。
# 残りは記号/業界の固定表で -20 等の別値)。
PALLET_CLEARANCE = 10


def cap_to_pallet(band_max: int, pallet_size: int) -> int:
    """段階表の上限を、そのパレットの現物サイズで頭打ちにする。

    【なぜ必要か】
    基準表は「製品サイズの帯 → その帯に使う**標準**パレット」の対応表で、
    帯の上限はその帯の標準パレットの寸法から来ている。ところが
    再計算はこれを**そのパレット自身の寸法**に当てはめるため、
    標準サイズでないパレットが帯の上限を名乗ってしまう。

    たとえば丈2970のパレットは「2651〜3150」の帯に入るので上限3140を
    もらうが、2970のパレットに3140の製品は当然載らない。幅も同じで、
    1490のパレットが上限1540を名乗る。この2つが重なると
    **1490x2970のパレットが1528x3053の製品の候補になる**。

    実マスタの値(1490なら1480、2970なら2960)は現物サイズ-10であり、
    帯の上限をここでクランプすると2584行中2533行がぴったり一致する
    (クランプ無しでは653行しか一致しない)。つまりこのクランプが
    本来の仕様で、移植した当時のVBAからは抜け落ちていた。
    **いまの移植元 `UpdateAll` は同じクランプを持っている**
    (`If dakeMaxCalc > dake - 10 Then ...`)ので、両者は一致している。

    クランプの結果が段階表のminを下回るパレット(幅2.5mmなど実データの
    異常値)は範囲が逆転するが、それは実マスタでも同じ状態なので
    そのままにする(その行は何を検索してもヒットしない)。
    """
    return min(band_max, pallet_size - PALLET_CLEARANCE)


def calc_ashi(dake: int) -> int:
    """脚数: PDF「タイトパレット選定基準表：丈方向」準拠。"""
    if dake <= 1550:
        return 2
    if dake <= 2650:
        return 3
    if dake <= 3150:
        return 4
    if dake <= 4150:
        return 5
    if dake <= 4600:
        return 6
    return 7


def calc_keta(haba: int) -> int:
    """桁数: PDF「タイトパレット選定基準表：幅方向」パレット幅→松板使用数。

    1801以上は**基準表に無い範囲**ですが、現物の1900幅パレットの桁が
    7本だったことから 7 を返します(移植元 `CalcKeta` の注記どおり)。
    以前は6で止めていたので、1801以上のパレットは桁数が1本少なく
    入っていました。
    """
    if haba <= 350:
        return 2
    if haba <= 700:
        return 3
    if haba <= 1280:
        return 4
    if haba <= 1400:
        return 5
    if haba <= 1800:
        return 6
    return 7


def calc_haba_min(haba: int) -> int:
    """巾適合min: PDF「タイトパレット選定基準表：幅方向」準拠。

    `calc_haba_max` と必ず対で使うこと(min <= max が保証される)。
    301〜400の帯が300始まりなのは2山計算対応(305等の半端値を吸収するため)。
    """
    table = (
        (150, 50), (200, 141), (250, 191), (300, 241),
        (400, 300), (450, 391), (500, 441), (550, 491), (600, 541),
        (650, 591), (700, 641), (750, 691), (800, 741), (850, 791),
        (900, 841), (950, 891), (1000, 941), (1050, 991), (1150, 1041),
        (1250, 1141), (1300, 1241), (1350, 1291), (1450, 1341), (1550, 1441),
        (1650, 1541), (1750, 1641), (1850, 1741),
    )
    for threshold, value in table:
        if haba <= threshold:
            return value
    return 1741  # 1850超


def calc_haba_max(haba: int) -> int:
    """巾適合max: `calc_haba_min` と対になる上限表。

    以前は「幅 - 10」で算出していたため、幅が10mm未満の行では負の値になり
    min を下回っていた(実データのPalletMasterに70件存在し、この行は
    どんな製品サイズで検索しても絶対にヒットしなかった)。
    """
    table = (
        (150, 140), (200, 190), (250, 240), (300, 290),
        (400, 390), (450, 440), (500, 490), (550, 540), (600, 590),
        (650, 640), (700, 690), (750, 740), (800, 790), (850, 840),
        (900, 890), (950, 940), (1000, 990), (1050, 1040), (1150, 1140),
        (1250, 1240), (1300, 1290), (1350, 1340), (1450, 1440), (1550, 1540),
        (1650, 1640), (1750, 1740), (1850, 1840),
    )
    for threshold, value in table:
        if haba <= threshold:
            return value
    return 99999  # 1850超は上限なし


@dataclass
class PalletRangeTable:
    """`BuildPalletRangeTable` の移植。記号/業界キーごとの固定適合範囲。"""

    w_min: dict[str, int] = field(default_factory=dict)
    w_max: dict[str, int] = field(default_factory=dict)
    l_min: dict[str, int] = field(default_factory=dict)
    l_max: dict[str, int] = field(default_factory=dict)
    combo: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    keta: dict[str, int] = field(default_factory=dict)
    ashi: dict[str, int] = field(default_factory=dict)


def build_pallet_range_table() -> PalletRangeTable:
    t = PalletRangeTable()

    # 記号別 桁数・脚数固定値
    keta_ashi = {
        "C1": (3, 3), "C2": (4, 3), "C3": (3, 3), "C4": (4, 3),
        "C5": (4, 2), "C6": (4, 3), "C7": (4, 2), "C8": (4, 2),
        "P1": (4, 2), "P2": (4, 2), "P3": (4, 2), "P4": (4, 2),
        "P5": (5, 2), "P6": (4, 3), "P7": (4, 3),
    }
    for sym, (keta, ashi) in keta_ashi.items():
        t.keta[sym] = keta
        t.ashi[sym] = ashi

    def add_range(key: str, w_min: int, w_max: int, l_min: int, l_max: int) -> None:
        nkey = normalize_key(key)
        t.w_min[nkey] = w_min
        t.w_max[nkey] = w_max
        t.l_min[nkey] = l_min
        t.l_max[nkey] = l_max

    # 業界単独
    add_range("1×2", 935, 1005, 1700, 2005)
    add_range("4×8", 1185, 1255, 1800, 2505)
    add_range("5×10", 1470, 1540, 2700, 3090)
    add_range("4×10", 1215, 1260, 2700, 3130)
    add_range("3×6", 900, 945, 1700, 2030)
    add_range("5×8", 1485, 1530, 2000, 2530)

    # Cシリーズ
    add_range("C1", 410, 490, 600, 750)
    add_range("C2", 450, 530, 680, 1110)
    add_range("C3", 480, 560, 680, 860)
    add_range("C4", 560, 640, 820, 1030)
    add_range("C5", 560, 640, 780, 1290)
    add_range("C6", 650, 730, 830, 1110)
    add_range("C7", 740, 820, 560, 850)
    add_range("C8", 920, 1000, 640, 1080)

    # Pシリーズ
    add_range("P1", 500, 580, 440, 580)
    add_range("P2", 550, 630, 480, 630)
    add_range("P3", 650, 730, 480, 730)
    add_range("P4", 850, 930, 560, 930)
    add_range("P5", 1015, 1095, 680, 1095)
    add_range("P6", 1150, 1230, 830, 1230)
    add_range("P7", 1250, 1330, 860, 1330)

    # 複合キー(業界|記号)
    combo_key = f"{normalize_key('5×10')}|{normalize_key('強度UP')}"
    t.combo[combo_key] = (1485, 1540, 2700, 3090)

    return t


def is_blank(value: Any) -> bool:
    """VBA `IsEmpty2` の移植。**NULL・空文字・0 をすべて「空」とみなす。**

    脚数・桁数は「まだ入っていない行を埋める」ためのもので、現場が手で
    入れた値を潰してはいけません。0 も空に含めるのは、取り込み元の欄が
    0 で埋まっていることがあるためです(空欄と0の意味は同じ)。
    """
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    try:
        return float(text) == 0
    except ValueError:
        return False


@dataclass
class LooseMatch:
    """書き方が違っていたので、そろえてから固定表に当てた行。

    **拾ったことを黙っていません。** 拾えば選定は通りますが、マスタに
    ゆれが残っていること自体は直したほうがよい事実です。管理番号と
    元の文字を出すので、どの行を直せばよいかがそのまま分かります。
    """

    number: int
    column: str          # "業界" / "記号"
    raw: str             # マスタに入っている文字そのまま
    matched: str         # そろえた結果、当たったキー

    def line(self) -> str:
        return f"管理番号 {self.number}  {self.column}「{self.raw}」→ {self.matched}"


# まとめに並べる「表記ゆれ」の上限。全部並べると読む気が失せるので、
# 数だけ言って残りはログへ回す(**黙って切らない**)
LOOSE_LIST_LIMIT = 10


@dataclass
class RecomputeSummary:
    """何を何件直したか。**移植元の完了メッセージと同じ内訳を持つ。**

    「終わりました」だけでは、直ったのか何も起きなかったのかが
    分かりません。項目ごとに数えて出します。
    """

    ok: bool
    total: int = 0
    fixed: int = 0
    calculated: int = 0
    error: Optional[str] = None
    # 項目ごとの内訳(VBA `UpdatePalletAll` の完了メッセージと同じ並び)
    dake_min: int = 0
    dake_max: int = 0
    haba_min: int = 0
    haba_max: int = 0
    ashi: int = 0
    keta: int = 0
    # 書き方をそろえて初めて当たった行。**拾ったことを黙っていない**
    loose: list[LooseMatch] = field(default_factory=list)

    def counts(self) -> list[tuple[str, int]]:
        """内訳を(名前, 件数)で。画面もログもこれを読む。"""
        return [
            ("丈適合min更新", self.dake_min),
            ("丈適合max更新", self.dake_max),
            ("巾適合min補完", self.haba_min),
            ("巾適合max補完", self.haba_max),
            ("脚数補完", self.ashi),
            ("桁数補完", self.keta),
            ("固定適合上書き", self.fixed),
        ]

    def summary(self) -> str:
        """画面に出すまとめ。"""
        if not self.ok:
            return f"適合範囲を再計算できませんでした。\n{self.error or ''}"
        lines = [f"{self.total}行を見直しました。"]
        lines += [f"  {name}: {count}件" for name, count in self.counts()]
        if self.loose:
            # **拾えたことと、ゆれが残っていることは別。** 両方言う
            lines.append("")
            lines.append(f"書き方をそろえて拾った行: {len(self.loose)}件")
            lines.append("  (選定は通ります。マスタ側をそろえておくと確実です)")
            lines += [f"  {m.line()}" for m in self.loose[:LOOSE_LIST_LIMIT]]
            if len(self.loose) > LOOSE_LIST_LIMIT:
                lines.append(f"  ほか {len(self.loose) - LOOSE_LIST_LIMIT}件"
                             f"(全部は app.log に出ています)")
        return "\n".join(lines)


def loose_key_rows(conn: sqlite3.Connection) -> list[LooseMatch]:
    """書き方をそろえて初めて固定表に当たる行を**数えるだけ**。

    設定画面の「いまの状態」が読みます。再計算を押さなくても
    「マスタに表記ゆれがある」と分かるようにするためで、
    **書き込みは一切しません**。
    """
    table = build_pallet_range_table()
    rows = db.fetch_all(
        conn, "SELECT 管理番号, 記号, 業界 FROM PalletMaster",
        caller_name="loose_key_rows")
    if not rows:
        return []

    found: list[LooseMatch] = []
    for row in rows:
        for column in ("業界", "記号"):
            raw = str(row[column] or "")
            key = normalize_key(raw)
            if not key or raw.strip() == key:
                continue
            if key in table.w_min or any(
                    key in pair.split("|") for pair in table.combo):
                found.append(LooseMatch(number=row["管理番号"], column=column,
                                        raw=raw, matched=key))
    return found


def recompute_fit_ranges(conn: sqlite3.Connection) -> RecomputeSummary:
    """VBA `RunUpdatePalletAll` の移植。

    PalletMaster全行について、優先順位
        1. (業界|記号)の複合キーが固定表にあればそれを使う
        2. 記号が固定表にあればそれを使う(桁数/脚数は表にあれば併せて設定)
        3. 業界が固定表にあればそれを使う
        4. どれにも該当しなければ段階判定表から計算する
    で 巾適合min/max・丈適合min/max を再計算する。

    【脚数・桁数は「補完」で、上書きではない】
    移植元では、範囲を固定表から入れた行も計算した行も、最後に必ず
    同じ後始末(`SkipCalc:` の先)を通ります。そこでの脚数・桁数は
    **空のときだけ**埋めます ── 現場が手で入れた本数を潰さないためです。
    以前の移植はここを取り違えていて、

      - 計算した行では既存の脚数・桁数を**毎回上書き**していた
      - 業界だけで当たった行(1×2 など)や複合キーの行では、
        脚数・桁数が**いつまでも空のまま**だった

    元のVBAにあった「書き込みテスト」「別接続での永続化確認」は
    診断目的のみで業務ロジックに影響しないため、Python版では省略する。
    """
    table = build_pallet_range_table()
    rows = db.fetch_all(
        conn,
        "SELECT 管理番号, 幅, 丈, 記号, 業界, 脚数, 桁数 FROM PalletMaster",
        caller_name="recompute_fit_ranges",
    )
    if rows is None:
        return RecomputeSummary(ok=False, error="PalletMasterの読み取りに失敗しました")
    if not rows:
        return RecomputeSummary(ok=True, total=0)

    got = RecomputeSummary(ok=True, total=len(rows))
    with conn:
        for row in rows:
            dake = row["丈"] or 0
            haba = row["幅"] or 0
            sym2 = normalize_key(row["記号"] or "")
            gyk = normalize_key(row["業界"] or "")
            c_key = f"{gyk}|{sym2}"

            updates: dict[str, int] = {}
            from_table = False

            def note_loose(column: str, raw: Any, key: str) -> None:
                """そろえて初めて当たったなら、そのことを控える。

                元の文字と、そろえた結果を並べて出す。マスタのどの行を
                直せばよいかが、そのまま読める形にする。
                """
                text = str(raw or "")
                if text.strip() != key:
                    got.loose.append(LooseMatch(
                        number=row["管理番号"], column=column,
                        raw=text, matched=key))

            # --- 固定適合の上書き(最優先) -------------------------
            #     ① 複合キー(業界|記号) → ② 記号単独 → ③ 業界単独
            if c_key in table.combo:
                w_min, w_max, l_min, l_max = table.combo[c_key]
                updates.update({"巾適合min": w_min, "巾適合max": w_max,
                                "丈適合min": l_min, "丈適合max": l_max})
                note_loose("業界", row["業界"], gyk)
                note_loose("記号", row["記号"], sym2)
                got.fixed += 1
                from_table = True
            elif sym2 in table.w_min:
                updates.update({
                    "巾適合min": table.w_min[sym2], "巾適合max": table.w_max[sym2],
                    "丈適合min": table.l_min[sym2], "丈適合max": table.l_max[sym2],
                })
                # 記号で当たった行の桁数・脚数は**表の値で上書きする**。
                # 記号は現物の型そのものなので、表のほうが確かな事実
                if sym2 in table.keta:
                    updates["桁数"] = table.keta[sym2]
                if sym2 in table.ashi:
                    updates["脚数"] = table.ashi[sym2]
                note_loose("記号", row["記号"], sym2)
                got.fixed += 1
                from_table = True
            elif gyk and gyk in table.w_min:
                updates.update({
                    "巾適合min": table.w_min[gyk], "巾適合max": table.w_max[gyk],
                    "丈適合min": table.l_min[gyk], "丈適合max": table.l_max[gyk],
                })
                note_loose("業界", row["業界"], gyk)
                got.fixed += 1
                from_table = True

            # --- 段階表からの計算 -----------------------------------
            if not from_table:
                # min/maxは必ず対の段階表から引く(条件なしで常に上書きし、
                # 既存の誤った値も正しい範囲に矯正する)。
                # 上限は現物サイズでクランプする(下の cap_to_pallet を参照)
                if dake > 0:
                    updates["丈適合min"] = calc_dake_min(dake)
                    updates["丈適合max"] = cap_to_pallet(calc_dake_max(dake), dake)
                    got.dake_min += 1
                    got.dake_max += 1
                if haba > 0:
                    updates["巾適合min"] = calc_haba_min(haba)
                    updates["巾適合max"] = cap_to_pallet(calc_haba_max(haba), haba)
                    got.haba_min += 1
                    got.haba_max += 1
                got.calculated += 1

            # --- 後始末:脚数・桁数の補完 ---------------------------
            # **どの道を通った行もここを通る**(移植元の `SkipCalc:`)。
            # 埋めるのは**空のときだけ** ── 現場が手で入れた本数を潰さない
            if dake > 0 and "脚数" not in updates and is_blank(row["脚数"]):
                updates["脚数"] = calc_ashi(dake)
                got.ashi += 1
            if haba > 0 and "桁数" not in updates and is_blank(row["桁数"]):
                updates["桁数"] = calc_keta(haba)
                got.keta += 1

            if updates:
                set_sql = ", ".join(f"[{c}] = ?" for c in updates)
                conn.execute(
                    f"UPDATE PalletMaster SET {set_sql} WHERE 管理番号 = ?",
                    (*updates.values(), row["管理番号"]),
                )

    log.info("適合範囲を計算し直しました(%s行): %s", got.total,
             " / ".join(f"{name}={count}" for name, count in got.counts()))
    if got.loose:
        # **画面には上限つきで出すが、ログには全部残す。**
        # 何十件あってもマスタを直しきれるようにするため
        log.info("書き方をそろえて拾った行 %s件:\n  %s", len(got.loose),
                 "\n  ".join(m.line() for m in got.loose))
    return got


# ------------------------------------------------------------------
# 検索・位置表示
# ------------------------------------------------------------------
@dataclass
class SearchResult:
    rows: list[sqlite3.Row]
    matched_positions: set[str]


def search_pallets(conn: sqlite3.Connection, width: int, height: int, mode: str = "exact") -> SearchResult:
    """VBA `SearchAndHighlight` の移植。mode: "exact" または "range"(±50mm)。"""
    if mode == "range":
        tol = config.SEARCH_RANGE_TOLERANCE
        sql = "SELECT * FROM PalletMaster WHERE 位置 <> '' AND ABS(幅 - ?) <= ? AND ABS(丈 - ?) <= ?"
        params: tuple = (width, tol, height, tol)
    else:
        sql = "SELECT * FROM PalletMaster WHERE 位置 <> '' AND 幅 = ? AND 丈 = ?"
        params = (width, height)

    rows = db.fetch_all(conn, sql, params, caller_name="search_pallets") or []
    positions = {r["位置"] for r in rows if r["位置"]}
    return SearchResult(rows=rows, matched_positions=positions)


def list_positions(conn: sqlite3.Connection) -> list[str]:
    """既知の保管位置一覧(位置ボタングリッドの元データ)。"""
    rows = db.fetch_all(
        conn, "SELECT DISTINCT 位置 FROM PalletMaster WHERE 位置 <> '' ORDER BY 位置",
        caller_name="list_positions",
    ) or []
    return [r["位置"] for r in rows]


def pallets_at_position(conn: sqlite3.Connection, position: str) -> list[sqlite3.Row]:
    """VBA `ShowInventoryByLocation` の移植(位置一致、前後空白・大小文字を無視)。"""
    return db.fetch_all(
        conn,
        "SELECT * FROM PalletMaster WHERE TRIM(位置) = TRIM(?) COLLATE NOCASE",
        (position,),
        caller_name="pallets_at_position",
    ) or []


# ------------------------------------------------------------------
# 受入・払出トランザクション
# ------------------------------------------------------------------
@dataclass
class TransactionResult:
    ok: bool
    message: str
    new_stock: Optional[int] = None
    conflict: bool = False


def receive(
    conn: sqlite3.Connection,
    *,
    width: int,
    length: int,
    qty: int,
    position: str,
    symbol: str = "",
    industry: str = "",
    unit: str = "",
    note: str = "",
) -> TransactionResult:
    """VBA `btnReceive_Click` の移植(受入処理)。"""
    if qty <= 0:
        return TransactionResult(ok=False, message="台数は1以上を入力してください。")
    if not position:
        return TransactionResult(ok=False, message="位置を入力してください。")

    note = db.sanitize_for_db(note)
    now = db.now_db_string()
    industry_value = industry or "一般"

    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM PalletMaster WHERE 幅 = ? AND 丈 = ? AND 位置 = ?",
                (width, length, position),
            ).fetchone()

            if row is not None:
                new_stock = row["在庫数"] + qty
                set_cols: dict[str, object] = {"在庫数": new_stock, "更新日時": now}
                if unit:
                    set_cols["単位"] = unit
                if note:
                    set_cols["備考"] = note
                set_sql = ", ".join(f"[{c}] = ?" for c in set_cols)
                cur = conn.execute(
                    f"UPDATE PalletMaster SET {set_sql} WHERE 管理番号 = ? AND 更新日時 = ?",
                    (*set_cols.values(), row["管理番号"], row["更新日時"]),
                )
                if cur.rowcount == 0:
                    return TransactionResult(
                        ok=False, conflict=True,
                        message="他の端末がこの在庫を同時に更新しました。画面を更新してからやり直してください。",
                    )
                hist_symbol = row["記号"] or symbol
                hist_industry = row["業界"] or industry_value
            else:
                conn.execute(
                    "INSERT INTO PalletMaster "
                    "(幅, 丈, 巾適合min, 巾適合max, 丈適合min, 丈適合max, 業界, 記号, 位置, "
                    " 在庫数, リスト管理, 桁数, 脚数, コード, 単位, 備考, 更新日時) "
                    "VALUES (?, ?, 0, 0, 0, 0, ?, ?, ?, ?, '', 0, 0, '', ?, ?, ?)",
                    (width, length, industry_value, symbol, position, qty, unit, note, now),
                )
                new_stock = qty
                hist_symbol = symbol
                hist_industry = industry_value

            conn.execute(
                "INSERT INTO パレット入出庫履歴 "
                "(幅, 丈, 業界, 記号, 位置, 区分, 数量, 在庫数_更新後, 更新日時, 備考) "
                "VALUES (?, ?, ?, ?, ?, '受入', ?, ?, ?, ?)",
                (width, length, hist_industry, hist_symbol, position, qty, new_stock, now, note),
            )
    except sqlite3.Error as exc:
        log.warning("receive: エラー %s", exc)
        return TransactionResult(ok=False, message=f"データベース更新に失敗しました。({exc})")

    # 受入後は毎回、全行の適合範囲を再計算する(VBA `RunUpdatePalletAll` 呼び出しを踏襲)
    recompute_fit_ranges(conn)

    return TransactionResult(ok=True, message=f"受け入れを登録しました(在庫数: {new_stock})。", new_stock=new_stock)


def issue(
    conn: sqlite3.Connection,
    *,
    width: int,
    length: int,
    position: str,
    qty: int,
) -> TransactionResult:
    """VBA `btnDispatch_Click` の移植(払出処理)。"""
    if qty <= 0:
        return TransactionResult(ok=False, message="数量は1以上を入力してください。")

    now = db.now_db_string()

    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM PalletMaster WHERE 幅 = ? AND 丈 = ? AND 位置 = ?",
                (width, length, position),
            ).fetchone()
            if row is None:
                return TransactionResult(ok=False, message="対象レコードが見つかりませんでした。")

            cur_stock = row["在庫数"]
            new_stock = cur_stock - qty
            if new_stock < 0:
                return TransactionResult(
                    ok=False,
                    message=f"在庫数を超える数量は払い出せません。(在庫数: {cur_stock}, 要求数: {qty})",
                )

            if new_stock == 0 and (row["リスト管理"] or "").strip() != "要":
                cur = conn.execute(
                    "DELETE FROM PalletMaster WHERE 管理番号 = ? AND 更新日時 = ?",
                    (row["管理番号"], row["更新日時"]),
                )
            else:
                cur = conn.execute(
                    "UPDATE PalletMaster SET 在庫数 = ?, 更新日時 = ? WHERE 管理番号 = ? AND 更新日時 = ?",
                    (new_stock, now, row["管理番号"], row["更新日時"]),
                )
            if cur.rowcount == 0:
                return TransactionResult(
                    ok=False, conflict=True,
                    message="他の端末がこの在庫を同時に更新しました。画面を更新してからやり直してください。",
                )

            conn.execute(
                "INSERT INTO パレット入出庫履歴 "
                "(幅, 丈, 業界, 記号, 位置, 区分, 数量, 在庫数_更新後, 更新日時, 備考) "
                "VALUES (?, ?, ?, ?, ?, '払出', ?, ?, ?, '')",
                (width, length, row["業界"], row["記号"], position, qty, new_stock, now),
            )
    except sqlite3.Error as exc:
        log.warning("issue: エラー %s", exc)
        return TransactionResult(ok=False, message=f"データベース更新に失敗しました。({exc})")

    return TransactionResult(ok=True, message=f"払い出しを登録しました(在庫数: {new_stock})。", new_stock=new_stock)
