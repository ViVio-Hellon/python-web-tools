"""帳票のHTML生成(printing)のユニットテスト。

印刷そのもの(OSのプリンタ)は環境依存なので、生成されるHTMLと
ページ設定CSSだけを検証する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import printing as p


class PageSetupTests(unittest.TestCase):
    def test_余白0を頼んでも紙の端から5mm内側に描く(self):
        """プリンターは紙の縁から約4mmに印刷できない。余白0の帳票は、画面では
        収まって見えても紙では縁が欠ける(現場の指摘)。下限をかける。"""
        css = p.PageSetup(orientation=p.LANDSCAPE, margin_mm=0).to_css()
        self.assertIn("size: A4 landscape", css)
        self.assertIn(f"margin: {p.SAFE_MARGIN_MM}mm", css)
        self.assertGreaterEqual(p.SAFE_MARGIN_MM, 5.0)

    def test_既定の余白も5mm以上(self):
        self.assertGreaterEqual(p.PageSetup().effective_margin_mm, 5.0)

    def test_広い余白はそのまま(self):
        css = p.PageSetup(orientation=p.PORTRAIT, margin_mm=10).to_css()
        self.assertIn("A4 portrait", css)
        self.assertIn("margin: 10.0mm", css)

    def test_画面のプレビューは紙と同じ大きさと余白(self):
        """以前はプレビューの余白が8mm固定で、余白0の帳票は紙いっぱいに描いていた。"""
        css = p.PageSetup(orientation=p.LANDSCAPE, margin_mm=0).screen_css()
        self.assertIn("width: 297.0mm", css)
        self.assertIn("min-height: 210.0mm", css)
        self.assertIn(f"padding: {p.SAFE_MARGIN_MM}mm", css)
        # 印刷できる範囲の境目を点線で見せる(画面だけ)
        self.assertIn("dashed", css)
        self.assertIn("@media screen", css)


class RenderTests(unittest.TestCase):
    def test_each_sheet_becomes_a_page(self):
        report = p.Report(title="t")
        report.add_sheet("<p>1</p>")
        report.add_sheet("<p>2</p>")
        html = p.render_html(report)
        self.assertEqual(html.count('class="sheet"'), 2)
        self.assertIn("page-break-after", html)

    def test_title_is_escaped(self):
        html = p.render_html(p.Report(title="<x>&"))
        self.assertIn("&lt;x&gt;&amp;", html)
        self.assertNotIn("<x>", html)

    def test_page_setup_css_is_included(self):
        report = p.Report(title="t", setup=p.PageSetup(orientation=p.PORTRAIT))
        self.assertIn("A4 portrait", p.render_html(report))

    def test_extra_css_is_appended(self):
        report = p.Report(title="t", setup=p.PageSetup(extra_css=".x{color:red}"))
        self.assertIn(".x{color:red}", p.render_html(report))

    def test_screen_hint_is_not_printed(self):
        html = p.render_html(p.Report(title="t"))
        self.assertIn("screen-only", html)
        self.assertIn("@media print { .screen-only { display: none; } }", html)

    def test_印刷するボタンと閉じるボタンがあり紙には出ない(self):
        html = p.render_html(p.Report(title="t"))
        self.assertIn('<button type="button" class="printbar__go" id="printNow">', html)
        self.assertIn('id="printClose"', html)
        self.assertIn("@media print { .printbar { display: none !important; } }", html)
        # 直している途中の欄は、確定させてから印刷する(直した内容を取りこぼさない)
        self.assertIn('closest("[data-edit]")) editing.blur();', html)
        # 帳票の先頭(紙より前)に置く
        self.assertLess(html.index('id="printNow"'), html.index('<div class="sheet">')
                        if '<div class="sheet">' in html else len(html))


class EscapeTests(unittest.TestCase):
    def test_escapes_markup(self):
        self.assertEqual(p.escape('<a href="x">'), "&lt;a href=&quot;x&quot;&gt;")

    def test_none_becomes_blank(self):
        self.assertEqual(p.escape(None), "")

    def test_numbers_pass_through(self):
        self.assertEqual(p.escape(1150), "1150")


class TableTests(unittest.TestCase):
    def test_rows_and_header(self):
        html = p.table([[1, 2]], header=["A", "B"])
        self.assertIn("<th>A</th>", html)
        self.assertIn("<td>1</td>", html)

    def test_widths_produce_colgroup(self):
        html = p.table([[1]], widths=["30%"])
        self.assertIn('<col style="width:30%">', html)

    def test_cell_values_are_escaped(self):
        self.assertIn("&lt;b&gt;", p.table([["<b>"]]))

    def test_label_pairs_fill_incomplete_rows(self):
        html = p.label_pairs([("A", 1), ("B", 2), ("C", 3)], columns=2)
        # 3項目/2列 → 2行目は空セルで埋まる
        self.assertEqual(html.count("<tr>"), 2)
        self.assertIn("<td>C</td>", html)


if __name__ == "__main__":
    unittest.main()


class TemplateMarginTests(unittest.TestCase):
    """帳票の部品を通らずに `@page` を書いている画面(配置図印刷など)も、
    紙の端から5mm以上内側に描く。"""

    def test_テンプレートの余白も5mm以上(self):
        import re
        root = Path(__file__).resolve().parent.parent / "app" / "templates"
        found = []
        for path in root.rglob("*.html"):
            for m in re.finditer(r"@page\s*\{[^}]*margin:\s*([\d.]+)mm", path.read_text(encoding="utf-8")):
                found.append((path.name, float(m.group(1))))
        self.assertTrue(found, "前提: 配置図印刷のテンプレートに @page がある")
        for name, mm in found:
            with self.subTest(name=name):
                self.assertGreaterEqual(mm, p.SAFE_MARGIN_MM)
