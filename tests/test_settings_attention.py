"""設定: 「要確認」が何なのかを言う・直ったら消す の試験

現場の声:「(取り込み元の見出しの)要確認、何が要確認かわからんのですが」
「別タブから戻ると消えてました...謎すぎるんで直してください」

【分かったこと】
- 取り込み元の見出しの「要確認」は、**設定の節ぜんぶ**(権限・この端末も含む)の一番重い状態だった。
  何の項目かは書いていなかった
- 見出しとレールの「?」は**開いたときにサーバが描いたまま**。保存・取り込みで直っても付け直さず、
  別の画面へ行って戻る(描き直す)と消えた

【ここで守りたいこと】
1. 取り込み元の見出しは、取り込み元の節(マスタ・看板・閾値・仕掛台帳)だけを背負う
2. 何が要確認かを一言で出す(「節 › 項目: 値」。多ければ「ほか N件」)
3. 保存・状態の取り直しの応答に、見出しとレールの印を入れる。画面はそれで付け直す
4. レールの「?」に指を置くと、何が要確認かが出る
"""
from __future__ import annotations

import unittest
from pathlib import Path

from packaging_tool.presenters import settings as presenter
from tests import test_web_settings

NG, WARN, OK = presenter.NG, presenter.WARN, presenter.OK


def section(title: str, *checks: tuple[str, str, str]) -> presenter.Section:
    return presenter.Section(title, checks=[presenter.Check(l, v, lv) for l, v, lv in checks])


class AttentionTests(unittest.TestCase):
    def test_何が要確認かを重い順に言う(self) -> None:
        sections = [
            section("仕掛台帳", ("そろい具合", "3/3 ファイル", OK),
                    ("2つ目 SIKALOT.sqlite3", "見つかりません", WARN)),
            section("梱包資材マスタ", ("ファイル", "見つかりません", NG)),
        ]
        items = presenter.attention(sections)
        self.assertEqual([i["label"] for i in items], ["ファイル", "2つ目 SIKALOT.sqlite3"])
        self.assertEqual(presenter.attention_text(items),
                         "梱包資材マスタ › ファイル: 見つかりません"
                         " / 仕掛台帳 › 2つ目 SIKALOT.sqlite3: 見つかりません")

    def test_多ければほかN件(self) -> None:
        sections = [section("仕掛台帳", *[(f"項目{i}", "変", WARN) for i in range(4)])]
        self.assertTrue(presenter.attention_text(presenter.attention(sections)).endswith(" ほか2件"))
        self.assertEqual(presenter.attention_text([]), "")

    def test_取り込み元の見出しは取り込み元の節だけ(self) -> None:
        view = presenter.SettingsViewModel(sections=[
            section("梱包資材マスタ", ("ファイル", "ある", OK)),
            section("この端末の権限", ("権限の出どころ", "既定を使っています", WARN)),
        ])
        self.assertEqual(view.level, WARN)                       # 全体は要確認
        state = presenter.source_state(view)
        self.assertEqual((state["level"], state["label"], state["text"]), (OK, "問題なし", ""))

    def test_レールの印に何が要確認かを持たせる(self) -> None:
        view = presenter.SettingsViewModel(sections=[
            section("この端末の権限", ("権限の出どころ", "既定を使っています", WARN))])
        self.assertEqual(presenter.rail_badge(view), {
            "text": "?", "kind": "todo",
            "title": "要確認: この端末の権限 › 権限の出どころ: 既定を使っています"})
        ok = presenter.SettingsViewModel(sections=[section("梱包資材マスタ", ("ファイル", "ある", OK))])
        self.assertEqual(presenter.rail_badge(ok), {"text": "", "kind": "done", "title": ""})


class PageTests(test_web_settings.DataWebTestCase):
    def test_保存と取り直しの応答に見出しと印が入る(self) -> None:
        body = self.client.get("/api/settings/state", headers=self.auth()).get_json()
        sections = [presenter.Section(s["title"], checks=[
            presenter.Check(c["label"], c["value"], c["level"]) for c in s["checks"]])
            for s in body["sections"]]
        view = presenter.SettingsViewModel(sections=sections)
        self.assertEqual(body["source_state"], presenter.source_state(view))
        self.assertEqual(body["rail_badge"], presenter.rail_badge(view))

    def test_画面の見出しと何が要確認か(self) -> None:
        html = self.client.get("/settings").get_data(as_text=True)
        body = self.client.get("/api/settings/state", headers=self.auth()).get_json()
        src = body["source_state"]
        self.assertIn(f'<span class="lv lv--{src["level"]}" id="sourceLevel">{src["label"]}</span>', html)
        self.assertIn('id="sourceAttention"', html)
        self.assertIn('data-goto="status">いまの状態で見る', html)
        if body["rail_badge"]["title"]:
            self.assertIn(f'title="{body["rail_badge"]["title"]}"', html)

    def test_画面は応答のたびに付け直す(self) -> None:
        js = (Path(__file__).resolve().parent.parent / "app/static/js/views/settings.js").read_text("utf-8")
        render = js.split("function renderStatus")[1].split("\n}\n")[0]
        self.assertIn("renderSource(state.source_state)", render)
        self.assertIn("renderRailBadge(state.rail_badge)", render)
        rail = js.split("function renderRailBadge")[1].split("\n}\n")[0]
        self.assertIn("node.remove()", rail, "直ったら印を消す")


if __name__ == "__main__":
    unittest.main()
