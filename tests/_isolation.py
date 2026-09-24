"""テストが本物の設定・記録を書き換えないようにする

【なぜ `tests/__init__.py` だけでは足りないのか】
`tests/__init__.py` は環境変数を先に立てることで `packaging_tool.config` に
一時フォルダを掴ませている。これは**その `__init__.py` が読まれる場合に
限って**効く。ところが unittest の探索は、開始フォルダをそのまま
最上位フォルダとして扱うと、テストを**パッケージ配下ではなく単体の
モジュールとして**取り込む。つまり:

    python3 -m unittest discover -s tests        # tests/__init__.py は読まれない
    python3 -m unittest discover -s tests -t .   # 読まれる(READMEはこちら)

前者で流すと安全網が丸ごと外れ、`data/user_config.json`(追跡対象)に
テストの値が書き込まれ、`%LOCALAPPDATA%` の記録も本番と混ざる。
**黙って起きる**のが厄介で、テストは全部OKのまま作業ツリーだけが汚れる。

そこでこのモジュールが、環境変数に頼らず**いま掴んでいるパスを見て**
リポジトリ内や本番領域を指していたら一時フォルダへ向け直す。

【なぜ取り込み時に直せば間に合うのか】
unittest はテストを**全部取り込んでから**走らせる。書き込みが起きるのは
走っている最中なので、どれか1つのテストモジュールが取り込み時にここを
呼んでいれば、その時点で全テストぶんの向き先が直っている
(`tests/test_isolation.py` が呼んでいる)。
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

# 何度呼ばれても1回だけ効かせる(取り込み順に依存させない)
_done = False


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def _escape(name: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix=f"packaging_tool_test_{name}_"))
    atexit.register(shutil.rmtree, tmp, True)
    return tmp


def ensure_isolated() -> list[str]:
    """危ない向き先を一時フォルダへ替える。替えた項目の名前を返す。

    既に `tests/__init__.py` が逃がしてあれば何もしない(戻り値は空)。
    """
    global _done
    if _done:
        return []
    _done = True

    from packaging_tool import config, logging_utils

    repo = Path(config.BASE_DIR)
    moved: list[str] = []

    # 利用者設定: リポジトリの `data/user_config.json` は**追跡対象**。
    # ここに書かれると、テストを流しただけで差分が出る
    if _under(config.USER_CONFIG_PATH, repo):
        config.USER_CONFIG_PATH = _escape("config") / "user_config.json"
        moved.append("USER_CONFIG_PATH")

    # 手元のDB: `data/packaging_tool.db` も**追跡対象**。
    #
    # 大半のテストは `:memory:` を使うが、`create_app()` は起動時に
    # `アクセス権限` を引くため**本物を開く**。開くだけでも
    # `PRAGMA journal_mode=WAL` がファイルを書き換えるので、流しただけで
    # 差分が出る(実際に踏んだ)。
    if _under(config.DB_PATH, repo):
        config.DB_PATH = _escape("db") / "packaging_tool.db"
        moved.append("DB_PATH")

    # ログと、ロック・長時間処理の記録が入るローカル領域。
    # 本番の端末で流すと、動いているアプリの記録に混ざる
    for name in ("LOG_DIR",):
        current = getattr(config, name)
        if _under(current, repo) or not _under(current, Path(tempfile.gettempdir())):
            setattr(config, name, _escape(name.lower()))
            moved.append(name)

    # 配布設定: ツールのフォルダの直下の `配布設定/`。
    # 書き出しの試験を流すと、配るつもりのないフォルダができる
    from packaging_tool import distribution
    if _under(distribution.DIR, repo):
        distribution.DIR = _escape("dist") / "配布設定"
        moved.append("distribution.DIR")

    logging_utils.silence_console()
    return moved
