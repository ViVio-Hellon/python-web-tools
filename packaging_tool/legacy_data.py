"""ツールのフォルダの中に書いていた運用データを、フォルダの外へ移す(VER4.8.125)。

業務ツール統合ランチャー 1.8.9 からの依頼:「ツールのフォルダーは更新のたびに丸ごと上書きされる前提にしてください。
データベース・利用者が変えた設定・ログ・作ったファイルを、ツールのフォルダーの中に書かないでください」
「すでに中に書いているなら、外へ移し、前の場所にあるものは初回に読み込む(または移す)」。

VER4.8.124 までは、次のものの既定の置き場所が**ツールのフォルダの中**(`data\\`・`export\\`)だった:

    data\\packaging_tool.db   作業用DB(取り込み直せるが、まだ送っていない発注などが入っていることがある)
    data\\user_config.json    この端末の設定(置き場所・拠点・ログの出力先・管理者パスワード…)
    data\\floor_plan.json     この端末で直した配置図
    data\\pallet_map.json     この端末で直した保管位置マップ
    export\\                  書き出したファイル

VER4.8.125 からはローカル領域(`%LOCALAPPDATA%\\PackagingTool\\data\\`・`export\\`)。起動のはじめに、
**新しい場所にまだ無いものだけ**、前の場所から写す(前の場所のものは消さない ── 共有のフォルダから
動かしていると、ほかの PC もそこから写すため。次の更新でフォルダごと入れ替わる)。

**読めないときは写さない**(VER4.8.122 の決まり)。新しい場所に途中までのファイルを残さない。
環境変数で場所を決めているとき(`PACKAGING_TOOL_DB_PATH` など。試験・診断用)は何もしない。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from . import config
from .logging_utils import get_logger

log = get_logger("legacy_data")

# 前の版の置き場所(ツールのフォルダの中)
LEGACY_DATA_DIR = config.BASE_DIR / "data"
DB_NAME = "packaging_tool.db"
CONFIG_NAME = "user_config.json"
MAP_NAMES = ("floor_plan.json", "pallet_map.json")


def _exists(path: Path) -> bool:
    """**あると確かめられた**ときだけ True(届かない・読めないは「無い」にしない。呼び手は写さない)。"""
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False


def _copy_db(src: Path, dest: Path) -> None:
    """DB を写す。**書いている途中の姿を写さない**よう SQLite の backup で(-wal の中身も入る)。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".移し中")
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.replace(tmp, dest)


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".移し中")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def plan() -> list[tuple[Path, Path, str]]:
    """(前の場所, 新しい場所, 何か)。環境変数で決めた場所・前と同じ場所は入れない。"""
    out: list[tuple[Path, Path, str]] = []
    if "PACKAGING_TOOL_DB_PATH" not in os.environ:
        out.append((LEGACY_DATA_DIR / DB_NAME, config.DB_PATH, "作業用DB"))
        for name in MAP_NAMES:
            out.append((LEGACY_DATA_DIR / name, config.DB_PATH.parent / name, "直した図"))
    if "PACKAGING_TOOL_CONFIG_PATH" not in os.environ:
        out.append((LEGACY_DATA_DIR / CONFIG_NAME, config.USER_CONFIG_PATH, "この端末の設定"))
    return [(src, dest, what) for src, dest, what in out
            if os.path.normcase(os.path.abspath(src)) != os.path.normcase(os.path.abspath(dest))]


def migrate() -> list[str]:
    """前の場所(ツールのフォルダの中)にあって、新しい場所にまだ無いものを写す。写したものの一言を返す。"""
    notes: list[str] = []
    for src, dest, what in plan():
        try:
            if _exists(dest) or not _exists(src):
                continue
        except OSError as exc:
            log.warning("%s を確かめられないので写しません(%s → %s): %s", what, src, dest, exc)
            continue
        try:
            if src.name == DB_NAME:
                _copy_db(src, dest)
            else:
                _copy_file(src, dest)
        except (OSError, sqlite3.Error) as exc:
            Path(str(dest) + ".移し中").unlink(missing_ok=True)
            log.warning("%s を前の場所から写せませんでした(写さずに続けます。前の場所はそのまま): %s → %s: %s",
                        what, src, dest, exc, extra={"keep": True, "kind": "前の場所から写せない"})
            continue
        note = f"{what}を前の場所(ツールのフォルダの中)から写しました: {src} → {dest}"
        log.info("%s", note)
        notes.append(note)
    return notes
