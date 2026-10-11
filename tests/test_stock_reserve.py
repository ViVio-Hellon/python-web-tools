"""倉庫連携で在庫を充てる(引当)と、払い出しで持ち出す(持出)の試験

`packaging_tool/stock_reserve.py`・`presenters/warehouse.stock_view`・
`presenters/inventory`(図の「持出」の印)・`app/routes/warehouse`(送信)を見る。

【ここで守りたいこと】
1. 使える在庫 = 在庫数 − ほかの発注の持ち出し待ち。**同じ在庫を2回充てない**
2. 少ない置き場から使い切る
3. 払い出すと古い引当から順に「持出」になり、印が減る
4. 取り消した発注の引当は数えない
5. 送る直前に在庫が減っていたら、送らずに発注数を戻させる
6. 発注と引当は片方だけ残らない
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from unittest import mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import (config, db, import_specs, modes, pallet_service,  # noqa: E402
                            stock_reserve, sync_writeback, warehouse_service as svc)
from packaging_tool.presenters import inventory as inv  # noqa: E402
from packaging_tool.presenters import warehouse as presenter  # noqa: E402
from tests import _web  # noqa: E402
from tests.test_web_inventory import isolate_map  # noqa: E402

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


def add_stock(conn, position: str, qty: int, *, width=1100, length=1300) -> None:
    conn.execute(
        "INSERT INTO PalletMaster (幅, 丈, 業界, 記号, 位置, 在庫数, リスト管理, コード, 単位,"
        " 更新日時) VALUES (?, ?, '一般', '', ?, ?, '', '', '台', ?)",
        (width, length, position, qty, db.now_db_string()))
    conn.commit()


def reserve(conn, order_key: str, takes: dict[str, int], *, lot="L1", when=None,
            width=1100, length=1300) -> None:
    with conn:
        stock_reserve.write_reserve(
            conn, order_key=order_key, lot_no=lot, width=width, length=length,
            takes=[stock_reserve.Take(p, c) for p, c in takes.items()],
            terminal="PC1", now=when)


class OfferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()
        add_stock(self.conn, "A1", 2)
        add_stock(self.conn, "S1", 1)
        add_stock(self.conn, "A3", 5)
        add_stock(self.conn, "B1", 4, width=1200)        # 別のサイズ

    def test_少ない置き場から並べる(self) -> None:
        found = stock_reserve.offer(self.conn, 1100, 1300)
        self.assertEqual([(p.position, p.free) for p in found.places],
                         [("S1", 1), ("A1", 2), ("A3", 5)])
        self.assertEqual(found.total, 8)

    def test_少ない置き場から使い切る(self) -> None:
        takes = stock_reserve.plan(stock_reserve.offer(self.conn, 1100, 1300), 4)
        self.assertEqual([(t.position, t.count) for t in takes], [("S1", 1), ("A1", 2), ("A3", 1)])

    def test_足りなければあるだけ(self) -> None:
        takes = stock_reserve.plan(stock_reserve.offer(self.conn, 1100, 1300), 20)
        self.assertEqual(sum(t.count for t in takes), 8)

    def test_持ち出し待ちの分は使えない(self) -> None:
        """**同じ在庫を2回充てない。**"""
        reserve(self.conn, "K1", {"S1": 1, "A1": 1})
        found = stock_reserve.offer(self.conn, 1100, 1300)
        self.assertEqual([(p.position, p.free, p.waiting) for p in found.places],
                         [("A1", 1, 1), ("A3", 5, 0)])
        self.assertEqual(found.total, 6)

    def test_置き場名の大文字小文字と空白は同じ置き場(self) -> None:
        reserve(self.conn, "K1", {" a1 ": 2})
        found = stock_reserve.offer(self.conn, 1100, 1300)
        self.assertNotIn("A1", [p.position for p in found.places])

    def test_取り消した発注の引当は数えない(self) -> None:
        reserve(self.conn, "K1", {"A3": 5})
        self.conn.execute(
            "INSERT INTO 資材パレット注文管理 (LotNo, 品名, 発注コード, 単位, 厚, 幅, 丈,"
            " 発注キー, 取り消し済) VALUES ('L1', 'x', 'c', '台', 1, 1, 1, 'K1', '1')")
        self.conn.commit()
        self.assertEqual(stock_reserve.offer(self.conn, 1100, 1300).total, 8)
        self.assertEqual(stock_reserve.waiting(self.conn), [])


class ConsumeTests(unittest.TestCase):
    """払い出すと、古い引当から順に持ち出したことになる。"""

    def setUp(self) -> None:
        self.conn = make_db()
        add_stock(self.conn, "A1", 5)

    def issue(self, qty: int, position: str = "A1"):
        return pallet_service.issue(self.conn, width=1100, length=1300,
                                    position=position, qty=qty, terminal="PC9")

    def test_払い出すと持ち出し待ちが減る(self) -> None:
        reserve(self.conn, "K1", {"A1": 2}, lot="LOT-A")
        result = self.issue(1)
        self.assertTrue(result.ok, result.message)
        self.assertIn("持ち出し待ちを済みにしました(Lot LOT-A 1台)", result.message)
        self.assertEqual(stock_reserve.waiting_by_position(self.conn), {"A1": 1})

    def test_古い引当から充てる(self) -> None:
        reserve(self.conn, "K2", {"A1": 1}, lot="NEW", when="2026-10-11 10:00:00")
        reserve(self.conn, "K1", {"A1": 1}, lot="OLD", when="2026-10-11 09:00:00")
        result = self.issue(1)
        self.assertIn("Lot OLD 1台", result.message)
        self.assertEqual([w.lot_no for w in stock_reserve.waiting(self.conn)], ["NEW"])

    def test_持ち出し待ちより多く払い出しても待っている分だけ(self) -> None:
        reserve(self.conn, "K1", {"A1": 1})
        self.issue(3)
        rows = self.conn.execute(
            "SELECT 区分, 台数, 端末 FROM パレット在庫引当 WHERE 区分 = '持出'").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("持出", 1, "PC9")])
        self.assertEqual(stock_reserve.waiting(self.conn), [])

    def test_持ち出し待ちが無ければふつうの払い出し(self) -> None:
        result = self.issue(1)
        self.assertEqual(result.message, "払い出しを登録しました(在庫数: 4)。")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM パレット在庫引当").fetchone()[0], 0)

    def test_ほかのサイズや置き場の引当には充てない(self) -> None:
        add_stock(self.conn, "A2", 5)
        reserve(self.conn, "K1", {"A2": 1})
        reserve(self.conn, "K2", {"A1": 1}, width=1200)
        self.issue(1)
        self.assertEqual(len(stock_reserve.waiting(self.conn)), 2)

    def test_持出が多すぎても負にしない(self) -> None:
        """2台が同じ在庫を同時に払い出した(取り込みで持出が2行届いた)。"""
        reserve(self.conn, "K1", {"A1": 1})
        for _ in range(2):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO パレット在庫引当 (引当ID, 発注キー, LotNo, 幅, 丈, 位置, 区分, 台数, 日時)"
                    " VALUES (lower(hex(randomblob(8))), 'K1', 'L1', 1100, 1300, 'A1', '持出', 1, '')")
        self.assertEqual(stock_reserve.waiting(self.conn), [])
        self.assertEqual(stock_reserve.offer(self.conn, 1100, 1300).total, 5)


class CreateOrdersTests(unittest.TestCase):
    ORDER = {"lot_no": "L1", "hinmei": "一般 1100x1300", "hatchu_code": "P1",
             "tani": "台", "atu": "3", "haba": "1100", "take": "1300", "hatchu_suu": "0"}

    def setUp(self) -> None:
        self.conn = make_db()

    def test_発注数0は在庫を使うときだけ通す(self) -> None:
        self.assertFalse(svc.create_orders(self.conn, [dict(self.ORDER)], terminal="T").ok)
        got = svc.create_orders(self.conn, [{**self.ORDER, "stock_used": 3}], terminal="T")
        self.assertTrue(got.ok, got.message)

    def test_引当が書けなければ発注も残さない(self) -> None:
        def broken(tx, rows, made):
            tx.execute("INSERT INTO まだ無い表 VALUES (1)")
        got = svc.create_orders(self.conn, [{**self.ORDER, "hatchu_suu": "2"}],
                                terminal="T", after=broken)
        self.assertFalse(got.ok)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM 資材パレット注文管理").fetchone()[0], 0)

    def test_引当は発注と同じ発注キーで残る(self) -> None:
        def after(tx, rows, made):
            stock_reserve.write_reserve(tx, order_key=rows[0]["発注キー"], lot_no="L1",
                                        width=1100, length=1300,
                                        takes=[stock_reserve.Take("A1", 2)], terminal="T")
        got = svc.create_orders(self.conn, [{**self.ORDER, "hatchu_suu": "2"}],
                                terminal="T", after=after)
        self.assertTrue(got.ok)
        key = self.conn.execute("SELECT 発注キー FROM 資材パレット注文管理").fetchone()[0]
        self.assertEqual([w.order_key for w in stock_reserve.waiting(self.conn)], [key])


class StockViewTests(unittest.TestCase):
    """倉庫連携の「在庫分を引きますか?」の言葉と数。"""

    DRAFT = {"lot_no": "L1", "hatchu_suu": "10", "pallet_width": 1100, "pallet_length": 1300}

    def setUp(self) -> None:
        self.conn = make_db()
        add_stock(self.conn, "A1", 2)
        add_stock(self.conn, "S1", 1)

    def test_案内と発注数の変わり方(self) -> None:
        view = presenter.stock_view(self.conn, self.DRAFT, "10")
        self.assertEqual(view["ask"], "※在庫があります: 1100×1300 が S1 に 1台・A1 に 2台"
                                      "(計 3台)。在庫分を発注数から引きますか?")
        self.assertEqual(view["change"], "発注数 10 → 7")
        self.assertEqual((view["use"], view["after"]), (3, 7))
        self.assertIn("在庫から 3台 使います(S1 1台・A1 2台)", view["used_text"])

    def test_在庫で全部まかなえるとき(self) -> None:
        view = presenter.stock_view(self.conn, self.DRAFT, "2")
        self.assertEqual((view["use"], view["after"]), (2, 0))
        self.assertIn("発注数 0 で知らせます", view["used_text"])

    def test_発注数が無ければ押す前に入れてもらう(self) -> None:
        view = presenter.stock_view(self.conn, self.DRAFT, "")
        self.assertEqual(view["use"], 0)
        self.assertIn("発注数を入れてから", view["why"])

    def test_出さないとき(self) -> None:
        self.assertIsNone(presenter.stock_view(self.conn, {**self.DRAFT, "is_ex_order": True}, "10"))
        self.assertIsNone(presenter.stock_view(self.conn, {"lot_no": "L1"}, "10"))
        self.assertIsNone(presenter.stock_view(
            self.conn, {**self.DRAFT, "pallet_width": 999}, "10"))


class InventoryMarkTests(unittest.TestCase):
    """簡易在庫の図と一覧の「持出」の印。"""

    def setUp(self) -> None:
        isolate_map(self)
        self.conn = make_db()
        add_stock(self.conn, "A1", 2)
        add_stock(self.conn, "S1", 1)
        reserve(self.conn, "K1", {"A1": 2}, lot="LOT-A", when="2026-10-11 14:02:00")

    def position(self, view, name):
        return next(p for p in view.positions if p.name == name)

    def test_図の棚に台数が付く(self) -> None:
        view = inv.build_map(self.conn)
        self.assertEqual(self.position(view, "A1").waiting, 2)
        self.assertFalse(self.position(view, "A1").short)
        self.assertEqual(self.position(view, "S1").waiting, 0)
        self.assertEqual(view.waiting_note, "持ち出し待ち: A1 2台")

    def test_在庫数より多いとずれの印(self) -> None:
        reserve(self.conn, "K2", {"S1": 3})
        self.assertTrue(self.position(inv.build_map(self.conn), "S1").short)

    def test_一覧の行と開いた棚に出る(self) -> None:
        view = inv.at_position(self.conn, "A1")
        self.assertEqual(view.rows[0][inv.WAITING_LABEL], 2)
        self.assertEqual(view.waiting_detail,
                         ["1100×1300 2台 ── Lot LOT-A(10/11 14:02 に引当)"])
        self.assertEqual(inv.at_position(self.conn, "S1").rows[0][inv.WAITING_LABEL], "")

    def test_一覧の列はVBAの並びのうしろに足す(self) -> None:
        self.assertEqual([label for label, _, _ in inv.LIST_COLUMNS][:7],
                         [label for label, _, _ in inv.STOCK_COLUMNS])
        self.assertEqual(inv.LIST_COLUMNS[-1][0], "持出待ち")


class SyncSpecTests(unittest.TestCase):
    """全端末に印を出すため、共有へ送り、取り込みで配る。"""

    def test_書き戻しと取り込みに入っている(self) -> None:
        spec = next(s for s in sync_writeback.WRITEBACK_SPECS
                    if s.sqlite_table == config.TBL_STOCK_RESERVE)
        self.assertEqual(spec.identity_columns, ("引当ID",))
        self.assertIn(config.TBL_STOCK_RESERVE, sync_writeback.SHARED_TABLE_DDL)
        self.assertIn(config.TBL_STOCK_RESERVE, import_specs.IMPORT_SPECS)
        self.assertIn(config.TBL_STOCK_RESERVE, import_specs.OPTIONAL_TABLES)

    def test_共有に作る表と手元の表の列が同じ(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(sync_writeback.SHARED_TABLE_DDL[config.TBL_STOCK_RESERVE])
        shared = [r[1] for r in conn.execute(f"PRAGMA table_info([{config.TBL_STOCK_RESERVE}])")]
        local = [r[1] for r in make_db().execute(
            f"PRAGMA table_info([{config.TBL_STOCK_RESERVE}])")]
        self.assertEqual([c for c in local if c != "作成元"], shared)
        imported = [src for src, _, _ in import_specs.IMPORT_SPECS[config.TBL_STOCK_RESERVE]]
        self.assertEqual(sorted(imported), sorted(c for c in shared if c != "管理番号"))


# ==================================================================
# 画面とAPI
# ==================================================================
DRAFT = {"lot_no": "4102781", "hinmei": "一般　P1　1100x1300", "hatchu_code": "PL-001",
         "tani": "台", "zaisitu": "A5052", "choshitu": "H32", "atu": "3.0",
         "haba": "1500", "take": "2500", "yoto_code": "K100", "nounyusaki": "A社",
         "hatchu_suu": "10", "pallet_width": 1100, "pallet_length": 1300}
FIELDS = {k: v for k, v in DRAFT.items() if not k.startswith("pallet_")}


@unittest.skipUnless(HAS_WEB, _SKIP)
class StockWebTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import warehouse as routes
        from packaging_tool import access_control, work_context

        self.conn = _web.bind_db(self, routes, make_db())
        self.field = _web.make_client(modes.FIELD, port=8714,
                                      grant=access_control.grant_of("mode:field"))
        self.material = _web.make_client(
            modes.MATERIAL, port=8724,
            grant=access_control.grant_of("mode:field", "mode:material"))
        add_stock(self.conn, "A1", 2)
        add_stock(self.conn, "S1", 1)
        context = work_context.get_context()
        context.lot_no = "4102781"
        context.set_pending_orders([dict(DRAFT)])
        self.addCleanup(context.take_pending_orders)
        for name in ("refresh_orders", "write_back_in_background"):
            patcher = mock.patch(f"packaging_tool.data_sync.{name}")
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def auth(self) -> dict:
        return {"X-Tool-Token": _web.TOKEN}

    def send(self, **kw):
        return self.field.post("/api/warehouse/send",
                               json={**FIELDS, "from_draft": True, **kw}, headers=self.auth())

    def drafts_on_page(self, client) -> list:
        """画面に埋めた下書き(`data-orders`)。"""
        import html as html_lib
        import json
        import re
        page = client.get("/warehouse", headers=self.auth()).get_data(as_text=True)
        found = re.search(r'data-orders="([^"]*)"', page)
        return json.loads(html_lib.unescape(found.group(1))) if found else []

    def test_画面に在庫の案内が出る(self) -> None:
        html = self.field.get("/warehouse", headers=self.auth()).get_data(as_text=True)
        self.assertIn('id="stockOffer"', html)
        stock = self.drafts_on_page(self.field)[0]["stock"]
        self.assertIn("在庫分を発注数から引きますか", stock["ask"])
        self.assertEqual(stock["change"], "発注数 10 → 7")

    def test_資材モードの画面には出さない(self) -> None:
        drafts = self.drafts_on_page(self.material)
        self.assertFalse(drafts and drafts[0].get("stock"))

    def test_押すとどこから何台かが返る(self) -> None:
        body = self.field.post("/api/warehouse/stock/plan", json={"qty": "10"},
                               headers=self.auth()).get_json()
        self.assertEqual((body["stock"]["use"], body["stock"]["after"]), (3, 7))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM パレット在庫引当").fetchone()[0], 0, "押しただけでは書かない")

    def test_在庫を使って送る(self) -> None:
        res = self.send(hatchu_suu="7", use_stock=3)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        body = res.get_json()
        self.assertIn("在庫から 3台 使います(S1 1台・A1 2台)", body["message"])
        self.refresh_orders.assert_called_once()
        order = self.conn.execute(
            "SELECT 発注数, 発注キー FROM 資材パレット注文管理").fetchone()
        self.assertEqual(order["発注数"], 7)
        waits = stock_reserve.waiting(self.conn)
        self.assertEqual({(w.position, w.count) for w in waits}, {("S1", 1), ("A1", 2)})
        self.assertEqual({w.order_key for w in waits}, {order["発注キー"]})
        comments = [r[0] for r in self.conn.execute("SELECT 本文 FROM 発注コメント")]
        self.assertEqual(comments, ["在庫から 3台 使います(1100×1300: S1 1台・A1 2台)。"
                                    "発注数 10 → 7"])

    def test_在庫で全部まかなえれば発注数0で送れる(self) -> None:
        self.assertEqual(self.send(hatchu_suu="0", use_stock=3).status_code, 200)

    def test_送る前に在庫が減っていたら送らない(self) -> None:
        reserve(self.conn, "OTHER", {"A1": 2})          # ほかの端末が先に使った
        res = self.send(hatchu_suu="7", use_stock=3)
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "stock_changed")
        self.assertEqual(body["before"], 10)
        self.assertIn("いま使える在庫は 1台", body["error"]["message"])
        self.assertEqual(body["stock"]["total"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM 資材パレット注文管理").fetchone()[0], 0)

    def test_下書きでなければ在庫は充てない(self) -> None:
        res = self.field.post("/api/warehouse/send", json={**FIELDS, "use_stock": 3},
                              headers=self.auth())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], "bad_stock")

    def test_在庫を使わなければ今までどおり(self) -> None:
        self.assertEqual(self.send().status_code, 200)
        self.refresh_orders.assert_not_called()
        self.assertEqual(stock_reserve.waiting(self.conn), [])

    def test_資材モードからは押せない(self) -> None:
        res = self.material.post("/api/warehouse/stock/plan", json={"qty": "10"},
                                 headers=self.auth())
        self.assertIn(res.status_code, (403, 404))


if __name__ == "__main__":
    unittest.main()
