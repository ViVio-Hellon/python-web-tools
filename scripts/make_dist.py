#!/usr/bin/env python3
r"""配布用フォルダを作る

【なぜ要るのか】
配るときに手でフォルダをコピーすると、**配ってはいけないもの**が紛れます。

    data\packaging_tool.db   … 端末ごとの手元のDB。別の端末のものを配ると、
                               その端末の「まだ送っていない発注・実績」まで持ち込む
    data\user_config.json    … その端末の設定(拠点・よく使う条件)
    tests\ / logs\ / export\ / __pycache__ / .git …

このスクリプトは**配るものだけ**を新しいフォルダへ写します。設定を一緒に
配りたいときは、先に設定画面の「配布設定」で書き出しておいてください
(ツールの直下の `配布設定\`。配った先が起動時に読み込みます)。

    python scripts\make_dist.py                     # ツールの隣に「資材複合ツール_VERx.y.z」
    python scripts\make_dist.py --out D:\配布\今回   # 置き場所を指定
    python scripts\make_dist.py --zip               # zip も作る
    python scripts\make_dist.py --no-settings       # 配布設定を入れない

【作ったあとに確かめること】
できたフォルダの `配布メモ.txt` に、版・入れた配布設定・配った先ですることを
書いてあります。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 直下で**配るもの**。`tests/test_web_settings.RepoRootTests.ALLOWED` から、
# 開発にしか使わないもの(tests・.gitignore・.gitattributes)を除いたもの
INCLUDE: tuple[str, ...] = (
    "README.md", "requirements.txt",
    "Start.vbs", "start.bat", "stop.bat",
    "start_app.py", "server.py", "boot_server.py", "launch_guard.py",
    "process_manager.py",
    # デスクトップ版の入口(exe が子として起動する)
    "bridge.py",
    "app", "config", "docs", "packaging_tool", "scripts", "vendor",
)

# リポジトリには置くが配らないもの(デスクトップ版の外枠のソースと、それを作る仕組み)。
# 配るのは作った exe だけ(下の `EXE_NAME`)
# .claude は Claude Code の開発環境の設定(クラウドのセッション開始時に試験の部品を入れる)。現場には要らない
DEV_ONLY: tuple[str, ...] = ("src-tauri", ".github", ".claude")

# デスクトップ版の exe。GitHub Actions(Windows)が作る `PackagingTool.exe` を、
# 配るときはこの名前でフォルダの直下に置く(押す物が分かる名前にする)
EXE_NAME = "梱包資材総合ツール.exe"
# 作る配布フォルダの名前(既定の置き場所 `ツールの隣\<これ>_VER版`)。現場の指示で「資材複合ツール」。
# 中の exe の名前・画面の名前は変えない(フォルダの名前だけ)
DIST_FOLDER_NAME = "資材複合ツール"
# exe を探す場所(先に見つかったもの)。`--exe` で指定もできる
EXE_CANDIDATES: tuple[str, ...] = (
    "PackagingTool.exe",
    "src-tauri/target/release/PackagingTool.exe",
)

# 中にあっても写さないもの(名前で見る。フォルダならその下ごと)
EXCLUDE_NAMES: tuple[str, ...] = (
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".coverage", ".coverage.*",
    "htmlcov", "*.tmp", "*.bak-*", ".DS_Store", "Thumbs.db",
)

# 配布設定のフォルダ(`packaging_tool/distribution.py` の置き場所と同じ)。
# **INCLUDE には入れない** ── 入れるかどうかは --no-settings で決める
SETTINGS = Path("配布設定")

# できたフォルダに**入っていてはいけない**もの(最後に確かめる)
FORBIDDEN: tuple[str, ...] = (
    "data/packaging_tool.db", "data/user_config.json", "tests", ".git",
)


def _version() -> str:
    try:
        return json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def _excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE_NAMES)


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if _excluded(n)}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _settings_lines(folder: Path) -> list[str]:
    """配布設定の中身を、メモと画面に出す形で(パスワードの値は出さない)。"""
    from packaging_tool import distribution
    lines = []
    path = distribution.settings_path(folder)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return [f"  (読めませんでした: {exc})"]
        for key, value in (data.get("settings") or {}).items():
            shown = "(設定済み)" if key == "admin_password" else value
            if isinstance(value, bool):
                shown = "する" if value else "しない"
            lines.append(f"  {distribution.ITEM_LABELS.get(key, key)}: {shown}")
        if data.get("created_at"):
            lines.append(f"  (作成 {data.get('created_at')} / {data.get('created_on', '')})")
    for key, label in distribution.MAPS:
        if distribution.map_file(key, folder).is_file():
            lines.append(f"  {label}: {distribution.MAPS_DIRNAME}\\{key}.json")
    return lines or ["  (中身がありません)"]


def find_exe() -> Path | None:
    for name in EXE_CANDIDATES:
        path = ROOT / name
        if path.is_file():
            return path
    return None


def build(out: Path, *, with_settings: bool = True, force: bool = False,
          make_zip: bool = False, exe: Path | None = None) -> tuple[Path, list[str]]:
    """配布用フォルダを作る。戻り値は (できたフォルダ, 画面に出す行)。

    断るときは `SystemExit`(理由の文つき)。
    """
    out = out.resolve()
    if _inside(out, ROOT):
        raise SystemExit(f"ツールのフォルダの中には作れません: {out}\n"
                         "(次に作るとき、前に作ったものまで写してしまいます)")
    if out.exists() and any(out.iterdir()):
        if not force:
            raise SystemExit(f"{out} はもうあって、中身があります。\n"
                             "別の場所を --out で指定するか、--force で作り直してください。")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    missing = []
    for name in INCLUDE:
        src = ROOT / name
        if not src.exists():
            missing.append(name)
            continue
        if src.is_dir():
            shutil.copytree(src, out / name, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(src, out / name)
    if missing:
        raise SystemExit("配るはずのファイルがありません: " + ", ".join(missing))

    # 配布設定: 入れる/入れないを**はっきり決める**(--no-settings)
    settings_src = ROOT / SETTINGS
    lines = [f"配布用フォルダを作りました: {out}", f"版: VER{_version()}"]

    # デスクトップ版の exe(あれば)。無くてもブラウザ版(Start.vbs)で動く
    exe = exe if exe is not None else find_exe()
    if exe is not None and exe.is_file():
        shutil.copy2(exe, out / EXE_NAME)
        lines.append(f"デスクトップ版を入れました: {EXE_NAME}(元: {exe})")
    else:
        lines.append("デスクトップ版(exe)は入っていません。GitHub Actions の"
                     "「デスクトップ版(Windows)」で作った PackagingTool.exe を、"
                     "ツールのフォルダの直下に置いてから作り直してください"
                     "(無くてもブラウザ版の Start.vbs で動きます)。")
    if with_settings and settings_src.is_dir():
        shutil.copytree(settings_src, out / SETTINGS, ignore=_ignore)
        lines.append(f"{SETTINGS} フォルダを入れました(配った先が起動時に読み込みます):")
        lines += _settings_lines(out / SETTINGS)
    else:
        lines.append("配布設定は入れていません。配った先で1台ずつ設定画面から"
                     "取り込み元を設定してください。")
        if with_settings:
            lines.append("  (設定画面の「配布設定」で書き出すと、次からは一緒に配れます)")

    # 入っていてはいけないものが無いか、最後に確かめる
    leaked = [p for p in FORBIDDEN if (out / p).exists()]
    leaked += [str(p.relative_to(out)) for p in out.rglob("*") if _excluded(p.name)]
    if leaked:
        shutil.rmtree(out)
        raise SystemExit("配ってはいけないものが入ったため、作るのをやめました: "
                         + ", ".join(sorted(set(leaked))))

    # 配った先で押すもの(現場の依頼)。指す先はその PC での場所なので、ここでは作らない
    lines.append("配った先で scripts\\make_shortcuts.vbs をダブルクリックすると、ツールのフォルダに"
                 "Start.vbs と exe のショートカットができます(指す先はその PC での場所)。")
    files = sum(1 for p in out.rglob("*") if p.is_file())
    lines.append(f"ファイル数: {files}")
    (out / "配布メモ.txt").write_text(_memo(lines), encoding="utf-8-sig")

    if make_zip:
        archive = shutil.make_archive(str(out), "zip", root_dir=out.parent,
                                      base_dir=out.name)
        lines.append(f"zip も作りました: {archive}")
    return out, lines


def _memo(lines: list[str]) -> str:
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return "\n".join([
        f"梱包資材総合ツール 配布メモ({today})",
        "",
        *lines,
        "",
        "配った先ですること",
        "  1. このフォルダを好きな場所に置く(以前の版のフォルダに上書きしない)",
        "  2. Python 3.9 以降と、Flask・waitress が入っているか確かめる",
        "     (入っていなければ: python -m pip install -r requirements.txt)",
        f"  3. {EXE_NAME} で起動する(デスクトップ版。ポートを使いません)。",
        "     exe が無いとき・動かないときは Start.vbs(ブラウザ版)で起動できます。",
        "     配布設定があれば、このとき読み込みます",
        "  4. 設定画面の「動作」で拠点を選ぶ(拠点はラインごとに違います)",
        "",
        "以前の版から入れ替えるとき(上の1〜4の代わり)",
        "  1. 古い版のツールを閉じる(終了ボタン)",
        "  2. 古いフォルダの data フォルダを、フォルダごと新しいフォルダへ写す",
        "     (中身: packaging_tool.db … まだ送れていない発注・受払・使用実績と取り込んだ控え",
        "            user_config.json … 置き場所・拠点などの設定",
        "            floor_plan.json / pallet_map.json … 配置図)",
        "     一部だけ写すと、写さなかったものは出荷時に戻ります",
        f"  3. 新しいフォルダの {EXE_NAME}(または Start.vbs)で起動する。拠点も設定も前のまま使えます",
        "  4. 別の端末の data は写さない(その端末の未送信分を持ち込みます)",
        "",
        "入れていないもの: data(端末ごとの中身)・tests・logs・export",
        "",
    ])


def default_out() -> Path:
    """既定の作る場所: ツールの隣の「資材複合ツール_VER版」。"""
    return ROOT.parent / f"{DIST_FOLDER_NAME}_VER{_version()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配布用フォルダを作る")
    parser.add_argument("--out", help=f"作る場所(既定: ツールの隣に「{DIST_FOLDER_NAME}_VER版」)")
    parser.add_argument("--no-settings", action="store_true",
                        help="配布設定(ツール直下の 配布設定 フォルダ)を入れない")
    parser.add_argument("--force", action="store_true",
                        help="作る場所に中身があれば消して作り直す")
    parser.add_argument("--zip", action="store_true", help="zip も作る")
    parser.add_argument("--exe", help="入れるデスクトップ版の exe(既定: 直下の PackagingTool.exe など)")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else default_out()
    try:
        _out, lines = build(out, with_settings=not args.no_settings,
                            force=args.force, make_zip=args.zip,
                            exe=Path(args.exe) if args.exe else None)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
