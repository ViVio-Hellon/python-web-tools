"""仕掛台帳の2つ目の置き場所(`config.lot_db_dir2`)。

現場の声:「仕掛DBの参照パスを2つセットできるように。1つ目で対象が見つけられなかった
(ファイルが無かった)、またはファイルはあるがデータ上で対象が見つけられなかった → 2つ目を参照」。

【ここで守りたいこと】
1. 1つ目に**ファイルが無い**ものは、2つ目から読む(ファイルごと)
2. 1つ目に**ファイルはあるがロット(受注)が無い**ときは、2つ目にあるものを足す
3. 同じロット・受注が両方にあれば**1つ目を正**とする(2つ目で上書きしない)
4. 1つ目のファイルが読めなければ、2つ目を1つ目として読む
5. 2つ目を設定していなければ、今までどおり(何も足さない)
6. ロット検索で、2つ目にしかないロットが見つかる
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import (config, data_sync, db, import_specs,  # noqa: E402
                            lot_service, source_db, sync_sources, user_settings)


def write_ledger(folder: Path, filename: str, table: str, rows: list[dict]) -> Path:
    """仕掛台帳の1ファイル。列は取り込みが読む列ぜんぶ(取り込み元の名前)。"""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    spec = import_specs.LOT_IMPORT_SPECS[table]
    cols = [src for _local, src, _conv in spec]
    conn = sqlite3.connect(path)
    conn.execute(f'CREATE TABLE "{import_specs.LOT_SOURCE_TABLE}" ('
                 + ", ".join(f'"{c}" TEXT' for c in cols) + ")")
    for row in rows:
        conn.execute(f'INSERT INTO "{import_specs.LOT_SOURCE_TABLE}" VALUES ('
                     + ",".join("?" * len(cols)) + ")", [row.get(c, "") for c in cols])
    conn.commit()
    conn.close()
    return path


def src(table: str, local: str) -> str:
    """手元の列名 → 取り込み元の列名。"""
    for col, source, _conv in import_specs.LOT_IMPORT_SPECS[table]:
        if col == local:
            return source
    raise KeyError(local)


def lot(no: str, use: str = "") -> dict:
    return {src("仕掛ロット", "ロット番号"): no, src("仕掛ロット", "用途名"): use,
            src("仕掛ロット", "製造板幅"): "1000", src("仕掛ロット", "製造板丈"): "2000"}


def hiki(no: str, order: str) -> dict:
    return {src("仕掛引当", "ロット番号"): no, src("仕掛引当", "受注番号"): order,
            src("仕掛引当", "引当数量"): "1"}


def order(no: str, name: str) -> dict:
    return {src("仕掛受注", "受注番号"): no, src("仕掛受注", "納入先名称"): name}


class SecondLotDirTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="lot2_"))
        self.first = base / "1つ目"
        self.second = base / "2つ目"
        self.first.mkdir()
        self.second.mkdir()
        for key, value in ((config.KEY_LOT_DB_DIR, self.first),
                           (config.KEY_LOT_DB_DIR2, self.second),
                           (config.KEY_MASTER_DB_DIR, base / "マスタ")):
            saved = user_settings.get(key)
            user_settings.save(key, str(value))
            self.addCleanup(user_settings.save, key, saved or "")
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.addCleanup(source_db.sweep_old_copies)

    def local(self, sql: str) -> list:
        return [tuple(r) for r in self.conn.execute(sql)]

    def test_1つ目に無いファイルは2つ目から読む(self) -> None:
        write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [lot("A000001")])
        write_ledger(self.second, "SIKAHIKI.sqlite3", "仕掛引当", [hiki("A000001", "J1")])
        found = sync_sources.find_lot_dbs()
        self.assertEqual(found["仕掛ロット"].parent, self.first)
        self.assertEqual(found["仕掛引当"].parent, self.second)
        data_sync.import_lot_ledger(self.conn)
        self.assertEqual(self.local("SELECT ロット番号, 受注番号 FROM 仕掛引当"), [("A000001", "J1")])

    def test_1つ目に無いロットは2つ目から足す(self) -> None:
        write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [lot("A000001", "1つ目")])
        write_ledger(self.second, "SIKALOT.sqlite3", "仕掛ロット",
                     [lot("A000001", "2つ目"), lot("B000002", "2つ目だけ")])
        result = data_sync.import_lot_ledger(self.conn)
        rows = dict(self.local("SELECT ロット番号, 用途名 FROM 仕掛ロット"))
        # 両方にあるロットは1つ目を正とする。2つ目にしかないロットは足す
        self.assertEqual(rows, {"A000001": "1つ目", "B000002": "2つ目だけ"})
        self.assertTrue(any("2つ目の置き場所から足しました" in n for n in result.notes),
                        result.notes)

    def test_引当と受注も1つ目に無いものを足す(self) -> None:
        write_ledger(self.first, "SIKAHIKI.sqlite3", "仕掛引当", [hiki("A000001", "J1")])
        write_ledger(self.second, "SIKAHIKI.sqlite3", "仕掛引当",
                     [hiki("A000001", "J9"), hiki("B000002", "J2"), hiki("B000002", "J3")])
        write_ledger(self.first, "SIKAODR.sqlite3", "仕掛受注", [order("J1", "1つ目")])
        write_ledger(self.second, "SIKAODR.sqlite3", "仕掛受注",
                     [order("J1", "2つ目"), order("J2", "2つ目だけ")])
        data_sync.import_lot_ledger(self.conn)
        self.assertEqual(sorted(self.local("SELECT ロット番号, 受注番号 FROM 仕掛引当")),
                         [("A000001", "J1"), ("B000002", "J2"), ("B000002", "J3")])
        self.assertEqual(dict(self.local("SELECT 受注番号, 納入先名称 FROM 仕掛受注")),
                         {"J1": "1つ目", "J2": "2つ目だけ"})

    def test_1つ目が読めなければ2つ目を読む(self) -> None:
        (self.first / "SIKALOT.sqlite3").write_bytes(b"not sqlite" * 100)
        write_ledger(self.second, "SIKALOT.sqlite3", "仕掛ロット", [lot("B000002")])
        result = data_sync.import_lot_ledger(self.conn)
        self.assertEqual(self.local("SELECT ロット番号 FROM 仕掛ロット"), [("B000002",)])
        self.assertFalse([e for e in result.errors if e.startswith("仕掛ロット:")], result.errors)
        self.assertTrue(any("2つ目" in w for w in result.warnings), result.warnings)

    def test_ファイルごと_ロットごとに2つ目を見る(self) -> None:
        """現場の質問の例そのまま。**1つ目を丸ごと捨てて2つ目へ移るのではない。**

            1つ目: SIKALOT が無い          → SIKALOT だけ 2つ目から
            1つ目: SIKAHIKI がある         → 1つ目を使い続ける。
                   その中にロットが無い     → そのロットの引当だけ 2つ目の SIKAHIKI から
            1つ目: SIKAODR が無い          → SIKAODR だけ 2つ目から
        """
        write_ledger(self.first, "SIKAHIKI.sqlite3", "仕掛引当", [hiki("X000001", "J1")])
        write_ledger(self.second, "SIKALOT.sqlite3", "仕掛ロット",
                     [lot("X000001", "2つ目"), lot("Y000002", "2つ目")])
        write_ledger(self.second, "SIKAHIKI.sqlite3", "仕掛引当",
                     [hiki("X000001", "J9"), hiki("Y000002", "J2")])
        write_ledger(self.second, "SIKAODR.sqlite3", "仕掛受注",
                     [order("J1", "2つ目"), order("J2", "2つ目")])
        found = sync_sources.find_lot_dbs()
        self.assertEqual({t: p.parent for t, p in found.items()},
                         {"仕掛ロット": self.second, "仕掛引当": self.first, "仕掛受注": self.second})
        data_sync.import_lot_ledger(self.conn)
        # X の引当は1つ目のまま(J1。2つ目の J9 で上書きしない)。Y は1つ目に無いので2つ目から
        self.assertEqual(sorted(self.local("SELECT ロット番号, 受注番号 FROM 仕掛引当")),
                         [("X000001", "J1"), ("Y000002", "J2")])
        self.assertTrue(lot_service.search_lot(self.conn, "Y000002").found)

    def test_1つ目が空や表の無いファイルなら2つ目を読む(self) -> None:
        """「データが無い」も、ファイルが無いときと同じく2つ目にある分が入る。"""
        write_ledger(self.second, "SIKALOT.sqlite3", "仕掛ロット", [lot("Y000002")])
        for case in ("空", "表が無い", "0バイト"):
            with self.subTest(case=case):
                path = self.first / "SIKALOT.sqlite3"
                path.unlink(missing_ok=True)
                if case == "空":
                    write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [])
                elif case == "表が無い":
                    conn = sqlite3.connect(path)
                    conn.execute("CREATE TABLE other(x)")
                    conn.commit()
                    conn.close()
                else:
                    path.write_bytes(b"")
                data_sync.import_lot_ledger(self.conn)
                self.assertEqual(self.local("SELECT ロット番号 FROM 仕掛ロット"), [("Y000002",)])
                source_db.sweep_old_copies()

    def test_1つ目でBOX最終実績が空なら2つ目の値で埋める(self) -> None:
        """現場の質問:「SIKALOT で BOX最終実績_板厚・板幅・板丈 のデータが無かった場合、
        パス2で見に行くってことですよね?」── ロットはあっても値が空なら、2つ目を見る。"""
        def boxed(no: str, t: str, w: str, l: str, n: str = "", place: str = "") -> dict:
            row = lot(no, "1つ目")
            row.update({src("仕掛ロット", "BOX最終実績_板厚"): t, src("仕掛ロット", "BOX最終実績_板幅"): w,
                        src("仕掛ロット", "BOX最終実績_板丈"): l, src("仕掛ロット", "BOX最終実績_枚本数"): n,
                        src("仕掛ロット", "BOX最終実績_設備名"): place})
            return row
        write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [
            boxed("A000001", "", "", ""),                 # 空 → 2つ目で埋める
            boxed("A000001", "", "", ""),                 # 同じロットの別の工程の行も
            boxed("B000002", "6", "1250", ""),            # 丈だけ空 → まとまりごと2つ目に
            boxed("C000003", "8", "1500", "3000", "40", "KEN1"),   # そろっている → 1つ目のまま
            boxed("D000004", "", "", ""),                 # 2つ目にも無い → 空のまま
        ])
        write_ledger(self.second, "SIKALOT.sqlite3", "仕掛ロット", [
            boxed("A000001", "5", "1200", "2500", "30", "KEN2"),
            boxed("B000002", "7", "1300", "2600", "31", "KEN3"),
            boxed("C000003", "9", "9999", "9999", "99", "別"),
            boxed("D000004", "4", "", ""),                # 2つ目も欠けている → 使わない
        ])
        result = data_sync.import_lot_ledger(self.conn)
        got = self.local("SELECT ロット番号, BOX最終実績_板厚, BOX最終実績_板幅, BOX最終実績_板丈,"
                         " BOX最終実績_枚本数, BOX最終実績_設備名, 用途名 FROM 仕掛ロット ORDER BY 管理番号")
        self.assertEqual(got, [
            ("A000001", 5.0, 1200.0, 2500.0, 30, "KEN2", "1つ目"),
            ("A000001", 5.0, 1200.0, 2500.0, 30, "KEN2", "1つ目"),
            ("B000002", 7.0, 1300.0, 2600.0, 31, "KEN3", "1つ目"),
            ("C000003", 8.0, 1500.0, 3000.0, 40, "KEN1", "1つ目"),
            ("D000004", 0.0, 0.0, 0.0, 0, "", "1つ目"),
        ])
        self.assertTrue(any("BOX最終実績(板厚・板幅・板丈)が空のロット 2件" in n for n in result.notes),
                        result.notes)

    def test_2つ目を設定していなければ足さない(self) -> None:
        user_settings.save(config.KEY_LOT_DB_DIR2, "")
        write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [lot("A000001")])
        write_ledger(self.second, "SIKALOT.sqlite3", "仕掛ロット", [lot("B000002")])
        self.assertIsNone(config.lot_db_dir2())
        data_sync.import_lot_ledger(self.conn)
        self.assertEqual(self.local("SELECT ロット番号 FROM 仕掛ロット"), [("A000001",)])

    def test_ロット検索で2つ目にしかないロットが見つかる(self) -> None:
        write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [lot("A000001")])
        write_ledger(self.second, "SIKALOT.sqlite3", "仕掛ロット", [lot("B000002")])
        data_sync.import_lot_ledger(self.conn)
        self.assertTrue(lot_service.search_lot(self.conn, "B000002").found)

    def test_起動時の取り込みは2つ目が変わったときも読み直す(self) -> None:
        first = write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [lot("A000001")])
        second = write_ledger(self.second, "SIKALOT.sqlite3", "仕掛ロット", [lot("B000002")])
        none = mock.patch.multiple(sync_sources, find_material_db=mock.DEFAULT,
                                   find_kanban_db=mock.DEFAULT, find_threshold_db=mock.DEFAULT)
        with none as found:
            for m in found.values():
                m.return_value = None
            data_sync.auto_import(self.conn)
            self.assertEqual(len(self.local("SELECT * FROM 仕掛ロット")), 2)
            # 2つ目だけ増えた(1つ目は変わっていない)
            conn = sqlite3.connect(second)
            conn.execute(f'INSERT INTO "{import_specs.LOT_SOURCE_TABLE}" ("{src("仕掛ロット", "ロット番号")}")'
                         " VALUES ('C000003')")
            conn.commit()
            conn.close()
            # 更新時刻は1秒より細かい差を見ない(`import_reason`)。後から書かれた形にする
            import os
            stat = second.stat()
            os.utime(second, (stat.st_atime, stat.st_mtime + 60))
            data_sync.auto_import(self.conn)
        self.assertIn(("C000003",), self.local("SELECT ロット番号 FROM 仕掛ロット"))
        self.assertTrue(first.exists())


@unittest.skipUnless(__import__("importlib").util.find_spec("flask"), "Flask が無い")
class SettingsTests(unittest.TestCase):
    """2つ目の置き場所も、変えるときは管理者パスワードが要る(1つ目と同じ)。"""

    def setUp(self) -> None:
        from packaging_tool import admin_password
        saved = user_settings.get(config.KEY_LOT_DB_DIR2)
        self.addCleanup(user_settings.save, config.KEY_LOT_DB_DIR2, saved or "")
        user_settings.save(config.KEY_LOT_DB_DIR2, "")
        self.password = admin_password.DEFAULT if hasattr(admin_password, "DEFAULT") else None

    def test_パスワード無しでは変えられず空なら使わない(self) -> None:
        from packaging_tool.presenters import settings as presenter
        place = str(Path(tempfile.mkdtemp(prefix="lot2_set_")) / "予備の台帳")
        refused = presenter.save(lot_dir2=place)
        self.assertFalse(refused.ok)
        self.assertIn("仕掛台帳の2つ目の置き場所", refused.message)
        self.assertIsNone(config.lot_db_dir2())
        with mock.patch.object(presenter.admin_password, "verify", return_value=True):
            self.assertTrue(presenter.save(lot_dir2=place, password="x").ok)
        self.assertEqual(config.lot_db_dir2(), Path(place))
        with mock.patch.object(presenter.admin_password, "verify", return_value=True):
            self.assertTrue(presenter.save(lot_dir2="", password="x").ok)
        self.assertIsNone(config.lot_db_dir2())

    def test_画面に2つ目の欄がある(self) -> None:
        from packaging_tool.presenters import settings as presenter
        html = (_ROOT / "app/templates/settings.html").read_text("utf-8")
        self.assertIn('id="lotDir2"', html)
        self.assertIn("lot_dir2: el.lotDir2.value",
                      (_ROOT / "app/static/js/views/settings.js").read_text("utf-8"))
        self.assertIn("lot_dir2", presenter.PROTECTED_LABELS)


if __name__ == "__main__":
    unittest.main()
