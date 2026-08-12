"""ログ出力 (VBA `DebugLog`/`InitializeDebugLog` 相当)

VBA版はFileSystemObjectで手動追記し、さらに端末のライン名を
レジストリから推測してサブフォルダ分けする、という凝った作りだったが、
Python版ではファイル配置の複雑さの割に価値が薄いため、標準の
`logging` モジュールに置き換える。

- 出力先: config.LOG_DIR 配下に `packaging_tool_YYYYMMDD.log`
- コンソール(Debug.Printのイミディエイト相当)にも同時出力
"""
from __future__ import annotations

import logging
from datetime import date

from . import config

_configured = False


def configure_logging() -> None:
    """アプリ起動時に一度だけ呼ぶ。二重呼び出しは無害(冪等)。"""
    global _configured
    if _configured:
        return

    config.ensure_dirs()
    log_file = config.LOG_DIR / f"packaging_tool_{date.today():%Y%m%d}.log"

    root = logging.getLogger("packaging_tool")
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s",
                                   datefmt="%Y/%m/%d %H:%M:%S")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

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
    出力先は `PACKAGING_TOOL_LOG_DIR` で変えられる。
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
