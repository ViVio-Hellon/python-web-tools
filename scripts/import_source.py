#!/usr/bin/env python3
"""取り込み元(sqlite3) → 手元DB の取り込み(コマンドライン版)

通常はアプリの設定画面から取り込めるので、このスクリプトは定期実行
(タスクスケジューラ)や、画面を開かずに流したいときに使う。

引数を省略すると**設定した置き場所**から自動で探して取り込む。

    python3 scripts/import_source.py                # マスタ+仕掛台帳をまとめて
    python3 scripts/import_source.py --master-only  # マスタだけ
    python3 scripts/import_source.py --lot-only     # 仕掛台帳だけ
    python3 scripts/import_source.py --check        # 置き場所の診断だけ

明示的にファイルを指定することもできる。

    python3 scripts/import_source.py 梱包資材マスタ.sqlite3
    python3 scripts/import_source.py --lot-dir /path/to/台帳
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import config, data_sync, db  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sqlite3_path", type=Path, nargs="?",
                        help="梱包資材マスタの.sqlite3(省略時は起動フォルダから探す)")
    parser.add_argument("--lot-dir", type=Path,
                        help="仕掛台帳3ファイルのあるフォルダ(省略時は設定のパス)")
    parser.add_argument("--master-only", action="store_true", help="マスタだけ取り込む")
    parser.add_argument("--lot-only", action="store_true", help="仕掛台帳だけ取り込む")
    parser.add_argument("--check", action="store_true",
                        help="取り込まずに、どのファイルが見つかるかだけ表示する")
    args = parser.parse_args()

    if args.master_only and args.lot_only:
        parser.error("--master-only と --lot-only は同時に指定できません")

    print(data_sync.describe_environment())
    if args.check:
        return

    config.ensure_dirs()
    print(f"\nDB path: {config.DB_PATH}\n")

    with db.connect() as conn:
        db.apply_schema(conn)
        if args.lot_only:
            result = data_sync.import_lot_ledger(conn, args.lot_dir)
        elif args.master_only:
            result = data_sync.import_master(conn, args.sqlite3_path)
        else:
            result = data_sync.import_master(conn, args.sqlite3_path)
            result.merge(data_sync.import_lot_ledger(conn, args.lot_dir))

    print(result.summary())
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
