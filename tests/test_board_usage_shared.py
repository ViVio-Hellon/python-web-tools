"""ボード人気度を全端末の合計で出す(使用実績を共有へ集める)。

共有の梱包資材マスタは Access から変換したもので、**ボード使用実績の表は
無い**。最初に送る端末が作る。2台(手元のDBは別々)で押して、どちらの
端末の人気度も合計になることを見る。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packaging_tool import board_usage, config, data_sync, sync_writeback
from packaging_tool.models import PlacedBoardModel
from packaging_tool.presenters import settings as settings_presenter
from tests.test_warehouse_handoff import make_local, make_shared

USAGE = config.TBL_BOARD_USAGE


def boards(*sizes: tuple[int, int]) -> list[PlacedBoardModel]:
    return [PlacedBoardModel(width=w, length=l, original_width=w, original_length=l)
            for w, l in sizes]


class SharedUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="usage_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        make_shared(self.src)
        conn = sqlite3.connect(self.src)
        conn.execute('CREATE TABLE "BoardMaster" (ボード幅, ボード丈, ボードタイプ, データラベル)')
        conn.executemany('INSERT INTO "BoardMaster" VALUES (?, ?, "ハードボード", "")',
                         [("1000", "1400"), ("100", "2000"), ("660", "1310")])
        conn.commit()
        conn.close()
        self.a, self.b = make_local(), make_local()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        for conn in (self.a, self.b):
            data_sync.import_master(conn, self.src)

    def use(self, conn, *sizes) -> None:
        board_usage.record_usage(conn, boards(*sizes), "ハードボード")

    def shared_tables(self) -> set[str]:
        conn = sqlite3.connect(self.src)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()

    def sheets(self, conn) -> dict[tuple[int, int], int]:
        got = board_usage.popularity(conn)
        return {(r.width, r.length): r.sheets for r in got.rows if r.sheets}

    def test_2台で押した数がどちらの端末でも合計になる(self):
        """以前は各端末の手元にしか無く、人気度はその端末で押した分だけだった。"""
        self.use(self.a, (1000, 1400), (100, 2000), (100, 2000))
        self.use(self.b, (100, 2000), (660, 1310))
        data_sync.write_back(self.a, self.src)
        data_sync.write_back(self.b, self.src)
        self.assertIn(USAGE, self.shared_tables())          # 最初に送った端末が作る
        for conn in (self.a, self.b):
            data_sync.import_master(conn, self.src)
            self.assertEqual(self.sheets(conn),
                             {(1000, 1400): 1, (100, 2000): 3, (660, 1310): 1})
            self.assertEqual(board_usage.popularity(conn).total_sheets, 5)

    def test_取り込み直しても二重に数えない(self):
        self.use(self.a, (100, 2000))
        for _ in range(3):
            data_sync.write_back(self.a, self.src)
            data_sync.import_master(self.a, self.src)
            data_sync.refresh_orders(self.a, self.src)
        self.assertEqual(self.sheets(self.a), {(100, 2000): 1})

    def test_開いたままの端末にも出る(self):
        self.use(self.a, (660, 1310))
        data_sync.write_back(self.a, self.src)
        got = data_sync.refresh_orders(self.b, self.src)
        self.assertEqual(self.sheets(self.b), {(660, 1310): 1})
        # 画面の「○件を取り込みました」は発注まわり(共有の発注2件)だけ。
        # 使用実績を数えると、発注が来たように見える
        self.assertEqual(got.imported, 2)

    def test_送れないあいだは手元の記録を消さない(self):
        """共有に表を作れない(読み取り専用など)あいだも、取り込みで消えない。"""
        self.use(self.a, (100, 2000))
        with mock.patch.object(sync_writeback, "ensure_shared_tables",
                               lambda *a, **k: []):
            result = data_sync.import_master(self.a, self.src)
        self.assertNotIn(USAGE, result.imported)
        self.assertEqual(self.sheets(self.a), {(100, 2000): 1})
        self.assertEqual(board_usage.unsent_sheets(self.a), 1)

    def test_送るものが無ければ共有を書き換えない(self):
        """共有は全員のファイル。用も無いのに表を作ると更新時刻が変わる。"""
        before = os.stat(self.src).st_mtime_ns
        data_sync.write_back(self.a, self.src)
        self.assertNotIn(USAGE, self.shared_tables())
        self.assertEqual(os.stat(self.src).st_mtime_ns, before)

    def test_画面は全端末の合計と送れていない分を言う(self):
        self.use(self.a, (100, 2000), (100, 2000))
        view = settings_presenter._board_usage(self.a)
        self.assertIn("全端末の累計 2枚", view["summary"])
        self.assertIn("まだ送れていない 2枚", view["summary"])
        data_sync.write_back(self.a, self.src)
        view = settings_presenter._board_usage(self.a)
        self.assertNotIn("送れていない", view["summary"])


class DistMemoTests(unittest.TestCase):
    def test_入れ替えはdataフォルダごと写すと書く(self):
        """以前の配布メモは配置図の2ファイルだけを写させていた。そのとおりに
        入れ替えると、使用実績・未送信の発注・設定が消えた。"""
        from scripts import make_dist
        memo = make_dist._memo([])
        self.assertIn("data フォルダを、フォルダごと新しいフォルダへ写す", memo)
        self.assertIn("packaging_tool.db", memo)
        self.assertIn("使用実績", memo)
        self.assertIn("別の端末の data は写さない", memo)


if __name__ == "__main__":
    unittest.main()
