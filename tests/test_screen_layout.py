"""画面の骨格 ── 入力と結果が行ったり来たりしないこと

【何を守っているか】
VBAのフォームは1本の列に「入力 → 結果 → また入力」と積まれていた。
資材選択がその典型で、配置図(結果)がパレット・製品・ボード(入力)と
アングル(入力)の**あいだ**に挟まっていた。図を見るには操作を全部
スクロールして通り過ぎ、直すには戻る ── 1回直すたびに視線と手が往復する。

設計指針 §4.2 が定めている形は決まっている。

    結果は左(スクロールしてよい) / 操作は右(スクロールさせない)

**この形は目で見ないと崩れたことに気づけない**ので、ここで機械が見る。
「テンプレートに `.split` があるか」「結果カードが結果カラムに居るか」
という構造の検査で、見た目そのものは Playwright の実確認が受け持つ。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402

_isolation.ensure_isolated()

TEMPLATES = _ROOT / "app" / "templates"

# 2カラム(結果 | 操作)にする画面と、**結果カラムに居なければならない**もの。
# ここに挙げたものが操作カラム側に移ったら試験が落ちる
TWO_COLUMN = {
    "selection.html": ["planCard"],          # 配置図
    "inventory.html": ["mapCard", "listCard"],  # 保管位置の図・在庫一覧(3列)
    "layout.html": ["map"],                  # 配置図(SVG)
    "warehouse.html": ["rows"],              # 発注一覧
}

# 倉庫連携だけは書く順が逆(操作=送信フォームが先、結果=一覧が後ろ)。
# 現場の要望で、この画面は「一覧を眺める」より先に「送る」が主な作業
# なため、意図して他画面と順序を変えている(§4.2 の例外として明記)
# 簡易在庫も同じく操作(検索)が先。**検索 | 図 | 在庫一覧 の3列**で、
# 図と一覧を同時に見せる(現場の声:「位置を押すと図が裏に回る。
# 検索・map・在庫で縦に区切るほうがよい」。VBA の UFMAP も同じ並び)
REVERSED_ORDER = {"warehouse.html", "inventory.html"}

# 1カラムのままでよい画面。**結果しか無い**か、設定のように
# 順に読むものなので、左右に分ける理由がない
ONE_COLUMN = ("log.html", "settings.html", "lot.html")


def read(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def block(text: str, start: str, end: str) -> str:
    """`start` から `end` の手前までを切り出す。"""
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


class TwoColumnTests(unittest.TestCase):
    def test_結果と操作が左右に分かれている(self) -> None:
        for name in TWO_COLUMN:
            with self.subTest(screen=name):
                text = read(name)
                self.assertIn('class="split', text,
                              f"{name}: 2カラムになっていません")
                self.assertIn('split__results', text)
                self.assertIn('split__controls', text)

    def test_結果は結果カラムに居る(self) -> None:
        """**結果が操作のあいだに挟まっていないこと。**

        挟まっていると、図を見るのに操作を通り過ぎることになる。
        """
        for name, ids in TWO_COLUMN.items():
            text = read(name)
            if name in REVERSED_ORDER:
                # 操作(controls)が先に書かれているので、結果は
                # split__results から末尾までが結果カラムの中身
                results = text[text.index('split__results'):]
            else:
                results = block(text, 'split__results', 'split__controls')
            for target in ids:
                with self.subTest(screen=name, id=target):
                    self.assertIn(f'id="{target}"', results,
                                  f"{name}: {target} が結果カラムの外にあります")

    def test_操作カラムは結果より後ろに書く(self) -> None:
        """読み上げとタブ移動の順序も「結果 → 操作」にそろえる。

        `REVERSED_ORDER` に挙げた画面は例外(下のテストで別に確かめる)。
        """
        for name in TWO_COLUMN:
            if name in REVERSED_ORDER:
                continue
            with self.subTest(screen=name):
                text = read(name)
                self.assertLess(text.index('split__results'),
                                text.index('split__controls'))

    def test_倉庫連携は操作カラムが結果より先に書く(self) -> None:
        """送信フォームが主な作業なので、この画面だけ順序が逆(意図的)。"""
        for name in REVERSED_ORDER:
            with self.subTest(screen=name):
                text = read(name)
                self.assertLess(text.index('split__controls'),
                                text.index('split__results'))

    def test_1カラムのままでよい画面は分けない(self) -> None:
        """分ける理由が無いのに分けると、片側が空いて落ち着かない。"""
        for name in ONE_COLUMN:
            with self.subTest(screen=name):
                self.assertNotIn('split__controls', read(name))


class SimultaneousTests(unittest.TestCase):
    """図と中身は**同時に**見える(面で切り替えない)。

    以前は図と一覧(置き場の中身)を面(タブ)で切り替えていて、位置を押すと
    図が裏に回った(現場の声:「同時に表示させてほしい」)。
    """

    def test_簡易在庫は図と一覧を面で分けない(self) -> None:
        text = read("inventory.html")
        self.assertNotIn('data-tabs="inventory-result"', text)
        self.assertLess(text.index('id="controlCol"'), text.index('id="mapCard"'))
        self.assertLess(text.index('id="mapCard"'), text.index('id="listCard"'))

    def test_棚検索は図と中身を面で分けない(self) -> None:
        text = read("layout.html")
        self.assertNotIn('data-tabs="layout-result"', text)
        # 中身は右の列(検索の下)
        controls = text[text.index('split__controls'):]
        self.assertIn('id="materialRows"', controls)


class SpacingTests(unittest.TestCase):
    """間隔は列の `gap` が作る。個々の `margin-top` を撒かない。

    撒くと、カードを足したり順を変えたりするたびに margin を直すことに
    なり、直し忘れたぶんだけ間隔が不揃いになる。
    """

    def test_列の中にmargin_topを撒かない(self) -> None:
        for name in TWO_COLUMN:
            with self.subTest(screen=name):
                text = read(name)
                columns = text[text.index('split__results'):]
                # 未取り込みの帯だけは例外(カードではなく差し込みの警告)
                found = [m for m in re.findall(
                    r'<section[^>]*style="margin-top:12px"', columns)]
                self.assertEqual(found, [], f"{name}: {found}")


class ScrollRuleTests(unittest.TestCase):
    """スクロールの規則(§4.2)を CSS 側で固定する。"""

    def setUp(self) -> None:
        self.css = (_ROOT / "app" / "static" / "css" / "layout.css").read_text(
            encoding="utf-8")

    def test_ページ全体はスクロールしない(self) -> None:
        """現在地を見失う。"""
        self.assertIn("html, body { height: 100%; overflow: hidden; }", self.css)

    def test_結果カラムはスクロールしてよい(self) -> None:
        self.assertRegex(self.css, r"\.split__results\s*\{[^}]*overflow:\s*auto")

    def test_操作カラムはスクロールできる(self) -> None:
        """**隠すより動かせるほうがまし。**

        もとは `overflow: hidden`(操作は探すものだから動かさない)で、
        画面が低いときだけスクロールを許していた。ところが一覧
        (パレット・ボード・アングル)を開いたままにできるようになって、
        高い画面でも下の設定に届かなくなった ── 現場の声「資材選択に
        スクロールがないから設定できない。一覧が開くのは良いことなので、
        スクロールバーを足してほしい」。

        消えた操作には気づけないが、スクロールバーは「まだ下がある」と
        言ってくれる(§4.2 の但し書き)。
        """
        self.assertRegex(self.css,
                         r"\.split__controls\s*\{[^}]*overflow-y:\s*auto")
        # 高さで出し分けない(高い画面でも入りきらないことがある)
        self.assertNotRegex(
            self.css,
            r"@media \(max-height: \d+px\)\s*\{[^}]*"
            r"\.split__controls\s*\{[^}]*overflow")

    def test_画面ごとにこの規則を上書きしない(self) -> None:
        """**「入りきらない」を `overflow` で隠さない。**

        簡易在庫は `overflow-y: auto` を自前で持っていて、操作の列が
        1366幅で 121px はみ出していることを覆い隠していた。入りきらない
        なら、面(受け入れ / 払い出し)で分けるのが答え。
        """
        for name in TWO_COLUMN:
            with self.subTest(screen=name):
                self.assertNotRegex(
                    read(name),
                    r"\.split__controls\s*\{[^}]*overflow[^}]*auto")


class CardHeaderTests(unittest.TestCase):
    """カードの見出しは**入りきらないものを潰さない**。"""

    def test_見出しは折り返す(self) -> None:
        """折り返さないと、幅の足りない分を全部の子が分け合って縮み、
        凡例の字が1文字ずつ縦に割れ、後ろのボタンは行から押し出されて
        見えなくなる ── 現場の声「配置図からコントロールが消えた」。
        配置編集を入れて見出しの中身が増えたときに出た。
        """
        css = (_ROOT / "app" / "static" / "css" / "components.css").read_text(
            encoding="utf-8")
        self.assertRegex(css, r"\.card > header \{[^}]*flex-wrap:\s*wrap")


class StepDisclosureTests(unittest.TestCase):
    """資材選択の段階的開示。

    5段すべてを開くと 1,489px になり、FHD 100%(894px)でも
    入りきらない。開くのは**いまの段だけ**にする(§1.3)。
    """

    def setUp(self) -> None:
        self.html = read("selection.html")

    def test_いまの段以外は畳む(self) -> None:
        self.assertIn(
            '.step:not([data-state="current"]):not(.is-open) > .pad { display: none; }',
            self.html)

    def test_畳んだ段も見出しを押せば開く(self) -> None:
        """直したくなったときに辿り着けなくならない。"""
        self.assertIn('.step:not([data-state="current"]) > header { cursor: pointer; }',
                      self.html)

    def test_実績パターンは畳まない(self) -> None:
        """**呼び出しは日常操作。** 認証が要るのは保存だけ。

        もとは「管理者」として畳んであり、パスワード欄がそこにあったので
        開く理由があった。その欄を設定画面へ移したとたん開く理由が消え、
        **過去の実績を呼び出す機能ごと見えなくなった**
        ── 現場の声「PalletPatternsからみる機能なくなりました?」。
        """
        self.assertNotIn('card--admin foldable', self.html)
        self.assertIn("実績パターン", self.html)
        # 呼び出しの入口(表)と、保存の入口が両方ある
        self.assertIn('id="patternRows"', self.html)
        self.assertIn('id="savePattern"', self.html)

    def test_段の状態はサーバから来る(self) -> None:
        """画面が条件を組み立て直さない(判断を2か所に置かない)。"""
        for key in ("size", "boards", "angles", "outputs"):
            with self.subTest(step=key):
                self.assertIn(f'data-state="{{{{ step.{key}.state }}}}"', self.html)


if __name__ == "__main__":
    unittest.main()
