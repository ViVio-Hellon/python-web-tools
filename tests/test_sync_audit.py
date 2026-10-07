"""全体監査(「同じ種類の不具合が他にもあるのでは」)で見つけて直したもの。

1. 先頭の1行を取る/当たった行すべてを書き換える
   - 確認・取消の印を共有へ書くとき、行番号も送信IDも当たらないと**中身**
     (登録日時・LotNo・品名)で探し、当たった行**すべて**に印を書いていた
     → 一意の 発注キー を先に見る。中身で2行以上当たったら書かない
   - 取り込み直したあとの同じ発注を探すとき、中身の先頭の行を取っていた → 発注キーを先に
2. 送れていないのに「無い」と答える
   - 送れていない行を数えるときに DB のエラー(掴まれている等)が出ると「無い」と
     答えていた → 総入れ替えの取り込みが走って未送信を消しうる。分からなければ「ある」
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import outbox_sync, source_db, sync_writeback, warehouse_service  # noqa: E402

ORDER_SPEC = next(s for s in sync_writeback.WRITEBACK_SPECS
                  if s.sqlite_table == "資材パレット注文管理")
COLS = ["管理番号", "登録日時", "LotNo", "品名", "発注キー", "確認済み", "取り消し済", "送信ID"]


def shared_db(rows: list[tuple]) -> Path:
    path = Path(tempfile.mkdtemp(prefix="audit_")) / "梱包資材マスタ.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE "資材パレット注文管理" (' + ", ".join(f'"{c}"' for c in COLS) + ")")
    conn.executemany('INSERT INTO "資材パレット注文管理" VALUES (?,?,?,?,?,?,?,?)', rows)
    conn.commit()
    conn.close()
    return path


def local_row(**values) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ["管理番号", "登録日時", "LotNo", "品名", "発注キー", "取込元管理番号"]
    conn.execute("CREATE TABLE t (" + ", ".join(cols) + ")")
    conn.execute("INSERT INTO t VALUES (?,?,?,?,?,?)", [values.get(c) for c in cols])
    return conn.execute("SELECT * FROM t").fetchone()


class MarkTargetTests(unittest.TestCase):
    def marks(self, path: Path) -> list[tuple]:
        conn = sqlite3.connect(path)
        out = conn.execute('SELECT 管理番号, 確認済み FROM "資材パレット注文管理" ORDER BY 1').fetchall()
        conn.close()
        return out

    def put(self, path: Path, row: sqlite3.Row):
        with source_db.connect(path) as src:
            with src.transaction() as tx:
                return outbox_sync._put_marks(tx, ORDER_SPEC, None, row, {"確認済み": "1"})

    def test_同じ中身が2行なら印を書かない(self) -> None:
        same = ("2026-10-07 09:00:00", "H8330P0", "タイト")
        path = shared_db([(1, *same, "", None, None, "a"), (2, *same, "", None, None, "b")])
        why = self.put(path, local_row(管理番号=9, 登録日時=same[0], LotNo=same[1], 品名=same[2]))
        self.assertIn("同じ中身の行が2行", why)
        self.assertEqual(self.marks(path), [(1, None), (2, None)])

    def test_発注キーがあればその行だけに書く(self) -> None:
        same = ("2026-10-07 09:00:00", "H8330P0", "タイト")
        path = shared_db([(1, *same, "K1", None, None, "a"), (2, *same, "K2", None, None, "b")])
        why = self.put(path, local_row(管理番号=9, 登録日時=same[0], LotNo=same[1], 品名=same[2], 発注キー="K2"))
        self.assertIsNone(why)
        self.assertEqual(self.marks(path), [(1, None), (2, "1")])

    def test_取り込み直したあとも発注キーで同じ発注を探す(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE 資材パレット注文管理 (管理番号 INTEGER PRIMARY KEY, 取込元管理番号,"
                     " 発注キー, 登録日時, LotNo, 品名)")
        same = ("2026-10-07 09:00:00", "H8330P0", "タイト")
        conn.execute("INSERT INTO 資材パレット注文管理 VALUES (10, NULL, 'K1', ?, ?, ?)", same)
        conn.execute("INSERT INTO 資材パレット注文管理 VALUES (11, NULL, 'K2', ?, ?, ?)", same)
        before = {"取込元管理番号": None, "発注キー": "K2", "登録日時": same[0], "LotNo": same[1], "品名": same[2]}
        # 番号が振り直された(11 → 21)
        conn.execute("UPDATE 資材パレット注文管理 SET 管理番号 = 21 WHERE 発注キー = 'K2'")
        self.assertEqual(warehouse_service.find_again(conn, 11, before), 21)


class UnsentCountTests(unittest.TestCase):
    def test_数えられなければ送れていない分があるとみなす(self) -> None:
        conn = sqlite3.connect(":memory:")
        with mock.patch.object(outbox_sync, "pending_rows",
                               side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(outbox_sync.unsent_tables(conn, [ORDER_SPEC]),
                             {ORDER_SPEC.sqlite_table: 1})
        with mock.patch.object(outbox_sync, "unpushed_mark_rows",
                               side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(outbox_sync.unpushed_mark_tables(conn, [ORDER_SPEC]),
                             {ORDER_SPEC.sqlite_table: 1})

    def test_手元に表が無いだけなら無いと答える(self) -> None:
        conn = sqlite3.connect(":memory:")
        with mock.patch.object(outbox_sync, "pending_rows",
                               side_effect=sqlite3.OperationalError("no such table: x")):
            self.assertEqual(outbox_sync.unsent_tables(conn, [ORDER_SPEC]), {})

    def test_実績も数えられなければ取り込みを見送る(self) -> None:
        from packaging_tool import pattern_sync
        conn = mock.MagicMock()
        conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        self.assertTrue(pattern_sync.pending(conn))


class QueuedNoteTests(unittest.TestCase):
    def test_共有が見えないときだけ言い添える(self) -> None:
        from packaging_tool import data_sync, sync_sources
        with mock.patch.object(sync_sources, "find_material_db", return_value=None):
            self.assertIn("相手にはまだ見えていません", data_sync.queued_note())
        with mock.patch.object(sync_sources, "find_material_db", return_value=Path("x")):
            self.assertEqual(data_sync.queued_note(), "")


if __name__ == "__main__":
    unittest.main()


class SamePlaceStockTests(unittest.TestCase):
    """同じ位置に同じ寸法の在庫が2行あれば、どちらも黙って動かさない。"""

    def test_受入も払出も断る(self) -> None:
        from packaging_tool import db, pallet_service
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        for sym in ("", "EX"):
            conn.execute("INSERT INTO PalletMaster (幅, 丈, 業界, 記号, 位置, 在庫数, 更新日時)"
                         " VALUES (1100, 2600, '一般', ?, 'B1', 3, '2026-01-01 00:00:00')", (sym,))
        conn.commit()
        got = pallet_service.issue(conn, width=1100, length=2600, position="B1", qty=1)
        self.assertFalse(got.ok)
        self.assertIn("2行あります", got.message)
        got = pallet_service.receive(conn, width=1100, length=2600, position="B1", qty=1,
                                     symbol="", industry="一般", unit="台", note="")
        self.assertFalse(got.ok)
        self.assertEqual([r[0] for r in conn.execute("SELECT 在庫数 FROM PalletMaster")], [3, 3])
