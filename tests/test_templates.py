"""テンプレートの構造 ── 目で見ないと気づけない崩れを機械が見る

【なぜここで見るか】
HTML の閉じ忘れはブラウザが黙って直してしまう。直した形は書いた形と
違うので、「なぜかカードが表の中に入っている」のような、原因の分から
ない崩れになる。**実際に踏んだ**(タブを足したときに `</section>` が
3枚ずれて、在庫一覧が図の中に入った)ので、ここで釣り合いを数える。

面(タブ)についても同じで、`aria-controls` の指す先が無い・既定の面が
無いといった間違いは、**開いてみるまで分からない**。開かないタブは
中身が消えたのと同じなので、ここで結び付きを確かめる。
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

# 終了タグの要らないもの。`option` は常に省いて書いている
VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr", "option",
})
TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*?(/?)>", re.S)


def strip_noise(text: str) -> str:
    """Jinja のコメントと `<script>` / `<style>` の中身を外す。

    中に `</div>` を含む文字列があると、数え間違える。
    """
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    return re.sub(r"<(script|style)\b.*?</\1>", "", text, flags=re.S)


def unbalanced(text: str) -> list[str]:
    """釣り合っていないタグを行番号付きで返す。"""
    text = strip_noise(text)
    stack: list[tuple[str, int]] = []
    bad: list[str] = []
    for match in TAG.finditer(text):
        closing, name, self_closing = match.group(1), match.group(2).lower(), match.group(3)
        if name in VOID or self_closing:
            continue
        line = text[:match.start()].count("\n") + 1
        if not closing:
            stack.append((name, line))
            continue
        if stack and stack[-1][0] == name:
            stack.pop()
            continue
        found = next((i for i in range(len(stack) - 1, -1, -1)
                      if stack[i][0] == name), None)
        if found is None:
            bad.append(f"{line}行: </{name}> に対応する開始がない")
        else:
            for nm, ln in stack[found + 1:]:
                bad.append(f"{ln}行: <{nm}> が閉じていない"
                           f"(</{name}> {line}行 で打ち切られた)")
            del stack[found:]
    bad.extend(f"{ln}行: <{nm}> が閉じていない" for nm, ln in stack)
    return bad


class BalanceTests(unittest.TestCase):
    def test_文字の大きさは階段からしか取らない(self) -> None:
        """0.5px 違いは差として読めず、揃っていないことだけが伝わる。

        以前は18種類あった。6段に畳んだので、直に px を書かないこと。
        例外は「字」ではなく「形」のものだけ ── SVG の中の文字
        (`viewBox` の利用者単位)と、記号1文字の大きさ。
        """
        allowed = ("shelf text", "area text", ".chip::before", ".sortmark")
        css = [*(_ROOT / "app" / "static" / "css").glob("*.css"),
               *TEMPLATES.glob("*.html")]
        bad = []
        for path in css:
            for no, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                if any(a in line for a in allowed):
                    continue
                if re.search(r"font-size: *[0-9.]+px", line):
                    bad.append(f"{path.name}:{no} {line.strip()[:60]}")
        self.assertEqual(bad, [], "階段(--fs-*)を使ってください:\n  " + "\n  ".join(bad))

    def test_同じidを2度書かない(self) -> None:
        """同じ `id` が2つあると、片方はJSから触れず**残り続ける**。

        資材選択の「EX受注のロットを引くと使えます」が2行出ていた
        (同じ `<p id="exOnlyWhy">` が並んでいた)。
        """
        pat = re.compile(r'\bid="([A-Za-z][\w-]*)"')
        for path in sorted(TEMPLATES.glob("*.html")):
            text = strip_noise(path.read_text(encoding="utf-8"))
            seen: dict[str, int] = {}
            for name in pat.findall(text):
                seen[name] = seen.get(name, 0) + 1
            twice = sorted(n for n, k in seen.items() if k > 1)
            with self.subTest(template=path.name):
                self.assertEqual(twice, [], f"{path.name}: 同じidが2つあります")

    def test_階段は6段そろっている(self) -> None:
        """段が抜けると、抜けた役割の分だけ直書きに戻る。"""
        tokens = (_ROOT / "app" / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
        for step in ("--fs-2xs", "--fs-xs", "--fs-sm", "--fs-md", "--fs-lg", "--fs-xl"):
            with self.subTest(step=step):
                self.assertRegex(tokens, rf"{step}: *[0-9]+px")

    def test_押すものは44pxを割らない(self) -> None:
        """表の行は詰めてよいが、**押す的は詰めない**(§1.2)。"""
        css = (_ROOT / "app" / "static" / "css" / "components.css").read_text(encoding="utf-8")
        btn = css[css.index(".btn {"):css.index(".btn[hidden]")]
        self.assertIn("min-height: 44px", btn)
        self.assertIn("min-width: 44px", btn)

    def test_面の表示を上書きするなら閉じたときも面倒を見る(self) -> None:
        """`#panel-… { display: … }` は `.tabpanel[hidden]` より強い。

        id の指定(1-0-0)が `.tabpanel[hidden]`(0-2-0)に勝つので、
        素で `display:flex` と書くと**閉じているはずの面が出たまま**に
        なる。実際、マスタ管理の面が取り込み元の面に重なって出た。
        `:not([hidden])` を付けるか、`display` に触らないこと。
        """
        rule = re.compile(r"#panel-[a-z-]+(?P<rest>[^{]*)\{(?P<body>[^}]*)\}")
        for path in sorted(TEMPLATES.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            for found in rule.finditer(text):
                if "display" not in found.group("body"):
                    continue
                with self.subTest(template=path.name,
                                  rule=found.group(0)[:60]):
                    self.assertIn("[hidden]", found.group("rest"),
                                  "閉じたときに消える書き方にしてください")

    def test_タグの釣り合いが取れている(self) -> None:
        for path in sorted(TEMPLATES.glob("*.html")):
            with self.subTest(template=path.name):
                bad = unbalanced(path.read_text(encoding="utf-8"))
                self.assertEqual(bad, [], f"{path.name}:\n  " + "\n  ".join(bad))


try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

TABBED_PAGES = ("/selection", "/inventory", "/layout", "/settings")
# 現場モードの7画面。**どれを開いても**帯の左端は同じでなければならない
ALL_PAGES = ("/lot", "/selection", "/inventory", "/warehouse", "/layout",
             "/log", "/settings")


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class BrandTests(unittest.TestCase):
    """帯の左端 ── アプリ名と版のバッジ。

    **描いたあとのHTMLで見る。** テンプレートに書いてあることは
    `test_版はどの画面でも消えない` が見ているが、それだけでは
    「描いたら消えていた」を捕まえられない(値が空になる・条件で
    落ちる・別のブロックに上書きされる)。

    端末はフォルダごとコピーして配るので、「その端末に入っているのは
    どれか」を電話で聞かれる。画面の左上を読んでもらえば答えられる、が
    成り立たなくなると、そのたびにこちらが調べに行くことになる。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from app import create_app
        from packaging_tool import app_config

        app_config.load(force=True)
        cls.label = app_config.version_label()
        cls.name = app_config.display_name()
        app = create_app(token="test-token", port=8795)
        app.config["READY"] = True
        client = app.test_client()
        cls.pages = {}
        for path in ALL_PAGES:
            res = client.get(path, headers={"X-Tool-Token": "test-token"})
            assert res.status_code == 200, f"{path}: HTTP {res.status_code}"
            cls.pages[path] = res.get_data(as_text=True)

    def test_どの画面にも名前と版が出る(self) -> None:
        for path, text in self.pages.items():
            with self.subTest(screen=path):
                self.assertIn('class="brand"', text)
                self.assertIn(self.name, text)
                self.assertIn(self.label, text)

    def test_版は名前の隣にある(self) -> None:
        """離れたところに出ると「何の番号か」が読み取れない。"""
        block = re.compile(
            r'<div class="brand">(.*?)</div>', re.S)
        for path, text in self.pages.items():
            with self.subTest(screen=path):
                found = block.search(text)
                self.assertIsNotNone(found, f"{path}: 帯の左端がありません")
                self.assertIn(self.name, found.group(1))
                self.assertIn(self.label, found.group(1))

    def test_帯のいちばん左にある(self) -> None:
        """スロット(Lot・パレット…)より前。**画面の持ち主を示す場所**。"""
        for path, text in self.pages.items():
            with self.subTest(screen=path):
                self.assertLess(text.index('class="brand"'),
                                text.index('class="ribbon__slots"'))

    def test_起動待機画面にも出る(self) -> None:
        """いちばん最初に見る画面。ここで版が読めると問い合わせが減る。"""
        from app import create_app

        app = create_app(token="test-token", port=8796)
        app.config["READY"] = False              # まだ起動中
        res = app.test_client().get("/", headers={"X-Tool-Token": "test-token"})
        text = res.get_data(as_text=True)
        self.assertIn(self.label, text)


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class TabTests(unittest.TestCase):
    """面(タブ)の結び付き。

    ・`aria-controls` の指す面が同じ画面にあること
    ・`aria-labelledby` が指すタブがあること
    ・**既定の面が1つだけ**あること(無いと、初回に何も開かない)

    **描いたあとのHTMLで見る。** 設定画面のタブは `{% for %}` で
    組み立てるので、テンプレートの文字だけでは結び付きを追えない。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from app import create_app
        app = create_app(token="test-token", port=8793)
        app.config["READY"] = True
        client = app.test_client()
        cls.pages = {}
        for path in TABBED_PAGES:
            res = client.get(path, headers={"X-Tool-Token": "test-token"})
            assert res.status_code == 200, f"{path}: HTTP {res.status_code}"
            cls.pages[path] = res.get_data(as_text=True)

    def screens(self):
        return self.pages.items()

    def test_タブは実在する面を指す(self) -> None:
        for name, text in self.screens():
            panels = set(re.findall(r'class="tabpanel[^"]*"\s+id="([^"]+)"', text))
            controls = re.findall(r'aria-controls="([^"]+)"', text)
            self.assertTrue(controls, f"{name}: タブが1つも無い")
            for target in controls:
                with self.subTest(screen=name, panel=target):
                    self.assertIn(target, panels,
                                  f"{name}: {target} という面がありません")

    def test_面は実在するタブを指す(self) -> None:
        for name, text in self.screens():
            ids = set(re.findall(r'id="(tab-[^"]+)"', text))
            for source in re.findall(r'aria-labelledby="(tab-[^"]+)"', text):
                with self.subTest(screen=name, tab=source):
                    self.assertIn(source, ids,
                                  f"{name}: {source} というタブがありません")

    def test_どの組にも既定の面がある(self) -> None:
        """既定が無いと、前に見た面を覚えていない初回に何も開かない。"""
        for name, text in self.screens():
            # 1画面に組は1つ。増えたらここも数え直す
            groups = re.findall(r'class="tabs"\s+data-tabs="([^"]+)"', text)
            self.assertTrue(groups, f"{name}: data-tabs が付いていない")
            with self.subTest(screen=name):
                self.assertEqual(text.count('data-default="1"'), len(groups),
                                 f"{name}: 既定の面が組の数と合いません")

    def test_面には見出しの印を置く場所がある(self) -> None:
        """**中身の状態を見出しが背負う**(問題を隠さない)。"""
        for name, text in self.screens():
            with self.subTest(screen=name):
                tabs = len(re.findall(r'class="tab"[^>]*role="tab"', text))
                self.assertTrue(tabs, f"{name}: タブが1つも無い")
                self.assertEqual(tabs, text.count('class="tab__badge"'),
                                 f"{name}: 印の場所が無いタブがあります")

    def test_描いた時点で面はどれか1つが開いている(self) -> None:
        """JSが動く前から読める。開くのを待たせない。"""
        for name, text in self.screens():
            with self.subTest(screen=name):
                # 既定の面は `hidden` を外して描く … ではなく、JSが開く。
                # ここで見るのは「開く先が決まっていること」
                self.assertIn('data-default="1"', text)


class ShellSwapTests(unittest.TestCase):
    """外枠を保つ遷移(`app/static/js/nav.js`)が成り立つ条件。

    差し替えは「`<main id="main">` を入れ替え、`#pagescripts` の中を
    動かし直す」だけで出来ている。**その2つが崩れると、移った先が
    白紙になる**ので、ここで固定する。
    """

    def pages(self):
        for path in sorted(TEMPLATES.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            if text.lstrip().startswith("{% extends"):
                yield path.name, text

    def test_外枠には差し替える目印がある(self) -> None:
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        for marker in ('<main class="work" id="main">',
                       '<div id="pagescripts" hidden>',
                       '<header class="ribbon">',
                       '<nav class="rail"'):
            with self.subTest(marker=marker):
                self.assertIn(marker, base)

    def test_画面ごとのスクリプトは箱の中に置く(self) -> None:
        """箱の外に置くと、差し替えたときに動かない。"""
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        box = base.index('<div id="pagescripts"')
        self.assertLess(box, base.index("{% block scripts %}"))
        for name, text in self.pages():
            with self.subTest(screen=name):
                # 画面側は scripts ブロックの中でだけ <script> を書く
                outside = re.sub(r"\{% block scripts %\}.*?\{% endblock %\}",
                                 "", text, flags=re.S)
                self.assertNotIn("<script", outside,
                                 f"{name}: scripts ブロックの外に <script> があります")

    def test_差し替えても残るものを消さない(self) -> None:
        """トーストと接続断の帯は「いまの状態」を持つ。作り直さない。"""
        nav = (_ROOT / "app" / "static" / "js" / "nav.js").read_text(encoding="utf-8")
        swap = re.search(r"const SWAP = \[([^\]]*)\]", nav).group(1)
        for kept in ("toasts", "offline"):
            with self.subTest(kept=kept):
                self.assertNotIn(kept, swap)

    def test_モード切替だけは読み込み直す(self) -> None:
        """出せる画面そのものが入れ替わるので、レールごと作り直させる。"""
        app_js = (_ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        mode = app_js[app_js.index("function wireMode"):]
        self.assertIn("location.href", mode[:mode.index("\n}")])

    def test_帯の進捗は差し替えのたびに描き直す(self) -> None:
        """帯は差し替えの対象。**新しい帯は空で来る。**

        描き直さないと、画面を移った先で「何も動いていない」ように
        見える ── 取り込みは数分かかるので、そこで待つのをやめてしまう。
        """
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        for marker in ('id="rb-running"', 'id="rb-run-what"',
                       'id="rb-run-how"', 'id="rb-run-fill"'):
            with self.subTest(marker=marker):
                self.assertIn(marker, base)
        # 帯そのものが差し替え対象であること
        nav = (_ROOT / "app" / "static" / "js" / "nav.js").read_text(encoding="utf-8")
        swap = re.search(r"const SWAP = \[([^\]]*)\]", nav).group(1)
        self.assertIn("ribbon", swap)
        # 差し替え後に描き直していること
        app_js = (_ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        wire = app_js[app_js.index("export function wireShell"):]
        self.assertIn("running.paint()", wire[:wire.index("\n}")])

    def test_版はどの画面でも消えない(self) -> None:
        """**どれが入っている端末か**を、画面を見れば答えられること。

        帯は差し替えの対象だが、行き先ごとに作り直されるテンプレートは
        `base.html` ただ1つなので、ここに在れば全画面に出る。
        """
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.assertIn('class="brand"', base)
        self.assertIn("{{ version_label }}", base)
        # 帯がまだ無い起動待機画面にも出す。**こちらはテンプレートではない**
        # ── Flask を読み込む前に出すので、素のPythonが組み立てる
        # (`packaging_tool/boot_screen.py`)
        from packaging_tool import app_config, boot_screen

        page = boot_screen.render(
            display_name="梱包資材総合ツール", version_label="VER9.9.9",
            token="t", app_id="x", poll_ms=300, home_url="/lot")
        self.assertIn("VER9.9.9", page)
        self.assertIn(app_config.version_label(), boot_screen.render(
            display_name="梱包資材総合ツール",
            version_label=app_config.version_label(),
            token="t", app_id="x", poll_ms=300, home_url="/lot"))

    def test_版を画面に書き写さない(self) -> None:
        """出どころは `config/app.json` ただ1つ。

        テンプレートに数字を直接書くと、上げたときに片方だけ古くなる。
        """
        for path in TEMPLATES.glob("*.html"):
            with self.subTest(screen=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"VER\s*\d+\.\d+\.\d+",
                                    f"{path.name}: 版を直接書いています")

    def test_走っているものの見張りは1か所(self) -> None:
        """帯と設定画面が別々に叩くと、片方だけ古い、が起こる。"""
        js = _ROOT / "app" / "static" / "js"
        callers = [p.name for p in js.rglob("*.js")
                   if "/api/jobs" in p.read_text(encoding="utf-8")]
        self.assertEqual(callers, ["jobs.js"])


if __name__ == "__main__":
    unittest.main()
