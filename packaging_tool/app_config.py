"""アプリ固有値の唯一の出どころ (`config/app.json` の読み取り)

社内「汎用Webアプリ作成 基盤仕様書」5.2 が、アプリごとに決める値
(アプリケーションID・表示名・使用ポート・ローカル保存フォルダー名・監視レベル)を
**複数ファイルへ直接書き散らさず、設定または共通定数へ集約する**ことを求めている。
このモジュールがその集約点になる。

【`config.py` との違い】
- `config.py`  … 業務の定数(テーブル名・Accessの置き場所・業務ルールの閾値)
- `app_config.py` … アプリという「入れ物」の値(ID・ポート・ローカル領域)

起動基盤(`start_app.py` / `launch_guard.py` / `server.py`)はこちらだけを見る。
業務コードはこちらを見ない。この分離が、基盤仕様書 2.5「起動処理とアプリ本体の分離」
にあたる。

【なぜ TOML ではなく JSON か】
`tomllib` は Python 3.11 以降の標準ライブラリで、3.9/3.10 では
追加パッケージ(`tomli`)が要る。依存を Flask と waitress の2つに抑える方針と、
README が掲げる Python 3.9 以上という条件の両方を満たすため JSON にした。
JSON はコメントを書けないので、各キーの説明はこのモジュールに置いてある。

【ローカル領域】(基盤仕様書 2.7 / 4.6)
ログ・キャッシュ・実行時情報・DBは、共有配置されうるアプリ本体側ではなく
利用者ごとのローカル領域へ置く。Windows は `%LOCALAPPDATA%`、
それ以外は `~/.local/share`(XDG)を使う。

    %LOCALAPPDATA%\\PackagingTool\\
      runtime\\  … PID・ポート・状態(停止後は消してよい)
      logs\\     … launcher.log / guard.log / app.log
      pycache\\  … PYTHONPYCACHEPREFIX の向き先
      cache\\    … 再取得できる高速化用データ
      work\\     … 帳票の一時HTMLなど
      backup\\   … DBのバックアップ
      data\\     … packaging_tool.db / user_config.json など**消してはいけない**もの

`data` は基盤仕様書の一覧には無い追加。仕様書 2.7 の目的は
「アプリ本体と、実行中に変化するファイルを分ける」ことであり、DBと利用者設定は
まさにそれにあたる。`cache`/`work` と違って消せないので、独立した名前にしている。

**この段階(Phase 0)では値を定義するだけで、DBの実際の移動は行わない。**
`config.DB_PATH` は従来どおり `<リポジトリ>/data/` を指している。
移動は Phase 2(起動基盤)で、旧ファイルを残したまま行う。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

# このファイルの2つ上 = リポジトリのルート(= 基盤仕様書のいう ApplicationRoot)
APP_ROOT = Path(__file__).resolve().parent.parent

# アプリ固有値の置き場所。環境変数で差し替えられるようにしておくと、
# 検証時に本番設定を書き換えずに試せる
CONFIG_PATH = Path(os.environ.get(
    "PACKAGING_TOOL_APP_CONFIG", str(APP_ROOT / "config" / "app.json")))

# モードは `packaging_tool/modes.py` が持つ。ここは**ポートを引くため**に
# 名前を借りるだけで、モードの定義を二重に持たない
from . import modes  # noqa: E402  (定数より先に読む必要がある)

# `config/app.json` が読めないときに使う値。
# 設定ファイルが壊れていても「起動はして、画面に理由を出す」ほうが、
# 起動そのものが失敗するより調査しやすい(基盤仕様書 ステップ5)。
_FALLBACK: dict[str, Any] = {
    "app_id": "nlm.packaging-tool",
    "display_name": "梱包資材総合ツール",
    "version": "0.0.0",
    "monitor_level": 2,
    "local_dir_name": "PackagingTool",
    "server": {
        "host": "127.0.0.1",
        "port_retry": 3,
        # 設定ファイルのキーは `roles` のまま。既に現場に配ってある
        # `config/app.json` を書き換えずに済ませる(値の意味は変わらない)
        "roles": {
            modes.FIELD: {"port": 8713},
            modes.MATERIAL: {"port": 8723},
        },
    },
    "monitoring": {
        "health_poll_seconds": 15,
        "job_poll_ms": 300,
    },
}

# 読み込み結果のキャッシュ。設定は起動中に変わらない
_cache: Optional[dict[str, Any]] = None
# 既定値へ落ちた理由(画面やログに出して原因を追えるようにする)
_load_error: str = ""


def load(*, force: bool = False) -> dict[str, Any]:
    """`config/app.json` を読む。壊れていても例外を投げず既定値へ落とす。"""
    global _cache, _load_error
    if _cache is not None and not force:
        return _cache

    _load_error = ""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("トップレベルがオブジェクトではありません")
        _cache = _merge(_FALLBACK, raw)
    except FileNotFoundError:
        _load_error = f"設定ファイルがありません: {CONFIG_PATH}"
        _cache = _copy(_FALLBACK)
    except Exception as exc:  # noqa: BLE001 - 理由を残して既定値で続行する
        _load_error = f"設定ファイルを読めませんでした ({CONFIG_PATH}): {exc}"
        _cache = _copy(_FALLBACK)
    _canonicalize_modes(_cache)
    return _cache


def _canonicalize_modes(conf: dict[str, Any]) -> None:
    """設定ファイルの旧いモード名をいまの名前に寄せる。

    現場に配ってある `config/app.json` は `warehouse` で書かれている。
    そのままだと既定値の `material` と**別のキーとして並んでしまい**、
    ポートを変えてあっても黙って無視される(変えたつもりで変わらない、
    という一番たちの悪い壊れ方になる)。読み込んだ時点で1つに寄せる。
    """
    table = conf.get("server", {}).get("roles")
    if not isinstance(table, dict):
        return
    for old, new in modes.LEGACY_NAMES.items():
        if old in table:
            # 書いてあるほうが利用者の意図。既定値を上書きする
            table[new] = table.pop(old)


def load_error() -> str:
    """既定値へ落ちた場合の理由。正常なら空文字。"""
    load()
    return _load_error


def _copy(value: Any) -> Any:
    """入れ子の dict を実体コピーする(既定値を書き換えさせない)。"""
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    return value


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """既定値に設定ファイルの値をかぶせる。

    設定ファイルに書き忘れたキーがあっても既定値で埋まるので、
    キーが1つ足りないだけで起動できなくなることがない。
    """
    result = _copy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = _copy(value)
    return result


# ------------------------------------------------------------------
# アプリの識別
# ------------------------------------------------------------------
def app_id() -> str:
    """起動確認APIで照合する識別子(基盤仕様書 2.3)。

    同じポートを別のアプリが使っている場合に、誤って「起動成功」と
    判定しないためのもの。単にHTTPが返るかどうかでは判別できない。
    """
    return str(load()["app_id"])


def display_name() -> str:
    return str(load()["display_name"])


# 版の書き方。**メジャー.マイナー.パッチ の3つの数字だけ**。
#
# 現場の端末はフォルダごとコピーして配るので、「いまどれが入っているか」を
# 聞かれたときに答えられる番号が要る。番号は `config/app.json` の
# `version` ただ1つが出どころで、画面(帯のバッジ)・`/api/health`・
# 設定画面はすべてここを読む。
#
# 上げ方は `docs/変更履歴.md` に書いてある。要約すると:
#   メジャー … 現場の手順が変わる(操作を覚え直す必要がある)
#   マイナー … できることが増える。手順はそのまま
#   パッチ   … 直しただけ。見た目も手順も変わらない
_VERSION_FORM = re.compile(r"^\d+\.\d+\.\d+$")

# 画面に出すときの前置き。「1.0.0」だけだと何の番号か分からない
VERSION_PREFIX = "VER"


def version() -> str:
    return str(load()["version"])


def version_label() -> str:
    """帯のバッジに出す形。例 `VER1.0.0`。"""
    return f"{VERSION_PREFIX}{version()}"


def version_problem() -> str:
    """版の書き方がおかしければ理由。正しければ空文字。

    番号が読めない形だと「どれが新しいのか」を並べて比べられなくなる。
    起動は止めず(版が読めなくても業務はできる)、設定画面に出す。
    """
    text = version()
    if _VERSION_FORM.match(text):
        return ""
    return (f"版の書き方が違います: {text!r}。"
            "config/app.json の version は「1.0.0」のように"
            "数字3つで書いてください。")


def monitor_level() -> int:
    """基盤仕様書 3章の監視レベル。本アプリは2(長時間処理あり)。"""
    return int(load()["monitor_level"])


# ------------------------------------------------------------------
# サーバ
# ------------------------------------------------------------------
def host() -> str:
    """待ち受けアドレス。`127.0.0.1` 固定にすることでLANから到達できなくなり、
    Windowsのファイアウォール警告も出ない。"""
    return str(load()["server"]["host"])


# デスクトップ版(Tauri)の窓が画面を読み込む宛先のホスト名。外枠
# (`src-tauri/src/main.rs` の `SCHEME`)と揃える。Windows の WebView2 は
# `http://app.localhost/`、ほかの OS は `app://localhost/` になる
BRIDGE_HOSTS = ("app.localhost", "localhost")


def _mode_conf(mode: str) -> dict[str, Any]:
    """設定ファイルのモード別の節。旧名(`warehouse`)でも引ける。

    ファイル側のキーは `load()` が寄せてあるので、ここで見るのは
    引数の旧名だけでよい。
    """
    table = load()["server"]["roles"]
    key = modes.normalize(mode)
    if key in table:
        return table[key]
    raise ValueError(
        f"未知のモード: {mode!r} (使えるのは {', '.join(modes.KEYS)})")


def port(mode: str = modes.FIELD) -> int:
    return int(_mode_conf(mode)["port"])


def port_candidates(mode: str = modes.FIELD) -> list[int]:
    """使用中だったときに順に試すポート。

    基盤仕様書 2.4 は多重起動の防止を求めているが、**別のアプリ**が
    そのポートを使っている場合もある。前者はロックファイルで判定して
    ブラウザだけ開き、後者はここの候補で回避する。
    """
    base = port(mode)
    retry = int(load()["server"]["port_retry"])
    return [base + i for i in range(retry + 1)]


def port_range_conflicts() -> list[str]:
    """モードごとの候補ポートが重なっていないかを調べる。

    重なっていると事故になる。現場が 8713 を取れずに繰り上がって
    資材の 8723 を使ってしまうと、資材モードを起動できません。
    利用者からは「資材モードが開けない」としか見えず原因が分かりません。

    `port` と `port_retry` は設定ファイルで変えられるので、
    変更のたびに機械が検査できるようにここに置く(テストが呼ぶ)。
    """
    problems: list[str] = []
    ranges = {mode: set(port_candidates(mode)) for mode in modes.KEYS}
    checked: set[frozenset[str]] = set()
    for a in modes.KEYS:
        for b in modes.KEYS:
            if a == b or frozenset((a, b)) in checked:
                continue
            checked.add(frozenset((a, b)))
            overlap = sorted(ranges[a] & ranges[b])
            if overlap:
                problems.append(
                    f"{a} と {b} の候補ポートが重なっています: {overlap}"
                    f" ({a}={sorted(ranges[a])} / {b}={sorted(ranges[b])})")
    return problems


# ------------------------------------------------------------------
# 監視
# ------------------------------------------------------------------
def health_poll_seconds() -> int:
    """ブラウザが生存確認を送る間隔(基盤仕様書 2.9)。"""
    return int(load()["monitoring"]["health_poll_seconds"])


def job_poll_ms() -> int:
    """取り込み進捗のポーリング間隔。tkinter版の `after(300)` に合わせてある。"""
    return int(load()["monitoring"]["job_poll_ms"])


# ------------------------------------------------------------------
# ユーザー別ローカル領域 (基盤仕様書 2.7 / 4.6)
# ------------------------------------------------------------------
def local_root() -> Path:
    """`%LOCALAPPDATA%\\<local_dir_name>`(非Windowsは XDG 相当)。

    環境変数 `PACKAGING_TOOL_LOCAL_DIR` で丸ごと差し替えられる。
    検証時に本番の領域を汚さずに試すための逃げ道。
    """
    override = os.environ.get("PACKAGING_TOOL_LOCAL_DIR")
    if override and override.strip():
        return Path(override.strip())

    name = str(load()["local_dir_name"])
    base = os.environ.get("LOCALAPPDATA")
    if base:                                   # Windows
        return Path(base) / name
    # Linux/macOS。XDGの慣習に従う(開発機とCI用)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / name
    return Path.home() / ".local" / "share" / name


# 領域の名前。実体の作成は `ensure_local_dirs()` で行う
LOCAL_SUBDIRS = ("runtime", "logs", "pycache", "cache", "work", "backup", "data")


def local_dir(name: str) -> Path:
    """ローカル領域の中のフォルダを1つ取る。"""
    if name not in LOCAL_SUBDIRS:
        raise ValueError(
            f"未知のローカル領域: {name!r} (使えるのは {', '.join(LOCAL_SUBDIRS)})")
    return local_root() / name


def ensure_local_dirs() -> Path:
    """ローカル領域を作る。既にあれば何もしない。"""
    root = local_root()
    for name in LOCAL_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def describe() -> str:
    """診断用の1枚。`start.bat` とログの先頭に出す(基盤仕様書 2.6)。"""
    lines = [
        f"アプリID      : {app_id()}",
        f"表示名        : {display_name()}",
        f"バージョン    : {version()}",
        f"監視レベル    : {monitor_level()}",
        f"設定ファイル  : {CONFIG_PATH}",
        f"アプリ本体    : {APP_ROOT}",
        f"ローカル領域  : {local_root()}",
    ]
    for mode in modes.ALL:
        lines.append(f"ポート({mode.label}): {port(mode.key)}"
                     f"  候補 {port_candidates(mode.key)}")
    if _load_error:
        lines.append(f"【注意】{_load_error} — 既定値で動作しています")
    for problem in port_range_conflicts():
        lines.append(f"【注意】{problem}")
    return "\n".join(lines)


if __name__ == "__main__":       # python -m packaging_tool.app_config で確認できる
    print(describe())
