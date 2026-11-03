"""書き込み先をツールのフォルダの外へ(VER4.8.125)。

業務ツール統合ランチャー 1.8.9 からの依頼:「ツールのフォルダーは更新のたびに丸ごと上書きされる前提にしてください。
データベース・利用者が変えた設定・ログ・作ったファイルを、ツールのフォルダーの中に書かないでください」
「すでに中に書いているなら、外へ移し、前の場所にあるものは初回に読み込む(または移す)」。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import app_config, config, legacy_data  # noqa: E402


def _inside(path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(_ROOT.resolve())
        return True
    except ValueError:
        return False


class DefaultsTests(unittest.TestCase):
    """既定の置き場所がツールのフォルダの外であること(環境変数で決めていないとき)。"""

    def defaults(self) -> dict[str, Path]:
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]);"
                "from packaging_tool import config, floor_plan, pallet_map;"
                "print(json.dumps({k: str(v) for k, v in {"
                "'db': config.DB_PATH, 'settings': config.USER_CONFIG_PATH, 'logs': config.LOG_DIR,"
                "'export': config.DEFAULT_EXPORT_DIR, 'floor': floor_plan.USER_PATH,"
                "'pallet': pallet_map.USER_PATH}.items()}))")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PACKAGING_TOOL_")}
        env["PACKAGING_TOOL_LOCAL_DIR"] = tempfile.mkdtemp(prefix="outside_")
        out = subprocess.run([sys.executable, "-B", "-c", code, str(_ROOT)], env=env,
                             capture_output=True, text=True, check=True).stdout
        return {k: Path(v) for k, v in json.loads(out).items()}

    def test_DB_設定_ログ_直した図_書き出し先はツールのフォルダの外(self) -> None:
        for name, path in self.defaults().items():
            with self.subTest(name=name):
                self.assertFalse(_inside(path), f"{name}: {path}")


class MigrateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.old = root / "ツール" / "data"
        self.old.mkdir(parents=True)
        self.new = root / "ローカル" / "data"
        env = {k: v for k, v in os.environ.items()
               if k not in ("PACKAGING_TOOL_DB_PATH", "PACKAGING_TOOL_CONFIG_PATH")}
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(legacy_data, "LEGACY_DATA_DIR", self.old),
                        mock.patch.object(config, "DB_PATH", self.new / "packaging_tool.db"),
                        mock.patch.object(config, "USER_CONFIG_PATH", self.new / "user_config.json")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def put_old(self) -> None:
        with closing(sqlite3.connect(self.old / "packaging_tool.db")) as raw, raw:
            raw.execute("CREATE TABLE t (a)")
            raw.execute("INSERT INTO t VALUES ('まだ送っていない発注')")
        (self.old / "user_config.json").write_text('{"position": "HVC"}', encoding="utf-8")
        (self.old / "floor_plan.json").write_text('{"canvas": {}}', encoding="utf-8")

    def test_前の場所のものを初回に写す_前の場所は消さない(self) -> None:
        self.put_old()
        notes = legacy_data.migrate()
        self.assertEqual(len(notes), 3)
        with closing(sqlite3.connect(self.new / "packaging_tool.db")) as raw:
            self.assertEqual(raw.execute("SELECT a FROM t").fetchall(), [("まだ送っていない発注",)])
        self.assertEqual(json.loads((self.new / "user_config.json").read_text("utf-8")), {"position": "HVC"})
        self.assertTrue((self.new / "floor_plan.json").exists())
        self.assertTrue((self.old / "packaging_tool.db").exists())       # 共有のフォルダならほかの PC も写す

    def test_新しい場所にもうあれば写さない(self) -> None:
        self.put_old()
        self.new.mkdir(parents=True)
        (self.new / "user_config.json").write_text('{"position": "L-1"}', encoding="utf-8")
        legacy_data.migrate()
        self.assertEqual(json.loads((self.new / "user_config.json").read_text("utf-8")), {"position": "L-1"})
        self.assertEqual(legacy_data.migrate(), [])                       # 2回目は何もしない

    def test_読めなければ写さず_途中のファイルを残さない(self) -> None:
        self.put_old()
        with mock.patch.object(legacy_data, "_copy_db", side_effect=sqlite3.OperationalError("locked")):
            notes = legacy_data.migrate()
        self.assertFalse((self.new / "packaging_tool.db").exists())
        self.assertFalse(any(p.name.endswith(".移し中") for p in self.new.iterdir()))
        self.assertEqual(len(notes), 2)                                   # 設定と図は写した

    def test_環境変数で決めた場所には写さない(self) -> None:
        self.put_old()
        with mock.patch.dict(os.environ, {"PACKAGING_TOOL_DB_PATH": "x", "PACKAGING_TOOL_CONFIG_PATH": "y"}):
            self.assertEqual(legacy_data.plan(), [])


class PycacheTests(unittest.TestCase):
    def test_入口はツールのフォルダの中に_pycache_を書かない(self) -> None:
        """`process_manager.py --status`(stop.bat が呼ぶ)を動かしても、ツールのフォルダに .pyc ができない。"""
        local = Path(tempfile.mkdtemp(prefix="pyc_"))
        before = {p for p in _ROOT.rglob("*.pyc")}
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")}
        env["PACKAGING_TOOL_LOCAL_DIR"] = str(local)
        subprocess.run([sys.executable, str(_ROOT / "process_manager.py"), "--status"], env=env,
                       capture_output=True, text=True, timeout=60)
        made = {p for p in _ROOT.rglob("*.pyc")} - before
        self.assertEqual(made, set(), "ツールのフォルダの中に .pyc を書きました")
        self.assertTrue(any((local / "pycache").rglob("*.pyc")), "外の pycache に書いていません")

    def test_置き場所を外へ向ける(self) -> None:
        with mock.patch.dict(os.environ, {"PACKAGING_TOOL_LOCAL_DIR": tempfile.mkdtemp(prefix="pyc2_")}), \
                mock.patch.object(sys, "pycache_prefix", None), \
                mock.patch.object(sys, "dont_write_bytecode", True):
            os.environ.pop("PYTHONPYCACHEPREFIX", None)
            app_config.use_local_pycache()
            self.assertFalse(_inside(Path(sys.pycache_prefix)))
            self.assertFalse(sys.dont_write_bytecode)
            os.environ.pop("PYTHONPYCACHEPREFIX", None)


if __name__ == "__main__":
    unittest.main()
