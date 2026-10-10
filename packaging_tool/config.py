"""アプリ設定・定数

VBA版の `UFdaily` 標準モジュール冒頭にあった定数群のうち、
Python/SQLite版で意味を持つものを移植する。

【移植方針】
- VBA版はDBファイル(.accdb)を社内ネットワーク共有(UNC path)上に置き、
  複数端末からADO経由で直接開いていた。Python/SQLite版では環境依存の
  実パスをソースに埋め込まず、`PACKAGING_TOOL_DB_PATH` 環境変数 or
  既定のローカルパス(./data/packaging_tool.db)を使う。
  社内共有フォルダに置く場合は環境変数でパスを差し替えること。
- レジストリ(GetSetting/SaveSetting)によるライン名等の永続化は、
  ローカル設定ファイル(JSON)に置き換える。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import app_config

APP_NAME = "PackagingTool"

# ------------------------------------------------------------------
# パス設定
# ------------------------------------------------------------------
# プロジェクトルート (このファイルの2つ上の階層)
BASE_DIR = Path(__file__).resolve().parent.parent

# SQLite DBファイルパス。環境変数で共有フォルダ上のパスに差し替え可能。
DB_PATH = Path(os.environ.get("PACKAGING_TOOL_DB_PATH", str(BASE_DIR / "data" / "packaging_tool.db")))

# ログ出力先ディレクトリ。
#
# 既定は**利用者ごとのローカル領域**(Windows なら
# `%LOCALAPPDATA%\PackagingTool\logs`)。基盤仕様書 2.7 が、
# 共有フォルダーやクラウド同期領域で起きる速度低下・同期競合・
# アクセス権の問題を避けるため、実行中に変化するファイルを
# アプリ本体から分けることを求めているため。
#
# アプリ本体を共有フォルダに置いて複数の端末から使う運用では、
# ログを本体側に書くと端末どうしで混ざる。分けておけば混ざらない。
#
# 環境変数 `PACKAGING_TOOL_LOG_DIR` で従来どおりの場所にも戻せる。
LOG_DIR = Path(os.environ.get("PACKAGING_TOOL_LOG_DIR",
                              str(app_config.local_dir("logs"))))
# 環境変数で決めたときは、設定画面の値より**環境変数が勝つ**(試験・診断用)
LOG_DIR_FROM_ENV = "PACKAGING_TOOL_LOG_DIR" in os.environ

# 設定画面で指定したログの出力先のうち、**書けることを確かめたもの**。
# 決めるのは `trace_log.apply_log_dir()` だけ。ログを書くたびに設定
# ファイルを読みに行かないよう(読むとそれ自体がログを書く)、ここに置く
_active_log_dir: Optional[Path] = None


def log_dir() -> Path:
    """いまログを書いているフォルダ。設定画面で指定があればそちら。"""
    return _active_log_dir or LOG_DIR

# ローカル設定ファイル(レジストリ代替: ライン選択などの永続化に使用)
USER_CONFIG_PATH = Path(os.environ.get("PACKAGING_TOOL_CONFIG_PATH", str(BASE_DIR / "data" / "user_config.json")))

# ------------------------------------------------------------------
# 取り込み元(sqlite3)の置き場所
# ------------------------------------------------------------------
# 梱包資材マスタを探すフォルダ。
#
# 【複数ラインで使う前提】ラインごとに端末があるので、取り込み元は
# **共有フォルダに1つ置いて全ラインがそれを見る**。フォルダは設定画面で
# 変えられ(`user_settings` に保存)、環境変数でも指定できる。
# どちらも無ければ起動フォルダを見る(1台で試すとき用)。
#
# 実際に使うパスは `master_db_dir()` で取る。ここは「設定が無いときの既定」。
DEFAULT_MASTER_DB_DIR = Path(os.environ.get(
    "PACKAGING_TOOL_MASTER_DB_DIR",
    os.environ.get(
        "PACKAGING_TOOL_ACCDB_DIR",     # 旧名。既存の設定を切らさない
        r"\\nlmfangyshrd\各課共有\0130_日軽稲沢\梱包課\AIM\【■】_参照用ファイル")))

# 梱包資材マスタのファイル名(VBA `DB_NAME`)。
# 見つからない場合はフォルダ内の sqlite3 を探して自動判別する。
MATERIAL_DB_NAME = os.environ.get("PACKAGING_TOOL_MATERIAL_DB",
                                  "梱包資材マスタ.sqlite3")

# 看板(在庫薄警告)マスタの置き場所。
#
# Form状態管理・看板_* の8テーブルは、以前は梱包資材マスタ.sqlite3の
# 一部として取り込む定義になっていたが、実際にはそのファイルに
# 一度も入っておらず、取り込みのたびに「取り込めませんでした」に
# 数えられていた(`import_specs.KANBAN_TABLES` を参照)。
# 現場から渡された実データは**別ファイル**(看板マスタ.sqlite3)に
# 入っていたので、そちらを見に行くように分ける。
#
# 既定値は梱包資材マスタと**同じ共有フォルダ**にしてある(現場の写しが
# 同じ場所に置かれていたため)。設定画面では独立した欄で変えられる。
DEFAULT_KANBAN_DB_DIR = Path(os.environ.get(
    "PACKAGING_TOOL_KANBAN_DB_DIR", str(DEFAULT_MASTER_DB_DIR)))

# 看板マスタのファイル名。見つからなければフォルダ内の自動判別はしない
# (梱包資材マスタと同じフォルダに置かれることがあるため、名前の緩い
# 一致で探すと梱包資材マスタ自身を誤って拾いかねない)。
KANBAN_DB_NAME = os.environ.get("PACKAGING_TOOL_KANBAN_DB",
                                "看板マスタ.sqlite3")

# パレット適合閾値マスタの置き場所。
#
# 移植元(VBA)は閾値だけを別ファイル(PalletThresholdMaster.accdb)に
# 持っていて、現場の写しもその形で配られている。当初は梱包資材マスタに
# 同居させる前提で作ったが、**取り込み元にその7表が無い**ので読み込め
# なかった(現場の指摘:「取り込み元にパレット閾値の条件がないので
# テーブルを読み込めていない」)。看板マスタと同じく、別ファイルとして
# 見に行く。
#
# 既定は梱包資材マスタと同じ共有フォルダ。設定画面の独立した欄で
# 変えられる。
DEFAULT_THRESHOLD_DB_DIR = Path(os.environ.get(
    "PACKAGING_TOOL_THRESHOLD_DB_DIR", str(DEFAULT_MASTER_DB_DIR)))

# 閾値マスタのファイル名。看板マスタと同じ理由で、見つからなくても
# フォルダ内の自動判別はしない(梱包資材マスタ自身を誤って拾わない)
THRESHOLD_DB_NAME = os.environ.get("PACKAGING_TOOL_THRESHOLD_DB",
                                   "PalletThresholdMaster.sqlite3")

# CSVなどの書き出し先。**読む場所ではなく書く場所**なので、既定は
# 共有ではなくこのツールのフォルダの下にする ── 共有に届かない端末でも
# 書き出しそのものは必ず成功させる。
DEFAULT_EXPORT_DIR = Path(os.environ.get(
    "PACKAGING_TOOL_EXPORT_DIR", str(BASE_DIR / "export")))

# 仕掛台帳(ロット検索用)の置き場所。
#   \\nlmsrvngy03\Read\【New】仕掛\台帳\
# 共有に届かない端末では、設定画面か環境変数でローカルのコピー先を指定する。
LOT_DB_DIR = Path(os.environ.get(
    "PACKAGING_TOOL_LOT_DB_DIR", r"\\nlmsrvngy03\Read\【New】仕掛\台帳"))

# 仕掛台帳の3ファイル(手元のテーブル名 → 取り込み元のファイル名)。
# 拡張子違い(.db)も `source_db.find` が拾う
LOT_DB_FILES = {
    "仕掛ロット": "SIKALOT.sqlite3",
    "仕掛引当": "SIKAHIKI.sqlite3",
    "仕掛受注": "SIKAODR.sqlite3",
}


def resolve_dir(text: str) -> Path:
    r"""打たれた道を、**実際に見に行く道**にする。

    2通りの書き方を受けます。

        絶対  `\\サーバ\共有\…` / `C:\data\…` / `/mnt/share/…`
              打たれたまま使います
        相対  `data\src` / `..\共有` / `src`
              **アプリのフォルダから**たどります(`config.BASE_DIR`)

    相対を「いまの作業フォルダ」から見ないのが要点です。作業フォルダは
    どこから起動したかで変わるので、同じ設定でも端末ごとに違う場所を
    指すことになります。アプリのフォルダなら、フォルダごとコピーして
    配る運用でも、写しの中の同じ場所を指し続けます。

    (`~` は利用者のフォルダに開きます。共有に届かない端末で、手元の
     写しを指すのに使えます)
    """
    trimmed = (text or "").strip().strip('"')
    if not trimmed:
        raise ValueError("道が空です")
    path = Path(trimmed).expanduser()
    if path.is_absolute():
        return path
    return BASE_DIR / path


def is_relative_setting(text: str) -> bool:
    """その書き方は相対か。画面に「どちらとして読んだか」を出すため。"""
    trimmed = (text or "").strip().strip('"')
    if not trimmed:
        return False
    return not Path(trimmed).expanduser().is_absolute()


def master_db_dir() -> Path:
    """いま使う梱包資材マスタのフォルダ。設定画面の値を優先する。

    `user_settings` を遅延importするのは、`config` を先に読む
    モジュールとの循環参照を避けるため。
    """
    from . import user_settings
    for key in (KEY_MASTER_DB_DIR, KEY_ACCDB_DIR_LEGACY):
        configured = user_settings.get(key)
        if isinstance(configured, str) and configured.strip():
            return resolve_dir(configured)
    return DEFAULT_MASTER_DB_DIR


def lot_db_dir() -> Path:
    """仕掛台帳のフォルダ。設定が無ければ既定の共有パス。"""
    from . import user_settings
    configured = user_settings.get(KEY_LOT_DB_DIR)
    if isinstance(configured, str) and configured.strip():
        return resolve_dir(configured)
    return LOT_DB_DIR


def lot_db_dir2() -> Optional[Path]:
    """仕掛台帳の**2つ目の**フォルダ。設定していなければ None(既定は無い)。

    1つ目で見つからないときに見る(現場の声:「仕掛DBの参照パスを2つセットできるように。
    1つ目でファイルが無い、またはファイルはあるが対象が見つからない → 2つ目」)。
        ファイルが無い         … そのファイルは2つ目から読む(`sync_sources.find_lot_dbs`)
        ファイルに対象が無い   … 取り込みのあとに、2つ目にだけあるロット・受注を足す
                                 (`sync_import.merge_second_lot`)
        ロットはあるが値が空   … BOX最終実績の寸法が空か BOX最終実績_設備名 が HOT なら、2つ目の同じ
                                 ロットの行の BOX設計寸法を候補に控え、ロット情報の画面で選ぶ
                                 (`sync_import.collect_box_choices`)
    2つ目の SIKALOT は圧縮版で、設計_設備ｺｰｽ・実績_設備ｺｰｽ などが無い。2つ目から入ったロットは
    BOX かどうか決められないので、製造寸法を出して画面で断る(`sync_import.COURSE_UNKNOWN_TABLE`)
    """
    from . import user_settings
    configured = user_settings.get(KEY_LOT_DB_DIR2)
    if isinstance(configured, str) and configured.strip():
        return resolve_dir(configured)
    return None


def lot_db2_files() -> dict[str, str]:
    """仕掛台帳の**2つ目の**ファイル名(手元のテーブル名 → ファイル名)。

    現場の依頼:「2つ目はファイル名も指定できるように。SIKALOT は名前が同じでも圧縮版だったように、
    混乱を招く。名前を変えておかないと持ち運びもしにくい」。**1つ目は LOT_DB_FILES のまま**。
    条件(現場の約束): 名前は変えても、中身の要る列は必ずそろえる(設定画面の「いまの状態」で確かめる)。
    設定していない表は1つ目と同じ名前(今までどおり)。
    """
    from . import user_settings
    configured = user_settings.get(KEY_LOT_DB2_FILES)
    names = dict(LOT_DB_FILES)
    if isinstance(configured, dict):
        for table in LOT_DB_FILES:
            value = configured.get(table)
            if isinstance(value, str) and value.strip():
                names[table] = value.strip()
    return names


def lot_db_dirs() -> list[Path]:
    """仕掛台帳を探すフォルダ(1つ目 → 2つ目)。同じフォルダは2度並べない。"""
    first = lot_db_dir()
    second = lot_db_dir2()
    return [first] + ([second] if second is not None and second != first else [])


def kanban_db_dir() -> Path:
    """いま使う看板マスタのフォルダ。設定画面の値を優先する。"""
    from . import user_settings
    configured = user_settings.get(KEY_KANBAN_DB_DIR)
    if isinstance(configured, str) and configured.strip():
        return resolve_dir(configured)
    return DEFAULT_KANBAN_DB_DIR


def export_dir() -> Path:
    """CSVの書き出し先。設定画面の値を優先する。

    既定を**このツールのフォルダの下**にしてあるのは、共有に届かない
    端末でも必ず書けるからです。共有に置きたい人は設定で指すだけ。
    取り込み元(読む場所)と違い、書けないと操作そのものが失敗します。
    """
    from . import user_settings
    configured = user_settings.get(KEY_EXPORT_DIR)
    if isinstance(configured, str) and configured.strip():
        return resolve_dir(configured)
    return DEFAULT_EXPORT_DIR


def threshold_db_dir() -> Path:
    """いま使うパレット閾値マスタのフォルダ。設定画面の値を優先する。"""
    from . import user_settings
    configured = user_settings.get(KEY_THRESHOLD_DB_DIR)
    if isinstance(configured, str) and configured.strip():
        return resolve_dir(configured)
    return DEFAULT_THRESHOLD_DB_DIR


# `user_settings` に入れるキー
KEY_MASTER_DB_DIR = "master_db_dir"
# 旧名(Accessだったころ)。**読むだけ**残す ── 設定済みの端末が
# 更新の日に置き場所を見失わないように
KEY_ACCDB_DIR_LEGACY = "accdb_dir"
KEY_LOT_DB_DIR = "lot_db_dir"
# 仕掛台帳の2つ目の置き場所(1つ目で見つからないときに見る。`lot_db_dir2`)
KEY_LOT_DB_DIR2 = "lot_db_dir2"
# 仕掛台帳の2つ目のファイル名({手元のテーブル名: ファイル名}。`lot_db2_files`)
KEY_LOT_DB2_FILES = "lot_db2_files"
KEY_KANBAN_DB_DIR = "kanban_db_dir"
KEY_THRESHOLD_DB_DIR = "threshold_db_dir"
KEY_EXPORT_DIR = "export_dir"
# 「表を持ってくる」で Access を変換するツール(accdb_converter)のフォルダ
KEY_CONVERTER_DIR = "converter_dir"
KEY_AUTO_IMPORT = "auto_import_on_start"
# ログの出力先(空なら既定 = この端末のローカル領域)。**この端末だけ**の設定
KEY_LOG_DIR = "log_dir"

# 包装仕様書の図面を返すURLのひな形(`{no}` が包装仕様NOに置き換わる)。
# 既定は空 ── 社内の閲覧システムのエンドポイントはこのリポジトリからは
# 分からず、推測を埋め込むと間違った先を叩き続けることになる。
# 詳しくは `spec_sheet` モジュールの説明を参照
KEY_SPEC_SHEET_URL = "spec_sheet_url"

# 起動時の自動取り込みを既定でONにするか。
# 元ファイルが更新されているときだけ取り込むので、毎回全部読み直しはしない。
AUTO_IMPORT_DEFAULT = True

# 管理者パスワード(VBA `MaterialMasterForm` の Private Const ADMIN_PASSWORD)
#
# 【注意】これは暗号化もハッシュ化もされない、実績パターンの保存ボタンを
# 誤って押されないようにするためのUIガードにすぎない(VBA版も同じ)。
# 本来の意味でのアクセス制御ではないため、ソースに平文で残さず
# 環境変数 PACKAGING_TOOL_ADMIN_PASSWORD で上書きすることを推奨する。
ADMIN_PASSWORD = os.environ.get("PACKAGING_TOOL_ADMIN_PASSWORD", "nisk")

# ------------------------------------------------------------------
# テーブル名 (VBA版 Public Const TBL_* の移植)
# ------------------------------------------------------------------
TBL_PALLET_MASTER = "PalletMaster"
TBL_BOARD_MASTER = "BoardMaster"
TBL_CORNERBOARD_MASTER = "CornerboardMaster"
TBL_PALLET_PATTERN = "PalletPatterns"
TBL_WAREHOUSE_ORDER = "資材パレット注文管理"
TBL_HOGOZAI = "梱包保護材"
TBL_MATSUZAI_KAKUZAI = "松板角材"
TABLE_NAME_STATE = "Form状態管理"
TBL_STOCK_HISTORY = "パレット入出庫履歴"  # UFMAP `TBL_HISTORY` 相当(受入/払出の履歴)

# 実績(スナップショット保存)。VBA `modPatternStore` の TBL_PT_* / PT_FORMAT_VER。
#
# **VBA と同じ値にする**(取り込み元の同じ表を VBA と Python の両方が読み書き
# するため)。表の名前はここ以外に書かないこと ── 手元の表は `schema.sql`
# ではなく `pattern_store.ensure_tables` がここから作る。
TBL_PT_HEADER = "パレット実績ヘッダ"
# ボード使用実績(資材選択の「使用する」)。**全端末ぶんを共有に集める**
# (書き戻しで送り、取り込みで受け取る)。人気度は全端末の合計になる
TBL_BOARD_USAGE = "ボード使用実績"
# 発注ごとの現場⇔倉庫のやり取り。**全端末で共有**(書き戻しで送り、取り込みで受け取る)
TBL_ORDER_COMMENT = "発注コメント"
# どのコメントをこの端末で読んだか。**端末ごと**(共有へは送らない)
TBL_ORDER_COMMENT_READ = "発注コメント既読"
# **相手がコメントを見たか。** 誰が(端末と現場/倉庫)・いつ見たかを残し、全端末で
# 共有する(書いた人の画面に「倉庫 ○○ が見ました」を出すため)。上の既読とは別物 ──
# あちらは「この端末で開いたか」(未読の印)、こちらは「相手に届いて読まれたか」
TBL_ORDER_COMMENT_SEEN = "発注コメント閲覧"
# **切断依頼を倉庫へ送る**(`cut_requests.py`)。送った紙面と、状態(受け取った・切った・
# 取り消し)の記録。どちらも足すだけで、全端末で共有する
TBL_CUT_REQUEST = "切断依頼"
TBL_CUT_REQUEST_EVENT = "切断依頼状態"
TBL_PT_SELECT = "パレット実績選定"
TBL_PT_PLACE = "パレット実績配置"
TBL_PT_CUT = "パレット実績カット"
# 保存形式(VBA: 列構成を変えたら上げる)。**形が違う実績は読まない・出さない**
PT_FORMAT_VER = 2

# 看板(在庫薄警告)テーブル一覧。VBA `BoardKanbanTables()` の移植。
BOARD_KANBAN_TABLES = (
    "看板_大板小板", "看板_AIM", "看板_HVC", "看板_LVC", "看板_L1", "看板_コイル",
)

# ------------------------------------------------------------------
# 業務定数
# ------------------------------------------------------------------
# 検索の「±50」レンジモード(UFMAP)で使う許容差(mm)
SEARCH_RANGE_TOLERANCE = 50

# DB書き込みリトライ設定 (ExecuteSQLWithRetry / adoSQL 相当)
MAX_RETRY = 3
RETRY_BASE_WAIT_SEC = 1.0  # VBA: Sleep 1000 * retryCount (ミリ秒) を秒に換算


def ensure_dirs() -> None:
    """DB/ログ/設定ファイル用のディレクトリが無ければ作成する。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
