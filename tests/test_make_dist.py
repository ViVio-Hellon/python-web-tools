"""配布用フォルダを作るスクリプト(`scripts/make_dist.py`)"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_spec = importlib.util.spec_from_file_location("make_dist", _ROOT / "scripts" / "make_dist.py")
make_dist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_dist)


class MakeDistTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.out = self.tmp / "dist"

    def test_配るものだけを写す(self) -> None:
        out, _lines = make_dist.build(self.out, with_settings=False)
        names = {p.name for p in out.iterdir()}
        self.assertTrue(set(make_dist.INCLUDE) <= names)
        for bad in ("tests", "data", ".git", "export", "logs"):
            self.assertNotIn(bad, names)
        self.assertFalse(list(out.rglob("__pycache__")))
        self.assertFalse(list(out.rglob("*.pyc")))
        self.assertTrue((out / "packaging_tool" / "distribution.py").exists())
        self.assertTrue((out / "配布メモ.txt").exists())

    def test_配布設定は入れると決めたときだけ(self) -> None:
        settings = _ROOT / make_dist.SETTINGS
        created = not settings.exists()
        if created:
            settings.write_text(json.dumps({
                "format": 1, "settings": {"lot_db_dir": r"\\srv\台帳",
                                          "admin_password": "pbkdf2$1$x$y"},
                "maps": {}}, ensure_ascii=False), encoding="utf-8")
            self.addCleanup(settings.unlink)
        out, lines = make_dist.build(self.out)
        self.assertTrue((out / make_dist.SETTINGS).exists())
        memo = (out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        if created:
            self.assertIn(r"仕掛台帳の置き場所: \\srv\台帳", memo)
            # パスワードの撹拌値もメモには出さない
            self.assertNotIn("pbkdf2$1$x$y", memo)
        out2, _ = make_dist.build(self.tmp / "dist2", with_settings=False)
        self.assertFalse((out2 / make_dist.SETTINGS).exists())

    def test_ツールのフォルダの中には作らない(self) -> None:
        with self.assertRaises(SystemExit):
            make_dist.build(_ROOT / "export" / "dist")

    def test_中身のある場所は作り直すと言われたときだけ(self) -> None:
        self.out.mkdir()
        (self.out / "前の.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit):
            make_dist.build(self.out, with_settings=False)
        self.assertTrue((self.out / "前の.txt").exists())
        make_dist.build(self.out, with_settings=False, force=True)
        self.assertFalse((self.out / "前の.txt").exists())

    def test_直下の一覧と食い違わない(self) -> None:
        """直下に何かを足したら、**配るかどうかを決めさせる**。"""
        from tests.test_web_settings import RepoRootTests
        dev_only = {"tests", ".gitignore", ".gitattributes"}
        self.assertEqual(set(make_dist.INCLUDE), RepoRootTests.ALLOWED - dev_only)

    def test_バッチは英字だけ(self) -> None:
        """cmd.exe はコンソールのコードページで読むので、日本語を入れない。"""
        raw = (_ROOT / "scripts" / "make_dist.bat").read_bytes()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()
