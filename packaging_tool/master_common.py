"""マスタ管理の決まりごと ── どの表を、誰が、どこで直せるか

**直せる表だけを直す。** 何を直せるか(`MANAGED`)、直せない表はなぜか
(`VIEW_ONLY_WHY`)、誰が直せるか(`can_edit`)、その表はどのファイルに
あるか(`source_for`)を、ここ1か所で決める。

**書き先は取り込み元だけ。** 手元のDBは総入れ替えで取り込まれるので、
手元を直しても次の取り込みで消える。書いたあとにその表だけ取り込み
直す(`_follow`)ところまでが1つの操作。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# **他の段は名前ではなくモジュールで呼ぶ。** 差し替え(試験の stub)の
# 当て先が持ち主の1か所で済む(`data_sync` を分けたときの教訓)
from . import config, data_sync, db, import_specs, source_db
from .logging_utils import get_logger

log = get_logger("master_admin.common")

# 一度に出す行数。**全部は出さない。**
# 注文管理は数千行あり、全部描いても読めないし、共有から引くだけで待つ。
# 出さなかった分は必ず数で言う(`Page.note`)── 黙って切ると
# 「これで全部だ」と読めてしまう
ROW_LIMIT = 200

# 空欄なら今の日時を入れる列。手で打たせる意味が無く、打ち間違いだけが増える
STAMP_COLUMNS = ("更新日時", "登録日時")


# 行を指す隠しの列名。取り込み元の `rowid` をこの名前で持ち回る。
# **業務の列と衝突しない名前**にする(全角を混ぜてあるのはそのため)
ROW_KEY = "__行"


# ==================================================================
# どの表を扱うか
# ==================================================================
@dataclass(frozen=True)
class Managed:
    """直せる表1つ。"""

    table: str
    label: str
    mark: str
    note: str


# **直せるのは資材課が持っている表だけ。**
# 並びは触る頻度の順(ヒックの法則)。パレットとボードが大半を占める
MANAGED: tuple[Managed, ...] = (
    Managed(config.TBL_PALLET_MASTER, "パレット", "パ",
            "寸法・適合範囲・記号・保管位置・在庫数"),
    Managed(config.TBL_BOARD_MASTER, "ボード", "ボ",
            "ボードの幅丈と種別(ハードボード / IK / プロテック)"),
    Managed(config.TBL_CORNERBOARD_MASTER, "アングル", "ア",
            "アングルの丈"),
    Managed(config.TBL_HOGOZAI, "梱包保護材", "保",
            "板厚・板幅・板丈の範囲と、使う保護材"),
    Managed(config.TBL_MATSUZAI_KAKUZAI, "松板・角材", "松",
            "品名・厚・幅・丈の範囲と発注コード"),
    Managed("アクセス権限", "アクセス権限", "権",
            "誰がどのモードを使えるか"),
    # パレット適合閾値(タイトパレット選定基準表)。以前はプログラムに
    # 直接書かれていた数値で、基準が変わっても現場では直せなかった。
    # 触る頻度は低いので末尾に置く
    Managed("PalletDakeThreshold", "パレット閾値(丈)", "丈",
            "丈の帯ごとの、載せられる製品丈の範囲"),
    Managed("PalletHabaThreshold", "パレット閾値(幅)", "幅",
            "幅の帯ごとの、載せられる製品幅の範囲"),
    Managed("PalletAshiThreshold", "パレット閾値(脚数)", "脚",
            "丈の帯ごとの脚の本数"),
    Managed("PalletKetaThreshold", "パレット閾値(桁数)", "桁",
            "幅の帯ごとの桁(松板)の本数"),
    Managed("PalletSymbolMaster", "パレット記号", "記",
            "記号(C1/P1…)ごとの固定適合と桁数・脚数"),
    Managed("PalletIndustryMaster", "パレット業界", "業",
            "業界(1×2/4×8…)ごとの固定適合"),
    Managed("PalletComboMaster", "パレット業界×記号", "組",
            "業界と記号の組合せごとの固定適合(記号・業界より優先)"),
)
BY_TABLE: dict[str, Managed] = {m.table: m for m in MANAGED}

# 直せない表と、その理由。**出さないのではなく、理由を出す。**
# 「なぜこの表だけ直せないのか」が分からないと、画面が壊れて見える
VIEW_ONLY_WHY: dict[str, str] = {
    config.TBL_PALLET_PATTERN:
        "実績から貯まる表です。資材選択で「この配置を覚える」を押すと増えます。",
    config.TBL_WAREHOUSE_ORDER:
        "このツールが書き戻す表です。発注の取り消しは倉庫連携の画面から行います。",
    config.TBL_STOCK_HISTORY:
        "このツールが書き戻す表です。受け払いは簡易在庫の画面から行います。",
    "Form状態管理": "上流の設備が書く表です。",
}
DEFAULT_VIEW_ONLY = "このツールが直す表ではありません。中身の確認だけできます。"


# 「表を持ってくる」(`table_bring`)で足した表の記録。**梱包資材マスタの中に
# 置く** ── どの端末のマスタ管理からも「足した表」として直せるようにするため
# (端末ごとの設定に置くと、足した端末でしか直せない)
BROUGHT_REGISTRY = "ツールで足した表"


def brought_tables(path: Optional[Path]) -> frozenset[str]:
    """「表を持ってくる」で足した表。マスタ管理で直せる。"""
    if path is None:
        return frozenset()
    try:
        rows = source_db.read_query(
            path, f"SELECT 表 FROM {source_db.quote_identifier(BROUGHT_REGISTRY)}")
    except source_db.SourceError:
        return frozenset()                      # まだ1つも足していない
    return frozenset(str(r["表"]) for r in rows if r.get("表"))


def view_only_why(table: str) -> str:
    """その表を直せない理由。直せる表なら空。"""
    if table in BY_TABLE:
        return ""
    if table in brought_tables(data_sync.find_material_db()):
        return ""
    return VIEW_ONLY_WHY.get(table, DEFAULT_VIEW_ONLY)

# ==================================================================
# 直せるかどうか
# ==================================================================
# 断りの種類。**文言から推し量らない**(設計.md §1)
REFUSE_NOT_ALLOWED = "not_allowed"      # 権限が無い
REFUSE_NOT_EDITABLE = "not_editable"    # この表は直す表ではない
REFUSE_BAD_VALUE = "bad_value"          # 入れた値の形が違う
REFUSE_NO_SOURCE = "no_source"          # 取り込み元に届かない
REFUSE_NO_ROW = "no_row"                # その行がもう無い
REFUSE_WRITE_FAILED = "write_failed"    # 書けなかった
REFUSE_NOT_CREATABLE = "not_creatable"  # この表は作る表ではない
REFUSE_ALREADY = "already"              # もうある


# ==================================================================
# どのファイルにある表か
# ==================================================================
def source_for(table: str) -> Optional[Path]:
    """その表がどのファイルにあるか。**表ごとに置き場所が違う。**

    以前は全部が梱包資材マスタ1つにあった。いまは移植元(VBA)と同じ形で
    別ファイルに分かれているものがある ── 現場の写しがその形で配られて
    いて、梱包資材マスタの中には無いため(現場の指摘:「取り込み元に
    パレット閾値の条件がないのでテーブルを読み込めていない」)。

        パレット適合閾値の7表 … PalletThresholdMaster.sqlite3
        それ以外              … 梱包資材マスタ.sqlite3

    **判断はここ1つ。** 画面も保存も取り込み直しも同じ答えを使う。
    分かれていると、読めた表に書けない(あるいはその逆)が起きる。
    """
    if table in import_specs.THRESHOLD_TABLES:
        return data_sync.find_threshold_db()
    return data_sync.find_material_db()


def source_label(table: str) -> str:
    """その表の取り込み元の呼び名。見つからないときの案内に使う。"""
    if table in import_specs.THRESHOLD_TABLES:
        return f"パレット閾値マスタ({config.THRESHOLD_DB_NAME})"
    return f"梱包資材マスタ({config.MATERIAL_DB_NAME})"


def source_dir(table: str) -> Path:
    if table in import_specs.THRESHOLD_TABLES:
        return config.threshold_db_dir()
    return config.master_db_dir()


# ==================================================================
# 取り込み元に作ってよい表
# ==================================================================
# **このツールが後から足した表だけ**を作る。
#
# 判断の出どころは `import_specs.OPTIONAL_TABLES` ── 「取り込み元に
# 無くても失敗にしない表」は、上流(資材課)が持っていない表、つまり
# このツールが持ち込んだ表だからです。同じ事実を2か所に持たないので、
# ここで別の一覧は作りません。
#
# 逆に PalletMaster のような上流の表は作りません。無いのは資材課側の
# 事情(ファイルが違う・移された)で、空の表を作ってしまうと**その事情が
# 「0件」という形に化けて**、原因を探せなくなります。
def creatable_tables() -> frozenset[str]:
    return frozenset(t for t in import_specs.OPTIONAL_TABLES if t in BY_TABLE)

def can_edit(conn: Optional[sqlite3.Connection], table: str = "") -> tuple[bool, str]:
    """この端末はマスタを直せるか。**直せないなら理由も返す。**

    権限で見る。いま開いているモードでは見ない ── 資材課の人が現場の
    画面を見ているあいだだけ直せなくなるのは、権限の分け方として
    説明が付かない。

    【mode:material だけでは、どの表も書けない】
    以前は mode:material を持つ端末なら無条件にどの表でも書けたが、
    「mode:material を付与してるからってどのマスタもいじれたら困る」
    という現場の判断で、**管理者パスワードを必ずもう一手間はさむ**
    ことにした。資材モードは「マスタを見に来てよい・触ってよい担当か」
    を分け、パスワードは「いま本当に書くつもりか」を確かめる ──
    役割が違う2つの関門を両方通す。
    パスワードの確認は設定画面「パスワード」タブの管理者認証
    (`selection_session`)をそのまま使う ── アプリの起動プロセスに
    1つなので、別の認証をもう1つ持たない。

    【アクセス権限マスタだけは、資材モードが無くても開く】
    書き間違えて全員から mode:material を消してしまうと、直せる人が
    どこにもいなくなる(現場の指摘: 「アクセス権の書き換えミスっちゃうと
    二度と書きかえれなくなっちゃう」)。この表(`access_control.TABLE`)
    だけは資材モードを問わず、パスワードだけで開く復旧経路にしてある。
    """
    from . import access_control, modes, selection_session

    if conn is None:
        return False, "手元のデータベースを開けませんでした。"
    grant = access_control.resolve(conn)
    if not grant.has_master:
        # まだ誰も登録されていない。ここを塞ぐと、資材モードに入るための
        # 最初の1行をどこからも入れられなくなる
        return True, ""

    authenticated = selection_session.get_session(conn).admin

    if table == access_control.TABLE:
        # 資材モードを問わない復旧経路。パスワードだけが関門
        if authenticated:
            return True, ""
        return False, (
            f"{access_control.TABLE} は書き間違えると誰も直せなくなるおそれが"
            "あるので、管理者パスワードを入れないと直せません"
            "(モードは問いません)。"
            "設定画面の「パスワード」タブ「マスタ編集の認証」でパスワードを入れてから、"
            "もう一度この面を開いてください。")

    if not grant.allows_mode(modes.MATERIAL):
        return False, (
            f"マスタを直せるのは{modes.label(modes.MATERIAL)}モードを持つ端末だけです。"
            f"{access_control.TABLE} に {grant.identity.label()} と "
            f"{access_control.mode_permission(modes.MATERIAL)} の行を足してください。"
            "(いまの権限は「いまの状態」の面で確かめられます)")
    if not authenticated:
        return False, (
            f"{modes.label(modes.MATERIAL)}モードに加えて、管理者パスワードを"
            "入れないと直せません。"
            "設定画面の「パスワード」タブ「マスタ編集の認証」でパスワードを入れてから、"
            "もう一度この面を開いてください。")
    return True, ""

@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""

def _write_failed(table: str, exc: Exception) -> Result:
    log.warning("%s へ書けませんでした: %s", table, exc)
    return Result(False, f"取り込み元へ書けませんでした: {exc}", REFUSE_WRITE_FAILED)


def _label(table: str) -> str:
    return table

def _follow(conn: sqlite3.Connection, path: Path, table: str) -> str:
    """書いた表だけを取り込み直して、手元を追いつかせる。

    ここを飛ばすと、取り込み元は直っているのに画面の動きは変わらない
    ── **直したのに効かない**が一番たちが悪い。
    """
    if table not in import_specs.IMPORT_SPECS:
        # 持ってきた表など、このツールが手元へ取り込まない表。取り込み元に
        # 書けたらそれで終わり(手元に写しは無い)
        return "。"
    try:
        result = data_sync.import_tables(
            conn, path, {table: import_specs.IMPORT_SPECS[table]},
            required=import_specs.REQUIRED_KEY_COLUMNS,
            blank_is_missing=import_specs.BLANK_IS_MISSING,
            optional=import_specs.OPTIONAL_TABLES,
            fallbacks=import_specs.NULL_FALLBACKS)
    except sqlite3.Error as exc:                 # pragma: no cover - 上流で拾う
        log.warning("%s を取り込み直せません: %s", table, exc)
        return "。ただし手元に取り込めなかったので「まとめて取り込み」を押してください"

    # **入ったかどうかは `imported` で見る。** `errors` には「入ったが
    # 言っておくべきこと」(元に無い列があった等)も混ざるので、これを
    # 失敗として読むと、直せているのに「失敗しました」と出る
    if table not in result.imported:
        return "。ただし手元に取り込めませんでした: " + " / ".join(result.errors)

    notes = [f"手元も{result.imported[table]}件に更新しました"]
    if table in import_specs.THRESHOLD_TABLES:
        # **閾値を直しただけでは、パレットの適合範囲は変わりません。**
        # 適合範囲はマスタの列に書いてあり、閾値から計算し直して初めて
        # 入ります。ここを飛ばすと「基準表は直したのに、選定の結果が
        # 変わらない」になる(PalletMaster を直したときと同じ理由)
        from . import pallet_service
        summary = pallet_service.recompute_fit_ranges(conn)
        notes.append("適合範囲も計算し直しました" if summary.ok
                     else f"ただし適合範囲の再計算に失敗しました({summary.error})")
    if table == config.TBL_PALLET_MASTER:
        # 寸法を直したら適合範囲も変わる。取り込みと同じ後始末をする
        # (`data_sync.import_master` と同じ理由)
        from . import pallet_service
        summary = pallet_service.recompute_fit_ranges(conn)
        notes.append("適合範囲も計算し直しました" if summary.ok
                     else f"ただし適合範囲の再計算に失敗しました({summary.error})")
    if table == "アクセス権限":
        # **足した権限は、開き直すまで全部は効かない。** 使える画面は
        # 起動時の権限で決まっているので、切り替えは通っても画面が出ない
        notes.append("権限を変えたので、アプリを開き直すと全部が効きます")
    if result.errors:
        # 取り込みが言いたいこと。**黙って捨てない**
        notes.append("なお " + " / ".join(result.errors))
    return "。" + "、".join(notes) + "。"
