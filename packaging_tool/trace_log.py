"""後から追えるログ ── 出力先の決め方と、エラー記録の読み出し

現場の声:「エラー等の後追いが現状できないと感じている。ログを残し
なぜなぜで分析できるようにしておいてほしい。ログ出力は設定でパス指定
できるようにする」。

書く側の仕掛け(操作番号・エラー番号・エラー記録)は `logging_utils`。
ここは次を持つ:

- **出力先**: 環境変数 > 設定画面 > 既定(この端末のローカル領域)。
  どこでも **`<出力先>/packagingtool/` を作ってその中に、PC名入りの名前で書く**
  (VER4.8.111〜。日報複合ツールに合わせた決まり。`log_names`)── 共有フォルダを全端末で
  指しても混ざらず、ほかのツールのログとも分かれる。書けないフォルダは選ばせない。起動時に
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

from . import config, log_names, logging_utils
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
    SOURCE_SETTING: "設定画面で指定したフォルダ(の下の packagingtool フォルダ)",
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
    """この端末の名前(ファイル名・前の版のフォルダ名に使う形)。"""
    return log_names.pc_name()


def configured_text() -> str:
    """設定画面に入っている値(空なら既定)。"""
    from . import user_settings
    value = user_settings.get(config.KEY_LOG_DIR)
    return value.strip() if isinstance(value, str) else ""


def folder_for(text: str) -> Path:
    """指定したフォルダの下の書き先: `<指定>/packagingtool`(VER4.8.111〜)。

    指定先はほかのツールとも共用するので、**ツールIDのフォルダを作ってその中に**書く。
    どの端末のものかはファイル名のPC名で分かる(全端末が同じフォルダに書く。日報複合ツールと同じ)。
    前の版: `<指定>/<端末名>`(〜4.8.106)・`<指定>/packaging_tool/<端末名>`(4.8.107〜4.8.110)。
    """
    return config.resolve_dir(text) / log_names.TOOL_ID


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
@dataclass(frozen=True)
class Source:
    """ログを探すフォルダ1つ。

    `new` … いまの形(`packagingtool_…_<PC>_…`)。`pc` が空なら**すべての端末**のファイル。
    `new` でないもの … 前の版の形(PC名が無い)。フォルダの中は全部 `pc` の端末のもの。
    """

    folder: Path
    new: bool
    pc: str = ""


def _configured_base() -> Optional[Path]:
    """設定画面で指定した出力先(ツールIDのフォルダの1つ上)。指定が無ければ None。"""
    if config._active_log_dir is not None:
        return config._active_log_dir.parent
    text = "" if config.LOG_DIR_FROM_ENV else configured_text()
    if not text:
        return None
    try:
        return config.resolve_dir(text)
    except Exception:                               # noqa: BLE001 - 読めなければ見ない
        return None


def sources(everyone: bool = False) -> list[Source]:
    """**自分のツールのログ**を探すフォルダ(前の場所も含む)。

    - いまの書き先(`<出力先>/packagingtool`)。`everyone` ならほかの端末のファイルも
    - 既定の書き先(ローカルの `logs/packagingtool`)。共有へ書けなかった日の分
    - 前の場所(前の版の名前。**この端末のもの**): ローカルの `logs` の直下、
      指定先の `<端末名>`(〜4.8.106)と `packaging_tool/<端末名>`(4.8.107〜4.8.110)
    - `everyone` なら前の版の `packaging_tool/<ほかの端末>`。指定先の直下のほかのフォルダは
      **見ない**(ほかのツールのフォルダかもしれない。決まり5)
    """
    me = _pc_name()
    found: list[Source] = []
    current = config.log_dir()
    shared = config._active_log_dir is not None
    found.append(Source(current, True, "" if (everyone and shared) else me))
    if current != config.LOG_DIR:
        found.append(Source(config.LOG_DIR, True, me))
    found.append(Source(config.LOG_DIR.parent, False, me))
    base = _configured_base()
    if base is not None:
        found.append(Source(base / me, False, me))
        legacy = base / log_names.LEGACY_TOOL
        found.append(Source(legacy / me, False, me))
        if everyone:
            try:
                others = sorted(p for p in legacy.iterdir() if p.is_dir() and p.name != me)
            except OSError:
                others = []
            found += [Source(p, False, p.name) for p in others]
    unique: list[Source] = []
    seen: set[tuple[str, bool, str]] = set()
    for src in found:
        key = (str(src.folder), src.new, src.pc)
        if key not in seen:
            seen.add(key)
            unique.append(src)
    return unique


def _files(src: Source, new_pattern: re.Pattern, old_pattern: re.Pattern):
    """そのフォルダの、その種類のファイル: (パス, 端末名, 日付の字) を返す。"""
    try:
        paths = list(src.folder.iterdir())
    except OSError:
        return []
    out = []
    for path in paths:
        if src.new:
            got = new_pattern.match(path.name)
            if got and (not src.pc or got.group("pc") == src.pc):
                out.append((path, got.group("pc"), got.groups()[-1]))
        else:
            got = old_pattern.match(path.name)
            if got:
                out.append((path, src.pc, got.groups()[-1]))
    return out


def own_files(new_pattern: re.Pattern, old_pattern: re.Pattern) -> list[Path]:
    """この端末の、その種類のファイル(前の場所・前の名前も)。"""
    return [path for src in sources() for path, _pc, _d in _files(src, new_pattern, old_pattern)]


def prune(today: Optional[date] = None) -> list[str]:
    """古いログを消す。**消したことはログに書く**(黙って消さない)。

    消すのは**自分のツールの、この端末のもの**だけ(前の場所の自分のログも。決まり4・5)。
    共有の書き先にあるほかの端末のファイル、ほかのツールのファイル・フォルダには触らない。
    """
    today = today or date.today()
    day_limit = f"{today - timedelta(days=KEEP_DAYS):%Y%m%d}"
    month_limit = (today.year * 12 + today.month - 1) - KEEP_ERROR_MONTHS
    removed: list[str] = []
    for src in sources(everyone=False):
        old: list[Path] = []
        for new_pattern, old_pattern in ((log_names.NEW_DAY, log_names.OLD_DAY),
                                         (log_names.NEW_DIAG, log_names.OLD_DIAG)):
            old += [p for p, _pc, d in _files(src, new_pattern, old_pattern) if d < day_limit]
        for p, _pc, ym in _files(src, log_names.NEW_ERROR, log_names.OLD_ERROR):
            if int(ym[:4]) * 12 + int(ym[4:]) - 1 < month_limit:
                old.append(p)
        for path in old:
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


def _read_records(everyone: bool) -> list[dict]:
    # 書くのは別のスレッド(VER4.8.115)。並んでいる分を書き終えてから読む(待つのは上限まで)
    logging_utils.flush_logs(1.0)
    rows: list[tuple[str, int, dict]] = []
    seq = 0
    for src in sources(everyone):
        files = _files(src, log_names.NEW_ERROR, log_names.OLD_ERROR)
        # 端末ごとに直近3か月ぶん
        by_pc: dict[str, list] = {}
        for item in files:
            by_pc.setdefault(item[1], []).append(item)
        picked = [item for group in by_pc.values()
                  for item in sorted(group, key=lambda i: i[2], reverse=True)[:3]]
        for path, pc, _ym in picked:
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
                    row.setdefault("pc", pc)
                    # 同じ端末のその日の動作ログを引くため(`operation_lines`)
                    row["_folder"] = str(src.folder)
                    row["_log_pc"] = pc
                    row["_log_new"] = src.new
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
    lines: list[str] = []
    logging_utils.flush_logs(1.0)          # 並んでいる分を書き終えてから読む(VER4.8.115)
    if row.get("_folder"):
        # エラー記録と同じ場所・同じ端末のその日の動作ログ(前の版の形なら前の名前)
        pc = str(row.get("_log_pc") or row.get("pc") or "")
        name = log_names.day_log(day, pc) if row.get("_log_new", True) else log_names.old_day_log(day)
        candidates = [Path(row["_folder"]) / name]
    else:
        candidates = [src.folder / (log_names.day_log(day, src.pc) if src.new and src.pc
                                    else log_names.old_day_log(day))
                      for src in sources(True)]
    for path in candidates:
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


# ==================================================================
# 時間のかかった操作(「毎回なのか、初回だけなのか」を、その端末のログで答える)
# ==================================================================
# 現場の声:「倉庫連携の読み込みに時間がかかった。毎回なのか初回だけなのか知りたい」。
# 操作の足跡(`操作 GET /warehouse → 200 (1234ms)`)は VER3.6.0 から全部残っている。
# それを読み、遅かった操作ごとに**起動から何秒後か・その画面を何回目に開いたときか**
# を添える。1回目だけ遅いなら初回だけ、2回目以降も遅いなら毎回。

SLOW_MS = 1000

_OP_LINE = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \| \S+ \| \w+ \| (?:\[\w+\] )?"
    r"(?:操作|断り) (GET|POST) (\S+) → (\d+)[^(]*\((\d+)ms\)")
_START_LINE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \| .* \| 起動: 版 ")

# 経路 → 画面での呼び名(前方一致。長いものから)
_LABELS: tuple[tuple[str, str], ...] = (
    ("/api/warehouse/refresh", "倉庫連携: 取り込み元を見る(読み直す・見張り)"),
    ("/api/warehouse/comments", "倉庫連携: コメントを開く"),
    ("/api/warehouse", "倉庫連携の操作"),
    ("/warehouse", "倉庫連携の画面"),
    ("/api/selection", "資材選択の操作"),
    ("/selection", "資材選択の画面"),
    ("/api/inventory", "簡易在庫の操作"),
    ("/inventory", "簡易在庫の画面"),
    ("/api/layout", "棚検索の操作"),
    ("/layout", "棚検索の画面"),
    ("/api/lot", "ロット検索の操作"),
    ("/lot", "ロット検索の画面"),
    ("/api/master", "マスタ管理"),
    ("/api/settings", "設定の操作"),
    ("/settings", "設定の画面"),
    ("/report", "帳票"),
)


def _label(path: str) -> str:
    for prefix, label in _LABELS:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return label
    return path


def slow_operations(day: Optional[date] = None, *, threshold_ms: int = SLOW_MS,
                    limit: int = 200) -> dict[str, Any]:
    """その日の、時間のかかった操作と、画面ごとのまとめ。"""
    day = day or date.today()
    logging_utils.flush_logs(1.0)          # 並んでいる分を書き終えてから読む(VER4.8.115)
    path = logging_utils.log_path_for(day)
    if not path.exists():
        # 前の版が書いた日(前の場所・前の名前。この端末のもの)
        path = next((src.folder / log_names.old_day_log(day) for src in sources()
                     if not src.new and (src.folder / log_names.old_day_log(day)).exists()), path)
    items: list[dict[str, Any]] = []
    totals: dict[str, dict[str, Any]] = {}
    started: Optional[datetime] = None
    nth: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        found = _START_LINE.match(line)
        if found:
            started = datetime.strptime(found.group(1), "%Y/%m/%d %H:%M:%S")
            nth = {}
            continue
        found = _OP_LINE.match(line)
        if not found:
            continue
        at_text, method, target, status, ms = found.groups()
        route = target.split("?", 1)[0]
        key = f"{method} {route}"
        nth[key] = nth.get(key, 0) + 1
        at = datetime.strptime(at_text, "%Y/%m/%d %H:%M:%S")
        total = totals.setdefault(key, {"label": _label(route), "count": 0, "slow": 0,
                                        "slow_nth": [], "max_ms": 0})
        total["count"] += 1
        took = int(ms)
        total["max_ms"] = max(total["max_ms"], took)
        if took < threshold_ms:
            continue
        total["slow"] += 1
        total["slow_nth"].append(nth[key])
        items.append({
            "at": at_text[11:], "label": _label(route), "what": f"{method} {target[:120]}",
            "ms": took, "status": int(status), "nth": nth[key],
            "since_start": int((at - started).total_seconds()) if started else None,
        })
    summary = []
    for key, total in totals.items():
        if not total["slow"]:
            continue
        first_only = all(n == 1 for n in total["slow_nth"])
        summary.append({
            "label": total["label"], "what": key, "count": total["count"],
            "slow": total["slow"], "max_ms": total["max_ms"], "first_only": first_only,
            "text": (f"{total['label']}: 今日 {total['count']}回のうち "
                     f"{total['slow']}回が{threshold_ms // 1000}秒以上"
                     f"(いちばん長くて {total['max_ms'] / 1000:.1f}秒)── "
                     + ("起動して最初の1回だけです"
                        if first_only else
                        "2回目以降にもあります(初回だけではありません)")),
        })
    summary.sort(key=lambda s: -s["max_ms"])
    items.reverse()                                 # 新しい順
    return {"day": f"{day:%Y/%m/%d}", "threshold_ms": threshold_ms,
            "items": items[:limit], "summary": summary,
            "found_log": bool(lines)}
