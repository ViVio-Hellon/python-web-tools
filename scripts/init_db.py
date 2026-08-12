#!/usr/bin/env python3
"""SQLiteデータベースを初期化する(schema.sqlを適用)。

使い方:
    python3 scripts/init_db.py             # ./data/packaging_tool.db を作成
    python3 scripts/init_db.py --seed       # 動作確認用のサンプルデータも投入する
    PACKAGING_TOOL_DB_PATH=... python3 scripts/init_db.py  # 別パスに作成
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import config, db  # noqa: E402


def seed_sample_data(conn) -> None:
    """手元で動作確認するためのサンプル在庫データ。"""
    now = db.now_db_string()
    samples = [
        (1470, 2700, "一般", "5×10", "A-01", 12, "", "台", ""),
        (1470, 2700, "一般", "5×10", "A-02", 3, "", "台", ""),
        (920, 640, "一般", "C8", "B-05", 20, "", "枚", "定番サイズ"),
        (500, 440, "一般", "P1", "B-06", 0, "要", "枚", "在庫0だが常時表示"),
        (1185, 1800, "一般", "4×8", "C-10", 8, "", "台", ""),
    ]
    for width, length, industry, symbol, pos, qty, list_mgmt, unit, note in samples:
        conn.execute(
            "INSERT INTO PalletMaster "
            "(幅, 丈, 巾適合min, 巾適合max, 丈適合min, 丈適合max, 業界, 記号, 位置, "
            " 在庫数, リスト管理, 桁数, 脚数, コード, 単位, 備考, 更新日時) "
            "VALUES (?, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?, 0, 0, '', ?, ?, ?)",
            (width, length, industry, symbol, pos, qty, list_mgmt, unit, note, now),
        )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true", help="サンプルデータを投入する")
    args = parser.parse_args()

    config.ensure_dirs()
    print(f"DB path: {config.DB_PATH}")

    with db.connect() as conn:
        db.apply_schema(conn)
        if args.seed:
            seed_sample_data(conn)
            print("サンプルデータを投入しました。")

    print("初期化が完了しました。")


if __name__ == "__main__":
    main()
