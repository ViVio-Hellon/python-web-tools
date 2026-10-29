"""ログ出力 (VBA `DebugLog`/`InitializeDebugLog` 相当)

VBA版はFileSystemObjectで手動追記し、さらに端末のライン名を
レジストリから推測してサブフォルダ分けする、という凝った作りだったが、
Python版ではファイル配置の複雑さの割に価値が薄いため、標準の
`logging` モジュールに置き換える。

- 出力先: `config.log_dir()`(`<出力先>\\packagingtool`)配下に `packagingtool_<PC>_YYYYMMDD.log`
  (設定画面で変えられる。決め方は `trace_log.apply_log_dir`)
- コンソール(Debug.Printのイミディエイト相当)にも同時出力

【後から追えるようにする仕掛け】(現場の声:「エラー等の後追いが現状
できない。ログを残し、なぜなぜで分析できるようにしてほしい」)

- **操作番号**: 画面からの要求1つごとに番号を振り、その要求の間に
  出た行すべての頭に `[番号]` を付ける。同じ番号で引けば、1つの操作で
  何が起きたかが(選定の細かい判断まで)まとめて読める
- **エラー記録**: ERROR 以上の行には**エラー番号**を振り、
  `packagingtool_エラー記録_<PC>_YYYYMM.jsonl` に1件1行で残す。どの操作の途中だったか、
  その直前に何をしていたか(直近の行)、エラーの中身(トレースバック)、
  端末・利用者・版を一緒に入れる ── 「なぜ」を1段ずつ遡る材料を、
  起きたその場で揃えておく。画面のエラー表示にも同じ番号を出すので、
  現場から番号を聞けば、その1件にたどり着ける
"""
from __future__ import annotations

import collections
import contextvars
import json
import logging
import os
import queue
import secrets
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from . import config

_configured = False

# 行の形。`op` は操作番号(`_OpFilter` が埋める)
LINE_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(op)s%(message)s"
DATE_FORMAT = "%Y/%m/%d %H:%M:%S"

# エラー記録に添える「直前の動き」の行数
RECENT_LINES = 40


# **ログの置き場所と名前の決まり**は `log_names`(VER4.8.111〜。日報複合ツールに合わせた)。
# `<出力先>\packagingtool\packagingtool_<PC>_<日付>.log` のように、フォルダにもファイル名にも
# ツールIDを入れ、ファイル名にはPC名も入れる(共有フォルダで何台もが同じファイルに書かない)
from . import log_names  # noqa: E402


def log_path_for(day: date) -> "Path":
    """その日のログファイル。**名前の作り方は `log_names` 1か所**。"""
    return config.log_dir() / log_names.day_log(day)


def error_path_for(when: datetime) -> Path:
    """その月のエラー記録。月ごとに分ける(1件1行の JSON)。"""
    return config.log_dir() / log_names.error_log(when)


# ------------------------------------------------------------------
# 操作番号
# ------------------------------------------------------------------
_operation: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "trace_operation", default=None)


def new_operation_id() -> str:
    """操作番号。ログを番号で引くので、**同じ日に重ならない**長さにする。"""
    return secrets.token_hex(3)


# 同じ秒に振ったエラー番号の末尾(秒 → 使った末尾)。**同じ秒に同じ番号を2度振らない**ため。
# 末尾は16進2桁なので、覚えておかないと同じ秒の2件が 1/256 で同じ番号になる(実際に試験で当たった)
_REFS_KEEP = 8                      # 覚えておく秒の数(古い秒から捨てる)
_ref_lock = threading.Lock()
_refs_used: "collections.OrderedDict[str, set[str]]" = collections.OrderedDict()


def new_error_ref(now: Optional[datetime] = None) -> str:
    """エラー番号。**番号を見ただけで、いつのことか分かる**形にする
    (現場から口頭で聞いても、どの日のどのあたりかが分かる)。

    同じ秒の番号は、この端末(このプロセス)の中では重ならない。1秒に256件を超えたら
    末尾を4桁にする(そこまで出ることは無いが、黙って重ねない)。"""
    now = now or datetime.now()
    stamp = f"E{now:%y%m%d-%H%M%S}"
    with _ref_lock:
        used = _refs_used.setdefault(stamp, set())
        _refs_used.move_to_end(stamp)
        while len(_refs_used) > _REFS_KEEP:
            _refs_used.popitem(last=False)
        width = 1 if len(used) < 256 else 2
        suffix = secrets.token_hex(width)
        while suffix in used:
            suffix = secrets.token_hex(width)
        used.add(suffix)
    return f"{stamp}-{suffix}"


def begin_operation(info: dict) -> contextvars.Token:
    """ここから先の行に、この操作の番号を付ける。`end_operation` と対で使う。"""
    return _operation.set(dict(info))


def end_operation(token: contextvars.Token) -> None:
    _operation.reset(token)


def current_operation() -> Optional[dict]:
    """いま処理している操作(無ければ None)。"""
    return _operation.get()


class _OpFilter(logging.Filter):
    """行に操作番号を付け、ERROR 以上にはエラー番号を振る。

    **どのハンドラが先に呼ばれても同じ番号になる**よう、番号はここで
    1度だけ振る(付いていれば触らない)。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        op = _operation.get()
        if not hasattr(record, "op_id"):
            record.op_id = (op or {}).get("id", "")
        if record.levelno >= logging.ERROR and not getattr(record, "ref", ""):
            record.ref = new_error_ref(datetime.fromtimestamp(record.created))
        if record.levelno >= logging.ERROR and not hasattr(record, "recent_lines"):
            # **書くのは別のスレッド**(`_AsyncWriter`)なので、その時点の「直前の動き」と
            # 操作は、ここ(起きたスレッド)で写しておく。この行そのものはまだ入っていない
            record.recent_lines = recent_lines()
            record.operation = op or {}
        head = f"[{record.op_id}] " if record.op_id else ""
        if getattr(record, "ref", ""):
            head += f"<{record.ref}> "
        record.op = head
        return True


# ------------------------------------------------------------------
# 直前の動き(エラー記録に添える)
# ------------------------------------------------------------------
_recent: collections.deque = collections.deque(maxlen=RECENT_LINES * 2)
_recent_lock = threading.Lock()


class _RecentHandler(logging.Handler):
    """INFO 以上の行を、直近の分だけ覚えておく。

    DEBUG(選定の細かい判断)は入れない ── 数千行出るので、直前に
    **何の操作をしたか**が押し流される。細かい行は操作番号で
    ログファイルから引ける。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # **1件1行。** トレースバックはエラー記録の「中身」に入るので、
            # ここでは頭の1行だけにする(直前の動きが読めなくなる)
            line = self.format(record).split("\n", 1)[0]
        except Exception:                       # noqa: BLE001 - ログで止めない
            return
        with _recent_lock:
            _recent.append(line)


def recent_lines() -> list[str]:
    with _recent_lock:
        return list(_recent)


# ------------------------------------------------------------------
# エラー記録
# ------------------------------------------------------------------
_error_lock = threading.Lock()


def _who() -> dict[str, str]:
    """端末・利用者・版。**取れなくても記録は残す。**"""
    found: dict[str, str] = {}
    try:
        from . import access_control
        ident = access_control.current_identity()
        found["pc"], found["login"] = ident.pc_name, ident.login_id
    except Exception:                           # noqa: BLE001
        pass
    try:
        from . import app_config
        found["version"] = app_config.version()
    except Exception:                           # noqa: BLE001
        pass
    return found


class _ErrorRecordHandler(logging.Handler):
    """ERROR 以上を `packagingtool_エラー記録_<PC>_YYYYMM.jsonl` へ1件1行で残す。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            when = datetime.fromtimestamp(record.created)
            detail = getattr(record, "detail", "") or ""
            trace = (record.exc_text if getattr(record, "exc_text", None)
                     else logging.Formatter().formatException(record.exc_info) if record.exc_info
                     else "")
            if trace:
                detail = (detail + "\n" if detail else "") + trace
            ref = getattr(record, "ref", "")
            # 起きたスレッドで写した分(`_OpFilter`)。直接呼ばれたとき(試験)はいまの分
            recent = getattr(record, "recent_lines", None)
            if recent is None:
                recent = recent_lines()
            operation = getattr(record, "operation", None)
            if operation is None:
                operation = current_operation() or {}
            entry: dict[str, Any] = {
                "ref": ref,
                "at": when.strftime(DATE_FORMAT),
                "level": record.levelname,
                "where": record.name,
                "message": record.getMessage(),
                "detail": detail,
                "operation": operation,
                "recent": [line for line in recent if ref not in line][-RECENT_LINES:],
                **_who(),
            }
            path = _target_dir(record) / log_names.error_log(when)
            with _error_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:                       # noqa: BLE001 - ログで業務を止めない
            self.handleError(record)


class DailyFileHandler(logging.FileHandler):
    """日付が変わったら、その日のファイルへ書き換える。

    **開いたままの端末のため。** 書き出し先を起動時に1度だけ決めると、
    夜勤をまたいだ端末は翌日ぶんを前日のファイルに書き続ける ── 13日の
    不具合を追うときに、13日のログが12日のファイルに入っている。

    `TimedRotatingFileHandler` を使わないのは、あちらが「いまのファイルを
    日付付きの名前へ退ける」作りで、こちらの名前の付け方
    (`packagingtool_<PC>_YYYYMMDD.log` にそのまま書く)と噛み合わないため。
    日付を見て開き直すだけで足りる。
    """

    def __init__(self, day: date, **kwargs) -> None:
        self._day = day
        super().__init__(log_path_for(day), **kwargs)

    def emit(self, record: logging.LogRecord) -> None:
        # **書く直前に見る。** 日付が変わったこと・設定画面で出力先を
        # 変えたことは、次の1行で気づく。出力先は**その行が起きたときの**もの(`_target_dir`)
        self._day = date.today()
        target = os.path.abspath(_target_dir(record) / log_names.day_log(self._day))
        if target != self.baseFilename:
            self.close()
            self.baseFilename = target
            self.stream = None                    # 次の emit で開き直す
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
            except OSError:
                pass                              # 書けなければ handleError へ
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        """**指定の出力先に書けなくなったら、既定の場所へ戻して書き直す。**

        共有フォルダを指していて、途中でネットワークが切れた ── そのまま
        だとそこから先のログが全部失われる(追いたいのはまさにその時間)。
        """
        folder = _target_dir(record)
        if folder != config.LOG_DIR:
            _abandon(folder, f"{folder} に書けなくなったので")
            try:
                self.emit(record)               # 今度は既定の場所(`_target_dir`)
                return
            except Exception:                   # noqa: BLE001
                pass
        super().handleError(record)


# 指定の出力先に書けず、既定の場所へ戻したときの理由(画面に出す)
fallback_why = ""

# 書けなくなった・遅すぎた出力先(VER4.8.115)。並んでいた行もここへは書かない
_abandoned: set = set()


def _target_dir(record: logging.LogRecord) -> Path:
    """その行を書くフォルダ。**行が起きたときの出力先**(`_AsyncWriter` が写す)。

    書くのは別のスレッドなので、書くときの設定を見ると、出力先を変える前の行まで新しい
    場所へ入る。やめた出力先(`_abandoned`)の行は、いまの出力先へ書く。
    """
    folder = getattr(record, "log_dir", None)
    if folder is None:
        return config.log_dir()
    if folder in _abandoned and folder != config._active_log_dir:
        return config.log_dir()
    return folder


def _abandon(folder: Path, why: str) -> None:
    """その出力先をやめる。いまの出力先ならローカル(既定)へ戻し、理由を画面に出す。"""
    global fallback_why
    _abandoned.add(folder)
    if config._active_log_dir is not None and Path(config._active_log_dir) == Path(folder):
        fallback_why = (f"{why}、{config.LOG_DIR} に書いています"
                        f"({datetime.now():%Y/%m/%d %H:%M})")
        config._active_log_dir = None


# ------------------------------------------------------------------
# ファイルへ書くのは別のスレッド(VER4.8.115)
# ------------------------------------------------------------------
# 現場の声:「資材と現場を切り替えると時々フリーズする」。
#
# ログは1行ごとに、その場(画面の要求のスレッド)でファイルへ書いていた。出力先が共有
# フォルダだと、1行ごとにネットワークの往復がある。書き込みはハンドラの錠を持ったまま
# 行うので、**共有が遅い・一時的に応答しないと、ログを1行でも書くスレッドがぜんぶ
# そこで待つ** ── モードの切替も「切り替えました」を書くので止まる。資材選択は細かい
# 判断(DEBUG)を数千行書く。
#
# 要求のスレッドは**並べるだけ**にし、書くのは1本のスレッドに任せる。共有が遅くても
# 画面は止まらない。1行に `SLOW_WRITE_SEC` 以上かかったら、この端末のローカルの
# ログフォルダへ切り替える(書けなくなったときと同じ。理由は設定の「ログ」の面に出る)。
SLOW_WRITE_SEC = 5.0
# 書き待ちがこれを超えたら、細かい判断(DEBUG)は捨てる(INFO 以上は捨てない)
MAX_PENDING = 20000


# 1行の書き込みがこれだけ返らなければ、その出力先(共有)をやめて、新しい書き手で
# ローカルへ書き続ける(VER4.8.115)。共有が応答しないと、書き込みは何十秒も戻らない
STUCK_SEC = 15.0


class _AsyncWriter(logging.Handler):
    """受け取った行を並べて、別のスレッドで書く。書く先(ハンドラ)は `make_targets` が作る。

    書き込みが `STUCK_SEC` 返らないときは、**新しい書き手(スレッドとハンドラ)に替えて**続ける
    (`takeover`)。止まった書き手は、戻ってきたらそこで終わる。コンソールは替えない(管が
    詰まっているなら、新しい書き手も同じところで止まる)── 止まっているあいだは捨てる。
    """

    def __init__(self, make_targets, *, name: str = "log-writer",
                 console: bool = False, takeover: bool = True) -> None:
        super().__init__(level=logging.DEBUG)
        self.make_targets = make_targets
        self.targets: list[logging.Handler] = make_targets()
        self.thread_name = name
        # コンソール(標準エラー)へ書く係か(`silence_console` が外す)
        self.is_console = console
        self.takeover = takeover
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._pending = 0
        self._count_lock = threading.Lock()
        self.dropped = 0
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        # いま書いている1行(いつから・どこへ)。止まっているかを見るため
        self._busy_since: Optional[float] = None
        self._busy_folder: Optional[Path] = None
        self._generation = 0

    # --- 要求のスレッド ---------------------------------------------------
    def handle(self, record: logging.LogRecord) -> bool:  # noqa: D401 - logging の口
        """**錠を取らない。** 並べるだけなので、ほかのスレッドを待たせない。"""
        if not self.filter(record):
            return False
        self.emit(record)
        return True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._stuck():
                return                           # コンソールが詰まっている。捨てる
            if record.levelno < logging.INFO and self._pending >= MAX_PENDING:
                self.dropped += 1
                return
            _freeze(record)
            if not hasattr(record, "log_dir"):
                record.log_dir = config.log_dir()   # 行が起きたときの出力先(`_target_dir`)
            with self._count_lock:
                self._pending += 1
            self._ensure_thread()
            self._queue.put(record)
        except Exception:                       # noqa: BLE001 - ログで業務を止めない
            pass

    def flush(self, timeout: float = 2.0) -> bool:  # type: ignore[override]
        """ここまでに並べた分を書き終えるまで待つ(上限 `timeout` 秒)。書き終えたら True。"""
        if self._thread is None or not self._thread.is_alive():
            return self._pending == 0
        done = threading.Event()
        self._queue.put(done)
        return done.wait(timeout)

    def close(self) -> None:
        self.flush(2.0)
        super().close()

    @property
    def pending(self) -> int:
        return self._pending

    def _stuck(self) -> bool:
        """書き込みが `STUCK_SEC` 返っていないか。ファイルなら新しい書き手に替える(False を返す)。"""
        since = self._busy_since
        if since is None or time.monotonic() - since < STUCK_SEC:
            return False
        if not self.takeover:
            self.dropped += 1
            return True
        with self._thread_lock:
            if self._busy_since != since:
                return False                     # その間に戻ってきた
            folder = self._busy_folder
            if folder is None or Path(folder) == Path(config.LOG_DIR):
                return False                     # ローカルなら替えても同じ。待つ
            self._generation += 1
            self._busy_since, self._busy_folder = None, None
            self.targets = self.make_targets()
            self._thread = None
        _abandon(folder, f"{folder} への書き込みが {STUCK_SEC:.0f}秒 返らないので")
        return False

    # --- 書くスレッド -----------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, args=(self._generation,),
                                            name=self.thread_name, daemon=True)
            self._thread.start()

    def _run(self, generation: int) -> None:
        targets = self.targets
        # 替えられたら(`_stuck`)、いま書いている1行を終えたところで終わる
        while generation == self._generation:
            item = self._queue.get()
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                self._write(item, targets, generation)
            finally:
                with self._count_lock:
                    self._pending -= 1

    def _write(self, record: logging.LogRecord, targets, generation: int) -> None:
        if self.dropped:
            dropped, self.dropped = self.dropped, 0
            note = logging.LogRecord(record.name, logging.WARNING, __file__, 0,
                                     "書き待ちが多すぎた・書けなかったので、%s行 捨てました",
                                     (dropped,), None)
            note.op = ""
            self._write_one(note, targets, generation)
        self._write_one(record, targets, generation)

    def _write_one(self, record: logging.LogRecord, targets, generation: int) -> None:
        for handler in targets:
            if record.levelno < handler.level:
                continue
            began = time.monotonic()
            if generation == self._generation:
                self._busy_folder = (_target_dir(record) if isinstance(handler, DailyFileHandler)
                                     else None)
                self._busy_since = began
            try:
                handler.handle(record)
            except Exception:                   # noqa: BLE001 - 書けなくても続ける
                pass
            finally:
                if generation == self._generation:
                    self._busy_since = None
            took = time.monotonic() - began
            if took >= SLOW_WRITE_SEC and isinstance(handler, DailyFileHandler):
                _too_slow(took, _target_dir(record))


def _freeze(record: logging.LogRecord) -> None:
    """**あとで別のスレッドが書いても、起きたときの中身になるように**固める。

    引数(%s の中身)はあとで変わることがあるので、ここで文にしてしまう。トレースバックも
    ここで文にする(`Formatter` は `exc_text` があればそれを使う)。
    """
    if record.args:
        try:
            record.msg = record.getMessage()
            record.args = None
        except Exception:                       # noqa: BLE001 - 固められなくても書く
            pass
    if record.exc_info and not record.exc_text:
        try:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        except Exception:                       # noqa: BLE001
            pass


def _too_slow(took: float, folder: Path) -> None:
    """1行に時間がかかりすぎた。その出力先(共有)をやめ、ローカルへ切り替える。"""
    if Path(folder) == Path(config.LOG_DIR):
        return
    _abandon(folder, f"{folder} への書き込みに {took:.0f}秒 かかったので"
                     "(共有フォルダの応答が戻ったら、ログの面で出力先を保存し直してください)")


_writer: Optional[_AsyncWriter] = None


def flush_logs(timeout: float = 2.0) -> bool:
    """ここまでのログを書き終えるまで待つ(読む前・終わる前に呼ぶ)。書き終えたら True。"""
    if _writer is None:
        return True
    return _writer.flush(timeout)


def file_handler() -> Optional["DailyFileHandler"]:
    """その日のログファイルへ書くハンドラ(書くスレッドが使う)。"""
    if _writer is None:
        return None
    return next((h for h in _writer.targets if isinstance(h, DailyFileHandler)), None)


def configure_logging() -> None:
    """アプリ起動時に一度だけ呼ぶ。二重呼び出しは無害(冪等)。"""
    global _configured
    if _configured:
        return
    # **先に立てる。** ファイル名にPC名を入れる(`log_names.pc_name`)ために身元を引くと、
    # その途中でここがもう一度呼ばれることがある(2重に呼ぶと同じ行が2回書かれる)
    _configured = True

    config.ensure_dirs()

    root = logging.getLogger("packaging_tool")
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(LINE_FORMAT, datefmt=DATE_FORMAT)
    op_filter = _OpFilter()

    # エラー記録とその日のファイルは**書くスレッドが書く**(`_AsyncWriter`。VER4.8.115)。
    # 先に置く ── 「直前の動き」にエラーの行そのものがまだ入っていないうちに写す(`_OpFilter`)
    global _writer

    def file_targets() -> list[logging.Handler]:
        # 書き手を替えるとき(`_AsyncWriter._stuck`)も同じ形で作り直す
        daily = DailyFileHandler(date.today(), encoding="utf-8", delay=True)
        daily.setFormatter(formatter)
        return [_ErrorRecordHandler(level=logging.ERROR), daily]

    _writer = _AsyncWriter(file_targets)
    _writer.addFilter(op_filter)
    root.addHandler(_writer)

    recent_handler = _RecentHandler(level=logging.INFO)
    recent_handler.setFormatter(formatter)
    recent_handler.addFilter(op_filter)
    root.addHandler(recent_handler)

    # コンソールが無いときは付けない。
    # **`pythonw.exe` では `sys.stderr` が `None`** になる(Start.vbs は
    # コンソールを出さないために pythonw を使う)。`StreamHandler()` は
    # そのとき `stream = None` を抱え、1行出すたびに `None.write` で
    # 例外を起こす。`logging` が握りつぶすので表には出ないが、
    # 選定アルゴリズムは実データ1件で数千行のDEBUGを出すため、
    # 誰にも見えない例外をその回数ぶん払うことになる。
    if _has_console():
        # **コンソールも別のスレッドで書く**(VER4.8.115)。デスクトップ版では標準エラーが外枠
        # (exe)への管で、読む側が止まると管が詰まり、書いたスレッドがそこで止まる。ファイルとは
        # 別の1本にする(コンソールが詰まっても、ファイルのログは書き続ける)
        def console_targets() -> list[logging.Handler]:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            return [console_handler]

        console = _AsyncWriter(console_targets, name="log-console", console=True, takeover=False)
        console.addFilter(op_filter)
        root.addHandler(console)

    _configured = True


def _has_console() -> bool:
    """標準エラー出力に書けるか。

    `pythonw.exe` は `sys.stderr` が `None`。リダイレクト先が閉じている
    場合に備えて `write` を持つかどうかまで見る。
    """
    import sys
    return getattr(sys, "stderr", None) is not None and hasattr(sys.stderr, "write")


def get_logger(name: str = "packaging_tool") -> logging.Logger:
    """モジュール別ロガーを取得する(未初期化なら自動で初期化する)。"""
    configure_logging()
    return logging.getLogger(f"packaging_tool.{name}" if name != "packaging_tool" else name)


def silence_console() -> None:
    """コンソールへのログ出力だけを止める(ファイルへの出力は残す)。

    テストや診断スクリプトのように、**そのプログラム自身の出力が主役**で
    あって業務ログが主役ではない場面のためのもの。
    選定アルゴリズムはDEBUGを大量に出すため(実データ41通りで19万文字)、
    止めないと肝心の結果が流れて読めない。

    `get_logger()` はインポート時に呼ばれることがあり、その時点で既に
    コンソール用ハンドラが付いている。あとから `configure_logging()` が
    走る場合もあるので、既存のハンドラを外すだけでなく、
    以降の追加も弾くように `addHandler` を包む。

    ファイルへの出力は残すので、調査に必要な情報は失われない。
    出力先は `PACKAGING_TOOL_LOG_DIR`(または設定画面)で変えられる。
    """
    logger = logging.getLogger("packaging_tool")

    def _is_console(handler: logging.Handler) -> bool:
        # 別のスレッドで書く係(VER4.8.115)。中身がコンソールなら外す
        if getattr(handler, "is_console", False):
            return True
        # FileHandler は StreamHandler の派生なので先に除外する
        return (isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, logging.FileHandler))

    for handler in list(logger.handlers):
        if _is_console(handler):
            logger.removeHandler(handler)

    if getattr(logger, "_console_silenced", False):
        return                                  # 二重に包まない(冪等)

    original_add = logger.addHandler

    def add_handler(handler: logging.Handler) -> None:
        if _is_console(handler):
            return
        original_add(handler)

    logger.addHandler = add_handler             # type: ignore[method-assign]
    logger._console_silenced = True             # type: ignore[attr-defined]
