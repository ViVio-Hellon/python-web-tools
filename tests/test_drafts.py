"""打ちかけ(送る・登録する前の入力)を、止まっても失わない(`app/static/js/drafts.js`)

統合ツール(all-tools 1.2.0)は、外から止めるときに開いている画面へ「打ちかけを置いて」と
頼んでから止める。総合ツールは**打つたびにこの端末へ置いておく**ので、止める前に頼んで
待つ必要が無く、強制で止められても・ブラウザが落ちても残る(docs/ランチャー連携.md)。

画面での動き(打つ → ブラウザを落とす → 開き直すと戻る・送ったら消える・別の行には
戻さない・もとの値が変わっていたら戻さない)は Chromium で確かめた。ここでは、
**組み込みが外れていないか**を固定する。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

JS = _ROOT / "app" / "static" / "js"


def read(rel: str) -> str:
    return (JS / rel).read_text("utf-8")


class DraftModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.js = read("drafts.js")

    def test_打つたびに置く(self) -> None:
        """止まる直前ではなく、いつも置く(強制で止められても残る)。"""
        self.assertIn('document.addEventListener("input"', self.js)
        self.assertIn("localStorage", self.js)

    def test_パスワードは置かない(self) -> None:
        self.assertIn('input.type === "password"', self.js)

    def test_もとの値が変わっていたら戻さない(self) -> None:
        """その間に別の端末が直した行へ、古い打ちかけを被せない。"""
        self.assertIn("saved.base !== base", self.js)

    def test_古い打ちかけは捨てる(self) -> None:
        self.assertRegex(self.js, r"MAX_AGE_MS\s*=\s*14 \* 24")

    def test_戻したら知らせる(self) -> None:
        self.assertIn("前に打ちかけていた内容を戻しました", self.js)
        self.assertIn("まだ送っていません", self.js)


class WiringTests(unittest.TestCase):
    """どの画面の、どの欄を置くか。**行ごと・発注ごと**に名前を分ける。"""

    def test_倉庫連携_発注ごとのコメント(self) -> None:
        js = read("views/warehouse.js")
        self.assertIn("draftStore.bind(el.commentText, `warehouse.comment.${row.mgr_no}`", js)
        # 前の発注に打ちかけた文を、次の発注へ持ち越さない
        self.assertRegex(js, r'el\.commentText\.value = "";\s*\n\s*draftStore\.bind\(el\.commentText')
        self.assertRegex(js, r'el\.commentText\.value = "";\s*\n\s*draftStore\.done\(el\.commentText\)')

    def test_倉庫連携_送る前の欄(self) -> None:
        js = read("views/warehouse.js")
        self.assertIn('draftStore.bind(el.sendComment, "warehouse.send.comment"', js)
        self.assertIn("draftStore.bind(node, `warehouse.send.${key}`", js)
        # 送ったら消す・「入力を消す」で発注の欄は消す
        self.assertIn("draftStore.done(el.sendComment, ...Object.values(el.fields))", js)
        self.assertIn("draftStore.done(...Object.values(el.fields))", js)
        # 資材選択から届いた下書きのときは、発注の欄を置かない(サーバが預かっている)
        self.assertRegex(js, r"if \(drafts\.length\) return;\s*\n\s*for \(const \[key, node\] of Object\.entries\(el\.fields\)\)")

    def test_在庫_受け入れ(self) -> None:
        js = read("views/inventory.js")
        self.assertIn('const RECEIVE_FIELDS = ["rw", "rl", "rqty", "rpos", "rsym", "rind", "runit", "rnote"]', js)
        self.assertIn("draftStore.bind(el[id], `inventory.receive.${id}`", js)
        self.assertIn("draftStore.done(...RECEIVE_FIELDS.map((id) => el[id]))", js)

    def test_マスタ管理_表と行ごと(self) -> None:
        js = read("views/master.js")
        self.assertIn('`master.${view.table}.${key === null ? "new" : key}.${input.dataset.column}`', js)
        # 書いた・本人が閉じたら捨てる(止められたときは閉じる合図が来ないので残る)
        self.assertRegex(js, r'el\.mEdit\.addEventListener\("close", \(\) => \{[^}]*draftStore\.done')

    def test_置く欄にパスワードが無い(self) -> None:
        for rel in ("views/warehouse.js", "views/inventory.js", "views/master.js"):
            with self.subTest(rel=rel):
                for key in re.findall(r"draftStore\.bind\([^,]+, [`\"]([^`\"]+)", read(rel)):
                    self.assertNotIn("pass", key.lower())


if __name__ == "__main__":
    unittest.main()
