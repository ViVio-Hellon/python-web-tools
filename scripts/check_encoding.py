#!/usr/bin/env python3
"""取り込み元の文字を調べる ── **壊れているのか、読み方の問題なのか**

【なぜ要るのか】
文字化けの原因は2つに1つで、**直し方がまったく違います。**

    (A) 読み方の問題   … ファイルは無事。こちらの読み方が違うだけ。
                          取り込み直せば直る
    (B) ファイルが壊れている … 変換の時点で字が失われている。
                          取り込み直しても直らない。変換をやり直す

画面の文字を見比べても、この2つは区別が付きません。どちらも同じように
化けて見えるからです。**バイトを見れば区別が付く**ので、それをここで
やります。

    python3 scripts/check_encoding.py                  # 設定の置き場所から探す
    python3 scripts/check_encoding.py --file X.sqlite3 # ファイルを指定
    python3 scripts/check_encoding.py --table 仕掛 --column 用途名
    python3 scripts/check_encoding.py --samples 20     # 出す例の数(既定5)

【何を見ているか】
1. このファイルの文字がどちらの入れ方で入っているか(`sniff_encoding`)
2. **すでに `�` が入っていないか** ── 入っていたら (B)。
   `�` は「読めなかった字」の跡で、**元のバイトはもう戻りません**
3. 判定した入れ方で読めない値が無いか ── あれば (B) の疑い
4. 実際の値を、生のバイトと合わせて出す(目で確かめられるように)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import config, data_sync, source_db  # noqa: E402

# 「読めなかった字」の跡。これが入っていたら元のバイトは戻らない
REPLACEMENT = "�"


def find_files(explicit: str = "") -> list[Path]:
    """調べるファイル。指定が無ければ、設定の置き場所から全部拾う。"""
    if explicit:
        return [Path(explicit)]
    found: list[Path] = []
    try:
        material = data_sync.find_material_db()
        if material:
            found.append(material)
    except OSError:
        pass
    try:
        found.extend(data_sync.find_lot_dbs().values())
    except OSError:
        pass
    try:
        kanban = data_sync.find_kanban_db()
        if kanban:
            found.append(kanban)
    except OSError:
        pass
    return found


def text_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    with source_db.identifiers_as_utf8(conn):
        return [r[1] for r in conn.execute(
            f"PRAGMA table_info({source_db.quote_identifier(table)})")
            if str(r[2]).upper().startswith("TEXT")]


def tables_of(conn: sqlite3.Connection) -> list[str]:
    with source_db.identifiers_as_utf8(conn):
        return [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def inspect(path: Path, *, only_table: str = "", only_column: str = "",
            samples: int = 5) -> bool:
    """1ファイルを調べる。**壊れていたら True** を返す。"""
    print(f"\n{'=' * 66}\n{path}\n{'=' * 66}")

    probe = source_db.probe(path)
    if not probe.ok:
        print(f"  開けませんでした: {probe.error}")
        return False

    encoding = probe.encoding or source_db.ENCODING_UTF8
    label = "Shift-JIS(CP932)" if encoding == source_db.ENCODING_CP932 else "UTF-8"
    print(f"  文字の入れ方の判定 : {label}")
    print(f"  開き方             : {probe.opened_by}")
    print(f"  テーブル           : {len(probe.tables)}個")

    damaged = 0        # すでに `�` が入っている値(=もう戻らない)
    unreadable = 0     # どちらの入れ方でも読めない値
    checked = 0
    shown = 0

    conn = source_db._connect(path, read_only=True)
    try:
        names = [only_table] if only_table else tables_of(conn)
        for table in names:
            try:
                columns = text_columns(conn, table)
            except sqlite3.Error:
                continue
            if only_column:
                columns = [c for c in columns if c == only_column]
            if not columns:
                continue
            picked = ", ".join(source_db.quote_identifier(c) for c in columns)
            saved = conn.text_factory
            conn.text_factory = bytes          # 生のバイトで確かめる
            try:
                rows = conn.execute(
                    f"SELECT {picked} FROM "
                    f"{source_db.quote_identifier(table)}").fetchall()
            except sqlite3.Error as exc:
                print(f"  {table}: 引けません ({exc})")
                continue
            finally:
                conn.text_factory = saved

            for row in rows:
                for column, value in zip(columns, row):
                    if not isinstance(value, bytes) or value.isascii():
                        continue
                    checked += 1
                    # **すでに壊れている**かどうかが、いちばん知りたいこと
                    if REPLACEMENT.encode("utf-8") in value:
                        damaged += 1
                        if shown < samples:
                            shown += 1
                            print(f"\n  [壊れています] {table}.{column}")
                            print(f"    バイト   : {value[:40].hex(' ')}")
                            print(f"    読んだ形 : "
                                  f"{value.decode(encoding, 'replace')[:40]!r}")
                        continue
                    try:
                        value.decode(encoding)
                    except UnicodeDecodeError:
                        unreadable += 1
                        if shown < samples:
                            shown += 1
                            print(f"\n  [{label}で読めません] {table}.{column}")
                            print(f"    バイト   : {value[:40].hex(' ')}")
    finally:
        conn.close()

    print(f"\n  日本語を含む値     : {checked}件")
    print(f"  すでに壊れている値 : {damaged}件")
    print(f"  判定した入れ方で読めない値: {unreadable}件")

    if damaged:
        print("\n  → (B) 取り込み元のファイルが壊れています。")
        print("     読めなかった字の跡(\\ufffd)が**元のファイルに入っています**。")
        print("     元のバイトは失われているので、取り込み直しても直りません。")
        print("     Access からの変換をやり直してください。")
        return True
    if unreadable:
        print("\n  → 判定した入れ方で読めない値があります。")
        print("     判定が違うか、その値だけ壊れている可能性があります。")
        return True
    print("\n  → (A) 取り込み元は無事です。読み方の問題でした。")
    print("     「まとめて取り込み」で入れ直せば直ります。")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="取り込み元の文字を調べる(壊れているのか読み方か)")
    parser.add_argument("--file", default="", help="調べるファイル")
    parser.add_argument("--table", default="", help="この表だけ")
    parser.add_argument("--column", default="", help="この列だけ")
    parser.add_argument("--samples", type=int, default=5, help="出す例の数")
    args = parser.parse_args()

    files = find_files(args.file)
    if not files:
        print("取り込み元が見つかりません。--file で指定してください。")
        print(f"  探した場所: {config.master_db_dir()} / {config.lot_db_dir()}")
        return 1

    bad = False
    for path in files:
        if inspect(path, only_table=args.table, only_column=args.column,
                   samples=args.samples):
            bad = True

    print(f"\n{'=' * 66}")
    if bad:
        print("結論: 取り込み元に壊れた値があります(上の [壊れています] を参照)。")
        print("      その値は取り込み直しでは直りません。")
    else:
        print("結論: 取り込み元は無事です。取り込み直せば画面も直ります。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
