"""ログ出力 (VBA `DebugLog`/`InitializeDebugLog` 相当)

VBA版はFileSystemObjectで手動追記し、さらに端末のライン名を
レジストリから推測してサブフォルダ分けする、という凝った作りだったが、
Python版ではファイル配置の複雑さの割に価値が薄いため、標準の
`logging` モジュールに置き換える。

- 出力先: `config.log_dir()` 配下に `packaging_tool_YYYYMMDD.log`
  (設定画面で変えられる。決め方は `trace_log.apply_log_dir`)
- コンソール(Debug.Printのイミディエイト相当)にも同時出力

【後から追えるようにする仕掛け】(現場の声:「エラー等の後追いが現状
できない。ログを残し、なぜなぜで分析できるようにしてほしい」)

- **操作番号**: 画面からの要求1つごとに番号を振り、その要求の間に
  出た行すべての頭に `[番号]` を付ける。同じ番号で引けば、1つの操作で
  何が起きたかが(選定の細かい判断まで)まとめて読める
- **エラー記録**: ERROR 以上の行には**エラー番号**を振り、
  `エラー記録_YYYYMM.jsonl` に1件1行で残す。どの操作の途中だったか、
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
import secrets
import threading
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


def log_path_for(day: date) -> "Path":
    """その日のログファイル。**名前の作り方はここ1か所**。"""
    return config.log_dir() / f"packaging_tool_{day:%Y%m%d}.log"


def error_path_for(when: datetime) -> Path:
    """その月のエラー記録。月ごとに分ける(1件1行の JSON)。"""
    return config.log_dir() / f"エラー記録_{when:%Y%m}.jsonl"


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
    """ERROR 以上を `エラー記録_YYYYMM.jsonl` へ1件1行で残す。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            when = datetime.fromtimestamp(record.created)
            detail = getattr(record, "detail", "") or ""
            if record.exc_info:
                detail = (detail + "\n" if detail else "") + logging.Formatter().formatException(record.exc_info)
            ref = getattr(record, "ref", "")
            entry: dict[str, Any] = {
                "ref": ref,
                "at": when.strftime(DATE_FORMAT),
                "level": record.levelname,
                "where": record.name,
                "message": record.getMessage(),
                "detail": detail,
                "operation": current_operation() or {},
                "recent": [line for line in recent_lines() if ref not in line][-RECENT_LINES:],
                **_who(),
            }
            path = error_path_for(when)
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
    (`packaging_tool_YYYYMMDD.log` にそのまま書く)と噛み合わないため。
    日付を見て開き直すだけで足りる。
    """

    def __init__(self, day: date, **kwargs) -> None:
        self._day = day
        super().__init__(log_path_for(day), **kwargs)

    def emit(self, record: logging.LogRecord) -> None:
        # **書く直前に見る。** 日付が変わったこと・設定画面で出力先を
        # 変えたことは、次の1行で気づく
        self._day = date.today()
        target = os.path.abspath(log_path_for(self._day))
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
        if config._active_log_dir is not None:
            global fallback_why
            fallback_why = (f"{config._active_log_dir} に書けなくなったので、"
                            f"{config.LOG_DIR} に書いています"
                            f"({datetime.now():%Y/%m/%d %H:%M})")
            config._active_log_dir = None
            try:
                self.emit(record)
                return
            except Exception:                   # noqa: BLE001
                pass
        super().handleError(record)


# 指定の出力先に書けず、既定の場所へ戻したときの理由(画面に出す)
fallback_why = ""


def configure_logging() -> None:
    """アプリ起動時に一度だけ呼ぶ。二重呼び出しは無害(冪等)。"""
    global _configured
    if _configured:
        return

    config.ensure_dirs()

    root = logging.getLogger("packaging_tool")
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(LINE_FORMAT, datefmt=DATE_FORMAT)
    op_filter = _OpFilter()

    # エラー記録を先に置く ── 「直前の動き」にエラーの行そのものが
    # まだ入っていないうちに写す
    error_handler = _ErrorRecordHandler(level=logging.ERROR)
    error_handler.addFilter(op_filter)
    root.addHandler(error_handler)

    file_handler = DailyFileHandler(date.today(), encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(op_filter)
    root.addHandler(file_handler)

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
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.addFilter(op_filter)
        root.addHandler(console_handler)

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
