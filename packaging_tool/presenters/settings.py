"""設定画面の表示内容

**この端末の設定はここに集める。** 以前は置き場所が「データ」画面、
拠点が資材選択と棚検索の中、と散っていた。どこで何が変えられるのかを
探すこと自体が手間で、変えたつもりで変わっていない事故も起きる。

【状態は1項目ずつ良し悪しを付けて返す】
取り込みがうまくいかない原因はほぼ決まっている ── ファイルが
見つからない / テーブル名が違う / 中身が sqlite3 でない。
まとめて1つの文字列にすると「どこが問題なのか」が読み取れないので、
項目ごとに `level` を付ける。画面はそれを記号と色にするだけで、
**良し悪しの判断は持たない**。

【直し方まで書く】
「見つかりません」で終わらせず、`detail` に探した場所や次の一手を書く。
現場が自分で直せなければ、結局こちらに聞くことになる。
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import (admin_password, config, data_sync, floor_plan, jobs, location_service,
                lot_browse_session, lot_query, source_db, spec_sheet,
                user_settings)
from ..bounded import wait_seconds as _wait_seconds
from ..logging_utils import get_logger

log = get_logger("presenters.settings")

# 状態の重さ。画面はこの値で色と記号を決める
OK = "ok"          # 問題なし
WARN = "warn"      # 動くが、このままだと困ることがある
NG = "ng"          # このままでは進めない
INFO = "info"      # 良し悪しではない事実

# 「書き方のゆれ」を見出しに並べる件数。**全部は並べない**が、
# 数は必ず言う(切ったことを黙らない)
LOOSE_SAMPLE = 3
# 発注コードの重複の例に出す組の数(危ない種類から)
DUPE_SAMPLE = 3

# 見出しに出す言葉。**色だけで良し悪しを伝えない**(§3.8)。
# 工場は照明も見え方もまちまちで、色覚特性のある人もいる
LEVEL_LABEL = {
    OK: "問題なし",
    WARN: "要確認",
    NG: "足りません",
    INFO: "情報",
}

# 画面の面(タブ)。**縦に積むとスクロールが要る**ので面で分ける。
#   key, 見出し, その面に載せるもの
#
# 並びは触る頻度の順(ヒックの法則)。毎日押す「取り込み」を先頭に、
# うまくいかないときだけ触るものを後ろに置く。
#
# **問題を隠さないこと**が条件。中身に「足りません」があるタブは、
# 開いていなくても見出しがそう言う(`tab_badges()`)── 隠したせいで
# 気づけなくなるなら、スクロールのほうがまだましになる。
TABS: tuple[tuple[str, str], ...] = (
    ("run", "取り込みと反映"),
    ("status", "いまの状態"),
    ("master", "マスタ管理"),
    ("source", "取り込み元"),
    ("behavior", "動作"),
    # パスワードは「動作」の中に混ぜない。**触る理由も、触る人も違う**
    # ── 拠点や自動取り込みは日々の振る舞い、こちらは権限の関門で、
    # 並べて置くと「動作の設定を保存」で一緒に変わるように見える
    ("password", "パスワード"),
    # 配る前に1回だけ触る面。**日々の設定とは別に置く**(触る人も時も違う)
    ("distribution", "配布設定"),
    ("filters", "よく使う条件"),
    # どのボードがよく使われているかを見る面。設定を**変える**面では
    # ないが、ボードマスタに何を載せておくかを決める材料なので置く。
    #
    # **「使用率」とは呼ばない。** 配置の段に出している「使用率」は
    # パレットをどれだけ覆えたか(被覆率)で、まったく別のもの。
    # 同じ語を2か所で違う意味に使うと、どちらの話か読めなくなる
    ("boards", "ボード人気度"),
    ("history", "最近の結果"),
    # エラーを後から追う面(エラー記録・ログの出力先)。現場の声:
    # 「エラー等の後追いが現状できない。なぜなぜで分析できるように」
    ("logs", "ログ"),
    # どのファイルが**このPCだけ**で、どれが**全PC共通**か(現場の声)。
    # 引き継ぎ・バックアップのときに見る面
    ("storage", "保存場所"),
)
TAB_KEYS = frozenset(key for key, _ in TABS)
# 最初に開く面。**毎日押すもの**を既定にする
DEFAULT_TAB = "run"

# 「いまの状態」の面に載せる節(`build()` が作る順)。この面だけは
# 複数の節を束ねるので、どの節がどの面かをここで決める
STATUS_TAB = "status"

# 取り込みの対象。画面のボタンとAPIの引数がずれないよう、ここで並べる
TARGETS: tuple[tuple[str, str, str], ...] = (
    ("all", "まとめて取り込み", "梱包資材マスタと仕掛台帳の両方を読み直します"),
    ("master", "マスタだけ", "梱包資材マスタ(パレット・ボード・アングル等)"),
    ("lot", "仕掛台帳だけ", "SIKALOT / SIKAHIKI / SIKAODR の3ファイル"),
)
TARGET_KEYS = frozenset(key for key, _, _ in TARGETS)

# 状態の表示名。ジョブの `state` から引く
JOB_STATE_LABEL = {
    jobs.STATE_RUNNING: "実行中",
    jobs.STATE_DONE: "完了",
    jobs.STATE_FAILED: "失敗",
    jobs.STATE_INTERRUPTED: "中断",
}

# 状態ピルの種別。**共通の語彙**(components.css の `.st--*`)へ寄せる。
# 画面ごとに別の名前を付けると、同じ状態が画面ごとに違う色になる
JOB_STATE_KIND = {
    jobs.STATE_RUNNING: "run",
    jobs.STATE_DONE: "ok",
    jobs.STATE_FAILED: "ng",
    jobs.STATE_INTERRUPTED: "warn",
}


@dataclass
class Check:
    """状態の1項目。"""

    label: str
    value: str
    level: str = INFO
    detail: str = ""       # 直し方まで書く。「見つかりません」で終わらせない


@dataclass
class Section:
    title: str
    checks: list[Check] = field(default_factory=list)
    # 見出しに置く1文字の印。**色を知覚できなくても字で分かる**ように、
    # 分類そのものを字にする(梱包資材マスタ=「材」など)
    mark: str = "・"
    # 直しに行く先。**問題を見せた場所から、直せる場所へ繋ぐ。**
    # 「見つかりません」と書いてある面には直す手立てが無いので、
    # 利用者はタブを探し回ることになる。(文言, 行き先の面)
    action: tuple[str, str] = ("", "")
    # 見出しに出す言葉の差し替え。**「要確認」だけでは何をすればよいか
    # 分からない**ので、次にすることが決まっているまとまりはそれを言う
    # (現場の指摘:「この端末の権限の 要確認 もちょっと意味が分からない」)。
    # 空なら段階の名前(足りません/要確認)をそのまま出す
    headline: str = ""

    @property
    def level(self) -> str:
        """このまとまりで一番重いもの。見出しに出す。"""
        for level in (NG, WARN, OK):
            if any(c.level == level for c in self.checks):
                return level
        return INFO

    @property
    def badge_tone(self) -> str:
        """印の色。**中身の一番重い状態を見出しが背負う**(タブと同じ規則)。"""
        return {NG: "ng", WARN: "ng", OK: "ok"}.get(self.level, "accent")

    def label(self) -> str:
        """見出しに出す言葉。差し替えが無ければ段階の名前。"""
        return self.headline or LEVEL_LABEL.get(self.level, self.level)


@dataclass
class SettingsViewModel:
    sections: list[Section] = field(default_factory=list)

    # --- 取り込み元の置き場所 ---
    # 打たれたまま(相対で書かれていれば相対のまま)
    master_dir: str = ""
    lot_dir: str = ""
    lot_dir2: str = ""             # 仕掛台帳の2つ目(空なら使わない)
    # 仕掛台帳の2つ目のファイル名({表: 名前}。設定していなければ1つ目と同じ名前)。
    # LS4LOT の2つ目の名前も入る(鍵 `config.LS4_KEY`。VER4.8.78)
    lot_files2: dict = field(default_factory=dict)
    # LS4LOT の1つ目(VER4.8.78)。仕掛台帳とは別の置き場所。空なら見ない
    ls4_dir: str = ""
    ls4_name: str = ""
    ls4_dir_real: str = ""
    kanban_dir: str = ""
    threshold_dir: str = ""
    export_dir: str = ""
    # 倉庫連携の発注CSVの出力先(空なら書き出し先と同じ)
    order_csv_dir: str = ""
    # 実際に見に行く道。**相対で書いたときに「どこを見ているか」を出す**
    master_dir_real: str = ""
    lot_dir_real: str = ""
    lot_dir2_real: str = ""
    kanban_dir_real: str = ""
    threshold_dir_real: str = ""
    export_dir_real: str = ""
    order_csv_dir_real: str = ""
    path_base: str = ""            # 相対の起点(アプリのフォルダ)

    # --- 動作 ---
    auto_import: bool = True
    position: str = ""
    positions: list[str] = field(default_factory=list)
    # 包装仕様書の図面を返すURLのひな形。空なら図面は出ない(機能を使わない)
    spec_sheet_url: str = ""
    spec_sheet_problem: str = ""
    # 管理者パスワードを**この端末で変えてあるか**。値そのものは出さない
    admin_custom: bool = False
    admin_min_length: int = 4
    # いま管理者認証が通っているか(マスタを直すのに要る。VER2.17.0)。
    # 認証そのものは資材選択画面にあったが、「いつもどこだっけ」という
    # 声を受けてここへ移した(`app/routes/selection.py` の
    # `/api/selection/auth` はそのまま使う ── プロセスに1つの状態なので
    # どちらの画面から認証しても同じ)
    admin_authenticated: bool = False

    # --- よく使う条件(ロット一覧) ---
    lot_filters: list[dict[str, Any]] = field(default_factory=list)

    # --- ボード人気度 ---
    # **ボード一覧を軸にした**割合。使っていない寸法も0%で並ぶ
    # (一覧に載っているのに使っていないものを見つけるのが目的)
    board_usage: dict[str, Any] = field(default_factory=dict)

    # 取り込みができない状態なら、ボタンを押させる前に理由を出す
    can_import: bool = True
    import_reason: str = ""
    # 共有フォルダの確認が、待つ上限までに終わらなかった(VER4.8.115)。画面は少しあとで
    # 引き直す(`shared_facts`)。そのあいだも、ほかの面(ログ・取り込み元・動作)は使える
    status_pending: bool = False

    @property
    def level(self) -> str:
        for level in (NG, WARN, OK):
            if any(s.level == level for s in self.sections):
                return level
        return INFO


# ------------------------------------------------------------------
# いまの状態
# ------------------------------------------------------------------
def build(conn=None, startup_modes=None, *, fresh: bool = False) -> SettingsViewModel:
    """設定と、見つかっているファイルから画面ぜんぶを組み立てる。

    `conn` を渡すと権限の節も出す。省略できるようにしてあるのは、
    DBを開けない状態でも設定画面だけは開けるようにするため ──
    開けなければ、置き場所を直す画面にも辿り着けない。

    `startup_modes` は**起動したときに使えたモード**。いま使えるモードと
    食い違っていたら、開き直すまで効かないことを言う(下記 `_access_section`)。

    **共有フォルダを見に行くところは背景で、待つのは上限まで**(VER4.8.115。`shared_facts`)。
    共有が遅いと設定画面そのものが開けず、ログも見られなかった。`fresh` は置き場所を
    変えた直後 ── 変える前に始まった確認の答えを使わない。
    """
    facts, pending = shared_facts(STATUS_WAIT_SEC, fresh=fresh)

    view = SettingsViewModel(
        master_dir=_typed(config.KEY_MASTER_DB_DIR, config.master_db_dir(),
                          config.KEY_ACCDB_DIR_LEGACY),
        lot_dir=_typed(config.KEY_LOT_DB_DIR, config.lot_db_dir()),
        lot_dir2=_lot_dir2_typed(),
        lot_files2=_second_names(),
        ls4_dir=_ls4_dir_typed(),
        ls4_name=_ls4_name_typed(),
        ls4_dir_real=str(config.ls4_db_dir() or ""),
        kanban_dir=_typed(config.KEY_KANBAN_DB_DIR, config.kanban_db_dir()),
        threshold_dir=_typed(config.KEY_THRESHOLD_DB_DIR,
                             config.threshold_db_dir()),
        export_dir=_typed(config.KEY_EXPORT_DIR, config.export_dir()),
        master_dir_real=str(config.master_db_dir()),
        lot_dir_real=str(config.lot_db_dir()),
        lot_dir2_real=str(config.lot_db_dir2() or ""),
        kanban_dir_real=str(config.kanban_db_dir()),
        threshold_dir_real=str(config.threshold_db_dir()),
        export_dir_real=str(config.export_dir()),
        order_csv_dir=_order_csv_typed(),
        order_csv_dir_real=str(config.order_csv_dir()),
        path_base=str(config.BASE_DIR),
        auto_import=bool(user_settings.get(config.KEY_AUTO_IMPORT,
                                           config.AUTO_IMPORT_DEFAULT)),
        # 未登録なら空(画面は「未登録」を出す)。既定の L1 を出すと、登録
        # していないのに L1 に決まっているように見える(配った直後の端末がこれ)
        # 前の版で保存した「L1」は「L-1」として出す(選択肢と合うように。VER4.8.87)
        position=user_settings.get_position(default=""),
        # 配置図の拠点 + マスタで足した拠点(BoardMaster のデータラベル「拠点〇〇」)
        positions=location_service.base_point_names(conn),
        spec_sheet_url=spec_sheet.url_template(),
        spec_sheet_problem=spec_sheet.template_problem(spec_sheet.url_template()),
        admin_custom=admin_password.is_custom(),
        admin_min_length=admin_password.MIN_LENGTH,
        admin_authenticated=_admin_authenticated(conn),
        lot_filters=_lot_filters(),
        board_usage=_board_usage(conn),
    )
    if facts is None:
        shared = []
    else:
        shared = [
            _material_section(facts.material, conn, found=facts.material_probe,
                              broken=facts.material_guard),
            facts.kanban_section,
            facts.threshold_section,
            facts.lot_section,
        ]
    view.status_pending = pending
    view.sections = [
        *([_waiting_section(facts)] if pending or facts is None else []),
        *shared,
        _access_section(conn, startup_modes),
        _terminal_section(),
    ]

    # **押せないのは、読む元が1つも無いときだけ。**
    # 一部が欠けているだけなら、残りは取り込める(欠けは「いまの状態」が
    # 節ごとに言う)。ここで止めると、届いているものまで入らなくなる。
    # まだ確かめ終わっていないうちは止めない(分からないのに「できません」と言わない)
    if facts is not None and facts.material is None and not facts.lots:
        view.can_import = False
        # **どこを探して何が無かったかを言う。** 「1つも見つかりません」
        # だけでは、置き場所が違うのか・名前が違うのか・共有に届いて
        # いないのかが分からず、直しようがない(現場の指摘:
        # 「取り込めているし取り込める状態でもできませんとでる」)
        view.import_reason = (
            "取り込み元のファイルが1つも見つかりません。探した場所は "
            f"{config.master_db_dir()}({config.MATERIAL_DB_NAME})と "
            f"{' / '.join(str(d) for d in config.lot_db_dirs())}"
            f"({' / '.join(config.LOT_DB_FILES.values())})です。"
            "「いまの状態」に節ごとの結果が出ています。")
    return view


# ------------------------------------------------------------------
# 共有フォルダを見に行くところ(VER4.8.115)── 背景の1本で、待つのは上限まで
# ------------------------------------------------------------------
# 現場の声:「一度でもフリーズすると起動しなおしても設定タブが開けなくなる。設定も開けなく
# なるのでログすらみれない」。設定画面は開くたびに、取り込み元のファイルを探し・開き(手元へ
# 写してから中を見る)・二重登録の防止まで確かめていた。共有が遅いと、それが終わるまで画面が
# 返らなかった。ここを背景の1本にし、画面は `STATUS_WAIT_SEC` だけ待つ。
#
#     間に合った     … いままでどおり
#     間に合わない   … 前に確かめた結果(無ければ「確かめています」の1枚)で出し、少しあとで
#                      画面が引き直す(`status_pending`)
#     走っている最中 … 2本目は始めない
STATUS_WAIT_SEC = _wait_seconds(2.0)
# まだ確かめ終わっていないとき、画面が引き直すまでの間(ミリ秒。画面側が使う)
STATUS_RETRY_MS = 3000


@dataclass
class SharedFacts:
    """共有フォルダを見に行って分かったこと。**手元のDBは使わない**(背景のスレッドで集める)。"""

    material: Optional[Path]
    material_probe: Optional["source_db.Probe"]
    material_guard: Optional[list[str]]
    lots: dict[str, Path]
    kanban_section: "Section"
    threshold_section: "Section"
    lot_section: "Section"
    at: float = 0.0
    # どの置き場所の設定で確かめたか(`_config_signature`)。設定が違えば使わない
    signature: tuple = ()


_status_flight = None
_last_facts: Optional[SharedFacts] = None


def _config_signature() -> tuple:
    """いまの置き場所の設定(どこを見るか)。**確かめた結果がどの設定のものか**を見分けるため。

    置き場所を保存した直後に確かめ直しが間に合わないと、前の置き場所で確かめた結果が
    出ていた(新しい置き場所にファイルがあるのに「1つも見つかりません」)。
    """
    try:
        return (str(config.master_db_dir()), tuple(str(d) for d in config.lot_db_dirs()),
                str(config.kanban_db_dir()), str(config.threshold_db_dir()),
                str(config.ls4_db_dir() or ""), tuple(sorted(_second_names().items())))
    except Exception:                               # noqa: BLE001 - 分からなければ毎回別物
        return (time.time(),)


def _gather_shared() -> SharedFacts:
    """背景の1本。取り込み元を探して、開いて、確かめる。"""
    global _last_facts
    signature = _config_signature()
    material = _find_material()
    probe = guard = None
    if material is not None:
        probe = source_db.probe(material)
        if probe.ok:
            guard = _guard_broken(material)
    lots = _find_lots()
    facts = SharedFacts(
        material=material, material_probe=probe, material_guard=guard, lots=lots,
        kanban_section=_kanban_section(_find_kanban()),
        threshold_section=_threshold_section(_find_threshold()),
        lot_section=_lot_section(lots), at=time.time(), signature=signature)
    _last_facts = facts
    return facts


def shared_facts(wait: float, *, fresh: bool = False) -> tuple[Optional[SharedFacts], bool]:
    """(分かったこと, まだ確かめている途中か)。待つのは `wait` 秒まで。

    間に合わなければ、前に確かめた結果を返す(1度も無ければ None)。**ほかの置き場所の設定で
    確かめた結果は返さない**(置き場所を保存した直後に、前の置き場所の答えを出さない)。
    """
    global _status_flight
    from ..bounded import Flight
    if _status_flight is None:
        _status_flight = Flight("設定画面の状態の確認", _gather_shared)
    if fresh and _status_flight.running():
        # 置き場所を変える前に始まった確認。終わるのを待ってから確かめ直す
        _status_flight.wait_idle(wait)
    signature = _config_signature()
    outcome = _status_flight.run(wait)
    if (outcome.done and outcome.value is not None
            and outcome.value.signature != signature):
        # 前の置き場所で始まった確認に乗った。いまの置き場所で確かめ直す
        outcome = _status_flight.run(wait)
    if outcome.error is not None:
        log.warning("設定画面の状態を確かめられませんでした: %s", outcome.error)
    if (outcome.done and outcome.value is not None
            and outcome.value.signature == signature):
        return outcome.value, False
    stale = _last_facts if _last_facts is not None and _last_facts.signature == signature else None
    return stale, True


def start_shared_check() -> None:
    """共有フォルダの確認を、**いま**背景で始める(走っていればそのまま)。

    設定画面は先に発注の取り込み直しを待つので、その間に確認も進めておく(待つ時間を重ねない)。
    """
    global _status_flight
    from ..bounded import Flight
    if _status_flight is None:
        _status_flight = Flight("設定画面の状態の確認", _gather_shared)
    _status_flight.start()


def forget_shared_facts() -> None:
    """試験用。前に確かめた結果を忘れる(走っている確認は終わるまで待つ)。"""
    global _last_facts
    if _status_flight is not None:
        _status_flight.wait_idle(10.0)
    _last_facts = None


def _waiting_section(facts: Optional[SharedFacts]) -> "Section":
    """確かめ終わっていないときの1枚。**ほかの面は使える**ことも言う。"""
    section = Section("共有フォルダの確認", mark="待")
    if facts is None:
        section.checks.append(Check(
            "取り込み元", "確かめています", INFO,
            "共有フォルダの応答を待っています。終わったらこの面を出し直します。"
            "そのあいだも、取り込み・取り込み元・動作・ログの面は使えます。"))
    else:
        section.checks.append(Check(
            "取り込み元", "確かめ直しています", INFO,
            f"共有フォルダの応答に時間がかかっているので、{time.strftime('%H:%M:%S', time.localtime(facts.at))} "
            "に確かめた結果を出しています。終わったらこの面を出し直します。"))
    return section


# 2つ目の名前の欄に並ぶもの(鍵 → 1つ目の名前)。仕掛台帳の3つと LS4LOT(VER4.8.78)
def _first_names() -> dict[str, str]:
    return {**config.LOT_DB_FILES, config.LS4_KEY: config.LS4_DB_NAME}


def _second_names() -> dict[str, str]:
    """いまの2つ目の名前(仕掛台帳の3つ + LS4LOT)。"""
    return {**config.lot_db2_files(), config.LS4_KEY: config.ls4_db2_name()}


_BAD_NAME_CHARS = '\\/:*?"<>|'


def _lot_files2_problem(sent: dict) -> str:
    """2つ目のファイル名の形の誤り。**名前だけ**を受ける(フォルダは「仕掛台帳(2つ目)」の欄で決める)。"""
    if not isinstance(sent, dict):
        return "仕掛台帳の2つ目のファイル名の送り方が正しくありません。"
    firsts = _first_names()
    for table, value in sent.items():
        if table not in firsts:
            return f"仕掛台帳の2つ目に、知らない表 {table} のファイル名が送られました。"
        text = str(value or "").strip()
        if any(ch in text for ch in _BAD_NAME_CHARS):
            return (f"{firsts[table]} の2つ目のファイル名「{text}」に、ファイル名に使えない"
                    "文字(\\ / : * ? \" < > |)があります。フォルダは「仕掛台帳(2つ目)」の欄に入れてください。")
    return ""


def _ls4_name_problem(sent: str) -> str:
    """LS4LOT(1つ目)のファイル名の形の誤り。**名前だけ**(フォルダは「LS4LOT」の欄)。"""
    text = str(sent or "").strip()
    if any(ch in text for ch in _BAD_NAME_CHARS):
        return (f"LS4LOT のファイル名「{text}」に、ファイル名に使えない文字(\\ / : * ? \" < > |)が"
                "あります。フォルダは「LS4LOT」の置き場所の欄に入れてください。")
    return ""


def _ls4_dir_typed() -> str:
    """LS4LOT の1つ目の置き場所(打たれたまま)。**既定は無い**ので、未設定なら空。"""
    value = user_settings.get(config.KEY_LS4_DB_DIR)
    return value.strip() if isinstance(value, str) else ""


def _ls4_name_typed() -> str:
    """LS4LOT の1つ目のファイル名(打たれたまま)。未設定なら空(画面は LS4LOT.sqlite3 を薄く出す)。"""
    value = user_settings.get(config.KEY_LS4_DB_NAME)
    return value.strip() if isinstance(value, str) else ""


def _lot_dir2_typed() -> str:
    """仕掛台帳の2つ目の置き場所(打たれたまま)。**既定は無い**ので、未設定なら空。"""
    value = user_settings.get(config.KEY_LOT_DB_DIR2)
    return value.strip() if isinstance(value, str) else ""


def _typed(key: str, fallback: Path, *legacy: str) -> str:
    """**打たれたままの道**を返す。相対で書いた人には相対のまま見せる。

    ここで絶対に直して返すと、保存するたびに書いた形が消え、
    「相対で書いたはずなのに絶対になっている」と読める。
    設定が無いときだけ、いま使っている道(既定)を出す。
    """
    from .. import user_settings
    for name in (key, *legacy):
        configured = user_settings.get(name)
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    return str(fallback)


def _board_usage(conn: Optional[sqlite3.Connection]) -> dict[str, Any]:
    """ボードの人気度 ── どのサイズがよく使われているか。

    **パレットをどれだけ覆えたかとは別の話です。** そちらは1回の配置の
    出来ばえで、配置の段に「使用率」「はみ出し」として出ています。
    ここで答えるのは「どのサイズを多く持っておけばよいか」で、
    何回ぶんも積み上がって初めて意味を持ちます。

    **使っていない寸法も並べます。** 一覧に載っているのに一度も
    使っていないサイズを見つけるのもこの表の役目なので、そこを隠すと
    意味がなくなります(出さなければ「無い」のか「0回」なのか
    区別できません)。

    一覧に無いのに使われた寸法は別に添えます ── マスタに足し忘れて
    いるか、寸法を打ち間違えているかのどちらかで、どちらも直すべき
    事実です。
    """
    empty = {"rows": [], "unlisted": [], "total_sheets": 0, "top": "",
             "unsent_sheets": 0, "summary": ""}
    if conn is None:
        return empty
    from .. import board_usage as usage

    def line(row: usage.PopularityRow) -> dict[str, Any]:
        return {"width": row.width, "length": row.length,
                "board_type": row.board_type, "sheets": row.sheets,
                "times": row.times, "share": row.share,
                "last_used_at": row.last_used_at, "used": row.used}

    got = usage.popularity(conn)
    top = got.top
    unsent = usage.unsent_sheets(conn)
    top_text = (f"よく使うのは {top.board_type} {top.width}×{top.length}"
                f"({top.share}%)" if top else "")
    if top:
        summary = f"{top_text} / 全端末の累計 {got.total_sheets}枚"
    else:
        summary = "まだ使われていません" if got.rows else "0 件"
    if unsent:
        # 送れていない分は手元にしか無い。**合計に入っているが、ほかの端末
        # からはまだ見えない**ことを言う
        summary += f"(うち、この端末からまだ送れていない {unsent}枚)"
    return {
        "unsent_sheets": unsent,
        "summary": summary,
        "rows": [line(r) for r in got.rows],
        "unlisted": [line(r) for r in got.unlisted],
        "total_sheets": got.total_sheets,
        # 見出しの一文。**一覧を全部読まなくても現状が分かる**ように、
        # 「いちばん使うもの」と「累計」だけを言う
        "top": top_text,
    }


def _lot_filters() -> list[dict[str, Any]]:
    """ロット一覧の「よく使う条件」。ここからも消せるようにする。

    足すのは一覧の画面、消すのはここ、では探し回ることになるので、
    **一覧を出すのはここ**にして、条件の中身まで見せる。
    """
    out: list[dict[str, Any]] = []
    for name, items in sorted(lot_browse_session.LotBrowseSession.saved().items()):
        labels = []
        for item in items:
            column = lot_query.BY_KEY.get(item.get("column", ""))
            if column is None:
                continue
            labels.append(f"{column.label} {item.get('op', '')} {item.get('value', '')}")
        out.append({"name": name, "conditions": labels})
    return out


def _find_material() -> Optional[Path]:
    try:
        return data_sync.find_material_db()
    except OSError:
        # 共有フォルダに届かないとき(UNCパスのタイムアウト等)。
        # 例外を上げると画面自体が開かなくなる
        return None


def _find_threshold() -> Optional[Path]:
    try:
        return data_sync.find_threshold_db()
    except OSError:
        return None


def _find_lots() -> dict[str, Path]:
    try:
        return data_sync.find_lot_dbs()
    except OSError:
        return {}


def _find_kanban() -> Optional[Path]:
    try:
        return data_sync.find_kanban_db()
    except OSError:
        return None


def _admin_authenticated(conn) -> bool:
    """いま管理者パスワードが通っているか。プロセスに1つの状態を覗くだけ。"""
    if conn is None:
        return False
    from .. import selection_session
    return selection_session.get_session(conn).admin


# 置き場所を直しに行く先。**問題を見せた場所から繋ぐ**
FIX_SOURCE = ("置き場所を直す", "source")
# アクセス権限を直しに行く先。マスタ管理タブで編集する
FIX_ACCESS = ("マスタ管理でアクセス権限を編集", "master")
# 取り込み直しに行く先。**入れ直さないと直らないもの**があるので繋ぐ
FIX_IMPORT = ("取り込み直す", "run")
# 重複 → 「取り込み」の面の「--fix を実行(重複を消す)」
FIX_DEDUPE = ("--fix を実行(重複を消す)へ", "run")


def _material_section(material: Optional[Path], conn=None, *,
                      found: Optional["source_db.Probe"] = None,
                      broken: Optional[list[str]] = None) -> Section:
    """梱包資材マスタの節。`found`・`broken` は背景で確かめた分(`shared_facts`)。渡されなければここで確かめる。"""
    section = Section("梱包資材マスタ", mark="材")
    _check_local_master(section, conn)
    if material is None:
        section.checks.append(Check(
            "ファイル", "見つかりません", NG,
            f"探した場所: {config.master_db_dir()}"
            f"(名前は {config.MATERIAL_DB_NAME}。"
            f"拡張子 {' / '.join(source_db.SUFFIXES)} を見ます)"))
        section.action = FIX_SOURCE
        return section

    section.checks.append(Check("ファイル", material.name, OK, str(material)))
    _link_check(section, material, conn)

    # **開けなかったときは、開いてみて分かったことを全部出す。**
    # 「sqlite3 として読めません」だけでは、現場も私たちも直せない
    if found is None:
        found = source_db.probe(material)
    if not found.ok:
        section.checks.append(Check("中身", "開けませんでした", NG,
                                    _why_unreadable(found)))
        section.action = FIX_SOURCE
        return section
    _opening_check(section, found)
    _encoding_check(section, found)

    names = found.tables
    from .. import import_specs
    # 看板マスタ側のテーブル(KANBAN_TABLES)は別ファイルの持ち物なので、
    # ここでは数えない ── 数えると、梱包資材マスタには本来入っていない
    # 8テーブルが毎回「不足」または「未作成」に見えてしまう
    expect = {t: v for t, v in import_specs.IMPORT_SPECS.items()
              if t not in import_specs.KANBAN_TABLES}
    missing = [t for t in expect
               if t not in names and t not in import_specs.OPTIONAL_TABLES]
    optional_missing = [t for t in import_specs.OPTIONAL_TABLES
                        if t not in names]
    if missing:
        # 取り込みが空になる原因はたいてい「テーブル名が違う」
        section.checks.append(Check(
            "テーブル", f"{len(names)}個(不足 {len(missing)}件)", WARN,
            "取り込む予定なのに無い: " + ", ".join(missing)))
    else:
        section.checks.append(Check("テーブル", f"{len(names)}個", OK))
    if optional_missing:
        # 無くても動く表。**足りない扱いにしない** ── 毎回「要確認」が
        # 出ると、本当に直すべき不足が埋もれる
        section.checks.append(Check(
            "任意のテーブル", f"{len(optional_missing)}件が未作成", INFO,
            "無くても動きます: " + ", ".join(optional_missing)))
    _guard_check(section, material, broken)
    return section


def _link_check(section: Section, material: Path, conn) -> None:
    """この端末の中身が、どの共有マスタのものか(取り違え防止。`shared_link`)。

    **読むだけ**(付け替えは送る・取り込むときに `shared_link.bind` が行う)。
    """
    if conn is None:
        return
    from .. import shared_link, sync_writeback
    try:
        previous = shared_link.remembered(conn)
    except sqlite3.Error:                         # pragma: no cover - 古いDB
        return
    if previous is None or shared_link.same_place(previous, material):
        section.checks.append(Check("つながっている共有", material.parent.name, OK,
                                    f"この端末の中身は {material} のものです"))
        return
    pending = sum(sync_writeback._unsent_writeback_tables(conn).values())
    link = shared_link.Link(current=str(material), previous=previous, pending=pending)
    if link.blocked:
        section.checks.append(Check("つながっている共有", "置き場所が変わりました", NG, link.why()))
        return
    section.checks.append(Check(
        "つながっている共有", "置き場所が変わりました", INFO,
        f"前: {previous} → いま: {material}。送る物は残っていないので、"
        "次の取り込みで中身を入れ替えて、こちらに切り替えます"))


def _guard_check(section: Section, material: Path,
                 broken: Optional[list[str]] = None) -> None:
    """二重登録の防止が効いているか。

    書き戻しは送信IDを取り込み元の一意インデックスに賭けています。
    ところがインデックスを作れないことがあり(送信IDが空の行が2つ以上
    あるなど)、そのときは**黙って防止なしで送り続けます** ── ログに
    1行出るだけで、現場からは何も変わって見えません。効いていないまま
    再送が起きると二重登録になります。

    **効いているときは黙っています。** 全部の項目に印が付くと、印が
    意味を持たなくなるためです。
    """
    if broken is None:
        broken = _guard_broken(material)
        if broken is None:
            return

    if broken:
        section.checks.append(Check(
            "二重登録の防止", f"効いていません({len(broken)}件)", WARN,
            "  ".join(broken)))


def _guard_broken(material: Path) -> Optional[list[str]]:
    """二重登録の防止が効いていない表の理由。確かめられなければ None(`_guard_check`)。"""
    from .. import data_sync, outbox_sync

    broken: list[str] = []
    try:
        with source_db.connect(material) as src:
            for spec in data_sync.WRITEBACK_SPECS:
                if spec.access_table not in src.table_names():
                    continue          # まだ送ったことがない表。まだ問えない
                state = outbox_sync.guard_state(src, spec)
                if not state.ok:
                    broken.append(state.why())
            # 実績の「同じ実績は1件だけ」(VER4.8.120)
            from .. import pattern_copies
            why = pattern_copies.guard_state(src)
            if why:
                broken.append(why)
    except source_db.SourceError as exc:       # pragma: no cover - 上で弾く
        log.debug("重複防止の状態を確かめられません: %s", exc)
        return None
    return broken


def _kanban_section(kanban: Optional[Path]) -> Section:
    """看板(在庫薄警告)マスタ。梱包資材マスタとは別ファイル。

    `_material_section` と同じ形にしてある(覚え直しを増やさないため)。
    見つからないのを NG にはしない ── 分けたばかりで置き場所が
    未設定の端末が多いうちは、**在庫薄警告を使わない端末**まで
    「直すべき不足」に見せると本当に直すべき問題が埋もれる。
    """
    from .. import import_specs

    section = Section("看板マスタ", mark="看")
    if kanban is None:
        section.checks.append(Check(
            "ファイル", "見つかりません", WARN,
            f"探した場所: {config.kanban_db_dir()}"
            f"(名前は {config.KANBAN_DB_NAME}。"
            f"拡張子 {' / '.join(source_db.SUFFIXES)} を見ます)。"
            "在庫薄警告(Form状態管理・看板_*)を使わないなら、"
            "このままで構いません。"))
        section.action = FIX_SOURCE
        return section

    section.checks.append(Check("ファイル", kanban.name, OK, str(kanban)))

    found = source_db.probe(kanban)
    if not found.ok:
        section.checks.append(Check("中身", "開けませんでした", NG,
                                    _why_unreadable(found)))
        section.action = FIX_SOURCE
        return section
    _opening_check(section, found)

    names = found.tables
    missing = [t for t in import_specs.KANBAN_TABLES if t not in names]
    if missing:
        section.checks.append(Check(
            "テーブル", f"{len(names)}個(不足 {len(missing)}件)", WARN,
            "取り込む予定なのに無い: " + ", ".join(missing)))
    else:
        section.checks.append(Check("テーブル", f"{len(names)}個", OK))
    return section


def _threshold_section(threshold: Optional[Path]) -> Section:
    """パレット適合閾値マスタ。梱包資材マスタとは別ファイル。

    見つからないのを NG にはしない ── 見つからないあいだは手元に
    入れてある基準表の初期値(`pallet_threshold.SEED`)で動くので、
    現場は止まりません。ただし**資材課が直した値は届いていない**ので、
    黙ってもいません。
    """
    from .. import import_specs

    section = Section("パレット閾値マスタ", mark="閾")
    if threshold is None:
        section.checks.append(Check(
            "ファイル", "見つかりません", WARN,
            f"探した場所: {config.threshold_db_dir()}"
            f"(名前は {config.THRESHOLD_DB_NAME}。"
            f"拡張子 {' / '.join(source_db.SUFFIXES)} を見ます)。"
            "いまは手元に入れてある基準表の初期値で動いています ── "
            "資材課が直した値を使うには、このファイルの置き場所を"
            "指してください。"))
        section.action = FIX_SOURCE
        return section

    section.checks.append(Check("ファイル", threshold.name, OK, str(threshold)))

    found = source_db.probe(threshold)
    if not found.ok:
        section.checks.append(Check("中身", "開けませんでした", NG,
                                    _why_unreadable(found)))
        section.action = FIX_SOURCE
        return section
    _opening_check(section, found)

    names = found.tables
    missing = [t for t in import_specs.THRESHOLD_TABLES if t not in names]
    if missing:
        section.checks.append(Check(
            "テーブル", f"{len(names)}個(不足 {len(missing)}件)", WARN,
            "取り込む予定なのに無い: " + ", ".join(missing)))
    else:
        section.checks.append(Check("テーブル", f"{len(names)}個", OK))
    return section


def _encoding_check(section: Section, found: "source_db.Probe") -> None:
    """中の文字をどちらの入れ方と判断したか。**判定を隠さない。**

    文字化けの問い合わせが来たとき、これが出ていれば
    「判定を誤った(CP932なのにUTF-8と読んだ等)」のか
    「元のファイルがそもそも壊れている」のかが、その場で切り分けられる。
    出ていないと、また同じ調べ直しを一からやることになる。
    """
    if not found.encoding:
        return
    cp932 = found.encoding == source_db.ENCODING_CP932
    section.checks.append(Check(
        "文字の入れ方", "Shift-JIS(CP932)" if cp932 else "UTF-8", INFO,
        ("中の日本語がCP932で入っていると判断しました。読むときに変換します。"
         if cp932 else
         "中の日本語がUTF-8で入っていると判断しました。そのまま読みます。")
        + "  ここが実際と食い違っていると文字化けします"
          "(化けているのにここが合っていれば、元のファイル側の問題です)。"))


def _why_unreadable(found: "source_db.Probe") -> str:
    """開けなかった理由と、**次にすること**。

    原因はだいたい3つに絞れます。どれなのかが分かれば現場で直せます:
    中身が別物 / 誰かが掴んでいる / WAL のまま共有に置かれている。
    """
    facts = [f"大きさ {found.size:,} バイト"]
    if found.sidecars:
        facts.append("付き添いファイル " + " / ".join(found.sidecars))
    tried = "、".join(f"{name}={why or 'OK'}" for name, why in found.attempts)

    if not found.is_sqlite:
        return (f"先頭が sqlite3 の印ではありません({', '.join(facts)})。"
                "名前だけ変えた別形式のファイル(Access 等)ではないか、"
                "変換が途中で止まっていないかを確かめてください。")
    if "wal" in found.sidecars:
        return (f"WAL のまま置かれています({', '.join(facts)})。"
                "共有フォルダの上では WAL のファイルは開けません。"
                "変換したPCで `PRAGMA journal_mode=DELETE;` を実行してから"
                "置き直してください。試した順: " + tried)
    return (f"{', '.join(facts)}。他のアプリ(変換ツール・Access 等)が"
            "掴んでいないか、共有フォルダの読み取り権限があるかを"
            "確かめてください。試した順: " + tried)


def _opening_check(section: Section, found: "source_db.Probe") -> None:
    """どうやって開いたか。**うまくいっているなら「要確認」にしない。**

    【毎回「要確認」になっていた】
    以前はここが `opened_by != WAY_URI` で判断していました。VER2.23.1
    に書いた時点では、それで正しかったのです ── 読むときの1手目は
    URI で、手元への写しは**開けなかったときの最後の手**でした。

    VER2.76.1 で順番を入れ替えました。共有のファイルを開いたままに
    すると上流が差し替えられず、`SIKALOT.pending_...` が共有に溜まる
    ためで、**読むときは最初から手元へ写します**(`source_db._open`)。
    ところがこちらの判断を直し忘れたので、

        開き方  手元への写し  要確認
        ふだんの開き方は通りませんでした()。

    が毎回出るようになりました。括弧が空なのは、URI の失敗理由を
    取りに行ったのに**そもそも試していないので理由が無い**からです。
    現場の「取り込めているはずなのに要確認になる」がこれでした。

    **できているのにできていないと出す**のがいちばん高くつきます。
    本当の問題が同じ顔で並ぶので、次からは誰も読まなくなります。

    【いまの決め方】
    「どの手で開けたか」ではなく、**折れた手があったかどうか**
    (`Probe.normal`)。折れていなければ、写して読むのはふだんの経路
    なので問題なしとして、なぜ写すのかだけを添えます。
    """
    if found.normal:
        section.checks.append(Check("開き方", found.opened_by, OK,
                                    _why_normal(found)))
        return
    section.checks.append(Check("開き方", found.opened_by, WARN,
                                _why_detoured(found)))


def _why_normal(found: "source_db.Probe") -> str:
    """ふだんどおりに開けたときの一言。**良し悪しではなく事実。**"""
    if found.opened_by != source_db.WAY_COPY:
        return "共有の上から直接読めています。"
    note = ("読むたびに手元へ写してから読んでいます。共有のファイルを"
            "開いたままにすると、変換する側が新しいファイルに置き換え"
            "られず、共有に `.pending_…` が溜まるためです"
            "(中身が変わったときだけ写し直します)。")
    if found.journal.lower() == "wal":
        # 写しているので困りはしない。**速くなる余地**として伝える
        note += ("なおこのファイルは WAL です。変換したPCで "
                 "`PRAGMA journal_mode=DELETE;` を実行して置き直すと、"
                 "共有の上でも開けるようになります(必須ではありません)。")
    return note


def _why_detoured(found: "source_db.Probe") -> str:
    """**実際に折れた手があった**ときの言い分。動いていても言う。"""
    broke = " / ".join(f"{name}: {why}" for name, why in found.failures)
    tail = ""
    if found.opened_by == source_db.WAY_COPY:
        tail = ("いまは手元へ写して読んでいるので動いていますが、"
                "置かれ方に手当てが要ります。")
        if found.journal.lower() == "wal":
            tail += ("元が WAL です。変換したPCで "
                     "`PRAGMA journal_mode=DELETE;` を実行して置き直して"
                     "ください。")
    return f"通らなかった開き方があります({broke})。{tail}"


def _check_local_master(section: Section, conn) -> None:
    """**手元のDB**に中身が入っているか。取り込み元の有無とは別の話。

    取り込み元のファイルが在って、テーブル名も合っていて、それでも
    手元が空、ということが起きる ── まだ一度も取り込んでいない場合と、
    取り込みが途中で転んだ場合。このとき現場には「ボード種別が出ない」
    「パレット検索が当たらない」という形でしか表面化せず、原因に
    辿り着けない。**最初に気づけるようにする。**
    """
    if conn is None:
        return                                  # DBを開けない状態(§設定は開く)
    # **中身が同じ行**が溜まっていないか。取り込みのたびに倍になる不具合
    # (〜VER2.2.0)は現場から見えなかったので、こちらから言う
    for spec in data_sync.WRITEBACK_SPECS:
        same = data_sync.duplicate_count(conn, spec.sqlite_table,
                                         spec.key_column)
        if same:
            section.checks.append(Check(
                "重複", f"{spec.sqlite_table} に {same}件", WARN,
                "この端末の手元のDBに、中身がまったく同じ行があります"
                "(同じ1件が2回以上入ったもの)。"
                "「取り込み」の面の「--fix を実行(重複を消す)」を押すと消えます。"))
            section.action = FIX_DEDUPE
    # **実績の写し**(同じ1件の実績が何件も入ったもの。VER4.8.119)。現場の共有で 2件が 37,154件に
    # なり、取り込み・資材選択を重くしていた。共有から取り込んだものなので、共有を片付ければ消える
    from .. import config as _config, pattern_copies
    copies = pattern_copies.local_count(conn)
    if copies:
        section.checks.append(Check(
            "重複", f"{_config.TBL_PT_HEADER} に写し {copies}件", WARN,
            "同じ1件の実績(登録日時・中身が同じ)が何件も入っています。共有の梱包資材マスタから"
            "取り込んだものです。「取り込み」の面の「--fix を実行(重複を消す)」を押すと、控えを"
            "取ってから共有の写しを消し、この端末も取り込み直します(ほかの端末は次の取り込みで直ります)。"))
        section.action = FIX_DEDUPE

    # 書き方のゆれ。**拾えてはいるが、直しておいたほうが確実**。
    # 「5x10」と小文字で書かれた行は、そろえてから固定表に当てている
    from .. import pallet_service

    loose = pallet_service.loose_key_rows(conn)
    if loose:
        sample = "、".join(f"{m.raw}→{m.matched}"
                           for m in loose[:LOOSE_SAMPLE])
        more = f" ほか{len(loose) - LOOSE_SAMPLE}件" if len(loose) > LOOSE_SAMPLE else ""
        section.checks.append(Check(
            "書き方のゆれ", f"パレットマスタに {len(loose)}件", WARN,
            f"{sample}{more}。そろえて拾っているので選定は通りますが、"
            f"マスタ側をそろえておくと確実です。"
            f"「適合範囲を再計算」を押すと、どの行かが全部出ます。"))

    # **発注コードの重複。** 同じコードが2つの品物を指すと、倉庫がどちらを用意するか
    # 決められない(現場の声:「パレットマスターに発注コードが重複しているものがあった。
    # 重複チェックは必要」)。Access の中に前からあったので、取り込みでは直らない
    from .. import pallet_dupes
    dupes = pallet_dupes.from_conn(conn)
    if dupes:
        sample = "、".join(g.describe() for g in dupes[:DUPE_SAMPLE])
        more = f" ほか{len(dupes) - DUPE_SAMPLE}種" if len(dupes) > DUPE_SAMPLE else ""
        section.checks.append(Check(
            "発注コードの重複", f"パレットマスタに {len(dupes)}種({sum(len(g.rows) for g in dupes)}行)",
            WARN,
            f"{pallet_dupes.summary(dupes)}。例: {sample}{more}。"
            "同じコードが別の品物を指すと、倉庫がどちらを用意するか決められません。"
            "「マスタ管理」で PalletMaster を開き「発注コードの重複だけ」を押すと全部出ます"
            "(直すのは Access の PalletMaster か、マスタ管理で)。"))

    # **文字化けは版を上げただけでは消えない。** 置き換わった時点で元の
    # バイトが失われているので、取り込み直すまで手元に残る
    # (現場の声:「文字化け治ってないよ」── 読む側は直っていた)
    garbled = data_sync.mojibake_rows(conn)
    if garbled:
        total = sum(garbled.values())
        detail = " / ".join(f"{t} {n}件" for t, n in sorted(garbled.items()))
        section.checks.append(Check(
            "文字化けした行", f"{total}件", WARN,
            f"{detail}。取り込み元をUTF-8として決め打ちで読んでいたころに"
            "入った字です(読み方は直しました)。**置き換わった字は戻らない**"
            "ので、「まとめて取り込み」を押して入れ直してください。"))
        section.action = FIX_IMPORT

    empty = data_sync.missing_master_tables(conn)
    if empty:
        section.checks.append(Check(
            "手元の中身", f"{len(empty)}件が空", NG,
            "空のまま: " + " / ".join(empty)
            + "。「まとめて取り込み」を押してください。"))
    else:
        section.checks.append(Check("手元の中身", "取り込み済み", OK))


def _lot_section(lots: dict[str, Path]) -> Section:
    total = len(config.LOT_DB_FILES)
    section = Section("仕掛台帳", mark="台")
    level = OK if len(lots) == total else (WARN if lots else NG)
    section.checks.append(Check("そろい具合", f"{len(lots)}/{total} ファイル", level))
    for table, filename in config.LOT_DB_FILES.items():
        found = lots.get(table)
        section.checks.append(Check(
            filename, str(found) if found else "見つかりません",
            OK if found else NG))
    # **文字化けの報告はここの表(仕掛かり一覧)から来る。** どちらの
    # 入れ方と判断したかを出しておく(`_encoding_check` の説明)
    for path in lots.values():
        _encoding_check(section, source_db.probe(path))
        break                          # 3ファイルとも同じ作られ方。1つで足りる
    from .. import sync_sources
    section.checks.append(Check(
        "探した場所", " → ".join(str(d) for d in sync_sources.lot_search_dirs()), INFO,
        "ファイルごとに前から探します(1つ目 → 2つ目)。2つ目は設定したときだけ見ます"))
    second = config.lot_db_dir2()
    if second is not None:
        extra = sync_sources.find_second_lot_dbs()
        section.checks.append(Check(
            "2つ目の置き場所", f"{second}({len(extra)}/{total} ファイル)",
            OK if extra else WARN,
            "1つ目にファイルが無ければ2つ目から読み、1つ目にファイルはあっても目当ての"
            "ロット・受注が無ければ、取り込みのあとに2つ目から足します(同じものは1つ目を使う)"))
        # 2つ目は**2つ目のファイル名**で探す。名前は変えてよいが、要る列はそろえる約束(現場)
        for table, filename in config.lot_db2_files().items():
            found = extra.get(table)
            if found is None:
                section.checks.append(Check(
                    f"2つ目 {filename}", "見つかりません", WARN,
                    f"{config.LOT_DB_FILES[table]} の2つ目として探す名前です"))
                continue
            lacking = sync_sources.second_missing_columns(found, table)
            section.checks.append(Check(
                f"2つ目 {filename}", str(found) if not lacking else f"足りない列: {', '.join(lacking)}",
                OK if not lacking else WARN,
                (f"{config.LOT_DB_FILES[table]} の2つ目。要る列はそろっています" if not lacking else
                 f"{config.LOT_DB_FILES[table]} の2つ目として読みますが、要る列が足りません"
                 "(ファイル名は変えてよいが、中身の要る列はそろえる約束です)。"
                 + ("設計_設備ｺｰｽ が無いと、2つ目から出したロットは BOX かどうか分かりません"
                    if "設計_設備ｺｰｽ" in lacking else ""))))
    _ls4_checks(section)
    if len(lots) < total:
        section.action = FIX_SOURCE
    return section


def _ls4_checks(section: Section) -> None:
    """LS4LOT(VER4.8.78)。**中身はまだ使っていない**ので、見つかるか・写して読めるかだけを出す。

    1つ目は LS4LOT の欄の置き場所と名前、2つ目は仕掛台帳(2つ目)の置き場所と LS4LOT の2つ目の名前。
    読むのは仕掛台帳と同じ `source_db.probe`(**手元へ写してから読む**)。
    """
    places = []
    first = config.ls4_db_dir()
    if first is None:
        section.checks.append(Check(
            "LS4LOT(1つ目)", "置き場所を設定していません", INFO,
            "LS4LOT は仕掛台帳(SIKALOT・SIKAHIKI・SIKAODR)とは別の置き場所です。空なら見ません"))
    else:
        places.append(("LS4LOT(1つ目)", first, config.ls4_db_name()))
    second = config.lot_db_dir2()
    if second is not None:
        places.append(("LS4LOT(2つ目)", second, config.ls4_db2_name()))
    for label, folder, name in places:
        path = source_db.find_named(folder, name)
        if path is None:
            section.checks.append(Check(label, "見つかりません", WARN, f"{folder} に {name} を探しました"))
            continue
        found = source_db.probe(path)
        if not found.ok:
            section.checks.append(Check(label, "読めません", WARN, f"{path}: {found.error}"))
        elif config.LS4_TABLE not in found.tables:
            section.checks.append(Check(
                label, f"表「{config.LS4_TABLE}」がありません", WARN,
                f"{path} にある表: {', '.join(found.tables) or 'なし'}"))
        elif not found.normal:
            section.checks.append(Check(label, str(path), WARN, _why_detoured(found)))
        else:
            section.checks.append(Check(
                label, str(path), OK,
                f"{found.opened_by}で読めました(表「{config.LS4_TABLE}」あり)。"
                "このツールはまだ LS4LOT の中身を使っていません"))


def _access_section(conn, startup_modes=None) -> Section:
    """この端末は誰で、何ができるか。

    **権限は、通ったときより通らなかったときのほうが説明を必要とする。**
    「資材モードが選べない」とだけ分かっても、原因がマスタ未取込なのか
    登録漏れなのかIDの綴り違いなのかで、次にすることが全部違う。
    そこで身元・効いた行・落ちた理由をそのまま出す。

    `conn` が無い(DBを開けない)ときも画面は開く。権限の節だけが
    「調べられませんでした」になる。
    """
    from .. import access_control, modes

    section = Section("この端末の権限", mark="権")
    if conn is None:
        section.checks.append(Check("状態", "調べられませんでした", WARN,
                                    "手元のデータベースを開けませんでした。"))
        return section

    grant = access_control.resolve(conn)
    section.checks.append(Check(
        "ログインID / PC名", grant.identity.label(), INFO,
        "Windows から取った値です。画面からは変えられません。"))

    labels = access_control.summarize(grant.codes)
    section.checks.append(Check(
        "使えるモード",
        " / ".join(modes.label(m) for m in grant.allowed_modes()) or "なし",
        OK if grant.allowed_modes() else NG,
        "持っている権限: " + (", ".join(labels) if labels else "なし")))

    # 作業ライン(VER4.8.86)。アクセス権限の「権限」の列に書いたライン(ほかのツールと同じ読み方)
    state = access_control.line_state(conn, grant.identity)
    mine, admin = list(state.lines), state.admin
    section.checks.append(Check(
        "作業ライン", ("すべて(Administrator)" if admin else " / ".join(mine)) or "登録されていません",
        OK if mine else INFO,
        ("このラインに入れた予定ロットが、ロット検索に知らせとして届きます。" if mine else
         (f"{access_control.TABLE} の、この端末({grant.identity.label()})の行の「権限」にラインを"
          "正規の呼び名で書くと(L-1 / LVC / HVC / 機側 / AIM / コイル / トット / バランサー / NS1。"
          "L1・LS は読みません)、そのラインの予定ロットがロット検索に知らせとして届きます。"
          + state.near_text()))))
    # 表ぜんたいの登録ライン(VER4.8.90。「いまの状態: 登録ラインを出してください」)。
    # どのラインがどの端末に届くか・**どの端末にも届かないライン**が見える
    table = access_control.registered_lines(conn)
    from packaging_tool import line_names as _ln
    used = [n for n in _ln.OFFICIAL if table[n]]
    section.checks.append(Check(
        "登録ライン", f"{len(used)} / {len(_ln.OFFICIAL)} ライン", INFO,
        f"{access_control.TABLE} の「権限」に書いてあるライン(どの端末に知らせが届くか): "
        + " / ".join(f"{n}: {', '.join(table[n]) or 'なし'}" for n in _ln.OFFICIAL)
        + f" / 作業長: {', '.join(table[_ln.LEADER]) or 'なし'}"
        + f" / Administrator(すべてのライン): {', '.join(table[_ln.ADMINISTRATOR]) or 'なし'}"
        + "。「なし」のラインに入れた予定ロットは、Administrator の端末にしか届きません。"))
    # 作業長(VER4.8.88)。作業予定ラインを送れるのは作業長の端末だけ。Administrator も作業長(VER4.8.89)
    leader = access_control.is_leader(conn, grant.identity)
    section.checks.append(Check(
        "作業長", ("はい(Administrator)" if admin else "はい") if leader else "いいえ", OK if leader else INFO,
        "倉庫連携の「作業予定ライン」でロットをラインに入れられます。" if leader else
        (f"「作業予定ライン」を送れるのは作業長の端末だけです。{access_control.TABLE} の、この端末"
         f"({grant.identity.label()})の行の「権限」に「作業長」(すべてのラインも持たせるなら "
         "Administrator)と書きます。")))

    # マスタを直すには資材モードに加えて管理者パスワードも要る(VER2.17.0)。
    # 入力欄は以前は資材選択画面にあり、**いつもどこだっけと探すことになる**
    # という現場の声を受けて、この設定画面自体へ移した(VER2.19.0)。
    # 置き場所そのものを常に出しておく(値そのものは出さない。設計書 §3.6)
    section.checks.append(Check(
        "マスタを直すパスワードの入力欄", "この画面の「動作」タブ「マスタ編集の認証」欄",
        INFO,
        f"{access_control.TABLE}を含め、マスタを直すには資材モードに加えて"
        "管理者パスワードが要ります。入力は設定画面の「動作」タブにある"
        "「マスタ編集の認証」で行います(この端末で一度通せば、"
        "他の画面からもそのまま使えます)。パスワードそのものはここには出しません。"))

    if grant.reason:
        # 既定へ落ちたときだけ理由を出す。落ちていなければ黙っている。
        # **何をどこに足せばよいかをコピーしてそのまま渡せる形にする**。
        # 「アクセス権限に行を足してください」とだけ言われても、
        # マスタ管理タブから直せることも、そもそも直せるのが資材モードを
        # 持つ人だけ(=たいてい自分ではない)ことも伝わらない
        mode_codes = ", ".join(
            f"{p.code}({p.label})" for p in access_control.permissions())
        section.checks.append(Check(
            "権限の出どころ", "既定を使っています", WARN,
            grant.reason + "。直すには、資材モードを持つ人に"
            f"「設定 > マスタ管理 > {access_control.TABLE}」で次の行を"
            "足してもらってください: "
            f"ログインID = {grant.identity.login_id or '(空でPC名だけでもよい)'} / "
            f"PC名 = {grant.identity.pc_name or '(空でIDだけでもよい)'} / "
            f"権限 = 使いたいモードのコード({mode_codes})。"
            "自分がその資材モードを持っていれば、下のボタンから直接開けます。"))
        section.action = FIX_ACCESS
    else:
        section.checks.append(Check(
            "権限の出どころ", f"{access_control.TABLE} の {len(grant.matched)}行",
            # 本ツールが読んだコードだけを出す(ほかのツール用の値が同じ欄に
            # 書かれていても、ここに並べると本ツールの権限に見える)
            OK, " / ".join(f"{r.condition_label()} → {', '.join(r.own_codes())}"
                           for r in grant.matched)))

    # **1つしか使えるモードが無いのは、既定へ落ちたときだけではない。**
    # マスタが正しく1行だけ許可している(現場ではごく普通の)ときも、
    # 「他のモードはどうすれば使えるのか」を知りたいのは同じ。
    # 上のCheckが「問題なし(OK)」でも、ここは黙らない。
    all_modes = tuple(m.key for m in modes.ALL)
    missing_modes = [m for m in all_modes if m not in grant.allowed_modes()]
    if missing_modes and not grant.reason:
        missing_labels = " / ".join(modes.label(m) for m in missing_modes)
        mode_codes = ", ".join(
            f"{access_control.mode_permission(m)}({modes.label(m)}モード)"
            for m in missing_modes)
        section.checks.append(Check(
            "他のモードを使うには", f"{missing_labels} は未許可",
            INFO,
            "資材モードを持つ人に"
            f"「設定 > マスタ管理 > {access_control.TABLE}」で次の行を"
            "足してもらってください: "
            f"ログインID = {grant.identity.login_id or '(空でPC名だけでもよい)'} / "
            f"PC名 = {grant.identity.pc_name or '(空でIDだけでもよい)'} / "
            f"権限 = {mode_codes}。"))
        if not section.action[0]:
            section.action = FIX_ACCESS

    # **足した権限が、開き直すまで全部は効かない。**
    # 使えるモードは要求のたびに引き直すので切り替えは通るが、URL の登録は
    # 起動時の権限で決まっている。マスタ管理から権限を足せるようになって
    # (VER2.1.0)ここを踏みやすくなったので、黙っていない
    if startup_modes is not None:
        fresh = [m for m in grant.allowed_modes() if m not in set(startup_modes)]
        if fresh:
            # **見出しにも書く。** ここだけは「要確認」ではなく、
            # することそのものを出す ── 読んだ人が次にするのは
            # 「確かめる」ではなく「起動し直す」
            section.headline = "開き直してください"
            section.checks.append(Check(
                "開き直しが要ります",
                " / ".join(modes.label(m) for m in fresh), WARN,
                "起動したあとに権限が増えました。モードの切り替えはできますが、"
                "その画面の一部は**開き直すまで出ません**"
                "(使える画面は起動時の権限で決まります)。"
                "アプリを終了して、もう一度起動してください。"))

    for problem in access_control.problems(conn):
        # 書いた人は効いているつもりでいる。黙って無視しない
        section.checks.append(Check("マスタの問題", "確認してください", WARN, problem))

    # ほかのツールと同じ表を使っている。本ツールが読まない権限は**参考**として
    # 出すだけ(警告にしない ── そのツールにとっては正しい値)
    foreign = access_control.foreign_codes(conn)
    if foreign:
        section.checks.append(Check(
            "ほかのツールの権限", f"{len(foreign)}種類を読み飛ばしています", INFO,
            f"{access_control.TABLE} の権限の列には、本ツールでは読まない値があります"
            "(ほかのツール用として読み飛ばしています。直す必要はありません): "
            + ", ".join(foreign)))

    section.checks.append(Check(
        "使える権限コード", str(len(access_control.permissions())), INFO,
        " / ".join(f"{p.code} = {p.label}" for p in access_control.permissions())))
    return section


def _terminal_section() -> Section:
    """この端末の事実。**設定ではない**ので変えられないが、要るとき要る。

    「どのDBを見ているのか」「ログはどこか」を聞かれるたびに調べるのは
    無駄なので、画面に出しておく。
    """
    import sys

    from .. import app_config

    section = Section("この端末", mark="端")
    section.checks.append(Check("手元のデータベース", str(config.DB_PATH), INFO))
    section.checks.append(Check("ログ", str(config.log_dir()), INFO))
    section.checks.append(Check("読み取り方式", data_sync.backend_name(), OK,
                                "取り込み元が sqlite3 なので、"
                                "追加のドライバは要りません"))
    # 版。**帯に出ているものと同じ出どころ**(`config/app.json`)。
    # 書き方が違うと「どれが新しいか」を並べて比べられなくなるので、
    # そのときだけ理由を添えて要確認にする
    problem = app_config.version_problem()
    section.checks.append(Check(
        "バージョン", app_config.version_label(),
        WARN if problem else INFO, problem))
    # **どのフォルダを動かしているか。** 「入れ替えたのに古いまま」の
    # ときに、まずここを読めば分かる ── 版が古ければ、入れ替えた先とは
    # 別のフォルダから起動している(ショートカットの向き先が古い等)
    section.checks.append(Check(
        "アプリの置き場所", str(app_config.APP_ROOT), INFO,
        "版が古いときは、入れ替えたフォルダとここが同じか確かめてください"))
    section.checks.append(Check("Python", sys.version.split()[0], INFO))
    return section


# ------------------------------------------------------------------
# 長時間処理
# ------------------------------------------------------------------
def job_dict(job: Optional[jobs.Job]) -> Optional[dict[str, Any]]:
    if job is None:
        return None
    return {
        "id": job.id,
        "kind": job.kind,
        "label": job.label,
        "state": job.state,
        "state_label": JOB_STATE_LABEL.get(job.state, job.state),
        "state_kind": JOB_STATE_KIND.get(job.state, "run"),
        "pct": job.pct,
        "message": job.message,
        "ok": job.ok,
        "summary": job.summary,
        "error": job.error,
        "elapsed_sec": round(job.elapsed_sec, 1),
        "running": job.is_running,
        # 通ってきた段。**横棒1本では「どこまで終わったか」が読めない**
        "steps": lane_steps(job.steps),
        "steps_total": len(job.steps),
        "steps_note": lane_note(job.steps),
    }


# レーンに並べる段の上限。**全部は並べない。**
# 取り込みは19段ある。19枚のカードを一度に見ても「どこで転んだか」は
# 読み取れないし、面がスクロールし始める(§1.4 作業記憶は4±1)。
LANE_LIMIT = 6


def lane_steps(steps: list[jobs.Step]) -> list[dict[str, Any]]:
    """レーンに出す段を選ぶ。

    **失敗した段は必ず出す。** 隠すと、直すべきものが見えなくなる。
    次に「いま走っている段」、残りは新しいものから埋める ── 通り過ぎた
    段より、いまの手前で何があったかのほうが知りたい。

    段が1つしかないときは何も返さない(横棒1本で足りる)。
    """
    total = len(steps)
    if total < 2:
        return []

    order: list[int] = [i for i, s in enumerate(steps) if not s.running and not s.ok]
    order += [i for i, s in enumerate(steps) if s.running]
    order += list(range(total - 1, -1, -1))

    keep: list[int] = []
    for index in order:
        if index in keep:
            continue
        keep.append(index)
        if len(keep) >= LANE_LIMIT:
            break
    keep.sort()
    return [step_dict(steps[i], no=i + 1, total=total) for i in keep]


def lane_note(steps: list[jobs.Step]) -> str:
    """並べきれなかった段のこと。**黙って落とさない。**"""
    hidden = len(steps) - len(lane_steps(steps))
    if hidden <= 0:
        return ""
    failed = sum(1 for s in steps if not s.running and not s.ok)
    shown_failed = sum(1 for d in lane_steps(steps) if d["state"] == "ng")
    missed = failed - shown_failed
    if missed > 0:
        return f"ほか {hidden} 件(うち {missed} 件が失敗)"
    return f"ほか {hidden} 件は完了しました"


# 段の見出しに使う言い換え。進捗の文は「いま何をしているか」なので
# 進行形で書かれている(「BoardMaster を読み込み中...」)。レーンには
# **終わった段も並ぶ**ので、そのままだと終わったものまで動いて
# いるように読める。動いているかどうかは状態(`state`)が持つので、
# 見出しからは進行形を落として「何について」だけを残す。
_STEP_TAIL = re.compile(r"\s*を?[^\sを]{0,8}?(?:中|しています)[.。…]*\s*$")


def step_title(message: str) -> str:
    trimmed = _STEP_TAIL.sub("", message).strip()
    # 落とすと何も残らない文(「準備しています」)はそのまま使う
    return trimmed or message.strip()


def step_dict(step: jobs.Step, *, no: int = 1, total: int = 1) -> dict[str, Any]:
    """1段。状態の語彙は共通(`.st--*` / `.lane--*`)にそろえる。"""
    if step.running:
        state = "run"
    else:
        state = "done" if step.ok else "ng"
    return {
        "no": no,
        "total": total,
        "label": step_title(step.label),
        "state": state,
        "state_label": {"run": "実行中", "done": "完了", "ng": "失敗"}[state],
        "elapsed_sec": round(step.elapsed_sec, 1),
    }


def jobs_dict(registry: Optional[jobs.JobRegistry] = None) -> dict[str, Any]:
    """`GET /api/jobs` が返す形。

    走っているものが無くても `recent` は返す。ブラウザを開き直したときに
    「さっきの取り込みはどうなったのか」が分かるようにするため
    (基盤仕様書 監視レベル2)。
    """
    registry = registry or jobs.get_registry()
    running = registry.running()
    return {
        "running": [job_dict(running)] if running else [],
        "recent": [job_dict(job) for job in registry.recent()],
        "busy": running is not None,
    }


# 「取り込み元」の面が見出しに背負う節(取り込み元の置き場所で直るもの)。
# **面と関係の無い節(権限・この端末)の「要確認」を、取り込み元の見出しに出さない**
SOURCE_SECTIONS = ("梱包資材マスタ", "看板マスタ", "パレット閾値マスタ", "仕掛台帳")
# 何が要確認かを一言で言うときに並べる件数(残りは「ほか N件」)
ATTENTION_SHOWN = 2


def attention(sections: list[Section]) -> list[dict[str, str]]:
    """「足りません」「要確認」の中身(重い順)。**何が**要確認かを言うため。

    現場の声:「何が要確認かわからんのですが」── 見出しの「要確認」は節ぜんぶの
    いちばん重い状態だけで、どの項目かを書いていなかった。
    """
    items = [{"section": s.title, "label": c.label, "value": c.value, "level": c.level}
             for s in sections for c in s.checks if c.level in (NG, WARN)]
    items.sort(key=lambda i: 0 if i["level"] == NG else 1)
    return items


def attention_text(items: list[dict[str, str]]) -> str:
    """「仕掛台帳 › 2つ目 SIKALOT.sqlite3: 見つかりません ほか1件」。無ければ空。"""
    if not items:
        return ""
    head = " / ".join(f"{i['section']} › {i['label']}: {i['value']}"
                      for i in items[:ATTENTION_SHOWN])
    rest = len(items) - ATTENTION_SHOWN
    return head + (f" ほか{rest}件" if rest > 0 else "")


def section_level(sections: list[Section]) -> str:
    """節の束の一番重い状態。"""
    for level in (NG, WARN, OK):
        if any(s.level == level for s in sections):
            return level
    return INFO


def source_state(view: SettingsViewModel) -> dict[str, str]:
    """「取り込み元」の面の見出しに出す状態と、何がそうなのか。"""
    sections = [s for s in view.sections if s.title in SOURCE_SECTIONS]
    level = section_level(sections)
    return {"level": level, "label": LEVEL_LABEL.get(level, level),
            "text": attention_text(attention(sections))}


def rail_badge(view: SettingsViewModel) -> dict[str, str]:
    """レールの「設定」に出す印。`title` に何が要確認かを入れる(印に指を置くと読める)。"""
    text = {NG: "!", WARN: "?"}.get(view.level, "")
    detail = attention_text(attention(view.sections))
    return {"text": text, "kind": "todo" if text else "done",
            "title": f"{LEVEL_LABEL[view.level]}: {detail}" if text and detail else ""}


def tab_badges(view: SettingsViewModel) -> dict[str, dict[str, str]]:
    """タブ見出しに出す状態と件数。

    **開いていないタブの問題を隠さない。** 面で分けたせいで
    「足りません」に気づけなくなるなら、スクロールのほうがまだまし。
    中身の一番重い状態を見出しが背負う。

    件数も出す(情報の匂い、§2.4)── 押す前に、その先に何があるかが
    分かるようにする。
    """
    badges: dict[str, dict[str, str]] = {}

    # いまの状態: 節の中で一番重いもの。問題なしのときは黙っている
    # (全部のタブに印が付くと、印が意味を持たなくなる)
    if view.level in (NG, WARN):
        badges[STATUS_TAB] = {"level": view.level, "text": LEVEL_LABEL[view.level]}

    # 取り込み: 押せないなら先に言う
    if not view.can_import:
        badges["run"] = {"level": NG, "text": "できません"}

    # 動作: 図面URLの書き方が違えば、開く前に分かるようにする
    if view.spec_sheet_problem:
        badges["behavior"] = {"level": WARN, "text": "要確認"}

    if view.lot_filters:
        badges["filters"] = {"level": INFO, "text": str(len(view.lot_filters))}
    return badges


def _distribution_summary() -> dict[str, Any]:
    from .. import distribution
    return distribution.summary()


# ------------------------------------------------------------------
# 保存場所(このPCだけのもの / 複数PCで共有するもの)
# ------------------------------------------------------------------
# 現場の声:「ローカルに保存してそのPCで引き継いで使うものは、設定に
# そういうファイルがあると明記してほしい。複数PCで共有するものと、
# そのPCで引き継ぐものは違う」。**場所はいまの設定から引く**(書き写すと
# 設定を変えた日に食い違う)。
@dataclass(frozen=True)
class StoragePlace:
    what: str        # 何か
    where: str       # いまの場所(実際のパス)
    contents: str    # 中に入っているもの
    carry: str       # 無くなったら / 引き継ぎ方


def storage_places() -> dict[str, list[dict[str, str]]]:
    """「保存場所」の面に出す2つの表。`local` はこのPCだけ、`shared` は全PC共通。"""
    from .. import app_config, pallet_map, selection_log_store

    master = config.master_db_dir() / config.MATERIAL_DB_NAME
    local = [
        StoragePlace(
            "設定ファイル", str(config.USER_CONFIG_PATH),
            "取り込み元・書き出し先・ログの出力先の場所、拠点、自動取り込み、図面URL、"
            "管理者パスワード(撹拌した値)、よく使う条件",
            "設定し直し。新しい版に入れ替えるときは data フォルダごと持っていく"),
        StoragePlace(
            "手元のDB", str(config.DB_PATH),
            "取り込んだマスタ・仕掛台帳の写し、選定の記録、"
            "発注・受払・使用実績・コメントの控え(まだ共有へ送れていないものも)、"
            "コメントの既読",
            "取り込めば戻る。ただし、まだ共有へ送れていないものは戻らない"),
        StoragePlace(
            "棚検索の配置図", str(floor_plan.USER_PATH),
            "配置編集で動かした置き場と背景の写真",
            "配置編集をやり直し(無ければ出荷時の配置で動く)"),
        StoragePlace(
            "簡易在庫の保管位置マップ", str(pallet_map.USER_PATH),
            "配置編集で動かした保管位置と背景の写真",
            "配置編集をやり直し(無ければ出荷時の配置で動く)"),
        StoragePlace(
            "動作ログ・エラー記録", str(config.log_dir()),
            "操作ごとの記録とエラー記録(設定の「ログ」の面で出力先を変えられる。"
            "共有フォルダを指せば、ほかのPCからも見られる)",
            "無くても動く。困ったときに開発担当へ送るもの"),
        StoragePlace(
            "選定ログ", str(selection_log_store.directory()),
            f"選定ログ(日ごと。{selection_log_store.KEEP_DAYS}日で消える)",
            "無くても動く"),
        StoragePlace(
            "作業用フォルダ", str(app_config.local_root()),
            "一時ファイル、表を持ってくる前のバックアップ など",
            "無くても動く"),
    ]
    shared = [
        StoragePlace(
            "梱包資材マスタ", str(master),
            "マスタ(ボード・パレット・資材など)、倉庫への発注、受払の履歴、"
            "ボード使用実績、発注コメント、実績パターン",
            "各PCが書き戻しで送り、取り込みで受け取る"),
        StoragePlace(
            "仕掛台帳", " / ".join(str(d) for d in config.lot_db_dirs()),
            "仕掛ロット・仕掛引当・仕掛受注(2つ目は、1つ目に無いファイル・ロットだけ読む)",
            "読むだけ"),
        StoragePlace(
            "看板マスタ", str(config.kanban_db_dir() / config.KANBAN_DB_NAME),
            "在庫(看板)の表", "読むだけ"),
        StoragePlace(
            "パレット閾値マスタ",
            str(config.threshold_db_dir() / config.THRESHOLD_DB_NAME),
            "パレット選定の閾値", "読むだけ"),
    ]
    return {"local": [vars(p) for p in local], "shared": [vars(p) for p in shared],
            "export": str(config.export_dir())}


def to_dict(view: SettingsViewModel) -> dict[str, Any]:
    return {
        "level": view.level,
        "tab_badges": tab_badges(view),
        # 何が要確認か(見出しの「要確認」だけでは分からない)。画面を描き直すたびに付け直す
        "source_state": source_state(view),
        "rail_badge": rail_badge(view),
        # 文言はサーバが持つ(設計書 §4)。画面側で書き分けない
        "level_label": LEVEL_LABEL,
        "sections": [
            # `mark` / `badge_tone` は見出しの印。**色を知覚できなくても
            # 分類が字で分かる**ようにするためのもので、画面側では作れない
            # (どの節が何の印かを知っているのはここだけ)
            {"title": s.title, "level": s.level,
             "mark": s.mark, "badge_tone": s.badge_tone,
             # 見出しに出す言葉。**画面では決めない**(§4)
             "label": s.label(),
             # 直しに行く先。**問題を見せた面には直す手立てが無い**ので、
             # そこから繋ぐ(文言も行き先もサーバが決める)
             "action": {"label": s.action[0], "tab": s.action[1]}
                       if s.action[0] else None,
             "checks": [{"label": c.label, "value": c.value,
                         "level": c.level, "detail": c.detail}
                        for c in s.checks]}
            for s in view.sections
        ],
        "master_dir": view.master_dir,
        "lot_dir": view.lot_dir,
        "lot_dir2": view.lot_dir2,
        # 2つ目のファイル名。画面は表ごとに 1つ目の名前 → 2つ目の名前 の順で並べる
        "lot_files2": [{"table": t, "first": _first_names()[t], "name": n}
                       for t, n in view.lot_files2.items()],
        "ls4_dir": view.ls4_dir,
        "ls4_name": view.ls4_name,
        "ls4_dir_real": (view.ls4_dir_real
                         if view.ls4_dir_real != view.ls4_dir else ""),
        "kanban_dir": view.kanban_dir,
        "threshold_dir": view.threshold_dir,
        "export_dir": view.export_dir,
        # 相対で書かれたときに「実際どこを見ているか」。同じ道なら空で返す
        # ── 同じものを2行に出すと、違うものに見える
        "master_dir_real": (view.master_dir_real
                            if view.master_dir_real != view.master_dir else ""),
        "lot_dir_real": (view.lot_dir_real
                         if view.lot_dir_real != view.lot_dir else ""),
        "lot_dir2_real": (view.lot_dir2_real
                          if view.lot_dir2_real != view.lot_dir2 else ""),
        "kanban_dir_real": (view.kanban_dir_real
                            if view.kanban_dir_real != view.kanban_dir else ""),
        "threshold_dir_real": (view.threshold_dir_real
                               if view.threshold_dir_real != view.threshold_dir
                               else ""),
        "export_dir_real": (view.export_dir_real
                            if view.export_dir_real != view.export_dir else ""),
        "order_csv_dir": view.order_csv_dir,
        # 空(書き出し先と同じ)のときも、実際に出る場所は出す
        "order_csv_dir_real": (view.order_csv_dir_real
                               if view.order_csv_dir_real != view.order_csv_dir else ""),
        "path_base": view.path_base,
        "auto_import": view.auto_import,
        "position": view.position,
        "positions": view.positions,
        "spec_sheet_url": view.spec_sheet_url,
        "spec_sheet_problem": view.spec_sheet_problem,
        "spec_sheet_placeholder": spec_sheet.PLACEHOLDER,
        # **値は返さない。** 変えてあるかどうかだけ
        "admin_custom": view.admin_custom,
        # 配布設定(`packaging_tool/distribution.py`)。パスワードの値は出さない
        "distribution": _distribution_summary(),
        "admin_min_length": view.admin_min_length,
        "admin_authenticated": view.admin_authenticated,
        "lot_filters": view.lot_filters,
        "board_usage": view.board_usage,
        "can_import": view.can_import,
        "import_reason": view.import_reason,
        # まだ確かめている途中なら、画面が少しあとで引き直す(VER4.8.115)
        "status_pending": view.status_pending,
        "status_retry_ms": STATUS_RETRY_MS,
    }


# ------------------------------------------------------------------
# 設定の保存
# ------------------------------------------------------------------
@dataclass
class SaveResult:
    ok: bool = True
    message: str = ""
    reason: str = ""


# 断りの種類。**文言から推し量らない**
REFUSE_BAD_INPUT = "bad_input"
REFUSE_NOT_LISTED = "not_listed"
REFUSE_NEED_PASSWORD = "need_password"


# ------------------------------------------------------------------
# 管理者パスワードで守る設定
#
# 【なぜ置き場所だけなのか】
# 置き場所を変えると、**このツールが読み書きする相手そのもの**が
# 変わります。取り込みは総入れ替えなので、間違った先を指したまま
# 取り込むと手元の中身が入れ替わり、書き戻し(`outbox_sync`)も
# そちらへ行きます。押し間違いが**別のファイルを書き換える**ところ
# まで届く設定は、ここだけです。
#
# 自動取り込み・拠点・仕様書URL・よく使う条件は、間違えても
# その端末の見え方が変わるだけなので守りません。**守る対象を
# 増やすほど、現場はパスワードを紙に貼る**ようになります。
#
# これはUIガードで、権限ではありません。誰がその端末を使えるかは
# `access_control`(アクセス権限マスタ)が決めます。
PROTECTED_LABELS = {
    "master_dir": "梱包資材マスタの置き場所",
    "lot_dir": "仕掛台帳の置き場所",
    "lot_dir2": "仕掛台帳の2つ目の置き場所",
    "lot_files2": "仕掛台帳の2つ目のファイル名",
    "ls4_dir": "LS4LOT の置き場所",
    "ls4_name": "LS4LOT のファイル名",
    "kanban_dir": "看板マスタの置き場所",
    "threshold_dir": "パレット閾値マスタの置き場所",
}


def _protected_changes(master_dir: Optional[str], lot_dir: Optional[str],
                       kanban_dir: Optional[str],
                       threshold_dir: Optional[str] = None,
                       lot_dir2: Optional[str] = None,
                       lot_files2: Optional[dict] = None,
                       ls4_dir: Optional[str] = None,
                       ls4_name: Optional[str] = None) -> list[str]:
    """今回**本当に変わる**置き場所の名前。

    値が変わらない保存で聞かないのは、設定画面が置き場所を毎回
    まとめて送るためです。拠点を選び直しただけでパスワードを聞かれると、
    現場は「何をしても聞かれる」と受け取り、パスワードそのものが
    形骸化します。
    """
    now = {
        "master_dir": _typed(config.KEY_MASTER_DB_DIR, config.master_db_dir(),
                             config.KEY_ACCDB_DIR_LEGACY),
        "lot_dir": _typed(config.KEY_LOT_DB_DIR, config.lot_db_dir()),
        "lot_dir2": _lot_dir2_typed(),
        "ls4_dir": _ls4_dir_typed(),
        "ls4_name": _ls4_name_typed(),
        "kanban_dir": _typed(config.KEY_KANBAN_DB_DIR, config.kanban_db_dir()),
        "threshold_dir": _typed(config.KEY_THRESHOLD_DB_DIR,
                                config.threshold_db_dir()),
    }
    sent = {"master_dir": master_dir, "lot_dir": lot_dir, "lot_dir2": lot_dir2,
            "ls4_dir": ls4_dir, "ls4_name": ls4_name,
            "kanban_dir": kanban_dir, "threshold_dir": threshold_dir}
    changed = [PROTECTED_LABELS[key] for key, value in sent.items()
               if value is not None and value.strip() != now[key]]
    if lot_files2 is not None and _lot_files2_cleaned(lot_files2) != _second_names():
        changed.append(PROTECTED_LABELS["lot_files2"])
    return changed


def _order_csv_typed() -> str:
    """打ってある発注CSVの出力先。打っていなければ空(書き出し先と同じ所に出る)。"""
    value = user_settings.get(config.KEY_ORDER_CSV_DIR)
    return value.strip() if isinstance(value, str) else ""


def _lot_files2_cleaned(sent: dict) -> dict[str, str]:
    """送られた2つ目のファイル名を、保存する形にする。空・知らない表は1つ目の名前(今までどおり)。

    LS4LOT(VER4.8.78)は、**送られてこなければいまの名前のまま**(前の画面から保存しても消さない)。
    """
    names = _first_names()
    if not (isinstance(sent, dict) and config.LS4_KEY in sent):
        names[config.LS4_KEY] = config.ls4_db2_name()
    for table in list(names):
        value = sent.get(table) if isinstance(sent, dict) else None
        if isinstance(value, str) and value.strip():
            names[table] = value.strip()
    return names


def save(master_dir: Optional[str] = None, lot_dir: Optional[str] = None,
         auto_import: Optional[bool] = None, spec_url: Optional[str] = None,
         position: Optional[str] = None,
         password: Optional[str] = None,
         kanban_dir: Optional[str] = None,
         threshold_dir: Optional[str] = None,
         lot_dir2: Optional[str] = None,
         lot_files2: Optional[dict] = None,
         order_csv_dir: Optional[str] = None,
         ls4_dir: Optional[str] = None,
         ls4_name: Optional[str] = None,
         conn: Any = None) -> SaveResult:
    """設定を保存する。**渡されたものだけ**を触る。

    `None` は「この項目は今回いじらない」の意味。画面が一部だけ送って
    きたときに、送っていない項目を消さないため。

    置き場所の空文字は「既定に戻す」で、そのまま保存してよい
    (`config.master_db_dir()` が既定値を返すようになる)。

    置き場所を**変えるとき**だけ管理者パスワードが要ります
    (`PROTECTED_LABELS` の説明を参照)。関門をここに置くのは、
    画面からもスクリプトからも同じ道を通すためです。
    """
    if spec_url is not None:
        problem = spec_sheet.template_problem(spec_url)
        if problem:
            # 書けてしまってから「なぜか図面が出ない」を追うより、
            # その場で言うほうが早い
            return SaveResult(False, problem, REFUSE_BAD_INPUT)

    if position is not None:
        allowed = location_service.base_point_names(conn)
        if position not in allowed:
            # 図に無い拠点を入れると、疲労度も棚検索も当たらなくなる
            return SaveResult(
                False,
                f"拠点は {' / '.join(allowed)} から選んでください。",
                REFUSE_NOT_LISTED)

    # **書く前に通す関門。** ここより下で1つでも書いてしまうと、
    # 断ったのに一部だけ変わった状態が残る
    if lot_files2 is not None:
        problem = _lot_files2_problem(lot_files2)
        if problem:
            return SaveResult(False, problem, REFUSE_BAD_INPUT)
    if ls4_name is not None:
        problem = _ls4_name_problem(ls4_name)
        if problem:
            return SaveResult(False, problem, REFUSE_BAD_INPUT)
    changing = _protected_changes(master_dir, lot_dir, kanban_dir,
                                  threshold_dir, lot_dir2, lot_files2,
                                  ls4_dir, ls4_name)
    if changing:
        if not admin_password.verify(str(password or "")):
            # 合っていないのか、そもそも送っていないのかは言い分けない
            # ── 総当たりの手がかりになる。**何が要るか**だけを言う
            log.warning("置き場所の変更を断りました(管理者パスワード): %s",
                        " / ".join(changing))
            return SaveResult(
                False,
                f"{' と '.join(changing)}を変えるには管理者パスワードが要ります。",
                REFUSE_NEED_PASSWORD)
        log.info("置き場所を変えます(管理者パスワード確認済み): %s",
                 " / ".join(changing))

    if master_dir is not None:
        user_settings.save(config.KEY_MASTER_DB_DIR, master_dir.strip())
    if lot_dir is not None:
        user_settings.save(config.KEY_LOT_DB_DIR, lot_dir.strip())
    if lot_dir2 is not None:
        # 空にすると2つ目は使わない(既定は無い)
        user_settings.save(config.KEY_LOT_DB_DIR2, lot_dir2.strip())
    if lot_files2 is not None:
        user_settings.save(config.KEY_LOT_DB2_FILES, _lot_files2_cleaned(lot_files2))
    if ls4_dir is not None:
        # 空にすると LS4LOT は見ない(既定は無い)
        user_settings.save(config.KEY_LS4_DB_DIR, ls4_dir.strip())
    if ls4_name is not None:
        # 空なら LS4LOT.sqlite3
        user_settings.save(config.KEY_LS4_DB_NAME, ls4_name.strip())
    if kanban_dir is not None:
        user_settings.save(config.KEY_KANBAN_DB_DIR, kanban_dir.strip())
    if threshold_dir is not None:
        user_settings.save(config.KEY_THRESHOLD_DB_DIR, threshold_dir.strip())
    if order_csv_dir is not None:
        # **管理者パスワードは要らない**(書き出し先と同じ理由。出力をどこに置くかは
        # その端末の都合で、変えても全員の見えるデータは変わらない)
        user_settings.save(config.KEY_ORDER_CSV_DIR, order_csv_dir.strip())
    if auto_import is not None:
        user_settings.save(config.KEY_AUTO_IMPORT, bool(auto_import))
    if spec_url is not None:
        user_settings.save(config.KEY_SPEC_SHEET_URL, spec_url.strip())
    if position is not None:
        user_settings.set_position(position)
    return SaveResult(True, "設定を保存しました")


def export_board_usage(conn, directory: str = "") -> SaveResult:
    """ボード人気度をCSVに書き出す。**書いた場所を返す。**

    渡された書き出し先は**そのまま設定として覚える** ── 毎回打ち直す
    ものではないため。次からは空で押せば同じ場所に出る。

    【管理者パスワードを要らなくしてある理由】
    取り込み元の置き場所(`PROTECTED_LABELS`)は、変えると**全員の
    見えるデータが変わる**ので守っています。書き出し先は違います ──
    出力を自分のどこに置くかという、その端末の都合です。守る対象を
    増やすほど、現場はパスワードを紙に貼るようになります。
    """
    from .. import board_usage

    text = (directory or "").strip()
    if text:
        user_settings.save(config.KEY_EXPORT_DIR, text)
    try:
        path = board_usage.write_csv(
            conn, config.resolve_dir(text) if text else None)
    except OSError as exc:
        # **どこへ書こうとして駄目だったのかを言う。** 「書けません」
        # だけでは、道が違うのか権限が無いのかが分からない
        where = config.resolve_dir(text) if text else config.export_dir()
        log.warning("ボード人気度を書き出せません(%s): %s", where, exc)
        return SaveResult(
            False, f"{where} に書き出せませんでした({exc})。"
                   "書き出し先を確かめてください。", REFUSE_BAD_INPUT)
    return SaveResult(True, f"書き出しました: {path}")


def delete_lot_filter(name: str) -> SaveResult:
    """よく使う条件を1つ消す。"""
    result = lot_browse_session.get_session().delete_saved(name)
    return SaveResult(result.ok, result.message, result.reason)
