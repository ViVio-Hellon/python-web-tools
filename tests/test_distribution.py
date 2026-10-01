"""配布設定 ── ツールの直下の `配布設定\\` を、配った先の端末が読み込む

    1. 一度起動して配布先の設定をする(設定画面の「配布設定」で書き出す)
    2. `配布設定\\` が作られ、配下に必要なものが入る
    3. 配った先は `配布設定\\` があれば読み込む
    4. その端末にすでにあるもの(設定・保存した配置図)は読み込まない
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402,F401

from packaging_tool import (admin_password, config, distribution,  # noqa: E402
                            floor_plan, pallet_map, user_settings)

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

PW = config.ADMIN_PASSWORD
NEW_PW = "newpass123"


class Terminal:
    """1台ぶんの設定ファイルと、保存した配置図の置き場。"""

    def __init__(self, case: unittest.TestCase, root: Path, name: str) -> None:
        self.dir = root / name
        self.dir.mkdir()
        self.case = case

    def use(self) -> None:
        for target, attr, value in (
                (config, "USER_CONFIG_PATH", self.dir / "user_config.json"),
                (floor_plan, "USER_PATH", self.dir / "floor_plan.json"),
                (pallet_map, "USER_PATH", self.dir / "pallet_map.json")):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.case.addCleanup(patcher.stop)


class DistributionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(distribution, "DIR", self.root / "tool" / "配布設定")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = Terminal(self, self.root, "source")
        self.dest = Terminal(self, self.root, "dest")

    def configure_source(self) -> None:
        self.source.use()
        user_settings.save(config.KEY_MASTER_DB_DIR, r"\\srv\共有\マスタ")
        user_settings.save(config.KEY_LOT_DB_DIR, r"\\srv\台帳")
        user_settings.save(config.KEY_AUTO_IMPORT, False)
        user_settings.save(config.KEY_SPEC_SHEET_URL, "http://spec/{spec}")
        user_settings.set_position("L1")
        admin_password.change(PW, NEW_PW, NEW_PW)

    def export(self, items=None, maps=(), password=NEW_PW):
        items = [k for k, _, default in distribution.ITEMS if default] \
            if items is None else items
        return distribution.export(password, list(items), list(maps))

    def written(self) -> dict:
        return json.loads(distribution.settings_path().read_text(encoding="utf-8"))


class ExportTests(DistributionTestCase):
    def test_パスワードが無ければ作らない(self) -> None:
        self.configure_source()
        result = self.export(password="ちがう")
        self.assertEqual(result.reason, distribution.REFUSE_NEED_PASSWORD)
        self.assertFalse(distribution.DIR.exists())

    def test_配布設定フォルダが作られ配下に入る(self) -> None:
        self.configure_source()
        self.assertTrue(self.export(maps=["floor_plan", "pallet_map"]).ok)
        names = {p.relative_to(distribution.DIR).as_posix()
                 for p in distribution.DIR.rglob("*") if p.is_file()}
        self.assertEqual(names, {"設定.json", "配置図/floor_plan.json",
                                 "配置図/pallet_map.json", "はじめに読む.txt"})

    def test_選んだものだけ_拠点は既定で入れない(self) -> None:
        self.configure_source()
        self.export()
        settings = self.written()["settings"]
        self.assertEqual(settings[config.KEY_MASTER_DB_DIR], r"\\srv\共有\マスタ")
        self.assertIs(settings[config.KEY_AUTO_IMPORT], False)
        self.assertNotIn(user_settings.KEY_POSITION, settings)
        # パスワードは撹拌した値だけ。平文はどこにも入らない
        for path in distribution.DIR.rglob("*"):
            if path.is_file():
                self.assertNotIn(NEW_PW, path.read_text(encoding="utf-8-sig"), path)
        self.assertIn(admin_password.KEY, settings)

    def test_設定していない項目は入れない(self) -> None:
        """配った先の既定値を「空」で上書きしない。"""
        self.configure_source()
        result = self.export()
        self.assertNotIn(config.KEY_KANBAN_DB_DIR, self.written()["settings"])
        self.assertIn("既定のまま", result.message)

    def test_配置図は保存したもの_無ければ出荷時の配置(self) -> None:
        self.configure_source()
        floor_plan.USER_PATH.write_text(json.dumps({"marker": "own"}), encoding="utf-8")
        self.export(maps=["floor_plan", "pallet_map"])
        self.assertEqual(json.loads(distribution.map_file("floor_plan")
                                    .read_text(encoding="utf-8")), {"marker": "own"})
        self.assertEqual(distribution.map_file("pallet_map").read_bytes(),
                         pallet_map.DEFAULT_PATH.read_bytes())

    def test_書き出し直すと前の中身は置き換わる(self) -> None:
        self.configure_source()
        self.export(maps=["floor_plan"])
        self.export(maps=[])
        self.assertFalse(distribution.map_file("floor_plan").exists())

    def test_画面にパスワードの値を出さない(self) -> None:
        self.configure_source()
        self.export()
        text = json.dumps(distribution.summary(), ensure_ascii=False)
        self.assertNotIn(user_settings.get(admin_password.KEY), text)
        self.assertIn("(設定済み)", text)

    def test_知らない項目は断る(self) -> None:
        self.configure_source()
        self.assertEqual(self.export(items=["謎"]).reason, distribution.REFUSE_BAD_INPUT)

    def test_消すにもパスワード(self) -> None:
        self.configure_source()
        self.export()
        self.assertFalse(distribution.remove("ちがう").ok)
        self.assertTrue(distribution.DIR.exists())
        self.assertTrue(distribution.remove(NEW_PW).ok)
        self.assertFalse(distribution.DIR.exists())


class ApplyTests(DistributionTestCase):
    def distributed(self, **kwargs) -> None:
        self.configure_source()
        self.assertTrue(self.export(**kwargs).ok)
        self.dest.use()

    def test_配布設定フォルダがあれば読み込む(self) -> None:
        self.distributed()
        result = distribution.apply_on_start()
        self.assertIn("梱包資材マスタの置き場所", result.applied)
        self.assertEqual(user_settings.get(config.KEY_MASTER_DB_DIR), r"\\srv\共有\マスタ")
        self.assertIs(user_settings.get(config.KEY_AUTO_IMPORT), False)
        self.assertTrue(admin_password.verify(NEW_PW))
        self.assertIsNone(user_settings.get(user_settings.KEY_POSITION))

    def test_既存データがある項目は読み込まない(self) -> None:
        self.distributed()
        user_settings.save(config.KEY_LOT_DB_DIR, r"\\この端末\台帳")
        result = distribution.apply_on_start()
        self.assertEqual(user_settings.get(config.KEY_LOT_DB_DIR), r"\\この端末\台帳")
        self.assertIn("仕掛台帳の置き場所", result.kept)
        # 無い項目は埋める
        self.assertEqual(user_settings.get(config.KEY_MASTER_DB_DIR), r"\\srv\共有\マスタ")

    def test_起動のたびに見ても端末で直した値は戻さない(self) -> None:
        self.distributed()
        distribution.apply_on_start()
        user_settings.save(config.KEY_AUTO_IMPORT, True)     # 端末で直した
        self.assertEqual(distribution.apply_on_start().applied, [])
        self.assertIs(user_settings.get(config.KEY_AUTO_IMPORT), True)

    def test_読み込み直しはパスワードで上書き(self) -> None:
        self.distributed()
        user_settings.save(config.KEY_LOT_DB_DIR, r"\\この端末\台帳")
        # 照合するのは**この端末の**パスワード(まだ読み込んでいないので既定)
        self.assertFalse(distribution.reapply("ちがう").ok)
        self.assertTrue(distribution.reapply(PW).ok)
        self.assertEqual(user_settings.get(config.KEY_LOT_DB_DIR), r"\\srv\台帳")

    def test_無い_壊れた_形が違うなら何もしない(self) -> None:
        self.dest.use()
        self.assertEqual(distribution.apply_on_start().applied, [])
        distribution.DIR.mkdir(parents=True)
        distribution.settings_path().write_text("{壊れ", encoding="utf-8")
        self.assertEqual(distribution.apply_on_start().applied, [])
        distribution.settings_path().write_text(json.dumps({"format": 99, "settings": {
            config.KEY_LOT_DB_DIR: "x"}}), encoding="utf-8")
        self.assertEqual(distribution.apply_on_start().applied, [])
        self.assertIsNone(user_settings.get(config.KEY_LOT_DB_DIR))

    def test_知らない鍵は入れない(self) -> None:
        self.dest.use()
        distribution.DIR.mkdir(parents=True)
        distribution.settings_path().write_text(json.dumps({"format": 1, "settings": {
            "謎の鍵": 1, config.KEY_LOT_DB_DIR: r"\\x"}}), encoding="utf-8")
        distribution.apply_on_start()
        self.assertNotIn("謎の鍵", user_settings.load_all())
        self.assertEqual(user_settings.get(config.KEY_LOT_DB_DIR), r"\\x")


class RedistributeTests(DistributionTestCase):
    """現場の声:「配布先で設定しないことがある」。

    以前の決まりは「すでにあるものは読み込まない」だけで、
      ・空の値も「すでにある」と数え、いつまでも埋めなかった
      ・設定を直して配り直しても、一度読んだ端末は前の値のままだった
    """

    def distributed(self) -> None:
        self.configure_source()
        self.assertTrue(self.export().ok)
        self.dest.use()

    def test_空の値は入っていないのと同じに扱って埋める(self) -> None:
        self.distributed()
        user_settings.save(config.KEY_MASTER_DB_DIR, "")       # 空欄のまま保存した
        user_settings.save(admin_password.KEY, "")              # 既定に戻した
        result = distribution.apply_on_start()
        self.assertIn("梱包資材マスタの置き場所", result.applied)
        self.assertEqual(user_settings.get(config.KEY_MASTER_DB_DIR), r"\\srv\共有\マスタ")
        self.assertTrue(admin_password.verify(NEW_PW))

    def test_配り直したら前の配布のまま変えていない項目は入れ替える(self) -> None:
        self.distributed()
        distribution.apply_on_start()
        user_settings.save(config.KEY_LOT_DB_DIR, r"\\この端末\台帳")   # 端末で変えた
        # 配る側で置き場所を直して配り直す
        self.source.use()
        user_settings.save(config.KEY_MASTER_DB_DIR, r"\\新srv\マスタ")
        user_settings.save(config.KEY_LOT_DB_DIR, r"\\新srv\台帳")
        self.assertTrue(self.export().ok)
        self.dest.use()
        result = distribution.apply_on_start()
        self.assertIn("梱包資材マスタの置き場所", result.applied)
        self.assertEqual(user_settings.get(config.KEY_MASTER_DB_DIR), r"\\新srv\マスタ")
        # 端末で変えた項目はそのまま
        self.assertIn("仕掛台帳の置き場所", result.kept)
        self.assertEqual(user_settings.get(config.KEY_LOT_DB_DIR), r"\\この端末\台帳")
        # 同じ配布設定は2度読まない
        self.assertEqual(distribution.apply_on_start().applied, [])

    def test_配り直した配置図は端末で保存していなければ入れ替える(self) -> None:
        self.configure_source()
        floor_plan.USER_PATH.write_text(json.dumps({"marker": "v1"}), encoding="utf-8")
        pallet_map.USER_PATH.write_text(json.dumps({"marker": "p1"}), encoding="utf-8")
        self.export(maps=["floor_plan", "pallet_map"])
        self.dest.use()
        distribution.apply_on_start()
        pallet_map.USER_PATH.write_text(json.dumps({"marker": "端末で編集"}), encoding="utf-8")
        self.source.use()
        floor_plan.USER_PATH.write_text(json.dumps({"marker": "v2"}), encoding="utf-8")
        pallet_map.USER_PATH.write_text(json.dumps({"marker": "p2"}), encoding="utf-8")
        self.export(maps=["floor_plan", "pallet_map"])
        self.dest.use()
        result = distribution.apply_on_start()
        self.assertEqual(json.loads(floor_plan.USER_PATH.read_text(encoding="utf-8")),
                         {"marker": "v2"})
        self.assertIn("簡易在庫の保管位置マップ", result.kept)
        self.assertEqual(json.loads(pallet_map.USER_PATH.read_text(encoding="utf-8")),
                         {"marker": "端末で編集"})


class MapTests(DistributionTestCase):
    def test_保存した配置図が無い端末には入れる(self) -> None:
        self.configure_source()
        floor_plan.USER_PATH.write_text(json.dumps({"marker": "source"}), encoding="utf-8")
        self.export(maps=["floor_plan"])
        self.dest.use()
        self.assertIn("棚検索の配置図", distribution.apply_on_start().applied)
        self.assertEqual(json.loads(floor_plan.USER_PATH.read_text(encoding="utf-8")),
                         {"marker": "source"})

    def test_保存した配置図がある端末には入れない(self) -> None:
        self.configure_source()
        floor_plan.USER_PATH.write_text(json.dumps({"marker": "source"}), encoding="utf-8")
        self.export(maps=["floor_plan"])
        self.dest.use()
        floor_plan.USER_PATH.write_text(json.dumps({"marker": "dest"}), encoding="utf-8")
        self.assertIn("棚検索の配置図", distribution.apply_on_start().kept)
        self.assertEqual(json.loads(floor_plan.USER_PATH.read_text(encoding="utf-8")),
                         {"marker": "dest"})
        self.assertTrue(distribution.reapply(NEW_PW).ok)
        self.assertEqual(json.loads(floor_plan.USER_PATH.read_text(encoding="utf-8")),
                         {"marker": "source"})

    def test_配置図は置き換えるだけで差し替えられる(self) -> None:
        """フォルダの中の .json を手で置き換えれば、それが配られる。"""
        self.dest.use()
        (distribution.DIR / distribution.MAPS_DIRNAME).mkdir(parents=True)
        distribution.map_file("pallet_map").write_text(
            json.dumps({"marker": "hand"}), encoding="utf-8")
        self.assertIn("簡易在庫の保管位置マップ", distribution.apply_on_start().applied)
        self.assertEqual(json.loads(pallet_map.USER_PATH.read_text(encoding="utf-8")),
                         {"marker": "hand"})


class StartupTests(unittest.TestCase):
    def test_取り込みより先に読む(self) -> None:
        """置き場所が入っているので、読む前に取り込むと既定の場所を見る。"""
        text = (_ROOT / "start_app.py").read_text(encoding="utf-8")
        body = text[text.index("def _initialize"):]
        self.assertLess(body.index("distribution.apply_on_start()"),
                        body.index("_diagnose_sources()"))

    def test_配布設定は追跡しない(self) -> None:
        self.assertIn("配布設定/", (_ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_置き場所はツールの直下(self) -> None:
        self.assertEqual(Path(distribution.__file__).resolve().parent.parent / "配布設定",
                         config.BASE_DIR / "配布設定")


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class WebTests(DistributionTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tests import _web
        from app.routes import settings as routes
        self.configure_source()
        _web.bind_db(self, routes)
        self.client = _web.make_client()
        self.auth = _web.auth()

    def post(self, path, body):
        return self.client.post(path, json=body, headers=self.auth)

    def test_パスワードが違えば403(self) -> None:
        res = self.post("/api/settings/distribution/export",
                        {"items": [config.KEY_LOT_DB_DIR], "maps": [], "password": "x"})
        self.assertEqual(res.status_code, 403)

    def test_書き出すと状態に出る(self) -> None:
        res = self.post("/api/settings/distribution/export",
                        {"items": [config.KEY_LOT_DB_DIR], "maps": ["pallet_map"],
                         "password": NEW_PW})
        self.assertEqual(res.status_code, 200, res.get_json())
        dist = res.get_json()["distribution"]
        self.assertTrue(dist["exists"])
        self.assertEqual(dist["contents"], [
            {"label": "仕掛台帳の置き場所", "value": r"\\srv\台帳"},
            {"label": "簡易在庫の保管位置マップ", "value": r"配置図\pallet_map.json"}])

    def test_形が違えば400(self) -> None:
        res = self.post("/api/settings/distribution/export",
                        {"items": "lot", "maps": [], "password": NEW_PW})
        self.assertEqual(res.status_code, 400)

    def test_画面に面がある(self) -> None:
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('id="panel-distribution"', html)
        self.assertIn('data-dist-item="position"', html)


if __name__ == "__main__":
    unittest.main()
