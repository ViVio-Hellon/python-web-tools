"""タブのアイコン(`/favicon.ico`)。

帳票・起動待機・カタログのような単独のページはアイコンを持たないので、
ブラウザが `/favicon.ico` を取りに来る。以前は 404 で、帳票を開くたびに
エラーが1行記録されていた(通しの試験で見つけた)。
"""
from __future__ import annotations

import unittest

from tests import _web


class FaviconTests(unittest.TestCase):
    def test_中身なしで返す_404にしない(self) -> None:
        client = _web.make_client(port=8716)
        res = client.get("/favicon.ico")          # ブラウザはトークンを付けない
        self.assertEqual(res.status_code, 204)
        self.assertEqual(res.data, b"")


if __name__ == "__main__":
    unittest.main()
