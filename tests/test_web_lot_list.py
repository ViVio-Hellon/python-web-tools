"""仕掛一覧の画面とAPI

【何を守っているか】
- **どの操作でも一覧ぜんぶが返る**。断られたときも表が消えない
- **400 のときサーバの状態は動いていない**(設計書 §1 の3番)
- **選んだ行を決めるのはサーバ**。画面が独自に覚えない
- よく使う条件は保存できて、押すと**足される**(置き換えない)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402

_isolation.ensure_isolated()

from packaging_tool import config, lot_browse_session, work_context  # noqa: E402
from tests.test_lot_query import insert  # noqa: E402
from tests import _web  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ"
TOKEN = _web.TOKEN          # 土台と同じものを使う


@unittest.skipUnless(HAS_WEB, _SKIP)
class ListWebTestCase(unittest.TestCase):

    def setUp(self) -> None:
        from app.routes import lot as routes

        lot_browse_session.reset_session()
        work_context.reset_context()
        self.addCleanup(lot_browse_session.reset_session)
        self.addCleanup(work_context.reset_context)

        # 保存フィルタは `user_settings` に書く。**本物を書き換えない**
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(
            config, "USER_CONFIG_PATH", Path(tmp.name) / "user_config.json")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.conn = _web.bind_db(self, routes)
        insert(self.conn, "1111111", zaishitsu="A5052", thickness=3.0)
        insert(self.conn, "2222222", zaishitsu="A1050", thickness=0.8)
        insert(self.conn, "3333333", zaishitsu="A5052", thickness=8.0)
        self.client = _web.make_client(port=8715, ready=False)

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}

    def get(self, path="/api/lot/list") -> dict:
        res = self.client.get(path, headers=self.auth())
        self.assertEqual(res.status_code, 200, path)
        return res.get_json()

    def post(self, path, body=None, expect=200) -> dict:
        res = self.client.post(path, json=body or {}, headers=self.auth())
        self.assertEqual(res.status_code, expect, f"{path}: {res.get_json()}")
        return res.get_json()

    def lots(self, body=None) -> list[str]:
        return [r["lot_no"] for r in (body or self.get())["rows"]]

    def session(self):
        return lot_browse_session.get_session()


class StateTests(ListWebTestCase):

    def test_一覧が出る(self) -> None:
        body = self.get()
        self.assertEqual(self.lots(body), ["1111111", "2222222", "3333333"])
        self.assertEqual([h["label"] for h in body["headers"]][:4],
                         ["ロット番号", "引当有無", "用途コード", "用途名"])
        self.assertEqual(body["count_note"], "3 件")

    def test_引当有無が一覧に出る(self) -> None:
        """同じロット番号が仕掛引当(SIKAHIKINOW)にあれば1、無ければ0。"""
        self.conn.execute(
            "INSERT INTO 仕掛引当 (ロット番号, 引当番号) VALUES ('2222222','60000001')")
        self.conn.commit()
        body = self.get()
        at = [h["key"] for h in body["headers"]].index("hiki")
        got = {r["lot_no"]: r["values"][at] for r in body["rows"]}
        self.assertEqual(got, {"1111111": "0", "2222222": "1", "3333333": "0"})

    def test_引当有無で絞り込める(self) -> None:
        self.conn.execute(
            "INSERT INTO 仕掛引当 (ロット番号, 引当番号) VALUES ('2222222','60000001')")
        self.conn.commit()
        body = self.post("/api/lot/list/filter/add",
                         {"column": "hiki", "op": "=", "value": "1"})
        self.assertEqual(self.lots(body), ["2222222"])

    def test_数値の列は右寄せの印が付く(self) -> None:
        """寄せ方を決めるのもサーバ。画面が列名で判断すると分裂する。"""
        by = {h["key"]: h["numeric"] for h in self.get()["headers"]}
        self.assertTrue(by["thickness"])
        self.assertFalse(by["yoto_name"])

    def test_件数は切られたことが分かる言い方(self) -> None:
        body = self.post("/api/lot/list/page-size", {"size": 50})
        self.assertEqual(body["count_note"], "3 件")
        # 3件しかないので切られない。切られる側は lot_query の試験で見る


class FilterTests(ListWebTestCase):

    def test_条件を足すと絞られる(self) -> None:
        body = self.post("/api/lot/list/filter/add",
                         {"column": "zaishitsu", "op": "=", "value": "A5052"})
        self.assertEqual(self.lots(body), ["1111111", "3333333"])
        self.assertEqual([c["label"] for c in body["conditions"]],
                         ["製造材質 = A5052"])

    def test_一覧に無い列は400で状態も動かない(self) -> None:
        before = list(self.session().conditions)
        res = self.client.post("/api/lot/list/filter/add",
                               json={"column": "秘密", "op": "=", "value": "x"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.session().conditions, before)

    def test_数値でない値は400(self) -> None:
        res = self.client.post("/api/lot/list/filter/add",
                               json={"column": "thickness", "op": "=",
                                     "value": "あ"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)
        self.assertIn("数値", res.get_json()["error"]["message"])

    def test_同じ条件は増えない(self) -> None:
        for _ in range(2):
            body = self.post("/api/lot/list/filter/add",
                             {"column": "zaishitsu", "op": "=", "value": "A5052"})
        self.assertEqual(len(body["conditions"]), 1)

    def test_条件を外す(self) -> None:
        self.post("/api/lot/list/filter/add",
                  {"column": "zaishitsu", "op": "=", "value": "A5052"})
        body = self.post("/api/lot/list/filter/remove", {"index": 0})
        self.assertEqual(body["conditions"], [])
        self.assertEqual(len(body["rows"]), 3)

    def test_もう無い条件を外そうとしたら断る(self) -> None:
        """黙って別の条件を外すと、外したつもりのないものが外れる。"""
        res = self.client.post("/api/lot/list/filter/remove", json={"index": 5},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)

    def test_全解除は検索語も消す(self) -> None:
        self.post("/api/lot/list/filter/add",
                  {"column": "zaishitsu", "op": "=", "value": "A5052"})
        self.post("/api/lot/list/search", {"text": "111"})
        body = self.post("/api/lot/list/filter/clear")
        self.assertEqual(body["conditions"], [])
        self.assertEqual(body["text"], "")
        self.assertEqual(len(body["rows"]), 3)

    def test_一覧検索(self) -> None:
        body = self.post("/api/lot/list/search", {"text": "A1050"})
        self.assertEqual(self.lots(body), ["2222222"])

    def test_0件のときは何をすればよいか言う(self) -> None:
        body = self.post("/api/lot/list/search", {"text": "ありえない"})
        self.assertEqual(body["rows"], [])
        self.assertIn("ありえない", body["empty_why"])

    def test_条件つきで0件なら条件を外せと言う(self) -> None:
        self.post("/api/lot/list/filter/add",
                  {"column": "zaishitsu", "op": "=", "value": "A5052"})
        body = self.post("/api/lot/list/search", {"text": "A1050"})
        self.assertEqual(body["rows"], [])
        self.assertIn("条件", body["empty_why"])


class SortAndSizeTests(ListWebTestCase):

    def test_同じ列を押すと昇降が入れ替わる(self) -> None:
        body = self.post("/api/lot/list/sort", {"column": "thickness"})
        self.assertEqual(self.lots(body), ["2222222", "1111111", "3333333"])
        marks = {h["key"]: h["sorted"] for h in body["headers"]}
        self.assertEqual(marks["thickness"], "asc")

        body = self.post("/api/lot/list/sort", {"column": "thickness"})
        self.assertEqual(self.lots(body), ["3333333", "1111111", "2222222"])
        self.assertEqual(
            {h["key"]: h["sorted"] for h in body["headers"]}["thickness"], "desc")

    def test_別の列を押すと昇順から(self) -> None:
        self.post("/api/lot/list/sort", {"column": "thickness"})
        self.post("/api/lot/list/sort", {"column": "thickness"})   # desc
        body = self.post("/api/lot/list/sort", {"column": "lot_no"})
        self.assertEqual(
            {h["key"]: h["sorted"] for h in body["headers"]}["lot_no"], "asc")

    def test_一覧に無い列では並べ替えられない(self) -> None:
        res = self.client.post("/api/lot/list/sort", json={"column": "秘密"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)

    def test_表示件数は一覧から選ぶ(self) -> None:
        body = self.post("/api/lot/list/page-size", {"size": 100})
        self.assertEqual(body["page_size"], 100)
        res = self.client.post("/api/lot/list/page-size", json={"size": 7},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.session().page_size, 100)   # 動いていない


class SuggestTests(ListWebTestCase):

    def test_打った文字から条件を作る(self) -> None:
        body = self.get("/api/lot/list/suggest?q=A50")
        self.assertIn("製造材質 = A5052", [i["label"] for i in body["items"]])

    def test_候補はそのまま足せる形で返る(self) -> None:
        item = next(i for i in self.get("/api/lot/list/suggest?q=A50")["items"]
                    if i["kind"] == "condition")
        body = self.post("/api/lot/list/filter/add",
                         {"column": item["column"], "op": item["op"],
                          "value": item["value"]})
        self.assertEqual(len(body["conditions"]), 1)

    def test_状態は変えない(self) -> None:
        self.get("/api/lot/list/suggest?q=A50")
        self.assertEqual(self.session().conditions, [])


class SavedTests(ListWebTestCase):

    def add_one(self) -> None:
        self.post("/api/lot/list/filter/add",
                  {"column": "zaishitsu", "op": "=", "value": "A5052"})

    def test_保存して押すと足される(self) -> None:
        self.add_one()
        body = self.post("/api/lot/list/saved/save", {"name": "A5052だけ"})
        self.assertEqual(body["saved"], ["A5052だけ"])

        self.post("/api/lot/list/filter/clear")
        body = self.post("/api/lot/list/saved/load", {"name": "A5052だけ"})
        self.assertEqual([c["label"] for c in body["conditions"]],
                         ["製造材質 = A5052"])

    def test_足すのであって置き換えない(self) -> None:
        """置き換えにすると、いま絞ってある内容が黙って消える。"""
        self.add_one()
        self.post("/api/lot/list/saved/save", {"name": "A5052だけ"})
        self.post("/api/lot/list/filter/clear")
        self.post("/api/lot/list/filter/add",
                  {"column": "thickness", "op": "≥", "value": "5"})
        body = self.post("/api/lot/list/saved/load", {"name": "A5052だけ"})
        self.assertEqual(len(body["conditions"]), 2)
        self.assertEqual(self.lots(body), ["3333333"])

    def test_空は保存させない(self) -> None:
        """押しても何も起きない条件ができてしまう。"""
        body = self.get()
        self.assertFalse(body["can_save"])
        self.assertIn("条件を1つ以上", body["save_why"])
        res = self.client.post("/api/lot/list/saved/save", json={"name": "空"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)

    def test_名前が無いと保存させない(self) -> None:
        self.add_one()
        res = self.client.post("/api/lot/list/saved/save", json={"name": "  "},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)

    def test_消せる(self) -> None:
        self.add_one()
        self.post("/api/lot/list/saved/save", {"name": "A5052だけ"})
        body = self.post("/api/lot/list/saved/delete", {"name": "A5052だけ"})
        self.assertEqual(body["saved"], [])

    def test_無いものは押せない(self) -> None:
        res = self.client.post("/api/lot/list/saved/load", json={"name": "無い"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)

    def test_列の構成が変わっても落ちない(self) -> None:
        """保存したあとに列を減らすことはありうる。読めた分だけ足す。"""
        from packaging_tool import user_settings
        user_settings.save(lot_browse_session.KEY_SAVED, {
            "古い条件": [{"column": "もう無い列", "op": "=", "value": "x"},
                       {"column": "zaishitsu", "op": "=", "value": "A5052"}]})
        body = self.post("/api/lot/list/saved/load", {"name": "古い条件"})
        self.assertEqual([c["label"] for c in body["conditions"]],
                         ["製造材質 = A5052"])

    def test_全部復元できないなら断る(self) -> None:
        from packaging_tool import user_settings
        user_settings.save(lot_browse_session.KEY_SAVED, {
            "壊れた": [{"column": "もう無い列", "op": "=", "value": "x"}]})
        res = self.client.post("/api/lot/list/saved/load", json={"name": "壊れた"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)


class PickTests(ListWebTestCase):
    """行を押す = そのロットを引く。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as routes
        from tests.test_lot_service import insert_hiki, insert_odr
        insert_hiki(self.conn, lot_no="1111111")
        insert_odr(self.conn)
        self.routes = routes

    def test_選んだ行はサーバが決める(self) -> None:
        """画面が独自に覚えると、別のロットを引いても前の行が光ったまま。"""
        body = self.get()
        self.assertNotIn(True, [r["current"] for r in body["rows"]])

        res = self.client.get("/api/lot/1111111", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        listed = res.get_json()["list"]
        self.assertEqual([r["lot_no"] for r in listed["rows"] if r["current"]],
                         ["1111111"])

    def test_引き直すと光る行も移る(self) -> None:
        self.client.get("/api/lot/1111111", headers=self.auth())
        res = self.client.get("/api/lot/3333333", headers=self.auth())
        listed = res.get_json()["list"]
        self.assertEqual([r["lot_no"] for r in listed["rows"] if r["current"]],
                         ["3333333"])


class AutoPickTests(ListWebTestCase):
    """**1件になったら、それが答え。**

    番号を打つ専用の欄を無くしたので、ここが速い道を引き受ける。
    規則はこれだけ ── 同じロットかどうかで分けると「打ったのに
    開かないことがある」になり、利用者が規則を言葉にできなくなる。
    """

    def setUp(self) -> None:
        super().setUp()
        from tests.test_lot_service import insert_hiki, insert_odr
        insert_hiki(self.conn, lot_no="1111111")
        insert_odr(self.conn)

    def test_1件に絞れたら詳細まで返る(self) -> None:
        """追加の往復なしにそのまま開けるようにする。"""
        body = self.post("/api/lot/list/search", {"text": "1111111"})
        self.assertEqual(len(body["rows"]), 1)
        self.assertIn("detail", body)
        self.assertTrue(body["detail"]["found"])
        self.assertEqual(body["detail"]["lot_no"], "1111111")

    def test_1件に絞れたら作業中のロットになる(self) -> None:
        """リボンと資材展開が見るのはここ。"""
        body = self.post("/api/lot/list/search", {"text": "1111111"})
        self.assertEqual(work_context.get_context().lot_no, "1111111")
        self.assertEqual(body["ribbon"]["lot"], "1111111")
        self.assertEqual([r["lot_no"] for r in body["rows"] if r["current"]],
                         ["1111111"])

    def test_2件以上なら確定しない(self) -> None:
        """勝手に1つ選ぶと、選んだつもりのないロットで作業が進む。"""
        body = self.post("/api/lot/list/search", {"text": "A5052"})
        self.assertEqual(len(body["rows"]), 2)
        self.assertNotIn("detail", body)
        self.assertEqual(work_context.get_context().lot_no, "")

    def test_0件でも確定しない(self) -> None:
        body = self.post("/api/lot/list/search", {"text": "ありえない"})
        self.assertNotIn("detail", body)

    def test_条件で1件になっても同じ(self) -> None:
        """検索語でも条件チップでも扱いを変えない。**規則は1つ。**"""
        self.post("/api/lot/list/filter/add",
                  {"column": "zaishitsu", "op": "=", "value": "A1050"})
        body = self.get()
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(work_context.get_context().lot_no, "2222222")


class PageTests(ListWebTestCase):

    def test_画面に一覧が埋まっている(self) -> None:
        """空の表が一瞬出ると「データが無い」ように見える。"""
        html = self.client.get("/lot", headers=self.auth()).get_data(as_text=True)
        self.assertIn("仕掛一覧", html)
        self.assertIn("1111111", html)
        self.assertIn("条件を追加", html)

    def test_取り込み前は一覧を出さない(self) -> None:
        self.conn.execute("DELETE FROM 仕掛ロット")
        self.conn.commit()
        body = self.get()
        self.assertFalse(body["available"])
        self.assertIn("設定画面", body["unavailable_message"])


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
