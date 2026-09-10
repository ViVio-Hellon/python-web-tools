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


def kanban_db_dir() -> Path:
    """いま使う看板マスタのフォルダ。設定画面の値を優先する。"""
    from . import user_settings
    configured = user_settings.get(KEY_KANBAN_DB_DIR)
    if isinstance(configured, str) and configured.strip():
        return resolve_dir(configured)
    return DEFAULT_KANBAN_DB_DIR


# `user_settings` に入れるキー
KEY_MASTER_DB_DIR = "master_db_dir"
# 旧名(Accessだったころ)。**読むだけ**残す ── 設定済みの端末が
# 更新の日に置き場所を見失わないように
KEY_ACCDB_DIR_LEGACY = "accdb_dir"
KEY_LOT_DB_DIR = "lot_db_dir"
KEY_KANBAN_DB_DIR = "kanban_db_dir"
KEY_AUTO_IMPORT = "auto_import_on_start"

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
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
