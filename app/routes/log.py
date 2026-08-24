"""選定ログ (旧 `mpMain` の3ページ目 `txtUserLog`)

パレット検索・ボード自動選定が**なぜその結果になったか**を1行ずつ見る画面。
現場が自分で原因を追えるようにするためのもので、
「候補が出てこない」「選んだボードがおかしい」の問い合わせはここを見れば大半が解決する。

【tkinter版からの変更】
行に**種別**(決定 / 除外 / 情報 / 警告)を付けて絞り込めるようにした。
除外理由は数十行出ることがあり、その中から「決定」の行を探すのは
本文を読まないとできなかった。文言そのものは変えない。
"""
from __future__ import annotations

import re

from flask import Blueprint, current_app, jsonify, render_template, request

from packaging_tool import selection_log_store
from packaging_tool.user_log import LogEntry, get_user_log

from ..shell import shell_context

bp = Blueprint("log", __name__)

# 1回の取得で返す最大行数。選定1回で数百行出ることがあるので上限を置く
MAX_ROWS = 500

# 行の種別。**文言は変えず**、先頭の形で分類するだけ。
# 分類できなければ "info" に倒す(勝手に警告扱いしない)
# 画面に出す種別名。JS側 (`views/log.js`) と同じ文言にする
KIND_LABEL = {"decide": "決定", "exclude": "除外", "warn": "警告", "info": "情報"}

KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("exclude", r"^\s*[×✕]?\s*除外"),
    ("exclude", r"範囲外|桁数偶数|属性不一致"),
    ("warn", r"未取り込み|取得できません|引けません|失敗"),
    ("decide", r"採用|決定|選定しました|PASS\d"),
)


def classify(entry: LogEntry) -> str:
    """行の種別を推測する。表示の絞り込みにだけ使う。

    `emphasis`(VBA `UserLog` の第2引数 True = 節目の行)は
    そのまま強調表示に使うので、種別とは別に返す。
    """
    for kind, pattern in KIND_PATTERNS:
        if re.search(pattern, entry.text):
            return kind
    return "info"


def _serialize(entry: LogEntry) -> dict:
    return {
        "seq": entry.seq,
        "text": entry.text,
        "emphasis": bool(entry.emphasis),
        "kind": classify(entry),
        # パレット/ボードどちらの途中で出た行か。空ならどちらでもない
        # (アングル・手動操作など)。事実として`UserLog`側で付けている
        # (種別=kindのような表示都合の推測ではない)
        "area": entry.area,
        # 時刻は行の左に小さく出す。身元は**大きく出さない**が、
        # 後から追えるように渡しておく(画面では行の説明に入る)
        "at": entry.at,
        "who": entry.who(),
    }


def _stored(row: dict) -> dict:
    """日ごとのファイルから読んだ行を、画面と同じ形にする。

    種別は保存していない ── 分類は**表示の都合**であって事実ではないので、
    読むときに毎回付ける(規則を直したら過去の行にも新しい規則が効く)。
    """
    entry = LogEntry(text=str(row.get("text", "")),
                     emphasis=bool(row.get("emphasis")),
                     at=str(row.get("at", "")),
                     login_id=str(row.get("login_id", "")),
                     pc_name=str(row.get("pc_name", "")),
                     area=str(row.get("area", "")))
    return _serialize(entry)


@bp.get("/log")
def page():
    log = get_user_log()
    return render_template(
        "log.html",
        initial=[_serialize(e) for e in log.entries[-MAX_ROWS:]],
        kind_label=KIND_LABEL,
        last_seq=log.last_seq,
        **shell_context("log", badges=_badges()),
    )


@bp.get("/api/log")
def fetch():
    """`?since=<seq>` より後の行だけ返す。

    連番が跳んでいたら(古い行が上限で捨てられていたら)`reset` を立て、
    画面側は全部描き直す。取りこぼしたまま continue すると、
    表示と実体がずれたことに誰も気づけない。
    """
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0

    log = get_user_log()
    entries = log.entries_since(since)
    reset = bool(entries) and entries[0].seq > since + 1

    if len(entries) > MAX_ROWS:
        entries = entries[-MAX_ROWS:]
        reset = True

    return jsonify({
        "rows": [_serialize(e) for e in entries],
        "last_seq": log.last_seq,
        "total": len(log),
        "reset": reset,
    })


@bp.get("/api/log/days")
def log_days():
    """日ごとのまとめ。**開く前に、その日に何があったかが分かる。**"""
    return jsonify({
        "today": selection_log_store.today(),
        "days": [d.to_dict() for d in selection_log_store.summaries()],
    })


@bp.get("/api/log/day/<date>")
def log_day(date: str):
    """その日の行。終了しても残っているので、後から追える。"""
    rows = selection_log_store.read_day(date)
    return jsonify({
        "date": date,
        "rows": [_stored(r) for r in rows],
        "shown": len(rows),
    })


@bp.post("/api/log/clear")
def clear():
    """VBA `ClearUserLog` の移植。

    **消えるのは画面の分だけ。** 日ごとのファイルは残る ── 後から
    追えるようにするのが目的なので、画面を片付けたら消える、では困る。
    """
    get_user_log().clear()
    current_app.logger.info("選定ログを消去しました")
    return jsonify({"ok": True, "last_seq": get_user_log().last_seq,
                    "message": "選定ログを消去しました"})


def _badges() -> dict[str, tuple[str, str]]:
    """レールに出す件数。行く前に「そこに何があるか」を示す。"""
    count = len(get_user_log())
    return {"log": (str(count), "todo")} if count else {}
