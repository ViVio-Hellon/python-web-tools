"""テストが本物のファイルを書き換えていないこと

【これが落ちたとき何が起きているか】
`data/user_config.json` は**追跡対象**で、現場の拠点や共有フォルダの
設定が入っている。テストがここに書くと、流しただけで作業ツリーに差分が
出るうえ、本番の端末で流せば現場の設定が黙って変わる。

同じことが `data/packaging_tool.db` でも一度起きている(ブラウザ試験が
実績パターンを書き込み、`compare_ui.py` の突き合わせが崩れた)。
**気づけない副作用は、いずれ必ず判断を狂わせる**ので、機械で止める。

【直しかた】
このファイルの import で `_isolation.ensure_isolated()` を呼んでいるので、
通常は落ちない。落ちたということは、向き先を戻す何かが後から入っている
(`config.USER_CONFIG_PATH` を書き換えたまま戻していないテスト等)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# ★ 取り込み時に呼ぶ。unittest は全モジュールを取り込んでから走らせるので、
#   ここで直しておけば、どのテストが書き込もうとしても本物には届かない
from tests import _isolation  # noqa: E402

_MOVED = _isolation.ensure_isolated()

from packaging_tool import config  # noqa: E402


class IsolationTests(unittest.TestCase):

    def _assert_outside(self, path: Path, what: str) -> None:
        repo = Path(config.BASE_DIR).resolve()
        try:
            rel = Path(path).resolve().relative_to(repo)
        except (ValueError, OSError):
            return                                  # リポジトリの外 = 安全
        self.fail(
            f"{what} がリポジトリ内 ({rel}) を指しています。"
            " テストの書き込みが作業ツリーを汚します。"
            " tests/_isolation.py の説明を参照してください。")

    def test_利用者設定は本物を指していない(self) -> None:
        """`data/user_config.json` に書かれると現場の設定が変わる。"""
        self._assert_outside(config.USER_CONFIG_PATH, "USER_CONFIG_PATH")

    def test_ログは本物を指していない(self) -> None:
        self._assert_outside(config.LOG_DIR, "LOG_DIR")

    def test_業務DBは本物を指していない(self) -> None:
        """`data/packaging_tool.db` も**追跡対象**。

        大半のテストは `:memory:` を使うが、`create_app()` は起動時に
        `アクセス権限` を引くため `config.DB_PATH` を開く。**開くだけでも**
        `PRAGMA journal_mode=WAL` がファイルを書き換えるので、流しただけで
        差分が出る(実際に踏んだ)。
        """
        self._assert_outside(config.DB_PATH, "DB_PATH")

    def test_基準DBは残っている(self) -> None:
        """`compare_ui.py` はリポジトリの実DBを基準に突き合わせる。

        逃がすのは**テストの書き込み先**であって、基準そのものではない。
        """
        real = Path(config.BASE_DIR) / "data" / "packaging_tool.db"
        self.assertTrue(
            real.exists(),
            "基準DBが見当たりません。compare_ui.py の突き合わせができません")

    def test_逃がしそこねを検知できる(self) -> None:
        """検知そのものが働くことを確かめる(自己点検)。

        判定を信じるためには、**落ちるべきときに落ちる**ことを
        1度は見ておく必要がある。
        """
        with self.assertRaises(AssertionError):
            self._assert_outside(
                Path(config.BASE_DIR) / "data" / "user_config.json", "見本")


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
