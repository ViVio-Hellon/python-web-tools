#!/usr/bin/env python3
"""取り込み元にできてしまった**重複行**を数える / 消す

【なぜ要るのか】
VER2.2.1 より前は、取り込みのたびに書き戻し対象のテーブル
(資材パレット注文管理 / パレット入出庫履歴)が**倍になっていました**。
取り込みは総入れ替えで管理番号が振り直されるのに、同期記録には古い
行IDが残るため、取り込んだ行がどれも「未送信」に見え、次の取り込みの
前に走る書き戻しが取り込み元から来た行をもう一度送っていたためです。

原因は直しましたが、**すでに増えてしまった行は残ります。**
このスクリプトはそれを数え、頼まれれば消します。

    python3 scripts/dedupe_writeback.py                # 共有の梱包資材マスタを数えるだけ(既定)
    python3 scripts/dedupe_writeback.py --local        # この端末の手元の作業用DBを数える
    python3 scripts/dedupe_writeback.py --show         # 重なっている行の中身も出す(原因を追うとき)
    python3 scripts/dedupe_writeback.py --fix          # 消す(控えを取ってから)
    python3 scripts/dedupe_writeback.py --file  X.sqlite3
    python3 scripts/dedupe_writeback.py --table 資材パレット注文管理

【共有と手元のどちらを見るか】
設定画面の「同じ内容の行」は**この端末の手元の作業用DB**を数えた結果です。手元は取り込みの
たびに共有の中身で入れ替わるので、まず共有を数えます(既定)。共有に重なりがあれば `--fix`
で消してから、各端末で取り込み直すと手元も直ります。共有に無く手元にだけあるときは、
取り込み直せば消えます(手元を `--fix` で消す必要はありません)。

【何を重複とみなすか】
**管理番号と送信IDを除いた全部の列が一致する行**です。どちらも
「送るときに振られる番号」で、中身ではありません。一致した組の中で
**いちばん小さい管理番号だけを残し**、残りを消します(先に入った行が
本物で、あとから増えたのが写しだからです)。

【消す前に必ず控えを取ります】
`--fix` を付けたときだけ書き換えます。書き換える前に、同じフォルダへ
`<名前>.bak-YYYYMMDDHHMMSS.sqlite3` として丸ごと写しを取ります。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import config, data_sync, dedupe  # noqa: E402

# 中身の比較から外す列(`packaging_tool.dedupe` と同じ)
IGNORED = dedupe.IGNORED


def duplicates(conn: sqlite3.Connection, table: str) -> tuple[list[int], int]:
    """消してよい行のID一覧と、全体の行数を返す。"""
    counted = dedupe.count_conn(conn, table)
    return counted.drop, counted.total


def show(conn: sqlite3.Connection, table: str, limit: int = 5) -> None:
    """重なっている組を、比べなかった列(管理番号・送信ID)も含めて出す。

    **送信IDが違う組**は、同じ1件が別の送信として2回届いたもの(書き戻しの再送)。
    送信IDが空の組は、送信IDを書けなかった時期・別の道(表を持ってくる など)で入ったもの。
    """
    groups = dedupe.count_conn(conn, table).groups
    for number, group in enumerate(groups, start=1):
        if number > limit:
            print(f"    …ほかにも組があります(先頭 {limit}組だけ出しました)")
            break
        print(f"    ・{len(group.rows)}行: {group.describe()}")
        for ids in group.ids():
            print(f"        {ids}")


def backup(path: Path) -> Path:
    return dedupe.backup(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path,
                        help="梱包資材マスタの.sqlite3(省略時は設定の置き場所から探す)")
    parser.add_argument("--table", action="append",
                        help="見るテーブル(省略時は書き戻し対象すべて)")
    parser.add_argument("--fix", action="store_true",
                        help="重複を消す(付けなければ数えるだけ)")
    parser.add_argument("--local", action="store_true",
                        help="この端末の手元の作業用DBを見る(設定画面の警告と同じもの)")
    parser.add_argument("--show", action="store_true",
                        help="重なっている行の中身(管理番号・送信IDも)を出す")
    args = parser.parse_args()

    if args.local:
        path = config.DB_PATH
        if args.fix:
            print("手元の作業用DBは取り込みのたびに共有の中身で入れ替わります。消さずに、"
                  "共有を数えて(--local を外す)、設定画面から取り込み直してください。",
                  file=sys.stderr)
            return 1
    else:
        path = args.file or data_sync.find_material_db()
    if path is None or not Path(path).exists():
        print(f"梱包資材マスタが見つかりません({config.master_db_dir()})",
              file=sys.stderr)
        return 1
    path = Path(path)
    tables = args.table or [s.sqlite_table for s in data_sync.WRITEBACK_SPECS]

    print(f"対象: {path}")
    plan: dict[str, list[int]] = {}
    conn = sqlite3.connect(path)
    try:
        for table in tables:
            try:
                drop, total = duplicates(conn, table)
            except sqlite3.Error as exc:
                print(f"  {table}: 見られません({exc})")
                continue
            plan[table] = drop
            note = f"重複 {len(drop)}件" if drop else "重複なし"
            print(f"  {table}: {total}件 → {note}")
            if args.show and drop:
                show(conn, table)
    finally:
        conn.close()

    if not any(plan.values()):
        print("消すものはありません。")
        return 0
    if not args.fix:
        print("\n数えただけです。消すには --fix を付けてください"
              "(消す前に控えを取ります)。")
        return 0

    fixed = dedupe.fix_shared(path, list(plan))
    if not fixed.ok:
        print(fixed.summary(), file=sys.stderr)
        return 1
    print("\n" + fixed.summary())
    print("終わりました。設定画面から「まとめて取り込み」を押してください"
          "(設定画面の「同じ内容の行」からも、片付けと取り込み直しを1回でできます)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
