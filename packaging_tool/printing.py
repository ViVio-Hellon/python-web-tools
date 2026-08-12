"""帳票のHTML生成 (VBA の Excel帳票 + `PrintOut` の代替)

VBA版は Excel を「レイアウトエンジン + プリンタドライバ」として使っていた。

    Set ws = Worksheets.Add(...)          ' シートを新規作成して値と罫線を置く
    With ws.PageSetup
        .Orientation = xlLandscape
        .PaperSize   = xlPaperA4
        .Zoom = False : .FitToPagesWide = 1 : .FitToPagesTall = 1
        .TopMargin = 0 ...
        .PrintArea = "$A$1:$T$7"
    End With
    ws.PrintOut Copies:=1, Preview:=False

Python版はExcelに依存せず、**HTML + 印刷用CSS** で同じ紙面を作る。
ページ設定はCSSの `@page` がほぼ1対1で対応する。

    .Orientation = xlLandscape  ->  @page { size: A4 landscape; }
    .TopMargin = 0 等            ->  @page { margin: 0; }
    .FitToPagesWide = 1          ->  幅100%のテーブルで組む
    .PrintArea                   ->  .sheet ブロック1つ = 1ページ

**ここが作るのは文字列だけ**で、ファイルにも書かないし印刷も起こさない。
Web版では `app/routes/selection.py` が `render_html()` の結果をそのまま
応答として返し、利用者がブラウザの印刷(Ctrl+P)で出す。
tkinter版のころは一時ファイルへ書いてOSの印刷動詞に渡していたが、
その経路はもう誰も通らないので落とした。

この方式の利点:
    - 追加インストール不要(標準ライブラリだけ)
    - Excelが入っていない端末でも印刷できる
    - ブラウザの「PDFとして保存」で控えが残せる
    - 帳票レイアウトの調整がCSSで完結する

貼付用ラベルのように「現物と寸法がぴったり合う」必要がある帳票は、
ブラウザの余白設定に左右されるため実機で確認すること。
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .logging_utils import get_logger

log = get_logger("printing")

# 用紙の向き(VBA `xlLandscape` / `xlPortrait`)
LANDSCAPE = "landscape"
PORTRAIT = "portrait"


@dataclass
class PageSetup:
    """VBA `PageSetup` に対応する紙面設定。"""

    paper: str = "A4"
    orientation: str = LANDSCAPE
    margin_mm: float = 0.0
    # 追加のスタイル(帳票ごとの罫線・フォント等)
    extra_css: str = ""

    def to_css(self) -> str:
        return (
            f"@page {{ size: {self.paper} {self.orientation}; "
            f"margin: {self.margin_mm}mm; }}"
        )


@dataclass
class Report:
    """1つの帳票。`sheets` の1要素が1ページになる。"""

    title: str
    sheets: list[str] = field(default_factory=list)
    setup: PageSetup = field(default_factory=PageSetup)

    def add_sheet(self, body_html: str) -> None:
        self.sheets.append(body_html)


BASE_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { font-family: "Meiryo UI", "Yu Gothic UI", sans-serif; color: #000; }
.sheet { page-break-after: always; }
.sheet:last-child { page-break-after: auto; }
table.form { width: 100%; border-collapse: collapse; table-layout: fixed; }
table.form th, table.form td { border: 1px solid #000; padding: 2px 4px; }
table.form th { background: #f0f0f0; font-weight: bold; }
.right { text-align: right; }
.center { text-align: center; }
.big { font-size: 28pt; font-weight: bold; }
@media screen {
  body { background: #e5e7eb; padding: 12px; }
  .sheet { background: #fff; margin: 0 auto 12px; padding: 8mm;
           box-shadow: 0 1px 4px rgba(0,0,0,.3); }
  .screen-only { margin: 0 auto 12px; max-width: 900px; color: #374151;
                 font-size: 12px; }
}
@media print { .screen-only { display: none; } }
"""

# 画面で見たときだけ出る操作案内(印刷はされない)
_PRINT_HINT = (
    '<p class="screen-only">この画面で <b>Ctrl+P</b> を押すと印刷できます。'
    "印刷ダイアログで用紙・余白がページ設定どおりか確認してください"
    "(「背景のグラフィック」を有効にすると網掛けも印刷されます)。</p>"
)


def escape(value: object) -> str:
    """帳票に値を埋めるときのエスケープ。"""
    return html.escape("" if value is None else str(value))


def render_html(report: Report) -> str:
    """帳票をHTML文字列にする。"""
    sheets = "\n".join(f'<div class="sheet">{s}</div>' for s in report.sheets)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ja"><head><meta charset="utf-8">'
        f"<title>{escape(report.title)}</title>"
        f"<style>{report.setup.to_css()}\n{BASE_CSS}\n{report.setup.extra_css}</style>"
        f"</head><body>{_PRINT_HINT}{sheets}</body></html>"
    )


# ------------------------------------------------------------------
# 帳票を組み立てるときの小道具
# ------------------------------------------------------------------
def table(rows: Sequence[Sequence[object]], *, header: Optional[Sequence[object]] = None,
          widths: Optional[Sequence[str]] = None, css_class: str = "form") -> str:
    """罫線付きの表を作る(Excelのセル罫線に相当)。"""
    parts = [f'<table class="{css_class}">']
    if widths:
        parts.append("<colgroup>")
        parts.extend(f'<col style="width:{w}">' for w in widths)
        parts.append("</colgroup>")
    if header:
        parts.append("<thead><tr>")
        parts.extend(f"<th>{escape(c)}</th>" for c in header)
        parts.append("</tr></thead>")
    parts.append("<tbody>")
    for row in rows:
        parts.append("<tr>")
        parts.extend(f"<td>{escape(c)}</td>" for c in row)
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def label_pairs(pairs: Sequence[tuple[str, object]], *, columns: int = 2) -> str:
    """「項目名: 値」の並びを表で組む(VBAのラベル+値のセル配置に相当)。"""
    rows: list[list[object]] = []
    current: list[object] = []
    for caption, value in pairs:
        current.extend([caption, value])
        if len(current) >= columns * 2:
            rows.append(current)
            current = []
    if current:
        current.extend([""] * (columns * 2 - len(current)))
        rows.append(current)
    return table(rows)
