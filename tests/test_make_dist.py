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
    def test_既定の配布フォルダの名前は資材複合ツール_VER版(self) -> None:
        """現場の指示「配布フォルダの名前を 資材複合ツール に」。中の exe の名前は変えない。"""
        version = json.loads((_ROOT / "config" / "app.json").read_text(encoding="utf-8"))["version"]
        out = make_dist.default_out()
        self.assertEqual(out.name, f"資材複合ツール_VER{version}")
        self.assertEqual(out.parent, _ROOT.parent)                 # ツールの隣
        self.assertEqual(make_dist.EXE_NAME, "梱包資材総合ツール.exe")

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
        memo = (out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn("scripts\\make_shortcuts.vbs", memo)          # 配った先ですること

    def test_配布設定フォルダは入れると決めたときだけ(self) -> None:
        from unittest import mock
        from packaging_tool import distribution
        src = self.tmp / "tool_settings"
        (src / distribution.MAPS_DIRNAME).mkdir(parents=True)
        distribution.settings_path(src).write_text(json.dumps({
            "format": 1, "settings": {"lot_db_dir": r"\\srv\台帳",
                                      "admin_password": "pbkdf2$1$x$y"}},
            ensure_ascii=False), encoding="utf-8")
        distribution.map_file("floor_plan", src).write_text("{}", encoding="utf-8")
        with mock.patch.object(make_dist, "SETTINGS", Path("配布設定")), \
                mock.patch.object(make_dist, "ROOT", make_dist.ROOT):
            real = make_dist.ROOT / make_dist.SETTINGS
            if real.exists():
                self.skipTest("開発機に配布設定があるため")
            import shutil
            shutil.copytree(src, real)
            self.addCleanup(shutil.rmtree, real, True)
            out, _lines = make_dist.build(self.out)
            out2, _ = make_dist.build(self.tmp / "dist2", with_settings=False)
        self.assertTrue(distribution.settings_path(out / "配布設定").exists())
        self.assertTrue(distribution.map_file("floor_plan", out / "配布設定").exists())
        memo = (out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn(r"仕掛台帳の置き場所: \\srv\台帳", memo)
        self.assertIn(r"棚検索の配置図: 配置図\floor_plan.json", memo)
        self.assertNotIn("pbkdf2$1$x$y", memo)
        self.assertFalse((out2 / "配布設定").exists())

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
        dev_only = {"tests", ".gitignore", ".gitattributes"} | set(make_dist.DEV_ONLY)
        self.assertEqual(set(make_dist.INCLUDE), RepoRootTests.ALLOWED - dev_only)

    def test_デスクトップ版のexeは作ってあれば入れる(self) -> None:
        exe = self.tmp / "PackagingTool.exe"
        exe.write_bytes(b"MZ")
        out, lines = make_dist.build(self.out, with_settings=False, exe=exe)
        self.assertEqual((out / make_dist.EXE_NAME).read_bytes(), b"MZ")
        self.assertTrue((out / "bridge.py").exists())
        memo = (out / "配布メモ.txt").read_text(encoding="utf-8-sig")
        self.assertIn(make_dist.EXE_NAME, memo)
        self.assertFalse((out / "src-tauri").exists())

    def test_exeが無ければブラウザ版だけと書く(self) -> None:
        out, lines = make_dist.build(self.out, with_settings=False, exe=self.tmp / "無い.exe")
        self.assertFalse((out / make_dist.EXE_NAME).exists())
        self.assertIn("デスクトップ版(exe)は入っていません", "\n".join(lines))

    def test_バッチは英字だけ(self) -> None:
        """cmd.exe はコンソールのコードページで読むので、日本語を入れない。"""
        raw = (_ROOT / "scripts" / "make_dist.bat").read_bytes()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()


class ShortcutScriptTests(unittest.TestCase):
    """配った先で押す `scripts\\make_shortcuts.vbs`(現場の依頼: 配布フォルダを配ったあとに押して、
    Start.vbs と exe のショートカットを**そのときの場所**で作る)。"""

    PATH = _ROOT / "scripts" / "make_shortcuts.vbs"

    def text(self) -> str:
        return self.PATH.read_bytes().decode("cp932")

    def test_CP932_CRLF_BOMなし(self) -> None:
        data = self.PATH.read_bytes()
        self.assertNotEqual(data[:3], b"\xef\xbb\xbf")
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))       # LF だけの行が無い
        self.assertIn("資材複合ツール", self.text())                  # CP932 として正しい語になる

    def names(self) -> dict[str, str]:
        """`NAME = U("8CC7 6750 …") [& ".exe"]` を文字にもどす(.vbs の中では文字の番号で持つ)。"""
        import re
        out = {}
        for name, codes, tail in re.findall(r'^(\w+) = U\("([0-9A-F ]+)"\)(?: & "([^"]*)")?',
                                            self.text(), re.M):
            out[name] = "".join(chr(int(c, 16)) for c in codes.split()) + tail
        return out

    def test_日本語の名前は文字の番号で持つ(self) -> None:
        """英語の Windows(CI)でも名前が化けないように。実際に CI で
        「資材複合ツール(ブラウザ版).lnk」が化けた名前でできていた。"""
        self.assertEqual(self.names(), {
            "APP_NAME": make_dist.DIST_FOLDER_NAME, "EXE_NAME": make_dist.EXE_NAME,
            "BROWSER": "(ブラウザ版)", "DESKTOP": "(デスクトップ版)"})
        for line in self.text().splitlines():
            code = line.split("'", 1)[0]                    # 注釈は除く
            if '"' in code:
                quoted = code.split('"')[1::2]
                self.assertFalse(any(".lnk" in q and not q.isascii() for q in quoted), line)

    def test_Start_vbsとexeを指す_場所は押したときのフォルダ(self) -> None:
        text = self.text()
        self.assertIn('MakeLink APP_NAME & BROWSER & ".lnk", "Start.vbs", False', text)
        self.assertIn('MakeLink APP_NAME & DESKTOP & ".lnk", EXE_NAME, True', text)
        # scripts の1つ上(ツールのフォルダ)を、押したときの場所から求める
        self.assertIn("fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))", text)
        # 結果は WScript.Echo(cscript では文字で出るので止まらない)
        self.assertNotIn("msgbox", text.lower())

    def test_配布フォルダに入る(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, _lines = make_dist.build(Path(tmp) / "dist", with_settings=False)
            self.assertEqual((out / "scripts" / "make_shortcuts.vbs").read_bytes(),
                             self.PATH.read_bytes())

    @unittest.skipUnless(sys.platform == "win32", "Windows の cscript で本当に作る")
    def test_Windowsでは本当に2つのショートカットができる(self) -> None:
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "資材複合ツール_VER9"
            (tool / "scripts").mkdir(parents=True)
            shutil.copy2(self.PATH, tool / "scripts" / "make_shortcuts.vbs")
            (tool / "Start.vbs").write_bytes(b"' x\r\n")
            (tool / make_dist.EXE_NAME).write_bytes(b"MZ")
            done = subprocess.run(["cscript", "//nologo", str(tool / "scripts" / "make_shortcuts.vbs")],
                                  capture_output=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            for name in ("資材複合ツール(ブラウザ版).lnk", "資材複合ツール(デスクトップ版).lnk"):
                self.assertTrue((tool / name).exists(), name)
