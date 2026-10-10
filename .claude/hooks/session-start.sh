#!/bin/bash
# Claude Code のクラウド開発環境(claude.ai/code)だけで、セッションの始めに
# 試験に要る部品を入れる。画面の試験(Flask)も含めて全件の試験を流せるようにする。
#
#   入れるもの: requirements.txt(Flask・waitress)と pytest
#               ── CI(.github/workflows/desktop-windows.yml)の「Python の部品を入れる」と同じ
#   入れる先  : リポジトリの外の仮想環境 ~/.cache/python-web-tools/venv(このコンテナの中だけ)
#               システムの Python には入れない(Debian が入れた古い blinker を pip が置き換えられず失敗するため)
#   使い方    : セッションの中では python3 / pytest がこの仮想環境のものになる(CLAUDE_ENV_FILE で PATH を前に足す)
#
# 現場の PC・配布フォルダ・ツールの起動(Start.vbs / start_app.py)には関係しない:
#   - CLAUDE_CODE_REMOTE=true のとき(クラウドのセッション)だけ動く。手元の Claude Code では何もしない
#   - .claude も仮想環境もリポジトリの配るものに入らない(scripts/make_dist.py の INCLUDE に無い・リポジトリの外)
#
# 何度流しても同じ(仮想環境があれば作り直さず、入っていれば pip は何もしない)。失敗したら理由を出して止める。
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:?CLAUDE_PROJECT_DIR がありません}"
VENV="${HOME}/.cache/python-web-tools/venv"

fail() {
  echo "【試験の部品を入れられませんでした】$1" >&2
  echo "  手で入れるとき: python3 -m venv \"$VENV\" && \"$VENV/bin/python\" -m pip install -r requirements.txt pytest" >&2
  exit 1
}

command -v python3 >/dev/null 2>&1 || fail "python3 が見つかりません"

if [ ! -x "$VENV/bin/python" ]; then
  mkdir -p "$(dirname "$VENV")"
  if ! log=$(python3 -m venv "$VENV" 2>&1); then
    fail "仮想環境を作れませんでした(python3-venv が無い可能性があります)。出力:
$log"
  fi
fi

if ! log=$("$VENV/bin/python" -m pip install --disable-pip-version-check --quiet -r requirements.txt pytest 2>&1); then
  fail "pip install が失敗しました(ネットワーク・プロキシ・版の指定を確かめてください)。pip の出力:
$log"
fi

"$VENV/bin/python" - <<'PY' || fail "入れたはずの部品を読み込めません(上の Python の出力を見てください)"
import importlib.metadata as meta
import flask, waitress, pytest  # noqa: F401  読み込めることを確かめる
print("試験の部品: " + " / ".join(f"{n} {meta.version(n)}" for n in ("Flask", "waitress", "pytest")))
PY

# このセッションの python3 / pytest を仮想環境のものにする
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export VIRTUAL_ENV=\"$VENV\""
    echo "export PATH=\"$VENV/bin:\$PATH\""
  } >> "$CLAUDE_ENV_FILE"
fi
