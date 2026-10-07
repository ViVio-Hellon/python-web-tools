"""取り込みの診断記録(取り込み診断_YYYYMMDD.log)。

【なぜ別のファイルか】
「読まなくなった」と言われたとき、ふだんのログからは**何を読んで何を
見送ったのか**が組み立てられませんでした。見送った側は1行も出ない
(起動時の自動取り込みは、更新されていないファイルを黙って飛ばす)し、
読んだ側も件数しか残らないためです。

取り込みのたびに、次のことを1か所へ書きます。このファイルを送って
もらえば、画面を見に行かなくても原因を追えるようにするのが目的です。

    どこを探して、どのファイルを見つけたか(見つからなかったか)
    そのファイルの大きさ・更新時刻・中の表と行数
    読んだか見送ったか、見送ったならその理由
    表ごとの 元の行数 / 取り込んだ件数 / 飛ばした件数と理由 /
             足りない列・増えた列 / 手元の件数(前→後)
    仕掛ロットは ロット数(前→後)と、増えた・消えたロットの例

**取り込みそのものは止めません。** 書けなくても(ディスクが一杯など)
黙って続けます ── 診断のせいで取り込めなくなっては本末転倒です。
"""
from __future__ import annotations

import contextvars
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from . import config
from .logging_utils import get_logger

log = get_logger("import_diag")

# 入れ子の取り込み(まとめて取り込み → マスタ → 仕掛台帳)で見出しを
# 何度も書かないよう、いちばん外側だけが見出しを書く
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("import_diag_depth",
                                                             default=0)

# 例として並べるロット番号の数。全部並べると読めない
SAMPLE = 10


def path_for(day: Optional[date] = None) -> Path:
    return config.log_dir() / f"取り込み診断_{(day or date.today()):%Y%m%d}.log"


def latest_path() -> Optional[Path]:
    """いちばん新しい記録。今日のが無ければ、前の日のうち最新のもの。

    ログフォルダ(`%LOCALAPPDATA%\\PackagingTool\\logs`)は Windows では
    隠しフォルダの中にあり、現場からは探せない(「ログフォルダなんてない」)。
    設定画面のボタンから開けるように、ここで見つける。
    """
    today = path_for()
    if today.exists():
        return today
    try:
        found = sorted(today.parent.glob("取り込み診断*.log"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return found[0] if found else None


def write(text: str) -> None:
    """1行(複数行でもよい)を足す。書けなくても取り込みは止めない。"""
    try:
        path = path_for()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for line in str(text).splitlines() or [""]:
                fh.write(f"{datetime.now():%H:%M:%S} {line}\n")
    except Exception as exc:                       # noqa: BLE001 - 診断は止めない
        # OSError だけでなく、置き場所の名前がおかしい(ValueError)なども
        # 飲み込む。ここで漏れると取り込みそのものが止まる
        log.debug("取り込み診断を書けません: %s", exc)


@contextmanager
def run(label: str) -> Iterator[bool]:
    """取り込み1回ぶんの見出しと終わりを書く。入れ子なら何もしない。

    返す値は「いちばん外側か」。外側だけが結果のまとめに記録の場所を足す。
    """
    depth = _depth.get()
    token = _depth.set(depth + 1)
    outer = depth == 0
    started = time.time()
    try:
        # 見出しも try の内側で書く。外で落ちると深さが戻らず、
        # 以後の取り込みが全部「入れ子」扱いになって見出しが消える
        if outer:
            write("=" * 70)
            write(f"■ {label}  (版 {_version()})")
            write(f"  マスタの場所: {_safe(config.master_db_dir)}")
            write(f"  仕掛台帳の場所: {_safe(config.lot_db_dir)}")
            if config.lot_db_dir2() is not None:
                write(f"  仕掛台帳の2つ目の場所: {_safe(config.lot_db_dir2)}")
        yield outer
    finally:
        _depth.reset(token)
        if outer:
            write(f"■ 終わり ({time.time() - started:.1f}秒)")


def describe_file(label: str, path: Optional[Path],
                  searched: Iterable[Any] = ()) -> None:
    """見つけたファイル(見つからなければ探した場所)を書く。"""
    if path is None:
        where = ", ".join(str(s) for s in searched) or "(設定の場所)"
        write(f"[{label}] 見つかりません  探した場所: {where}")
        return
    try:
        stat = Path(path).stat()
        write(f"[{label}] {path}")
        write(f"    大きさ {stat.st_size:,} バイト / 更新 "
              f"{datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}")
    except OSError as exc:
        write(f"[{label}] {path}  (見に行けません: {exc})")


def decision(path: Path, read: bool, reason: str) -> None:
    """読んだか見送ったか。**見送ったほうこそ書く**(ふだんのログには出ない)。"""
    write(f"  {'読む' if read else '見送り'}: {Path(path).name} ── {reason}")


def local_count(conn: sqlite3.Connection, table: str) -> Optional[int]:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0])
    except sqlite3.Error:
        return None


def lot_numbers(conn: sqlite3.Connection, table: str) -> Optional[set[str]]:
    """ロット番号の集合(仕掛台帳の表だけ)。前後を比べて増減を出す。"""
    try:
        return {str(r[0]) for r in conn.execute(
            f"SELECT DISTINCT ロット番号 FROM [{table}]")}
    except sqlite3.Error:
        return None


def table_report(table: str, source: Path, *, source_rows: int,
                 missing: list[str], lacking_ok: list[str], extra: list[str],
                 imported: Optional[int], skipped: dict[str, int],
                 skipped_samples: list[str], before: Optional[int],
                 after: Optional[int], lots_before: Optional[set[str]] = None,
                 lots_after: Optional[set[str]] = None, error: str = "") -> None:
    """表1つぶん。"""
    write(f"  [{table}] ← {Path(source).name}  元の行数 {source_rows:,}")
    if missing:
        write(f"    元に無い列: {', '.join(missing)}")
    if lacking_ok:
        write(f"    元に無い列(無くてよい): {', '.join(lacking_ok)}")
    if extra:
        shown = ", ".join(extra[:15]) + (f" ほか{len(extra) - 15}列" if len(extra) > 15 else "")
        write(f"    取り込まない列: {len(extra)}列 ({shown})")
    if error:
        write(f"    ✕ 取り込めませんでした: {error}")
    if imported is not None:
        why = " / ".join(f"{k} {v:,}件" for k, v in skipped.items() if v)
        write(f"    取り込み {imported:,}件" + (f"  飛ばした {why}" if why else ""))
        if skipped_samples:
            write(f"    飛ばした行の例: {', '.join(skipped_samples[:SAMPLE])}")
    write(f"    手元の件数 {_n(before)} → {_n(after)}")
    if lots_before is not None and lots_after is not None:
        added = sorted(lots_after - lots_before)
        gone = sorted(lots_before - lots_after)
        write(f"    ロット数 {len(lots_before):,} → {len(lots_after):,}"
              f"  (増えた {len(added):,} / 消えた {len(gone):,})")
        if added:
            write(f"    増えた例: {', '.join(added[:SAMPLE])}")
        if gone:
            write(f"    消えた例: {', '.join(gone[:SAMPLE])}")


def _n(value: Optional[int]) -> str:
    return "?" if value is None else f"{value:,}"


def _safe(func) -> str:
    try:
        return str(func())
    except Exception as exc:                       # noqa: BLE001 - 診断は止めない
        return f"(分かりません: {exc})"


def _version() -> str:
    try:
        from . import app_config
        return app_config.version()
    except Exception:                              # noqa: BLE001
        return "?"
