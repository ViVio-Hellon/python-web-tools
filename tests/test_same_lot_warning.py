"""倉庫連携: 同じロットで発注していたら警告する(止めはしない)の試験

現場の声:「倉庫連携で資材発注した際に、ロットNoが重複していたら同じロットで発注していると
警告を出して下さい(追加注文もあるので、あくまで警告でいいです)」

【ここで守りたいこと】
1. 送る前に、同じロットの発注(取り消していないもの)を見せて訊く。「送信する(追加の注文)」で送れる(止めない)
2. **ほかの端末が出した発注も数える。** 確かめる前に共有から取り込み直す
   (前は手元だけを見ていたので、まだ取り込んでいない他の端末の発注は素通りした)
3. 送る前から分かるよう、LotNo が入ったら欄の下に一言出す(`/api/warehouse/same-lot`)
4. まとめて送る2行のどちらかのロットに送信済みがあれば訊く
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from packaging_tool import warehouse_service as svc
from tests.test_web_warehouse import ORDER, WarehouseWebTestCase

TABLE = "資材パレット注文管理"


def count(conn) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0]


class SameLotTestCase(WarehouseWebTestCase):
    def post(self, body: dict, expect: int = 200) -> dict:
        res = self.clients["field"].post("/api/warehouse/send", json=body, headers=self.auth())
        self.assertEqual(res.status_code, expect, res.get_json())
        return res.get_json()

    def other_terminal_sent(self, lot_no: str = ORDER["lot_no"]):
        """共有から取り込み直すと、ほかの端末(F2)が出した発注が手元に入る。"""
        def refresh(conn, *args, **kwargs):
            if not conn.execute(f'SELECT 1 FROM "{TABLE}" WHERE 送信端末 = ?', ("F2",)).fetchone():
                svc.create_order(conn, **{**ORDER, "lot_no": lot_no, "hatchu_suu": "2"},
                                 terminal="F2")
                conn.commit()
            got = mock.Mock(updated=[TABLE], looked=True, errors=[])
            got.message.return_value = ""
            return got
        from app.routes import warehouse as routes
        patcher = mock.patch.object(routes.data_sync, "refresh_orders", side_effect=refresh)
        patcher.start()
        self.addCleanup(patcher.stop)


class AskBeforeSendTests(SameLotTestCase):
    def test_ほかの端末の発注も数える(self) -> None:
        """手元にはまだ無い(取り込む前)。送る前に取り込み直して気づく。"""
        self.other_terminal_sent()
        self.assertEqual(count(self.conn), 0)
        body = self.post(ORDER, expect=409)
        self.assertEqual(body["error"]["code"], "already_sent")
        self.assertEqual(count(self.conn), 1)                 # F2 の分だけ。送っていない
        self.assertIn("発注数:2", body["ask"]["fields"][0]["value"])

    def test_警告なので追加の注文として送れる(self) -> None:
        self.post(ORDER)
        ask = self.post(ORDER, expect=409)["ask"]
        self.assertEqual(ask["title"], "同じロットで発注しています")
        self.assertIn("このロット(4102781)は、すでに 1件 発注しています", ask["lead"])
        self.assertIn("追加の注文", ask["why"])
        self.assertEqual(ask["ok"], "送信する(追加の注文)")
        self.post({**ORDER, "confirm_duplicate": True})
        self.assertEqual(count(self.conn), 2)

    def test_まとめて送る2行目のロットでも訊く(self) -> None:
        self.post({**ORDER, "lot_no": "4102782"})
        body = self.post({"rows": [ORDER, {**ORDER, "lot_no": "4102782"}]}, expect=409)
        self.assertIn("4102782", body["ask"]["lead"])
        self.assertEqual(count(self.conn), 1)

    def test_取り込み直しに失敗しても手元で確かめて送れる(self) -> None:
        from app.routes import warehouse as routes
        with mock.patch.object(routes.data_sync, "refresh_orders", side_effect=OSError("共有が無い")):
            self.post(ORDER)
            self.post(ORDER, expect=409)


class InlineWarnTests(SameLotTestCase):
    """送る前の一言(欄の下)。"""

    def get(self, lot_no: str) -> dict:
        res = self.clients["field"].get(f"/api/warehouse/same-lot?lot_no={lot_no}",
                                        headers=self.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def test_送信済みが無ければ何も言わない(self) -> None:
        self.assertEqual(self.get("4102781"), {"lot_no": "4102781", "count": 0, "message": ""})
        self.assertEqual(self.get("")["count"], 0)

    def test_ほかの端末の発注も数えて一言出す(self) -> None:
        self.other_terminal_sent()
        got = self.get("4102781")
        self.assertEqual(got["count"], 1)
        self.assertIn("このロット(4102781)は、すでに 1件 発注しています", got["message"])
        self.assertIn("追加の注文なら、そのまま送れます", got["message"])

    def test_取り消した発注は数えない(self) -> None:
        mgr = self.post(ORDER)["mgr_no"]
        self.assertEqual(self.get("4102781")["count"], 1)
        svc.cancel_order(self.conn, mgr, terminal=svc.this_terminal())
        self.assertEqual(self.get("4102781")["count"], 0)

    def test_資材モードには無い(self) -> None:
        res = self.clients["material"].get("/api/warehouse/same-lot?lot_no=4102781",
                                           headers=self.auth())
        self.assertIn(res.status_code, (403, 404))

    def test_画面に欄と呼び出しがある(self) -> None:
        html = self.clients["field"].get("/warehouse", headers=self.auth()).get_data(as_text=True)
        self.assertIn('id="sameLotWarn"', html)
        js = (Path(__file__).resolve().parent.parent / "app/static/js/views/warehouse.js").read_text(
            encoding="utf-8")
        self.assertIn("/api/warehouse/same-lot", js)
