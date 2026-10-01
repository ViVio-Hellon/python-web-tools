"""後から追えるログ ── 出力先の決め方と、エラー記録の読み出し

現場の声:「エラー等の後追いが現状できないと感じている。ログを残し
なぜなぜで分析できるようにしておいてほしい。ログ出力は設定でパス指定
できるようにする」。

書く側の仕掛け(操作番号・エラー番号・エラー記録)は `logging_utils`。
ここは次を持つ:

- **出力先**: 環境変数 > 設定画面 > 既定(この端末のローカル領域)。
  設定画面で指定したフォルダには、**端末名のフォルダを作ってその中に
  書く** ── 共有フォルダを全端末で指しても混ざらず、どの端末の
  ことかがフォルダ名で分かる。書けないフォルダは選ばせない。起動時に
  書けなければ(共有に届かない日)既定の場所に書き、理由を画面に出す
- **エラー記録の一覧と1件の中身**(設定画面「ログ」の面)。1件の中身には、
  同じ操作番号の行をログファイルから集めて添える ── 選定の細かい判断
  (DEBUG)まで含めて「その操作で何が起きたか」を通しで読める
- **なぜなぜ用の書き出し**: 1件を、起きたこと・いつ・どこで・直前の
  操作・中身・「なぜ」を書き込む欄、の形の文章にする(コピーして
  報告やなぜなぜ分析の紙にそのまま貼れる)
- **古いログの片付け**(黙って消さない。消したことはログに書く)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from . import config, logging_utils
from .logging_utils import get_logger

log = get_logger("trace_log")

# 動作ログ・取り込み診断を残す日数。「先々月のあれ」まで答えられる長さ
KEEP_DAYS = 90
# エラー記録を残す月数。1件ずつは小さいので長く持つ
KEEP_ERROR_MONTHS = 24

# 一覧に出す件数の上限
LIST_LIMIT = 200
# 1件の中身に添える、同じ操作番号の行の上限
OP_LINES_LIMIT = 400

REFUSE_NEED_PASSWORD = "need_password"
REFUSE_BAD_INPUT = "bad_input"

SOURCE_ENV = "env"
SOURCE_SETTING = "setting"
SOURCE_DEFAULT = "default"
SOURCE_LABEL = {
    SOURCE_ENV: "環境変数 PACKAGING_TOOL_LOG_DIR で決まっています(設定画面より優先)",
    SOURCE_SETTING: "設定画面で指定したフォルダ(の下の、この端末の名前のフォルダ)",
    SOURCE_DEFAULT: "既定(この端末のローカル領域)",
}


@dataclass
class Result:
    ok: bool
    message: str
    reason: str = ""


# ==================================================================
# 出力先
# ==================================================================
def _pc_name() -> str:
    from . import access_control
    name = access_control.current_identity().pc_name or "この端末"
    # フォルダ名に使えない文字は置き換える
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def configured_text() -> str:
    """設定画面に入っている値(空なら既定)。"""
    from . import user_settings
    value = user_settings.get(config.KEY_LOG_DIR)
    return value.strip() if isinstance(value, str) else ""


def folder_for(text: str) -> Path:
    """指定したフォルダの下の、この端末の書き先。"""
    return config.resolve_dir(text) / _pc_name()


def _writable(folder: Path) -> str:
    """書けるか試す。書けなければ理由(書ければ空)。"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".書けるか確認"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"{folder} に書けません({exc})"
    return ""


# 起動時に指定の出力先へ書けなかったときの理由
_startup_why = ""


def apply_log_dir() -> Path:
    """設定を読んで、ログの出力先を決める。起動時と、設定を変えたときに呼ぶ。"""
    global _startup_why
    before = config.log_dir()
    chosen: Optional[Path] = None
    _startup_why = ""
    text = "" if config.LOG_DIR_FROM_ENV else configured_text()
    if text:
        target = folder_for(text)
        why = _writable(target)
        if why:
            _startup_why = (f"{why}。既定の {config.LOG_DIR} に書いています"
                            "(共有フォルダに届かない日など)")
            log.warning("ログの出力先: %s", _startup_why)
        else:
            chosen = target
    after = chosen or config.LOG_DIR
    if after != before:
        # **前の場所にも、新しい場所にも、移ったことを残す。** 片方だけ
        # 見て「途中からログが無い」「途中から始まっている」と迷わないように
        log.info("ログの出力先を変えます: %s → %s", before, after)
    config._active_log_dir = chosen
    logging_utils.fallback_why = ""
    if after != before:
        log.info("ログの出力先をここに変えました(前は %s)", before)
    return after


def state() -> dict[str, Any]:
    """設定画面に出す、出力先のいまの様子。"""
    if config.LOG_DIR_FROM_ENV:
        source = SOURCE_ENV
    elif config._active_log_dir is not None:
        source = SOURCE_SETTING
    else:
        source = SOURCE_DEFAULT
    warn = logging_utils.fallback_why or _startup_why
    return {
        "path": str(config.log_dir()),
        "default": str(config.LOG_DIR),
        "configured": configured_text(),
        "source": source,
        "source_label": SOURCE_LABEL[source],
        "locked": config.LOG_DIR_FROM_ENV,
        "warn": warn,
        "pc": _pc_name(),
        "keep_days": KEEP_DAYS,
        "keep_months": KEEP_ERROR_MONTHS,
    }


def set_log_dir(text: str, password: str) -> Result:
    """ログの出力先を変える(空 = 既定に戻す)。

    置き場所なので、ほかの置き場所と同じく管理者パスワードが要る
    (ログは後から追う証拠。黙ってよそへ移されると追えなくなる)。
    **書けないフォルダは保存しない** ── 保存してから「書けない」と
    分かっても、その間のログはもう失われている。
    """
    from . import admin_password, user_settings
    if config.LOG_DIR_FROM_ENV:
        return Result(False, "いまは環境変数 PACKAGING_TOOL_LOG_DIR で決まっているので、"
                             "ここでは変えられません。", REFUSE_BAD_INPUT)
    if not admin_password.verify(str(password or "")):
        return Result(False, "ログの出力先を変えるには管理者パスワードが要ります。",
                      REFUSE_NEED_PASSWORD)
    text = (text or "").strip().strip('"')
    if text:
        if not Path(text).expanduser().is_absolute():
            return Result(False, "フォルダは C:\\… や \\\\サーバ\\共有\\… のように"
                                 "頭から書いてください。", REFUSE_BAD_INPUT)
        why = _writable(folder_for(text))
        if why:
            return Result(False, f"{why}。保存していません。", REFUSE_BAD_INPUT)
    if not user_settings.save(config.KEY_LOG_DIR, text):
        return Result(False, "設定ファイルに書けませんでした。", REFUSE_BAD_INPUT)
    where = apply_log_dir()
    log.info("ログの出力先: %s", where)
    if text:
        return Result(True, f"ログの出力先を変えました: {where}")
    return Result(True, f"ログの出力先を既定に戻しました: {where}")


# ==================================================================
# 片付け
# ==================================================================
_DAY_FILE = re.compile(r"^(?:packaging_tool|取り込み診断)_(\d{8})\.log$")
_MONTH_FILE = re.compile(r"^エラー記録_(\d{6})\.jsonl$")


def prune(today: Optional[date] = None) -> list[str]:
    """古いログを消す。**消したことはログに書く**(黙って消さない)。"""
    today = today or date.today()
    folder = config.log_dir()
    day_limit = f"{today - timedelta(days=KEEP_DAYS):%Y%m%d}"
    month_limit = (today.year * 12 + today.month - 1) - KEEP_ERROR_MONTHS
    removed: list[str] = []
    try:
        files = list(folder.iterdir())
    except OSError:
        return removed
    for path in files:
        old = False
        found = _DAY_FILE.match(path.name)
        if found and found.group(1) < day_limit:
            old = True
        found = _MONTH_FILE.match(path.name)
        if found:
            ym = found.group(1)
            if int(ym[:4]) * 12 + int(ym[4:]) - 1 < month_limit:
                old = True
        if old:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                continue
    if removed:
        log.info("古いログを消しました(動作ログ %s日・エラー記録 %sか月を残します): %s",
                 KEEP_DAYS, KEEP_ERROR_MONTHS, ", ".join(sorted(removed)))
    return removed


# ==================================================================
# エラー記録を読む
# ==================================================================
@dataclass
class ErrorItem:
    ref: str
    at: str
    pc: str
    where: str
    message: str
    screen: str
    data: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "at": self.at, "pc": self.pc, "where": self.where,
                "message": self.message, "screen": self.screen}


def _folders(everyone: bool) -> list[Path]:
    """読むフォルダ。`everyone` なら、同じ指定先にいるほかの端末のぶんも。"""
    mine = config.log_dir()
    if not everyone or config._active_log_dir is None:
        return [mine]
    root = mine.parent
    try:
        others = sorted(p for p in root.iterdir() if p.is_dir() and p != mine)
    except OSError:
        others = []
    return [mine, *others]


def _read_records(everyone: bool) -> list[dict]:
    rows: list[tuple[str, int, dict]] = []
    seq = 0
    for folder in _folders(everyone):
        try:
            files = sorted(folder.glob("エラー記録_*.jsonl"), reverse=True)
        except OSError:
            continue
        for path in files[:3]:                      # 直近3か月ぶん
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue                        # 書きかけの1行は飛ばす
                if isinstance(row, dict) and row.get("ref"):
                    row.setdefault("pc", folder.name)
                    row["_folder"] = str(folder)        # 同じ端末のログを引くため
                    seq += 1
                    rows.append((str(row.get("at", "")), seq, row))
    # 新しい順。同じ秒のものは後から書いたものを先に
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in rows]


def _screen(row: dict) -> str:
    op = row.get("operation") or {}
    if op.get("client"):
        return f"画面: {op.get('page', '')}"
    if op.get("job"):
        return f"処理: {op['job']}"
    if op.get("path"):
        return f"{op.get('method', '')} {op['path']}".strip()
    return ""


def list_errors(*, everyone: bool = False, query: str = "",
                limit: int = LIST_LIMIT) -> list[ErrorItem]:
    """新しい順のエラー記録。`query` はエラー番号・文言・端末のどれでも当たる。"""
    text = (query or "").strip().casefold()
    items: list[ErrorItem] = []
    for row in _read_records(everyone):
        item = ErrorItem(ref=str(row.get("ref", "")), at=str(row.get("at", "")),
                         pc=str(row.get("pc", "")), where=str(row.get("where", "")),
                         message=str(row.get("message", "")).splitlines()[0][:200]
                         if row.get("message") else "",
                         screen=_screen(row), data=row)
        if text and text not in " ".join(
                [item.ref, item.message, item.pc, item.screen,
                 str(row.get("detail", ""))]).casefold():
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items


def find_error(ref: str, *, everyone: bool = True) -> Optional[dict]:
    """エラー番号で1件。同じ操作番号の行も添える。"""
    for row in _read_records(everyone):
        if row.get("ref") == ref:
            found = dict(row)
            found["operation_lines"] = operation_lines(row)
            found["screen"] = _screen(row)
            return found
    return None


def operation_lines(row: dict, *, limit: int = OP_LINES_LIMIT) -> list[str]:
    """そのエラーと同じ操作番号の行を、その日のログファイルから集める。"""
    op_id = (row.get("operation") or {}).get("id", "")
    at = str(row.get("at", ""))
    if not op_id or len(at) < 10:
        return []
    try:
        day = datetime.strptime(at[:10], "%Y/%m/%d").date()
    except ValueError:
        return []
    mark = f"[{op_id}]"
    name = f"packaging_tool_{day:%Y%m%d}.log"
    lines: list[str] = []
    folders = [Path(row["_folder"])] if row.get("_folder") else _folders(True)
    for folder in folders:
        path = folder / name
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if mark in line:
                        lines.append(line.rstrip("\n"))
        except OSError:
            continue
        if lines:
            break
    if len(lines) > limit:
        # 長いときは頭と尻を残す(始まりとエラーの直前がいちばん大事)
        half = limit // 2
        lines = lines[:half] + [f"…(途中 {len(lines) - limit}行 略)…"] + lines[-half:]
    return lines


def why_why_text(row: dict) -> str:
    """1件を、なぜなぜ分析の下書きにする(コピーして使う)。"""
    op = row.get("operation") or {}
    lines = [
        f"■ エラー番号: {row.get('ref', '')}",
        f"■ いつ: {row.get('at', '')}",
        f"■ どの端末で: {row.get('pc', '')}(利用者 {row.get('login', '')}・"
        f"版 {row.get('version', '')})",
        f"■ どの操作で: {_screen(row) or '(画面の操作の外)'}",
    ]
    if op.get("input"):
        lines.append(f"■ そのときの入力: {op['input']}")
    if op.get("page"):
        lines.append(f"■ 開いていた画面: {op['page']}")
    lines += [
        f"■ 起きたこと: {row.get('message', '')}",
        f"■ 出どころ: {row.get('where', '')}",
        "",
        "■ 直前の動き(新しいものが下):",
        *[f"  {line}" for line in (row.get("recent") or [])[-15:]],
        "",
        "■ なぜなぜ(書き込んでください):",
        "  なぜ1: ",
        "  なぜ2: ",
        "  なぜ3: ",
        "  なぜ4: ",
        "  なぜ5: ",
        "  対策: ",
        "",
        "■ エラーの中身(開発担当向け):",
        str(row.get("detail", "") or "(なし)"),
    ]
    return "\n".join(lines)


# ==================================================================
# 要求を1行で言う(操作の足跡)
# ==================================================================
_SECRET_KEY = re.compile(r"pass|pw|token|secret|暗証", re.IGNORECASE)
LONG_TEXT = 120
INPUT_LIMIT = 600


def mask_input(value: Any, _depth: int = 0) -> Any:
    """ログに残す入力。**パスワードは伏せ、長いもの(画像など)は縮める。**"""
    if _depth > 4:
        return "…"
    if isinstance(value, dict):
        return {k: ("(伏せます)" if _SECRET_KEY.search(str(k)) else
                    mask_input(v, _depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        shown = [mask_input(v, _depth + 1) for v in value[:20]]
        if len(value) > 20:
            shown.append(f"…ほか {len(value) - 20}件")
        return shown
    if isinstance(value, str) and len(value) > LONG_TEXT:
        return f"{value[:40]}…({len(value)}文字)"
    return value


def describe_input(value: Any) -> str:
    """入力を1行の文字にする。"""
    if value in (None, "", {}, []):
        return ""
    try:
        text = json.dumps(mask_input(value), ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > INPUT_LIMIT:
        text = text[:INPUT_LIMIT] + f"…({len(text)}文字)"
    return text
