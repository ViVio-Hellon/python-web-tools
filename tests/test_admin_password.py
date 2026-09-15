"""管理者パスワードを現場で変えられるようにする (⑦)

【何のためのものか】
実績パターンの保存を**誤って押されない**ためのUIガードです。誰がその
端末を使えるかは `access_control`(アクセス権限マスタ)が決めます。

【ここで守りたいこと】
- **平文で持たない。** 配布はフォルダごとコピーなので、設定ファイルは
  そのまま持ち出せる
- 変えるには**いまのパスワードが要る**(肩越しに見ていた人が勝手に
  変えられる、を作らない)
- 一度も変えていない端末は**これまでどおり通る**(入れ替えただけで
  認証が通らなくなる、を作らない)
- 断りの種類は定数で運ぶ。**文言から推し量らない**(設計.md §1)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import admin_password, config, user_settings


class AdminPasswordTestCase(unittest.TestCase):
    """設定は**本物のファイルに書かない**(テストで端末の値が変わらない)。"""

    def setUp(self) -> None:
        self.store: dict = {}
        get = mock.patch.object(
            user_settings, "get",
            lambda key, default=None: self.store.get(key, default))
        save = mock.patch.object(
            user_settings, "save",
            lambda key, value: self.store.__setitem__(key, value))
        get.start()
        save.start()
        self.addCleanup(get.stop)
        self.addCleanup(save.stop)


class DefaultTests(AdminPasswordTestCase):
    """一度も変えていない端末。"""

    def test_既定のパスワードが通る(self):
        self.assertTrue(admin_password.verify(config.ADMIN_PASSWORD))

    def test_違うものは通らない(self):
        self.assertFalse(admin_password.verify(config.ADMIN_PASSWORD + "x"))

    def test_変えていないことが分かる(self):
        self.assertFalse(admin_password.is_custom())


class ChangeTests(AdminPasswordTestCase):
    def test_変えたら新しいほうで通る(self):
        result = admin_password.change(config.ADMIN_PASSWORD, "kouba2026",
                                       "kouba2026")
        self.assertTrue(result.ok, result.message)
        self.assertTrue(admin_password.verify("kouba2026"))
        self.assertTrue(admin_password.is_custom())

    def test_変えたら古いほうは通らない(self):
        admin_password.change(config.ADMIN_PASSWORD, "kouba2026", "kouba2026")
        self.assertFalse(admin_password.verify(config.ADMIN_PASSWORD))

    def test_平文では持たない(self):
        """配布はフォルダごとコピー。設定ファイルはそのまま持ち出せる。"""
        admin_password.change(config.ADMIN_PASSWORD, "kouba2026", "kouba2026")
        stored = self.store[admin_password.KEY]
        self.assertNotIn("kouba2026", stored)
        self.assertTrue(stored.startswith(f"{admin_password.SCHEME}$"))

    def test_同じ値でも保存のたびに違う文字列になる(self):
        """塩が毎回変わる。2台の端末が同じパスワードでも見分けられない。"""
        admin_password.change(config.ADMIN_PASSWORD, "kouba2026", "kouba2026")
        first = self.store[admin_password.KEY]
        admin_password.reset("kouba2026")
        admin_password.change(config.ADMIN_PASSWORD, "kouba2026", "kouba2026")
        self.assertNotEqual(first, self.store[admin_password.KEY])
        self.assertTrue(admin_password.verify("kouba2026"))

    # -- 断り ---------------------------------------------------------
    def test_いまのパスワードが違えば断る(self):
        result = admin_password.change("まちがい", "kouba2026", "kouba2026")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, admin_password.REFUSE_WRONG)
        self.assertNotIn(admin_password.KEY, self.store)

    def test_短すぎれば断る(self):
        short = "a" * (admin_password.MIN_LENGTH - 1)
        result = admin_password.change(config.ADMIN_PASSWORD, short, short)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, admin_password.REFUSE_TOO_SHORT)

    def test_確認用と一致しなければ断る(self):
        result = admin_password.change(config.ADMIN_PASSWORD,
                                       "kouba2026", "kouba2027")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, admin_password.REFUSE_MISMATCH)

    def test_同じ値なら断る(self):
        result = admin_password.change(config.ADMIN_PASSWORD,
                                       config.ADMIN_PASSWORD,
                                       config.ADMIN_PASSWORD)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, admin_password.REFUSE_SAME)

    def test_断ったときは設定を動かさない(self):
        """断ったのに半分だけ変わっている、を作らない(設計.md §6.1)。"""
        admin_password.change(config.ADMIN_PASSWORD, "kouba2026", "kouba2026")
        before = self.store[admin_password.KEY]
        admin_password.change("まちがい", "betsuno", "betsuno")
        self.assertEqual(self.store[admin_password.KEY], before)


class ResetTests(AdminPasswordTestCase):
    def test_既定に戻せる(self):
        admin_password.change(config.ADMIN_PASSWORD, "kouba2026", "kouba2026")
        result = admin_password.reset("kouba2026")
        self.assertTrue(result.ok, result.message)
        self.assertFalse(admin_password.is_custom())
        self.assertTrue(admin_password.verify(config.ADMIN_PASSWORD))

    def test_いまのパスワードを知らなければ戻せない(self):
        admin_password.change(config.ADMIN_PASSWORD, "kouba2026", "kouba2026")
        result = admin_password.reset("まちがい")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, admin_password.REFUSE_WRONG)
        self.assertTrue(admin_password.is_custom())


class BrokenValueTests(AdminPasswordTestCase):
    def test_壊れた値は通さない(self):
        """設定ファイルを手で書き換えられても、素通りはさせない。"""
        for junk in ("", "   ", "pbkdf2$こわれ", "kouba2026", "a$b$c$d"):
            self.store[admin_password.KEY] = junk
            self.assertFalse(admin_password.verify("kouba2026"), junk)


class LogMaskTests(unittest.TestCase):
    def test_設定のログに値を出さない(self):
        """設定の変更はログに残るが、**パスワードは残さない。**"""
        self.assertIn(admin_password.KEY, user_settings.HIDDEN_IN_LOG)


class AuthenticateTests(unittest.TestCase):
    """資材選択の管理者認証がここを通っているか。"""

    def test_照合は1か所だけ(self):
        """照合が2か所にあると、変えても片方だけ効く状態が作れる。

        **どのファイルにあるかは問わない。** 認証がどこに引っ越しても
        「`admin_password.verify` を通す」「`config.ADMIN_PASSWORD` を
        自分で読まない」の2つが守られていればよい。
        """
        pkg = Path(__file__).resolve().parent.parent / "packaging_tool"
        verifies, reads = [], []
        for source in pkg.rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            if "admin_password.verify" in text:
                verifies.append(source.name)
            # 出どころ(`admin_password` 自身)は読んでよい
            if "config.ADMIN_PASSWORD" in text and source.name != "admin_password.py":
                reads.append(source.name)
        self.assertTrue(verifies, "admin_password.verify を通す所が無い")
        self.assertEqual(reads, [], f"自前で照合している: {reads}")


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
