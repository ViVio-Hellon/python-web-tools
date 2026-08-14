"""描画計画のJSON変換 (`presenters.render_json`) の確認

配置図は Canvas から SVG へ作り替えるが、**計算は作り替えない**。
`placement_render` / `angle_render` が返す計画をそのまま dict にして、
ブラウザ側は `<rect>` を書くだけにする。

ここで見るのは「計画の中身を落とさずに JSON へ移せているか」と
「JSONにできる型だけになっているか」の2点。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import angle_render, placement_render  # noqa: E402
from packaging_tool.models import PlacedBoardModel  # noqa: E402
from packaging_tool.presenters import render_json  # noqa: E402


def _placed(**kw) -> PlacedBoardModel:
    """配置済みボード1枚。座標系は X=丈方向 / Y=幅方向。"""
    base = dict(width=1130, length=750, x=0, y=0,
                board_category=placement_render.CATEGORY_LOWER,
                original_width=750, original_length=1130, instance_id="b1")
    base.update(kw)
    return PlacedBoardModel(**base)


class RenderPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.placed = [
            _placed(x=0, y=0, instance_id="b1"),
            _placed(x=750, y=0, instance_id="b2"),
        ]
        self.plan = render_json.build_render_plan_dict(
            self.placed, placement_render.CATEGORY_LOWER, 1150, 2650)

    def test_JSONにできる(self) -> None:
        """dataclass や tuple が残っていると `json.dumps` で落ちる。"""
        json.dumps(self.plan, ensure_ascii=False)

    def test_枠とボードが入っている(self) -> None:
        self.assertIsNotNone(self.plan["border"])
        self.assertEqual(len(self.plan["boards"]), 2)

    def test_矩形の項目がそろっている(self) -> None:
        rect = self.plan["boards"][0]
        for key in ("x", "y", "width", "height", "fill", "outline",
                    "caption", "font_size"):
            self.assertIn(key, rect)

    def test_元の計画と同じ値になる(self) -> None:
        """変換で数値がずれていないこと(丸めの範囲内)。"""
        original = placement_render.build_render_plan(
            self.placed, placement_render.CATEGORY_LOWER, 1150, 2650,
            render_json.LOGICAL_CANVAS_W, render_json.LOGICAL_CANVAS_H)
        self.assertAlmostEqual(self.plan["scale"], original.scale, places=3)
        self.assertEqual(len(self.plan["boards"]), len(original.boards))
        for got, want in zip(self.plan["boards"], original.boards):
            self.assertAlmostEqual(got["x"], want.x, places=3)
            self.assertAlmostEqual(got["width"], want.width, places=3)
            self.assertEqual(got["fill"], want.fill)
            self.assertEqual(got["caption"], want.caption)

    def test_空の配置でも壊れない(self) -> None:
        plan = render_json.build_render_plan_dict(
            [], placement_render.CATEGORY_LOWER, 1150, 2650)
        json.dumps(plan)
        self.assertEqual(plan["boards"], [])

    def test_基準サイズが0なら計画は空(self) -> None:
        plan = render_json.build_render_plan_dict(
            self.placed, placement_render.CATEGORY_LOWER, 0, 0)
        self.assertIsNone(plan["border"])

    def test_凡例は数値と色の組(self) -> None:
        for width, color in self.plan["legend"]:
            self.assertIsInstance(width, int)
            self.assertIsInstance(color, str)


class RoundingTests(unittest.TestCase):
    def test_3桁に丸める(self) -> None:
        self.assertEqual(render_json.round_px(1.23456), 1.235)

    def test_マイナスゼロは0にする(self) -> None:
        """`-0.0` のままだとJSONに `-0.0` と出て、比較で揺れる。"""
        self.assertEqual(str(render_json.round_px(-0.0001)), "0.0")

    def test_Noneの矩形はNoneのまま(self) -> None:
        self.assertIsNone(render_json.rect_to_dict(None))
        self.assertIsNone(render_json.angle_rect_to_dict(None))


class AnglePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = render_json.build_angle_plan_dict(
            [1300, 1300], product_len=2502, pallet_len=2650, leg_count=4)

    def test_JSONにできる(self) -> None:
        json.dumps(self.plan, ensure_ascii=False)

    def test_断面の3段がそろっている(self) -> None:
        """アングルバー(上) / パレット面 / 脚(下)。"""
        self.assertIsNotNone(self.plan["pallet_bar"])
        self.assertEqual(len(self.plan["bars"]), 2)
        self.assertEqual(len(self.plan["legs"]), 4)

    def test_バーのフォントサイズがJSONに乗る(self) -> None:
        """`angle_render.Rect.font_size` を運ばないと、描画側の既定(7)に
        落ちて実機で読めない大きさになる(現場の声)。"""
        for bar in self.plan["bars"]:
            self.assertIn("font_size", bar)
            self.assertGreater(bar["font_size"], 7)

    def test_製品の左右端が入る(self) -> None:
        self.assertEqual(len(self.plan["product_edges"]), 2)

    def test_バーの項目がそろっている(self) -> None:
        bar = self.plan["bars"][0]
        for key in ("x", "y", "width", "height", "fill", "outline",
                    "caption", "text_color", "bold"):
            self.assertIn(key, bar)

    def test_元の計画と同じ値になる(self) -> None:
        original = angle_render.build_angle_plan(
            [1300, 1300], 2502, 2650, 4, render_json.LOGICAL_ANGLE_W)
        self.assertEqual(len(self.plan["bars"]), len(original.bars))
        for got, want in zip(self.plan["bars"], original.bars):
            self.assertAlmostEqual(got["x"], want.x, places=3)
            self.assertEqual(got["caption"], want.caption)

    def test_製品丈0なら空(self) -> None:
        plan = render_json.build_angle_plan_dict([], 0, 0, 2)
        self.assertIsNone(plan["pallet_bar"])
        self.assertEqual(plan["bars"], [])

    def test_カットありでも壊れない(self) -> None:
        plan = render_json.build_angle_plan_dict(
            [2500], product_len=2000, pallet_len=2100, leg_count=2, need_cut=True)
        json.dumps(plan)

    def test_注記は文字要素(self) -> None:
        plan = render_json.build_angle_plan_dict(
            [1500, 1500], product_len=2000, pallet_len=0, leg_count=2)
        for note in plan["notes"]:
            self.assertIn("text", note)
            self.assertIsInstance(note["text"], str)


class ViewBoxTests(unittest.TestCase):
    def test_既定はボード配置図の寸法(self) -> None:
        self.assertEqual(render_json.view_box(), "0 0 980 460")

    def test_アングルは低い帯(self) -> None:
        self.assertEqual(
            render_json.view_box(render_json.LOGICAL_ANGLE_CANVAS), "0 0 980 136")

    def test_ゴールデンと同じ論理キャンバスを使う(self) -> None:
        """`scripts/compare_ui.py` が固定している値と一致すること。

        ずれると「ゴールデンは合っているのにWeb版の図が違う」が起きる。
        """
        golden = Path(__file__).resolve().parent / "golden" / "selection_placement.json"
        if not golden.exists():
            self.skipTest("ゴールデンがありません")
        data = json.loads(golden.read_text(encoding="utf-8"))
        self.assertEqual(data["_logical_canvas"],
                         [render_json.LOGICAL_CANVAS_W, render_json.LOGICAL_CANVAS_H])


if __name__ == "__main__":
    unittest.main()
