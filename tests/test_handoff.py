"""画面をまたぐ受け渡し — ボードMAP / パレットMAP (旧版 `btnMap` / `btnUFMAP`)

【何のための道か】
選定したボードが5種類あるとき、置き場を1件ずつ探させると5回打ち直す
ことになります。旧版はここを1押しで済ませ、**合計疲労度**まで出して
いました ── 「どれを取りに行くか」ではなく「全部でどれだけ歩くか」が
分かるのが要点です。

【ここで守りたいこと】
- **品目や寸法を画面から送らせない。** 何が決まっているかはサーバ側の
  作業状態(`work_context`)が持っている。送らせると、同じ事実が
  画面とサーバの2か所に生まれる
- 渡すものが無いときは **422**(業務としての断り)で、移らない
- 渡ってきていない画面は受け側のボタンを**出さない**
- 置き場が登録されていない資材を**黙って落とさない**
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import (floor_plan, layout_session, selection_session,  # noqa: E402
                            user_settings, work_context)
from packaging_tool.presenters import inventory as inv_presenter  # noqa: E402
from packaging_tool.presenters import layout as layout_presenter  # noqa: E402
from tests import _web  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"


def insert_board(conn, *, width, length, label="lblItem1") -> None:
    conn.execute(
        "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
        " VALUES (?,?,?,?)", (width, length, "ハードボード", label))
    conn.commit()


def insert_angle(conn, *, length, label="lblItem2") -> None:
    conn.execute(
        "INSERT INTO CornerboardMaster (アングル丈, データラベル) VALUES (?,?)",
        (length, label))
    conn.commit()


# ==================================================================
# 業務層 — 渡すものを組み立てる / 受けて光らせる
# ==================================================================
class MapItemsTests(unittest.TestCase):
    """`selection_session.map_items()` — 渡す中身。"""

    def setUp(self) -> None:
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.session = selection_session.get_session(self.conn)

    def test_何も選んでいなければ空(self):
        self.assertEqual(self.session.map_items(), [])

    def test_上用と下用とアングルを全部入れる(self):
        """取りに行くのは1回の作業。種類ごとに分けても現場はまとめて回る。"""
        from packaging_tool import board_selection_service as svc

        self.session.selected.lower.append(svc.SelectedBoard(750, 1130, 2))
        self.session.selected.upper.append(svc.SelectedBoard(600, 900, 1))
        self.session.selected_angles.append(1200)

        items = self.session.map_items()
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0], {"kind": "board", "category": "下用",
                                    "width": 750, "length": 1130, "count": 2})
        self.assertEqual(items[1], {"kind": "board", "category": "上用",
                                    "width": 600, "length": 900, "count": 1})
        self.assertEqual(items[2], {"kind": "angle", "width": 0,
                                    "length": 1200, "count": 1})

    def test_同じ区分で同じサイズの行は合算する(self):
        """別案では同じサイズが「主」と「カット前提」の2行に分かれる。
        取りに行くのは1か所なので1件にまとめる(VBA `AddOrMergeSel`)。
        区分が違えば別の件(上用と下用は別に数える)。"""
        from packaging_tool import board_selection_service as svc

        self.session.selected.lower += [svc.SelectedBoard(1000, 2000, 2, "主"),
                                        svc.SelectedBoard(1000, 2000, 1, "カット前提")]
        self.session.selected.upper.append(svc.SelectedBoard(1000, 2000, 1, "主"))

        items = self.session.map_items()
        self.assertEqual([(i["category"], i["count"]) for i in items],
                         [("下用", 3), ("上用", 1)])


class StockSizeTests(unittest.TestCase):
    """`selection_session.stock_size()` — パレットMAPが渡す寸法。"""

    def setUp(self) -> None:
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.session = selection_session.get_session(self.conn)

    def test_パレットが決まっていなければ空(self):
        self.assertEqual(self.session.stock_size(), {})

    def test_決まったパレットの寸法を渡す(self):
        from packaging_tool import board_selection_service as svc

        self.session.palette = svc.Palette(width=1100, length=1100)
        self.assertEqual(self.session.stock_size(),
                         {"width": 1100, "length": 1100})


class ShowSelectionTests(unittest.TestCase):
    """`layout_session.show_selection()` — 受けてまとめて光らせる。"""

    def setUp(self) -> None:
        layout_session.reset_session()
        self.addCleanup(layout_session.reset_session)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"
        self.addCleanup(lambda: setattr(floor_plan, "USER_PATH", original))

        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.session = layout_session.get_session()
        self.labels = floor_plan.load().item_names

    def test_渡すものが無ければ断る(self):
        """**入力の形の誤りではない**ので「渡ってきていない」と言い分ける。

        形が違う(400)と、業務として断る(422)は別のもの。ここは
        要求そのものは正しく、渡すものが無いだけ。
        """
        result = self.session.show_selection(self.conn, [])
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, layout_session.REFUSE_NO_HANDOFF)
        self.assertEqual(self.session.highlight, set())

    def test_複数の資材の置き場をまとめて光らせる(self):
        insert_board(self.conn, width=750, length=1130, label=self.labels[0])
        insert_board(self.conn, width=600, length=900, label=self.labels[1])
        result = self.session.show_selection(self.conn, [
            {"kind": "board", "width": 750, "length": 1130, "count": 1},
            {"kind": "board", "width": 600, "length": 900, "count": 1},
        ])
        self.assertTrue(result.ok)
        self.assertEqual(self.session.highlight,
                         {self.labels[0], self.labels[1]})

    def test_合計疲労度を出す(self):
        """要点は「全部でどれだけ歩くか」。1件ずつの距離ではない。"""
        insert_board(self.conn, width=750, length=1130, label=self.labels[0])
        user_settings.set_position(floor_plan.load().base_point_names[0])
        result = self.session.show_selection(self.conn, [
            {"kind": "board", "width": 750, "length": 1130, "count": 1}])
        self.assertTrue(result.ok)
        self.assertIn("合計疲労度=", result.message)

    def test_ボードとアングルが混ざっていても両方光る(self):
        insert_board(self.conn, width=750, length=1130, label=self.labels[0])
        insert_angle(self.conn, length=1200, label=self.labels[1])
        result = self.session.show_selection(self.conn, [
            {"kind": "board", "width": 750, "length": 1130, "count": 1},
            {"kind": "angle", "width": 0, "length": 1200, "count": 1},
        ])
        self.assertTrue(result.ok)
        self.assertEqual(self.session.highlight,
                         {self.labels[0], self.labels[1]})

    def test_置き場が無いものは黙って落とさない(self):
        """光らないぶんは合計にも入らない。言わないと嘘になる。"""
        insert_board(self.conn, width=750, length=1130, label=self.labels[0])
        result = self.session.show_selection(self.conn, [
            {"kind": "board", "width": 750, "length": 1130, "count": 1},
            {"kind": "board", "width": 999, "length": 999, "count": 1},
        ])
        self.assertTrue(result.ok)
        self.assertIn("未登録", result.message)
        self.assertIn("999×999", result.message)

    def test_1つも置き場が無ければ断って直し方を書く(self):
        result = self.session.show_selection(self.conn, [
            {"kind": "board", "width": 999, "length": 999, "count": 1}])
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, layout_session.REFUSE_NOT_FOUND)
        self.assertIn("データラベル", result.message)

    def test_前の検索結果は消える(self):
        """まとめて見るのは**引き直し**。前の1件が残ると数が合わない。"""
        insert_board(self.conn, width=750, length=1130, label=self.labels[0])
        insert_board(self.conn, width=600, length=900, label=self.labels[1])
        self.session.search(self.conn, "board", "600", "900")
        self.assertEqual(self.session.highlight, {self.labels[1]})

        self.session.show_selection(self.conn, [
            {"kind": "board", "width": 750, "length": 1130, "count": 1}])
        self.assertEqual(self.session.highlight, {self.labels[0]})


class WorkContextTests(unittest.TestCase):
    """作業状態が受け渡しの唯一の口。"""

    def setUp(self) -> None:
        self.context = work_context.reset_context()
        self.addCleanup(work_context.reset_context)

    def test_渡したものを持つ(self):
        self.context.set_map_items([{"kind": "board", "width": 750,
                                     "length": 1130, "count": 1}])
        self.context.set_stock_size(1100, 1100)
        self.assertEqual(len(self.context.map_items), 1)
        self.assertEqual(self.context.stock_size,
                         {"width": 1100, "length": 1100})

    def test_ロットが変われば持ち越さない(self):
        """前のロットの選定結果を次のロットで光らせない。"""
        self.context.set_map_items([{"kind": "board", "width": 750,
                                     "length": 1130, "count": 1}])
        self.context.set_stock_size(1100, 1100)
        self.context.clear()
        self.assertEqual(self.context.map_items, [])
        self.assertEqual(self.context.stock_size, {})


# ==================================================================
# API — 渡す口と受ける口
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class HandoffApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import layout as layout_routes
        from app.routes import selection as selection_routes

        layout_session.reset_session()
        selection_session.reset_session()
        work_context.reset_context()
        self.addCleanup(layout_session.reset_session)
        self.addCleanup(selection_session.reset_session)
        self.addCleanup(work_context.reset_context)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"
        self.addCleanup(lambda: setattr(floor_plan, "USER_PATH", original))

        self.conn = _web.bind_db(self, selection_routes)
        _web.bind_db(self, layout_routes, self.conn)
        self.client = _web.make_client(port=8731, ready=False)
        self.labels = floor_plan.load().item_names

    def post(self, path, expect=200) -> dict:
        res = self.client.post(path, json={}, headers=_web.auth())
        self.assertEqual(res.status_code, expect, f"{path}: {res.get_json()}")
        return res.get_json()

    # -- ボードMAP -----------------------------------------------------
    def test_何も選んでいなければ422で移らない(self):
        body = self.post("/api/selection/map", expect=422)
        self.assertEqual(body["error"]["code"], "no_selection")
        self.assertEqual(work_context.get_context().map_items, [])

    def test_選んだものを渡して棚検索へ送る(self):
        from packaging_tool import board_selection_service as svc

        session = selection_session.get_session(self.conn)
        session.selected.lower.append(svc.SelectedBoard(750, 1130, 2))

        body = self.post("/api/selection/map")
        self.assertEqual(body["next"], "/layout")
        self.assertEqual(body["count"], 1)
        self.assertEqual(len(work_context.get_context().map_items), 1)

    def test_棚検索は渡された資材だけで光らせる(self):
        """**画面から品目を送らない。** 送る口そのものが無い。"""
        from packaging_tool import board_selection_service as svc

        insert_board(self.conn, width=750, length=1130, label=self.labels[0])
        session = selection_session.get_session(self.conn)
        session.selected.lower.append(svc.SelectedBoard(750, 1130, 1))
        self.post("/api/selection/map")

        state = self.post("/api/layout/from-selection")
        hit = [s["name"] for s in state["shelves"] if s["state"] == "hit"]
        self.assertEqual(hit, [self.labels[0]])

    def test_渡っていないのに受け側を押したら422(self):
        state = self.post("/api/layout/from-selection", expect=422)
        self.assertIn("資材選択", state["message"])
        # 断られても図は消えない(壊れたのか断られたのか区別できなくなる)
        self.assertTrue(state["shelves"])

    def test_渡っていなければ受け側のボタンは出さない(self):
        """押せるのに何も起きないボタンを作らない(件数0=出さない)。"""
        res = self.client.get("/api/layout/state", headers=_web.auth())
        self.assertEqual(res.get_json()["handoff"], 0)

    def test_渡っていれば件数が画面に出る(self):
        from packaging_tool import board_selection_service as svc

        session = selection_session.get_session(self.conn)
        session.selected.lower.append(svc.SelectedBoard(750, 1130, 1))
        session.selected_angles.append(1200)
        self.post("/api/selection/map")

        res = self.client.get("/api/layout/state", headers=_web.auth())
        self.assertEqual(res.get_json()["handoff"], 2)

    # -- パレットMAP ---------------------------------------------------
    def test_パレットが決まっていなければ422で移らない(self):
        body = self.post("/api/selection/stock", expect=422)
        self.assertEqual(body["error"]["code"], "no_pallet")
        self.assertEqual(work_context.get_context().stock_size, {})

    def test_決まったパレットの寸法を簡易在庫へ送る(self):
        from packaging_tool import board_selection_service as svc

        session = selection_session.get_session(self.conn)
        session.palette = svc.Palette(width=1100, length=1100)

        body = self.post("/api/selection/stock")
        self.assertEqual(body["next"], "/inventory")
        self.assertEqual(body["width"], 1100)
        self.assertEqual(work_context.get_context().stock_size,
                         {"width": 1100, "length": 1100})

    def test_簡易在庫は渡された寸法で引いたところから始まる(self):
        """渡ってきているのに空の画面を出したら、渡した意味が無い。"""
        work_context.get_context().set_stock_size(1100, 1100)
        view = inv_presenter.initial(self.conn)
        self.assertTrue(view.from_selection)
        self.assertEqual(view.width_text, "1100")
        self.assertEqual(view.length_text, "1100")
        self.assertIn("1100×1100", view.caption)

    def test_渡っていなければ簡易在庫はまっさらで開く(self):
        view = inv_presenter.initial(self.conn)
        self.assertFalse(view.from_selection)
        self.assertEqual(view.width_text, "")
        self.assertIn("幅と丈", view.message)


@unittest.skipUnless(HAS_WEB, _SKIP)
class LayoutHandoffViewTests(unittest.TestCase):
    """棚検索のビューモデルが持つ受け渡しの件数。"""

    def setUp(self) -> None:
        layout_session.reset_session()
        work_context.reset_context()
        self.addCleanup(layout_session.reset_session)
        self.addCleanup(work_context.reset_context)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"
        self.addCleanup(lambda: setattr(floor_plan, "USER_PATH", original))

        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)

    def test_渡っていなければ0(self):
        view = layout_presenter.build(self.conn, layout_session.get_session())
        self.assertEqual(view.handoff, 0)

    def test_渡っていれば件数を出す(self):
        work_context.get_context().set_map_items([
            {"kind": "board", "width": 750, "length": 1130, "count": 1},
            {"kind": "angle", "width": 0, "length": 1200, "count": 1},
        ])
        view = layout_presenter.build(self.conn, layout_session.get_session())
        self.assertEqual(view.handoff, 2)
        self.assertEqual(layout_presenter.to_dict(view)["handoff"], 2)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
