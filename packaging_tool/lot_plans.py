"""予定ロット ── 長がロットを作業ラインに入れ、そのラインの端末に知らせる(VER4.8.85)

現場の声:
    「長」        ロット検索 → 資材選定 → 作業ライン選定 → 倉庫連携にて発注
    「各ライン作業者」 ツール起動 → 予定ロット欄 通知確認 → 検索 → ボード選定 → 作業
    「作業予定ラインで設定されたロットは、アクセス権限でのライン名と一致したPCのロット検索で
     通知が出ます。通知確認すると作業予定ラインされたロット情報が自動で開きます。
     通知確認ボタンを設けて見なくても通知は消せるようにして下さい」

【どう届けるのか ── 足すだけの1つの表】
    予定ロット … ロットをラインに入れた(予定)/ 外した(取り消し)。押すたびに1行(書き直さない)
切断依頼と同じく、書き戻しで共有へ送り、取り込みで全端末が受け取る。**書き直さない**ので、
2つの端末が同時に押しても行が上書きし合わない。

【いまの予定の決め方 ── どの端末でも同じ答え】
ロットごとに、いちばん新しい行(送った日時、同じなら共有に入った順)がいまの予定。
その行が「取り消し」なら、そのロットはどのラインの予定でもない。別のラインに入れ直すと、
新しい行が勝つ(前のラインの知らせは消える)。

【知らせ ── 端末ごと】
この端末のライン(アクセス権限の **権限** の列に書いたライン。`access_control.lines`)に入ったロットのうち、
まだ「確認」していないものが知らせ。確認したかは**この端末だけ**に残す(予定ロット確認。共有へは
送らない ── 同じラインの端末が何台あっても、それぞれに知らせる)。入れ直された予定は新しい行なので、
もう一度知らせる。

【一覧に出す範囲】
送ってから `LIST_DAYS` 日のあいだ(古い予定がいつまでも並ばないように)。
"""
from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config
from .logging_utils import get_logger
from .sqlite_toolkit import now_db_string

log = get_logger("lot_plans")

TABLE = config.TBL_LOT_PLAN
SEEN_TABLE = config.TBL_LOT_PLAN_SEEN

# 状態。**言葉は1か所に**
PLANNED = "予定"
CANCELLED = "取り消し"
# ラインの端末で「作業済み」を押した(VER4.8.109)。そのラインのどの端末の予定欄からも消える
DONE = "作業済み"

# 一覧に出す日数と件数
LIST_DAYS = 7
LIST_LIMIT = 50

# 断りの種類(画面は文言から推し量らない)
REFUSE_NO_LOT = "no_lot"
REFUSE_NO_LINE = "no_line"
REFUSE_NOT_PLANNED = "not_planned"
REFUSE_NOT_MINE = "not_mine"


@dataclass
class Result:
    ok: bool
    message: str
    reason: str = ""
    plan_id: str = ""
    changed: bool = True


@dataclass
class Plan:
    """予定ロットの1行(いまの予定として読むときは、そのロットのいちばん新しい行)。"""

    plan_id: str
    lot_no: str
    line: str
    state: str
    terminal: str
    sender: str
    sent_at: str
    summary: str = ""
    # この端末で確認した日時(空なら未確認 = 知らせ)。開いて確認したか
    seen_at: str = ""
    opened: bool = False

    @property
    def unseen(self) -> bool:
        return not self.seen_at

    def to_dict(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, "lot_no": self.lot_no, "line": self.line,
                "state": self.state, "terminal": self.terminal, "sender": self.sender,
                "sent_at": self.sent_at, "sent_label": short_time(self.sent_at),
                "summary": self.summary, "seen": bool(self.seen_at),
                "seen_at": self.seen_at, "opened": self.opened}


def normalize_lot(text: Any) -> str:
    """ロットNOの形をそろえる(全角半角・前後の空白・小文字)。"""
    return unicodedata.normalize("NFKC", "" if text is None else str(text)).strip().upper()


def line_key(name: Any) -> str:
    """ラインの名前を突き合わせる形。**文字の幅と前後の空白だけ**を揃える(`ＨＶＣ` = `HVC`)。

    ほかのツールと同じ読み方(`line_names`)。`L1` と `L-1` は別物(正規は L-1。VER4.8.86)。
    """
    from .line_names import same
    return same(name)


def short_time(at: str) -> str:
    """'2026-10-22 09:12:31' → '10/22 09:12'。"""
    try:
        return f"{at[5:7]}/{at[8:10]} {at[11:16]}"
    except (TypeError, IndexError):
        return at or ""


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
def _events(conn: sqlite3.Connection, where: str = "",
            params: tuple = ()) -> list[Plan]:
    """行を古い順に(送った日時、同じなら共有に入った順 = 管理番号)。表が無ければ空。"""
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT 予定ID, LotNo, ライン, 状態, 送った端末, 送った人, 送った日時, 要約"
            f" FROM {TABLE} WHERE 予定ID <> ''{where}"
            " ORDER BY 送った日時, 管理番号", params).fetchall()
    except sqlite3.Error:
        return []                       # 表がまだ無い(古い手元DB)
    return [Plan(plan_id=r["予定ID"], lot_no=r["LotNo"], line=r["ライン"],
                 state=r["状態"], terminal=r["送った端末"], sender=r["送った人"],
                 sent_at=r["送った日時"], summary=r["要約"]) for r in rows]


def _latest_by_lot(events: Iterable[Plan]) -> dict[str, Plan]:
    latest: dict[str, Plan] = {}
    for event in events:                # 古い順なので、あとの行が勝つ
        latest[normalize_lot(event.lot_no)] = event
    return latest


def current(conn: sqlite3.Connection, lot_no: str) -> Optional[Plan]:
    """そのロットのいまの予定。どのラインの予定でもなければ None。"""
    lot = normalize_lot(lot_no)
    if not lot:
        return None
    found = _latest_by_lot(_events(conn, " AND LotNo = ?", (lot,))).get(lot)
    if found is None or found.state != PLANNED:
        return None
    return found


def _seen(conn: sqlite3.Connection) -> dict[str, tuple[str, bool]]:
    try:
        rows = conn.execute(f"SELECT 予定ID, 確認日時, 開いた FROM {SEEN_TABLE}").fetchall()
    except sqlite3.Error:
        return {}
    return {str(r[0]): (str(r[1] or ""), bool(r[2])) for r in rows}


def for_lines(conn: sqlite3.Connection, lines: Iterable[str], *,
              now: Optional[datetime] = None, days: int = LIST_DAYS,
              limit: int = LIST_LIMIT) -> list[Plan]:
    """そのライン(この端末のライン)のいまの予定。**未確認を先に**、そのあと新しい順。

    送ってから `days` 日を過ぎたものは出さない。ラインが無ければ空。
    """
    keys = {line_key(n) for n in lines if line_key(n)}
    if not keys:
        return []
    since = ((now or datetime.now()) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    seen = _seen(conn)
    found: list[Plan] = []
    for plan in _latest_by_lot(_events(conn)).values():
        if plan.state != PLANNED or line_key(plan.line) not in keys or plan.sent_at < since:
            continue
        plan.seen_at, plan.opened = seen.get(plan.plan_id, ("", False))
        found.append(plan)
    found.sort(key=lambda p: p.sent_at, reverse=True)
    found.sort(key=lambda p: 0 if p.unseen else 1)
    return found[:limit]


def unseen_count(conn: sqlite3.Connection, lines: Iterable[str]) -> int:
    """この端末のラインに届いて、まだ確認していない予定の数(レールと見出しに出す)。"""
    return sum(1 for p in for_lines(conn, lines) if p.unseen)


# ------------------------------------------------------------------
# 入れる・外す・確認する
# ------------------------------------------------------------------
def send(conn: sqlite3.Connection, lot_no: str, line: str, *, terminal: str,
         sender: str = "", summary: str = "") -> Result:
    """ロットをラインに入れる(手元に足す。共有へは呼び手が書き戻しで送る)。

    もう同じラインの予定なら何もしない(知らせを2度出さない)。別のラインの予定なら入れ直す
    (新しい行が勝つ)。
    """
    lot = normalize_lot(lot_no)
    if not lot:
        return Result(False, "ロットNOがありません。ロット検索でロットを開いてから入れてください。",
                      REFUSE_NO_LOT, changed=False)
    name = str(line or "").strip()
    if not line_key(name):
        return Result(False, "作業ラインを選んでください。", REFUSE_NO_LINE, changed=False)
    now_plan = current(conn, lot)
    if now_plan is not None and line_key(now_plan.line) == line_key(name):
        return Result(True, f"ロット {lot} はもう {now_plan.line} の予定です"
                            f"({short_time(now_plan.sent_at)} {now_plan.terminal})。",
                      plan_id=now_plan.plan_id, changed=False)
    pid = _insert(conn, lot, name, PLANNED, terminal=terminal, sender=sender, summary=summary)
    said = f"ロット {lot} を {name} の予定に入れました。{name} の端末のロット検索に知らせが出ます。"
    if now_plan is not None:
        said = (f"ロット {lot} を {now_plan.line} から {name} の予定に入れ直しました。"
                f"{name} の端末のロット検索に知らせが出ます。")
    return Result(True, said, plan_id=pid)


def cancel(conn: sqlite3.Connection, lot_no: str, *, terminal: str,
           sender: str = "") -> Result:
    """ロットを予定から外す(取り消しの行を足す)。"""
    lot = normalize_lot(lot_no)
    now_plan = current(conn, lot)
    if now_plan is None:
        return Result(False, f"ロット {lot or '(なし)'} はどのラインの予定にも入っていません。",
                      REFUSE_NOT_PLANNED, changed=False)
    pid = _insert(conn, lot, now_plan.line, CANCELLED, terminal=terminal, sender=sender,
                  summary=now_plan.summary)
    return Result(True, f"ロット {lot} を {now_plan.line} の予定から外しました。", plan_id=pid)


def finish(conn: sqlite3.Connection, plan_id: str, *, lines: Iterable[str], leader: bool = False,
           terminal: str, sender: str = "") -> Result:
    """**作業済みにする**(作業済みの行を足す。VER4.8.109)。

    現場の声:「どれから作業するかは不明確で、予定に入れられた順に作業するとも限りません。残し方に困っています」
    → 確認済みは畳んで残し、終わったものは本人が「作業済み」で消す。共有へ送るので、そのラインの
    ほかの端末の予定欄からも消える(押し忘れても、送ってから `LIST_DAYS` 日で出なくなる)。

    押せるのは、その予定のラインを持つ端末と作業長。予定が入れ直された・外された・もう作業済みなら断る
    (古い画面から押した)。
    """
    pid = str(plan_id or "").strip()
    found = next((e for e in _events(conn, " AND 予定ID = ?", (pid,))), None) if pid else None
    if found is None:
        return Result(False, "その予定ロットは見つかりません。画面を更新してください。",
                      REFUSE_NOT_PLANNED, changed=False)
    now_plan = current(conn, found.lot_no)
    if now_plan is None or now_plan.plan_id != pid:
        latest = _latest_by_lot(_events(conn, " AND LotNo = ?", (normalize_lot(found.lot_no),))).get(
            normalize_lot(found.lot_no))
        if latest is not None and latest.state == DONE:
            return Result(True, f"ロット {found.lot_no} はもう作業済みです"
                                f"({short_time(latest.sent_at)} {latest.terminal})。",
                          plan_id=latest.plan_id, changed=False)
        return Result(False, f"ロット {found.lot_no} の予定は入れ直されたか外されています。画面を更新してください。",
                      REFUSE_NOT_PLANNED, changed=False)
    keys = {line_key(n) for n in lines if line_key(n)}
    if not leader and line_key(now_plan.line) not in keys:
        return Result(False, f"ロット {now_plan.lot_no} は {now_plan.line} の予定です。"
                             f"作業済みにできるのは {now_plan.line} の端末と作業長です。",
                      REFUSE_NOT_MINE, changed=False)
    new_id = _insert(conn, now_plan.lot_no, now_plan.line, DONE, terminal=terminal, sender=sender,
                     summary=now_plan.summary)
    return Result(True, f"ロット {now_plan.lot_no} を作業済みにしました({now_plan.line} の予定欄から消えます)。",
                  plan_id=new_id)


def last_done(conn: sqlite3.Connection, lot_no: str) -> Optional[Plan]:
    """そのロットのいちばん新しい行が「作業済み」なら、その行(倉庫連携の作業予定ラインのカードに出す)。"""
    lot = normalize_lot(lot_no)
    if not lot:
        return None
    latest = _latest_by_lot(_events(conn, " AND LotNo = ?", (lot,))).get(lot)
    return latest if latest is not None and latest.state == DONE else None


def _insert(conn: sqlite3.Connection, lot: str, line: str, state: str, *,
            terminal: str, sender: str, summary: str) -> str:
    pid = uuid.uuid4().hex
    with conn:
        conn.execute(
            f"INSERT INTO {TABLE} (予定ID, LotNo, ライン, 状態, 送った端末, 送った人, 送った日時, 要約)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, lot, line, state, terminal or "", sender or "", now_db_string(), summary or ""))
    log.info("予定ロット: Lot=%s ライン=%s 状態=%s 端末=%s 予定ID=%s", lot, line, state, terminal, pid)
    return pid


def mark_seen(conn: sqlite3.Connection, plan_id: str, *, opened: bool = False) -> bool:
    """この端末で確認した(知らせを消す)。開いて確認したなら `opened`。無い予定IDなら False。"""
    pid = str(plan_id or "").strip()
    if not pid:
        return False
    try:
        if conn.execute(f"SELECT 1 FROM {TABLE} WHERE 予定ID = ?", (pid,)).fetchone() is None:
            return False
        with conn:
            conn.execute(
                f"INSERT INTO {SEEN_TABLE} (予定ID, 確認日時, 開いた) VALUES (?, ?, ?)"
                " ON CONFLICT(予定ID) DO UPDATE SET 開いた = MAX(開いた, excluded.開いた)",
                (pid, now_db_string(), 1 if opened else 0))
    except sqlite3.Error as exc:
        log.warning("予定ロットの確認を書けませんでした: %s", exc)
        return False
    return True


def unsent_ids(conn: sqlite3.Connection) -> set[str]:
    """この端末で入れた・外したが、まだ共有へ届いていない予定(予定ID)。"""
    from . import outbox_sync, sync_writeback
    for spec in sync_writeback.WRITEBACK_SPECS:
        if spec.sqlite_table == TABLE:
            try:
                return {str(r["予定ID"]) for r in outbox_sync.pending_rows(conn, spec)}
            except sqlite3.Error:
                return set()
    return set()


# ------------------------------------------------------------------
# 共有から受け取る(ロット検索の見張り)
# ------------------------------------------------------------------
# 予定ロットだけを取り込み直したときの、取り込み元の姿(`refresh_if_changed`)
_seen_stamp: dict[str, tuple[int, int]] = {}
# 取り込み直しは1つずつ(ロット検索の見張りと、心拍の背景の取り込みが同時に走らない)
_refresh_lock = threading.Lock()
# 心拍からの背景の取り込みの間隔(秒)。ロット検索の見張り(15秒)と同じ
BACKGROUND_MIN_SEC = 15.0
_background_at = float("-inf")


def refresh_if_changed(conn: sqlite3.Connection, path: Optional[Path] = None) -> bool:
    """取り込み元が前に見たときから変わっていれば、**予定ロットだけ**取り込み直す。取り込んだら True。

    ロット検索の画面は共有を取り込み直さない(倉庫連携の見張りだけが取り込んでいた)。ラインの端末は
    ロット検索を開いたままにするので、ここで見張る(`board_usage.refresh_if_changed` と同じ形)。
    変わっていなければファイルの姿(大きさ・更新時刻)を見るだけで帰る。**この端末でまだ送っていない
    分があれば入れ替えない**(総入れ替えなので消えてしまう)。
    """
    if not _refresh_lock.acquire(blocking=False):
        return False                                 # ほかで取り込み直している最中
    try:
        return _refresh_if_changed(conn, path)
    finally:
        _refresh_lock.release()


def _refresh_if_changed(conn: sqlite3.Connection, path: Optional[Path]) -> bool:
    from . import import_specs, sync_import, sync_sources, sync_writeback
    path = path or sync_sources.find_material_db()
    if path is None:
        return False
    stamp = sync_sources.source_stamp(path)
    if stamp is None or _seen_stamp.get(str(path)) == stamp:
        return False
    if TABLE in sync_writeback._unsent_writeback_tables(conn):
        return False
    result = sync_import.import_tables(
        conn, path, {TABLE: import_specs.IMPORT_SPECS[TABLE]},
        required=import_specs.REQUIRED_KEY_COLUMNS,
        blank_is_missing=import_specs.BLANK_IS_MISSING,
        optional=import_specs.OPTIONAL_TABLES,
        fallbacks=import_specs.NULL_FALLBACKS)
    # 読めなかった表は次も読めない(共有にまだ無いなど)。同じ姿で何度も読みに行かない
    _seen_stamp[str(path)] = stamp
    if TABLE not in result.imported:
        return False
    log.info("予定ロットを取り込み直しました: %s件", result.imported[TABLE])
    return True


def refresh_in_background(*, now: Optional[float] = None) -> bool:
    """**心拍から、予定ロットの取り込み直しを背景で走らせる**(マスタ反映もれ #13。VER4.8.106)。走らせたら True。

    レールの「予N」は、ロット検索の画面でしか共有を見に行かなかったので、ほかの画面にいると
    届いた予定が印に出なかった。心拍(15秒ごと)のついでに見に行く。変わっていなければ
    ファイルの姿を見るだけで帰る(`refresh_if_changed`)。間を空け、同時に2つは走らせない。
    """
    import time as _time
    global _background_at
    moment = _time.monotonic() if now is None else now
    if moment - _background_at < BACKGROUND_MIN_SEC or _refresh_lock.locked():
        return False
    _background_at = moment

    def runner() -> None:
        from . import db
        try:
            with db.connect() as conn:
                refresh_if_changed(conn)
        except Exception:                            # noqa: BLE001 - 背景処理なので握る
            log.exception("予定ロットの取り込み直しで例外(手元の予定で続けます)")

    threading.Thread(target=runner, daemon=True, name="lot-plans-refresh").start()
    return True


def forget_seen_stamp() -> None:
    """試験用。次の `refresh_if_changed` で必ず見に行く。"""
    _seen_stamp.clear()


# ------------------------------------------------------------------
# 予定ロットを開いたときのパレット(VER4.8.92)
# ------------------------------------------------------------------
# 現場の声:「予定に届いたロットを開いた場合に、倉庫連携で発注したパレットサイズを検索して
# パレットが決定した状態でボード選定まで移行させてください」
#
# 発注(資材パレット注文管理)の 幅・丈 は**ロットの板の寸法**で、パレットの寸法ではない。
# パレットは 発注コード で引く(PalletMaster の コード は1行に1つ。同じ寸法・記号でも 組・台・枚 で
# コードが違うので、コードで引けば**発注した行そのもの**になる)。コードで引けないとき
# (マスタのコードが変わった・コードの欄に別の字が入っている)は、品名「業界　記号　幅x丈」で引く。
ORDER_TABLE = config.TBL_WAREHOUSE_ORDER
# 蓋(枚)の発注はパレットではない(EX・上下共用の2行目)
LID_UNIT = "枚"
# 品名 = 業界　記号　幅x丈(`lot_service.build_hinmei`)。記号は空のこともある
_HINMEI = re.compile(r"^(?P<industry>[^　]*)　(?P<symbol>[^　]*)　(?P<w>\d+)x(?P<l>\d+)$")


@dataclass
class OrderedPallet:
    """このロットに倉庫連携で発注したパレット(PalletMaster の1行)。"""

    width: int
    length: int
    symbol: str
    row_id: int
    code: str
    hinmei: str
    sent_at: str
    by_name: bool = False      # 発注コードで引けず、品名の寸法で引いた
    # パレットマスタに無い寸法(コードの欄が「単価表のマトリックス」など)。寸法だけ分かる(row_id 0)
    size_only: bool = False


def ordered_pallet(conn: sqlite3.Connection, lot_no: str) -> tuple[Optional[OrderedPallet], str]:
    """このロットに発注したパレット(**新しい発注から**見る)。見つからなければ (None, 理由)。

    取り消した発注・蓋(枚)・EX の合図(品名 EX)は見ない。
    """
    lot = normalize_lot(lot_no)
    if not lot:
        return None, "ロット番号がありません。"
    try:
        rows = conn.execute(
            f'SELECT 品名, 発注コード, 単位, 登録日時 FROM "{ORDER_TABLE}"'
            " WHERE UPPER(TRIM(LotNo)) = ? AND (取り消し済 IS NULL OR 取り消し済 <> '1')"
            " ORDER BY 登録日時 DESC, 管理番号 DESC", (lot,)).fetchall()
    except sqlite3.Error as exc:
        log.warning("発注を読めません: %s", exc)
        return None, "倉庫連携の発注を読めませんでした。"
    if not rows:
        return None, (f"Lot {lot} の倉庫連携の発注がありません"
                      "(まだ届いていないか、取り消されています)。")
    kinds: list[str] = []
    for hinmei, code, unit, sent_at in rows:
        hinmei, code, unit = (hinmei or "").strip(), (code or "").strip(), (unit or "").strip()
        if unit == LID_UNIT:
            kinds.append("蓋")
            continue
        if hinmei == "EX":
            kinds.append("EX")
            continue
        hit = _pallet_by_code(conn, code) if code and code != "---" else None
        by_name = False
        if hit is None:
            hit, by_name = _pallet_by_hinmei(conn, hinmei, unit), True
        if hit is not None:
            return OrderedPallet(width=int(hit["幅"]), length=int(hit["丈"]),
                                 symbol=(hit["記号"] or "").strip(), row_id=int(hit["管理番号"]),
                                 code=(hit["コード"] or "").strip(), hinmei=hinmei,
                                 sent_at=sent_at or "", by_name=by_name), ""
        # マスタに無い寸法でも、品名に寸法があれば**寸法だけ**決められる(ボード選定には寸法で足りる)
        named = _HINMEI.match(hinmei)
        if named:
            return OrderedPallet(width=int(named["w"]), length=int(named["l"]),
                                 symbol=named["symbol"].strip(), row_id=0, code=code, hinmei=hinmei,
                                 sent_at=sent_at or "", by_name=True, size_only=True), ""
        kinds.append(hinmei or code or "(空)")
    if kinds and all(k in ("EX", "蓋") for k in kinds):
        what = "・".join(dict.fromkeys(
            "EX の合図(品名 EX)" if k == "EX" else "蓋(枚)" for k in kinds))
        return None, f"Lot {lot} の発注は {what} だけなので、パレットは決まっていません。"
    return None, (f"Lot {lot} の発注({'・'.join(k for k in kinds if k not in ('EX', '蓋'))})の"
                  "パレットが、パレットマスタに見つかりません。")


def _pallet_by_code(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    conn_row = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM PalletMaster WHERE TRIM(コード) = ? ORDER BY 管理番号",
                            (code,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.row_factory = conn_row


def _pallet_by_hinmei(conn: sqlite3.Connection, hinmei: str, unit: str) -> Optional[sqlite3.Row]:
    """品名「業界　記号　幅x丈」で引く。記号は品名に出さないものがある(`filter_pallet_symbol`)ので、
    マスタの記号を同じ形にして比べる。同じ単位の行を先に、その中で管理番号の若い順。"""
    from .lot_service import filter_pallet_symbol
    found = _HINMEI.match(hinmei)
    if not found:
        return None
    conn_row = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM PalletMaster WHERE 幅 = ? AND 丈 = ? ORDER BY 管理番号",
                            (int(found["w"]), int(found["l"]))).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.row_factory = conn_row
    same = [r for r in rows
            if (r["業界"] or "").strip() == found["industry"].strip()
            and filter_pallet_symbol(r["記号"] or "") == found["symbol"].strip()]
    same.sort(key=lambda r: 0 if (r["単位"] or "").strip() == unit else 1)
    return same[0] if same else None
