"""アプリ固有値 (`config/app.json` と `app_config.py`) の確認

基盤仕様書 5.2 は、アプリごとに決める値を1か所へ集約することを求めている。
集約したうえで**壊れ方が安全か**(設定ファイルが無い・壊れている・
キーが足りない場合に起動そのものが失敗しないか)をここで固定する。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import app_config, modes  # noqa: E402


class _ConfigTestCase(unittest.TestCase):
    """設定の読み込み先を差し替えるための土台。

    `app_config` は結果をキャッシュするので、差し替えたら必ず読み直す。
    """

    def _use(self, payload: object) -> None:
        path = Path(self._tmp.name) / "app.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        app_config.CONFIG_PATH = path
        app_config.load(force=True)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = app_config.CONFIG_PATH

    def tearDown(self) -> None:
        app_config.CONFIG_PATH = self._orig_path
        app_config.load(force=True)
        self._tmp.cleanup()


class RealConfigTests(_ConfigTestCase):
    """リポジトリに入っている `config/app.json` そのものの検査。"""

    def setUp(self) -> None:
        super().setUp()
        app_config.load(force=True)          # 実ファイルを読み直す

    def test_実ファイルが読める(self) -> None:
        self.assertEqual(app_config.load_error(), "",
                         "config/app.json が読めていません")

    def test_基盤仕様書が求める値がそろっている(self) -> None:
        self.assertTrue(app_config.app_id())
        self.assertTrue(app_config.display_name())
        self.assertTrue(app_config.version())
        self.assertIn(app_config.monitor_level(), (1, 2, 3))
        self.assertTrue(str(app_config.local_root()))
        for mode in modes.KEYS:
            self.assertGreater(app_config.port(mode), 0)

    def test_監視レベルは2(self) -> None:
        """Access取り込み・書き戻しという長時間処理があるためレベル2。

        レベルを上げ下げするときは設計書 (§2.1) の判定根拠も直すこと。
        """
        self.assertEqual(app_config.monitor_level(), 2)

    def test_ローカルのみで待ち受ける(self) -> None:
        """`0.0.0.0` にするとLANから触れてしまう。ここは固定。"""
        self.assertEqual(app_config.host(), "127.0.0.1")

    def test_モードごとの候補ポートが重ならない(self) -> None:
        """現場が繰り上がって倉庫のポートを奪わないこと。

        奪うと倉庫モードが起動できず、しかも利用者からは原因が見えない。
        `port` か `port_retry` を変えたらここで気づけるようにしてある。
        """
        self.assertEqual(app_config.port_range_conflicts(), [])

    def test_現場と資材は別ポート(self) -> None:
        self.assertNotEqual(app_config.port(modes.FIELD),
                            app_config.port(modes.MATERIAL))


class VersionTests(_ConfigTestCase):
    """版の管理。**出どころは `config/app.json` の1か所だけ。**

    現場の端末はフォルダごとコピーして配るので、「どれが入っているか」を
    答えられる番号が要る。帯のバッジ・起動待機画面・設定画面・
    `/api/health` はすべてここを読む。
    """

    def setUp(self) -> None:
        super().setUp()
        app_config.load(force=True)          # 実ファイルを読み直す

    def test_実ファイルの版は数字3つ(self) -> None:
        """並べて比べられる形であること(上げ方は docs/変更履歴.md)。"""
        self.assertEqual(app_config.version_problem(), "")

    def test_画面に出す形は前置き付き(self) -> None:
        """「1.0.0」だけだと何の番号か分からない。"""
        self.assertEqual(app_config.version_label(),
                         f"VER{app_config.version()}")

    def test_書き方が違えば理由を返す(self) -> None:
        for bad in ("1.0", "v1.0.0", "1.0.0-rc1", "", "いち"):
            with self.subTest(version=bad):
                self._use({"version": bad})
                self.assertIn("版の書き方", app_config.version_problem())

    def test_設定画面にも同じ番号が出る(self) -> None:
        """帯と設定で別の値が出ると、どちらが本当か分からなくなる。"""
        from packaging_tool.presenters import settings as presenter
        self._use({"version": "2.3.4"})
        section = presenter._terminal_section()
        found = next(c for c in section.checks if c.label == "バージョン")
        self.assertEqual(found.value, "VER2.3.4")

    def test_書き方が違えば設定画面が要確認にする(self) -> None:
        from packaging_tool.presenters import settings as presenter
        self._use({"version": "1.0"})
        section = presenter._terminal_section()
        found = next(c for c in section.checks if c.label == "バージョン")
        self.assertEqual(found.level, presenter.WARN)
        self.assertIn("版の書き方", found.detail)

    def test_変更履歴にいまの版がある(self) -> None:
        """上げたのに履歴を書き忘れた、を機械で止める。"""
        from pathlib import Path as _Path
        text = (_Path(__file__).resolve().parent.parent
                / "docs" / "変更履歴.md").read_text(encoding="utf-8")
        self.assertIn(app_config.version_label(), text,
                      "docs/変更履歴.md にいまの版の節がありません")

    def test_変更履歴の目次が本文と合っている(self) -> None:
        """版を足したのに目次を作り直し忘れた、を機械で止める。

        目次は `scripts/make_changelog_index.py` が節見出しから作る。
        落ちたら `python3 scripts/make_changelog_index.py` を流す。
        """
        import importlib.util
        from pathlib import Path as _Path
        root = _Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "make_changelog_index", root / "scripts" / "make_changelog_index.py")
        index = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(index)
        text = (root / "docs" / "変更履歴.md").read_text(encoding="utf-8")
        self.assertIn(index.BEGIN, text, "変更履歴に項目別の目次がありません")
        self.assertEqual(index.apply(text), text,
                         "変更履歴の目次が古いままです。"
                         "python3 scripts/make_changelog_index.py を流してください")
        # 実績の保存・呼び出し(VER2.85.0)が目次から引けること(現場で探せなかった)
        line = next(l for l in text.splitlines() if "**実績パターン" in l)
        self.assertIn("[VER2.85.0](#ver2850)", line)


class FallbackTests(_ConfigTestCase):
    """壊れた設定でも起動できること(基盤仕様書 ステップ5)。"""

    def test_ファイルが無ければ既定値で動く(self) -> None:
        app_config.CONFIG_PATH = Path(self._tmp.name) / "存在しない.json"
        app_config.load(force=True)
        self.assertEqual(app_config.app_id(), "nlm.packaging-tool")
        self.assertIn("設定ファイルがありません", app_config.load_error())

    def test_壊れたJSONでも既定値で動く(self) -> None:
        self._use("{ これはJSONではない ")
        self.assertEqual(app_config.app_id(), "nlm.packaging-tool")
        self.assertIn("読めませんでした", app_config.load_error())

    def test_配列を書いても既定値で動く(self) -> None:
        self._use([1, 2, 3])
        self.assertEqual(app_config.app_id(), "nlm.packaging-tool")
        self.assertTrue(app_config.load_error())

    def test_キーが足りなくても残りは既定値で埋まる(self) -> None:
        """1つ書き忘れただけで起動できなくなる、を防ぐ。"""
        self._use({"display_name": "試験用"})
        self.assertEqual(app_config.display_name(), "試験用")     # 指定は効く
        self.assertEqual(app_config.app_id(), "nlm.packaging-tool")  # 残りは既定値
        self.assertEqual(app_config.port(modes.FIELD), 8713)
        self.assertEqual(app_config.load_error(), "")

    def test_入れ子も部分的に上書きできる(self) -> None:
        self._use({"server": {"roles": {"field": {"port": 9999}}}})
        self.assertEqual(app_config.port(modes.FIELD), 9999)
        # 同じ入れ子の中の触っていない値は残る
        self.assertEqual(app_config.host(), "127.0.0.1")

    def test_既定値は書き換えられない(self) -> None:
        """上書きが `_FALLBACK` を汚すと、次の読み込みに漏れる。"""
        self._use({"server": {"roles": {"field": {"port": 9999}}}})
        app_config.CONFIG_PATH = Path(self._tmp.name) / "無い.json"
        app_config.load(force=True)
        self.assertEqual(app_config.port(modes.FIELD), 8713)

    def test_未知のモードは例外(self) -> None:
        app_config.load(force=True)
        with self.assertRaises(ValueError):
            app_config.port("けんさ")


class PortConflictDetectionTests(_ConfigTestCase):
    """重なり検査そのものが効いているか(検査が空振りしていないこと)。"""

    def test_重なりを検出する(self) -> None:
        self._use({"server": {"port_retry": 3,
                              "roles": {"field": {"port": 8713},
                                        "warehouse": {"port": 8714}}}})
        problems = app_config.port_range_conflicts()
        self.assertTrue(problems, "重なっているのに検出できていません")
        self.assertIn("8714", problems[0])

    def test_離れていれば検出しない(self) -> None:
        self._use({"server": {"port_retry": 3,
                              "roles": {"field": {"port": 8713},
                                        "warehouse": {"port": 8723}}}})
        self.assertEqual(app_config.port_range_conflicts(), [])

    def test_候補は再試行回数ぶん(self) -> None:
        self._use({"server": {"port_retry": 2, "roles": {"field": {"port": 8000}}}})
        self.assertEqual(app_config.port_candidates(modes.FIELD),
                         [8000, 8001, 8002])


class LocalRootTests(_ConfigTestCase):
    """ユーザー別ローカル領域(基盤仕様書 2.7 / 4.6)。"""

    def setUp(self) -> None:
        super().setUp()
        self._env = {k: os.environ.get(k)
                     for k in ("PACKAGING_TOOL_LOCAL_DIR", "LOCALAPPDATA", "XDG_DATA_HOME")}
        for key in self._env:
            os.environ.pop(key, None)
        app_config.load(force=True)

    def tearDown(self) -> None:
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def test_環境変数で丸ごと差し替えられる(self) -> None:
        os.environ["PACKAGING_TOOL_LOCAL_DIR"] = self._tmp.name
        self.assertEqual(app_config.local_root(), Path(self._tmp.name))

    def test_WindowsではLOCALAPPDATAの下(self) -> None:
        os.environ["LOCALAPPDATA"] = r"C:\Users\test\AppData\Local"
        self.assertEqual(app_config.local_root().name, "PackagingTool")
        self.assertIn("AppData", str(app_config.local_root()))

    def test_非WindowsではXDGの下(self) -> None:
        os.environ["XDG_DATA_HOME"] = "/tmp/xdg"
        self.assertEqual(app_config.local_root(), Path("/tmp/xdg/PackagingTool"))

    def test_必要なフォルダがそろっている(self) -> None:
        """基盤仕様書 4.6 の6つ + DBを置く data。"""
        for name in ("runtime", "logs", "pycache", "cache", "work", "backup", "data"):
            self.assertIn(name, app_config.LOCAL_SUBDIRS)

    def test_作成できる(self) -> None:
        os.environ["PACKAGING_TOOL_LOCAL_DIR"] = str(Path(self._tmp.name) / "領域")
        root = app_config.ensure_local_dirs()
        for name in app_config.LOCAL_SUBDIRS:
            self.assertTrue((root / name).is_dir(), f"{name} が作られていません")
        app_config.ensure_local_dirs()       # 2回目でも壊れない

    def test_未知の領域名は例外(self) -> None:
        with self.assertRaises(ValueError):
            app_config.local_dir("どこか")


class DescribeTests(_ConfigTestCase):
    """診断表示(基盤仕様書 2.6)。"""

    def test_主要な値が出る(self) -> None:
        app_config.load(force=True)
        text = app_config.describe()
        for expected in ("アプリID", "ローカル領域", "ポート", app_config.app_id()):
            self.assertIn(expected, text)

    def test_既定値へ落ちた理由が出る(self) -> None:
        app_config.CONFIG_PATH = Path(self._tmp.name) / "無い.json"
        app_config.load(force=True)
        self.assertIn("【注意】", app_config.describe())

    def test_ポートの重なりも警告に出る(self) -> None:
        self._use({"server": {"roles": {"warehouse": {"port": 8714}}}})
        self.assertIn("重なっています", app_config.describe())


class LegacyModeNameTests(unittest.TestCase):
    """現場に配ってある `config/app.json` は `warehouse` で書かれている。

    そのままだと既定値の `material` と別のキーとして並び、**ポートを
    変えてあっても黙って無視される**。変えたつもりで変わらない、という
    一番たちの悪い壊れ方なので、ここで固定する。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._original = app_config.CONFIG_PATH
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        app_config.CONFIG_PATH = self._original
        app_config.load(force=True)

    def _use(self, data: dict) -> None:
        path = Path(self._tmp.name) / "app.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        app_config.CONFIG_PATH = path
        app_config.load(force=True)

    def test_旧名で書かれたポートが効く(self) -> None:
        self._use({"server": {"roles": {"warehouse": {"port": 9100}}}})
        self.assertEqual(app_config.port(modes.MATERIAL), 9100)

    def test_旧名で引いてもいまの名前で引いても同じ(self) -> None:
        self._use({"server": {"roles": {"warehouse": {"port": 9100}}}})
        self.assertEqual(app_config.port("warehouse"),
                         app_config.port(modes.MATERIAL))

    def test_いまの名前で書いてあればそのまま(self) -> None:
        self._use({"server": {"roles": {"material": {"port": 9200}}}})
        self.assertEqual(app_config.port(modes.MATERIAL), 9200)

    def test_キーが1つに寄る(self) -> None:
        """**同じ事実を2か所に持たない。** 旧名を残すと、どちらが
        効いているのかを読む人が判断しなければならない。"""
        self._use({"server": {"roles": {"warehouse": {"port": 9100}}}})
        self.assertNotIn("warehouse", app_config.load()["server"]["roles"])


if __name__ == "__main__":
    unittest.main()
