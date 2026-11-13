#!/usr/bin/env python3
"""Claude Code の圧縮(コンテキスト整理)で作業の状態を失わないための見張り。標準ライブラリだけ。

    precompact    圧縮の直前(PreCompact。auto / manual の両方)
                  - .claude/SESSION_STATE.md を .claude/state_backups/ へ控える(新しい5つだけ残す)
                  - SESSION_STATE.md の「圧縮時点のGit」の欄を、いまのブランチ・HEAD・変更ファイルで**置き換える**
                    (足していかない)
                  - 何が起きても終了コード 0(圧縮を止めない)
    sessionstart  起動・再開・圧縮のあと(SessionStart)
                  - SESSION_STATE.md の中身を標準出力へ出す(Claude の文脈に入る)

Windows でも動く(パスは pathlib・git は無ければ飛ばす・文字は UTF-8 で出す)。
プロジェクトごとに独立(CLAUDE_PROJECT_DIR か、このファイルの2つ上をプロジェクトとみなす)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

KEEP_BACKUPS = 5
GIT_BEGIN = "<!-- git-snapshot:begin -->"
GIT_END = "<!-- git-snapshot:end -->"


def project_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env) if env else Path(__file__).resolve().parent.parent.parent


def state_path() -> Path:
    return project_dir() / ".claude" / "SESSION_STATE.md"


def _out(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=project_dir(), capture_output=True, timeout=10)
        return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_snapshot(trigger: str) -> str:
    branch = _git("branch", "--show-current") or "(不明)"
    head = _git("log", "--oneline", "-1") or "(不明)"
    status = _git("status", "--short") or "(変更なし、または git を読めない)"
    lines = status.splitlines()
    if len(lines) > 40:
        lines = lines[:40] + [f"... ほか {len(lines) - 40} 件"]
    return "\n".join([
        GIT_BEGIN,
        "## 圧縮時点のGit(フックが自動で置き換える。手で書かない)",
        f"- 記録: {time.strftime('%Y-%m-%d %H:%M:%S')}(圧縮: {trigger})",
        f"- ブランチ: {branch}",
        f"- HEAD: {head}",
        "- 変更ファイル:",
        "```", *lines, "```",
        GIT_END,
    ])


def replace_snapshot(text: str, snap: str) -> str:
    b, e = text.find(GIT_BEGIN), text.find(GIT_END)
    if b != -1 and e > b:
        return text[:b] + snap + text[e + len(GIT_END):]
    return text.rstrip("\n") + "\n\n" + snap + "\n"


def precompact() -> None:
    trigger = "unknown"
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        trigger = str(data.get("trigger") or "unknown")
    except (ValueError, OSError):
        pass
    path = state_path()
    if not path.exists():
        return  # 読めない・無いときは作らない(書かない・消さない)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    backups = path.parent / "state_backups"
    try:
        backups.mkdir(exist_ok=True)
        (backups / f"SESSION_STATE-{time.strftime('%Y%m%d-%H%M%S')}-{trigger}.md").write_text(text, encoding="utf-8")
        for old in sorted(backups.glob("SESSION_STATE-*.md"))[:-KEEP_BACKUPS]:
            old.unlink()
    except OSError:
        pass
    new = replace_snapshot(text, git_snapshot(trigger))
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def sessionstart() -> None:
    try:
        source = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}").get("source", "")
    except (ValueError, OSError, AttributeError):
        source = ""
    path = state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    _out(f"【作業状態の引き継ぎ({source or 'start'})】以下は {path} の内容です。"
         "作業目的・未完了の作業・次に実行する作業を確かめてから続けてください。"
         "作業が進んだら、このファイルを更新してください(完了済みは短くまとめ、ためこまない)。\n\n"
         + text)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        {"precompact": precompact, "sessionstart": sessionstart}.get(mode, lambda: None)()
    except Exception as exc:  # noqa: BLE001  圧縮・起動を決して止めない
        sys.stderr.write(f"session_state.py: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
