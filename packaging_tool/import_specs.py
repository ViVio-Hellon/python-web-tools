"""取り込み定義(列の対応と型変換)

取り込み元のどの列を、手元のどの列に、どの型で入れるかの一覧。
取り込みスクリプト(`scripts/import_source.py`)と、アプリ内から実行する
取り込み(`data_sync`)の**両方がここを見る**ので、対応表は1か所だけ。

管理番号はSQLite側でAUTOINCREMENTに採番し直す(元IDを参照している
他テーブルは無い=外部キー制約が無いことをmdb-schemaで確認済み)。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from . import config, db


def _text(value: Any) -> str:
    """どんな値でも文字列にそろえる。

    取り込み元が sqlite3 になって、値は**型が付いたまま**返るように
    なりました(以前の mdb-export は何でも文字列でした)。数値に
    `.strip()` を呼んで落ちないよう、入口でここを通します。
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return str(value).strip()


def to_int(value: Any) -> Optional[int]:
    text = _text(value)
    if text == "":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def to_real(value: Any) -> Optional[float]:
    text = _text(value)
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


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
OPTIONAL_TABLES: frozenset[str] = frozenset({"アクセス権限"})

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
# SIKALOTNOW / SIKAHIKINOW / SIKAODRNOW の3ファイル(いずれもテーブル名は
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

