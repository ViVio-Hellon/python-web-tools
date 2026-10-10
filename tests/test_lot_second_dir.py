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
        """「データが無い」も2つ目にある分が入る。

        表が無い・0バイトはファイルが無いときと同じ(2つ目を1つ目として読む)。**表はあるが0行**
        のときは手元の前の中身を消さず(0行の表は手元を消さない)、手元に無いロットだけを2つ目から
        足す。この試験は手元が空から始めるので、どちらでも Y000002 だけになる。"""
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

    # --- BOX最終実績寸法の候補(1つ目にロットはあるが寸法が空) ------------------
    def write_raw(self, folder: Path, columns: list[str], rows: list[dict]) -> Path:
        """列を自由に決めた SIKALOT(**1つ目と2つ目で列の中身が違う**ことがある)。"""
        path = folder / "SIKALOT.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute(f'CREATE TABLE "{import_specs.LOT_SOURCE_TABLE}" ('
                     + ", ".join(f'"{c}" TEXT' for c in columns) + ")")
        for row in rows:
            conn.execute(f'INSERT INTO "{import_specs.LOT_SOURCE_TABLE}" VALUES ('
                         + ",".join("?" * len(columns)) + ")", [row.get(c, "") for c in columns])
        conn.commit()
        conn.close()
        return path

    def box_ledgers(self) -> None:
        """1つ目: BOXコースで BOX最終実績が空のロットH・そろっているロットC・BOXでないロットN。
        2つ目: 列が違う(BOX設計_設備名・BOX番号・BOX設計_* がある。本物の圧縮版は BOX実績_板厚・
        板幅・板丈 と 設計_設備ｺｰｽ が無いが、ここでは 設計_設備ｺｰｽ を空で持たせている)。"""
        def first(no: str, course: str, t: str = "", w: str = "", l: str = "") -> dict:
            row = lot(no, "1つ目")
            row.update({src("仕掛ロット", "設計_設備コース"): course,
                        src("仕掛ロット", "BOX最終実績_板厚"): t, src("仕掛ロット", "BOX最終実績_板幅"): w,
                        src("仕掛ロット", "BOX最終実績_板丈"): l})
            return row
        write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [
            first("H9022S0", "HOT PSW GFS KEN"),
            first("C000003", "HOT GFS KEN", "8", "1500", "3000"),
            first("N000004", "HOT L-1 KEN"),
        ])
        # 参照パス2専用の SIKALOT の形(44列。BOX実績_板厚・板幅・板丈 は無い)。読むのは BOX設計_*
        cols = ["ﾛｯﾄ番号", "BOX番号", "BOX設計_設備名", "BOX設計_板厚", "BOX設計_板幅", "BOX設計_板丈",
                "BOX最終実績_板厚", "BOX最終実績_板幅", "BOX最終実績_板丈", "設計_設備ｺｰｽ"]
        self.write_raw(self.second, cols, [
            {"ﾛｯﾄ番号": "H9022S0", "BOX番号": "4", "BOX設計_設備名": "KEN",
             "BOX設計_板厚": "100", "BOX設計_板幅": "544", "BOX設計_板丈": "544",
             # BOX最終実績は**読まない**(現場の指定)
             "BOX最終実績_板厚": "9", "BOX最終実績_板幅": "9", "BOX最終実績_板丈": "9"},
            {"ﾛｯﾄ番号": "H9022S0", "BOX番号": "2", "BOX設計_設備名": "PSW",
             "BOX設計_板厚": "100", "BOX設計_板幅": "1200", "BOX設計_板丈": "2850"},
            {"ﾛｯﾄ番号": "H9022S0", "BOX番号": "5", "BOX設計_設備名": "GFS"},            # 寸法が無い → 出さない
            # 板丈が 0 の行(実データの N7154X0 = 4.52 × 127.8 × 0)→ 0 のまま候補にする
            {"ﾛｯﾄ番号": "H9022S0", "BOX番号": "6", "BOX設計_設備名": "TLV",
             "BOX設計_板厚": "4.52", "BOX設計_板幅": "127.8", "BOX設計_板丈": "0"},
            {"ﾛｯﾄ番号": "C000003", "BOX番号": "2", "BOX設計_設備名": "GFS",
             "BOX設計_板厚": "9", "BOX設計_板幅": "9999", "BOX設計_板丈": "9999"},
            {"ﾛｯﾄ番号": "N000004", "BOX番号": "1", "BOX設計_設備名": "L-1",
             "BOX設計_板厚": "1", "BOX設計_板幅": "100", "BOX設計_板丈": "200"},
        ])

    def test_BOX最終実績が空なら2つ目の行を候補に控える_勝手に埋めない(self) -> None:
        self.box_ledgers()
        result = data_sync.import_lot_ledger(self.conn)
        self.assertEqual(self.local("SELECT ロット番号, BOX番号, BOX設計_設備名, 板厚, 板幅, 板丈, 出どころ"
                                    " FROM 仕掛ロット_2つ目 ORDER BY 管理番号"), [
            ("H9022S0", "4", "KEN", 100.0, 544.0, 544.0, "BOX設計"),
            ("H9022S0", "2", "PSW", 100.0, 1200.0, 2850.0, "BOX設計"),
            ("H9022S0", "6", "TLV", 4.52, 127.8, 0.0, "BOX設計"),
            # N000004 は BOX でないが BOX最終実績が空なので控える(画面で出すかはロット情報が決める)
            ("N000004", "1", "L-1", 1.0, 100.0, 200.0, "BOX設計"),
        ])
        # 1つ目の値は**書き換えない**(どれを使うかは画面で選ぶ)
        self.assertEqual(self.local("SELECT BOX最終実績_板厚 FROM 仕掛ロット WHERE ロット番号 = 'H9022S0'"),
                         [(0.0,)])
        self.assertTrue(any("候補として控えました" in n for n in result.notes), result.notes)
        # 取り込み直しても重ならない(作り直す)
        data_sync.import_lot_ledger(self.conn)
        self.assertEqual(self.local("SELECT COUNT(*) FROM 仕掛ロット_2つ目"), [(4,)])

    def test_選んだ候補の寸法を使う(self) -> None:
        self.box_ledgers()
        data_sync.import_lot_ledger(self.conn)
        plain = lot_service.search_lot(self.conn, "H9022S0").lot
        # 工程の順(BOX番号の小さい順)に並ぶ
        self.assertEqual([c.equipment for c in plain.box_choices], ["PSW", "KEN", "TLV"])
        self.assertEqual((plain.width, plain.length, plain.box_pick), (0.0, 0.0, ""))
        key = plain.box_choices[1].key
        picked = lot_service.search_lot(self.conn, "H9022S0", key).lot
        self.assertEqual((picked.thickness, picked.width, picked.length), (100.0, 544.0, 544.0))
        self.assertEqual(picked.box_pick, key)
        # 候補に無い鍵は無視する(1つ目のまま)
        self.assertEqual(lot_service.search_lot(self.conn, "H9022S0", "x|y").lot.width, 0.0)

    def test_候補はBOX実績寸法で最終実績が空のときだけ(self) -> None:
        self.box_ledgers()
        data_sync.import_lot_ledger(self.conn)
        self.assertEqual(lot_service.search_lot(self.conn, "C000003").lot.box_choices, [])   # そろっている
        self.assertEqual(lot_service.search_lot(self.conn, "N000004").lot.box_choices, [])   # BOXでない

    # --- BOX最終実績_設備名 が HOT(寸法が入っていても使わない) ---------------------
    def hot_ledgers(self) -> None:
        """1つ目: BOX最終実績がそろっているが設備名が HOT のロットT(BOXコース)、
        同じく HOT だが BOXでないロットU、そろっていて設備名が KEN のロットK。"""
        def first(no: str, course: str, equipment: str) -> dict:
            row = lot(no, "1つ目")
            row.update({src("仕掛ロット", "設計_設備コース"): course,
                        src("仕掛ロット", "BOX最終実績_設備名"): equipment,
                        src("仕掛ロット", "BOX最終実績_板厚"): "8",
                        src("仕掛ロット", "BOX最終実績_板幅"): "1500",
                        src("仕掛ロット", "BOX最終実績_板丈"): "3000"})
            return row
        write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [
            first("T000005", "HOT PSW GFS KEN", "HOT"),
            first("V000007", "HOT GFS KEN", " hot "),     # 前後の空白・小文字でも同じ
            first("U000006", "HOT L-1 KEN", "HOT"),
            first("K000008", "HOT GFS KEN", "KEN"),
        ])
        cols = ["ﾛｯﾄ番号", "BOX番号", "BOX設計_設備名", "BOX設計_板厚", "BOX設計_板幅", "BOX設計_板丈"]
        self.write_raw(self.second, cols, [
            {"ﾛｯﾄ番号": no, "BOX番号": "2", "BOX設計_設備名": "PSW",
             "BOX設計_板厚": "100", "BOX設計_板幅": "1200", "BOX設計_板丈": "2850"}
            for no in ("T000005", "V000007", "U000006", "K000008")])

    def test_BOX最終実績がHOTなら寸法があっても2つ目の候補へ(self) -> None:
        """現場:「HOT の BOX最終実績の板厚・板幅・板丈は使用するに値しない」。"""
        from packaging_tool.presenters import lot as lot_presenter

        self.hot_ledgers()
        result = data_sync.import_lot_ledger(self.conn)
        self.assertEqual(self.local("SELECT DISTINCT ロット番号 FROM 仕掛ロット_2つ目 ORDER BY 1"),
                         [("T000005",), ("U000006",), ("V000007",)])
        self.assertTrue(any("空か HOT のロット" in n for n in result.notes), result.notes)

        plain = lot_service.search_lot(self.conn, "T000005").lot
        self.assertEqual([c.equipment for c in plain.box_choices], ["PSW"])
        self.assertEqual(plain.box_final_problem, "HOT")
        # **選ぶまで HOT の値は使わない**(残すと資材展開でそのまま使われる)
        self.assertEqual((plain.thickness, plain.width, plain.length), (0.0, 0.0, 0.0))
        note = lot_presenter._dimension_note(plain)
        self.assertIn("BOX最終実績_設備名が HOT のため、その寸法は使いません", note)
        self.assertIn("横の欄で", note)

        picked = lot_service.search_lot(self.conn, "T000005", plain.box_choices[0].key).lot
        self.assertEqual((picked.thickness, picked.width, picked.length), (100.0, 1200.0, 2850.0))
        self.assertIn("2つ目の PSW のBOX設計寸法を使っています", lot_presenter._dimension_note(picked))

        self.assertEqual(lot_service.search_lot(self.conn, "V000007").lot.box_final_problem, "HOT")

    def test_HOTで選ぶ前の板厚_板幅_板丈は空白_資材展開は押せない(self) -> None:
        """現場の声:「空白で良くない？今から選ぶわけだし0が入ってると混乱されるよ」。"""
        from packaging_tool.presenters import lot as lot_presenter

        self.hot_ledgers()
        data_sync.import_lot_ledger(self.conn)
        result = lot_service.search_lot(self.conn, "T000005")
        view = lot_presenter.build(result)
        dims = {f.key: f.value for f in view.lot_fields
                if f.key in ("thickness", "width", "length")}
        self.assertEqual(dims, {"thickness": "", "width": "", "length": ""})
        self.assertFalse(view.can_expand)
        self.assertIn("使う寸法を選んでください", view.expand_reason)
        # オーダー寸法は空白にしない(使える値)
        order = next(f.value for f in view.lot_fields if f.key == "order_width")
        self.assertNotEqual(order, "")
        # 選んだら、その値を出す
        key = result.lot.box_choices[0].key
        picked = lot_presenter.build(lot_service.search_lot(self.conn, "T000005", key))
        dims = {f.key: f.value for f in picked.lot_fields
                if f.key in ("thickness", "width", "length")}
        self.assertEqual(dims, {"thickness": "100.000", "width": "1200.0", "length": "2850.0"})
        self.assertTrue(picked.can_expand)
        # HOT でないロットは今までどおり
        ken = lot_presenter.build(lot_service.search_lot(self.conn, "K000008"))
        self.assertEqual(next(f.value for f in ken.lot_fields if f.key == "width"), "1500.0")

    def test_HOTでもBOXでなければ製造寸法のまま_HOTでなければ1つ目のまま(self) -> None:
        self.hot_ledgers()
        data_sync.import_lot_ledger(self.conn)
        not_box = lot_service.search_lot(self.conn, "U000006").lot
        self.assertEqual((not_box.box_choices, not_box.box_final_problem), ([], ""))
        self.assertEqual((not_box.width, not_box.length), (1000.0, 2000.0))
        ken = lot_service.search_lot(self.conn, "K000008").lot
        self.assertEqual((ken.box_choices, ken.box_final_problem), ([], ""))
        self.assertEqual((ken.thickness, ken.width, ken.length), (8.0, 1500.0, 3000.0))

    def test_HOTで2つ目が無ければ理由を書く(self) -> None:
        from packaging_tool.presenters import lot as lot_presenter

        self.hot_ledgers()
        user_settings.save(config.KEY_LOT_DB_DIR2, "")
        data_sync.import_lot_ledger(self.conn)
        hot = lot_service.search_lot(self.conn, "T000005").lot
        self.assertEqual((hot.box_choices, hot.width), ([], 0.0))
        self.assertIn("HOT のため、その寸法は使いません(2つ目の仕掛台帳の置き場所を設定すると",
                      lot_presenter._dimension_note(hot))

    # --- 2つ目の圧縮版 SIKALOT(設計_設備ｺｰｽ が無い)から入ったロット ---------------------
    def compressed(self, *lots: str, with_course: bool = False) -> None:
        """2つ目の圧縮版 SIKALOT。本物は 設計_設備ｺｰｽ・実績_設備ｺｰｽ が無い(後日足す予定)。"""
        cols = ["ﾛｯﾄ番号", src("仕掛ロット", "製造板厚"), src("仕掛ロット", "製造板幅"),
                src("仕掛ロット", "製造板丈"), "BOX設計_設備名", "BOX最終実績_板厚",
                "BOX最終実績_板幅", "BOX最終実績_板丈"]
        if with_course:
            cols.append(src("仕掛ロット", "設計_設備コース"))
        self.write_raw(self.second, cols, [
            {"ﾛｯﾄ番号": no, src("仕掛ロット", "製造板厚"): "6.75",
             src("仕掛ロット", "製造板幅"): "216.5", src("仕掛ロット", "製造板丈"): "851.5",
             "BOX設計_設備名": "PSW", "BOX最終実績_板厚": "7.1", "BOX最終実績_板幅": "1305",
             "BOX最終実績_板丈": "2720", src("仕掛ロット", "設計_設備コース"): "HOT PSW GFS KEN"}
            for no in lots])

    def test_圧縮版から足したロットはBOXか分からないと断って製造寸法を出す(self) -> None:
        """現場の判断「つなぎなので案2」。黙って製造寸法を出さない。1つ目のロットには付けない。"""
        from packaging_tool.presenters import lot as lot_presenter
        write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [lot("A000001")])
        self.compressed("R6545E0")
        data_sync.import_lot_ledger(self.conn)
        only2 = lot_service.search_lot(self.conn, "R6545E0").lot
        self.assertTrue(only2.course_unknown)
        self.assertFalse(only2.is_box)
        self.assertEqual((only2.thickness, only2.width, only2.length), (6.75, 216.5, 851.5))
        self.assertIn("BOX かどうか分かりません。製造寸法を表示しています",
                      lot_presenter._dimension_note(only2))
        first = lot_service.search_lot(self.conn, "A000001").lot
        self.assertFalse(first.course_unknown)
        self.assertEqual(lot_presenter._dimension_note(first), "")

    def test_1つ目にSIKALOTが無く圧縮版を読んだら全ロットで断る(self) -> None:
        from packaging_tool.presenters import lot as lot_presenter
        self.compressed("R6545E0", "L816X51")
        data_sync.import_lot_ledger(self.conn)
        self.assertEqual(sorted(self.local("SELECT ロット番号 FROM 仕掛ロット_コース不明")),
                         [("L816X51",), ("R6545E0",)])
        self.assertIn("BOX かどうか分かりません",
                      lot_presenter._dimension_note(lot_service.search_lot(self.conn, "L816X51").lot))

    def test_1つ目が読めず圧縮版を読んでも断る(self) -> None:
        (self.first / "SIKALOT.sqlite3").write_bytes(b"not sqlite" * 100)
        self.compressed("R6545E0")
        data_sync.import_lot_ledger(self.conn)
        self.assertTrue(lot_service.search_lot(self.conn, "R6545E0").lot.course_unknown)

    def test_圧縮版に設計_設備ｺｰｽが入ればBOXとして扱い断らない(self) -> None:
        """後日、圧縮版に 設計_設備ｺｰｽ が入ったら、ツールを変えずに BOX の扱いになる。"""
        self.compressed("R6545E0", with_course=True)
        data_sync.import_lot_ledger(self.conn)
        info = lot_service.search_lot(self.conn, "R6545E0").lot
        self.assertFalse(info.course_unknown)
        self.assertTrue(info.is_box)
        self.assertEqual((info.width, info.length), (1305.0, 2720.0))
        self.assertEqual(self.local("SELECT COUNT(*) FROM 仕掛ロット_コース不明"), [(0,)])

    def test_取り込み直すと前の控えを残さない(self) -> None:
        self.compressed("R6545E0")
        data_sync.import_lot_ledger(self.conn)
        write_ledger(self.first, "SIKALOT.sqlite3", "仕掛ロット", [lot("R6545E0")])
        data_sync.import_lot_ledger(self.conn)
        self.assertFalse(lot_service.search_lot(self.conn, "R6545E0").lot.course_unknown)

    def test_2つ目が無ければ候補も無い(self) -> None:
        self.box_ledgers()
        user_settings.save(config.KEY_LOT_DB_DIR2, "")
        data_sync.import_lot_ledger(self.conn)
        self.assertEqual(self.local("SELECT COUNT(*) FROM 仕掛ロット_2つ目"), [(0,)])
        self.assertEqual(lot_service.search_lot(self.conn, "H9022S0").lot.box_choices, [])

    def test_見つからないときは各フォルダで何が見えたかを言う(self) -> None:
        """現場の声:「この間違ったパス先にも SIKALOT/SIKAHIKI/SIKAODR は置いてある」。
        「見つかりません」だけでは、届いていないのか名前が違うのか分からない。"""
        (self.first / "仕掛台帳_古い.sqlite3").write_bytes(b"")
        user_settings.save(config.KEY_LOT_DB_DIR2, str(self.second / "無いフォルダ"))
        result = data_sync.import_lot_ledger(self.conn)
        [message] = [e for e in result.errors if "仕掛台帳" in e]
        self.assertIn("SIKALOT.sqlite3・SIKAHIKI.sqlite3・SIKAODR.sqlite3", message)
        self.assertIn(f"{self.first}: 開けましたが該当するファイルがありません"
                      "(ある取り込み元: 仕掛台帳_古い.sqlite3)", message)
        self.assertIn(f"{self.second / '無いフォルダ'}: フォルダがありません", message)

    def test_設定していない場所は探さない(self) -> None:
        """現場の声:「仕掛台帳用の2つ目は設定なしなら探さないでしょ?」。以前はマスタの
        フォルダも予備で見ていた。2つ目を空にしたら、1つ目だけを見る。"""
        user_settings.save(config.KEY_LOT_DB_DIR2, "")
        master = Path(user_settings.get(config.KEY_MASTER_DB_DIR))
        write_ledger(master, "SIKALOT.sqlite3", "仕掛ロット", [lot("M000001")])
        self.assertEqual(sync_sources.lot_search_dirs(), [self.first])
        result = data_sync.import_lot_ledger(self.conn)
        self.assertEqual(self.local("SELECT COUNT(*) FROM 仕掛ロット"), [(0,)])
        [message] = [e for e in result.errors if "仕掛台帳" in e]
        self.assertNotIn(str(master), message)

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


class BoxPickScreenTests(unittest.TestCase):
    """ロット情報の画面: BOX実績寸法の横に、2つ目の候補を選ぶ欄(`lotdetail.boxPicker`)。"""

    def test_BOX実績寸法のときだけ選ぶ欄を出す(self) -> None:
        js = (Path(__file__).resolve().parent.parent / "app/static/js/lotdetail.js").read_text("utf-8")
        self.assertIn("function boxPicker(choices)", js)
        self.assertIn("choices: view.is_box ? (view.box_choices || []) : []", js)
        self.assertIn("?box_pick=${encodeURIComponent(key)}", js)
        # 見るだけの画面(発注一覧の「Lotを開く」)は peek で引き直す(作業中のロットにしない)
        self.assertIn('peekMode ? "/peek" : ""', js)
        warehouse = (Path(__file__).resolve().parent.parent
                     / "app/static/js/views/warehouse.js").read_text("utf-8")
        self.assertIn("lotdetail.show(body, { peek: true })", warehouse)
