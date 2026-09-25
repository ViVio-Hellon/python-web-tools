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
import json
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .logging_utils import get_logger

log = get_logger("printing")

# 用紙の向き(VBA `xlLandscape` / `xlPortrait`)
LANDSCAPE = "landscape"
PORTRAIT = "portrait"


# **紙の端からこれより内側にしか描かない(mm)。**
#
# プリンターは紙の縁から約4mmには印刷できない(給紙の都合で、機種によっては
# もっと広い)。余白0で作った帳票は、画面のプレビューでは紙いっぱいに収まって
# 見えるが、紙では縁の罫線や文字が欠ける(現場の指摘)。VBA版はExcelの余白0を
# そのまま使っていたので、同じ欠け方をしていたはず。
#
# 帳票ごとの `margin_mm` がこれより小さくても、**ここで下限をかける** ──
# 新しい帳票を足したときに余白0を書いても、紙で欠けない。
#
# **5mm ちょうどにしない。** 縁の罫線は余白の線の上に乗るので、描き方(にじみ)
# によっては 4.9mm に掛かる(PDF にして測ったら実際にそうなった)。プリンターの
# 個体差も見て 1mm 足す。現場の求めは「5mm以上内側」
SAFE_MARGIN_MM = 6.0

# 用紙の大きさ(縦置きの 幅, 高さ mm)。画面のプレビューを紙と同じ大きさで描く
PAPER_MM = {"A4": (210.0, 297.0), "A3": (297.0, 420.0), "B5": (182.0, 257.0)}


@dataclass
class PageSetup:
    """VBA `PageSetup` に対応する紙面設定。"""

    paper: str = "A4"
    orientation: str = LANDSCAPE
    # 余白。`SAFE_MARGIN_MM` より小さくしても、それより内側には寄らない
    margin_mm: float = SAFE_MARGIN_MM
    # 追加のスタイル(帳票ごとの罫線・フォント等)
    extra_css: str = ""

    @property
    def effective_margin_mm(self) -> float:
        """実際にかける余白。**下限は `SAFE_MARGIN_MM`。**"""
        return max(float(self.margin_mm), SAFE_MARGIN_MM)

    def paper_size_mm(self) -> tuple[float, float]:
        """向きを入れた用紙の (幅, 高さ)。"""
        w, h = PAPER_MM.get(self.paper, PAPER_MM["A4"])
        return (h, w) if self.orientation == LANDSCAPE else (w, h)

    def to_css(self) -> str:
        return (
            f"@page {{ size: {self.paper} {self.orientation}; "
            f"margin: {self.effective_margin_mm}mm; }}"
        )

    def screen_css(self) -> str:
        """画面のプレビューを**紙と同じ大きさ・同じ余白**で描く。

        以前はプレビューの余白が帳票によらず8mmで、余白0の帳票は紙いっぱいに
        描いていた ── 画面では収まって見えても、紙では縁が欠けた。いまは
        用紙の大きさで描き、印刷できる範囲の境目を点線で見せる。
        """
        w, h = self.paper_size_mm()
        m = self.effective_margin_mm
        return (
            "@media screen {\n"
            f"  .sheet {{ position: relative; width: {w}mm; min-height: {h}mm;"
            f" padding: {m}mm; }}\n"
            f"  .sheet::after {{ content: ''; position: absolute; inset: {m}mm;"
            " border: 1px dashed #c7ccd4; pointer-events: none; }\n"
            "}\n"
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
  /* 大きさと余白は帳票ごと(`PageSetup.screen_css`)。紙と同じに描く */
  .sheet { background: #fff; margin: 0 auto 12px;
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


def editable(key: str, value: object, *, placeholder: str = "") -> str:
    """**その場で直せる値**にする。

    VBA版は帳票をシートに出していたので、気に入らなければシートを
    直してから印刷できました。台数が決まらない・寸法を微調整したい・
    拠点名を頭に入れたい・期日を書きたい ── どれも紙に出す前に人が
    決めることで、選定の計算とは別物です(現場の声)。

    直した内容は `data-edit` の名前で送り返し、**別のロットを検索する
    まで**そのまま残します。名前は帳票の中で一意にしてください。

    印刷には何も足しません(点線も背景も画面のときだけ)。
    """
    ph = f' data-placeholder="{escape(placeholder)}"' if placeholder else ""
    return (f'<span class="edit" contenteditable="true" spellcheck="false"'
            f' data-edit="{escape(key)}"{ph}>{escape(value)}</span>')


# 直せる欄の見た目と、直した内容を送り返す仕掛け。
# **画面のときだけ**([@media screen])── 紙には点線も案内も出さない
_EDIT_CSS = """
@media screen {
  .edit { outline: none; border-bottom: 1px dashed #9aa3ad;
          min-width: 2em; display: inline-block; cursor: text; }
  .edit:hover { background: #eef4ff; }
  .edit:focus { background: #fff7d6; border-bottom-color: #1d4ed8; }
  .edit:empty::before { content: attr(data-placeholder); color: #9aa3ad; }
  .editbar { margin: 0 auto 12px; max-width: 900px; font-size: 12px;
             color: #374151; }
  .editbar b { color: #1d4ed8; }
  .editbar .saved { color: #15803d; margin-left: .5em; }
}
@media print { .edit { border: 0; } .editbar { display: none; } }
"""

_EDIT_HINT = (
    '<p class="editbar">点線の欄は<b>その場で直せます</b>'
    "(台数・寸法・担当者・期限日・見出しの頭など)。"
    "直した内容は<b>別のロットを検索するまで</b>残ります。"
    '<span class="saved" id="editSaved"></span></p>'
)

# 直した内容をサーバへ送り返す。**帳票は独立したページ**(別窓で開く)
# なので、アプリ本体のJSは読み込まれていない。ここだけで完結させる
_EDIT_SCRIPT = """
<script>
(function () {
  var url = %(url)s, note = document.getElementById("editSaved"), timer = 0;
  if (!url) return;
  function collect() {
    var out = {};
    document.querySelectorAll("[data-edit]").forEach(function (n) {
      out[n.dataset.edit] = n.textContent.trim();
    });
    return out;
  }
  function save() {
    fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                 body: JSON.stringify({ edits: collect() }) })
      .then(function (r) {
        note.textContent = r.ok ? "保存しました" : "保存できませんでした";
        window.setTimeout(function () { note.textContent = ""; }, 2000);
      })
      .catch(function () { note.textContent = "保存できませんでした"; });
  }
  document.addEventListener("input", function (e) {
    if (!e.target.closest("[data-edit]")) return;
    window.clearTimeout(timer);
    timer = window.setTimeout(save, 600);
  });
  // 打ち終わってすぐ印刷しても取りこぼさない
  document.addEventListener("blur", function (e) {
    if (e.target.closest("[data-edit]")) { window.clearTimeout(timer); save(); }
  }, true);
  // 改行は入れさせない(1行の欄なので、入ると印刷でずれる)
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target.closest("[data-edit]")) {
      e.preventDefault(); e.target.blur();
    }
  });
}());
</script>
"""


def render_html(report: Report, *, edit_url: str = "") -> str:
    """帳票をHTML文字列にする。

    `edit_url` を渡すと、`editable()` で作った欄がその場で直せるように
    なる(直した内容はそのURLへ送り返す)。渡さなければ読むだけ。
    """
    sheets = "\n".join(f'<div class="sheet">{s}</div>' for s in report.sheets)
    extra_css = _EDIT_CSS if edit_url else ""
    hint = _EDIT_HINT if edit_url else ""
    script = (_EDIT_SCRIPT % {"url": json.dumps(edit_url)}) if edit_url else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ja"><head><meta charset="utf-8">'
        f"<title>{escape(report.title)}</title>"
        f"<style>{report.setup.to_css()}\n{BASE_CSS}\n"
        f"{report.setup.screen_css()}\n{extra_css}\n"
        f"{report.setup.extra_css}</style>"
        f"</head><body>{_PRINT_HINT}{hint}{sheets}{script}</body></html>"
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
