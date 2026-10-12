"""EXのとき、台と枚を同時に発注する試験

現場の声:「EXモード・EXオンリーのとき、パレット一覧に台と枚の両方を出す(VER4.8.27)。
台と枚を同時に発注できるようにして下さい(1P0113の様に2つ同時に倉庫へ送る)」

【ここで守りたいこと】
1. EXで台を決めたあとに枚の行を決めると、台はそのままで「一緒に発注する枚」になる
2. 倉庫連携へ2行(1行目 台・2行目 枚)まとめて渡す。枚の発注数は決めつけずに空
3. 2行は1回で登録する(片方だけ届かない)。オーダーの選び方は1行のときと同じ
4. 同じ枚をもう一度決めると外れる。先に枚・あとで台でも組になる。EXでなければ組にしない
"""
from __future__ import annotations

import html as html_lib
import json
import re
import unittest

from packaging_tool import work_context
from packaging_tool.presenters import outputs
from tests.test_web_selection import SelectionWebTestCase, insert_pallet

LOT = "1234567"


class ExSheetPairTests(SelectionWebTestCase):
    ex = "EX"

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from app.routes import warehouse as wh_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        for module in (lot_routes, wh_routes):
            original = module.get_db
            module.get_db = lambda: self.conn
            self.addCleanup(lambda m=module, o=original: setattr(m, "get_db", o))
        insert_pallet(self.conn, width=1100, length=2000, code="D1", unit="台", industry="タイト")
        insert_pallet(self.conn, width=1100, length=2000, code="M1", unit="枚", industry="タイト",
                      symbol="EXﾀｲﾄ")
        insert_pallet(self.conn, width=1200, length=2400, code="M2", unit="枚", industry="タイト")
        insert_lot(self.conn)
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100", 納入先名称="A社", EX_輸出区分=self.ex)
        self.client.get(f"/api/lot/{LOT}", headers=self.auth())

    def row_of(self, code: str) -> dict:
        r = self.conn.execute("SELECT 管理番号, 幅, 丈, 記号 FROM PalletMaster WHERE コード = ?",
                              (code,)).fetchone()
        return {"id": r[0], "width": int(r[1]), "length": int(r[2]), "symbol": r[3] or ""}

    def decide(self, code: str) -> dict:
        return self.post("/api/selection/pallet/pick", self.row_of(code))

    def drafts(self) -> list:
        return work_context.get_context().peek_pending_orders()

    # --- 選ぶ ---------------------------------------------------------------
    def test_台のあとに枚を決めると台はそのままで組になる(self) -> None:
        self.decide("D1")
        state = self.decide("M1")
        session = self.session()
        self.assertEqual((session.pallet_row.code, session.ex_sheet_row.code), ("D1", "M1"))
        self.assertIn("枚", state["message"])
        self.assertTrue(state["ex_pair_set"])
        self.assertIn("コード M1", state["ex_pair_note"])
        marks = {(r["picked"], r["paired"]) for r in state["rows"] if r["id"] == self.row_of("M1")["id"]}
        self.assertEqual(marks, {(False, True)})

    def test_台だけなら枚の選び方を案内する(self) -> None:
        state = self.decide("D1")
        self.assertFalse(state["ex_pair_set"])
        self.assertIn("単位が枚の行を選んで", state["ex_pair_note"])

    def test_同じ枚をもう一度決めると外れる(self) -> None:
        self.decide("D1")
        self.decide("M1")
        self.decide("M1")
        self.assertIsNone(self.session().ex_sheet_row)
        self.assertEqual(len(self.drafts()), 1)

    def test_枚を選び直すと入れ替わる(self) -> None:
        self.decide("D1")
        self.decide("M1")
        self.decide("M2")
        self.assertEqual(self.session().ex_sheet_row.code, "M2")
        self.assertEqual(self.session().pallet_row.code, "D1")
        self.assertEqual((self.session().palette.width, self.session().palette.length), (1100, 2000),
                         "台(ボードを載せる寸法)は変えない")

    def test_先に枚あとで台でも組になる(self) -> None:
        self.decide("M1")
        self.assertIsNone(self.session().ex_sheet_row)
        self.decide("D1")
        self.assertEqual((self.session().pallet_row.code, self.session().ex_sheet_row.code), ("D1", "M1"))

    # --- 倉庫連携へ -----------------------------------------------------------
    def test_倉庫連携へ2行まとめて渡す(self) -> None:
        self.decide("D1")
        self.decide("M1")
        self.post("/api/selection/send")
        rows = self.drafts()
        self.assertEqual([(r["hatchu_code"], r["tani"]) for r in rows], [("D1", "台"), ("M1", "枚")])
        self.assertEqual(rows[1]["hinmei"], "タイト　EXﾀｲﾄ　1100x2000")
        self.assertEqual(rows[1]["hatchu_suu"], "", "枚の数は決めつけない")
        self.assertEqual(rows[0]["lot_no"], rows[1]["lot_no"])
        self.assertIn(outputs.EX_SHEET_NOTICE, rows[0]["notice"])

    def test_2行を1回で登録する(self) -> None:
        self.decide("D1")
        self.decide("M1")
        self.post("/api/selection/send")
        page = self.client.get("/warehouse", headers=self.auth()).get_data(as_text=True)
        drafts = json.loads(html_lib.unescape(re.search(r'data-orders="([^"]*)"', page).group(1)))
        self.assertEqual(len(drafts), 2)
        rows = [{k: v for k, v in d.items() if not k.startswith("pallet_") and k != "orders"}
                for d in drafts]
        rows[1]["hatchu_suu"] = ""
        res = self.client.post("/api/warehouse/send", json={**rows[0], "rows": rows, "from_draft": True},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400, "枚の発注数が空なら1行も登録しない")
        self.assertEqual(res.get_json()["error"]["row"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM 資材パレット注文管理").fetchone()[0], 0)
        rows[1]["hatchu_suu"] = "6"
        res = self.client.post("/api/warehouse/send", json={**rows[0], "rows": rows, "from_draft": True},
                               headers=self.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        got = self.conn.execute(
            "SELECT 発注コード, 単位, 発注数, 納入先, 受注番号 FROM 資材パレット注文管理 ORDER BY 管理番号").fetchall()
        self.assertEqual([tuple(r) for r in got], [("D1", "台", 1, "A社", "A100"), ("M1", "枚", 6, "A社", "A100")])

    def test_ロットを開き直すと組は外れる(self) -> None:
        self.decide("D1")
        self.decide("M1")
        self.session().clear_for_new_lot()
        self.assertIsNone(self.session().ex_sheet_row)

    def test_やり直すで組は外れる(self) -> None:
        self.decide("D1")
        self.decide("M1")
        self.post("/api/selection/clear")
        self.assertIsNone(self.session().ex_sheet_row)


class NotExTests(ExSheetPairTests):
    """EXでなければ、枚の行は一覧に出ず、組にもならない(今までどおり)。"""

    ex = ""

    for _name in [n for n in dir(ExSheetPairTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def test_EXでなければ組にしない(self) -> None:
        self.decide("D1")
        self.assertEqual(self.get()["ex_pair_note"], "")
        # 枚の行を決めると(番号で引けば単位は問わない。今までどおり)、選んでいる行が入れ替わるだけ
        self.decide("M1")
        self.assertIsNone(self.session().ex_sheet_row)
        self.assertEqual(self.session().pallet_row.code, "M1")
        self.post("/api/selection/send")
        self.assertEqual(len(self.drafts()), 1)


if __name__ == "__main__":
    unittest.main()
