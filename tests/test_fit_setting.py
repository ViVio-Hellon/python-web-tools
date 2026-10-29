"""パレット適合設定(VER4.8.116)── 直す表と行を自動で決め、変わるパレットを見せてから共有へ書く。

現場の声:「パレット適合、サイズ修正 → サイズですか？適合ですか？ → 入力 → 業界、記号があるのか？ →
入力 → 探して反映。極力この機能に関してはマスタをダイレクトに操作させないつくりにしてください。
影響範囲が大きいです」

【ここで守りたいこと】
1. **見本と反映が同じ計算**(`pallet_service.fit_row`)。見本で言った行だけが変わる
2. 業界・記号の無いパレットは**帯を分けて**その寸法だけ変える(現場の判断「帯を分ける」)
3. 見本のあとに共有が変わっていたら**何も書かずに止める**
4. 書く前に控えを取り、記録から**元に戻せる**
5. サイズの入れ替え: 在庫の行は前の寸法のまま・限定条件は新しい寸法へ・重なれば止める
6. ほかの端末は心拍で気づいて取り込み直す(開き直さなくてよい)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from packaging_tool import (app_config, config, data_sync, db, fit_setting, import_specs,
                            pallet_service, pallet_threshold, selection_session)


def _ddl(table: str) -> list[str]:
    """手元のスキーマから、その表を作る文(と索引)を写す。"""
    mem = sqlite3.connect(":memory:")
    try:
        db.apply_schema(mem)
        rows = mem.execute("SELECT sql FROM sqlite_master WHERE tbl_name = ? AND sql IS NOT NULL"
                           " ORDER BY type DESC", (table,)).fetchall()
    finally:
        mem.close()
    return [r[0] for r in rows]


def make_threshold(path: Path, *, untyped: bool = False) -> Path:
    """共有の閾値マスタ(7表・基準表の初期値入り)。

    `untyped` … Access から変換したときの形(列に型が無く、数が文字 '1001' で入っている)。現場の写しはこの形
    """
    conn = sqlite3.connect(path)
    if untyped:
        for table, seed_rows in pallet_threshold.SEED.items():
            names = list(seed_rows[0])
            cols = ["ID"] + names + [c for c in ("有効", "並び順", "更新日時") if c not in names]
            conn.execute(f'CREATE TABLE "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)})')
            for i, row in enumerate(seed_rows, start=1):
                values = [str(i)] + [str(row[n]) for n in names] + ["1", "0", "2026/01/01 00:00:00"]
                conn.execute(f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in cols)})', values)
        conn.commit()
        conn.close()
        return path
    for table in pallet_threshold.TABLES:
        for sql in _ddl(table):
            conn.execute(sql)
    conn.commit()
    pallet_threshold.seed(conn)
    conn.close()
    return path


MASTER_DDL = """
    CREATE TABLE PalletMaster (
        管理番号 INTEGER PRIMARY KEY, 幅 INTEGER, 丈 INTEGER,
        巾適合min INTEGER, 巾適合max INTEGER, 丈適合min INTEGER, 丈適合max INTEGER,
        業界 TEXT, 記号 TEXT, 位置 TEXT, 在庫数 INTEGER, リスト管理 TEXT,
        桁数 INTEGER, 脚数 INTEGER, コード TEXT, 単位 TEXT, 備考 TEXT, 更新日時 TEXT);
    CREATE TABLE PalletPatterns (
        管理番号 INTEGER PRIMARY KEY, パレット幅 INTEGER, パレット丈 INTEGER,
        製品幅 INTEGER, 製品丈 INTEGER, 登録日時 TEXT, 更新日時 TEXT, 使用回数 INTEGER);
"""


UNTYPED_MASTER_DDL = """
    CREATE TABLE PalletMaster ("管理番号", "幅", "丈", "巾適合min", "巾適合max", "丈適合min", "丈適合max",
        "業界", "記号", "位置", "在庫数", "更新日時", "リスト管理", "桁数", "脚数", "コード", "単位", "備考");
    CREATE TABLE PalletPatterns ("管理番号", "パレット幅", "パレット丈", "製品幅", "製品丈", "登録日時", "更新日時", "使用回数");
"""


class FitSettingTestCase(unittest.TestCase):
    """共有の梱包資材マスタ・閾値マスタと、手元のDB(取り込み済み)を用意する。"""

    # 共有を Access から変換した形(型の無い列・数は文字)で作るか
    UNTYPED = False

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="fitset_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.threshold = make_threshold(self.dir / config.THRESHOLD_DB_NAME, untyped=self.UNTYPED)
        self.master = self.dir / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(self.master)
        conn.executescript(UNTYPED_MASTER_DDL if self.UNTYPED else MASTER_DDL)
        for sql in _ddl(import_specs.PALLET_LIMIT_TABLE):
            conn.execute(sql)
        conn.commit()
        conn.close()
        for target, value in ((data_sync, "find_threshold_db"), (data_sync, "find_material_db")):
            path = self.threshold if value == "find_threshold_db" else self.master
            patcher = mock.patch.object(target, value, return_value=path)
            patcher.start()
            self.addCleanup(patcher.stop)
        backup_dir = self.dir / "local"
        patcher = mock.patch.object(app_config, "local_dir", side_effect=lambda name: backup_dir / name)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        db.apply_schema(self.conn)
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        selection_session.get_session(self.conn).admin = True
        fit_setting.forget_watch()
        self.addCleanup(fit_setting.forget_watch)
        self.next_no = 1

    # ------------------------------------------------------------------
    def pallet(self, width: int, length: int, *, code: str = "", unit: str = "台", industry: str = "",
               symbol: str = "", position: str = "", stock: int = 0, keta: int = 0, ashi: int = 0) -> None:
        conn = sqlite3.connect(self.master)
        values = (self.next_no, width, length, industry, symbol, position, stock,
                  "" if position else "要", keta, ashi, code, unit)
        if self.UNTYPED:                      # 現場の写しと同じく、数も文字で入れる
            values = tuple(str(v) for v in values)
        conn.execute("INSERT INTO PalletMaster (管理番号, 幅, 丈, 業界, 記号, 位置, 在庫数, リスト管理, 桁数, 脚数,"
                     " コード, 単位, 備考, 更新日時) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'','2026-01-01 00:00:00')",
                     values)
        conn.commit()
        conn.close()
        self.next_no += 1

    def sync(self) -> None:
        """取り込みと同じ姿にする(共有 → 手元 → 計算し直し)。"""
        fit_setting._import(self.conn, self.threshold, import_specs.THRESHOLD_TABLES)
        fit_setting._import(self.conn, self.master, fit_setting.MASTER_TABLES)
        pallet_service.recompute_fit_ranges(self.conn)

    def local(self, code: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM PalletMaster WHERE コード = ?", (code,)).fetchone()

    def shared(self, path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def bands(self, table: str = "PalletHabaThreshold", lo: int = 951, hi: int = 1100) -> list[tuple]:
        return [tuple(r) for r in self.shared(
            self.threshold, f"SELECT 入力最小値, 入力最大値, 適合最小値, 適合最大値 FROM {table}"
                            " WHERE CAST(入力最大値 AS INTEGER) >= ? AND CAST(入力最小値 AS INTEGER) <= ?"
                            " ORDER BY CAST(入力最小値 AS INTEGER)", (lo, hi))]

    def group(self, width: int, length: int, industry: str = "一般", symbol: str = "") -> str:
        found = fit_setting.find(self.conn, width, length)
        for g in found["groups"]:
            if g["industry"] == industry and g["symbol"] == symbol:
                return g["key"]
        self.fail(f"{width}×{length} {industry}・{symbol} の組がありません: {found}")


# ==================================================================
# 探す
# ==================================================================
class FindTests(FitSettingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pallet(1035, 3150, code="P-1", unit="台")
        self.pallet(1035, 3150, code="P-2", unit="枚")
        self.pallet(1035, 3150, position="A-1", stock=4)
        self.pallet(1035, 3150, code="C-1", industry="4×8", symbol="C1 ﾊﾟﾚｯﾄ台")
        self.sync()

    def test_業界記号ごとにまとめて_いまの決まりと範囲を出す(self) -> None:
        found = fit_setting.find(self.conn, 1035, 3150)
        groups = {g["label"]: g for g in found["groups"]}
        plain = groups["一般・記号なし"]
        self.assertEqual((plain["catalog"], plain["stock_rows"]), (2, 1))
        self.assertEqual(plain["rule"], pallet_service.RULE_BAND)
        # 帯 1001〜1050 → 991〜1040 を現物−10 で頭打ち = 1025
        self.assertEqual((plain["fit"]["巾適合min"], plain["fit"]["巾適合max"]), (991, 1025))
        self.assertEqual(groups["4×8・C1"]["rule"], pallet_service.RULE_SYMBOL)

    def test_無い寸法は近い寸法を案内する(self) -> None:
        found = fit_setting.find(self.conn, 1035, 3000)
        self.assertEqual(found["groups"], [])
        self.assertIn("3150", found["nearby"][0])


# ==================================================================
# 適合を直す: 帯を分ける
# ==================================================================
class SplitBandTests(FitSettingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pallet(1035, 3150, code="P-1")
        self.pallet(1035, 2000, code="P-2")       # 同じ幅(業界は問わず同じ帯)
        self.pallet(1040, 2000, code="P-3")       # 同じ帯の別の幅
        self.sync()
        self.req = {"width": 1035, "length": 3150, "group": self.group(1035, 3150),
                    "w_min": 1000, "w_max": 1020, "l_min": 2641, "l_max": 3140}

    def test_記号の無いパレットは帯を分けるが既定(self) -> None:
        plan = fit_setting.plan_fit(self.conn, self.req)
        self.assertEqual(plan.option, fit_setting.OPT_SPLIT)
        changed = {a["code"] for a in plan.affected}
        self.assertEqual(changed, {"P-1", "P-2"})            # 幅1040 は変わらない
        self.assertIn("ほかに 1行", " ".join(plan.notes))      # P-2 は「ほか」として言う

    def test_反映すると帯が3つに分かれ_手元にもすぐ効く(self) -> None:
        plan = fit_setting.plan_fit(self.conn, self.req)
        result = fit_setting.apply(self.conn, plan.id)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.bands(lo=1001, hi=1050),
                         [(1001, 1034, 991, 1040), (1035, 1035, 1000, 1020), (1036, 1050, 991, 1040)])
        row = self.local("P-1")
        self.assertEqual((row["巾適合min"], row["巾適合max"]), (1000, 1020))
        self.assertEqual(self.local("P-3")["巾適合max"], 1030)   # 1040 は前のまま(1040−10)
        # 手元の帯も検査を通る(穴・重なりが無い)
        pallet_pallet = pallet_threshold.load(self.conn)
        self.assertEqual(pallet_pallet.haba_max(1035), 1020)

    def test_見本で言った行と反映で変わった行が同じ(self) -> None:
        before = {r["コード"]: tuple(r[c] for c in fit_setting.FIT_COLUMNS)
                  for r in self.conn.execute("SELECT * FROM PalletMaster")}
        plan = fit_setting.plan_fit(self.conn, self.req)
        fit_setting.apply(self.conn, plan.id)
        after = {r["コード"]: tuple(r[c] for c in fit_setting.FIT_COLUMNS)
                 for r in self.conn.execute("SELECT * FROM PalletMaster")}
        self.assertEqual({c for c in before if before[c] != after[c]}, {a["code"] for a in plan.affected})

    def test_帯ぜんぶを選ぶと同じ帯のパレットが全部変わる(self) -> None:
        plan = fit_setting.plan_fit(self.conn, {**self.req, "option": fit_setting.OPT_BAND})
        self.assertEqual({a["code"] for a in plan.affected}, {"P-1", "P-2", "P-3"})

    def test_上限が現物マイナス10を超えるなら頭打ちになると言う(self) -> None:
        plan = fit_setting.plan_fit(self.conn, {**self.req, "w_max": 1030})
        self.assertIn("1025", " ".join(plan.notes))

    def test_いまと同じ範囲なら作らない(self) -> None:
        with self.assertRaises(fit_setting.FitError):
            fit_setting.plan_fit(self.conn, {**self.req, "w_min": 991, "w_max": 1025})

    def test_最小が最大より大きければ断る(self) -> None:
        with self.assertRaises(fit_setting.FitError):
            fit_setting.plan_fit(self.conn, {**self.req, "w_min": 1030, "w_max": 1000})

    def test_見本のあとに共有の帯が変わっていたら何も書かない(self) -> None:
        plan = fit_setting.plan_fit(self.conn, self.req)
        conn = sqlite3.connect(self.threshold)
        conn.execute("UPDATE PalletHabaThreshold SET 備考 = 'ほかの端末' WHERE 入力最小値 = 1001")
        conn.commit()
        conn.close()
        result = fit_setting.apply(self.conn, plan.id)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, fit_setting.REFUSE_CHANGED)
        self.assertEqual(self.bands(lo=1001, hi=1050), [(1001, 1050, 991, 1040)])

    def test_管理者認証が無ければ書かない(self) -> None:
        plan = fit_setting.plan_fit(self.conn, self.req)
        selection_session.get_session(self.conn).admin = False
        result = fit_setting.apply(self.conn, plan.id)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, fit_setting.REFUSE_NOT_ALLOWED)
        self.assertEqual(self.bands(lo=1001, hi=1050), [(1001, 1050, 991, 1040)])

    def test_書く前に控えを取る(self) -> None:
        plan = fit_setting.plan_fit(self.conn, self.req)
        fit_setting.apply(self.conn, plan.id)
        copies = list((self.dir / "local" / "backup").glob("*"))
        self.assertTrue(any(config.THRESHOLD_DB_NAME.split(".")[0] in c.name for c in copies), copies)

    def test_元に戻すと帯も適合も前のとおり(self) -> None:
        plan = fit_setting.plan_fit(self.conn, self.req)
        done = fit_setting.apply(self.conn, plan.id)
        back = fit_setting.undo(self.conn, done.log_no)
        self.assertTrue(back.ok, back.message)
        self.assertEqual(self.bands(lo=1001, hi=1050), [(1001, 1050, 991, 1040)])
        self.assertEqual(self.local("P-1")["巾適合max"], 1025)
        history = fit_setting.history()
        self.assertEqual(history[0]["種類"], fit_setting.KIND_UNDO)
        self.assertEqual(history[1]["戻した"], 1)
        self.assertFalse(fit_setting.undo(self.conn, done.log_no).ok)   # 2度は戻さない

    def test_もう一度直すときは分けた帯をそのまま直す(self) -> None:
        plan = fit_setting.plan_fit(self.conn, self.req)
        fit_setting.apply(self.conn, plan.id)
        again = fit_setting.plan_fit(self.conn, {**self.req, "w_min": 1005})
        self.assertEqual([o.action for o in again.ops], ["update"])
        fit_setting.apply(self.conn, again.id)
        self.assertEqual(self.bands(lo=1035, hi=1035), [(1035, 1035, 1005, 1020)])


# ==================================================================
# 適合を直す: 記号表・固定の表
# ==================================================================
class FixedTableTests(FitSettingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pallet(1100, 2000, code="Z-1", symbol="Z9 ﾊﾟﾚｯﾄ台", keta=4, ashi=3)
        self.pallet(1100, 2000, code="Z-2", symbol="Z9 ﾊﾟﾚｯﾄ蓋", unit="枚", keta=4, ashi=3)
        self.pallet(1100, 2000, code="N-1")                     # 同じ寸法・記号なし
        self.pallet(500, 760, code="C1-1", symbol="C1 ﾊﾟﾚｯﾄ台")
        self.pallet(510, 780, code="C1-2", symbol="C1 ﾊﾟﾚｯﾄ台")
        self.sync()

    def test_1つの寸法だけの記号は記号表に足すのが既定_その記号だけ変わる(self) -> None:
        req = {"width": 1100, "length": 2000, "group": self.group(1100, 2000, symbol="Z9"),
               "w_min": 1050, "w_max": 1085, "l_min": 1791, "l_max": 1990}
        plan = fit_setting.plan_fit(self.conn, req)
        self.assertEqual(plan.option, fit_setting.OPT_ADD_SYMBOL)
        self.assertEqual({a["code"] for a in plan.affected}, {"Z-1", "Z-2"})
        self.assertTrue(fit_setting.apply(self.conn, plan.id).ok)
        added = self.shared(self.threshold, "SELECT * FROM PalletSymbolMaster WHERE 記号 = 'Z9'")
        self.assertEqual((added[0]["巾適合最小値"], added[0]["桁数"], added[0]["脚数"]), (1050, 4, 3))
        self.assertEqual(self.local("Z-1")["巾適合min"], 1050)
        self.assertEqual(self.local("N-1")["巾適合min"], 1041)    # 記号なしは帯のまま

    def test_固定の表で決まるパレットはその行を直す_同じ記号の全寸法に効くと言う(self) -> None:
        req = {"width": 500, "length": 760, "group": self.group(500, 760, symbol="C1"),
               "w_min": 410, "w_max": 480, "l_min": 600, "l_max": 750}
        plan = fit_setting.plan_fit(self.conn, req)
        self.assertEqual(plan.option, fit_setting.OPT_EDIT)
        self.assertIn("2行", plan.options[0]["note"])
        self.assertEqual({a["code"] for a in plan.affected}, {"C1-1", "C1-2"})
        self.assertTrue(fit_setting.apply(self.conn, plan.id).ok)
        self.assertEqual(self.shared(self.threshold, "SELECT 巾適合最大値 FROM PalletSymbolMaster"
                                                     " WHERE 記号 = 'C1'")[0][0], 480)

    def test_現物より大きい製品まで載る範囲は止める(self) -> None:
        req = {"width": 500, "length": 760, "group": self.group(500, 760, symbol="C1"),
               "w_min": 410, "w_max": 505, "l_min": 600, "l_max": 750}
        with self.assertRaises(fit_setting.FitError) as caught:
            fit_setting.plan_fit(self.conn, req)
        self.assertEqual(caught.exception.reason, fit_setting.REFUSE_UNSAFE)

    def test_外してあった同じ記号の行は足さずに直して戻す(self) -> None:
        conn = sqlite3.connect(self.threshold)
        conn.execute("INSERT INTO PalletSymbolMaster (記号, 巾適合最小値, 巾適合最大値, 丈適合最小値, 丈適合最大値,"
                     " 桁数, 脚数, 有効) VALUES ('Z9', 1, 2, 3, 4, 0, 0, 0)")
        conn.commit()
        conn.close()
        self.sync()
        req = {"width": 1100, "length": 2000, "group": self.group(1100, 2000, symbol="Z9"),
               "w_min": 1050, "w_max": 1085, "l_min": 1791, "l_max": 1990}
        plan = fit_setting.plan_fit(self.conn, req)
        self.assertEqual([o.action for o in plan.ops], ["update"])
        self.assertTrue(fit_setting.apply(self.conn, plan.id).ok)
        rows = self.shared(self.threshold, "SELECT 有効, 巾適合最小値 FROM PalletSymbolMaster WHERE 記号 = 'Z9'")
        self.assertEqual([tuple(r) for r in rows], [(1, 1050)])


# ==================================================================
# サイズを直す
# ==================================================================
class SizeTests(FitSettingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pallet(1030, 3150, code="S-1", unit="台")
        self.pallet(1030, 3150, code="S-2", unit="枚")
        self.pallet(1030, 3150, position="B-2", stock=3)
        conn = sqlite3.connect(self.master)
        conn.execute(f'INSERT INTO "{import_specs.PALLET_LIMIT_TABLE}" (包装仕様NO, 業界, パレット幅, パレット丈, 有効)'
                     " VALUES ('1P9999', '', 1030, 3150, 1)")
        conn.commit()
        conn.close()
        self.sync()
        # 実績パターンは手元の写しで数える(取り込みが `pattern_sync` で入れるもの)
        self.conn.execute("INSERT INTO PalletPatterns (パレット幅, パレット丈, 製品幅, 製品丈, 登録日時, 更新日時)"
                          " VALUES (1030, 3150, 1000, 3000, '2026-01-01', '2026-01-01')")
        self.req = {"width": 1030, "length": 3150, "group": self.group(1030, 3150),
                    "new_width": 1035, "new_length": 3150}

    def test_コードが変わらなければ型録の行の寸法を書き換える_在庫の行は残す(self) -> None:
        plan = fit_setting.plan_size(self.conn, self.req)
        self.assertIn("在庫 1行", " ".join(plan.notes))
        self.assertIn("実績パターン 1件", " ".join(plan.notes))
        self.assertTrue(fit_setting.apply(self.conn, plan.id).ok)
        sizes = {r["コード"] or r["位置"]: (r["幅"], r["丈"])
                 for r in self.shared(self.master, "SELECT * FROM PalletMaster")}
        self.assertEqual(sizes, {"S-1": (1035, 3150), "S-2": (1035, 3150), "B-2": (1030, 3150)})
        # 限定条件は新しい寸法へ(前の寸法に型録の行が残らないので)
        limit = self.shared(self.master, f'SELECT パレット幅 FROM "{import_specs.PALLET_LIMIT_TABLE}"')
        self.assertEqual(limit[0][0], 1035)
        # 手元にもすぐ効く: 新しい寸法の適合は帯から(1035 → 991〜1025)
        row = self.local("S-1")
        self.assertEqual((row["幅"], row["巾適合max"]), (1035, 1025))

    def test_コードが変わるなら新しい行を足して前の行を消す(self) -> None:
        plan = fit_setting.plan_size(self.conn, {**self.req, "codes": {"S-1": "T-1", "S-2": "T-2"}})
        self.assertTrue(fit_setting.apply(self.conn, plan.id).ok)
        codes = {r["コード"]: (r["幅"], r["単位"])
                 for r in self.shared(self.master, "SELECT * FROM PalletMaster WHERE コード <> ''")}
        self.assertEqual(codes, {"T-1": (1035, "台"), "T-2": (1035, "枚")})

    def test_元に戻すと前のコードと管理番号の行が戻る(self) -> None:
        numbers = {r["コード"]: r["管理番号"] for r in self.shared(self.master, "SELECT * FROM PalletMaster")}
        plan = fit_setting.plan_size(self.conn, {**self.req, "codes": {"S-1": "T-1", "S-2": "T-2"}})
        done = fit_setting.apply(self.conn, plan.id)
        self.assertTrue(fit_setting.undo(self.conn, done.log_no).ok)
        rows = {r["コード"]: (r["管理番号"], r["幅"])
                for r in self.shared(self.master, "SELECT * FROM PalletMaster WHERE コード <> ''")}
        self.assertEqual(rows, {"S-1": (numbers["S-1"], 1030), "S-2": (numbers["S-2"], 1030)})
        self.assertEqual(self.shared(self.master, f'SELECT パレット幅 FROM "{import_specs.PALLET_LIMIT_TABLE}"')[0][0],
                         1030)

    def test_新しい寸法に同じ組と単位があれば止める(self) -> None:
        self.pallet(1035, 3150, code="X-1", unit="台")
        self.sync()
        with self.assertRaises(fit_setting.FitError) as caught:
            fit_setting.plan_size(self.conn, self.req)
        self.assertIn("もうあります", str(caught.exception))

    def test_使っているコードは新しいコードにできない(self) -> None:
        with self.assertRaises(fit_setting.FitError):
            fit_setting.plan_size(self.conn, {**self.req, "codes": {"S-1": "S-2", "S-2": "T-2"}})

    def test_コードを変えるなら全部の行に入れる(self) -> None:
        with self.assertRaises(fit_setting.FitError):
            fit_setting.plan_size(self.conn, {**self.req, "codes": {"S-1": "T-1"}})

    def test_在庫の行も動かすを選べば在庫の行も新しい寸法(self) -> None:
        plan = fit_setting.plan_size(self.conn, {**self.req, "stock": fit_setting.STOCK_MOVE})
        self.assertTrue(fit_setting.apply(self.conn, plan.id).ok)
        rows = self.shared(self.master, "SELECT 幅 FROM PalletMaster WHERE 位置 = 'B-2'")
        self.assertEqual([r[0] for r in rows], [1035])

    def test_前の寸法にほかのパレットが残るなら限定条件は書き換えない(self) -> None:
        self.pallet(1030, 3150, code="O-1", industry="4×8")
        self.sync()
        plan = fit_setting.plan_size(self.conn, self.req)
        self.assertIn("のまま残します", " ".join(plan.notes))
        fit_setting.apply(self.conn, plan.id)
        self.assertEqual(self.shared(self.master, f'SELECT パレット幅 FROM "{import_specs.PALLET_LIMIT_TABLE}"')[0][0],
                         1030)

    def test_分けた帯が使われなくなればつなぎ直す(self) -> None:
        # 1030 だけの帯を作っておく(適合設定で分けた帯)
        split = fit_setting.plan_fit(self.conn, {"width": 1030, "length": 3150, "group": self.req["group"],
                                                 "w_min": 1000, "w_max": 1015, "l_min": 2641, "l_max": 3140})
        self.assertTrue(fit_setting.apply(self.conn, split.id).ok)
        self.assertEqual(len(self.bands(lo=1001, hi=1050)), 3)
        plan = fit_setting.plan_size(self.conn, {**self.req, "stock": fit_setting.STOCK_MOVE})
        self.assertIn("つなぎ直します", " ".join(plan.notes))
        self.assertTrue(fit_setting.apply(self.conn, plan.id).ok)
        self.assertEqual(self.bands(lo=1001, hi=1050), [(1001, 1050, 991, 1040)])

    def test_在庫が残るあいだは分けた帯を残す(self) -> None:
        split = fit_setting.plan_fit(self.conn, {"width": 1030, "length": 3150, "group": self.req["group"],
                                                 "w_min": 1000, "w_max": 1015, "l_min": 2641, "l_max": 3140})
        fit_setting.apply(self.conn, split.id)
        plan = fit_setting.plan_size(self.conn, self.req)           # 在庫の行は 1030 のまま
        self.assertIn("まだ 1行 が使っている", " ".join(plan.notes))


# ==================================================================
# 製品で確かめる
# ==================================================================
class CheckProductTests(FitSettingTestCase):
    def test_前と後の候補を出す(self) -> None:
        self.pallet(1035, 3150, code="P-1")
        self.sync()
        plan = fit_setting.plan_fit(self.conn, {"width": 1035, "length": 3150,
                                                "group": self.group(1035, 3150),
                                                "w_min": 1000, "w_max": 1020, "l_min": 2641, "l_max": 3140})
        got = fit_setting.check_product(plan.id, 995, 3000)       # 前(991〜)も後(1000−5 = 995〜)も載る
        self.assertEqual((got["before"], got["after"]), (1, 1))
        got = fit_setting.check_product(plan.id, 990, 3000)       # 前は 991−5 で載る、後は 1000−5 に届かない
        self.assertEqual(got["removed"], [plan.labels[next(iter(plan.labels))]])


# ==================================================================
# ほかの端末
# ==================================================================
class OtherTerminalTests(FitSettingTestCase):
    def test_閾値マスタが変わったら取り込み直して計算し直す(self) -> None:
        self.pallet(1035, 3150, code="P-1")
        self.sync()
        # ほかの端末(手元のDBは別)。起動時に取り込み済み
        other = sqlite3.connect(":memory:")
        other.row_factory = sqlite3.Row
        self.addCleanup(other.close)
        db.apply_schema(other)
        fit_setting._import(other, self.threshold, import_specs.THRESHOLD_TABLES)
        fit_setting._import(other, self.master, fit_setting.MASTER_TABLES)
        pallet_service.recompute_fit_ranges(other)
        from packaging_tool import sync_auto
        sync_auto.mark_imported(other, self.threshold)
        fit_setting._note_seen_latest(self.threshold)
        self.assertFalse(fit_setting.refresh_if_changed(other))   # 変わっていなければ何もしない

        plan = fit_setting.plan_fit(self.conn, {"width": 1035, "length": 3150, "group": self.group(1035, 3150),
                                                "w_min": 1000, "w_max": 1020, "l_min": 2641, "l_max": 3140})
        self.assertTrue(fit_setting.apply(self.conn, plan.id).ok)
        future = time.time() + 5
        os.utime(self.threshold, (future, future))
        self.assertTrue(fit_setting.refresh_if_changed(other))
        row = other.execute("SELECT 巾適合min FROM PalletMaster WHERE コード = 'P-1'").fetchone()
        self.assertEqual(row[0], 1000)

    def test_サイズを直した記録が増えたらパレットも取り込む(self) -> None:
        self.pallet(1030, 3150, code="S-1")
        self.sync()
        other = sqlite3.connect(":memory:")
        other.row_factory = sqlite3.Row
        self.addCleanup(other.close)
        db.apply_schema(other)
        fit_setting._import(other, self.master, fit_setting.MASTER_TABLES)
        fit_setting._note_seen_latest(self.threshold)
        plan = fit_setting.plan_size(self.conn, {"width": 1030, "length": 3150, "group": self.group(1030, 3150),
                                                 "new_width": 1035, "new_length": 3150})
        self.assertTrue(fit_setting.apply(self.conn, plan.id).ok)
        fit_setting._seen_log = 0                                  # ほかの端末はまだ見ていない
        future = time.time() + 5
        os.utime(self.threshold, (future, future))
        self.assertTrue(fit_setting.refresh_if_changed(other))
        self.assertEqual(other.execute("SELECT 幅 FROM PalletMaster WHERE コード = 'S-1'").fetchone()[0], 1035)


# ==================================================================
# 現場の写しの形(型の無い列・数は文字)
# ==================================================================
class UntypedFileTests(FitSettingTestCase):
    """共有は Access から変換したファイルで、列に型が無く数が文字('1001')で入っている。
    **同じ形のまま書く**(数で書くと同じ列に文字と数が混ざる)。元に戻すのも同じ形で確かめて戻す。"""

    UNTYPED = True

    def setUp(self) -> None:
        super().setUp()
        self.pallet(1035, 3150, code="P-1")
        self.pallet(1035, 3150, code="P-2", unit="枚")
        self.sync()

    def kinds(self, path: Path, sql: str) -> set[str]:
        return {r[0] for r in self.shared(path, sql)}

    def test_帯を分けても文字のまま_元に戻せる(self) -> None:
        plan = fit_setting.plan_fit(self.conn, {"width": 1035, "length": 3150, "group": self.group(1035, 3150),
                                                "w_min": 1000, "w_max": 1020, "l_min": 2641, "l_max": 3140})
        done = fit_setting.apply(self.conn, plan.id)
        self.assertTrue(done.ok, done.message)
        self.assertEqual(self.kinds(self.threshold, "SELECT typeof(入力最小値) FROM PalletHabaThreshold"), {"text"})
        # 足した帯にも番号が入る(Access から変換した表は自動で入らない)
        self.assertEqual(self.kinds(self.threshold, "SELECT typeof(ID) FROM PalletHabaThreshold"), {"text"})
        self.assertEqual(self.local("P-1")["巾適合min"], 1000)
        self.assertTrue(fit_setting.undo(self.conn, done.log_no).ok)
        self.assertEqual(self.bands(lo=1001, hi=1050), [("1001", "1050", "991", "1040")])

    def test_サイズを直しても文字のまま_コードを変えても形をそろえる(self) -> None:
        plan = fit_setting.plan_size(self.conn, {"width": 1035, "length": 3150, "group": self.group(1035, 3150),
                                                 "new_width": 1040, "new_length": 3150,
                                                 "codes": {"P-1": "Q-1", "P-2": "Q-2"}})
        done = fit_setting.apply(self.conn, plan.id)
        self.assertTrue(done.ok, done.message)
        self.assertEqual(self.kinds(self.master, "SELECT typeof(幅) FROM PalletMaster"), {"text"})
        self.assertEqual(self.kinds(self.master, "SELECT typeof(管理番号) FROM PalletMaster"), {"text"})
        self.assertEqual(self.local("Q-1")["幅"], 1040)
        self.assertTrue(fit_setting.undo(self.conn, done.log_no).ok)
        self.assertEqual({r["コード"] for r in self.shared(self.master, "SELECT コード FROM PalletMaster")},
                         {"P-1", "P-2"})


# ==================================================================
# 計算を分けても前と同じ
# ==================================================================
class SameEngineTests(FitSettingTestCase):
    def test_共有から組んだ計算と手元の計算が同じ(self) -> None:
        for i, (w, l, sym, ind) in enumerate([(1035, 3150, "", ""), (500, 760, "C1 ﾊﾟﾚｯﾄ台", ""),
                                              (1500, 3000, "", "5×10"), (1500, 3000, "強度UP ﾊﾟﾚｯﾄ台", "5×10"),
                                              (2.5, 100, "", "")]):
            self.pallet(int(w), l, code=f"E-{i}", symbol=sym, industry=ind)
        self.sync()
        engine = fit_setting._engine(fit_setting.read_shared().conv)
        for row in fit_setting._pallets(self.conn):
            got = fit_setting._effective(row, fit_setting._fit(row, engine))
            local = self.local(row["コード"])
            with self.subTest(code=row["コード"]):
                self.assertEqual(got, {c: local[c] for c in fit_setting.SHOWN_COLUMNS})


if __name__ == "__main__":
    unittest.main()
