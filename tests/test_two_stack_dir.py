"""2山積の向き(VBA `m_2YamaDir`)── パレット検索が決めた向きで製品サイズを2倍する

VBA の仕様更新: それまで2山積を知っていたのはパレット検索だけで、上用の選定・
配置とカバー表示は1山分(製品幅170)で動いていた。上用は1山分しか選ばれず、
2山目の上には何も載らない。下用の「幅+230mm超」も 400−170 で、2山なら
400−340 のはず。

【ここで守りたいこと】
- パレット検索は、どちらの向きで2山にしたか(幅2山 / 丈2山)を結果に残す
- 「セット」はその向きを2倍して**2山分を1つの製品として**確定する
- 入力欄は1山分のまま(2倍を書き戻すと、次の検索で4倍になる)
- 向きは、2山積の切り替え・検索のやり直し・ロット切替・実績読込で忘れる
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import board_selection_service as svc  # noqa: E402
from packaging_tool import db, selection_session, work_context  # noqa: E402
from tests.test_board_selection_service import insert_pallet  # noqa: E402
from tests.test_web_selection import SelectionWebTestCase  # noqa: E402
from tests.test_web_selection import insert_pallet as web_insert_pallet  # noqa: E402


def _pallets(conn, insert) -> None:
    # 幅2山用: 製品 170×1000 を2山で並べた 340×1000 が載る(一般・奇数桁)
    insert(conn, width=400, length=1100, w_min=300, w_max=390,
           l_min=900, l_max=1090, industry="一般", keta=3)
    # 丈2山用: 製品 400×600 を丈方向に2山で 400×1200 が載る(スカシ・脚3本)
    insert(conn, width=450, length=1300, w_min=350, w_max=440,
           l_min=1150, l_max=1290, industry="スカシ", leg=3)


class AutoSelectStackDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        _pallets(self.conn, insert_pallet)

    def tearDown(self) -> None:
        self.conn.close()

    def _search(self, w, l, two_stack=True):
        return svc.auto_select_pallet(self.conn, product_width_text=str(w),
                                      product_length_text=str(l), two_stack=two_stack)

    def test_幅2山で決まれば向きは幅(self) -> None:
        r = self._search(170, 1000)
        self.assertTrue(r.ok)
        self.assertEqual((r.width, r.length, r.stack_dir), (400, 1100, "幅"))

    def test_丈2山で決まれば向きは丈(self) -> None:
        r = self._search(400, 600)
        self.assertTrue(r.ok)
        self.assertEqual((r.width, r.length, r.stack_dir), (450, 1300, "丈"))

    def test_2山積でなければ向きは空(self) -> None:
        insert_pallet(self.conn, width=200, length=1100, w_min=150, w_max=190,
                      l_min=900, l_max=1090)
        r = self._search(170, 1000, two_stack=False)
        self.assertTrue(r.ok)
        self.assertEqual(r.stack_dir, "")


class ApplyProductStackTests(unittest.TestCase):
    palette = svc.Palette(width=400, length=1100, overhang_ratio=1.1,
                          max_width=440, max_length=1210)

    def test_幅の向きなら幅を2倍(self) -> None:
        res, product, rotated = svc.apply_product_size(
            "170", "1000", self.palette, two_stack=True, stack_dir="幅")
        self.assertTrue(res.ok)
        self.assertEqual((product.width, product.length), (340, 1000))
        self.assertFalse(rotated)
        self.assertIn("※2山(幅170×2)", res.message)

    def test_丈の向きなら丈を2倍(self) -> None:
        palette = svc.Palette(width=450, length=1300)
        res, product, _ = svc.apply_product_size(
            "400", "600", palette, two_stack=True, stack_dir="丈")
        self.assertEqual((product.width, product.length), (400, 1200))
        self.assertIn("※2山(丈600×2)", res.message)

    def test_向きが決まっていなければ1山扱いと断る(self) -> None:
        res, product, _ = svc.apply_product_size(
            "170", "1000", self.palette, two_stack=True, stack_dir="")
        self.assertEqual((product.width, product.length), (170, 1000))
        self.assertIn("※2山積(向き未決定→1山扱い)", res.message)

    def test_2山積でなければ向きがあっても2倍しない(self) -> None:
        res, product, _ = svc.apply_product_size(
            "170", "1000", self.palette, two_stack=False, stack_dir="幅")
        self.assertEqual((product.width, product.length), (170, 1000))
        self.assertNotIn("2山", res.message)

    def test_2倍したら載らないなら断る(self) -> None:
        palette = svc.Palette(width=300, length=1100)
        res, _, _ = svc.apply_product_size(
            "170", "1000", palette, two_stack=True, stack_dir="幅")
        self.assertFalse(res.ok)


class TwoStackWebTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        _pallets(self.conn, web_insert_pallet)
        self.post("/api/selection/toggle/two_stack")
        self.assertTrue(self.session().two_stack)

    def search(self, w, l) -> dict:
        return self.post("/api/selection/pallet/search",
                         {"product_width": str(w), "product_length": str(l)})

    def test_検索で製品は2山分で確定し入力欄は1山分のまま(self) -> None:
        state = self.search(170, 1000)
        s = self.session()
        self.assertEqual(s.stack_dir, "幅")
        self.assertEqual((s.product.width, s.product.length), (340, 1000))
        context = work_context.get_context()
        self.assertEqual((context.product_width, context.product_length), (170, 1000))
        self.assertIn("※2山(幅170×2)", state["product_status"])

    def test_もう一度検索しても4倍にならない(self) -> None:
        self.search(170, 1000)
        context = work_context.get_context()
        self.search(context.product_width, context.product_length)
        s = self.session()
        self.assertEqual((s.product.width, s.product.length), (340, 1000))

    def test_セットし直しても2山分(self) -> None:
        self.search(170, 1000)
        state = self.post("/api/selection/product/apply",
                          {"width": "170", "length": "1000"})
        s = self.session()
        self.assertEqual((s.product.width, s.product.length), (340, 1000))
        self.assertIn("※2山(幅170×2)", state["product_status"])

    def test_回転して決まったら回転後の寸法を2倍する(self) -> None:
        """VBA は回転Passで決まると入力欄の幅・丈を入れ替えてから「セット」する。"""
        state = self.search(1000, 170)
        self.assertTrue(state["rotated"])
        s = self.session()
        self.assertEqual((s.product.width, s.product.length), (340, 1000))
        self.assertTrue(s.product_rotated)
        context = work_context.get_context()
        self.assertEqual((context.product_width, context.product_length), (170, 1000))

    def test_丈2山(self) -> None:
        state = self.search(400, 600)
        s = self.session()
        self.assertEqual(s.stack_dir, "丈")
        self.assertEqual((s.product.width, s.product.length), (400, 1200))
        self.assertIn("※2山(丈600×2)", state["product_status"])
        context = work_context.get_context()
        self.assertEqual((context.product_width, context.product_length), (400, 600))

    def test_2山積を切り替えたら向きを忘れる(self) -> None:
        self.search(170, 1000)
        self.post("/api/selection/toggle/two_stack")   # OFF
        self.post("/api/selection/toggle/two_stack")   # ON
        self.assertEqual(self.session().stack_dir, "")
        state = self.post("/api/selection/product/apply",
                          {"width": "170", "length": "1000"})
        s = self.session()
        self.assertEqual((s.product.width, s.product.length), (170, 1000))
        self.assertIn("向き未決定→1山扱い", state["product_status"])

    def test_検索で見つからなければ前の向きを残さない(self) -> None:
        self.search(170, 1000)
        self.post("/api/selection/pallet/search",
                  {"product_width": "9000", "product_length": "9000"}, expect=422)
        self.assertEqual(self.session().stack_dir, "")

    def test_ロットが変わったら向きを忘れる(self) -> None:
        self.search(170, 1000)
        self.session().clear_for_new_lot()
        s = self.session()
        self.assertEqual(s.stack_dir, "")
        self.assertEqual(s.product_stack_note, "")

    # --- 画面の流れ: 製品サイズで絞った一覧から行を選んで「パレット決定」 ---
    def pick(self, w, l, pw, pl) -> dict:
        return self.post("/api/selection/pallet/pick",
                         {"width": w, "length": l, "symbol": "",
                          "product_width": str(pw), "product_length": str(pl)})

    def test_一覧から選んでも向きが決まる_幅2山(self) -> None:
        state = self.pick(400, 1100, 170, 1000)
        s = self.session()
        self.assertEqual(s.stack_dir, "幅")
        self.assertEqual((s.product.width, s.product.length), (340, 1000))
        self.assertIn("※2山(幅170×2)", state["product_status"])
        context = work_context.get_context()
        self.assertEqual((context.product_width, context.product_length), (170, 1000))

    def test_一覧から選んでも向きが決まる_丈2山(self) -> None:
        self.pick(450, 1300, 400, 600)
        s = self.session()
        self.assertEqual(s.stack_dir, "丈")
        self.assertEqual((s.product.width, s.product.length), (400, 1200))

    def test_一覧で回転して当たった行は回転後の寸法を2倍する(self) -> None:
        self.pick(400, 1100, 1000, 170)
        s = self.session()
        self.assertEqual((s.product.width, s.product.length), (340, 1000))
        self.assertTrue(s.product_rotated)

    def test_2山積でなければ一覧から選んでも1山(self) -> None:
        self.post("/api/selection/toggle/two_stack")   # OFF
        web_insert_pallet(self.conn, width=200, length=1100, w_min=150, w_max=190,
                          l_min=900, l_max=1090)
        self.pick(200, 1100, 170, 1000)
        s = self.session()
        self.assertEqual(s.stack_dir, "")
        self.assertEqual((s.product.width, s.product.length), (170, 1000))

    def test_上用は2山分の幅で選ぶ(self) -> None:
        """上用の選定・配置が見る製品幅は2山分(ここを直すのが今回の目的)。"""
        self.search(170, 1000)
        self.assertEqual(self.session().product.width, 340)


if __name__ == "__main__":
    unittest.main()
