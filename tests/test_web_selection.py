"""資材選択(Web版) — パレット・製品サイズ・ボード選定・アングル

`packaging_tool/selection_session.py` と `app/routes/selection.py` を見る。

【ここで守りたいこと】
- **入力欄の値**と**確定した値**を混ぜない。資材展開は入力欄に入れるだけで、
  「セット」を押すまで確定ではない(1P0113の資材引きが入力欄を見るため)
- 一覧の絞り込みは**引き直す**。行を抱えると、EXの絞り込みを触った瞬間に
  検索結果が消える(tkinter版で実際に起きていた)
- 400(入力の形の誤り)と 422(業務としての断り)を取り違えない
- **一覧に無いものは選べない**。tkinter版は一覧から選ばせることで
  保証していたが、Web版は値がそのまま送られてくる
- 押せるかどうかは**サーバが決める**。画面が条件を組み立て直すと、
  サーバが断る条件と画面が押させる条件が別々に育つ
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import (  # noqa: E402
    board_selection_service as svc,
    selection_session,
    work_context,
)
from packaging_tool.presenters import selection as presenter  # noqa: E402
from tests import _web  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"
TOKEN = _web.TOKEN          # 土台と同じものを使う


def insert_pallet(conn, *, width, length, symbol="", unit="台",
                  w_min=None, w_max=None, l_min=None, l_max=None,
                  industry="一般", code="P1", leg=4, keta=3) -> None:
    conn.execute(
        "INSERT INTO PalletMaster (幅, 丈, 巾適合min, 巾適合max, 丈適合min, 丈適合max,"
        " 業界, 記号, 脚数, 桁数, コード, 単位, 位置, 更新日時)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (width, length,
         w_min if w_min is not None else width - 100,
         w_max if w_max is not None else width,
         l_min if l_min is not None else length - 100,
         l_max if l_max is not None else length,
         industry, symbol, leg, keta, code, unit, "L1", "2026-01-01T00:00:00"))
    conn.commit()


@unittest.skipUnless(HAS_WEB, _SKIP)
class SelectionWebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import selection as routes

        # セッションも作業中のロットもプロセスに1つ。試験の間で漏らさない
        selection_session.reset_session()
        work_context.reset_context()
        self.addCleanup(selection_session.reset_session)
        self.addCleanup(work_context.reset_context)

        self.conn = _web.bind_db(self, routes)
        self.client = _web.make_client(port=8715, ready=False)

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}

    def get(self, path="/api/selection/state") -> dict:
        res = self.client.get(path, headers=self.auth())
        self.assertEqual(res.status_code, 200, path)
        return res.get_json()

    def post(self, path, body=None, expect=200) -> dict:
        res = self.client.post(path, json=body or {}, headers=self.auth())
        self.assertEqual(res.status_code, expect, f"{path}: {res.get_json()}")
        return res.get_json()

    def session(self):
        return selection_session.get_session(self.conn)


# ==================================================================
# 画面
# ==================================================================
class PageTests(SelectionWebTestCase):
    def test_開ける(self) -> None:
        self.assertEqual(self.client.get("/selection").status_code, 200)

    def test_未取り込みなら空の一覧の前に理由を出す(self) -> None:
        """0件と「まだ取り込んでいない」は別。"""
        state = self.get()
        self.assertFalse(state["available"])
        self.assertIn("設定画面", state["unavailable_message"])

        insert_pallet(self.conn, width=1100, length=1100)
        self.assertTrue(self.get()["available"])

    def test_まだできないことを書く(self) -> None:
        """作りかけを黙って出すと、壊れているのか未実装なのか分からない。"""
        html = self.client.get("/selection").get_data(as_text=True)
        for _what, phase in presenter.PENDING_PARTS:
            self.assertIn(phase, html)

    def test_ボード候補の隣に選定済みの一覧がある(self) -> None:
        """候補は幅・丈・在庫の3列だけで右に余白が残るので、そこへ
        「いま何を入れたか」を出す(現場の声:「選定したボードのリストは
        必要です。右に余白があるから出してください」)。候補を選ぶあいだ
        「選定一覧」タブへ切り替えずに確かめられるようにする。"""
        html = self.client.get("/selection").get_data(as_text=True)
        self.assertIn('id="boardPickedUpperRows"', html)
        self.assertIn('id="boardPickedLowerRows"', html)
        # 候補の一覧(#boardRows)より後ろに無いと「隣」にならない
        self.assertLess(html.index('id="boardRows"'), html.index('id="boardPickedUpperRows"'))


# ==================================================================
# 一覧
# ==================================================================
class ListTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1100, length=1100, unit="台")
        insert_pallet(self.conn, width=1200, length=2400, unit="台")
        insert_pallet(self.conn, width=1300, length=1300, unit="台", symbol="EX1")
        insert_pallet(self.conn, width=1400, length=1400, unit="組")

    def widths(self, state) -> list[int]:
        return [row["width"] for row in state["rows"]]

    def test_既定は単位フィルタとEX除外(self) -> None:
        """VBA の既定。単位=台 だけ、記号にEXを含む行は出さない。"""
        self.assertEqual(self.widths(self.get()), [1100, 1200])

    def test_EXまで表示で全部出る(self) -> None:
        state = self.post("/api/selection/toggle/show_all")
        self.assertEqual(self.widths(state), [1100, 1200, 1300, 1400])
        self.assertTrue(state["show_all"])

    def test_EX行だと分かる(self) -> None:
        """色だけで伝えない。記号欄の "EX" も残る(§3.8)。"""
        state = self.post("/api/selection/toggle/show_all")
        ex = [row for row in state["rows"] if row["is_ex"]]
        self.assertEqual([row["width"] for row in ex], [1300])
        self.assertIn("EX", ex[0]["symbol"])

    def test_EXオンリーはEX受注のときだけ押せる(self) -> None:
        """押せてしまうと、効いていないのに効いたつもりになる。"""
        state = self.get()
        self.assertFalse(state["ex_only_enabled"])
        self.assertIn("EX受注", state["ex_only_why"])

        self.session().presenter.is_ex_order = True
        state = self.get()
        self.assertTrue(state["ex_only_enabled"])
        self.assertEqual(state["ex_only_why"], "")

    def test_EXオンリーを効かないときに押したら言う(self) -> None:
        state = self.post("/api/selection/toggle/ex_only")
        self.assertIn("EX受注", state["message"])

    def test_EXの2つは同時に立たない(self) -> None:
        """絞り込みは `ex_only` を先に見るので、両方ONだと
        「EXまで表示」は**押せるのに何も起きない**。効いていない印を
        画面に残さない(VBA `chkShowAll_Click` / `chkExOnly_Click`)。"""
        state = self.post("/api/selection/toggle/show_all")
        self.assertTrue(state["show_all"])

        state = self.post("/api/selection/toggle/ex_only")
        self.assertTrue(state["ex_only"])
        self.assertFalse(state["show_all"])

        state = self.post("/api/selection/toggle/show_all")
        self.assertTrue(state["show_all"])
        self.assertFalse(state["ex_only"])

    def test_消すほうは相手に触らない(self) -> None:
        """OFFにするのは片方だけ。**両方OFF**にできなくなると、
        EXを含まない既定の一覧に戻れない。"""
        self.post("/api/selection/toggle/show_all")
        state = self.post("/api/selection/toggle/show_all")
        self.assertFalse(state["show_all"])
        self.assertFalse(state["ex_only"])

    def test_知らない切り替えは400(self) -> None:
        self.post("/api/selection/toggle/nonsense", expect=400)

    def test_行に単位とコードを添える(self) -> None:
        """一覧の列には出していない情報(VBA `DynamicTip`)。"""
        note = self.get()["rows"][0]["note"]
        self.assertIn("台", note)
        self.assertIn("P1", note)

    def test_何を出しているか書く(self) -> None:
        """同じ見た目の表が全件だったり検索結果だったりする。"""
        self.assertIn("2 件", self.get()["list_note"])
        self.assertIn("EXまで表示",
                      self.post("/api/selection/toggle/show_all")["list_note"])


# ==================================================================
# パレット
# ==================================================================
class PalletTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1100, length=1100)

    def test_適用するとリボンに出る(self) -> None:
        state = self.post("/api/selection/pallet/apply",
                          {"width": "1100", "length": "1100"})
        self.assertTrue(state["pallet_set"])
        self.assertIn("1100", state["pallet_status"])
        self.assertEqual(state["ribbon"]["pallet"], "1100×1100")

    def test_数値でなければ400(self) -> None:
        """打ち間違いは業務上の断りではなく入力の形の誤り。"""
        res = self.client.post("/api/selection/pallet/apply",
                               json={"width": "あ", "length": "1100"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)

    def test_パレットを変えたら製品サイズの確定は解ける(self) -> None:
        """収まることを確かめたのは**そのパレットに対して**。

        パレットが変わればその確認は成り立たない。確定したままにすると、
        収まらない組み合わせが「セット済」として通る。
        """
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "1100"})
        state = self.post("/api/selection/product/apply",
                          {"width": "1000", "length": "1000"})
        self.assertTrue(state["product_set"])

        state = self.post("/api/selection/pallet/apply",
                          {"width": "500", "length": "500"})
        self.assertFalse(state["product_set"])

    def test_クリアで両方戻る(self) -> None:
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "1100"})
        state = self.post("/api/selection/clear")
        self.assertFalse(state["pallet_set"])
        self.assertFalse(state["product_set"])
        self.assertEqual(state["ribbon"]["pallet"], work_context.UNSET)


class PalletListLiveTests(SelectionWebTestCase):
    """製品 幅・丈を打つたびの一覧絞り込み(`/api/selection/pallet/list`)。

    **何も確定しない。** 一覧を出すだけで、パレットも製品サイズも
    セットされない ── セットは行を選んで「パレット決定」を押した
    ときだけ。
    """

    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700, unit="台")

    def test_幅だけでも一覧が絞られる(self) -> None:
        state = self.post("/api/selection/pallet/list", {"product_width": "1150"})
        self.assertEqual(len(state["rows"]), 1)
        self.assertIn("幅", state["list_note"])

    def test_丈だけでも一覧が絞られる(self) -> None:
        state = self.post("/api/selection/pallet/list", {"product_length": "2650"})
        self.assertEqual(len(state["rows"]), 1)
        self.assertIn("丈", state["list_note"])

    def test_両方そろうと厳密な適合判定になる(self) -> None:
        state = self.post("/api/selection/pallet/list",
                          {"product_width": "1000", "product_length": "2500"})
        self.assertEqual(len(state["rows"]), 1)

    def test_何も確定しない(self) -> None:
        """一覧を出すだけで、パレット・製品サイズは未セットのまま。"""
        state = self.post("/api/selection/pallet/list",
                          {"product_width": "1150", "product_length": "2650"})
        self.assertFalse(state["pallet_set"])
        self.assertFalse(state["product_set"])
        self.assertNotIn("message", state)

    def test_板厚が分かっていれば5x10フィルタも効く(self) -> None:
        """入力中の一覧でも、自動選定と同じ5×10板厚フィルタが掛かる(バグ修正)。"""
        insert_pallet(self.conn, width=1300, length=2800,
                      w_min=1200, w_max=1400, l_min=2700, l_max=2900,
                      industry="5×10", symbol="通常", unit="台")
        insert_pallet(self.conn, width=1300, length=2800,
                      w_min=1200, w_max=1400, l_min=2700, l_max=2900,
                      industry="5×10", symbol="強度UP", unit="台")
        self.session().presenter.manufactured_thickness = 5.0
        state = self.post("/api/selection/pallet/list",
                          {"product_width": "1250", "product_length": "2750"})
        self.assertEqual([r["symbol"] for r in state["rows"]], ["強度UP"])


# ==================================================================
# 製品サイズ
# ==================================================================
class ProductTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1100, length=2000)

    def test_パレットが先(self) -> None:
        """押してから断るより、押せない理由が先に見えているほうがよい。"""
        state = self.get()
        self.assertFalse(state["can_set_product"])
        self.assertIn("パレット", state["set_product_why"])

    def test_パレットが無ければ422(self) -> None:
        """形は正しいが業務として通せない ── 入力の誤りではない。"""
        res = self.client.post("/api/selection/product/apply",
                               json={"width": "1000", "length": "1000"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 422)

    def test_数値でなければ400(self) -> None:
        res = self.client.post("/api/selection/product/apply",
                               json={"width": "-", "length": "1000"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)

    def test_収まらなければ422(self) -> None:
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})
        res = self.client.post("/api/selection/product/apply",
                               json={"width": "3000", "length": "3000"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 422)
        self.assertIn("収まりません", res.get_json()["error"]["message"])

    def test_回転すれば収まるなら入れ替える(self) -> None:
        """VBA `btnApplyProductSize_Click`。黙って入れ替えず、回転したと言う。

        「回転した」は**そのとき起きたこと**なので知らせ(message)に、
        入れ替わった寸法は**いまの状態**なので状態欄に出す。状態欄に
        「回転済」を残すと、開き直しただけで毎回そう見える。
        """
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})
        state = self.post("/api/selection/product/apply",
                          {"width": "1800", "length": "1000"})
        self.assertTrue(state["rotated"])
        self.assertIn("回転", state["message"])
        self.assertEqual((state["product_width"], state["product_length"]),
                         ("1000", "1800"))
        self.assertIn("1000 × 1800", state["product_status"])

    def test_確定するとリボンに出る(self) -> None:
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})
        state = self.post("/api/selection/product/apply",
                          {"width": "1000", "length": "1800"})
        self.assertEqual(state["ribbon"]["product"], "1000×1800")


# ==================================================================
# 検索
# ==================================================================
class SearchTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 1000×1800 が収まるパレットと、収まらないパレット
        insert_pallet(self.conn, width=1100, length=2000,
                      w_min=900, w_max=1100, l_min=1700, l_max=2000)
        insert_pallet(self.conn, width=1400, length=2400,
                      w_min=1200, w_max=1400, l_min=2000, l_max=2400)

    def test_製品サイズがあれば自動選定(self) -> None:
        state = self.post("/api/selection/pallet/search",
                          {"product_width": "1000", "product_length": "1800"})
        self.assertTrue(state["found"])
        self.assertTrue(state["pallet_set"])
        self.assertEqual(state["selected"], {"width": 1100, "length": 2000})

    def test_1回で製品まで確定する(self) -> None:
        """**同じ確認を2回やらせない。**

        「載るパレットを探して見つかった」のだから、載ることはもう
        確かめてある。以前はこのあと「セット」を押させていた。
        """
        from packaging_tool import work_context
        context = work_context.get_context()
        context.product_width, context.product_length = 1000, 1800

        state = self.post("/api/selection/pallet/search",
                          {"product_width": "1000", "product_length": "1800"})
        self.assertTrue(state["pallet_set"])
        self.assertTrue(state["product_set"], "製品サイズが確定していません")
        self.assertEqual(self.steps_of(state)["boards"]["state"],
                         presenter.STEP_CURRENT)

    def test_ロットが無ければ製品は確定しない(self) -> None:
        """打ち込んだだけの寸法を、勝手に作業状態へ書き込まない。"""
        state = self.post("/api/selection/pallet/search",
                          {"product_width": "1000", "product_length": "1800"})
        self.assertTrue(state["pallet_set"])

    def steps_of(self, state) -> dict:
        return {s["key"]: s for s in state["steps"]}

    def test_検索すると一覧が候補だけになる(self) -> None:
        """検索前の一覧が残ると「検索したのに何も変わらない」ように見える。"""
        self.assertEqual(len(self.get()["rows"]), 2)
        state = self.post("/api/selection/pallet/search",
                          {"product_width": "1000", "product_length": "1800"})
        self.assertEqual([row["width"] for row in state["rows"]], [1100])
        self.assertIn("収まる候補", state["list_note"])

    def test_絞り込みを触っても検索結果は消えない(self) -> None:
        """行を抱えず**引き直す**のはこのため。

        tkinter版はここが無条件に全件へ戻っていて、検索した直後に
        「EXまで表示」を触っただけで絞り込みが消えていた。
        """
        self.post("/api/selection/pallet/search",
                  {"product_width": "1000", "product_length": "1800"})
        state = self.post("/api/selection/toggle/show_all")
        self.assertIn("収まる候補", state["list_note"])
        self.assertEqual([row["width"] for row in state["rows"]], [1100])

    def test_載るパレットが無ければ422(self) -> None:
        res = self.client.post("/api/selection/pallet/search",
                               json={"product_width": "9000",
                                     "product_length": "9000"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 422)
        # 断られても画面ぜんぶが入っている。古い一覧が残らない
        self.assertIn("rows", res.get_json())

    def test_製品サイズが空なら直接検索(self) -> None:
        """同じボタンで別のことが起きるので、何をしたかを言う。"""
        state = self.post("/api/selection/pallet/search",
                          {"pallet_width": "1100", "pallet_length": "2000"})
        self.assertTrue(state["direct"])
        self.assertIn("直接検索", state["message"])
        self.assertEqual([row["width"] for row in state["rows"]], [1100])

    def test_何も入っていなければ断る(self) -> None:
        """**条件を出していないのに絞ったことにしない。**

        全件をそのまま出して「前後50mmで探しました」と言うと、出た一覧を
        正しい答えだと読んでしまう。何を打てばよいかも言う(VBAも同じ)。
        """
        body = self.post("/api/selection/pallet/search", {}, expect=422)
        self.assertIn("パレット幅またはパレット丈", body["message"])
        self.assertFalse(body["found"])
        # 直接検索モードにも入れない(入ると一覧の見出しが「—×—の前後
        # 50mm」になり、条件があるように読める)
        self.assertNotEqual(self.session().list_mode, "direct")

    def test_片方だけでも探せる(self) -> None:
        state = self.post("/api/selection/pallet/search",
                          {"pallet_width": "1100"})
        self.assertTrue(state["direct"])

    def test_直接検索も引き直す(self) -> None:
        """条件だけ覚えているので、絞り込みを触っても文脈が残る。"""
        self.post("/api/selection/pallet/search",
                  {"pallet_width": "1100", "pallet_length": "2000"})
        state = self.post("/api/selection/toggle/show_all")
        self.assertEqual([row["width"] for row in state["rows"]], [1100])


# ==================================================================
# 入力欄の値と確定値
# ==================================================================
class SessionTests(SelectionWebTestCase):
    def test_入力欄と確定値は別(self) -> None:
        """1P0113の資材引きは**入力欄の値**を見る(presenters/selection.py 冒頭)。

        ここを1つにまとめると、その区別が消える。
        """
        insert_pallet(self.conn, width=1100, length=2000)
        context = work_context.get_context()
        context.product_width, context.product_length = 1000, 1800

        state = self.get()
        self.assertEqual(state["product_width"], "1000")   # 入力欄には入っている
        self.assertFalse(state["product_set"])             # まだ確定ではない
        self.assertFalse(self.session().product.is_set)

    def test_接続は要求ごとに差し替わる(self) -> None:
        """`sqlite3` の接続はスレッドをまたげない。抱えたままにしない。"""
        session = self.session()
        other = sqlite3.connect(":memory:")
        self.addCleanup(other.close)
        session.bind(other)
        self.assertIs(session.presenter.conn, other)
        self.assertIs(selection_session.get_session(self.conn).presenter.conn,
                      self.conn)


@unittest.skipUnless(HAS_WEB, _SKIP)
class ModeFromLotTests(SelectionWebTestCase):
    """ロットを引いた時点でモードが決まる(VBA `SearchAndDisplay` 末尾)。

    資材選択の画面を開いてから引き直すのでは、**開くまでモードが
    分からない**。ロット検索の結果がそのまま状態機械に通る。
    """

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        self._lot_original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", self._lot_original))

        insert_pallet(self.conn, width=1100, length=1100)
        insert_lot(self.conn)
        insert_hiki(self.conn)
        self._insert_odr = insert_odr

    def search(self, **odr) -> dict:
        self._insert_odr(self.conn, **odr)
        self.client.get("/api/lot/1234567", headers=self.auth())
        return self.get()

    def test_EX受注ならEXオンリーが押せるようになる(self) -> None:
        self.assertFalse(self.get()["ex_only_enabled"])
        state = self.search(EX_輸出区分="EX")
        self.assertTrue(state["ex_only_enabled"])
        self.assertEqual(state["banner"]["kind"], "ex")

    def test_在庫マップはロットを引き直すと作り直す(self) -> None:
        """**1日開けっぱなしでも朝の在庫のまま点が付き続けない。**

        原文は資材選択のページに入るたびに捨てて、次に使うときに
        作り直していた(`mpMain_Change`)。こちらは画面を行き来しても
        同じセッションが残るので、ロットの引き直しを区切りにする。
        """
        self.post("/api/selection/toggle/stock_aware")
        self.assertEqual(self.session().stock_map, {})

        # かんばんの在庫が入れ替わった(取り込み直した、現物が動いた)
        self.conn.execute(
            "INSERT INTO 看板_大板小板 (資材, サイズ, 欲, 不)"
            " VALUES ('ハードボード', '500×1000', '〇', '')")
        self.conn.commit()
        # 引き直すまでは古いまま(毎回6表を走査しないための割り切り)
        self.assertEqual(self.session().stock_map, {})

        self.search()
        self.assertTrue(self.session().stock_map)
        # 在庫考慮そのものは外れない ── マップの有無がON/OFFなので、
        # 捨てるだけだと黙って効かなくなる
        self.assertTrue(self.get()["boards"]["stock_aware"])

    def test_OFFのままなら作り直さない(self) -> None:
        """押していない機能のために6表を走査しない。"""
        self.conn.execute(
            "INSERT INTO 看板_大板小板 (資材, サイズ, 欲, 不)"
            " VALUES ('ハードボード', '500×1000', '〇', '')")
        self.conn.commit()
        self.search()
        self.assertIsNone(self.session().stock_map)
        self.assertFalse(self.get()["boards"]["stock_aware"])

    def test_EX受注はパレット未設定でも倉庫送信できる(self) -> None:
        """**EXの実データは別の職場から届き、そちらが優先される。**

        こちらから送るのは「EXである」と分かる合図だけなので、
        パレットの適用も発注コードも要らない(現場の仕様追加)。
        """
        from packaging_tool.presenters import outputs

        self.search(EX_輸出区分="EX")
        session = self.session()
        self.assertFalse(session.palette.is_set)
        self.assertIsNone(outputs.send_refusal(session))

    def test_EX受注でない通常はパレットを求める(self) -> None:
        """**通常の経路は変えない。** EXでだけ関門を外す。"""
        from packaging_tool.presenters import outputs

        self.search()
        session = self.session()
        refusal = outputs.send_refusal(session)
        self.assertIsNotNone(refusal)
        self.assertIn("パレット", refusal.message)

    def test_EX受注はコードと単位と発注数を空で送る(self) -> None:
        """発注コード=空 / 品名=EX / 単位=空 / 発注数=空。

        中途半端な値を送ると、別途届く実データとの突き合わせで混乱する。
        どのロットのEXかは LotNo と寸法で分かるので、そこは残す。
        """
        from packaging_tool.presenters import outputs

        self.search(EX_輸出区分="EX")
        rows = outputs.build_orders(self.session())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["hinmei"], outputs.EX_HINMEI)
        self.assertEqual(row["hatchu_code"], "")
        self.assertEqual(row["tani"], "")
        self.assertEqual(row["hatchu_suu"], "")
        self.assertTrue(row["is_ex_order"])
        # 突き合わせに要るものは残す
        self.assertEqual(row["lot_no"], "1234567")
        self.assertTrue(row["haba"])
        self.assertTrue(row["take"])

    def test_1P0113なら裸梱包の帯が出る(self) -> None:
        state = self.search(包装仕様NO="1P0113")
        self.assertEqual(state["banner"]["kind"], "1p0113")
        self.assertTrue(state["banner"]["visible"])
        self.assertTrue(state["mode_1p0113"])

    def test_通常の包装仕様なら帯は出ない(self) -> None:
        state = self.search(包装仕様NO="1P0001")
        self.assertFalse(state["banner"]["visible"])
        self.assertFalse(state["mode_1p0113"])

    def test_ロットが画面に出る(self) -> None:
        state = self.search()
        self.assertIn("1234567", state["lot_caption"])
        self.assertEqual(state["lot_no"], "1234567")


# ==================================================================
# ボード選定 (Phase 6b)
# ==================================================================
def insert_board(conn, *, width, length, board_type="ハードボード") -> None:
    conn.execute(
        "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ) VALUES (?,?,?)",
        (width, length, board_type))
    conn.commit()


def insert_angle(conn, length: int) -> None:
    conn.execute("INSERT INTO CornerboardMaster (アングル丈) VALUES (?)", (length,))
    conn.commit()


@unittest.skipUnless(HAS_WEB, _SKIP)
class BoardTestCase(SelectionWebTestCase):
    """パレットと製品サイズが決まっている状態から始める。"""

    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1100, length=2000)
        for width, length in ((1000, 1000), (500, 1000), (100, 2000)):
            insert_board(self.conn, width=width, length=length)
        insert_board(self.conn, width=900, length=900, board_type="プロテックボード")

    def sizes(self) -> None:
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply", {"width": "1000", "length": "1800"})

    def expand(self, width: int = 1000, length: int = 1800) -> None:
        """ロットから製品サイズが入った状態(`資材展開`のあと)。"""
        from packaging_tool import work_context
        context = work_context.get_context()
        context.product_width = width
        context.product_length = length

    def boards(self, state=None) -> dict:
        return (state or self.get())["boards"]


class StepTests(BoardTestCase):
    """作業の段 ── **いま押すべきものが1つに見えるか**。

    16個のボタンが同じ重さで並ぶと、押すべきものを毎回探すことになる
    (ヒックの法則)。順序・番号・要約・次の一手は**サーバが決める**ので、
    画面ごとに言い回しや条件が割れない。
    """

    def steps(self, state=None) -> dict:
        return {s["key"]: s for s in (state or self.get())["steps"]}

    def test_製品とパレットは1つの段(self) -> None:
        """**1つの決めごと。** ロットで製品サイズが決まり、それが載る
        パレットを選ぶ ── パレットは製品のために選ぶもの。

        段を分けると、いまの段だけが開く仕組み(§1.3)のせいで
        候補の一覧が必要なときに畳まれてしまう。
        """
        steps = self.steps()
        self.assertNotIn("product", steps)
        self.assertNotIn("pallet", steps)
        self.assertEqual(steps["size"]["number"], 1)
        self.assertEqual(steps["size"]["state"], presenter.STEP_CURRENT)

    def test_段が順に進む(self) -> None:
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply", {"width": "1000", "length": "1800"})
        steps = self.steps()
        self.assertEqual(steps["size"]["state"], presenter.STEP_DONE)
        self.assertEqual(steps["boards"]["state"], presenter.STEP_CURRENT)

    def test_片方だけでは終わらない(self) -> None:
        """パレットだけ決めても、載ることを確かめるまでは終わっていない。"""
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})
        self.assertEqual(self.steps()["size"]["state"], presenter.STEP_CURRENT)

    def test_畳んでも確定値が残る(self) -> None:
        """**要約に確定値を必ず残す。** 畳んだ段でも「何が決まっているか」
        は失われない(設計指針 §1.3)。"""
        self.sizes()
        summary = self.steps()["size"]["summary"]
        for value in ("1100", "2000", "1000", "1800"):
            with self.subTest(value=value):
                self.assertIn(value, summary,
                              f"畳んだ要約に {value} が残っていません: {summary}")

    def test_次の一手は1つだけ(self) -> None:
        """押すべきものが2つあると、どちらでもよいように読める。"""
        self.sizes()
        state = self.get()
        self.assertEqual(state["primary_action"], "autoSelect")
        self.assertTrue(state["next_hint"])

    def test_選定したら次は配置(self) -> None:
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        self.assertEqual(self.get()["primary_action"], "place")

    def test_配置したらボードは済になる(self) -> None:
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")
        self.assertEqual(self.steps()["boards"]["state"], presenter.STEP_DONE)

    def test_製品サイズが空なら入れる場所を指す(self) -> None:
        """**押すべきものが無いなら、無い理由を返す。** 空の強調を
        出すと、押せるボタンを探し続けることになる。"""
        state = self.get()
        self.assertFalse(state["can_decide"])
        self.assertEqual(state["primary_action"], "prodWidth")
        self.assertIn("製品の幅・丈", state["next_hint"])

    def test_製品サイズだけでは押せない(self) -> None:
        """**「パレット決定」は一覧で行を選ぶまで押せない。**

        製品 幅・丈が入っただけでは、まだ一覧を見ているだけの段階
        (行を選んでいなければ、決めるものが無い)。次の一手は
        一覧から選ぶことを指す。
        """
        self.expand()
        state = self.get()
        self.assertFalse(state["can_decide"])
        self.assertEqual(state["primary_action"], "palletRows")

    def test_一覧で行を選ぶと押せるようになる(self) -> None:
        """**行を選んで初めて「パレット決定」が押せる。**

        行を選ぶ(`pallet/pick`)と、載ることまで確かめて製品サイズも
        一緒に確定するので、段はそのまま「ボード選定」へ進む
        ── ここで確かめたいのは押せる条件(`can_decide`)そのもの。
        """
        self.expand()
        self.post("/api/selection/pallet/pick",
                  {"width": "1100", "length": "2000", "symbol": ""})
        self.assertTrue(self.get()["can_decide"])

    def test_番号は見える段だけで振る(self) -> None:
        """飛び番があると、抜けた段を探すことになる。"""
        visible = [s for s in self.get()["steps"] if not s["hidden"]]
        self.assertEqual([s["number"] for s in visible],
                         list(range(1, len(visible) + 1)))

    def test_現在の段はいつも1つ(self) -> None:
        for setup in (lambda: None,
                      lambda: self.post("/api/selection/pallet/apply",
                                        {"width": "1100", "length": "2000"}),
                      self.sizes):
            setup()
            current = [s for s in self.get()["steps"]
                       if s["state"] == presenter.STEP_CURRENT]
            with self.subTest(current=[s["key"] for s in current]):
                self.assertEqual(len(current), 1)

    def test_画面にも段が出ている(self) -> None:
        """JSが動く前から番号と題が読める。"""
        html = self.client.get("/selection").get_data(as_text=True)
        self.assertIn('data-step="size"', html)
        self.assertIn('class="stepno"', html)


class BoardListTests(BoardTestCase):
    def test_種別は未選択なら先頭が選ばれる(self) -> None:
        """tkinter版 `refresh_board_types` の `current(0)`。"""
        boards = self.boards()
        self.assertEqual(boards["board_types"], ["ハードボード", "プロテックボード"])
        self.assertEqual(boards["board_type"], "ハードボード")

    def test_候補は選んだ種別だけ(self) -> None:
        boards = self.boards(self.post("/api/selection/board-type",
                                       {"board_type": "プロテックボード"}))
        self.assertEqual([c["width"] for c in boards["candidates"]], [900])
        self.assertIn("プロテックボード", boards["candidate_note"])

    def test_知らない種別は400(self) -> None:
        self.post("/api/selection/board-type", {"board_type": "無い種別"},
                  expect=400)

    def test_マスタから消えた種別は選び直す(self) -> None:
        """残っていない種別のままだと候補が0件になり、理由が読めない。"""
        self.post("/api/selection/board-type", {"board_type": "プロテックボード"})
        self.conn.execute("DELETE FROM BoardMaster WHERE ボードタイプ = 'プロテックボード'")
        self.conn.commit()
        self.assertEqual(self.boards()["board_type"], "ハードボード")

    def test_在庫考慮ONで印がつく(self) -> None:
        """色だけで示さない。記号(▲薄)も出す。"""
        self.assertFalse(self.boards()["stock_aware"])
        boards = self.boards(self.post("/api/selection/toggle/stock_aware"))
        self.assertTrue(boards["stock_aware"])
        self.assertIn("在庫考慮", boards["candidate_note"])
        # 印の列そのものは常にある(在庫が薄い行が無ければ空文字)
        self.assertEqual(len(boards["candidates"][0]["values"]),
                         len(presenter.BOARD_COLUMNS))


class AutoSelectTests(BoardTestCase):
    def test_サイズが決まるまで押せない(self) -> None:
        """押してから断るより、押せない理由が先に見えているほうがよい。"""
        boards = self.boards()
        self.assertFalse(boards["can_select"])
        self.assertIn("パレット", boards["select_why"])

        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})
        self.assertIn("製品サイズ", self.boards()["select_why"])

        self.post("/api/selection/product/apply", {"width": "1000", "length": "1800"})
        self.assertTrue(self.boards()["can_select"])

    def test_サイズが無いまま送ったら422(self) -> None:
        """形は正しいが業務として通せない ── 入力の誤りではない。"""
        body = self.post("/api/selection/boards/auto-select", expect=422)
        # 断られても画面ぜんぶが入っている。押した拍子に一覧が消えない
        self.assertIn("boards", body)
        self.assertIn("パレット", body["message"])

    def test_断りの理由は最上位のmessageに置く(self) -> None:
        """画面はここから読む(`api.js` の `ApiError`)。

        入れ忘れると「通信に失敗しました (HTTP 422)」と出る ──
        **通信は成功しているのに通信の失敗として案内される**ので、
        現場は直しようがない。
        """
        for path in ("/api/selection/boards/auto-select",
                     "/api/selection/angles/auto"):
            with self.subTest(path=path):
                body = self.post(path, expect=422)
                self.assertTrue(body.get("message"), path)

    def test_候補が無ければ422(self) -> None:
        self.conn.execute("DELETE FROM BoardMaster")
        self.conn.commit()
        self.sizes()
        body = self.post("/api/selection/boards/auto-select", expect=422)
        self.assertIn("候補ボード", body["message"])

    def test_選定すると上下が埋まる(self) -> None:
        self.sizes()
        state = self.post("/api/selection/boards/auto-select")
        boards = state["boards"]
        self.assertTrue(boards["lower"])
        self.assertIn("種", boards["summary"])
        # 並びがそのまま実体。削除はこの位置で指す
        self.assertEqual([row["index"] for row in boards["lower"]],
                         list(range(len(boards["lower"]))))

    def test_種類と枚数を分けて出す(self) -> None:
        """取り違えると発注がずれる。"""
        self.sizes()
        boards = self.boards(self.post("/api/selection/boards/auto-select"))
        total = sum(row["count"] for row in boards["lower"])
        self.assertIn(f"下用 {len(boards['lower'])}種 {total}枚", boards["summary"])

    def test_見出しに具体的な寸法が出る(self) -> None:
        """配置図はボードを置くまで何も描かれない。段の見出し(畳んでいても
        読める)に寸法まで出ていないと、配置するまで何を選んだか
        分からない(現場の声)。"""
        self.sizes()
        boards = self.boards(self.post("/api/selection/boards/auto-select"))
        for row in boards["lower"]:
            with self.subTest(row=row):
                self.assertIn(f"{row['width']}×{row['length']}×{row['count']}",
                              boards["summary"])

    def test_クリアで空に戻る(self) -> None:
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        boards = self.boards(self.post("/api/selection/boards/clear"))
        self.assertEqual(boards["lower"], [])
        self.assertEqual(boards["upper"], [])
        self.assertIsNone(self.session().select_result)

    def test_クリアしてもアングルは残る(self) -> None:
        """tkinter版と同じ。片方だけ違う消え方をすると確かめようがない。"""
        insert_angle(self.conn, 1800)
        self.sizes()
        self.post("/api/selection/angles/add", {"length": 1800})
        state = self.post("/api/selection/boards/clear")
        self.assertEqual(state["angles"]["selected"], [1800])

    def test_疲労度は押しっぱなしで記録に残る(self) -> None:
        """後から「なぜこの選定結果か」を辿れるのは選定ログだけ。"""
        from packaging_tool.user_log import get_user_log
        state = self.post("/api/selection/toggle/fatigue")
        self.assertTrue(state["boards"]["fatigue"])
        self.assertTrue(any("疲労度優先" in entry.text
                            for entry in get_user_log().entries))

    def test_疲労度の拠点を画面に出す(self) -> None:
        """どの拠点で計算したかが分からないと並びの理由が読めない。"""
        self.assertTrue(self.boards()["base_point"])

    def test_疲労度はリボンに出る(self) -> None:
        """効いていることが画面のどこにも現れないまま結果だけが変わる。

        在庫考慮は候補一覧に「(在庫考慮)」と出るのでリボンには出さない
        (同じものを2か所に出さない)。
        """
        def chips(state):
            return [chip["text"] for chip in state["ribbon"]["modes"]]

        self.assertNotIn("疲労度優先", chips(self.get()))
        state = self.post("/api/selection/toggle/fatigue")
        self.assertIn("疲労度優先", chips(state))
        self.assertEqual([c["kind"] for c in state["ribbon"]["modes"]
                          if c["text"] == "疲労度優先"], ["fatigue"])

        self.assertNotIn("疲労度優先",
                         chips(self.post("/api/selection/toggle/fatigue")))

    def test_在庫考慮はリボンに出さない(self) -> None:
        state = self.post("/api/selection/toggle/stock_aware")
        self.assertEqual([c["text"] for c in state["ribbon"]["modes"]], [])

    def test_疲労度の持ち主はひとつ(self) -> None:
        """状態を2か所に持つと必ずどちらかが古くなる。

        リボンを組み立てる `shell_context()` はDB接続を持たないので、
        持ち主は `work_context` のほう。セッションは素通しする。
        """
        self.post("/api/selection/toggle/fatigue")
        self.assertTrue(work_context.get_context().fatigue)
        self.assertTrue(self.session().fatigue)

        work_context.get_context().fatigue = False
        self.assertFalse(self.session().fatigue)

    def test_ロットが変わっても疲労度は解けない(self) -> None:
        """押しっぱなしのモードは利用者が自分で切るまで効き続ける。"""
        self.post("/api/selection/toggle/fatigue")
        work_context.get_context().clear()
        self.assertTrue(self.session().fatigue)

    def test_疲労度ONでも選定できる(self) -> None:
        """棚データが無い環境では通常選定へ倒す。黙って倒さず理由を返す。"""
        self.sizes()
        self.post("/api/selection/toggle/fatigue")
        state = self.post("/api/selection/boards/auto-select")
        self.assertTrue(state["boards"]["lower"])

    def test_選定してはじめて配置できる(self) -> None:
        """押せる見た目で何も起きないのが一番わるい。"""
        self.sizes()
        boards = self.boards()
        self.assertFalse(boards["can_place"])
        self.assertIn("選定", boards["place_why"])

        boards = self.boards(self.post("/api/selection/boards/auto-select"))
        self.assertTrue(boards["can_place"])
        self.assertEqual(boards["place_why"], "")


class ManualBoardTests(BoardTestCase):
    def test_一覧に無いボードは足せない(self) -> None:
        """tkinter版は一覧から選ばせて保証していた(強制選択機能)。

        Web版は値がそのまま送られてくるので、サーバ側で確かめる。
        """
        self.sizes()
        body = self.post("/api/selection/boards/add",
                         {"category": "upper", "width": 9999,
                          "length": 9999, "count": 1}, expect=400)
        self.assertEqual(body["error"]["code"], "not_listed")

    def test_一覧にあれば足せる(self) -> None:
        self.sizes()
        boards = self.boards(self.post(
            "/api/selection/boards/add",
            {"category": "lower", "width": 500, "length": 1000, "count": 2}))
        self.assertEqual([(r["width"], r["length"], r["count"])
                          for r in boards["lower"]], [(500, 1000, 2)])

    def test_枚数が数でなければ400(self) -> None:
        self.sizes()
        body = self.post("/api/selection/boards/add",
                         {"category": "lower", "width": 500,
                          "length": 1000, "count": "いち"}, expect=400)
        self.assertEqual(body["error"]["field"], "count")

    def test_枚数が0以下なら400(self) -> None:
        self.sizes()
        self.post("/api/selection/boards/add",
                  {"category": "lower", "width": 500, "length": 1000,
                   "count": 0}, expect=400)

    def test_枚数の上限は100(self) -> None:
        """VBA `spnBoardCount` の `.Max = 100`。原文はテキスト欄に直接
        打てば素通りしたが、上限が無いと配置が現実に無い枚数を並べようと
        して黙って時間を使う。"""
        self.sizes()
        body = self.post("/api/selection/boards/add",
                         {"category": "lower", "width": 500,
                          "length": 1000, "count": 101}, expect=400)
        self.assertIn("1〜100", body["error"]["message"])

        boards = self.boards(self.post(
            "/api/selection/boards/add",
            {"category": "lower", "width": 500, "length": 1000, "count": 100}))
        self.assertEqual(boards["lower"][0]["count"], 100)

    def test_上下の指定を間違えたら400(self) -> None:
        self.sizes()
        self.post("/api/selection/boards/add",
                  {"category": "まんなか", "width": 500,
                   "length": 1000, "count": 1}, expect=400)

    def test_手動で触ったら自動選定の前提を捨てる(self) -> None:
        """狭幅パレット判定は自動選定時の並びに対するもの。

        引き継ぐと、手で足したボードを当時の前提のまま配置してしまう。
        """
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        self.assertIsNotNone(self.session().select_result)
        self.post("/api/selection/boards/add",
                  {"category": "lower", "width": 500, "length": 1000, "count": 1})
        self.assertIsNone(self.session().select_result)

    def test_削除は位置で指す(self) -> None:
        self.sizes()
        for width in (500, 100):
            self.post("/api/selection/boards/add",
                      {"category": "upper", "width": width,
                       "length": 1000 if width == 500 else 2000, "count": 1})
        boards = self.boards(self.post("/api/selection/boards/remove",
                                       {"category": "upper", "index": 0}))
        self.assertEqual([r["width"] for r in boards["upper"]], [100])

    def test_もう無い行を消そうとしたら400(self) -> None:
        """別のタブで先に消されている。黙って別の行を消さない。"""
        self.sizes()
        self.post("/api/selection/boards/remove",
                  {"category": "upper", "index": 3}, expect=400)


class SharedBoardTests(BoardTestCase):
    def test_上下共用なら上用の欄は無い(self) -> None:
        """保護材がアングル以外に確定すると上用=下用と同サイズになる。

        欄を残すと、上用を選び忘れたのか同じでよいのか区別できない。
        """
        boards = self.boards()
        self.assertTrue(boards["show_upper"])
        self.assertEqual(boards["lower_title"], presenter.LIST_TITLE_LOWER)

        self.session().presenter.apply_hosozai("プラダン")
        boards = self.boards()
        self.assertFalse(boards["show_upper"])
        self.assertEqual(boards["lower_title"], presenter.LIST_TITLE_SHARED)

    def test_アングルなら共用にならない(self) -> None:
        self.session().presenter.apply_hosozai("アングル")
        self.assertTrue(self.boards()["show_upper"])


# ==================================================================
# 候補変更(敷き詰め方式) ── 現行の選定・配置の**補助**
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class ChangeCandidateTests(SelectionWebTestCase):
    """押すたびに A→B→C と回り、選定と配置がまとめて入れ替わる。

    **現行の「ボード選定」「ボード配置」には触らない。** 押せばいつでも
    現行に戻せることを、ここで確かめておく。
    """

    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1540, length=2550)
        # 丈から入れば 1250×2500 で丈カットゼロに届く在庫
        for width, length in ((1250, 2500), (1030, 1520), (30, 2500),
                              (50, 1600), (100, 2000), (100, 2500)):
            insert_board(self.conn, width=width, length=length)

    def sizes(self) -> None:
        self.post("/api/selection/pallet/apply",
                  {"width": "1540", "length": "2550"})
        self.post("/api/selection/product/apply",
                  {"width": "1505", "length": "2502"})

    def change(self, expect=200) -> dict:
        return self.post("/api/selection/boards/candidate", expect=expect)

    def dims(self, state, side="lower") -> list[tuple[int, int]]:
        return [(r["width"], r["length"]) for r in state["boards"][side]]

    # --- 押せるかどうか -------------------------------------------
    def test_サイズが決まるまで押せない(self) -> None:
        boards = self.get()["boards"]
        self.assertFalse(boards["can_change"])
        self.assertIn("パレット", boards["change_why"])
        self.sizes()
        self.assertTrue(self.get()["boards"]["can_change"])

    def test_選定を先に押しておく必要は無い(self) -> None:
        """現行の選定を通さずに別の解を出すのが、このボタンの役目。"""
        self.sizes()
        self.assertFalse(self.get()["boards"]["lower"])
        self.assertTrue(self.get()["boards"]["can_change"])
        self.assertTrue(self.change()["boards"]["lower"])

    def test_サイズが無いまま送ったら422(self) -> None:
        body = self.change(expect=422)
        self.assertIn("boards", body, "断られても画面ぜんぶが返る")
        self.assertIn("パレット", body["message"])

    def test_プロテックは対象外(self) -> None:
        """プロテックのルール(製品幅基準・マイナス許容)を別案は持たない
        (VBA `btnChangeCandidate_Click`)。"""
        from packaging_tool.selection_tiling import PROTEC_TILING_REFUSAL
        self.assertIn("「ボード選定」「ボード配置」で配置してください",
                      PROTEC_TILING_REFUSAL)
        self.sizes()
        session = self.session()
        session.presenter.protec.is_protec = True
        boards = self.get()["boards"]
        self.assertFalse(boards["can_change"])
        self.assertEqual(boards["change_why"], PROTEC_TILING_REFUSAL)
        body = self.change(expect=422)
        self.assertEqual(body["message"], PROTEC_TILING_REFUSAL)

    # --- 切断(VBA 側の切断依頼の定義) ------------------------------
    def _to_axis(self, method: str) -> None:
        for _ in range(3):
            self.change()
            if self.session().placement_method == method:
                return
        self.fail(f"{method} に届かない")

    def test_カット辞書は別案の配置から作り直す(self) -> None:
        """前の選定のカットを残さない(課題表 1。VBA `RebuildCutInfoFromPlacement`)。"""
        from packaging_tool.selection_records import RestoredFacts
        self.sizes()
        self.session().restored = RestoredFacts(cut_info={"999x999": 1})
        self._to_axis("別案A")
        # A はカット無し
        self.assertEqual(self.session().cut_dicts(), ({}, {}, {}))
        self._to_axis("別案C")
        # C は 1250×2500 を1枚ずつ幅で切る(切断線=板の丈)
        self.assertEqual(self.session().cut_dicts(), ({"1250x2500": 2500}, {}, {}))

    def test_別案で切るのは行の最後の1枚だけ(self) -> None:
        """同じサイズでも主とカット前提を別の行にし、切る1枚だけ依頼に載せる
        (課題表 3・5)。幅は定義どおり 下用=パレット幅 / 上用=製品幅−20。"""
        from packaging_tool import reports
        self.sizes()
        self._to_axis("別案C")
        session = self.session()
        self.assertEqual([(b.width, b.length, b.count, b.tag) for b in session.selected.lower],
                         [(1250, 2500, 1, "主"), (1250, 2500, 1, "カット前提")])
        placed = session.placement.placed
        lower = reports.get_cut_size_info(session.selected.lower, placed, "下用", 1540, 2550)
        upper = reports.get_cut_size_info(session.selected.upper, placed, "上用", 1505, 2502)
        # 下用: 1540 − 1250 = 290 / 上用: (1505 − 20) − 1250 = 235
        self.assertEqual((lower.size_width_only, lower.count_width_only), ("290x2500", 1))
        self.assertEqual((upper.size_width_only, upper.count_width_only), ("235x2500", 1))
        self.assertEqual((lower.size_both, upper.size_both), ("", ""))

    # --- 何が出るか -----------------------------------------------
    def test_現行では出ない丈カットゼロの解が出る(self) -> None:
        self.sizes()
        current = self.post("/api/selection/boards/auto-select")
        self.assertNotIn((1250, 2500), self.dims(current))

        state = self.change()
        self.assertIn((1250, 2500), self.dims(state))
        self.assertIn((1250, 2500), self.dims(state, "upper"))

    def test_中身が変わる軸だけを回る(self) -> None:
        """**押しても画面が変わらないのは「壊れている」に見える。**

        A(枚数最小)とB(種類最小)は、しばしば同じ候補が両方の1位に
        なる(この在庫では「1250×2500 ×1 + 100×2500 ×3」が枚数でも
        種類でも最適)。軸だけ進めて中身が同じものを出すと、押した側には
        反応が無いのと区別がつかない。
        """
        self.sizes()
        seen = [self.change()["boards"]["change_axis"] for _ in range(4)]
        self.assertEqual([s[0] for s in seen], ["A", "C", "A", "C"],
                         "AとBが同じ内容なので、Bは飛ばしてCへ進む")

    def test_飛ばしたことを画面と選定ログに残す(self) -> None:
        """黙って飛ばすと「AのつぎがCなのはなぜか」が読めない。"""
        self.sizes()
        self.change()                              # A
        body = self.change()                       # B を飛ばして C
        self.assertTrue(any("飛ばしました" in note
                            for note in body.get("notes", [])), body.get("notes"))
        text = "\n".join(e.text
                         for e in self.session().presenter.user_log.entries)
        self.assertIn("同じ内容のため飛ばしました", text)

    def test_いま出している軸を自分と見比べない(self) -> None:
        """見に行くのは**残りの2つだけ**。

        3周ぶん回すと、3周目はいま出している軸そのものに戻ってきて
        「候補Aは候補Aと同じ内容のため飛ばしました」という読めない
        1行が出る。
        """
        self.conn.execute("DELETE FROM BoardMaster")
        self.conn.execute("DELETE FROM PalletMaster")
        insert_pallet(self.conn, width=1100, length=2000)
        for width, length in ((1000, 1000), (500, 1000), (100, 2000)):
            insert_board(self.conn, width=width, length=length)
        self.conn.commit()
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})

        self.change()                              # A
        # 選定ログはプロセスに1本(`user_log._shared`)で、同じ試験の中でも
        # 前の試験の行が残る。**この押下で増えた分だけ**を数える
        before = len(self.session().presenter.user_log.entries)
        self.change(expect=422)                    # 3軸とも同じ
        skips = [e.text
                 for e in self.session().presenter.user_log.entries[before:]
                 if "飛ばしました" in e.text]
        self.assertEqual(len(skips), 2, skips)     # B と C だけ
        for text in skips:
            self.assertNotIn("候補A(枚数最小) は候補A(枚数最小)", text)

    def test_前の軸と同じ中身は名前を変えて出し直さない(self) -> None:
        """C が A と同じで B だけ違うとき(実データ H8330P0 で起きた)。

        いま出ている軸とだけ比べていたので、A→B の次に「B と違う」C を
        出し、**A と同じ候補が候補Cとしてもう一度出ていた**。2回目の押下では
        「候補C は同じ内容なので飛ばしました」と言っていたのに、3回目で
        「候補C に切り替えました」と出て、言うことも食い違っていた。
        """
        from unittest import mock

        from packaging_tool import selection_tiling

        session = self.session()
        state = selection_tiling.TilingState(
            key=("x",), lower=(object(), object(), object()),
            upper=(object(), object(), object()), axis=-1)
        content = {0: "A案", 1: "B案", 2: "A案"}
        with mock.patch.object(type(session), "_axis_signature",
                               lambda self, st, axis: content[axis]):
            order, notes = [], []
            for _ in range(4):
                axis, skipped = session._next_tiling_axis(state)
                state.axis = axis
                order.append("ABC"[axis])
                notes.append(skipped)
        self.assertEqual(order, ["A", "B", "A", "B"])
        self.assertEqual(notes[1], [])             # A→B では C を持ち出さない
        self.assertEqual([(n[0], o[0]) for n, o in notes[2]], [("C", "A")])

    def test_作った時点で同じ軸に印を付ける(self) -> None:
        """押す前から「AとBは同じ」と分かっていれば、驚かずに済む。"""
        self.sizes()
        self.change()
        text = "\n".join(e.text
                         for e in self.session().presenter.user_log.entries)
        self.assertIn("と同じ内容", text)

    def test_Cはカット前提で枚数が減る(self) -> None:
        self.sizes()
        a = self.change()                          # A
        c = self.change()                          # B は飛ばされて C
        self.assertEqual(c["boards"]["change_axis"][0], "C")
        self.assertLess(sum(r["count"] for r in c["boards"]["lower"]),
                        sum(r["count"] for r in a["boards"]["lower"]))
        self.assertIn("カット前提",
                      {r["tag"] for r in c["boards"]["lower"]})

    def test_配置まで入れ替わる(self) -> None:
        """選定だけ替えて図が古いままだと、図と一覧が食い違う。"""
        self.sizes()
        state = self.change()
        self.assertTrue(state["plans"]["lower"])
        placed = self.session().placement.placed
        self.assertTrue(placed)
        # 行モデルは丈方向に積む。1行なら全部 X=0 から始まる
        self.assertEqual({p.x for p in placed if p.board_category == "下用"},
                         {0})

    # --- 現行に触らないこと ---------------------------------------
    def test_現行の選定を押せば戻る(self) -> None:
        self.sizes()
        self.change()
        state = self.post("/api/selection/boards/auto-select")
        self.assertNotIn((1250, 2500), self.dims(state))
        self.assertEqual(state["boards"]["change_axis"], "",
                         "現行に戻ったのだから、回り位置も戻す")

    def test_現行に戻ったあとはAから始まる(self) -> None:
        self.sizes()
        self.change()
        self.change()                      # B まで進めておく
        self.post("/api/selection/boards/auto-select")
        self.assertEqual(self.change()["boards"]["change_axis"][0], "A")

    def test_狭幅パレットの前提は持ち込まない(self) -> None:
        """敷き詰めは狭幅の専用経路を数え上げに吸収している。"""
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        self.change()
        self.assertIsNone(self.session().select_result)

    def test_クリアで回り位置も戻る(self) -> None:
        self.sizes()
        self.change()
        self.post("/api/selection/boards/clear")
        self.assertEqual(self.get()["boards"]["change_axis"], "")
        self.assertEqual(self.change()["boards"]["change_axis"][0], "A")

    # --- 前提が変わったら候補を捨てる -----------------------------
    #
    # **ここが崩れると、いま入力してある寸法と何の関係も無いボードが
    # 選定され、そのまま図になる。** 図と現物が食い違ったまま切断依頼や
    # 倉庫送信まで進めてしまう ── 静かに間違うたぐいの中でいちばん重い。
    def test_寸法を変えたら作り直してAから(self) -> None:
        self.sizes()
        self.change()
        self.change()                      # C まで進めておく
        self.post("/api/selection/product/apply",
                  {"width": "1500", "length": "2500"})
        self.assertEqual(self.change()["boards"]["change_axis"][0], "A")

    def test_変えた寸法で選び直す(self) -> None:
        """軸が戻るだけでは足りない。**中身も新しい寸法のもの**になる。"""
        self.sizes()
        before = self.change()["boards"]["lower"]
        # 製品を大きく変える。前の候補(1250×2500)は幅が足りなくなる
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "1600"})
        self.post("/api/selection/product/apply",
                  {"width": "1060", "length": "1560"})
        after = self.change()["boards"]["lower"]
        self.assertNotEqual([(r["width"], r["length"]) for r in before],
                            [(r["width"], r["length"]) for r in after])
        # 図も新しいパレットに収まっている。**在庫の実寸ではなく、
        # 置かれた大きさで見る** ── 細ボードは長辺を切って使うので、
        # 在庫の丈(2000等)がそのまま図の大きさになるとは限らない
        placed = [p for p in self.session().placement.placed
                  if p.board_category == "下用"]
        self.assertTrue(placed)
        self.assertLessEqual(max(p.x + p.length for p in placed), 1600 * 1.2)

    def test_パレットだけ変えても作り直す(self) -> None:
        self.sizes()
        self.change()
        self.change()                      # C
        self.post("/api/selection/pallet/apply",
                  {"width": "1540", "length": "2600"})
        self.post("/api/selection/product/apply",
                  {"width": "1505", "length": "2502"})
        self.assertEqual(self.change()["boards"]["change_axis"][0], "A")

    def test_ボード種別を変えても作り直す(self) -> None:
        """寸法は同じでも、引いてくる候補が変われば答えも変わる。"""
        insert_board(self.conn, width=1200, length=2400,
                     board_type="プロテックボード")
        self.sizes()
        self.change()
        self.change()                      # C
        self.post("/api/selection/board-type",
                  {"board_type": "プロテックボード"})
        self.post("/api/selection/board-type", {"board_type": "ハードボード"})
        self.assertEqual(self.change()["boards"]["change_axis"][0], "A",
                         "種別を往復しても、途中で前提が変わっている")

    def test_ボードマスタを入れ直したら作り直す(self) -> None:
        """**寸法もモードも変わらないのに候補だけ変わる**唯一の経路。

        取り込み直しでマスタが入れ替わったあと、もう無い寸法を出し
        続けると、現物が取れないボードで図が描かれる。
        """
        self.sizes()
        before = self.change()["boards"]["lower"]
        self.assertIn((1250, 2500), [(r["width"], r["length"]) for r in before])

        self.conn.execute("DELETE FROM BoardMaster WHERE ボード幅 = 1250")
        insert_board(self.conn, width=1500, length=2500)
        self.conn.commit()

        after = self.change()
        self.assertEqual(after["boards"]["change_axis"][0], "A")
        self.assertNotIn((1250, 2500),
                         [(r["width"], r["length"]) for r in after["boards"]["lower"]])

    def test_作り直したことを選定ログに残す(self) -> None:
        self.sizes()
        self.change()
        self.post("/api/selection/product/apply",
                  {"width": "1500", "length": "2500"})
        self.change()
        text = "\n".join(e.text
                         for e in self.session().presenter.user_log.entries)
        self.assertIn("前提が変わったので候補を作り直します", text)

    def test_3軸とも同じなら理由を言って断る(self) -> None:
        """全件検証で3軸すべて同じは上用15.3% / 下用42.4%。珍しくない。

        「候補が作れなかった」のと「作れたが全部同じだった」のとでは、
        次にすることが違う ── 前者は在庫や寸法を疑い、後者はこれが
        唯一の答えだと分かる。同じ文言で返してはいけない。
        """
        self.conn.execute("DELETE FROM BoardMaster")
        self.conn.execute("DELETE FROM PalletMaster")
        insert_pallet(self.conn, width=1100, length=2000)
        for width, length in ((1000, 1000), (500, 1000), (100, 2000)):
            insert_board(self.conn, width=width, length=length)
        self.conn.commit()
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})

        first = self.change()
        self.assertEqual(first["boards"]["change_axis"][0], "A")
        body = self.change(expect=422)
        self.assertIn("ほかに別の候補はありません", body["message"])
        self.assertIn("A", body["message"])
        # **断られても、いま出ている候補はそのまま残る**
        self.assertEqual(body["boards"]["lower"], first["boards"]["lower"])
        self.assertEqual(body["boards"]["change_axis"][0], "A")

    def test_上下そろう軸を先に出す(self) -> None:
        """現場のログと同じ形。**片側だけの答えは使えない。**

        上用は要カット0では作れないが、カット1枚まで許せば作れる ──
        このとき A(要カット0)を出すと下用だけ入れ替わって上用が空に
        なる。両方そろう軸(この在庫ではC)を先に選ぶ。
        """
        self.conn.execute("DELETE FROM BoardMaster")
        self.conn.execute("DELETE FROM PalletMaster")
        insert_pallet(self.conn, width=600, length=600)
        for width, length in ((600, 600), (30, 2500), (50, 1600),
                              (100, 2000), (100, 2500)):
            insert_board(self.conn, width=width, length=length)
        self.conn.commit()
        self.post("/api/selection/pallet/apply",
                  {"width": "600", "length": "600"})
        self.post("/api/selection/product/apply",
                  {"width": "552", "length": "572"})

        session = self.session()
        session.change_candidate()
        state = session.tiling
        # 前提: A/B に上用は無く、C にだけある
        self.assertIsNone(state.upper[0])
        self.assertIsNone(state.upper[1])
        self.assertIsNotNone(state.upper[2])
        self.assertIsNotNone(state.lower[0])

        self.assertEqual(state.axis, 2, "上下そろうCを選ぶ")
        self.assertTrue(session.selected.lower)
        self.assertTrue(session.selected.upper, "上用が空にならない")

    def test_片側だけ空になったら必ず言う(self) -> None:
        """現場の声:「候補変更で何も配置されないことがある」。

        敷き詰めで解が出るかは上用(製品幅-1まで)と下用(パレット幅+50
        まで)で別々に決まるので、片方だけ0件になることがある。黙って
        空にすると「押したら消えた」ようにしか見えない。
        """
        self.conn.execute("DELETE FROM BoardMaster")
        self.conn.execute("DELETE FROM PalletMaster")
        insert_pallet(self.conn, width=1100, length=2000)
        for width, length in ((1000, 1000), (500, 1000), (100, 2000)):
            insert_board(self.conn, width=width, length=length)
        self.conn.commit()
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})

        body = self.change()
        # この在庫では上用(許容幅920〜999)に着地する組み合わせが無い
        self.assertTrue(body["boards"]["lower"])
        self.assertFalse(body["boards"]["upper"])
        self.assertTrue(any("上用" in note and "ありませんでした" in note
                            for note in body.get("notes", [])),
                        body.get("notes"))

    def test_1枚も置けないなら選定を消さない(self) -> None:
        """押しただけで選定が消えたように見えるのがいちばん困る。"""
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        session = self.session()
        session.change_candidate()                 # 候補を作らせる
        before = [(b.width, b.length) for b in session.selected.lower]
        self.assertTrue(before)

        # 置けない候補にすり替える(幅0の行構成 → 1枚も置けない)
        from packaging_tool import tiling_algorithm as tiling
        broken = tiling.TileCand(
            h1=1000, n1=1,
            c1=tiling.TileRowComp(height=1000, dims=[500], qty=[1], width=0))
        session.tiling.lower = (broken, None, None)
        session.tiling.upper = (None, None, None)
        session.tiling.axis = -1
        result = session.change_candidate()

        self.assertFalse(result.ok)
        self.assertIn("配置できませんでした", result.message)
        self.assertEqual([(b.width, b.length) for b in session.selected.lower],
                         before, "選定はそのまま残す")

    def test_選定ログに決め手が残る(self) -> None:
        """どの軸のどこで決まったのかを後から追えるようにする。"""
        self.sizes()
        self.change()
        text = "\n".join(e.text for e in self.session().presenter.user_log.entries)
        self.assertIn("候補変更", text)
        self.assertIn("枚", text)
        self.assertIn("カット", text)

    def test_選定ログに置いた座標も残る(self) -> None:
        """**選んだものだけ分かっても、図がおかしい理由は追えない。**

        現行の「ボード配置」は置いた座標を残していたが、候補変更は
        選んだものを並べるだけで、配置の行が1つも無かった。現場から
        「配置がおかしい」と写真が届いたときに、選定ログを見ても
        どこに置いたのかが分からない(実際にそうなった)。

        書き方は現行と同じにする(`_log_placed`)。経路ごとに形が違うと、
        送られてきたログのどこを読めばよいかが毎回変わる。
        """
        self.sizes()
        # 選定ログはプロセスに1つ(他の試験のぶんが残っている)。
        # **この回で書かれた行だけ**を見たいので、押す前に空にする
        self.session().presenter.user_log.clear()
        self.change()
        session = self.session()
        lines = [e.text for e in session.presenter.user_log.entries
                 if "配置:" in e.text]
        self.assertEqual(len(lines), len(session.placement.placed),
                         "置いた枚数とログの行数が合っていません")
        for item in session.placement.placed:
            self.assertTrue(
                any(f"@ ({item.x}, {item.y})" in line for line in lines),
                f"{item.width}x{item.length} の座標がログにありません")


# ==================================================================
# アングル (Phase 6b)
# ==================================================================
class AngleTests(BoardTestCase):
    def setUp(self) -> None:
        super().setUp()
        for length in (900, 1800, 2000):
            insert_angle(self.conn, length)

    def angles(self, state=None) -> dict:
        return (state or self.get())["angles"]

    def test_候補が出る(self) -> None:
        self.assertEqual(self.angles()["candidates"], [900, 1800, 2000])

    def test_データが無ければ押せない(self) -> None:
        """`load_all_angle_lengths` は未登録でも [0] を返す。0本を候補にしない。"""
        self.conn.execute("DELETE FROM CornerboardMaster")
        self.conn.commit()
        self.sizes()
        angles = self.angles()
        self.assertEqual(angles["candidates"], [])
        self.assertFalse(angles["can_auto"])
        self.assertIn("アングルデータ", angles["auto_why"])

    def test_製品サイズが先(self) -> None:
        angles = self.angles()
        self.assertFalse(angles["can_auto"])
        self.assertIn("製品サイズ", angles["auto_why"])

        self.sizes()
        self.assertTrue(self.angles()["can_auto"])

    def test_製品サイズが無いまま自動なら422(self) -> None:
        body = self.post("/api/selection/angles/auto", expect=422)
        self.assertIn("angles", body)

    def test_候補に無い丈は足せない(self) -> None:
        self.post("/api/selection/angles/add", {"length": 1234}, expect=400)

    def test_候補から足せる(self) -> None:
        angles = self.angles(self.post("/api/selection/angles/add",
                                       {"length": 1800}))
        self.assertEqual(angles["selected"], [1800])

    def test_本数の上限は手動でも効く(self) -> None:
        """**自動選定と同じ上限。** 手で足すときだけ無制限だと、
        自動では出せない本数の選定結果ができ上がる。
        製品丈 5000未満は3本まで(VBA `btnAngleAdd_Click`)。"""
        for _ in range(3):
            self.post("/api/selection/angles/add", {"length": 1800})
        body = self.post("/api/selection/angles/add", {"length": 900},
                         expect=400)
        self.assertIn("最大3本", body["error"]["message"])
        self.assertEqual(self.angles()["selected"], [1800, 1800, 1800])

    def test_製品丈が5000以上なら6本まで(self) -> None:
        """上限そのものが製品丈で変わる(`ANGLE_MULTI_THRESHOLD`)。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "5200"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "5000"})
        for _ in range(6):
            self.post("/api/selection/angles/add", {"length": 1800})
        body = self.post("/api/selection/angles/add", {"length": 900},
                         expect=400)
        self.assertIn("最大6本", body["error"]["message"])

    def test_外せば足せる(self) -> None:
        """上限は「もう足せない」であって「詰んだ」ではない。"""
        for _ in range(3):
            self.post("/api/selection/angles/add", {"length": 1800})
        self.post("/api/selection/angles/remove", {"index": 0})
        angles = self.angles(self.post("/api/selection/angles/add",
                                       {"length": 900}))
        self.assertEqual(angles["selected"], [1800, 1800, 900])

    def test_外せる(self) -> None:
        self.post("/api/selection/angles/add", {"length": 1800})
        self.post("/api/selection/angles/add", {"length": 900})
        angles = self.angles(self.post("/api/selection/angles/remove",
                                       {"index": 0}))
        self.assertEqual(angles["selected"], [900])

    def test_もう無い行を外そうとしたら400(self) -> None:
        self.post("/api/selection/angles/remove", {"index": 0}, expect=400)

    def test_自動選定すると選択に入る(self) -> None:
        self.sizes()
        angles = self.angles(self.post("/api/selection/angles/auto"))
        self.assertTrue(angles["selected"])

    def test_手動で触ったらカット前提を捨てる(self) -> None:
        """手動追加はカット前提の情報を持たない(tkinter版と同じ)。"""
        self.sizes()
        self.post("/api/selection/angles/auto")
        self.session().angle_need_cut = True
        angles = self.angles(self.post("/api/selection/angles/add",
                                       {"length": 900}))
        self.assertFalse(angles["need_cut"])

    def test_見つからなければ422(self) -> None:
        """探した結果として見つからないのは、通信の失敗ではない。"""
        self.conn.execute("DELETE FROM CornerboardMaster")
        self.conn.commit()
        insert_angle(self.conn, 10)
        self.sizes()
        body = self.post("/api/selection/angles/auto", expect=422)
        self.assertIn("見つかりません", body["message"])
        self.assertEqual(body["angles"]["selected"], [])

    def test_上下共用ならアングルの欄ごと無い(self) -> None:
        """使わないものは出さない。消えた理由はその場所に書く。"""
        self.session().presenter.apply_hosozai("プラダン")
        angles = self.angles()
        self.assertFalse(angles["show"])
        self.assertIn("プラダン", angles["hosozai_label"])


# ==================================================================
# 配置図 (Phase 6c)
# ==================================================================
class PlacementTests(BoardTestCase):
    def plans(self, state=None) -> dict:
        return (state or self.get())["plans"]

    def placed(self) -> dict:
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        return self.post("/api/selection/boards/place")

    def test_選定前に配置したら422(self) -> None:
        self.sizes()
        body = self.post("/api/selection/boards/place", expect=422)
        self.assertIn("選定", body["message"])

    def test_サイズが無ければ422(self) -> None:
        body = self.post("/api/selection/boards/place", expect=422)
        self.assertIn("パレット", body["message"])

    def test_未配置と分かるように出す(self) -> None:
        """空欄にすると、配置していないのか図が壊れているのか区別できない。"""
        plans = self.plans()
        self.assertFalse(plans["placed"])
        self.assertEqual(plans["status"], presenter.STATUS_UNPLACED)
        self.assertIsNone(plans["lower"])

    def test_配置すると図の計画が返る(self) -> None:
        plans = self.plans(self.placed())
        self.assertTrue(plans["placed"])
        self.assertIn("配置完了", plans["status"])
        self.assertTrue(plans["lower"]["boards"])
        # 論理キャンバスは固定。拡大縮小はブラウザ(`viewBox`)に任せる
        self.assertEqual(plans["view_box"], "0 0 980 460")

    def test_色はトークンで返す(self) -> None:
        """3つのテーマがあるので、色そのものは `tokens.css` が決める。

        計画が持つVBAのRGBをそのまま描くと、ダークの地で図が読めない。
        """
        plans = self.plans(self.placed())
        rect = plans["lower"]["boards"][0]
        self.assertEqual(rect["fill_token"], "--mat-lower-fill")
        self.assertEqual(rect["line_token"], "--mat-lower-line")
        # 元の色は残す。ゴールデンが見ているのはそちら
        self.assertTrue(rect["fill"].startswith("#"))

    def test_上用と下用で輪郭の色が違う(self) -> None:
        """輪郭が接する相手が違う(淡い赤の上か、淡い緑の上か)。"""
        plans = self.plans(self.placed())
        self.assertEqual(plans["upper"]["boards"][0]["line_token"],
                         "--mat-upper-line")
        self.assertEqual(plans["lower"]["boards"][0]["line_token"],
                         "--mat-lower-line")

    def test_図と一覧が同じ鍵でつながる(self) -> None:
        """図のボードを押すと一覧の該当行が光る(§設計指針 共通運命)。"""
        state = self.placed()
        listed = {row["key"] for row in state["boards"]["lower"]}
        drawn = {rect["key"] for rect in state["plans"]["lower"]["boards"]}
        self.assertTrue(drawn)
        self.assertTrue(drawn <= listed, drawn - listed)

    def test_向きが変わっても同じ鍵になる(self) -> None:
        """配置は1枚ごとに向きを選ぶ。添字や並び順を鍵にできない。"""
        self.assertEqual(presenter.board_key("下用", 750, 1130),
                         presenter.board_key("下用", 1130, 750))

    def test_上用と下用は別の鍵(self) -> None:
        """同じ寸法が両方に出る。分けないと下用を押して上用が光る。"""
        self.assertNotEqual(presenter.board_key("下用", 750, 1130),
                            presenter.board_key("上用", 750, 1130))

    def test_下用の鍵は上用の図と混ざらない(self) -> None:
        state = self.placed()
        lower = {row["key"] for row in state["boards"]["lower"]}
        upper_drawn = {r["key"] for r in state["plans"]["upper"]["boards"]}
        self.assertFalse(lower & upper_drawn)

    def test_寸法は読み上げられる形でも持つ(self) -> None:
        """図の中の文字は小さく、細いボードには入らない。"""
        plans = self.plans(self.placed())
        self.assertIn("幅", plans["lower"]["boards"][0]["title"])

    def test_実測を図の下に出す(self) -> None:
        """配置してみて初めて分かるカット量とはみ出し(VBA `DisplayInfo`)。"""
        plans = self.plans(self.placed())
        self.assertIn("%", plans["usage"])
        self.assertEqual([line["label"] for line in plans["lines"]],
                         ["上用", "下用"])
        for line in plans["lines"]:
            self.assertIn(line["kind"], ("ok", "cut", "deficit"))

    def test_上下共用なら実測も1行(self) -> None:
        """上用の図が無いのに上用の実測だけ残ると、出ていないと読める。"""
        self.placed()
        self.session().presenter.apply_hosozai("プラダン")
        plans = self.plans()
        self.assertFalse(plans["show_upper"])
        self.assertIsNone(plans["upper"])
        self.assertEqual([line["label"] for line in plans["lines"]], ["上下共用"])

    def test_選定を触ると図は消える(self) -> None:
        """古い図が残ると、いま選んでいるボードと食い違ったまま先へ進む。"""
        for operation, body in (
            ("/api/selection/boards/add",
             {"category": "lower", "width": 500, "length": 1000, "count": 1}),
            ("/api/selection/boards/clear", None),
        ):
            with self.subTest(operation=operation):
                self.placed()
                self.assertTrue(self.plans()["placed"])
                self.post(operation, body)
                self.assertFalse(self.plans()["placed"])

    def test_寸法を変えても図は消える(self) -> None:
        """基準の枠が変われば前の配置は成り立たない。"""
        self.placed()
        self.post("/api/selection/pallet/apply", {"width": 1100, "length": 1900})
        self.assertFalse(self.plans()["placed"])


class AnglePlanTests(BoardTestCase):
    def setUp(self) -> None:
        super().setUp()
        for length in (900, 1800, 2000):
            insert_angle(self.conn, length)

    def plans(self, state=None) -> dict:
        return (state or self.get())["plans"]

    def test_選ぶまで押せない(self) -> None:
        self.sizes()
        angles = self.get()["angles"]
        self.assertFalse(angles["can_draw"])
        self.assertIn("アングル", angles["draw_why"])

        self.post("/api/selection/angles/add", {"length": 1800})
        self.assertTrue(self.get()["angles"]["can_draw"])

    def test_同じ理由を2つ出さない(self) -> None:
        """「先に製品サイズを」は自動選定と配置の両方を止める。

        ボタンごとに出すと、同じ行が2つ並ぶ。
        """
        angles = self.get()["angles"]
        self.assertEqual(angles["auto_why"], angles["draw_why"])
        self.assertEqual(angles["why"], [angles["auto_why"]])

        self.sizes()
        angles = self.get()["angles"]
        self.assertEqual(angles["auto_why"], "")
        self.assertEqual(angles["why"], [angles["draw_why"]])

    def test_選ぶ前に配置したら422(self) -> None:
        self.sizes()
        self.post("/api/selection/angles/draw", expect=422)

    def test_選んだだけでは図にしない(self) -> None:
        """確かめていない組み合わせが図になると、そのまま通ってしまう。"""
        self.sizes()
        self.post("/api/selection/angles/add", {"length": 1800})
        self.assertIsNone(self.plans()["angle"])

        state = self.post("/api/selection/angles/draw")
        self.assertIsNotNone(self.plans(state)["angle"])

    def test_バーは丈が読める色で返る(self) -> None:
        self.sizes()
        self.post("/api/selection/angles/add", {"length": 1800})
        plan = self.plans(self.post("/api/selection/angles/draw"))["angle"]
        bar = plan["bars"][0]
        self.assertIn("1800", bar["caption"])
        self.assertTrue(bar["fill_token"].startswith("--angle-bar-"))
        self.assertEqual(bar["text_token"], "--angle-bar-fg")

    def test_アングルを全部外すと図も消える(self) -> None:
        self.sizes()
        self.post("/api/selection/angles/add", {"length": 1800})
        self.post("/api/selection/angles/draw")
        self.post("/api/selection/angles/remove", {"index": 0})
        self.assertIsNone(self.plans()["angle"])


# ==================================================================
# プロテック → ボード種別
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class ProtecBoardTypeTests(SelectionWebTestCase):
    """ロットを引いた時点で種別まで切り替わる(VBA `CheckAndSetProtecMode`)。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        self._lot_original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", self._lot_original))

        insert_pallet(self.conn, width=1100, length=1100)
        insert_board(self.conn, width=1000, length=1000)
        insert_lot(self.conn)
        insert_hiki(self.conn)
        self._insert_odr = insert_odr

    def search(self, **odr) -> dict:
        self._insert_odr(self.conn, **odr)
        self.client.get("/api/lot/1234567", headers=self.auth())
        return self.get()

    def test_プロテックなら種別が切り替わる(self) -> None:
        insert_board(self.conn, width=900, length=900,
                     board_type="プロテックボード")
        state = self.search(包装仕様NO="1P1216")
        self.assertEqual(state["banner"]["kind"], "protec")
        self.assertEqual(state["boards"]["board_type"], "プロテックボード")

    def test_マスタに無ければ選定ログに残す(self) -> None:
        """黙って別の種別から選ぶと、なぜその結果なのかが分からない。"""
        from packaging_tool.user_log import get_user_log
        state = self.search(包装仕様NO="1P1216")
        self.assertEqual(state["boards"]["board_type"], "ハードボード")
        self.assertTrue(any("プロテックボード" in entry.text
                            for entry in get_user_log().entries))

    def test_通常の包装仕様なら切り替わらない(self) -> None:
        insert_board(self.conn, width=900, length=900,
                     board_type="プロテックボード")
        state = self.search(包装仕様NO="1P0001")
        self.assertEqual(state["boards"]["board_type"], "ハードボード")

    def test_プロテックは上用キャンバスも隠す(self) -> None:
        """現場の要望:「保護材上蓋と同じようにプロテックモードも

        『上用キャンバスを隠して上下共用扱いにする』を忘れないように」。
        以前は上下共用モードの対象外で、上用キャンバスが出たままだった
        (製品幅を大きく超えるボードを、超過禁止の枠に配置しようとして
        静かに失敗する不具合の原因)。

        アングルの表示可否(`angles.show`)は上用キャンバスとは独立
        (VBA `ShowAngleControls` の分離と同じ)。ここではマスタが空で
        保護材が引けない(未確定)ので、アングルは既定で表示のまま ──
        上用キャンバスが隠れていることとは別の話であることを、あわせて
        確かめる。
        """
        insert_board(self.conn, width=900, length=900,
                     board_type="プロテックボード")
        state = self.search(包装仕様NO="1P1216")
        self.assertTrue(state["angles"]["show"])   # 保護材未確定、既定は表示
        self.assertFalse(state["boards"]["show_upper"])
        self.assertEqual(state["boards"]["lower_title"], presenter.LIST_TITLE_SHARED)

    def test_保護材がアングルでもプロテックが勝つ(self) -> None:
        """保護材のマスタ設定に関わらず、上用キャンバスはプロテックが優先する。

        アングル自体は実際の保護材判定に従う(独立した判断)。
        """
        insert_board(self.conn, width=900, length=900,
                     board_type="プロテックボード")
        state = self.search(包装仕様NO="1P1216")
        self.assertFalse(state["boards"]["show_upper"])


# ==================================================================
# 1P1185(タイト限定)モード
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class Mode1P1185Tests(SelectionWebTestCase):
    """発動条件(AND): 包装仕様NO=1P1185/取引先にﾅﾒｶﾜｱﾙﾐ/製造板幅・丈1242〜1249。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        self._lot_original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", self._lot_original))
        insert_hiki(self.conn)
        self._insert_lot = insert_lot
        self._insert_odr = insert_odr

    def search(self, *, lot=None, odr=None) -> dict:
        self._insert_lot(self.conn, **(lot or {}))
        self._insert_odr(self.conn, **(odr or {}))
        self.client.get("/api/lot/1234567", headers=self.auth())
        return self.get()

    def test_全条件を満たすとバーが出てタイト以外は隠れる(self) -> None:
        insert_pallet(self.conn, width=1300, length=1300, w_min=1200, w_max=1400,
                     l_min=1200, l_max=1400, industry="タイト")
        insert_pallet(self.conn, width=1150, length=2650, w_min=1000, w_max=1200,
                     l_min=2500, l_max=2800, industry="タイト")
        state = self.search(
            lot={"製造板幅": 1245.0, "製造板丈": 1245.0},
            odr={"包装仕様NO": "1P1185", "取引先名称": "ｶ)ﾅﾒｶﾜｱﾙﾐ"})
        self.assertEqual(state["banner"]["kind"], "1p1185")
        self.assertEqual([row["width"] for row in state["rows"]], [1300])

    def test_取引先が違えば発動しない(self) -> None:
        insert_pallet(self.conn, width=1300, length=1300, w_min=1200, w_max=1400,
                     l_min=1200, l_max=1400, industry="タイト")
        state = self.search(
            lot={"製造板幅": 1245.0, "製造板丈": 1245.0},
            odr={"包装仕様NO": "1P1185", "取引先名称": "ｶ)ﾍﾞﾂ会社"})
        self.assertEqual(state["banner"]["kind"], "")
        self.assertEqual([row["width"] for row in state["rows"]], [1300])

    def test_幅丈が範囲外なら発動しない(self) -> None:
        insert_pallet(self.conn, width=1300, length=1300, w_min=1200, w_max=1400,
                     l_min=1200, l_max=1400, industry="タイト")
        state = self.search(
            lot={"製造板幅": 1300.0, "製造板丈": 1245.0},
            odr={"包装仕様NO": "1P1185", "取引先名称": "ﾅﾒｶﾜｱﾙﾐ"})
        self.assertEqual(state["banner"]["kind"], "")


# ==================================================================
# 管理者と実績パターン (Phase 6d)
# ==================================================================
class AdminTests(BoardTestCase):
    def admin(self, state=None) -> dict:
        return (state or self.get())["admin"]

    def login(self, password: str = "") -> dict:
        """管理者認証を通す。`password` を渡すとその値で試す(失敗を見る用)。"""
        from packaging_tool import config
        ok = not password
        return self.post("/api/selection/auth",
                         {"password": password or config.ADMIN_PASSWORD},
                         expect=200 if ok else 403)

    def test_パスワードは返さない(self) -> None:
        """照合はサーバでしかしない。渡すのは1ビットだけ(設計書 §3.6)。"""
        from packaging_tool import config
        body = self.login()
        self.assertNotIn(config.ADMIN_PASSWORD, json.dumps(body, ensure_ascii=False))
        self.assertTrue(body["admin"]["authenticated"])

    def test_誤りは403(self) -> None:
        """入力の形の誤り(400)でも業務の断り(422)でもない。"""
        body = self.login("ちがう")
        self.assertFalse(body["admin"]["authenticated"])
        self.assertIn("パスワード", body["message"])

    def test_認証しないと保存できない(self) -> None:
        """tkinter版はボタンを無効にして保っていた。Web版は経路で断る。"""
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")
        self.assertFalse(self.admin()["can_save"])

        body = self.post("/api/selection/pattern/save", expect=403)
        self.assertIn("管理者認証", body["message"])

    def test_配置していないと保存できない(self) -> None:
        self.login()
        self.assertIn("配置", self.admin()["save_why"])
        self.post("/api/selection/pattern/save", expect=422)

    def test_認証して配置してあれば保存できる(self) -> None:
        self.login()
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")
        self.assertTrue(self.admin()["can_save"])

        body = self.post("/api/selection/pattern/save")
        self.assertIn("保存しました", body["message"])
        # 保存したものが実績に出る
        self.assertTrue(self.admin()["patterns"])

    def test_実績はパレット寸法で引く(self) -> None:
        self.assertIn("パレットサイズ", self.admin()["patterns_note"])
        self.sizes()
        self.assertIn("1100×2000", self.admin()["patterns_note"])

    def test_実績を読み込むと保存した画面に戻る(self) -> None:
        """**計算し直さずに戻す**(VBA `LoadSinglePattern` のスナップショット版)。

        以前は寸法と枚数だけを戻して配置は無し・タグも失われていた。
        いまは選定リスト(タグ込み)と配置図が、保存したときのまま戻る。
        """
        self.login()
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        placed = self.post("/api/selection/boards/place")
        self.post("/api/selection/pattern/save")
        pattern_id = self.admin()["patterns"][0]["id"]

        self.post("/api/selection/boards/clear")
        self.assertEqual(self.get()["boards"]["lower"], [])

        state = self.post("/api/selection/pattern/load", {"id": pattern_id})
        self.assertEqual(state["boards"], placed["boards"])
        self.assertTrue(state["plans"]["placed"])
        self.assertEqual(state["plans"], placed["plans"])
        self.assertIn("配置方式: 通常", state["message"])

    def test_無いパターンは422(self) -> None:
        self.sizes()
        self.post("/api/selection/pattern/load", {"id": 99999}, expect=422)

    # -- 削除 (VBA `frmPatterns.btnDelete_Click`) ----------------------
    def saved(self) -> int:
        """保存済みの実績を1件作って、その番号を返す。"""
        self.login()
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")
        self.post("/api/selection/pattern/save")
        return self.admin()["patterns"][0]["id"]

    def test_実績を削除できる(self) -> None:
        """VBAにはあったが移植されていなかった(`delete_pattern_by_id` は
        用意されていて、呼ぶ所が無かった)。"""
        pattern_id = self.saved()
        body = self.post("/api/selection/pattern/delete", {"id": pattern_id})
        self.assertIn("削除しました", body["message"])
        self.assertFalse(self.admin()["patterns"])

    def test_認証しないと削除できない(self) -> None:
        """**保存と同じ扱い。** 実績は端末をまたいで共有するもので、
        消えたことに気づけるのは次に使おうとした人だけ。
        """
        pattern_id = self.saved()
        self.session().admin = False
        body = self.post("/api/selection/pattern/delete",
                         {"id": pattern_id}, expect=403)
        self.assertIn("管理者認証", body["message"])
        # 消えていない
        self.assertTrue(self.admin()["patterns"])

    def test_無いパターンの削除は422(self) -> None:
        self.login()
        self.post("/api/selection/pattern/delete", {"id": 99999}, expect=422)

    def test_番号が無ければ400(self) -> None:
        self.login()
        self.post("/api/selection/pattern/delete", {}, expect=400)

    def test_削除は認証したときだけ画面に出す(self) -> None:
        """押せないボタンを並べても操作が増えるだけ。保存と同じ条件。"""
        from pathlib import Path
        # 実績パターンの段は `selection_admin.js` が持つ(段ごとに完結)
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "views" / "selection_admin.js").read_text(encoding="utf-8")
        head = js[js.index("admin.patterns.map"):]
        body = head[:head.index("}));")]
        self.assertIn("deletePattern", body)
        # **`can_save` ではない。** あちらは「認証してある**かつ**配置して
        # ある」で、消すのに配置は要らない(消したい実績は、たいてい今の
        # 作業とは別物)
        self.assertIn("admin.authenticated", body)
        self.assertNotIn("admin.can_save", body)


# ==================================================================
# 1P0113 裸梱包 (Phase 6d)
# ==================================================================
class Mode1P0113Tests(BoardTestCase):
    def p1(self, state=None) -> dict:
        return (state or self.get())["p1"]

    def test_通常はパネルを出さない(self) -> None:
        p1 = self.p1()
        self.assertFalse(p1["on"])
        self.assertFalse(p1["forced"])
        self.assertEqual(p1["materials"], [])

    def test_強制すると裸梱包になる(self) -> None:
        state = self.post("/api/selection/1p0113/force")
        self.assertTrue(state["p1"]["on"])
        self.assertTrue(state["p1"]["forced"])
        self.assertEqual(state["banner"]["kind"], "1p0113")
        # 並びは 松板 → 角材(VBA と同じ。寸法の確認が上)
        self.assertEqual([m["key"] for m in state["p1"]["materials"]],
                         ["mat", "kak"])

        state = self.post("/api/selection/1p0113/force")
        self.assertFalse(state["p1"]["on"])

    def test_資材が引けないと理由を出す(self) -> None:
        state = self.post("/api/selection/1p0113/force")
        self.assertFalse(state["p1"]["ready"])
        self.assertIn("マスタ", state["p1"]["why"])

    def test_数量は1から10(self) -> None:
        self.post("/api/selection/1p0113/force")
        self.assertEqual(self.p1()["qty"], 1)

        state = self.post("/api/selection/1p0113/qty", {"qty": 4})
        self.assertEqual(state["p1"]["qty"], 4)
        # 本数・枚数にそのまま掛かる
        self.assertTrue(any("枚" in m["count_caption"]
                            for m in state["p1"]["materials"]))

        self.post("/api/selection/1p0113/qty", {"qty": 0}, expect=400)
        self.post("/api/selection/1p0113/qty", {"qty": 11}, expect=400)
        self.post("/api/selection/1p0113/qty", {"qty": "いち"}, expect=400)

    def test_裸梱包でもパレットは要らない(self) -> None:
        """VBA も `If Not Me.Is1P0113Mode Then` でパレット検証を飛ばす。"""
        from packaging_tool.presenters import outputs
        self.post("/api/selection/1p0113/force")
        session = self.session()
        refusal = outputs.send_refusal(session)
        # ロットが無いのが理由で、パレットが理由ではない
        self.assertIsNotNone(refusal)
        self.assertNotIn("パレット", refusal.message)


# ==================================================================
# 出す — 帳票と倉庫送信 (Phase 6d)
# ==================================================================
class OutputTests(BoardTestCase):
    def outputs(self, state=None) -> dict:
        return (state or self.get())["outputs"]

    def test_ロットが無ければどれも押せない(self) -> None:
        outputs = self.outputs()
        self.assertFalse(any(r["can"] for r in outputs["reports"]))
        self.assertFalse(outputs["can_send"])
        for report in outputs["reports"]:
            self.assertIn("ロット", report["why"])

    def test_帳票は出せないときもHTMLを返す(self) -> None:
        """別窓の先が真っ白やJSONの生文字列だと、壊れたとしか見えない。"""
        res = self.client.get("/report/label", headers=self.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.mimetype, "text/html")
        self.assertIn("ロット", res.get_data(as_text=True))

    def test_知らない帳票は404(self) -> None:
        res = self.client.get("/report/nonsense", headers=self.auth())
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.mimetype, "text/html")

    def test_行を押した時点で決まる(self) -> None:
        """**行を押すこと自体が「このパレットにする」という意思。**

        以前は「行を選ぶ」「適用」「セット」の3回で、しかも順番を
        間違えると断られていた。
        """
        state = self.post("/api/selection/pallet/pick",
                          {"width": 1100, "length": 2000, "symbol": ""})
        self.assertEqual((state["pallet_width"], state["pallet_length"]),
                         ("1100", "2000"))
        self.assertTrue(state["pallet_set"], "押しただけでは決まっていません")
        self.assertIsNotNone(self.session().pallet_row)

    def test_行を押すと製品も確定する(self) -> None:
        """ロットから製品サイズが来ていれば、載るかどうかまで確かめる。"""
        from packaging_tool import work_context
        context = work_context.get_context()
        context.product_width, context.product_length = 1000, 1800

        state = self.post("/api/selection/pallet/pick",
                          {"width": 1100, "length": 2000, "symbol": ""})
        self.assertTrue(state["product_set"])

    def test_載らないパレットを押しても断らない(self) -> None:
        """パレットを選べたこと自体は成り立つ。**理由は製品の段が出す。**

        ここで断ると、選び直すこともできなくなる。
        """
        from packaging_tool import work_context
        context = work_context.get_context()
        context.product_width, context.product_length = 5000, 5000

        state = self.post("/api/selection/pallet/pick",
                          {"width": 1100, "length": 2000, "symbol": ""})
        self.assertTrue(state["pallet_set"])
        self.assertFalse(state["product_set"])

    def test_一覧に無い行は選べない(self) -> None:
        self.post("/api/selection/pallet/pick",
                  {"width": 9999, "length": 9999, "symbol": ""}, expect=400)

    def test_寸法が変わったら選択行は外れる(self) -> None:
        """持ち越すと**別の寸法の発注コード**を倉庫へ送ることになる。"""
        self.post("/api/selection/pallet/pick",
                  {"width": 1100, "length": 2000, "symbol": ""})
        self.assertIsNotNone(self.session().pallet_row)

        self.post("/api/selection/pallet/apply", {"width": "900", "length": "900"})
        self.assertIsNone(self.session().pallet_row)


@unittest.skipUnless(HAS_WEB, _SKIP)
class SendTests(SelectionWebTestCase):
    """ロットを引いたうえでの倉庫送信と帳票。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from app.routes import warehouse as wh_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        for module in (lot_routes, wh_routes):
            original = module.get_db
            module.get_db = lambda: self.conn
            self.addCleanup(
                lambda m=module, o=original: setattr(m, "get_db", o))

        insert_pallet(self.conn, width=1100, length=2000, code="P9", unit="台")
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn)
        self.client.get("/api/lot/1234567", headers=self.auth())

    def test_パレット行を選ばないと送れない(self) -> None:
        """発注コードは一覧の行にしか無い。

        「必須項目が空です」より具体的に案内する(VBA踏襲)。
        """
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        outputs = self.get()["outputs"]
        self.assertFalse(outputs["can_send"])
        self.assertIn("一覧から", outputs["send_why"])

        body = self.post("/api/selection/send", expect=422)
        self.assertIn("一覧から", body["message"])

    def test_行を選べば送れる(self) -> None:
        self.post("/api/selection/pallet/pick",
                  {"width": 1100, "length": 2000, "symbol": ""})
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.assertTrue(self.get()["outputs"]["can_send"])

        state = self.post("/api/selection/send")
        self.assertEqual(state["sent"], 1)
        self.assertEqual(state["next_url"], "/warehouse")

    def test_ここでは登録しない(self) -> None:
        """発注は取り消しに人手が要る。送る前に画面で見せる。"""
        self.post("/api/selection/pallet/pick",
                  {"width": 1100, "length": 2000, "symbol": ""})
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/send")

        orders = work_context.get_context().pending_orders
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["hatchu_code"], "P9")
        # 発注一覧にはまだ何も入っていない
        self.assertEqual(self.get("/api/warehouse/orders")["found"], 0)

    def test_倉庫画面が受け取ると手放す(self) -> None:
        """残っていると、開き直すたびに同じ発注を出せてしまう。"""
        self.post("/api/selection/pallet/pick",
                  {"width": 1100, "length": 2000, "symbol": ""})
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/send")

        html = self.client.get("/warehouse").get_data(as_text=True)
        self.assertIn("届いています", html)
        self.assertEqual(work_context.get_context().pending_orders, [])
        # 2回目は下書きが出ない
        self.assertNotIn("届いています",
                         self.client.get("/warehouse").get_data(as_text=True))

    def test_ロットが変わったら下書きは捨てる(self) -> None:
        """前のロットの発注を次のロットで登録させない。"""
        self.post("/api/selection/pallet/pick",
                  {"width": 1100, "length": 2000, "symbol": ""})
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/send")
        self.assertTrue(work_context.get_context().pending_orders)

        work_context.get_context().clear()
        self.assertEqual(work_context.get_context().pending_orders, [])

    def test_Lot印刷はHTMLをそのまま返す(self) -> None:
        """一時ファイルに書いて開き、あとで消す後始末が要らない(§6.3)。"""
        res = self.client.get("/report/label", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "text/html")
        body = res.get_data(as_text=True)
        self.assertIn("1234567", body)
        self.assertIn("<style>", body)          # 印刷用CSSごと入っている

    def _cut_scenario(self) -> None:
        """カット前提の板を実際に切る配置(上用=製品幅−20、下用=パレット幅で切る)。

        1030×1520 を3枚、丈方向に 1030 ずつ並べる。3枚目が
        製品丈 2502 / パレット丈 2650 をはみ出すので丈カットもある。
        """
        self.post("/api/selection/pallet/apply",
                  {"width": "1150", "length": "2650"})
        self.post("/api/selection/product/apply",
                  {"width": "1122", "length": "2502"})
        insert_board(self.conn, width=1030, length=1520)
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")

    def test_カットが無ければ切断依頼は要らないと言う(self) -> None:
        """失敗ではない。押した人に「不要だった」と分かる必要がある。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        insert_board(self.conn, width=1000, length=1800)
        self.post("/api/selection/boards/add",
                  {"category": "lower", "width": 1000, "length": 1800, "count": 1})
        self.post("/api/selection/boards/place")

        res = self.client.get("/report/cut-request", headers=self.auth())
        self.assertEqual(res.status_code, 422)
        self.assertIn("カット前提のボードがないため、切断依頼は不要です。",
                      res.get_data(as_text=True))
        # 訊かずに押させる(押すとすぐ「不要」と分かる)
        self.assertEqual(self._ask(), {})

    def test_配置していなければ切断依頼は出せない(self) -> None:
        """切断依頼は実際の配置から集計する(VBA `btnCutRequest_Click`)。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1150", "length": "2650"})
        self.post("/api/selection/product/apply",
                  {"width": "1122", "length": "2502"})
        insert_board(self.conn, width=1030, length=1520)
        self.post("/api/selection/boards/auto-select")

        report = next(r for r in self.get()["outputs"]["reports"]
                      if r["key"] == "cut-request")
        self.assertFalse(report["can"])
        self.assertEqual(report["why"], "先に配置を実行してください。")
        res = self.client.get("/report/cut-request", headers=self.auth())
        self.assertEqual(res.status_code, 422)

    def test_カット前提ではないはみ出しは対象外としてログに出す(self) -> None:
        """PASS3 で許したはみ出しは切断依頼に載せない(課題表 4)。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        insert_board(self.conn, width=1200, length=1900)
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")
        self.assertEqual(self.session().selected.lower[0].tag, "主")

        res = self.client.get("/report/cut-request", headers=self.auth())
        self.assertEqual(res.status_code, 422)
        texts = [e.text for e in self.session().presenter.user_log.entries]
        self.assertTrue(any(t.startswith("[切断依頼対象外] 下用 1200x1900 1枚: 幅はみ出し100mm")
                            for t in texts), texts[-5:])
        self.assertIn("[切断依頼] カット前提のボードがないため、切断依頼は不要です", texts)

    # --- 紙面で直す(VBAはシートを直してから印刷できた) -----------
    def _cut_sheet(self) -> str:
        self._cut_scenario()
        res = self.client.get("/report/cut-request", headers=self.auth())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:200])
        return res.get_data(as_text=True)

    def test_帳票は紙面で直せる(self) -> None:
        """VBA版は帳票がExcelシートで出ていたので、気に入らなければ

        シートを直してから印刷できた。台数が決まらない・寸法を微調整
        したい・拠点名を頭に入れたい・期日を書きたい ── どれも紙に
        出す前に人が決めることで、選定の計算とは別物(現場の声)。
        """
        html = self._cut_sheet()
        for key in ("title_prefix", "tantou", "total_packages",
                    "due_date", "cut0_w_size", "cut0_w_count"):
            with self.subTest(key=key):
                self.assertIn(f'data-edit="{key}"', html)
        self.assertIn("/api/selection/report/cut-request/edits", html)

    def test_見出しに拠点名を付けない(self) -> None:
        """現場の指示で拠点名の自動付与をやめた。要るときだけ人が入れる。"""
        html = self._cut_sheet()
        self.assertIn("ハードボード切断依頼書", html)
        self.assertNotIn("L-1 ハードボード切断依頼書", html)

    def test_直した内容は次に開いても残る(self) -> None:
        self._cut_sheet()
        self.post("/api/selection/report/cut-request/edits",
                  {"edits": {"title_prefix": "L-1", "tantou": "山田",
                             "total_packages": "3"}})
        html = self.client.get("/report/cut-request",
                               headers=self.auth()).get_data(as_text=True)
        self.assertIn('data-edit="title_prefix" data-placeholder="拠点">L-1</span>', html)
        self.assertIn('data-edit="tantou" data-placeholder="—">山田</span>', html)
        self.assertIn('data-edit="total_packages">3</span>', html)

    def test_別のロットを引いたら消える(self) -> None:
        """**別のロットの帳票に前のロットの書き込みが残るのが困る。**"""
        self._cut_sheet()
        self.post("/api/selection/report/cut-request/edits",
                  {"edits": {"tantou": "山田"}})
        self.assertEqual(self.session().edits_for("cut-request"),
                         {"tantou": "山田"})
        self.session().clear_for_new_lot()
        self.assertEqual(self.session().edits_for("cut-request"), {})

    def test_知らない帳票の保存は404(self) -> None:
        self.post("/api/selection/report/whatever/edits",
                  {"edits": {"a": "b"}}, expect=404)

    def test_直した内容が読めなければ400(self) -> None:
        self.post("/api/selection/report/cut-request/edits",
                  {"edits": "こわれている"}, expect=400)

    def test_Lot貼付用も紙面で直せる(self) -> None:
        res = self.client.get("/report/label", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        for key in ("pkg", "deliv", "factory", "unit", "code"):
            with self.subTest(key=key):
                self.assertIn(f'data-edit="{key}"', html)
        self.assertIn("/api/selection/report/label/edits", html)

    def test_カットがあれば切断依頼が出る(self) -> None:
        """成功経路。断り側しか見ていないと、出せなくなっても気づけない。

        (tkinter版と突き合わせていた `test_outputs_parity.py` が
         見ていた経路。tkinter版の撤去にあわせてここへ移した)
        """
        self._cut_scenario()

        res = self.client.get("/report/cut-request", headers=self.auth())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        self.assertEqual(res.mimetype, "text/html")
        self.assertIn("1234567", res.get_data(as_text=True))

    def test_切断依頼の丈カットなし指定はクエリで通る(self) -> None:
        """`?use_len_cut=0`(VBA『丈カットを行いますか』のいいえ)を受け付ける。

        丈カットなし指定では丈を切らず、全部が「幅カットのみ」になる。
        具体的な計算は `reports.get_cut_size_info` のユニットテストで
        検証済み。ここでは配線(クエリ→帳票生成)だけ確かめる。
        """
        self._cut_scenario()

        yes = self.client.get("/report/cut-request", headers=self.auth())
        no = self.client.get("/report/cut-request?use_len_cut=0", headers=self.auth())
        self.assertEqual(no.status_code, 200, no.get_data(as_text=True)[:300])
        # 丈カットあり: 3枚目(2060〜3090)が製品丈 2502 を 588 はみ出す → 1030−588=442
        self.assertIn("1102x442", yes.get_data(as_text=True))
        self.assertNotIn("1102x442", no.get_data(as_text=True))

    def _ask(self) -> dict:
        """切断依頼を押す前に訊くこと。空なら訊かない。"""
        report = next(r for r in self.get()["outputs"]["reports"]
                      if r["key"] == "cut-request")
        return report["ask"]

    def test_通常モードは切る板があれば訊く(self) -> None:
        """切る板(カット前提で実際に切る板)があれば訊く。無ければ訊かない
        (`test_カットが無ければ切断依頼は要らないと言う`)。"""
        self._cut_scenario()
        self.assertIn("丈カットを行いますか", self._ask()["title"])

    def test_訊くときは選択肢を3つ出す(self) -> None:
        """現場の声:「キャンセルを選ぶと普通にキャンセルになる。

        そもそも本当にキャンセルしたい時どうするんだという話になる」。
        OK/キャンセルの2択だと「キャンセル=丈カットなし」に割り当てる
        しかなく、**やめるための行き先が無い**。3つに分ける。
        """
        self._cut_scenario()

        ask = self._ask()
        self.assertEqual([c["key"] for c in ask["choices"]],
                         ["yes", "no", "cancel"])
        self.assertEqual([c["label"] for c in ask["choices"]],
                         ["丈カットあり", "丈カットなし", "キャンセル"])
        # どれを選ぶと何が起きるかを、選ぶ前に読める
        for choice in ask["choices"]:
            self.assertTrue(choice["note"], choice)

    def test_プロテックで切る余地が無ければ訊かない(self) -> None:
        """**訊く意味があるのは、切るか切らないかを選べるときだけ。**

        丈カットそのものが無いのに訊くと、答えを使う先が無い問いを
        毎回押させることになる。
        """
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        insert_board(self.conn, width=1000, length=900,
                     board_type="プロテックボード")
        self.post("/api/selection/board-type",
                  {"board_type": "プロテックボード"})
        self.session().presenter.protec.is_protec = True
        self.post("/api/selection/boards/auto-select")

        result = self.session().select_result.lower_result.protec_result
        self.assertTrue(result.valid)
        self.assertFalse(result.need_length_cut)
        self.assertFalse(result.len_cut_optional)
        self.assertEqual(self._ask(), {})

    def test_プロテックで切る余地があれば訊く(self) -> None:
        """製品丈は超えるがパレット丈には収まる ── 現場が選べる。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        insert_board(self.conn, width=1000, length=1000,
                     board_type="プロテックボード")
        self.post("/api/selection/board-type",
                  {"board_type": "プロテックボード"})
        self.session().presenter.protec.is_protec = True
        self.post("/api/selection/boards/auto-select")

        result = self.session().select_result.lower_result.protec_result
        self.assertTrue(result.len_cut_optional)
        self.assertFalse(result.need_length_cut, "図にカット線は出さない")
        self.assertIn("丈カットを行いますか", self._ask()["title"])

    def test_配置していなければ配置図印刷は出せない(self) -> None:
        """候補を選んだだけでは足りない。配置してあることが前提。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        insert_board(self.conn, width=1100, length=2000)
        self.post("/api/selection/boards/add",
                  {"category": "lower", "width": 1100, "length": 2000, "count": 1})

        res = self.client.get("/report/plan", headers=self.auth())
        self.assertEqual(res.status_code, 422)
        self.assertIn("配置してください", res.get_data(as_text=True))

    def test_配置図印刷は図と選定一覧を含むHTMLを返す(self) -> None:
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        insert_board(self.conn, width=1100, length=2000)
        insert_board(self.conn, width=550, length=1000)
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")

        res = self.client.get("/report/plan", headers=self.auth())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        self.assertEqual(res.mimetype, "text/html")
        body = res.get_data(as_text=True)
        self.assertIn("1234567", body)
        self.assertIn("svgplan.js", body)
        self.assertIn("下用", body)

    def _placed(self) -> None:
        """パレット・製品を決めてボードを1枚置くところまで。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        insert_board(self.conn, width=1100, length=2000)
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")

    def test_配置しただけでは使用実績は積まれない(self) -> None:
        """置いてみただけの試しまで数えると、実際の使用実態とずれる。"""
        self._placed()
        self.assertEqual(self.get()["admin"]["usage"], [])

    def test_配置図の印刷では使用実績は積まれない(self) -> None:
        """**印刷は「使った」の合図ではない。**

        確かめるために出すこともあれば、出さずに使うこともある。
        以前はここで積んでいたが、押した人の意図と一致しなかった
        (現場の指摘:「何をもって使用なのか決めていない」)。
        """
        self._placed()
        self.client.get("/report/plan", headers=self.auth())
        self.assertEqual(self.get()["admin"]["usage"], [])

    def test_使用するを押すと積まれる(self) -> None:
        self._placed()
        self.assertTrue(self.get()["outputs"]["can_use"])

        self.post("/api/selection/boards/use")

        usage = self.get()["admin"]["usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["width"], 1100)
        self.assertEqual(usage[0]["length"], 2000)
        self.assertEqual(usage[0]["usage_count"], 1)

    def test_押したら共有へ送りにいく(self) -> None:
        """人気度は全端末の合計。手元に置いたままでは、ほかの端末に出ない。"""
        from unittest import mock
        from packaging_tool import data_sync
        self._placed()
        with mock.patch.object(data_sync, "write_back_in_background") as push:
            self.post("/api/selection/boards/use")
            self.post("/api/selection/boards/use")       # 二度目は積まないので送らない
        self.assertEqual(push.call_count, 1)

    def test_同じ配置を二度押しても増えない(self) -> None:
        """**押した手応えが無いと人はもう一度押す。**

        そのたびに増えると、実績が実態より多くなる。増やさないことと、
        増やさなかったと言うことの両方をする。
        """
        self._placed()
        self.post("/api/selection/boards/use")
        self.post("/api/selection/boards/use")

        usage = self.get()["admin"]["usage"]
        self.assertEqual(usage[0]["usage_count"], 1)
        outputs = self.get()["outputs"]
        self.assertFalse(outputs["can_use"])
        self.assertTrue(outputs["use_done"])
        self.assertIn("もう記録して", outputs["use_why"])

    def test_置き直せばまた押せる(self) -> None:
        """候補を替えて置き直したら、使ったのは**置き直したあと**のほう。

        ロットとパレットだけで見分けると「もう積んである」と断って
        しまい、実際に使ったものが記録されない。
        """
        self._placed()
        self.post("/api/selection/boards/use")
        self.assertFalse(self.get()["outputs"]["can_use"])

        insert_board(self.conn, width=550, length=2000)
        self.post("/api/selection/boards/clear")
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")

        self.assertTrue(self.get()["outputs"]["can_use"])

    def test_配置していなければ押せない(self) -> None:
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        outputs = self.get()["outputs"]
        self.assertFalse(outputs["can_use"])
        self.assertIn("配置", outputs["use_why"])

        # 断りは業務としての断り(422)。**押せてしまわないこと**と
        # **理由が返ること**の両方を見る
        body = self.post("/api/selection/boards/use", expect=422)
        self.assertIn("配置", body["message"])

    def test_カットして使っても棚から取った1枚として積む(self) -> None:
        """**消費したのはカット前の1枚。**

        カット後の寸法で積むと、ボード一覧に載っていない寸法ばかりが
        並び、何を何枚持っておけばよいのかが読めなくなる。カット後の
        寸法は別の列に添える。
        """
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1900"})
        insert_board(self.conn, width=1100, length=2000)
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/place")
        self.post("/api/selection/boards/use")

        rows = self.conn.execute(
            "SELECT * FROM ボード使用実績").fetchall()
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(row=dict(row)):
                # 積むのはボード一覧にある寸法。カット後は別の列
                self.assertEqual((row["ボード幅"], row["ボード丈"]), (1100, 2000))
                if row["切断後幅"]:
                    self.assertNotEqual(
                        (row["切断後幅"], row["切断後丈"]), (1100, 2000))

    def test_製品とパレットの寸法も一緒に残る(self) -> None:
        """ボードの寸法だけでは、なぜそのサイズが多いのか説明できない。"""
        self._placed()
        self.post("/api/selection/boards/use")

        row = self.conn.execute(
            "SELECT * FROM ボード使用実績").fetchone()
        self.assertEqual((row["製品幅"], row["製品丈"]), (1000, 1800))
        self.assertEqual((row["パレット幅"], row["パレット丈"]), (1100, 2000))
        self.assertTrue(row["使用日時"])

    def test_1P0113の倉庫送信は角材と松板の2行になる(self) -> None:
        """パレットを使わない裸梱包。行数と品名が通常モードと違う。

        (同じく `test_outputs_parity.py` から移した経路)
        """
        from packaging_tool import special_packaging as spk

        for name, atsu, haba, code in (
            (spk.KAKUZAI_NAME, spk.KAKUZAI_ATSU, spk.KAKUZAI_HABA, "K001"),
            (spk.MATSUITA_NAME, spk.MATSUITA_ATSU, spk.MATSUITA_HABA, "M001"),
        ):
            self.conn.execute(
                "INSERT INTO 松板角材 (品名, 厚, 幅, 丈min, 丈max, コード, 単位, 備考)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (name, atsu, haba, 1, 9999, code, "本", ""))
        self.conn.commit()

        session = self.session()
        session.presenter.force_1p0113 = True
        session.presenter.apply_1p0113_mode("", product_width=1000,
                                            product_length=1800)
        work_context.get_context().product_width = 1000
        work_context.get_context().product_length = 1800
        session.reload_1p0113_materials()
        self.assertTrue(session.presenter.mode_1p0113)

        self.post("/api/selection/send")
        orders = work_context.get_context().pending_orders
        self.assertEqual(len(orders), 2, orders)
        self.assertEqual([o["hatchu_code"] for o in orders], ["K001", "M001"])
        self.assertTrue(orders[0]["hinmei"].startswith(spk.KAKUZAI_SIZE_LABEL))
        self.assertTrue(orders[1]["hinmei"].startswith(spk.MATSUITA_SIZE_LABEL))


class WarehouseDraftOnDecideTests(SelectionWebTestCase):
    """パレット決定の時点で倉庫連携の下書きを先に用意する(現場の要望)。

    以前は「倉庫送信」を明示的に押すまで倉庫連携の入力欄は空のままで、
    ボードを選び終えたあとで初めてパレット・Lot情報を打ち直すことに
    なっていた。パレットはこの時点で決まっているのだから、先に埋めて
    おいてよい ── ただし**登録(送信)まではしない**。
    """

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        # 発注一覧もこの試験の接続から読む(開発機の手元DBの中身で結果を変えない)
        from app.routes import warehouse as warehouse_routes
        _web.bind_db(self, warehouse_routes, self.conn)

        insert_pallet(self.conn, width=1100, length=2000, code="P9", unit="台")
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn)
        self.client.get("/api/lot/1234567", headers=self.auth())

    def test_パレット決定だけで下書きが埋まる(self) -> None:
        """「倉庫送信」を押していなくても、決めた時点で下書きができる。"""
        self.assertEqual(work_context.get_context().pending_orders, [])
        self.post("/api/selection/pallet/pick",
                  {"width": 1100, "length": 2000, "symbol": ""})
        orders = work_context.get_context().pending_orders
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["hatchu_code"], "P9")

    def test_まだ登録はしない(self) -> None:
        """下書きを用意するだけで、発注一覧にはまだ入らない。"""
        self.post("/api/selection/pallet/pick",
                  {"width": 1100, "length": 2000, "symbol": ""})
        self.assertEqual(self.get("/api/warehouse/orders")["found"], 0)

    def test_一覧に無い寸法を適用しただけでは前提が無いので静かに諦める(self) -> None:
        """一覧の行を選んでいない(発注コードが無い)場合は下書きを作らない。

        `pallet/apply`(旧「セット」に相当)だけでは発注コードが
        分からないため、`_prepare_warehouse_draft` は
        `outputs.send_refusal` に断られて何もしない。
        """
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.assertEqual(work_context.get_context().pending_orders, [])


class ReasonTests(unittest.TestCase):
    """断りの種類はサービスが付ける。文言から推し量らない。"""

    def test_数値でないのは入力の形(self) -> None:
        result, _ = svc.apply_pallet_size("あ", "1100")
        self.assertEqual(result.reason, svc.REFUSE_BAD_INPUT)

    def test_収まらないのは業務の断り(self) -> None:
        palette = svc.apply_pallet_size("1100", "1100")[1]
        result, _, _ = svc.apply_product_size("9000", "9000", palette)
        self.assertEqual(result.reason, svc.REFUSE_BUSINESS)

    def test_パレット未設定も業務の断り(self) -> None:
        result, _, _ = svc.apply_product_size("100", "100", svc.Palette())
        self.assertEqual(result.reason, svc.REFUSE_BUSINESS)

    def test_通ったときは種別を持たない(self) -> None:
        result, _ = svc.apply_pallet_size("1100", "1100")
        self.assertEqual(result.reason, "")


class ManualAddPlacementTests(SelectionWebTestCase):
    """**手で追加したのに何も起きない、をやめる。**

    現場の指摘:「手動でボードを選択して上用へ追加 下用へ追加 とあるのに
    追加しても配置すらしない この仕様であればこのボタンはいらないのでは」。

    置けない寸法を足せば置けないのは当たり前だが、**そう言っていなかった**
    のが問題だった。選定ログにも画面にも何も出ず、押した人からはボタンが
    壊れているようにしか見えない。
    """

    def setUp(self) -> None:
        super().setUp()
        # 手動追加は**候補一覧にある寸法しか受け付けない**ので、
        # 試したい寸法を在庫に入れておく
        for width, length in ((1250, 1250), (450, 1520), (540, 900)):
            self.conn.execute(
                "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ)"
                " VALUES (?, ?, 'ハードボード')", (width, length))
        self.conn.commit()
        self.post("/api/selection/pallet/apply",
                  {"width": "550", "length": "985"})
        self.post("/api/selection/product/apply",
                  {"width": "482", "length": "967"})

    def add(self, side: str, width: int, length: int):
        return self.post("/api/selection/boards/add",
                         {"category": side, "width": str(width),
                          "length": str(length), "count": "1"})

    def place(self):
        return self.client.post("/api/selection/boards/place", json={},
                                headers=self.auth())

    def notes_of(self, res) -> str:
        body = res.get_json()
        notes = body.get("notes") or (body.get("error") or {}).get("notes") or []
        return " / ".join(notes)

    def test_置けない寸法を足したら理由が返る(self) -> None:
        """現場のログにあった 450x1520(パレット丈985)。"""
        self.add("lower", 450, 1520)
        text = self.notes_of(self.place())
        self.assertIn("450x1520", text)
        self.assertIn("丈がパレット丈", text)

    def test_1枚も置けなければ成功にしない(self) -> None:
        """「配置しました(0枚)」は、押した人には成功に見える。"""
        self.add("lower", 1250, 1250)
        self.assertEqual(self.place().status_code, 422)

    def test_置けたものがあれば成功のまま(self) -> None:
        """置けなかった分を言うために、置けた分まで失敗にはしない。

        どう置いても入らない 1250x1250 と、置ける 540x900 を混ぜる。
        (450x1520 は隣に板があるとスナップ探索で置けてしまうので、
         ここでは使わない ── 置けるものを「置けない」と言わせない)
        """
        self.add("lower", 540, 900)
        self.add("lower", 1250, 1250)
        res = self.place()
        self.assertEqual(res.status_code, 200)
        self.assertIn("1250x1250", self.notes_of(res))

    def test_選定ログにも残る(self) -> None:
        """あとから追えるようにする。画面の通知は消える。"""
        self.add("lower", 450, 1520)
        self.place()
        text = "\n".join(e.text for e in self.session().presenter.user_log.entries)
        self.assertIn("置けませんでした", text)
        self.assertIn("450x1520", text)



if __name__ == "__main__":                       # pragma: no cover
    unittest.main()


# ==================================================================
# 実績(スナップショット) ── VBA `btnSavePattern_Click` / `LoadSinglePattern`
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class SnapshotTests(AdminTests):
    """配置を承認した画面の状態を**そのまま**保存し、計算し直さずに戻す。"""

    def placed(self) -> dict:
        self.login()
        self.sizes()
        self.post("/api/selection/boards/auto-select")
        return self.post("/api/selection/boards/place")

    def header(self) -> dict:
        from packaging_tool import config
        row = self.conn.execute(
            f'SELECT * FROM "{config.TBL_PT_HEADER}" ORDER BY 実績ID DESC').fetchone()
        return dict(row)

    def test_ボード配置は配置方式_通常(self) -> None:
        self.placed()
        self.assertEqual(self.session().placement_method, "通常")
        self.post("/api/selection/pattern/save")
        self.assertEqual(self.header()["配置方式"], "通常")

    def test_保存の前に訊く文をサーバが渡す(self) -> None:
        self.placed()
        confirm = self.admin()["save_confirm"]
        self.assertIn("実績を保存", confirm["title"])
        self.assertIn("パレット: 1100 × 2000", confirm["body"])
        self.assertIn("配置方式: 通常", confirm["body"])

    def test_配置のあとで変わっていたら保存しない(self) -> None:
        """VBA:「配置後にパレット・製品サイズまたはボードが変更されています」。"""
        self.placed()
        # 配置を捨てずに中身だけ変わった(画面の外から変わった)ことにする
        self.session().selected.lower[0].count += 1
        self.assertIn("配置後に", self.admin()["save_why"])
        self.assertEqual(self.admin()["save_confirm"], {})
        body = self.post("/api/selection/pattern/save", expect=422)
        self.assertIn("配置し直して", body["message"])

    def test_ヘッダに画面の状態が入る(self) -> None:
        from packaging_tool import user_settings
        from unittest import mock
        self.placed()
        self.session().selected_angles = [1550, 1600]
        with mock.patch.object(user_settings, "get_position", return_value="A"):
            self.post("/api/selection/pattern/save")
        h = self.header()
        self.assertEqual((h["パレット幅"], h["パレット丈"], h["製品幅"], h["製品丈"]),
                         (1100, 2000, 1000, 1800))
        self.assertEqual(h["アングル"], "1550,1600")
        self.assertEqual(h["拠点"], "A")
        self.assertEqual(h["ボード種別"], self.session().board_type)

    def test_読込は計算し直さない(self) -> None:
        """保存したあとで配置の計算が変わっても、保存した座標のまま戻る。"""
        from packaging_tool import placement_algorithm as place
        from unittest import mock
        placed = self.placed()
        self.post("/api/selection/pattern/save")
        pid = self.admin()["patterns"][0]["id"]
        self.post("/api/selection/boards/clear")
        with mock.patch.object(place, "auto_place_boards",
                               side_effect=AssertionError("計算し直しています")):
            state = self.post("/api/selection/pattern/load", {"id": pid})
        self.assertEqual(state["plans"], placed["plans"])

    def test_ロットと食い違えば知らせる_上書きはしない(self) -> None:
        self.placed()
        session = self.session()
        session.presenter.last_hosozai = "ポリ"
        self.post("/api/selection/pattern/save")
        pid = self.admin()["patterns"][0]["id"]
        session.presenter.last_hosozai = "紙"
        state = self.post("/api/selection/pattern/load", {"id": pid})
        self.assertIn("保護材が異なります(保存時: ポリ / 現在: 紙)",
                      " ".join(state.get("notes", [])))
        self.assertEqual(session.presenter.last_hosozai, "紙")

    def test_別の寸法のパレット行は手放す(self) -> None:
        """古い行のままだと、別のパレットの発注コードで倉庫へ送れてしまう。"""
        from packaging_tool import config
        self.placed()
        self.post("/api/selection/pattern/save")
        pid = self.admin()["patterns"][0]["id"]
        self.conn.execute(f'UPDATE "{config.TBL_PT_HEADER}" SET パレット幅=1200')
        session = self.session()
        session.pallet_row = mock_row(1100, 2000)
        self.post("/api/selection/pattern/load", {"id": pid})
        self.assertIsNone(session.pallet_row)

    def test_読み込んだ実績は配置し直しても狭幅の前提を引き継ぐ(self) -> None:
        from packaging_tool import config
        self.placed()
        self.post("/api/selection/pattern/save")
        pid = self.admin()["patterns"][0]["id"]
        self.conn.execute(f'UPDATE "{config.TBL_PT_HEADER}" SET 狭幅下=1')
        self.post("/api/selection/pattern/load", {"id": pid})
        self.assertEqual(self.session().narrow_flags(), (True, False))
        # 手で足すと前提は捨てる(自動選定の結果と同じ扱い)
        self.post("/api/selection/boards/add",
                  {"category": "lower", "width": 500, "length": 1000, "count": 1})
        self.assertIsNone(self.session().restored)

    def test_カットは保存した値で切断依頼書に出る(self) -> None:
        self.placed()
        session = self.session()
        session.select_result.cut_info["500x1000"] = 450
        self.post("/api/selection/pattern/save")
        pid = self.admin()["patterns"][0]["id"]
        self.post("/api/selection/boards/clear")
        self.post("/api/selection/pattern/load", {"id": pid})
        self.assertEqual(session.cut_dicts()[0].get("500x1000"), 450)

    def test_保存_読込_削除は共有へ裏で送る(self) -> None:
        """送れなくても画面は止めない。送る入口は発注・受払と同じ。

        読込も送る ── 使用回数(+1)を共有へ足すため(VBA は共有を直接 +1)。
        """
        from unittest import mock
        self.placed()
        with mock.patch("packaging_tool.data_sync.write_back_in_background") as sent:
            self.post("/api/selection/pattern/save")
            pid = self.admin()["patterns"][0]["id"]
            self.post("/api/selection/pattern/load", {"id": pid})
            self.post("/api/selection/pattern/delete", {"id": pid})
        self.assertEqual(sent.call_count, 3)


def mock_row(width: int, length: int):
    from types import SimpleNamespace
    return SimpleNamespace(width=width, length=length, code="X", unit="枚")


@unittest.skipUnless(HAS_WEB, _SKIP)
class SnapshotTilingTests(ChangeCandidateTests):
    def test_候補変更は配置方式_別案(self) -> None:
        from packaging_tool import config
        self.post("/api/selection/auth", {"password": config.ADMIN_PASSWORD})
        self.sizes()
        self.change()
        self.assertEqual(self.session().placement_method, "別案A")
        self.post("/api/selection/pattern/save")
        row = self.conn.execute(
            f'SELECT 配置方式 FROM "{config.TBL_PT_HEADER}"').fetchone()
        self.assertEqual(row[0], "別案A")
        # 次の軸へ(同じ内容の軸は飛ばすので B とは限らない)
        self.change()
        self.assertIn(self.session().placement_method, ("別案B", "別案C"))
