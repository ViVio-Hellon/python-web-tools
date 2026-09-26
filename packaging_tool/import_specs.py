"""取り込み定義(列の対応と型変換)

取り込み元のどの列を、手元のどの列に、どの型で入れるかの一覧。
取り込みスクリプト(`scripts/import_source.py`)と、アプリ内から実行する
取り込み(`data_sync`)の**両方がここを見る**ので、対応表は1か所だけ。

管理番号はSQLite側でAUTOINCREMENTに採番し直す(元IDを参照している
他テーブルは無い=外部キー制約が無いことをmdb-schemaで確認済み)。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable, Optional

from . import config, db


def _text(value: Any) -> str:
    """どんな値でも文字列にそろえる。

    取り込み元が sqlite3 になって、値は**型が付いたまま**返るように
    なりました(以前の mdb-export は何でも文字列でした)。数値に
    `.strip()` を呼んで落ちないよう、入口でここを通します。

    【bytes で返ってくるとき】
    sqlite3 は TEXT 列を既定で UTF-8 として str に直すが、**UTF-8として
    読めない列は bytes のまま返す**(元の Access からの移行時、丸数字や
    ローマ数字など JIS 拡張の文字を Shift-JIS(CP932)のまま書いた行が
    混ざっているとこれが起きる)。以前は問答無用で UTF-8 decode
    (`errors="replace"`)していたため、読めない字がそのまま `�` に
    置き換わって**永久に失われていた**(現場の声:「仕掛かり一覧に
    文字化けがある」)。CP932 として読めるならそちらを優先する。
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("cp932").strip()
        except UnicodeDecodeError:
            return value.decode("utf-8", "replace").strip()
    return str(value).strip()


def to_int(value: Any) -> Optional[int]:
    """整数として取り込む。読めない値は `None`(=空)。

    **`OverflowError` も読めない値として扱う。** sqlite3 の REAL 列は
    無限大を持てて(`9e999` と書けば `inf` が入る)、上流の変換が転ぶと
    実際に入ってきます。以前は `ValueError` しか捕まえていなかったので、

        OverflowError: cannot convert float infinity to integer

    が `import_tables` を突き抜けていました。**そこが効きました** ──
    表ごとの受け(`except sqlite3.Error`)は `OverflowError` を捕まえ
    ないので、**その1セルのために「まとめて取り込み」が丸ごと止まり**、
    止まった先の表(看板・パレット閾値・仕掛台帳)は一切入りません。
    手元の中身は巻き戻るので消えはしませんが、現場からは
    「取り込んだのに今日のぶんが無い」に見えます。

    読めないセルは**空と同じ扱い**にします(`NULL_FALLBACKS` が
    既定値を入れる)。行ごと捨てないのは、他の列は読めているからです。
    """
    text = _text(value)
    if text == "":
        return None
    try:
        return int(float(text))
    except (ValueError, OverflowError):
        return None


def to_real(value: Any) -> Optional[float]:
    """実数として取り込む。読めない値は `None`(=空)。

    **無限大と非数は入れない。** `float("inf")` は例外を出さずに通るので、
    そのまま手元のDBへ入ります。寸法や比重として使われたときに初めて
    おかしくなり、原因が取り込み元にあることが分からなくなります。
    """
    text = _text(value)
    if text == "":
        return None
    try:
        number = float(text)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def to_text(value: Any) -> str:
    return _text(value)


def to_flag(value: Any) -> int:
    """Yes/No を 0/1 にそろえる。

    書く人によって `1` / `TRUE` / `Yes` / `○` / `-1`(Access の True)と
    まちまちになる。**空欄は 1(有効)**にする ── 有効列を空にした行を
    無効と読むと、書いた人は登録したつもりで効かない。無効にしたいときは
    はっきり `0` と書いてもらう。
    """
    text = _text(value).casefold()
    if text == "":
        return 1
    if text in ("0", "false", "no", "n", "×", "x", "無効", "off"):
        return 0
    return 1


def to_datetime_text(value: Any) -> Optional[str]:
    """日時を ISO8601 へそろえる。

    取り込み元の書き方は一定しない("yyyy/m/d h:mm:ss" のゼロ埋め無し、
    ISO、日付だけ)。読めない形は**そのまま持つ** ── 握りつぶすと
    移行時に目視で気づけない。
    """
    text = _text(value)
    if text == "":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
                "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return text


# テーブルごとの (手元の列名, 取り込み元の列名, 変換関数) 定義。
# 管理番号はSQLite側でAUTOINCREMENTに採番し直す(元IDを参照している
# 他テーブルは無い=外部キー制約が無いことをmdb-schemaで確認済み)。
ColumnSpec = tuple[str, str, Callable[[Any], object]]

IMPORT_SPECS: dict[str, list[ColumnSpec]] = {
    "BoardMaster": [
        ("ボード幅", "ボード幅", to_int),
        ("ボード丈", "ボード丈", to_int),
        ("ボードタイプ", "ボードタイプ", to_text),
        ("データラベル", "データラベル", to_text),
    ],
    # 全端末の「使用する」を集めた表(書き戻しで各端末が足していく)。
    # 管理番号は取り込まない ── 手元で振り直す(ほかの書き戻し表と同じ)
    config.TBL_BOARD_USAGE: [
        ("ボード幅", "ボード幅", lambda v: to_int(v) or 0),
        ("ボード丈", "ボード丈", lambda v: to_int(v) or 0),
        ("ボードタイプ", "ボードタイプ", to_text),
        ("枚数", "枚数", lambda v: to_int(v) or 0),
        ("切断後幅", "切断後幅", lambda v: to_int(v) or 0),
        ("切断後丈", "切断後丈", lambda v: to_int(v) or 0),
        ("製品幅", "製品幅", lambda v: to_int(v) or 0),
        ("製品丈", "製品丈", lambda v: to_int(v) or 0),
        ("パレット幅", "パレット幅", lambda v: to_int(v) or 0),
        ("パレット丈", "パレット丈", lambda v: to_int(v) or 0),
        ("ロット番号", "ロット番号", to_text),
        ("使用日時", "使用日時", to_text),
    ],
    "CornerboardMaster": [
        ("アングル丈", "アングル丈", to_int),
        ("データラベル", "データラベル", to_text),
    ],
    "PalletPatterns": [
        ("パレット幅", "パレット幅", lambda v: to_int(v) or 0),
        ("パレット丈", "パレット丈", lambda v: to_int(v) or 0),
        ("製品幅", "製品幅", lambda v: to_int(v) or 0),
        ("製品丈", "製品丈", lambda v: to_int(v) or 0),
        ("登録日時", "登録日時", lambda v: to_datetime_text(v) or db.now_db_string()),
        ("更新日時", "更新日時", lambda v: to_datetime_text(v) or db.now_db_string()),
        ("使用回数", "使用回数", lambda v: to_int(v) or 0),
        # A~J枠はスキーマ上NULL許容(未使用枠="板なし"の意味として区別するため0にはしない)
        *[(f"{c}{f}", f"{c}{f}", conv)
          for c in "ABCDEFGHIJ"
          for f, conv in (("幅", to_int), ("丈", to_int), ("枚数", to_int), ("用途", to_text))],
    ],
    "梱包保護材": [
        ("曖昧表現", "曖昧表現", to_text),
        ("包装仕様書", "包装仕様書", to_text),
        ("材質", "材質", to_text),
        ("調質", "調質", to_text),
        ("用途コード", "用途コード", to_text),
        ("板厚下", "板厚下", to_real),
        ("板厚上", "板厚上", to_real),
        ("板厚", "板厚", to_real),
        ("板幅下", "板幅下", to_int),
        ("板幅上", "板幅上", to_int),
        ("板幅", "板幅", to_int),
        ("板丈下", "板丈下", to_int),
        ("板丈上", "板丈上", to_int),
        ("板丈", "板丈", to_int),
        ("使用保護材", "使用保護材", to_text),
        ("更新日", "更新日", to_text),
        ("備考", "備考", to_text),
    ],
    "松板角材": [
        ("品名", "品名", to_text),
        ("厚", "厚", lambda v: to_real(v) or 0.0),
        ("幅", "幅", lambda v: to_int(v) or 0),
        ("丈min", "丈min", lambda v: to_int(v) or 0),
        ("丈max", "丈max", lambda v: to_int(v) or 0),
        ("コード", "コード", to_text),
        ("単位", "単位", to_text),
        ("備考", "備考", to_text),
    ],
    "資材パレット注文管理": [
        ("登録日時", "登録日時", to_datetime_text),
        ("LotNo", "LotNo", to_text),
        ("品名", "品名", to_text),
        ("発注コード", "発注コード", to_text),
        ("単位", "単位", to_text),
        ("材質", "材質", to_text),
        ("調質", "調質", to_text),
        ("厚", "厚", lambda v: to_real(v) or 0.0),
        ("幅", "幅", lambda v: to_int(v) or 0),
        ("丈", "丈", lambda v: to_int(v) or 0),
        ("用途コード", "用途コード", to_text),
        ("納入先", "納入先", to_text),
        ("発注数", "発注数", to_int),
        ("取り消し済", "取り消し済", to_text),
        ("取り消し日時", "取り消し日時", to_datetime_text),
        ("確認済み", "確認済み", to_text),
        ("確認日時", "確認日時", to_datetime_text),
        # **取り込み元の行番号を覚えておく。** 手元の管理番号は取り込みの
        # たびに振り直される(AUTOINCREMENT)ので、あとから確認の印を
        # 共有の同じ行へ書き戻すときの手がかりにならない
        ("取込元管理番号", "管理番号", to_int),
        # どの端末が送ったか。取り消せるのはその端末だけ。共有に列が無い
        # (最初の送信がまだ)あいだは空で入り、どの現場からも取り消せる
        ("送信端末", "送信端末", to_text),
    ],
    "PalletMaster": [
        ("幅", "幅", to_int),
        ("丈", "丈", to_int),
        ("巾適合min", "巾適合min", lambda v: to_int(v) or 0),
        ("巾適合max", "巾適合max", lambda v: to_int(v) or 0),
        ("丈適合min", "丈適合min", lambda v: to_int(v) or 0),
        ("丈適合max", "丈適合max", lambda v: to_int(v) or 0),
        ("業界", "業界", lambda v: to_text(v) or "一般"),
        ("記号", "記号", to_text),
        ("位置", "位置", to_text),
        ("在庫数", "在庫数", lambda v: to_int(v) or 0),
        ("更新日時", "更新日時", lambda v: to_datetime_text(v) or db.now_db_string()),
        ("リスト管理", "リスト管理", to_text),
        ("桁数", "桁数", lambda v: to_int(v) or 0),
        ("脚数", "脚数", lambda v: to_int(v) or 0),
        ("コード", "コード", to_text),
        ("単位", "単位", to_text),
        ("備考", "備考", to_text),
    ],
    "パレット入出庫履歴": [
        ("幅", "幅", to_int),
        ("丈", "丈", to_int),
        ("業界", "業界", to_text),
        ("記号", "記号", to_text),
        ("位置", "位置", to_text),
        ("区分", "区分", to_text),
        ("数量", "数量", to_int),
        ("在庫数_更新後", "在庫数_更新後", to_int),
        ("更新日時", "更新日時", to_datetime_text),
        ("備考", "備考", to_text),
    ],
    "Form状態管理": [
        ("ライン名", "ライン名", to_text),
        ("状態", "状態", to_text),
    ],
    # 誰が何をできるか。1行 = 1つの許可。
    # 権限を増やしても**この定義は変わらない** ── 増えるのは
    # 「権限」列に入る値だけで、使える値は `access_control` が持つ
    "アクセス権限": [
        ("ログインID", "ログインID", to_text),
        ("PC名", "PC名", to_text),
        ("権限", "権限", to_text),
        ("有効", "有効", to_flag),
        ("備考", "備考", to_text),
    ],
}

# ------------------------------------------------------------------
# パレット適合閾値マスタ(7表)
#
# 表名・列名は移植元(VBA `PalletThresholdMaster.accdb`)の写し。
# 末尾4列(有効・並び順・備考・更新日時)は7表とも共通なので、
# 表ごとの列だけ書いて後ろに足す ── **同じ4行を7回書かない**。
#
# `ID` を入れていないのは、他のマスタと同じ理由です(取り込み元の
# 採番をそのまま持たず、手元でAUTOINCREMENTに振り直す)。マスタ管理
# 画面が打ち込める列もここから決まるので、ID が画面に出ることもない。
# ------------------------------------------------------------------
_THRESHOLD_TAIL: list[ColumnSpec] = [
    ("有効", "有効", to_flag),
    ("並び順", "並び順", lambda v: to_int(v) or 0),
    ("備考", "備考", to_text),
    ("更新日時", "更新日時", lambda v: to_datetime_text(v) or ""),
]

_THRESHOLD_HEAD: dict[str, list[ColumnSpec]] = {
    "PalletDakeThreshold": [
        ("入力最小値", "入力最小値", lambda v: to_int(v) or 0),
        ("入力最大値", "入力最大値", lambda v: to_int(v) or 0),
        ("適合最小値", "適合最小値", lambda v: to_int(v) or 0),
        ("適合最大値", "適合最大値", lambda v: to_int(v) or 0),
    ],
    "PalletHabaThreshold": [
        ("入力最小値", "入力最小値", lambda v: to_int(v) or 0),
        ("入力最大値", "入力最大値", lambda v: to_int(v) or 0),
        ("適合最小値", "適合最小値", lambda v: to_int(v) or 0),
        ("適合最大値", "適合最大値", lambda v: to_int(v) or 0),
    ],
    "PalletAshiThreshold": [
        ("入力最小値", "入力最小値", lambda v: to_int(v) or 0),
        ("入力最大値", "入力最大値", lambda v: to_int(v) or 0),
        ("脚数", "脚数", lambda v: to_int(v) or 0),
    ],
    "PalletKetaThreshold": [
        ("入力最小値", "入力最小値", lambda v: to_int(v) or 0),
        ("入力最大値", "入力最大値", lambda v: to_int(v) or 0),
        ("桁数", "桁数", lambda v: to_int(v) or 0),
    ],
    "PalletSymbolMaster": [
        ("記号", "記号", to_text),
        ("巾適合最小値", "巾適合最小値", lambda v: to_int(v) or 0),
        ("巾適合最大値", "巾適合最大値", lambda v: to_int(v) or 0),
        ("丈適合最小値", "丈適合最小値", lambda v: to_int(v) or 0),
        ("丈適合最大値", "丈適合最大値", lambda v: to_int(v) or 0),
        ("桁数", "桁数", lambda v: to_int(v) or 0),
        ("脚数", "脚数", lambda v: to_int(v) or 0),
    ],
    "PalletIndustryMaster": [
        ("業界", "業界", to_text),
        ("巾適合最小値", "巾適合最小値", lambda v: to_int(v) or 0),
        ("巾適合最大値", "巾適合最大値", lambda v: to_int(v) or 0),
        ("丈適合最小値", "丈適合最小値", lambda v: to_int(v) or 0),
        ("丈適合最大値", "丈適合最大値", lambda v: to_int(v) or 0),
    ],
    "PalletComboMaster": [
        ("業界", "業界", to_text),
        ("記号", "記号", to_text),
        ("巾適合最小値", "巾適合最小値", lambda v: to_int(v) or 0),
        ("巾適合最大値", "巾適合最大値", lambda v: to_int(v) or 0),
        ("丈適合最小値", "丈適合最小値", lambda v: to_int(v) or 0),
        ("丈適合最大値", "丈適合最大値", lambda v: to_int(v) or 0),
    ],
}

THRESHOLD_TABLES: tuple[str, ...] = tuple(_THRESHOLD_HEAD)

for _threshold_table, _head in _THRESHOLD_HEAD.items():
    IMPORT_SPECS[_threshold_table] = _head + _THRESHOLD_TAIL

for _kanban_table in config.BOARD_KANBAN_TABLES + ("看板_LS",):
    IMPORT_SPECS[_kanban_table] = [
        ("資材", "資材", to_text),
        ("サイズ", "サイズ", to_text),
        ("欲", "欲", to_text),
        ("不", "不", to_text),
        ("更新日", "更新日", to_text),
        ("発送", "発送", to_text),
        ("倉庫確認日時", "倉庫確認日時", to_text),
        ("常設品", "常設品", to_text),
    ]


# 意味を成さない不完全レコードとみなしてスキップする(0で埋めない)。
REQUIRED_KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "PalletMaster": ("幅", "丈"),
    "仕掛ロット": ("ロット番号",),
    "仕掛受注": ("受注番号",),
    # 権限が空の行は許可になっていない。入れても意味が無い
    "アクセス権限": ("権限",),
}

# 空文字を「無し」として扱う列を持つテーブル。
# `REQUIRED_KEY_COLUMNS` は `None` だけを撥ねるので、`to_text` が
# 返す空文字は通ってしまう。権限コードのように**空では意味を成さない**
# ものは、ここに挙げて空も撥ねる
BLANK_IS_MISSING: dict[str, tuple[str, ...]] = {
    "アクセス権限": ("権限",),
}

# 取り込み元に無くても**失敗にしない**テーブル。
#
# アクセス権限はあとから足した表で、既存の梱包資材マスタには入っていない。
# 無いことを失敗として数えると、取り込みが毎回「失敗」と出て、本当に
# 直すべき問題が埋もれる。無いときは現場モードだけで動く仕様なので、
# 案内として1行出すに留める。
#
# パレット適合閾値の7表も同じ立場。移植元(VBA)は別ファイル
# (PalletThresholdMaster.accdb)に持っていたので、現場の梱包資材マスタ
# にはまだ入っていません。無いあいだは手元に入れた基準表の初期値
# (`pallet_threshold.SEED`)で動き、マスタ管理画面から取り込み元に
# 作れます。
#
# ボード使用実績も同じ。共有にはまだ無く、**最初に送った端末が作る**
# (`sync_writeback.ensure_shared_tables`)。作られるまでは取り込まない
# (手元の記録はそのまま残る)
OPTIONAL_TABLES: frozenset[str] = (frozenset({"アクセス権限", config.TBL_BOARD_USAGE})
                                   | frozenset(THRESHOLD_TABLES))

# **取り込み元に無くても知らせない列**(表 → 取り込み元の列名)。
#
# 2026-09-24 の SIKALOT(変換し直したもの)から、BOX実績_枚本数・前々工程/
# 前工程実績_* など19列が無くなった。このうち取り込んでいたのは下の3つで、
# どれも画面の判断には効かない:
#   BOX実績_枚本数            … BOX最終実績_枚本数 が空のときの代わりだけ
#   前々工程/前工程実績_枚本数 … 画面のどこにも出していない(上の説明)
# 無いと「元のファイルに無い列があります」が毎回出て、本当に困る列
# (例: 列名が変わってロット番号が空になった件)が埋もれる。無ければ 0 で入る。
# **鍵や判断に使う列はここに入れないこと。**
OPTIONAL_COLUMNS: dict[str, frozenset[str]] = {
    "仕掛ロット": frozenset({"BOX実績_枚本数", "前々工程実績_枚本数", "前工程実績_枚本数"}),
    # 最初に送った端末が共有に足す列(`sync_writeback.SHARED_ADDED_COLUMNS`)
    config.TBL_WAREHOUSE_ORDER: frozenset({"送信端末"}),
}

# 梱包資材マスタ.sqlite3 ではなく、**看板マスタ.sqlite3(別ファイル)** から
# 読むテーブル。以前はこの8つも `IMPORT_SPECS` の他の表と同じく梱包資材
# マスタから読む定義になっていたが、現場の梱包資材マスタには一度も
# 入っておらず、取り込みのたびに「取り込めませんでした」の8件に
# 数えられていた。現場から渡された実データは看板マスタ.sqlite3という
# 別ファイルにあったので、そちらから読むよう `data_sync.import_master`
# 側で経路を分ける。
#
# `OPTIONAL_TABLES` とは別の理由付け。OPTIONAL_TABLES は「そのテーブルの
# 機能自体を使わない端末があってよい」、こちらは「テーブルは要るが
# 読む場所が違う」。看板マスタが見つからないときは
# `data_sync.import_master` が1行の案内を出す(個別のテーブルごとに
# 8件のエラーを出さない)。
KANBAN_TABLES: frozenset[str] = frozenset(
    config.BOARD_KANBAN_TABLES + ("看板_LS", config.TABLE_NAME_STATE))

# ------------------------------------------------------------------
# 仕掛台帳(ロット検索ページ用)の取り込み定義
#
# VBA版は社内共有 \\nlmsrvngy03\Read\【New】仕掛\台帳\ にある
# SIKALOT / SIKAHIKI / SIKAODR の3ファイル(いずれもテーブル名は
# 「仕掛」)を直接読んでいた。梱包資材マスタとは別ファイルなので、
# --lot-db / --hiki-db / --odr-db で個別に指定して取り込む。
# 列は元のSELECT文が読んでいたものだけに絞っている。
# ------------------------------------------------------------------
LOT_IMPORT_SPECS: dict[str, list[tuple[str, str, object]]] = {
    "仕掛ロット": [
        ("ロット番号", "ﾛｯﾄ番号", to_text),
        ("用途コード", "用途ｺｰﾄﾞ", to_text),
        ("用途名", "用途名", to_text),
        ("製造材質", "製造材質", to_text),
        ("製造調質", "製造調質", to_text),
        ("製造板厚", "製造板厚", to_real),
        ("製造板幅", "製造板幅", to_real),
        ("製造板丈", "製造板丈", to_real),
        ("オーダー板厚", "ｵｰﾀﾞｰ板厚", to_real),
        ("オーダー板幅", "ｵｰﾀﾞｰ板幅", to_real),
        ("オーダー板丈", "ｵｰﾀﾞｰ板丈", to_real),
        ("設計_設備コース", "設計_設備ｺｰｽ", to_text),
        ("実績_設備コース", "実績_設備ｺｰｽ", to_text),
        ("BOX実績_板厚", "BOX実績_板厚", to_real),
        ("BOX実績_板幅", "BOX実績_板幅", to_real),
        ("BOX実績_板丈", "BOX実績_板丈", to_real),
        ("BOX実績_枚本数", "BOX実績_枚本数", to_int),
        # ひとつ前・ふたつ前の工程の枚本数。
        #
        # **画面のどこにも出していません。** 一覧から外し(1工程目の行では
        # 必ず空で、一覧が出す先頭行はたいてい1工程目なので、ほぼ全部が
        # 空欄の列になるため)、詳細は項目を決め打ちしているので
        # (`presenters/lot.py` の `LOT_FIELDS`)自動では増えません。
        #
        # 「一覧に無いだけで詳細では見られる」と**読み違えないこと** ──
        # 実際に一度取り違えて、現場に誤った判断材料を渡しました。
        #
        # それでも取り込みは続けます(現場の判断)。手元に置いておけば、
        # 要るようになったときに取り込み直さずに使えます。使わないと
        # 決めたら、ここと `schema.sql` の2行を消してください
        ("前々工程実績_枚本数", "前々工程実績_枚本数", to_int),
        ("前工程実績_枚本数", "前工程実績_枚本数", to_int),
        # そのロットの**最終工程**の実績(現場の依頼で追加)。
        # ロット内で1つに定まるので、一覧が出す先頭行にも正しい値が載る
        # (`schema.sql` の説明)。BOX実績_* は取り込みを続ける ──
        # 梱包数の見積りと「BOX実績寸法への差し替え」が使っている
        ("BOX最終実績_設備名", "BOX最終実績_設備名", to_text),
        ("BOX最終実績_板厚", "BOX最終実績_板厚", to_real),
        ("BOX最終実績_板幅", "BOX最終実績_板幅", to_real),
        ("BOX最終実績_板丈", "BOX最終実績_板丈", to_real),
        ("BOX最終実績_枚本数", "BOX最終実績_枚本数", to_int),
        # 試験指示票(先行データ)の要否判定に使う(VBA `AdvanceCheck`)
        ("品質グレード_表面処理", "品質ｸﾞﾚｰﾄﾞ_表面処理", to_text),
    ],
    "仕掛引当": [
        ("ロット番号", "ﾛｯﾄ番号", to_text),
        ("受注番号", "受注番号", to_text),
        ("引当数量", "引当数量", to_real),
        ("引当調整NO", "引当調整NO", to_text),
        # 番号であって数値ではない。数値にすると指数表記になり読めなくなる
        ("引当番号", "引当番号", to_text),
    ],
    "仕掛受注": [
        ("受注番号", "受注番号", to_text),
        ("納入先名称", "納入先名称", to_text),
        ("包装仕様NO", "包装仕様NO", to_text),
        ("取引先名称", "取引先名称", to_text),
        ("送り先名称", "送り先名称", to_text),
        ("納送用コメント", "納送用ｺﾒﾝﾄ", to_text),
        ("工場用コメント", "工場用ｺﾒﾝﾄ", to_text),
        ("VC_表", "VC_表", to_text),
        ("VC_裏", "VC_裏", to_text),
        ("EX_輸出区分", "EX_輸出区分", to_text),
        ("材質_比重", "材質_比重", to_real),
        ("梱包単位_重量", "梱包単位_重量", to_real),
        ("梱包単位_枚数", "梱包単位_枚数", to_real),
    ],
}

# 仕掛台帳側のテーブル名はどのファイルでも「仕掛」
LOT_SOURCE_TABLE = "仕掛"

# 変換結果がNoneのときにNOT NULL列へ入れる既定値。
# SQLiteの DEFAULT は「列を省略したとき」にしか効かず、NULLを明示的に
# INSERT すると NOT NULL 制約で落ちるため、変換関数ごとに補う。
# (仕掛台帳の実データはBOX実績系が空欄の行が多い)
NULL_FALLBACKS: dict[object, object] = {to_int: 0, to_real: 0.0, to_text: ""}

