"""配置編集の複数選択(Shift+クリック)を、棚検索でも簡易在庫と同じにする (VER2.83.0)

現場の声:「簡易在庫の配置編集は複数掴みも普通にできる。棚検索の配置
編集はできない(たまにできる)」「shift+で同時つかみが、簡易在庫は太線に
なるが、棚検索は点線のまま」。

1. **選んだ見た目が編集中の破線に負けていた。** 棚検索は
   `.shelf.is-multi rect` と `.map--editing .shelf rect` が同じ強さで、
   あとに書いた破線が勝っていた(簡易在庫は `#map` 付きで勝っていた)
2. **掴みしろの上の Shift+クリックが「1つだけ掴む」に入り、選んでいた
   箱が全部外れていた。** 棚検索の置き場は 12×12 まであり、掴みしろ(7)
   が箱の半分以上を占める ── 「たまにできる」の正体
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = _ROOT / "app" / "templates"
MAPEDIT = _ROOT / "app" / "static" / "js" / "mapedit.js"


def _specificity(selector: str) -> tuple[int, int, int]:
    """CSS の強さ(id, class, 要素)。ここで使う単純な選択子だけ数えられればよい。"""
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+", selector))
    elements = len(re.findall(r"(?:^|\s)([a-z]+)(?=$|[\s.#:\[])", selector))
    return ids, classes, elements


def _rules(html: str) -> list[tuple[int, str, str]]:
    """<style> の中の (位置, 選択子, 中身)。Jinja の注釈は落とす。"""
    css = re.sub(r"\{#.*?#\}", "", html, flags=re.S)
    return [(m.start(), m.group(1).strip(), m.group(2))
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


class SelectedLooksSelectedTests(unittest.TestCase):
    """**選んだ箱は編集中の破線に勝つ。** 両方の画面で機械で確かめる。"""

    CASES = (("layout.html", "shelf"), ("inventory.html", "pos"))

    def test_選んだ見た目が破線に勝つ(self) -> None:
        for name, box in self.CASES:
            with self.subTest(name):
                rules = _rules((TEMPLATES / name).read_text(encoding="utf-8"))
                multi = [(at, sel) for at, sel, body in rules
                         if f".{box}.is-multi" in sel and "dasharray" in body]
                dashed = [(at, sel) for at, sel, body in rules
                          if "map--editing" in sel and f".{box}" in sel
                          and "dasharray" in body and "is-multi" not in sel]
                self.assertTrue(multi and dashed, "規則が見つかりません")
                (m_at, m_sel), (d_at, d_sel) = multi[-1], dashed[-1]
                m, d = _specificity(m_sel), _specificity(d_sel)
                self.assertTrue(m > d or (m == d and m_at > d_at),
                                f"{m_sel} {m} が {d_sel} {d} に負けます")


class ShiftClickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.js = MAPEDIT.read_text(encoding="utf-8")

    def test_掴みしろの上でもShiftなら選ぶ(self) -> None:
        self.assertIn("if (event.shiftKey) {", self.js)
        self.assertNotIn('event.shiftKey && !event.target.classList.contains("grip")',
                         self.js)

    def test_掴みしろは箱に合わせて小さくなる(self) -> None:
        """固定の 7 のままだと、細い棚は箱の大半が掴みしろになる。"""
        self.assertIn("export function gripSize(w, h)", self.js)
        # 描くときも、掴んで動かしているあいだも同じ大きさ
        self.assertEqual(self.js.count("gripSize("), 3)
        self.assertNotRegex(self.js, r"item\.w - GRIP|w - GRIP")


if __name__ == "__main__":
    unittest.main()
