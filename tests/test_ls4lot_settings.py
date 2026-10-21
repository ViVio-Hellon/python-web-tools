"""LS4LOT の置き場所・ファイル名(VER4.8.78)。

現場の依頼:
    第1パス  SIKALOT・SIKAHIKI・SIKAODR 用のパス / LS4LOT 用のパス(分ける。LS4LOT は名前も変えられる)
    第2パス  SIKALOT・SIKAHIKI・SIKAODR・LS4LOT 用のパス(全部名前を変えられる)
    読むときは手元へ写してから読む

このツールはまだ LS4LOT の中身を使わない。置き場所と名前を持ち、設定画面の「いまの状態」で
写して読めるかを確かめるだけ。仕掛台帳の取り込み(3ファイル)には混ぜない。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from packaging_tool import config, source_db, sync_sources, user_settings  # noqa: E402
from packaging_tool.presenters import settings as presenter  # noqa: E402

KEYS = (config.KEY_LS4_DB_DIR, config.KEY_LS4_DB_NAME, config.KEY_LOT_DB2_FILES,
        config.KEY_LOT_DB_DIR2)


def make_ls4(path: Path) -> Path:
    """LS4LOT の形(表「仕掛」)のファイル。"""
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE "仕掛" ("ﾛｯﾄ番号" TEXT, "製造板厚" REAL, "製造板幅" REAL, "製造板丈" REAL)')
    conn.execute('INSERT INTO "仕掛" VALUES (?, ?, ?, ?)', ("A000001", 6.1, 1220, 2630))
    conn.commit()
    conn.close()
    return path


class Ls4Base(unittest.TestCase):
    def setUp(self) -> None:
        for key in KEYS:
            saved = user_settings.get(key)
            self.addCleanup(user_settings.save, key,
                            saved if saved is not None else ({} if key == config.KEY_LOT_DB2_FILES else ""))
            user_settings.save(key, {} if key == config.KEY_LOT_DB2_FILES else "")
        self.dir = Path(tempfile.mkdtemp(prefix="ls4_"))

    def save(self, **kwargs):
        with mock.patch.object(presenter.admin_password, "verify", return_value=True):
            return presenter.save(password="x", **kwargs)

    def checks(self) -> dict[str, presenter.Check]:
        section = presenter._lot_section({})
        return {c.label: c for c in section.checks if c.label.startswith("LS4LOT")}


class ConfigTests(Ls4Base):
    def test_設定していなければ見ない_名前は既定(self) -> None:
        self.assertIsNone(config.ls4_db_dir())
        self.assertEqual(config.ls4_db_name(), "LS4LOT.sqlite3")
        self.assertEqual(config.ls4_db2_name(), "LS4LOT.sqlite3")

    def test_仕掛台帳の3ファイルには混ぜない(self) -> None:
        user_settings.save(config.KEY_LOT_DB2_FILES, {config.LS4_KEY: "LS4_2.sqlite3"})
        self.assertEqual(set(config.lot_db2_files()), set(config.LOT_DB_FILES))
        self.assertEqual(config.ls4_db2_name(), "LS4_2.sqlite3")
        make_ls4(self.dir / "LS4LOT.sqlite3")
        self.assertEqual(sync_sources.find_lot_dbs(self.dir), {})


class SaveTests(Ls4Base):
    def test_1つ目は仕掛台帳と別の置き場所_名前も変えられる(self) -> None:
        result = self.save(ls4_dir=str(self.dir), ls4_name=" LS4_本番.sqlite3 ")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(config.ls4_db_dir(), self.dir)
        self.assertEqual(config.ls4_db_name(), "LS4_本番.sqlite3")
        self.assertNotEqual(config.lot_db_dir(), self.dir)       # 仕掛台帳の置き場所は動かない

    def test_2つ目は4つとも名前を変えられる(self) -> None:
        names = {"仕掛ロット": "A.sqlite3", "仕掛引当": "B.sqlite3", "仕掛受注": "C.sqlite3",
                 config.LS4_KEY: "D.sqlite3"}
        result = self.save(lot_files2=names)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(config.lot_db2_files(),
                         {"仕掛ロット": "A.sqlite3", "仕掛引当": "B.sqlite3", "仕掛受注": "C.sqlite3"})
        self.assertEqual(config.ls4_db2_name(), "D.sqlite3")

    def test_LS4LOTの名前を送らない保存では消さない(self) -> None:
        """前の画面(LS4LOT の欄が無い)から保存しても、LS4LOT の2つ目の名前は残る。"""
        self.assertTrue(self.save(lot_files2={config.LS4_KEY: "D.sqlite3"}).ok)
        self.assertTrue(self.save(lot_files2={"仕掛ロット": "A.sqlite3"}).ok)
        self.assertEqual(config.ls4_db2_name(), "D.sqlite3")
        self.assertEqual(config.lot_db2_files()["仕掛ロット"], "A.sqlite3")

    def test_変えるときはパスワードが要る_同じなら聞かない(self) -> None:
        refused = presenter.save(ls4_dir=str(self.dir))
        self.assertFalse(refused.ok)
        self.assertEqual(refused.reason, presenter.REFUSE_NEED_PASSWORD)
        self.assertIn("LS4LOT の置き場所", refused.message)
        refused = presenter.save(ls4_name="X.sqlite3")
        self.assertIn("LS4LOT のファイル名", refused.message)
        refused = presenter.save(lot_files2={config.LS4_KEY: "X.sqlite3"})
        self.assertIn("仕掛台帳の2つ目のファイル名", refused.message)
        self.assertIsNone(config.ls4_db_dir())
        same = presenter.save(ls4_dir="", ls4_name="", lot_files2={config.LS4_KEY: ""})
        self.assertTrue(same.ok, same.message)

    def test_名前にフォルダを打ったら断る(self) -> None:
        result = presenter.save(ls4_name=r"\\srv\台帳\LS4LOT.sqlite3")
        self.assertEqual(result.reason, presenter.REFUSE_BAD_INPUT)
        result = presenter.save(lot_files2={config.LS4_KEY: "a/b.sqlite3"})
        self.assertEqual(result.reason, presenter.REFUSE_BAD_INPUT)

    def test_配布設定に入れられる(self) -> None:
        from packaging_tool import distribution
        self.assertIn(config.KEY_LS4_DB_DIR, distribution.ITEM_KEYS)
        self.assertIn(config.KEY_LS4_DB_NAME, distribution.ITEM_KEYS)

    def test_画面に欄がある(self) -> None:
        html = (_ROOT / "app/templates/settings.html").read_text("utf-8")
        js = (_ROOT / "app/static/js/views/settings.js").read_text("utf-8")
        self.assertIn('id="ls4Dir"', html)
        self.assertIn('id="ls4Name"', html)
        self.assertIn("ls4_dir: el.ls4Dir.value", js)
        self.assertIn("ls4_name: el.ls4Name.value", js)
        rows = presenter._first_names()
        self.assertEqual(rows[config.LS4_KEY], "LS4LOT.sqlite3")


class StatusTests(Ls4Base):
    def test_設定していなければそう言う(self) -> None:
        checks = self.checks()
        self.assertEqual(checks["LS4LOT(1つ目)"].value, "置き場所を設定していません")
        self.assertNotIn("LS4LOT(2つ目)", checks)

    def test_1つ目_2つ目とも手元へ写して読む(self) -> None:
        first = self.dir / "1つ目"
        second = self.dir / "2つ目"
        first.mkdir()
        second.mkdir()
        make_ls4(first / "LS4_本番.sqlite3")
        make_ls4(second / "LS4_予備.sqlite3")
        user_settings.save(config.KEY_LS4_DB_DIR, str(first))
        user_settings.save(config.KEY_LS4_DB_NAME, "LS4_本番.sqlite3")
        user_settings.save(config.KEY_LOT_DB_DIR2, str(second))
        user_settings.save(config.KEY_LOT_DB2_FILES, {config.LS4_KEY: "LS4_予備.sqlite3"})
        checks = self.checks()
        for label, path in (("LS4LOT(1つ目)", first / "LS4_本番.sqlite3"),
                            ("LS4LOT(2つ目)", second / "LS4_予備.sqlite3")):
            self.assertEqual(checks[label].level, presenter.OK, checks[label].detail)
            self.assertEqual(checks[label].value, str(path))
            self.assertIn(source_db.WAY_COPY, checks[label].detail)

    def test_無い_表が違うなら要確認(self) -> None:
        user_settings.save(config.KEY_LS4_DB_DIR, str(self.dir))
        self.assertEqual(self.checks()["LS4LOT(1つ目)"].value, "見つかりません")
        conn = sqlite3.connect(self.dir / "LS4LOT.sqlite3")
        conn.execute('CREATE TABLE "別" (a TEXT)')
        conn.commit()
        conn.close()
        check = self.checks()["LS4LOT(1つ目)"]
        self.assertEqual(check.level, presenter.WARN)
        self.assertIn("仕掛", check.value)


class CopyOnlyTests(Ls4Base):
    """仕掛台帳は**写せないなら読まない**(VER4.8.79。現場の決め)。マスタは前のまま。"""

    def setUp(self) -> None:
        super().setUp()
        saved = user_settings.get(config.KEY_LOT_DB_DIR)
        self.addCleanup(user_settings.save, config.KEY_LOT_DB_DIR, saved or "")
        self.first = self.dir / "第1"
        self.second = self.dir / "第2"
        self.ls4 = self.dir / "LS4"
        for d in (self.first, self.second, self.ls4):
            d.mkdir()
        user_settings.save(config.KEY_LOT_DB_DIR, str(self.first))
        user_settings.save(config.KEY_LOT_DB_DIR2, str(self.second))
        user_settings.save(config.KEY_LS4_DB_DIR, str(self.ls4))
        user_settings.save(config.KEY_LS4_DB_NAME, "LS4_本番.sqlite3")
        user_settings.save(config.KEY_LOT_DB2_FILES, {"仕掛ロット": "SIKALOT_圧縮.sqlite3",
                                                      config.LS4_KEY: "LS4_予備.sqlite3"})

    def broken_copy(self):
        uri = mock.patch.object(source_db, "_try_uri", side_effect=AssertionError("共有を直接開いた"))
        plain = mock.patch.object(source_db, "_try_plain", side_effect=AssertionError("共有を直接開いた"))
        copy = mock.patch.object(source_db, "_try_copy", side_effect=OSError("写せません"))
        return copy, uri, plain

    def test_第1_第2パスの4つとも写せなければ読まない(self) -> None:
        files = [make_ls4(self.first / "SIKALOT.sqlite3"),
                 make_ls4(self.first / "SIKAHIKI.db"),          # 拡張子違いも同じ
                 make_ls4(self.second / "SIKALOT_圧縮.sqlite3"),
                 make_ls4(self.second / "SIKAODR.sqlite3"),
                 make_ls4(self.ls4 / "LS4_本番.sqlite3"),
                 make_ls4(self.second / "LS4_予備.sqlite3")]
        copy, uri, plain = self.broken_copy()
        for path in files:
            with self.subTest(path=path.name), copy, uri, plain:
                self.assertTrue(source_db.is_lot_ledger(Path(path)))
                found = source_db.probe(path)
                self.assertFalse(found.ok)
                self.assertEqual([n for n, _ in found.attempts], [source_db.WAY_COPY])
                self.assertIn("手元へ写せなかったので読みません", found.error)
                with self.assertRaises(source_db.SourceError):
                    source_db.read_table(path, "仕掛")

    def test_写せればふだんどおり写しを読む(self) -> None:
        path = make_ls4(self.first / "SIKALOT.sqlite3")
        rows = source_db.read_table(path, "仕掛")
        self.assertEqual(rows[0]["ﾛｯﾄ番号"], "A000001")
        self.assertEqual(source_db.probe(path).opened_by, source_db.WAY_COPY)

    def test_同じフォルダでも仕掛台帳でないファイルは前のまま(self) -> None:
        """梱包資材マスタを仕掛台帳と同じフォルダに置いている端末でも、マスタは巻き込まない。"""
        other = make_ls4(self.first / "梱包資材マスタ.sqlite3")
        elsewhere = self.dir / "よそ"
        elsewhere.mkdir()
        same_name = make_ls4(elsewhere / "SIKALOT.sqlite3")      # 仕掛台帳の置き場所でない
        for path in (other, same_name):
            with self.subTest(path=str(path)), mock.patch.object(
                    source_db, "_try_copy", side_effect=OSError("写せません")):
                self.assertFalse(source_db.is_lot_ledger(Path(path)))
                found = source_db.probe(path)
                self.assertTrue(found.ok)
                self.assertEqual(found.opened_by, source_db.WAY_URI)


if __name__ == "__main__":
    unittest.main()
