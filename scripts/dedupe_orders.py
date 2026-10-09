#!/usr/bin/env python3
"""資材パレット注文管理の**同じ注文の写し**を消す(書き方違いも同じとみなす)

`dedupe_writeback.py` は列が1文字でも違うと別の行と見る。Access から来た行
(`2026/08/11`・厚 `140.000`・空欄 NULL)と、ツールが送った行(`2026-08-11`・`140.0`・`''`)は
同じ注文でも別に見えて残る。ここでは「表を持ってくる」と同じ見分け方
(登録日時・LotNo・発注コード・厚・幅・丈 + 発注数。書き方の違いはそろえて比べる)で同じ注文を探し、
**番号がいちばん小さい行を残して**残りを消す(発注コメントの `#番号` は小さいほうを指している)。

確認・取り消しの印が写しで違う組は**消さない**(どれが正しいか機械では決めない)。

    python dedupe_orders.py          # 数えるだけ
    python dedupe_orders.py --fix    # 消す(先に丸ごと控えを取る)
    python dedupe_orders.py --file X.sqlite3
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import config, data_sync, source_db, table_bring  # noqa: E402

TABLE = config.TBL_WAREHOUSE_ORDER
KEYS = table_bring.APPEND_KEYS[TABLE] + ("発注数",)


def backup(path: Path) -> Path:
    """丸ごとの控え(SQLite の backup で、書きかけの姿を写さない)。

    `packaging_tool.dedupe` を使わないのは、VER4.7.8 より前の版にもこのファイルだけ置いて
    使えるようにするため。
    """
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    copy = path.with_name(f"{path.stem}.bak-{stamp}{path.suffix}")
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return copy


def _mark(row: dict) -> tuple[str, str]:
    return (str(row.get("確認済み") or ""), str(row.get("取り消し済") or ""))


def plan(rows: list[dict]) -> tuple[list[int], list[list[int]]]:
    """(消す管理番号, 印が違うので残した組)。"""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: int(r["管理番号"])):
        groups[table_bring._row_key(row, KEYS)].append(row)
    drop: list[int] = []
    kept: list[list[int]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        if len({_mark(r) for r in members}) > 1:
            kept.append([int(r["管理番号"]) for r in members])
            continue
        drop += [int(r["管理番号"]) for r in members[1:]]
    return sorted(drop), kept


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, help="梱包資材マスタ(省略時は設定の置き場所)")
    parser.add_argument("--fix", action="store_true", help="消す(付けなければ数えるだけ)")
    args = parser.parse_args()
    path = args.file or data_sync.find_material_db()
    if path is None or not Path(path).exists():
        print(f"梱包資材マスタが見つかりません({config.master_db_dir()})", file=sys.stderr)
        return 1
    path = Path(path)
    q = source_db.quote_identifier(TABLE)
    rows = source_db.read_query(path, f"SELECT * FROM {q}")
    drop, kept = plan(rows)
    print(f"対象: {path}")
    print(f"  {TABLE}: {len(rows)}行 → 同じ注文の写し {len(drop)}件")
    for group in kept:
        print(f"  ※ 確認・取り消しの印が違うので残します: 管理番号 {group}")
    if not drop:
        print("消すものはありません。")
        return 0
    if not args.fix:
        print("\n数えただけです。消すには --fix を付けてください(消す前に控えを取ります)。")
        return 0
    copy = backup(path)
    with source_db.connect(path) as conn, conn.transaction() as tx:
        # 書き込みの鍵を取ってから数え直す(数えたあとに届いた行と取り違えない)
        drop, _kept = plan(tx.query(f"SELECT * FROM {q}"))
        for number in drop:
            tx.execute(f"DELETE FROM {q} WHERE 管理番号 = ?", (number,))
    print(f"\n控え: {copy}")
    print(f"  {TABLE}: {len(drop)}件 消しました")
    print("終わりました。各端末は次の取り込み(起動時・設定の「まとめて取り込み」)で直ります。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
