"""デザイントークンのコントラスト比

`scripts/check_contrast.py` を毎回のテストで走らせる。色を足したり
調整したりしたときに、手で叩くのを忘れても気づけるようにするため。

現行のtkinter版では、いちばん見落として困る表示ほど読みにくくなっていた
(1P0113強制ON が 2.33:1、「未設定」帯が 3.37:1)。
目視では気づけないので、機械に見張らせる。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import check_contrast as cc  # noqa: E402


def _themes() -> dict:
    return cc.parse_tokens(cc.TOKENS_PATH.read_text(encoding="utf-8"))


class TokensFileTests(unittest.TestCase):
    def test_トークンが読める(self) -> None:
        self.assertTrue(cc.TOKENS_PATH.exists(), f"{cc.TOKENS_PATH} がありません")
        themes = _themes()
        self.assertEqual(set(themes), {cc.THEME_LIGHT, cc.THEME_DARK_SYSTEM,
                                       cc.THEME_DARK_EXPLICIT})

    def test_3つのテーマがそろっている(self) -> None:
        """既定・OSダーク・明示ダークの3状態。

        どれか1つでも欠けると「OSがダークの人にだけ読めない画面」が生まれる。
        """
        for name, tokens in _themes().items():
            with self.subTest(theme=name):
                self.assertGreater(len(tokens), 30, f"{name} のトークンが少なすぎます")

    def test_OSダークと明示ダークが一致する(self) -> None:
        """`@media` と `[data-theme="dark"]` は同じ値でなければならない。

        ずれると、OSの設定と画面のトグルで見た目が変わる。
        """
        themes = _themes()
        self.assertEqual(themes[cc.THEME_DARK_SYSTEM], themes[cc.THEME_DARK_EXPLICIT])

    def test_ダークがライトの全トークンを持つ(self) -> None:
        """ダークで定義し忘れたトークンはライトの値が残る。

        地だけ暗くなって文字が暗いまま、という壊れ方をするので、
        **色の役割ごとに**上書きされているかを確かめる。
        """
        themes = _themes()
        light, dark = themes[cc.THEME_LIGHT], themes[cc.THEME_DARK_SYSTEM]
        self.assertEqual(set(light), set(dark) - (set(dark) - set(light)),
                         "ライトにしか無いトークンがあります")

    def test_地と文字はダークで必ず変わる(self) -> None:
        themes = _themes()
        light, dark = themes[cc.THEME_LIGHT], themes[cc.THEME_DARK_SYSTEM]
        for name in ("ground", "surface", "ink", "muted"):
            with self.subTest(token=name):
                self.assertNotEqual(light[name], dark[name],
                                    f"--{name} がダークで上書きされていません")


class ContrastTests(unittest.TestCase):
    def test_すべての組み合わせが下限を満たす(self) -> None:
        findings = cc.check(_themes())
        failed = [str(f) for f in findings if not f.ok]
        self.assertEqual(
            failed, [],
            "コントラストが不足しています:\n" + "\n".join("  " + f for f in failed))

    def test_検査対象が十分にある(self) -> None:
        """ペアの表が空になっていたら、上のテストは何も守らない。"""
        findings = cc.check(_themes())
        self.assertGreater(len(findings), 100, "検査ペアが少なすぎます")

    def test_例外には理由が書いてある(self) -> None:
        for bg, fg, label, reason in cc.DOCUMENTED_EXCEPTIONS:
            with self.subTest(pair=f"{bg}/{fg}"):
                self.assertTrue(label.strip())
                self.assertGreater(len(reason.strip()), 20,
                                   f"{label} の理由が短すぎます")


class ColorMathTests(unittest.TestCase):
    """計算そのものが合っているか(検査が空振りしていないこと)。"""

    def test_白と黒は21対1(self) -> None:
        ratio = cc.contrast_ratio((255, 255, 255), (0, 0, 0))
        self.assertAlmostEqual(ratio, 21.0, places=1)

    def test_同じ色は1対1(self) -> None:
        self.assertAlmostEqual(cc.contrast_ratio((18, 52, 86), (18, 52, 86)), 1.0)

    def test_順序を入れ替えても同じ(self) -> None:
        a, b = (255, 255, 255), (100, 120, 140)
        self.assertAlmostEqual(cc.contrast_ratio(a, b), cc.contrast_ratio(b, a))

    def test_既知の値と一致する(self) -> None:
        """現行の 1P0113強制ON。設計書に 2.33:1 と書いてある。"""
        ratio = cc.contrast_ratio(cc.parse_hex("#ff8c1e"), cc.parse_hex("#ffffff"))
        self.assertAlmostEqual(ratio, 2.33, places=2)

    def test_3桁の16進も読める(self) -> None:
        self.assertEqual(cc.parse_hex("#fff"), (255, 255, 255))

    def test_色でないものはNone(self) -> None:
        for value in ("red", "var(--x)", "rgba(0,0,0,.5)", "", "#12345"):
            with self.subTest(value=value):
                self.assertIsNone(cc.parse_hex(value))


class ParserTests(unittest.TestCase):
    def test_入れ子のブロックを正しく閉じる(self) -> None:
        """`@media` の中の `:root` を取り違えないこと。"""
        css = """
        :root{ --a:#111111; --b:#222222; }
        @media (prefers-color-scheme: dark){
          :root:not([data-theme="light"]){ --a:#333333; }
        }
        :root[data-theme="dark"]{ --a:#333333; }
        """
        themes = cc.parse_tokens(css)
        self.assertEqual(themes[cc.THEME_LIGHT]["--a".lstrip("-")], "#111111")
        self.assertEqual(themes[cc.THEME_DARK_SYSTEM]["a"], "#333333")
        # 上書きしていないものはライトの値が残る(CSSのカスケードと同じ)
        self.assertEqual(themes[cc.THEME_DARK_SYSTEM]["b"], "#222222")

    def test_コメントを値と誤認しない(self) -> None:
        css = ":root{ /* --dummy:#000000; */ --a:#111111; }"
        self.assertNotIn("dummy", cc.parse_tokens(css)[cc.THEME_LIGHT])

    def test_rootが無ければ例外(self) -> None:
        with self.assertRaises(ValueError):
            cc.parse_tokens("body{color:red}")


class CliTests(unittest.TestCase):
    """コマンドとして呼んだときの終了コード。

    検査結果そのものは上のテストで見ているので、ここでは
    出力を飲み込んで終了コードだけを確かめる(テスト出力を汚さない)。
    """

    def _run(self, argv: list[str]) -> int:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return cc.main(argv)

    def test_終了コード0(self) -> None:
        self.assertEqual(self._run([]), 0)

    def test_一覧表示も動く(self) -> None:
        self.assertEqual(self._run(["--list"]), 0)

    def test_トークン表示も動く(self) -> None:
        self.assertEqual(self._run(["--tokens"]), 0)

    def test_ファイルが無ければ2(self) -> None:
        self.assertEqual(self._run(["--path", "/存在しない/tokens.css"]), 2)

    def test_不足があれば1(self) -> None:
        """検査が本当に落ちることの確認(空振りしていないこと)。"""
        import tempfile
        broken = (':root{--ground:#ffffff;--surface:#ffffff;--ink:#fefefe;'
                  '--muted:#fdfdfd;}')
        with tempfile.NamedTemporaryFile("w", suffix=".css", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(broken)
            path = handle.name
        self.assertEqual(self._run(["--path", path]), 1)


class UndefinedVariableTests(unittest.TestCase):
    """`var(--xxx)` が指す先が、`tokens.css` に本当にあるか。

    現場の声(パレット自動選定):「一番小さい面積のパレットを選択して
    おくが生きていない」の実体は、選んだ行のハイライト
    (`.table tr.on td { background:var(--info-bg); ... }`)が
    **未定義のCSS変数を指していたため、色が一切付かなかった**こと
    だった(処理自体は正しく動いていた)。

    未定義変数はブラウザが黙って無視する(エラーにならない)ため、
    実機で気づくまで誰にも見えない。CSS/テンプレートを足すたびに
    手で見比べるのは現実的でないので、機械的に洗い出す。
    `var(--x, フォールバック)` の2引数形は安全(フォールバックが効く)
    なので対象外。
    """

    # 意図的にフォールバック運用にしているトークン。ここに載せるのは
    # 「まだ無い」ことを承知の上で残すもの限定 ── 見落としを隠す
    # 抜け穴にしないよう、載せたら理由をコメントする
    _FALLBACK_ONLY: frozenset[str] = frozenset({
        "--night-muted",  # ログ画面の補助色。フォールバック運用で確定
    })
    # JS側が `style.setProperty` で都度書き込む変数(定義は要らない)
    _JS_MANAGED: frozenset[str] = frozenset({"--zoom"})

    def _referenced_vars(self) -> dict[str, set[Path]]:
        """`var(--x)` の1引数形だけを集める。ファイルごとに出どころも持つ。"""
        import re

        pattern = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*\)")
        found: dict[str, set[Path]] = {}
        targets = list((_ROOT / "app" / "static" / "css").glob("*.css"))
        targets += list((_ROOT / "app" / "templates").glob("*.html"))
        for path in targets:
            text = path.read_text(encoding="utf-8")
            for name in pattern.findall(text):
                found.setdefault(name, set()).add(path)
        return found

    def test_参照している変数は全てtokens_cssにある(self) -> None:
        themes = _themes()
        # `parse_tokens` はキーから "--" を落として返す
        light = {f"--{name}" for name in themes[cc.THEME_LIGHT]}
        referenced = self._referenced_vars()

        missing = {
            name: sorted(str(p.relative_to(_ROOT)) for p in paths)
            for name, paths in referenced.items()
            if name not in light
            and name not in self._FALLBACK_ONLY
            and name not in self._JS_MANAGED
        }
        self.assertEqual(
            missing, {},
            "未定義のCSS変数があります(ブラウザは黙って無視するので、"
            "気づかず色が付かないままになります): " + repr(missing))


if __name__ == "__main__":
    unittest.main()
