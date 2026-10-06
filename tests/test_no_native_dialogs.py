"""ブラウザの confirm / prompt / alert を使っていないこと。

デスクトップ版(Tauri の窓)では `window.confirm` / `window.prompt` の窓が**出ないまま**「OK」扱いで
処理が進んだ(別の道具での指摘)。表を消す・作り直す・入れ替える・配置図を出荷時に戻す・切断依頼を
取り消す ── 取り返しのつかない操作の前の確かめが黙って素通りになる。

確かめの窓は画面の中に自分で出す(`app/static/js/askbox.js`、帳票の窓は `printing._SEND_SCRIPT`)。
ここでは**使い戻していないか**を、画面のコードと帳票のコードを読んで確かめる。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# `confirm(` / `prompt(` / `alert(` を呼んでいる所。`confirmBox(`・`promptBox(`・
# `obj.confirm(`(自前のメソッド)・説明の文は除く
NATIVE = re.compile(r"(?<![\w.$])(?:window\.)?(confirm|prompt|alert)\s*\(")


def _code_lines(text: str):
    for number, line in enumerate(text.splitlines(), start=1):
        bare = line.strip()
        if bare.startswith(("//", "*", "/*")):
            continue
        yield number, line.split("//")[0]


class NoNativeDialogTests(unittest.TestCase):
    def sources(self):
        yield from sorted((ROOT / "app/static/js").rglob("*.js"))
        yield ROOT / "packaging_tool/printing.py"

    def test_confirm_prompt_alert_を呼んでいない(self) -> None:
        found = []
        for path in self.sources():
            text = path.read_text("utf-8")
            for number, code in _code_lines(text):
                code = code.replace("window.confirm` / `window.prompt", "")
                for match in NATIVE.finditer(code):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {match.group(0)}")
        self.assertEqual(found, [], "ブラウザの確かめの窓はデスクトップ版で出ません: "
                                    + " / ".join(found))

    def test_取り返しのつかない操作の前は自前の窓で訊く(self) -> None:
        """指摘の5か所と、同じ書き方をしていた5か所。"""
        expect = {
            "app/static/js/views/warehouse.js": ["await confirmBox(ask"],
            "app/static/js/views/master.js": ["await confirmBox(", "await promptBox(",
                                              "if (!(await confirmBox("],
            "app/static/js/views/settings.js": ["if (!(await confirmBox("],
            "app/static/js/views/layout.js": ["await confirmBox(\"出荷時の配置に戻します"],
            "app/static/js/views/inventory.js": ["await confirmBox(\"出荷時の配置に戻します"],
            "app/static/js/views/lotlist.js": ["await promptBox("],
            "app/static/js/app.js": ["await confirmBox(err.message"],
            "packaging_tool/printing.py": ["ask(err.message).then("],
        }
        for path, needles in expect.items():
            text = (ROOT / path).read_text("utf-8")
            for needle in needles:
                with self.subTest(path=path, needle=needle):
                    self.assertIn(needle, text)

    def test_試験が本当に見つけられる(self) -> None:
        """この試験そのものが、使い戻したときに落ちること。"""
        for line in ("if (!window.confirm(ask)) return;", "if (confirm('x')) {",
                     "const n = window.prompt('名前');", "alert('x')"):
            with self.subTest(line=line):
                self.assertTrue(NATIVE.search(line))
        for line in ("await confirmBox('x')", "await promptBox('x')", "dialog.confirm(x)",
                     "save_confirm(self)"):
            with self.subTest(line=line):
                self.assertFalse(NATIVE.search(line))


if __name__ == "__main__":
    unittest.main()
