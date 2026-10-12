"""EX・上下共用のとき、パレット(台)は「パレット決定」、蓋(枚)は「蓋選択」で選び、倉庫連携へ2行まとめて渡す試験

現場の声:「EXモードの時はパレット選択と蓋選択が押せるようにし、パレット選択で台を受け付ける、
蓋選択で枚を受け付ける。枚の選択は倉庫連携にしか影響を及ぼさない(ボード選定には関係ない)。
枚と台両方を表示する必要あり。ここまでこなして倉庫連携移行で2行発注となる」

【ここで守りたいこと】
1. EXのとき「パレット決定」は台の行だけ。枚の行で押すと寸法を変えずに断る
2. 「蓋選択」は枚の行だけ。パレットの寸法・製品サイズ・ボードは変えない(倉庫連携にだけ効く)
3. パレットと蓋があれば倉庫連携へ2行(1行目 台・2行目 枚)。枚の発注数は決めつけずに空。2行は1回で登録
4. 「蓋を外す」で1行に戻る。パレットを選び直しても蓋はそのまま。ロットを開き直す・やり直すで外れる
5. EXでなければ蓋選択は使えない(今までどおり)
"""
from __future__ import annotations

import html as html_lib
import json
import re
import unittest

from packaging_tool import lot_service, work_context
from packaging_tool.presenters import outputs
from tests.test_web_selection import SelectionWebTestCase, insert_pallet

LOT = "1234567"


class ExLidTests(SelectionWebTestCase):
    ex = "EX"
    # 記号。EX受注の一覧(EXオンリー)には記号に EX を含む行が出る
    sym1, sym2 = "EXﾀｲﾄ", "EX2方向"

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from app.routes import warehouse as wh_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        for module in (lot_routes, wh_routes):
            original = module.get_db
            module.get_db = lambda: self.conn
            self.addCleanup(lambda m=module, o=original: setattr(m, "get_db", o))
        insert_pallet(self.conn, width=1100, length=2000, code="D1", unit="台", industry="タイト",
                      symbol=self.sym1)
        insert_pallet(self.conn, width=1200, length=2400, code="D2", unit="台", industry="タイト",
                      symbol=self.sym1)
        insert_pallet(self.conn, width=1100, length=2000, code="M1", unit="枚", industry="タイト",
                      symbol=self.sym1)
        insert_pallet(self.conn, width=1200, length=2400, code="M2", unit="枚", industry="タイト",
                      symbol=self.sym2)
        insert_lot(self.conn)
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100", 納入先名称="A社", EX_輸出区分=self.ex)
        self.client.get(f"/api/lot/{LOT}", headers=self.auth())
        self.enter_mode()

    def enter_mode(self) -> None:
        """モードに入る。EXはロットを開いた時点で入っている。"""

    def row_of(self, code: str) -> dict:
        r = self.conn.execute("SELECT 管理番号, 幅, 丈, 記号 FROM PalletMaster WHERE コード = ?",
                              (code,)).fetchone()
        return {"id": r[0], "width": int(r[1]), "length": int(r[2]), "symbol": r[3] or ""}

    def decide(self, code: str, expect: int = 200) -> dict:
        return self.post("/api/selection/pallet/pick", self.row_of(code), expect=expect)

    def lid(self, code: str, expect: int = 200) -> dict:
        return self.post("/api/selection/lid/pick", self.row_of(code), expect=expect)

    def drafts(self) -> list:
        return work_context.get_context().peek_pending_orders()

    # --- 選ぶ ---------------------------------------------------------------
    def test_一覧には台と枚の両方が出て単位が分かる(self) -> None:
        state = self.get()
        self.assertEqual(sorted(r["unit"] for r in state["rows"]), ["台", "台", "枚", "枚"])
        self.assertTrue(state["lid_enabled"])
        self.assertIn("「蓋選択」", state["lid_note"])

    def test_パレット決定は台だけ_枚なら寸法を変えずに断る(self) -> None:
        self.decide("D1")
        state = self.decide("M2", expect=400)
        self.assertIn("蓋選択", state["error"]["message"])
        session = self.session()
        self.assertEqual(session.pallet_row.code, "D1")
        self.assertEqual((session.palette.width, session.palette.length), (1100, 2000))
        self.assertIsNone(session.lid_row)

    def test_蓋選択は枚だけ(self) -> None:
        state = self.lid("D2", expect=400)
        self.assertIn("パレット決定", state["error"]["message"])
        self.assertIsNone(self.session().lid_row)

    def test_蓋は倉庫連携にだけ効く(self) -> None:
        self.decide("D1")
        before = self.session().palette.width, self.session().palette.length, self.session().product
        state = self.lid("M2")                                   # 寸法の違う蓋でも
        session = self.session()
        self.assertEqual((session.palette.width, session.palette.length, session.product), before)
        self.assertEqual((session.pallet_row.code, session.lid_row.code), ("D1", "M2"))
        self.assertTrue(state["lid_set"])
        self.assertIn("コード M2", state["lid_note"])
        marks = {r["id"]: (r["picked"], r["paired"]) for r in state["rows"]}
        self.assertEqual(marks[self.row_of("D1")["id"]], (True, False))
        self.assertEqual(marks[self.row_of("M2")["id"]], (False, True))

    def test_先に蓋を選んでもよい_パレットを選び直しても蓋はそのまま(self) -> None:
        state = self.lid("M1")
        self.assertIn("パレット(台)を「パレット決定」で決めると", state["lid_note"])
        self.decide("D1")
        self.decide("D2")
        self.assertEqual((self.session().pallet_row.code, self.session().lid_row.code), ("D2", "M1"))

    def test_蓋を外す(self) -> None:
        self.decide("D1")
        self.lid("M1")
        self.assertEqual(len(self.drafts()), 2)
        state = self.post("/api/selection/lid/clear")
        self.assertIsNone(self.session().lid_row)
        self.assertFalse(state["lid_set"])
        self.assertEqual(len(self.drafts()), 1, "下書きもパレットの1行に戻る")

    # --- 倉庫連携へ -----------------------------------------------------------
    def test_倉庫連携へ2行まとめて渡す(self) -> None:
        self.decide("D1")
        self.lid("M1")
        self.post("/api/selection/send")
        rows = self.drafts()
        self.assertEqual([(r["hatchu_code"], r["tani"]) for r in rows], [("D1", "台"), ("M1", "枚")])
        self.assertEqual(rows[1]["hinmei"], lot_service.build_hinmei("タイト", self.sym1, 1100, 2000))
        self.assertEqual(rows[1]["hatchu_suu"], "", "蓋の数は決めつけない")
        self.assertEqual(rows[0]["lot_no"], rows[1]["lot_no"])
        self.assertIn(outputs.LID_NOTICE, rows[0]["notice"])

    def test_パレットを決めずに送ると蓋があってもEXの合図だけ(self) -> None:
        self.lid("M1")
        self.post("/api/selection/send")
        rows = self.drafts()
        self.assertEqual([(r["hinmei"], r.get("is_ex_order")) for r in rows], [("EX", True)])

    def test_2行を1回で登録する(self) -> None:
        self.decide("D1")
        self.lid("M1")
        self.post("/api/selection/send")
        page = self.client.get("/warehouse", headers=self.auth()).get_data(as_text=True)
        drafts = json.loads(html_lib.unescape(re.search(r'data-orders="([^"]*)"', page).group(1)))
        self.assertEqual(len(drafts), 2)
        rows = [{k: v for k, v in d.items() if not k.startswith("pallet_") and k != "orders"}
                for d in drafts]
        res = self.client.post("/api/warehouse/send", json={**rows[0], "rows": rows, "from_draft": True},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400, "蓋の発注数が空なら1行も登録しない")
        self.assertEqual(res.get_json()["error"]["row"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM 資材パレット注文管理").fetchone()[0], 0)
        rows[1]["hatchu_suu"] = "6"
        res = self.client.post("/api/warehouse/send", json={**rows[0], "rows": rows, "from_draft": True},
                               headers=self.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        got = self.conn.execute(
            "SELECT 発注コード, 単位, 発注数, 納入先, 受注番号 FROM 資材パレット注文管理 ORDER BY 管理番号").fetchall()
        self.assertEqual([tuple(r) for r in got], [("D1", "台", 1, "A社", "A100"), ("M1", "枚", 6, "A社", "A100")])

    def test_ロットを開き直すと蓋は外れる(self) -> None:
        self.decide("D1")
        self.lid("M1")
        self.session().clear_for_new_lot()
        self.assertIsNone(self.session().lid_row)

    def test_やり直すで蓋は外れる(self) -> None:
        self.decide("D1")
        self.lid("M1")
        self.post("/api/selection/clear")
        self.assertIsNone(self.session().lid_row)


class SharedLidTests(ExLidTests):
    """上下共用モード(保護材がアングル以外)でも同じ(現場の声:「上下共用モードの時も蓋が有ることが前提」)。"""

    ex = ""
    sym1, sym2 = "", ""

    def enter_mode(self) -> None:
        # 保護材がザラ板に決まった = 上下共用(ロットの保護材マスタを用意する代わりに、決まった結果だけを入れる)
        self.session().presenter.apply_hosozai("ザラ板")
        self.assertTrue(self.session().presenter.is_shared_board_mode)

    # 上下共用はEXではないので、パレットなしで送る形はEXと違う(VER4.8.35 で扱う)
    test_パレットを決めずに送ると蓋があってもEXの合図だけ = None

    def test_案内は上下共用と書く(self) -> None:
        self.assertTrue(self.get()["lid_note"].startswith("上下共用:"))

    def test_自動のパレット検索も台だけ(self) -> None:
        from packaging_tool import pallet_auto_select
        sheet = self.conn.execute("SELECT * FROM PalletMaster WHERE コード = 'M1'").fetchone()
        dai = self.conn.execute("SELECT * FROM PalletMaster WHERE コード = 'D1'").fetchone()
        kw = dict(ex_only_mode=False, show_all=False, last_hosozai="ザラ板")
        self.assertFalse(pallet_auto_select._listable(sheet, **kw))
        self.assertTrue(pallet_auto_select._listable(dai, **kw))


class NotExTests(ExLidTests):
    """EXでも上下共用でもなければ蓋選択は使えない。パレット決定は今までどおり(単位で断らない)。"""

    ex = ""
    sym1, sym2 = "", ""

    for _name in [n for n in dir(ExLidTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def test_EXでなければ蓋選択は使えない(self) -> None:
        state = self.get()
        self.assertFalse(state["lid_enabled"])
        self.assertEqual(state["lid_note"], "")
        self.lid("M1", expect=400)
        self.assertIsNone(self.session().lid_row)
        page = self.client.get("/selection", headers=self.auth()).get_data(as_text=True)
        self.assertRegex(page, r'id="pickLid"[^>]*hidden')

    def test_EXでなければパレット決定は単位で断らない(self) -> None:
        self.decide("M1")                       # 番号で引けば単位は問わない(今までどおり)
        self.assertEqual(self.session().pallet_row.code, "M1")


if __name__ == "__main__":
    unittest.main()
