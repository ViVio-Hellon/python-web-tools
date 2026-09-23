"""配置編集の「そろえる・隙間をなくす」

現場の声:「配置編集でコントロールをきれいに並べれない」。1つずつ
ドラッグで合わせると、1ドットずつずれた段々や、重なり・隙間が残る
(送られてきた図では、A1〜A11 の間隔がまちまちで、C1〜C7 は高さが
ばらばらに重なっていた)。

計算は `map_data.arrange` の1か所で、棚検索(`layout_session`)と
簡易在庫(`pallet_map_session`)が同じものを使う。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402

_isolation.ensure_isolated()

from packaging_tool import map_data as md  # noqa: E402

# 送られてきた図に似せた並び:間隔がまちまちで、1つだけ背が高い
ROW = {
    "C1": (100.0, 20.0, 10.0, 30.0),
    "C2": (113.0, 22.0, 10.0, 30.0),
    "C3": (119.0, 25.0, 10.0, 45.0),     # 重なっている・背が高い
    "C4": (140.0, 21.0, 10.0, 30.0),     # 離れている
}


class ArrangeTests(unittest.TestCase):
    """計算だけを見る。**どこへ動かすか**が正しいか。"""

    def test_左をそろえる(self) -> None:
        out = md.arrange(ROW, md.ALIGN_LEFT)
        self.assertEqual({x for x, *_ in out.values()}, {100.0})

    def test_右をそろえる(self) -> None:
        """右の端(x+w)が、いちばん右の箱の右端にそろう。"""
        out = md.arrange(ROW, md.ALIGN_RIGHT)
        self.assertEqual({x + w for x, _y, w, _h in out.values()}, {150.0})

    def test_上をそろえる(self) -> None:
        out = md.arrange(ROW, md.ALIGN_TOP)
        self.assertEqual({y for _x, y, *_ in out.values()}, {20.0})

    def test_下をそろえる(self) -> None:
        out = md.arrange(ROW, md.ALIGN_BOTTOM)
        self.assertEqual({y + h for _x, y, _w, h in out.values()}, {70.0})

    def test_高さはまん中の値にそろえる(self) -> None:
        """**1つだけ背の高い箱に全部が引っ張られない。** いちばん大きい
        箱に合わせる作りだと、C3 の45に全部が伸びる。"""
        out = md.arrange(ROW, md.SAME_HEIGHT)
        self.assertEqual({h for *_rest, h in out.values()}, {30.0})

    def test_幅はまん中の値にそろえる(self) -> None:
        boxes = {"a": (0, 0, 10, 5), "b": (0, 0, 12, 5), "c": (0, 0, 40, 5)}
        out = md.arrange(boxes, md.SAME_WIDTH)
        self.assertEqual({w for _x, _y, w, _h in out.values()}, {12})

    def test_そろえても他の向きは動かさない(self) -> None:
        """上をそろえたら横位置と大きさはそのまま。"""
        out = md.arrange(ROW, md.ALIGN_TOP)
        for name, (x, _y, w, h) in out.items():
            with self.subTest(name=name):
                self.assertEqual((x, w, h), (ROW[name][0], *ROW[name][2:]))

    def test_横に詰めると隙間も重なりも無くなる(self) -> None:
        out = md.arrange(ROW, md.PACK_ROW)
        order = sorted(out, key=lambda n: out[n][0])
        for left, right in zip(order, order[1:]):
            with self.subTest(pair=(left, right)):
                lx, _ly, lw, _lh = out[left]
                self.assertEqual(out[right][0], lx + lw)

    def test_横に詰めてもいちばん左は動かない(self) -> None:
        """**起点は動かさない。** 全部が図の端へ寄ると、並べ直すたびに
        写真と合わせ直すことになる。"""
        self.assertEqual(md.arrange(ROW, md.PACK_ROW)["C1"][0], 100.0)

    def test_横に詰めても並び順は変えない(self) -> None:
        """**いまの位置の順**。名前順にすると現場で並べた順が崩れる。"""
        boxes = {"B": (0.0, 0, 10, 5), "A": (50.0, 0, 10, 5)}
        out = md.arrange(boxes, md.PACK_ROW)
        self.assertLess(out["B"][0], out["A"][0])

    def test_横に詰めても上下は動かさない(self) -> None:
        out = md.arrange(ROW, md.PACK_ROW)
        for name in ROW:
            with self.subTest(name=name):
                self.assertEqual(out[name][1], ROW[name][1])

    def test_縦に詰める(self) -> None:
        col = {"a": (0, 10.0, 5, 8.0), "b": (3, 30.0, 5, 8.0), "c": (1, 21.0, 5, 8.0)}
        out = md.arrange(col, md.PACK_COLUMN)
        self.assertEqual([out[n][1] for n in ("a", "c", "b")], [10.0, 18.0, 26.0])
        self.assertEqual([out[n][0] for n in "abc"], [0, 3, 1])   # 左右は触らない

    def test_同じ位置なら名前で決める(self) -> None:
        """**毎回同じ結果になる**こと。押すたびに並びが入れ替わらない。"""
        boxes = {"b": (0.0, 0.0, 5, 5), "a": (0.0, 0.0, 5, 5)}
        out = md.arrange(boxes, md.PACK_ROW)
        self.assertEqual((out["a"][0], out["b"][0]), (0.0, 5.0))

    def test_1つなら何もしない(self) -> None:
        one = {"a": (3.0, 4.0, 5.0, 6.0)}
        for op in md.ARRANGE_OPS:
            with self.subTest(op=op):
                self.assertEqual(md.arrange(one, op), one)

    def test_知らない並べ方は断る(self) -> None:
        with self.assertRaises(ValueError):
            md.arrange(ROW, "center")

    def test_全部の並べ方に言葉がある(self) -> None:
        """画面に出す言葉はサーバが持つ。**足した並べ方の言い忘れ**を防ぐ。"""
        for op in md.ARRANGE_OPS:
            with self.subTest(op=op):
                self.assertTrue(md.ARRANGE_LABELS[op])

    def test_画面のボタンとサーバの並べ方がずれていない(self) -> None:
        """ボタンにあってサーバに無い並べ方は、押すと断られるだけになる。"""
        import re
        html = (_ROOT / "app" / "templates" / "_arrange.html").read_text(encoding="utf-8")
        buttons = set(re.findall(r'data-arrange="([a-z_]+)"', html))
        self.assertEqual(buttons, set(md.ARRANGE_OPS))


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
