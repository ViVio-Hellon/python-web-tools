"""倉庫連携の発注CSV(集計・発注データ)の試験

`packaging_tool/order_export.py`・`app/routes/warehouse.export_csv`・設定の「発注CSVの出力先」を見る。

【ここで守りたいこと】
1. 期間は登録日時の日付で、両端を含む。書き方の違う登録日時(`/` と `-`)も同じに読む
2. 集計はサイズ(品名の末尾)× 発注コード × 単位。同じ組み合わせは足す。丸めない
3. 集計に取り消し・発注数が空の発注を入れない。発注データには入れる
4. 出力先は設定の値。ブラウザには渡さず、そこへ書いて場所を返す
"""
from __future__ import annotations

import csv
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import config, db, modes, order_export  # noqa: E402
from tests import _web  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def add(conn, when, hinmei, code, qty, *, unit="台", cancelled="", width=1385, length=2770.5,
        lot="L1"):
    conn.execute(
        "INSERT INTO 資材パレット注文管理 (登録日時, LotNo, 品名, 発注コード, 単位, 厚, 幅, 丈, 発注数,"
        " 取り消し済, 発注キー) VALUES (?, ?, ?, ?, ?, 3.0, ?, ?, ?, ?, lower(hex(randomblob(8))))",
        (when, lot, hinmei, code, unit, width, length, qty, cancelled))
    conn.commit()


def read(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


class SizeTests(unittest.TestCase):
    def test_品名の末尾のパレット寸法(self) -> None:
        self.assertEqual(order_export.pallet_size("タイト　SP-18　1550x3100"), (1550, 3100))
        self.assertEqual(order_export.pallet_size("タイト　　2502.5x3100"), (2502.5, 3100))
        self.assertIsNone(order_export.pallet_size("角材 丈:1001-1999"))
        self.assertIsNone(order_export.pallet_size("EX"))


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        add(self.conn, "2026-10-01 08:00:00", "タイト　　1400x3150", "059551", 3)
        add(self.conn, "2026/10/05 15:20:11", "タイト　　1400x3150", "059551", 10)   # 書き方違い
        add(self.conn, "2026-10-05 16:00:00", "タイト　SP-18　1550x3100", "052212", 4)
        add(self.conn, "2026-10-06 09:00:00", "5×10　　1550x3100", "050054", 2, cancelled="1")
        add(self.conn, "2026-10-07 09:00:00", "EX", "", None, unit="")
        add(self.conn, "2026-10-07 10:00:00", "EX", "", 0, unit="")            # 本番の EX は 0
        add(self.conn, "2026-10-08 09:00:00", "角材 丈:1001-1999", "K-01", 6, unit="本")
        add(self.conn, "2026-09-30 23:59:59", "タイト　　1400x3150", "059551", 99)   # 期間外
        add(self.conn, "2026-10-12 00:00:00", "タイト　　1400x3150", "059551", 99)   # 期間外

    def export(self, kind, start=date(2026, 10, 1), end=date(2026, 10, 11)):
        got = order_export.export(self.conn, kind, start, end, self.dir)
        self.assertTrue(got.ok, got.message)
        return got, read(got.path)

    def test_集計は同じサイズと発注コードを足す(self) -> None:
        got, rows = self.export(order_export.KIND_SUMMARY)
        self.assertEqual(rows[0], list(order_export.SUMMARY_COLUMNS))
        self.assertEqual(rows[1:], [
            ["1400x3150", "1400", "3150", "059551", "台", "13", "2", "タイト　　1400x3150"],
            ["1550x3100", "1550", "3100", "052212", "台", "4", "1", "タイト　SP-18　1550x3100"],
            ["角材 丈:1001-1999", "", "", "K-01", "本", "6", "1", "角材 丈:1001-1999"],
        ])
        self.assertIn("取り消した 1件は数えていません", got.message)
        self.assertIn("発注数が 0 か空(EX など)の 2件は数えていません", got.message)
        self.assertIn(str(got.path), got.message)

    def test_発注データはそのまま(self) -> None:
        got, rows = self.export(order_export.KIND_ROWS)
        self.assertEqual(rows[0], list(order_export.ROW_COLUMNS))
        self.assertEqual(len(rows) - 1, 7, "期間内の7件(取り消し・EX も入れる)")
        first = dict(zip(rows[0], rows[1]))
        self.assertEqual((first["品名"], first["発注数"], first["幅"], first["丈"]),
                         ("タイト　　1400x3150", "3", "1385", "2770.5"), "丸めない")
        cancelled = [dict(zip(rows[0], r)) for r in rows[1:] if r[rows[0].index("取り消し済")] == "1"]
        self.assertEqual(len(cancelled), 1)
        self.assertNotIn("管理番号", rows[0], "端末ごとに振り直す番号は出さない")

    def test_小数の発注数も丸めずに足す(self) -> None:
        self.conn.execute("DELETE FROM 資材パレット注文管理")
        for qty in (0.1, 0.2):
            add(self.conn, "2026-10-02 08:00:00", "一般　　1100x1300", "P1", qty)
        _, rows = self.export(order_export.KIND_SUMMARY)
        self.assertEqual(rows[1][5], "0.3")

    def test_開始日が終了日より後なら断る(self) -> None:
        got = order_export.export(self.conn, order_export.KIND_ROWS,
                                  date(2026, 10, 9), date(2026, 10, 1), self.dir)
        self.assertFalse(got.ok)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_書けない場所なら場所を言って断る(self) -> None:
        blocker = self.dir / "file"
        blocker.write_text("x")
        got = order_export.export(self.conn, order_export.KIND_ROWS,
                                  date(2026, 10, 1), date(2026, 10, 9), blocker / "sub")
        self.assertFalse(got.ok)
        self.assertIn(str(blocker / "sub"), got.message)

    def test_出力先の既定は書き出し先(self) -> None:
        with mock.patch("packaging_tool.user_settings.get", return_value=None):
            self.assertEqual(config.order_csv_dir(), config.export_dir())


@unittest.skipUnless(HAS_WEB, _SKIP)
class ExportWebTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import warehouse as routes
        from packaging_tool import access_control

        self.conn = _web.bind_db(self, routes, make_db())
        add(self.conn, "2026-10-05 15:20:11", "タイト　　1400x3150", "059551", 3)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch("packaging_tool.config.order_csv_dir", return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        refresh = mock.patch("packaging_tool.data_sync.refresh_orders")
        self.refresh = refresh.start()
        self.addCleanup(refresh.stop)
        self.clients = {
            "field": _web.make_client(modes.FIELD, port=8715,
                                      grant=access_control.grant_of("mode:field")),
            "material": _web.make_client(
                modes.MATERIAL, port=8725,
                grant=access_control.grant_of("mode:field", "mode:material")),
        }

    def post(self, mode, **body):
        return self.clients[mode].post("/api/warehouse/export", json=body,
                                       headers={"X-Tool-Token": _web.TOKEN})

    def test_どちらのモードからも出せて場所を返す(self) -> None:
        for mode in ("field", "material"):
            with self.subTest(mode=mode):
                res = self.post(mode, kind="summary", start="2026-10-01", end="2026-10-11")
                self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
                path = Path(res.get_json()["path"])
                self.assertEqual(path.parent, self.dir)
                self.assertTrue(path.name.startswith("発注集計_20261001-20261011_"))
        self.assertEqual(self.refresh.call_count, 2, "出す前に共有を取り込む")

    def test_日付が無ければ欄を指して断る(self) -> None:
        res = self.post("field", kind="rows", start="", end="2026-10-11")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["field"], "start")

    def test_画面に出力の行と出力先がある(self) -> None:
        html = self.clients["material"].get(
            "/warehouse", headers={"X-Tool-Token": _web.TOKEN}).get_data(as_text=True)
        self.assertIn('data-export="summary"', html)
        self.assertIn('data-export="rows"', html)
        self.assertIn(str(self.dir), html)


@unittest.skipUnless(HAS_WEB, _SKIP)
class SettingTests(unittest.TestCase):
    def test_出力先はパスワード無しで保存できる(self) -> None:
        from packaging_tool import user_settings
        from packaging_tool.presenters import settings as presenter

        saved = {}
        with mock.patch.object(user_settings, "save", side_effect=saved.__setitem__):
            got = presenter.save(order_csv_dir="  D:/出力/発注  ")
        self.assertTrue(got.ok, got.message)
        self.assertEqual(saved, {config.KEY_ORDER_CSV_DIR: "D:/出力/発注"})

    def test_配布する設定に入っている(self) -> None:
        from packaging_tool import distribution
        self.assertIn(config.KEY_ORDER_CSV_DIR, distribution.ITEM_KEYS)


if __name__ == "__main__":
    unittest.main()
