"""配布設定 ── 1台で決めた設定を、配った先の端末で読み込む"""
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


class Terminal:
    """1台ぶんの設定ファイルと配置図の置き場。"""

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
        patcher = mock.patch.object(distribution, "PATH",
                                    self.root / "tool" / "config" / "distribution.json")
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
        admin_password.change(PW, "newpass123", "newpass123")

    def export(self, items=None, maps=(), password="newpass123"):
        items = [k for k, _, default in distribution.ITEMS if default] \
            if items is None else items
        return distribution.export(password, list(items), list(maps))


class ExportTests(DistributionTestCase):
    def test_パスワードが無ければ書き出さない(self) -> None:
        self.configure_source()
        result = self.export(password="ちがう")
        self.assertEqual(result.reason, distribution.REFUSE_NEED_PASSWORD)
        self.assertFalse(distribution.PATH.exists())

    def test_選んだものだけ_拠点は既定で入れない(self) -> None:
        self.configure_source()
        self.assertTrue(self.export().ok)
        data = json.loads(distribution.PATH.read_text(encoding="utf-8"))
        settings = data["settings"]
        self.assertEqual(settings[config.KEY_MASTER_DB_DIR], r"\\srv\共有\マスタ")
        self.assertIs(settings[config.KEY_AUTO_IMPORT], False)
        self.assertNotIn(user_settings.KEY_POSITION, settings)
        # パスワードは撹拌した値だけ。平文は入らない
        self.assertNotIn("newpass123", distribution.PATH.read_text(encoding="utf-8"))
        self.assertIn(admin_password.KEY, settings)

    def test_設定していない項目は入れない(self) -> None:
        """配った先の既定値を「空」で上書きしない。"""
        self.configure_source()
        self.export()
        settings = json.loads(distribution.PATH.read_text(encoding="utf-8"))["settings"]
        self.assertNotIn(config.KEY_KANBAN_DB_DIR, settings)

    def test_画面にパスワードの値を出さない(self) -> None:
        self.configure_source()
        self.export()
        summary = distribution.summary()
        text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(user_settings.get(admin_password.KEY), text)
        self.assertIn("(設定済み)", text)
        self.assertTrue(summary["applied_here"])   # 書き出した端末は使っている

    def test_知らない項目は断る(self) -> None:
        self.configure_source()
        self.assertEqual(self.export(items=["謎"]).reason, distribution.REFUSE_BAD_INPUT)

    def test_消すにもパスワード(self) -> None:
        self.configure_source()
        self.export()
        self.assertFalse(distribution.remove("ちがう").ok)
        self.assertTrue(distribution.PATH.exists())
        self.assertTrue(distribution.remove("newpass123").ok)
        self.assertFalse(distribution.PATH.exists())


class ApplyTests(DistributionTestCase):
    def distributed(self, **kwargs) -> None:
        self.configure_source()
        self.assertTrue(self.export(**kwargs).ok)
        self.dest.use()

    def test_配った先は起動時に読み込む(self) -> None:
        self.distributed()
        result = distribution.apply_on_start()
        self.assertIn("梱包資材マスタの置き場所", result.applied)
        self.assertEqual(user_settings.get(config.KEY_MASTER_DB_DIR), r"\\srv\共有\マスタ")
        self.assertIs(user_settings.get(config.KEY_AUTO_IMPORT), False)
        # パスワードも配った先で通る
        self.assertTrue(admin_password.verify("newpass123"))
        # 拠点は入れていない
        self.assertIsNone(user_settings.get(user_settings.KEY_POSITION))

    def test_同じ配布設定は2度読まない_端末で直した値を守る(self) -> None:
        self.distributed()
        distribution.apply_on_start()
        user_settings.save(config.KEY_AUTO_IMPORT, True)     # 端末で直した
        self.assertEqual(distribution.apply_on_start().applied, [])
        self.assertIs(user_settings.get(config.KEY_AUTO_IMPORT), True)

    def test_新しい配布設定は読む(self) -> None:
        self.distributed()
        distribution.apply_on_start()
        self.source.use()
        user_settings.save(config.KEY_LOT_DB_DIR, r"\\srv\新台帳")
        self.export()
        self.dest.use()
        self.assertTrue(distribution.apply_on_start().applied)
        self.assertEqual(user_settings.get(config.KEY_LOT_DB_DIR), r"\\srv\新台帳")

    def test_無い_壊れた_形が違うなら何もしない(self) -> None:
        self.dest.use()
        self.assertEqual(distribution.apply_on_start().applied, [])
        distribution.PATH.parent.mkdir(parents=True, exist_ok=True)
        distribution.PATH.write_text("{壊れ", encoding="utf-8")
        self.assertEqual(distribution.apply_on_start().applied, [])
        distribution.PATH.write_text(json.dumps({"format": 99, "settings": {
            config.KEY_LOT_DB_DIR: "x"}}), encoding="utf-8")
        self.assertEqual(distribution.apply_on_start().applied, [])
        self.assertIsNone(user_settings.get(config.KEY_LOT_DB_DIR))

    def test_知らない鍵は入れない(self) -> None:
        self.dest.use()
        distribution.PATH.parent.mkdir(parents=True, exist_ok=True)
        distribution.PATH.write_text(json.dumps({"format": 1, "settings": {
            "謎の鍵": 1, config.KEY_LOT_DB_DIR: r"\\x"}}), encoding="utf-8")
        distribution.apply_on_start()
        self.assertNotIn("謎の鍵", user_settings.load_all())
        self.assertEqual(user_settings.get(config.KEY_LOT_DB_DIR), r"\\x")


class MapTests(DistributionTestCase):
    def test_配置図は端末で編集していないときだけ入れる(self) -> None:
        self.configure_source()
        floor_plan.USER_PATH.write_text(json.dumps({"marker": "source"}), encoding="utf-8")
        self.assertTrue(self.export(maps=["floor_plan"]).ok)

        self.dest.use()
        distribution.apply_on_start()
        self.assertEqual(json.loads(floor_plan.USER_PATH.read_text(encoding="utf-8")),
                         {"marker": "source"})

    def test_編集済みの配置図は起動時には上書きしない_読み込み直しなら上書き(self) -> None:
        self.configure_source()
        floor_plan.USER_PATH.write_text(json.dumps({"marker": "source"}), encoding="utf-8")
        self.export(maps=["floor_plan"])
        self.dest.use()
        floor_plan.USER_PATH.write_text(json.dumps({"marker": "dest"}), encoding="utf-8")
        distribution.apply_on_start()
        self.assertEqual(json.loads(floor_plan.USER_PATH.read_text(encoding="utf-8")),
                         {"marker": "dest"})
        self.assertFalse(distribution.reapply("ちがう").ok)
        self.assertTrue(distribution.reapply("newpass123").ok)
        self.assertEqual(json.loads(floor_plan.USER_PATH.read_text(encoding="utf-8")),
                         {"marker": "source"})

    def test_編集していない配置図は入れずに知らせる(self) -> None:
        self.configure_source()
        result = self.export(maps=["pallet_map"])
        self.assertIn("編集していません", result.message)


class StartupTests(unittest.TestCase):
    def test_取り込みより先に読む(self) -> None:
        """置き場所が入っているので、読む前に取り込むと既定の場所を見る。"""
        text = (_ROOT / "start_app.py").read_text(encoding="utf-8")
        body = text[text.index("def _initialize"):]
        self.assertLess(body.index("distribution.apply_on_start()"),
                        body.index("_diagnose_sources()"))

    def test_配布設定は追跡しない(self) -> None:
        self.assertIn("config/distribution.json",
                      (_ROOT / ".gitignore").read_text(encoding="utf-8"))


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
                        {"items": [config.KEY_LOT_DB_DIR], "maps": [],
                         "password": "newpass123"})
        self.assertEqual(res.status_code, 200, res.get_json())
        dist = res.get_json()["distribution"]
        self.assertTrue(dist["exists"])
        self.assertEqual(dist["contents"], [{"label": "仕掛台帳の置き場所",
                                             "value": r"\\srv\台帳"}])

    def test_形が違えば400(self) -> None:
        res = self.post("/api/settings/distribution/export",
                        {"items": "lot", "maps": [], "password": "newpass123"})
        self.assertEqual(res.status_code, 400)

    def test_画面に面がある(self) -> None:
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('id="panel-distribution"', html)
        self.assertIn('data-dist-item="position"', html)


if __name__ == "__main__":
    unittest.main()
