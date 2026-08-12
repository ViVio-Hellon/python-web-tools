"""サーバ側のフォルダ参照 (VBA `filedialog.askdirectory` の置き換え)

設定画面の「取り込み元の置き場所」を決めるために、**サーバから
見えるフォルダ**を一覧する。

【なぜ要るのか】
ブラウザのファイル選択ダイアログはクライアント側のパスしか返さない。
アプリが読むのはサーバ(=このPC)から見たパスなので、選ばせても意味が
無い。Phase 4 ではパスの直接入力だけにして先送りにしていた。

【何を返すか / 返さないか】
- **フォルダの名前**と、そこにある **取り込み元ファイルの名前**だけ
- ファイルの中身は返さない。読み取り口をここに作らない

一覧そのものが目的の機能なので「上のフォルダへ行けないようにする」
たぐいの制限は付けない ── それでは置き場所を探せない。守りは
`app/__init__.py` の
**127.0.0.1 バインド + 起動トークン + Host検証 + 同一オリジン確認**が担う。

代わりに、**名前しか出さない**ことと**件数に上限を置く**ことで、
ここが「サーバの中を読む窓口」にならないようにしてある。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import source_db
from ..logging_utils import get_logger

log = get_logger("presenters.fs_browse")

# 1回に返す上限。共有フォルダには数千の項目があることがあるので、
# 全部返すと画面もJSONも重くなる
MAX_ENTRIES = 300

# 探しているファイル。これ以外の名前は出さない。
# 取り込み元の種類は `source_db` が決めるので、ここで別に持たない
SOURCE_SUFFIXES = source_db.SUFFIXES


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool = True


@dataclass
class BrowseView:
    """いま見ているフォルダ。"""

    path: str = ""
    parent: str = ""          # 上のフォルダ(無ければ空)
    exists: bool = False
    readable: bool = False
    message: str = ""
    dirs: list[Entry] = field(default_factory=list)
    files: list[Entry] = field(default_factory=list)
    truncated: bool = False
    # 一覧の出発点。Windows のドライブや %LOCALAPPDATA% など
    roots: list[Entry] = field(default_factory=list)
    # 相対で打たれたときの起点。**どこから見た相対か**を画面に出す
    relative_to: str = ""
    # ファイルを指されたとき、その名前(入れ物を開いたうえで印を付ける)
    picked: str = ""


def browse(path_text: str) -> BrowseView:
    """`path_text` のフォルダを一覧する。空なら出発点だけを返す。"""
    view = BrowseView(path=path_text, roots=_roots())
    if not path_text.strip():
        view.message = "フォルダを選んでください。"
        return view

    # **相対でも開ける。** どこから見た相対かは1か所で決まっている
    # (`config.resolve_dir` = アプリのフォルダから)。決まっていれば
    # 曖昧ではないので、断る理由が無い
    from .. import config

    try:
        path = config.resolve_dir(path_text)
    except ValueError:                           # pragma: no cover - 上で弾く
        view.message = "フォルダを選んでください。"
        return view
    if config.is_relative_setting(path_text):
        view.relative_to = str(config.BASE_DIR)

    # ファイルを指されたら、その入れ物を開く。「参照」で目当ての
    # ファイルを見つけたとき、いちいち親へ戻らせない
    try:
        if path.exists() and not path.is_dir():
            view.picked = path.name
            path = path.parent
    except OSError:
        pass

    view.path = str(path)
    view.parent = str(path.parent) if path.parent != path else ""
    if not path.exists():
        view.message = "そのフォルダはこのPCから見えません。"
        return view
    view.exists = True
    if not path.is_dir():                        # pragma: no cover - 上で畳む
        view.message = "フォルダではありません。"
        return view

    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        # 権限が無い・切断された共有フォルダなど。理由を出す
        log.info("フォルダを読めません(%s): %s", path, exc)
        view.message = f"このフォルダを読めません({exc.strerror or exc})。"
        return view

    view.readable = True
    for entry in entries:
        if len(view.dirs) + len(view.files) >= MAX_ENTRIES:
            view.truncated = True
            break
        try:
            is_dir = entry.is_dir()
        except OSError:                       # 壊れたリンクなど
            continue
        if is_dir:
            view.dirs.append(Entry(name=entry.name, path=str(entry), is_dir=True))
        elif entry.suffix.lower() in SOURCE_SUFFIXES:
            # 探しているファイルだけ出す。あることが分かれば十分で、
            # 中身を読ませる口はここに作らない
            view.files.append(Entry(name=entry.name, path=str(entry),
                                    is_dir=False))

    if view.files:
        view.message = f"取り込み元ファイル {len(view.files)} 件が見つかりました。"
    elif not view.dirs:
        view.message = "このフォルダは空です。"
    if view.truncated:
        view.message += f"(先頭 {MAX_ENTRIES} 件まで表示しています)"
    return view


def _roots() -> list[Entry]:
    """一覧の出発点。

    Windows は接続されているドライブ、それ以外は `/` とホーム。
    「どこから始めればよいか分からない」を作らない。
    """
    import string
    import sys

    roots: list[Entry] = []
    if sys.platform.startswith("win"):
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            try:
                if drive.exists():
                    roots.append(Entry(name=f"{letter}:", path=str(drive)))
            except OSError:
                continue
    else:
        for candidate in (Path("/"), Path.home()):
            roots.append(Entry(name=str(candidate), path=str(candidate)))
    return roots


def to_dict(view: BrowseView) -> dict[str, Any]:
    return {
        "path": view.path,
        "parent": view.parent,
        "exists": view.exists,
        "readable": view.readable,
        "message": view.message,
        "dirs": [_entry(e) for e in view.dirs],
        "files": [_entry(e) for e in view.files],
        "truncated": view.truncated,
        "roots": [_entry(e) for e in view.roots],
        "relative_to": view.relative_to,
        "picked": view.picked,
    }


def _entry(entry: Entry) -> dict[str, Any]:
    return {"name": entry.name, "path": entry.path, "is_dir": entry.is_dir}
