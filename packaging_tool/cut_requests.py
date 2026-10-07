"""切断依頼を倉庫へ送る(現場 → 倉庫)

現場の声:「現場側で切断依頼書を印刷して紙を渡していた。倉庫に送れるならそうしたい」。

【何を送るのか ── 紙面そのもの】
現場がプレビューで見た切断依頼書(手で直した内容も入れたもの)を、**そのまま固めて**
送る(`printing.Report` の題名・ページ・紙の設定)。倉庫は同じ紙面を開いて印刷する。
あとで資材選択をやり直しても、別のロットを検索しても、送った中身は変わらない
(現場の「送った切断依頼」からも開き直して印刷できる)。

【どう届けるのか ── 足すだけの2つの表】
    切断依頼       … 送った紙面。1回送るごとに1行(書き直さない)
    切断依頼状態   … 受け取った / 切った / 取り消し。押すたびに1行(書き直さない)
どちらもコメントと同じく、書き戻しで共有へ送り、取り込みで全端末が受け取る。
**書き直さない**ので、2つの端末が同時に押しても行が上書きし合わない。

【状態の決め方 ── どの端末でも同じ答え】
`切断依頼状態` の行を日時の順に並べ、決まった規則で読み進める(`_fold`)。

    未読 ──受け取った──→ 受け取った ──切った──→ 切った
     └──取り消し──→ 取り消し(倉庫が受け取る前だけ)

順番に合わない行(受け取ったあとの取り消しなど)は**読み飛ばす**。同時に押されても、
どの端末も同じ順で読むので同じ状態になる。間に合わなかった取り消しは、押した端末に
「先に倉庫が受け取っていました」と出す。

【誰が何をできるか】
    送る       … 現場モード(資材選択の切断依頼のプレビューから)
    受け取った … 倉庫(資材モード)で**開いたとき**に付く
    切った     … 倉庫(資材モード)
    取り消し   … 送った端末だけ、倉庫が受け取る前だけ
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config, db, printing
from .logging_utils import get_logger

log = get_logger("cut_requests")

TABLE = config.TBL_CUT_REQUEST
EVENT_TABLE = config.TBL_CUT_REQUEST_EVENT

SIDE_FIELD = "現場"
SIDE_MATERIAL = "倉庫"

# 状態。**言葉は1か所に**(画面はこの言葉と `state_kind` をそのまま出す)
SENT = "未読"
RECEIVED = "受け取った"
CUT = "切った"
CANCELLED = "取り消し"
STATE_KIND = {SENT: "todo", RECEIVED: "warn", CUT: "ok", CANCELLED: "off"}

# 1回に送れる紙面の大きさ(文字数)。切断依頼書は数十KB。壊れた紙面で共有を埋めない
MAX_REPORT_CHARS = 2_000_000
# 一覧に出す件数(新しい順)
LIST_LIMIT = 100

# 断りの種類(画面は文言から推し量らない)
REFUSE_NOT_FOUND = "not_found"
REFUSE_STATE = "state"
REFUSE_NOT_MINE = "not_mine"
REFUSE_TOO_BIG = "too_big"
REFUSE_NEED_CONFIRM = "need_confirm"


@dataclass
class Result:
    ok: bool
    message: str
    reason: str = ""
    request_id: str = ""


@dataclass
class Request:
    """送った切断依頼1件と、いまの状態。"""

    request_id: str
    lot_no: str
    summary: str
    terminal: str
    sent_at: str
    title: str = ""
    replaces: str = ""
    state: str = SENT
    # (状態, 端末, 日時) の古い順。読み飛ばした行は入れない
    history: list = field(default_factory=list)
    # 押したが順番に合わず効かなかったもの (状態, 端末, 日時)
    late: list = field(default_factory=list)
    replaced_by: str = ""

    def by(self, state: str) -> Optional[tuple[str, str]]:
        """その状態にした (端末, 日時)。"""
        for got, who, at in self.history:
            if got == state:
                return who, at
        return None


def _same(a: str, b: str) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


# ------------------------------------------------------------------
# 状態を決める
# ------------------------------------------------------------------
_ORDER = {RECEIVED: 1, CUT: 2, CANCELLED: 3}


def _fold(req: Request, events: list[tuple[str, str, str]]) -> None:
    """状態の行を古い順に読み、いまの状態を決める(モジュールの説明「状態の決め方」)。"""
    state = SENT
    for got, who, at in sorted(events, key=lambda e: (e[2], _ORDER.get(e[0], 9), e[1])):
        if got == RECEIVED and state == SENT:
            state = RECEIVED
        elif got == CUT and state in (SENT, RECEIVED):
            if state == SENT:                  # 開かずに切った(受け取ったことにもなる)
                req.history.append((RECEIVED, who, at))
            state = CUT
        elif got == CANCELLED and state == SENT:
            state = CANCELLED
        else:
            req.late.append((got, who, at))
            continue
        req.history.append((got, who, at))
    req.state = state


def _load(conn: sqlite3.Connection, where: str = "", params: tuple = (),
          *, limit: Optional[int] = None) -> list[Request]:
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT 依頼ID, LotNo, 要約, 送った端末, 送った日時, 題名, 差し替え元 FROM {TABLE}"
            f" WHERE 依頼ID <> ''{where} ORDER BY 送った日時 DESC, 管理番号 DESC"
            + (f" LIMIT {int(limit)}" if limit else ""), params).fetchall()
    except sqlite3.Error:
        return []                       # 表がまだ無い(古い手元DB)
    out = [Request(request_id=r["依頼ID"], lot_no=r["LotNo"], summary=r["要約"],
                   terminal=r["送った端末"], sent_at=r["送った日時"], title=r["題名"],
                   replaces=r["差し替え元"] or "") for r in rows]
    if not out:
        return out
    by_id = {r.request_id: r for r in out}
    events: dict[str, list] = {r.request_id: [] for r in out}
    marks = ",".join("?" for _ in by_id)
    for row in conn.execute(
            f"SELECT 依頼ID, 状態, 端末, 日時 FROM {EVENT_TABLE} WHERE 依頼ID IN ({marks})",
            tuple(by_id)):
        events[row["依頼ID"]].append((row["状態"], row["端末"], row["日時"]))
    for req in out:
        _fold(req, events[req.request_id])
    # 差し替えた側から、差し替えられた側へ印を付ける(一覧に「差し替え済み」)
    for row in conn.execute(
            f"SELECT 依頼ID, 差し替え元 FROM {TABLE} WHERE 差し替え元 IN ({marks})",
            tuple(by_id)):
        by_id[row["差し替え元"]].replaced_by = row["依頼ID"]
    return out


def get(conn: sqlite3.Connection, request_id: str) -> Optional[Request]:
    found = _load(conn, " AND 依頼ID = ?", (str(request_id or ""),))
    return found[0] if found else None


def recent(conn: sqlite3.Connection, *, limit: int = LIST_LIMIT) -> list[Request]:
    """一覧に出すもの。**まだ終わっていないもの**を先に、そのあと新しい順。"""
    found = _load(conn, limit=limit)
    open_first = {SENT: 0, RECEIVED: 0, CUT: 1, CANCELLED: 1}
    return sorted(found, key=lambda r: open_first.get(r.state, 1))


def open_for_lot(conn: sqlite3.Connection, lot_no: str) -> list[Request]:
    """そのロットで、まだ終わっていない(未読・受け取った)依頼。新しい順。"""
    return [r for r in _load(conn, " AND LotNo = ?", (str(lot_no or ""),))
            if r.state in (SENT, RECEIVED) and not r.replaced_by]


def unread_count(conn: sqlite3.Connection) -> int:
    """倉庫がまだ開いていない依頼の数(レールと一覧の見出しに出す)。"""
    return sum(1 for r in _load(conn) if r.state == SENT)


# ------------------------------------------------------------------
# 紙面
# ------------------------------------------------------------------
def _freeze(report: printing.Report) -> str:
    """送る紙面。**直せる欄は直せない形にして**固める(倉庫では読むだけ)。"""
    setup = report.setup
    sheets = [s.replace(' contenteditable="true"', "") for s in report.sheets]
    return json.dumps({"title": report.title, "sheets": sheets,
                       "setup": {"paper": setup.paper, "orientation": setup.orientation,
                                 "margin_mm": setup.margin_mm,
                                 "extra_css": setup.extra_css}},
                      ensure_ascii=False)


def report_of(conn: sqlite3.Connection, request_id: str) -> Optional[printing.Report]:
    """送った紙面を開き直す。無ければ None。"""
    try:
        row = conn.execute(f"SELECT 紙面 FROM {TABLE} WHERE 依頼ID = ?",
                           (str(request_id or ""),)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        data = json.loads(row[0] or "{}")
        setup = data.get("setup") or {}
        return printing.Report(
            title=str(data.get("title") or "切断依頼書"),
            sheets=[str(s) for s in data.get("sheets") or []],
            setup=printing.PageSetup(
                paper=str(setup.get("paper") or "A4"),
                orientation=str(setup.get("orientation") or printing.LANDSCAPE),
                margin_mm=float(setup.get("margin_mm") or printing.SAFE_MARGIN_MM),
                extra_css=str(setup.get("extra_css") or "")))
    except (ValueError, TypeError) as exc:
        log.warning("送った切断依頼の紙面を読めません: %s %s", request_id, exc)
        return None


# ------------------------------------------------------------------
# 送る・状態を付ける
# ------------------------------------------------------------------
def send(conn: sqlite3.Connection, report: printing.Report, *, lot_no: str,
         summary: str, terminal: str, replace: bool = False,
         again: bool = False) -> Result:
    """紙面を固めて送る(手元に足す。共有へは呼び手が書き戻しで送る)。

    同じロットにまだ終わっていない依頼があれば、**黙って2枚にしない**:
      - 倉庫がまだ開いていない … `replace` で前のものを取り消して差し替える
      - 倉庫がもう受け取った   … `again` でもう1枚(差し替えにはしない)
    どちらも無ければ、訊く文を `need_confirm` で返す。
    """
    paper = _freeze(report)
    if len(paper) > MAX_REPORT_CHARS:
        return Result(False, "紙面が大きすぎて送れません。", REFUSE_TOO_BIG)
    current = open_for_lot(conn, lot_no)
    replaces = ""
    if current:
        last = current[0]
        mine = _same(last.terminal, terminal)
        if last.state == SENT and replace:
            if not mine:
                return Result(False, f"前の依頼は {last.terminal} が送ったものなので、ここから"
                                     "差し替えられません。", REFUSE_NOT_MINE)
            replaces = last.request_id
        elif last.state == RECEIVED and again:
            pass
        elif last.state == SENT and not mine and again:
            pass                       # ほかの端末の分は残して、もう1枚
        elif last.state == SENT and not mine:
            # **差し替えを訊かない。** 差し替えられるのは送った端末だけなので、訊いて
            # 「送る」を押させてから断ることになる(通し点検: デスクトップ版で
            # 「差し替えますか？」→ 送る → 「ここからは差し替えられません」)
            return Result(False, f"このロット({lot_no})の切断依頼は {last.terminal} が "
                                 f"{last.sent_at} に送ってあり、倉庫はまだ開いていません"
                                 "(差し替えは送った端末からだけです)。これを別にもう1枚送りますか？"
                                 "(前のものはそのまま残ります)", REFUSE_NEED_CONFIRM,
                          last.request_id)
        elif last.state == SENT:
            return Result(False, f"このロット({lot_no})の切断依頼は {last.sent_at} にもう送って"
                                 "あり、倉庫はまだ開いていません。前のものを取り消して、"
                                 "これに差し替えますか？", REFUSE_NEED_CONFIRM, last.request_id)
        else:
            who, at = last.by(RECEIVED) or ("", "")
            return Result(False, f"このロット({lot_no})の切断依頼は、倉庫 {who} が {at} に"
                                 "もう受け取っています。これを別にもう1枚送りますか？"
                                 "(前のものはそのまま残ります)", REFUSE_NEED_CONFIRM,
                          last.request_id)
    rid = uuid.uuid4().hex
    now = db.now_db_string()
    with conn:
        if replaces:
            conn.execute(f"INSERT INTO {EVENT_TABLE} (依頼ID, 状態, 端末, 側, 日時)"
                         " VALUES (?, ?, ?, ?, ?)", (replaces, CANCELLED, terminal, SIDE_FIELD, now))
        conn.execute(
            f"INSERT INTO {TABLE} (依頼ID, LotNo, 要約, 送った端末, 送った日時, 題名, 紙面, 差し替え元)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, lot_no, summary, terminal, now, report.title, paper, replaces))
    log.info("切断依頼を送りました: Lot=%s 依頼ID=%s 端末=%s%s", lot_no, rid, terminal,
             f" (差し替え元 {replaces})" if replaces else "")
    said = f"切断依頼(Lot {lot_no})を倉庫へ送りました。"
    if replaces:
        said += "前に送ったものは取り消しました。"
    return Result(True, said, request_id=rid)


def mark(conn: sqlite3.Connection, request_id: str, state: str, *,
         terminal: str, side: str) -> Result:
    """状態を1つ進める。**順番に合わなければ断る**(手元で分かる範囲で)。"""
    req = get(conn, request_id)
    if req is None:
        return Result(False, "その切断依頼は見つかりません。画面を更新してください。",
                      REFUSE_NOT_FOUND)
    if state == CANCELLED:
        if not _same(req.terminal, terminal):
            return Result(False, f"取り消せるのは送った端末({req.terminal})だけです。",
                          REFUSE_NOT_MINE)
        if req.state != SENT:
            return Result(False, _state_why(req, "取り消せません"), REFUSE_STATE)
    elif state == RECEIVED:
        if req.state != SENT:
            return Result(True, "")              # もう受け取っている。何もしない
    elif state == CUT:
        if req.state not in (SENT, RECEIVED):
            return Result(False, _state_why(req, "切ったにできません"), REFUSE_STATE)
    else:
        return Result(False, f"知らない状態です: {state}", REFUSE_STATE)
    with conn:
        conn.execute(f"INSERT INTO {EVENT_TABLE} (依頼ID, 状態, 端末, 側, 日時)"
                     " VALUES (?, ?, ?, ?, ?)",
                     (req.request_id, state, terminal, side, db.now_db_string()))
    log.info("切断依頼を「%s」にしました: Lot=%s 依頼ID=%s 端末=%s(%s)",
             state, req.lot_no, req.request_id, terminal, side)
    words = {CANCELLED: "取り消しました", RECEIVED: "受け取りました", CUT: "切ったにしました"}
    return Result(True, f"切断依頼(Lot {req.lot_no})を{words[state]}。",
                  request_id=req.request_id)


def _state_why(req: Request, verb: str) -> str:
    if req.state == RECEIVED:
        who, at = req.by(RECEIVED) or ("", "")
        return f"倉庫 {who} が {at} にもう受け取っているので{verb}。"
    if req.state == CUT:
        who, at = req.by(CUT) or ("", "")
        return f"倉庫 {who} が {at} にもう切っているので{verb}。"
    if req.state == CANCELLED:
        return f"もう取り消されているので{verb}。"
    return f"{verb}。"


# ------------------------------------------------------------------
# 画面に渡す形
# ------------------------------------------------------------------
def state_text(req: Request) -> str:
    """状態の言い方。「受け取った(倉庫 SOUKO 10/05 14:22)」。"""
    if req.state == SENT:
        return "倉庫はまだ開いていません"
    who_at = req.by(req.state)
    text = req.state
    if who_at:
        side = "" if req.state == CANCELLED else "倉庫 "
        text += f"({side}{who_at[0]} {_short(who_at[1])})"
    return text


def _short(at: str) -> str:
    """'2026-10-05 14:22:31' → '10/05 14:22'。"""
    try:
        return f"{at[5:7]}/{at[8:10]} {at[11:16]}"
    except (TypeError, IndexError):
        return at or ""


def unsent_ids(conn: sqlite3.Connection) -> set[str]:
    """この端末で送った・押したが、まだ共有へ届いていない切断依頼(依頼ID)。

    依頼そのもの(紙面)と、状態(受け取った・切った・取り消し)のどちらかが
    残っていれば「未送信」。共有フォルダが見えない間に押した分が、相手に
    届いていないと分かるようにする(発注の「未送信」と同じ)。
    """
    from . import outbox_sync, sync_writeback
    out: set[str] = set()
    for spec in sync_writeback.WRITEBACK_SPECS:
        if spec.sqlite_table not in (TABLE, EVENT_TABLE):
            continue
        try:
            out |= {str(r["依頼ID"]) for r in outbox_sync.pending_rows(conn, spec)}
        except sqlite3.Error:
            continue
    return out


def to_dict(req: Request, *, terminal: str, material: bool,
            unsent: Optional[set] = None) -> dict[str, Any]:
    late = ""
    for got, who, at in req.late:
        if got == CANCELLED and _same(who, terminal):
            late = (f"{_short(at)} に取り消しを押しましたが、先に倉庫が受け取っていたため"
                    "取り消せませんでした。")
    return {
        "id": req.request_id, "lot_no": req.lot_no, "summary": req.summary,
        "terminal": req.terminal, "sent_at": req.sent_at, "sent_short": _short(req.sent_at),
        "title": req.title, "state": req.state, "state_kind": STATE_KIND.get(req.state, "todo"),
        "state_text": state_text(req),
        "mine": _same(req.terminal, terminal),
        "replaced": bool(req.replaced_by), "replaces": req.replaces,
        "late": late,
        "unsent": req.request_id in (unsent or set()),
        # できること。**画面で条件を組み立てない**
        "can_cut": material and req.state in (SENT, RECEIVED),
        "can_cancel": (not material and req.state == SENT
                       and _same(req.terminal, terminal)),
    }
