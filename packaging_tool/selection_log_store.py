"""選定ログを日ごとのファイルに残す

【なぜ要るのか】
選定ログはこれまで**プロセスの中だけ**にありました。終了すれば消えるので、
「先週のあのロット、なぜあの候補になったのか」を後から追えません。
現場から問い合わせが来るのは**たいてい後日**なので、そこで消えていると
確かめる手立てがどこにもない、ということになります。

【なぜ日ごとのファイルか】
「日付単位でまとめて見たい」がそのまま**ファイルの分け方**になります。
1日1ファイルなら、まとめるのに全部を読む必要がなく、古い日を捨てるのも
ファイルを消すだけです。DBの表にすると、取り込みや書き戻しと同じDBを
更にもう1か所から書くことになり、直列化の対象が増えます ── 選定ログは
**書いて読むだけ**で、業務の判断には一切使わないので、分けておきます。

1行1件の JSON(JSONL)です。壊れた行があっても、その行だけ飛ばせます。

【身元を持たせる理由】
どの端末の誰が動かしたときのログなのかが分からないと、後から追えません。
**画面には大きく出しません**(作業中に読むものではない)が、
データとしては1行ごとに持ちます。
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import app_config
from .logging_utils import get_logger

log = get_logger("selection_log_store")

# 置き場所。アプリのログと同じ根の下に、選定ログだけの部屋を作る
DIR_NAME = "selection"

# 残す日数。**黙って消さない**ために、消したことはログに書く。
# 60日あれば「先月のあれ」に答えられる
KEEP_DAYS = 60

# ファイル名の形。日付そのものなので、並べれば時系列になる
DATE_FORM = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 一度に読む上限。1日に数万行たまることがある
READ_LIMIT = 5000

_lock = threading.Lock()


def directory() -> Path:
    return app_config.local_dir("logs") / DIR_NAME


def path_for(date: str) -> Path:
    return directory() / f"{date}.jsonl"


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ==================================================================
# 書く
# ==================================================================
def append(entry: Any) -> None:
    """1行足す。**失敗しても業務を止めない。**

    ログが書けないことと、選定ができないことは別です。書けなければ
    アプリのログに1度だけ残して、選定はそのまま続けます。
    """
    try:
        record = {
            "at": entry.at,
            "login_id": entry.login_id,
            "pc_name": entry.pc_name,
            "text": entry.text,
            "emphasis": bool(entry.emphasis),
            "area": getattr(entry, "area", ""),
        }
        target = path_for(entry.at[:10] if entry.at else today())
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:                        # pragma: no cover - 環境依存
        log.warning("選定ログを残せませんでした: %s", exc)


# ==================================================================
# 読む
# ==================================================================
@dataclass
class DaySummary:
    """1日ぶんのまとめ。**開く前に、その日に何があったかが分かる。**"""

    date: str
    lines: int = 0
    # その日に動かした人(ログインID @ PC名)。**誰の分が混ざっているか**
    people: list[str] = field(default_factory=list)
    first_at: str = ""
    last_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "lines": self.lines, "people": self.people,
                "first_at": self.first_at, "last_at": self.last_at}


def days() -> list[str]:
    """残っている日。新しい順。"""
    try:
        found = [p.stem for p in directory().glob("*.jsonl")
                 if DATE_FORM.match(p.stem)]
    except OSError:
        return []
    return sorted(found, reverse=True)


def read_day(date: str, *, limit: int = READ_LIMIT) -> list[dict[str, Any]]:
    """その日の行。**壊れた行はその行だけ飛ばす。**"""
    if not DATE_FORM.match(date or ""):
        return []
    try:
        with open(path_for(date), encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def summarize(date: str) -> DaySummary:
    """1日ぶんを数える。中身は返さない(開いてから読む)。"""
    rows = read_day(date, limit=10 ** 9)
    found = DaySummary(date=date, lines=len(rows))
    if not rows:
        return found
    people: list[str] = []
    for row in rows:
        who = _who(row)
        if who and who not in people:
            people.append(who)
    found.people = people
    found.first_at = str(rows[0].get("at", ""))
    found.last_at = str(rows[-1].get("at", ""))
    return found


def _who(row: dict[str, Any]) -> str:
    login = str(row.get("login_id", "")).strip()
    pc = str(row.get("pc_name", "")).strip()
    if login and pc:
        return f"{login} @ {pc}"
    return login or pc


def summaries(dates: Optional[Iterable[str]] = None) -> list[DaySummary]:
    return [summarize(d) for d in (dates if dates is not None else days())]


# ==================================================================
# 片付け
# ==================================================================
def prune(keep_days: int = KEEP_DAYS) -> list[str]:
    """古い日を消す。**消したことはログに残す**(黙って消さない)。"""
    old = days()[keep_days:]
    removed: list[str] = []
    for date in old:
        try:
            path_for(date).unlink()
            removed.append(date)
        except OSError:                           # pragma: no cover
            continue
    if removed:
        log.info("選定ログの古い日を消しました(%s日ぶん残します): %s",
                 keep_days, ", ".join(removed))
    return removed
