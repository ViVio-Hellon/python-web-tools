"""テストを走らせる前の共通準備

`packaging_tool.logging_utils.get_logger()` は**インポートされた時点で**
ログ出力を初期化し、`config.LOG_DIR`(既定は `<リポジトリ>/logs/`)へ
ファイルを開き、コンソールにも同じ内容を流す。

そのままテストを流すと:

- リポジトリ内の `logs/packaging_tool_YYYYMMDD.log` に毎回8,000行以上が
  追記され、**実行しただけで作業ツリーが変更済みになる**
- コンソールに600行以上のDEBUG/INFOが混ざり、テストの結果が読みにくい

どちらもテストの副作用であって、確認したい中身ではない。
そこで**このパッケージが読み込まれた時点で**、ログの出力先を一時フォルダへ
向け、コンソールへの出力を止める。`config.LOG_DIR` はモジュール読み込み時に
環境変数を1度だけ読むので、`packaging_tool` を import する前に設定する必要が
ある。`unittest discover -s tests` も `python -m unittest tests.test_xxx` も、
テストモジュールより先にこの `__init__.py` を読むため、ここが正しい場所になる。

なおログ自体は止めない。一時フォルダには出るので、テストが落ちたときに
`PACKAGING_TOOL_LOG_DIR` を指定して流し直せば中身を確認できる。

    PACKAGING_TOOL_LOG_DIR=/tmp/ログ python3 -m unittest discover -s tests

移行後(Phase 2)はログの既定の置き場所そのものが `%LOCALAPPDATA%` 配下へ
移る(基盤仕様書 2.7)。そうなればリポジトリが汚れる問題は根本から消えるが、
それまでの間もテストは静かに流せるようにしておく。
"""
from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path


def _remove_tree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# --- 1. ログの出力先を一時フォルダへ ---------------------------------
# 利用者が明示的に指定していれば尊重する(落ちたときの調査用)
if not os.environ.get("PACKAGING_TOOL_LOG_DIR"):
    _log_dir = tempfile.mkdtemp(prefix="packaging_tool_test_logs_")
    os.environ["PACKAGING_TOOL_LOG_DIR"] = _log_dir
    atexit.register(_remove_tree, _log_dir)


# --- 2. 利用者設定とローカル領域も一時フォルダへ ----------------------
# `user_settings` は `<リポジトリ>/data/user_config.json` に書く。
# 設定画面のテストは置き場所の設定を保存するので、そのまま流すと
# **実行しただけで作業ツリーが変更済みになる**(ログと同じ問題)。
#
# ローカル領域(`%LOCALAPPDATA%`)にはロックファイルと長時間処理の記録が
# 入る。本番の領域を汚さないよう、こちらも逃がしておく。
for _var, _name in (("PACKAGING_TOOL_CONFIG_PATH", "user_config.json"),
                    # 手元のDBも**追跡対象**。`create_app()` は起動時に
                    # `アクセス権限` を引くため本物を開き、開くだけでも
                    # `PRAGMA journal_mode=WAL` がファイルを書き換える
                    ("PACKAGING_TOOL_DB_PATH", "packaging_tool.db"),
                    ("PACKAGING_TOOL_LOCAL_DIR", "")):
    if os.environ.get(_var):
        continue                         # 明示指定があれば尊重する(調査用)
    _dir = tempfile.mkdtemp(prefix="packaging_tool_test_")
    os.environ[_var] = str(Path(_dir) / _name) if _name else _dir
    atexit.register(_remove_tree, _dir)


# --- 3. コンソールへの出力を止める -----------------------------------
# 出力先をぜんぶ決めたあとに import する(`config` は読み込み時に
# 環境変数を1度だけ読むため、順序を入れ替えてはいけない)
from packaging_tool import logging_utils  # noqa: E402

logging_utils.silence_console()


# --- 4. この __init__.py が読まれない流しかたへの備え -----------------
# `python3 -m unittest discover -s tests`(`-t .` 無し)は、テストを
# パッケージ配下ではなく単体モジュールとして取り込むため、**ここまでの
# 準備が丸ごと効かない**。そのときは `tests/_isolation.py` が取り込み時に
# 向き先を見て直す。ここでも呼んでおけば、どちらの流しかたでも同じ状態
# から始まる(既に逃がしてあれば何もしない)。
from . import _isolation  # noqa: E402

_isolation.ensure_isolated()
