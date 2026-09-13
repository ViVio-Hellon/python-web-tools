"""待機表示(`busy.js`)の配線が抜けていないかの機械チェック。

【なぜ要るか】
`busy.js` の `watchClicks` は `.btn` / `.choice` / `.seg__btn` /
`[data-row-action]` を捕まえて、押した本人に「まだ返ってきていない」
ことを伝える(回る輪・経過秒)。この仕組み自体はJSのロジックであり、
Pythonのテストからは実行できない。ここで機械的に確かめられるのは
**配線**だけ ── サーバ通信をする行(`<tr>`)を作っている箇所に、
`data-row-action` を付け忘れていないか。

現場の声:「どの処理でも待ちがあるならプログレス出してください」→
実際には、ボタン以外の「行そのものが押せる」作り(引当行・ロット一覧・
共有フォルダのファイルブラウザ)が `watchClicks` の対象外になっており、
サーバ通信していても待機の姿にならなかった。

このテストは「付いているか」の網羅は保証しない(新しく足された
行クリックには気づけない)。**既知の3か所が退行していないか**だけを
機械的に見張る。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_STATIC = _ROOT / "app" / "static" / "js"


def _read(*parts: str) -> str:
    return (_STATIC.joinpath(*parts)).read_text(encoding="utf-8")


class BusyJsShapeTests(unittest.TestCase):
    """`busy.js` 自体が、ボタンと行の両方を面倒みる形になっているか。"""

    def setUp(self) -> None:
        self.text = _read("busy.js")

    def test_watchClicksが行の目印も拾う(self) -> None:
        self.assertIn("data-row-action", self.text)

    def test_markがTR特有の分岐を持つ(self) -> None:
        """`<tr>` は `disabled` を持てない(HTML標準として効かない)ので、

        ボタンと同じ扱いのままだと押せなくする効果が無い。
        """
        self.assertIn('tagName === "TR"', self.text)

    def test_行の待機は二重送信を防ぐ手段を持つ(self) -> None:
        self.assertIn("pointerEvents", self.text)


class RowActionWiringTests(unittest.TestCase):
    """サーバ通信する行の生成箇所に `data-row-action` が付いているか。

    行を作る関数の中身までは追わず、**該当ファイルに文字列として
    含まれているか**だけを見る(壊れやすい厳密なAST解析より、退行に
    早く気づけることを優先する)。
    """

    def test_ロット情報の引当行(self) -> None:
        # モーダルの中身は `lotdetail.js` にある(ロット検索と発注一覧の
        # 両方から同じものを出すため)。見る先もそちらへ移す
        text = _read("lotdetail.js")
        self.assertIn("dataset.rowAction", text)
        # 引当行のクリックが受注情報を取りに行く経路も生きていること
        self.assertIn("/hiki/", text)

    def test_ロット一覧の行(self) -> None:
        text = _read("views", "lotlist.js")
        self.assertIn("dataset.rowAction", text)

    def test_共有フォルダのファイルブラウザの行(self) -> None:
        """フォルダ階層をネットワーク越しに辿るので、特に待ちが出やすい。"""
        text = _read("views", "settings.js")
        self.assertIn("dataset.rowAction", text)
        self.assertIn("/api/fs/list", text)


if __name__ == "__main__":
    unittest.main()
