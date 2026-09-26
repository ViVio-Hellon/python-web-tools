"""このツール以外のタブが開いていても、使っている画面が止まらないこと。

ほかのサイトのタブが `<img>`・`<iframe>`・リンク・`window.open` でこのツールの
アドレスを読み込むと、以前は「新しいタブが開いた」扱いになり、利用者の画面が
「このタブは、あとから開いたタブに操作を譲りました」で止まっていた。
同じ試験で、要求が重なると資材選択の画面が 500 になることも見つかった。
"""
from __future__ import annotations

import sqlite3
import threading
import unittest

from packaging_tool import screen_lock, selection_session
from packaging_tool.presenters.selection import SelectionPresenter
from tests import _web


class ForeignPageTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import selection as routes
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        _web.bind_db(self, routes)
        self.client = _web.make_client(port=8717)
        # 利用者が開いている画面
        self.mine = "mine-0001"
        screen_lock.claim(self.mine)
        self.addCleanup(screen_lock.claim, "")

    def get(self, path: str, site: str):
        headers = {"Sec-Fetch-Site": site} if site else {}
        return self.client.get(path, headers=headers)

    def test_ほかのサイトから読み込まれても使っている画面を取り上げない(self):
        for path in ("/selection", "/lot?lot=H5400A0", "/layout", "/settings", "/"):
            for site in ("cross-site", "same-site"):
                res = self.get(path, site)
                self.assertEqual(res.status_code, 403, (path, site))
                self.assertIn("ほかのページから開かれた", res.get_data(as_text=True))
        self.assertTrue(screen_lock.is_active(self.mine))

    def test_アドレス欄やツールの中からは開ける(self):
        for site in ("none", "same-origin", ""):
            self.assertEqual(self.get("/selection", site).status_code, 200, site)

    def test_ほかのサイトから書き込みはできない(self):
        res = self.client.post("/api/selection/clear", json={},
                               headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)


class ThreadConnectionTests(unittest.TestCase):
    """作業状態は1つ、接続は要求(スレッド)ごと。"""

    def test_重なった要求がほかのスレッドの接続を使わない(self):
        mine = sqlite3.connect(":memory:")
        presenter = SelectionPresenter(mine)
        seen = []

        def other_request() -> None:
            theirs = sqlite3.connect(":memory:")
            presenter.conn = theirs          # 後から来た要求が差し替える
            seen.append(presenter.conn is theirs)

        thread = threading.Thread(target=other_request)
        thread.start()
        thread.join()
        self.assertEqual(seen, [True])
        # 先の要求は自分の接続のまま(以前は後の要求の接続に替わって 500)
        self.assertIs(presenter.conn, mine)
        presenter.conn.execute("SELECT 1").fetchone()


if __name__ == "__main__":
    unittest.main()
