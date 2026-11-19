"""作業ライン(VER4.8.144。`packaging_tool/work_lines.py`)

現場の声:
    「actualsdashboard_条件表.sqlite3: テーブル 条件表 にライン作業条件があります。LVC と HVC と SVC は VC の設備です」
    「設定タブに作業ラインタブ。どのラインで作業になるかのテストを行えるように」
    「倉庫連携時に、仕掛データと照らし合わせて条件に合うもの(作業ラインに入っているもの)を作業予定ラインにセットし、
     倉庫へ送信を押した段階で作業予定ラインに入っていなければ自動で出力」

【ここで守りたいこと】
1. 決まり方は実績ダッシュボードの条件表どおり(見る順番・VC優先・除外・空は条件なし・材質の前方一致)
2. グループ名をラインの呼び名に読み替える(ﾄｯﾄ(3SR) → トット、ﾊﾞﾗﾝｻｰﾗｲﾝ → バランサー)。当たらなければ作業ラインではない
3. 条件表・仕掛を読めなければ決めない(作業予定ラインに勝手に入れない)
4. 倉庫連携の作業予定ラインに選んでおく。倉庫へ送信で、まだ予定に入っていなければ自動で入れる
   (もう予定に入っている・作業済みのロットは触らない)
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packaging_tool import config, lot_plans, work_lines
from tests.test_web_selection import SelectionWebTestCase, insert_pallet

COLS = ["見る順番", "番号", "グループ名", "VC優先", "材質", "製造材質", "板厚MIN", "板厚MAX", "板厚MAX未満",
        "板幅MIN", "板幅MAX", "板幅MAX未満", "板丈MIN", "板丈MINより大", "板丈MAX", "板丈MAX未満",
        "単重MIN", "単重MAX", "単重MAX未満", "除外を使う"]
NOT_COLS = [f"除外_{c}" for c in COLS[4:19]]


def write_table(folder: Path, groups: list[dict], basis: str = "ｵｰﾀﾞｰ") -> Path:
    path = folder / work_lines.FILE_NAME
    path.unlink(missing_ok=True)
    work_lines._cache.clear()
    con = sqlite3.connect(path)
    cols = COLS + NOT_COLS
    con.execute(f'CREATE TABLE "条件表" ({", ".join(f"{chr(34)}{c}{chr(34)}" for c in cols)})')
    for i, g in enumerate(groups, start=1):
        row = {"見る順番": i, "番号": i, "VC優先": 0, "除外を使う": 0, **g}
        con.execute(f'INSERT INTO "条件表" VALUES ({",".join("?" * len(cols))})', [row.get(c) for c in cols])
    con.execute('CREATE TABLE "情報" ("項目" TEXT PRIMARY KEY, "値" TEXT)')
    con.executemany('INSERT INTO "情報" VALUES (?, ?)', [("版", "3"), ("寸法の基準", basis), ("除外の寸法の基準", basis)])
    con.commit()
    con.close()
    return path


def lot(**kw) -> dict:
    base = {"オーダー材質": "5052", "製造材質": "5052", "オーダー板厚": 3.0, "オーダー板幅": 1000.0,
            "オーダー板丈": 2000.0, "製造板厚": 3.0, "製造板幅": 1000.0, "製造板丈": 2000.0, "比重": 2.68,
            "形状": "10", "VC": "00"}
    base.update(kw)
    return base


class _Folder(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(config, "master_db_dir", lambda: self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        work_lines._cache.clear()
        self.addCleanup(work_lines._cache.clear)


class ClassifyTests(_Folder):
    def table(self, groups, **kw):
        write_table(self.dir, groups, **kw)
        return work_lines.load()

    def test_グループ名をラインの呼び名に読み替える(self) -> None:
        self.assertEqual(work_lines.group_line("ﾄｯﾄ(3SR)"), "トット")
        self.assertEqual(work_lines.group_line("ﾊﾞﾗﾝｻｰﾗｲﾝ"), "バランサー")
        self.assertEqual(work_lines.group_line("ﾊﾞﾗﾝｻｰﾗｲﾝ(532H)"), "バランサー")
        self.assertEqual(work_lines.group_line("L-1"), "L-1")
        self.assertEqual(work_lines.group_line("SVC"), "SVC")
        for other in ("丸徳ﾗｲﾝ", "生地その他", "VCその他", "3SR指定"):
            self.assertEqual(work_lines.group_line(other), "", other)

    def test_見る順番の順で最初に当たったグループ_空は条件なし(self) -> None:
        t = self.table([{"グループ名": "ﾄｯﾄ", "板厚MAX": 2.0}, {"グループ名": "L-1", "板厚MIN": 1.0},
                        {"グループ名": "生地その他"}])
        g, _ = work_lines.classify(t, lot())
        self.assertEqual(g.name, "L-1")
        g, _ = work_lines.classify(t, lot(オーダー板厚=1.5))
        self.assertEqual(g.name, "ﾄｯﾄ")

    def test_VC付きのロットはVC優先のグループが勝つ(self) -> None:
        t = self.table([{"グループ名": "L-1"}, {"グループ名": "LVC", "VC優先": 1, "板厚MIN": 1.0}])
        self.assertEqual(work_lines.classify(t, lot(VC="01"))[0].name, "LVC")
        self.assertEqual(work_lines.classify(t, lot(VC="00"))[0].name, "L-1")

    def test_除外に全部当たればその行だけ外す(self) -> None:
        t = self.table([{"グループ名": "LVC", "除外を使う": 1, "除外_製造材質": "-99*,3N*,1070", "除外_板厚MAX未満": 5.0},
                        {"グループ名": "L-1"}])
        self.assertEqual(work_lines.classify(t, lot(製造材質="3N05"))[0].name, "L-1")
        self.assertEqual(work_lines.classify(t, lot(製造材質="3N05", オーダー板厚=6.0))[0].name, "LVC",
                         "除外の条件が1つでも外れれば除外しない")
        self.assertEqual(work_lines.classify(t, lot(製造材質="5052"))[0].name, "LVC")

    def test_両端の含み方と単重(self) -> None:
        t = self.table([{"グループ名": "SVC", "板厚MIN": 3.0, "板厚MAX未満": 3.0}, {"グループ名": "HVC", "単重MAX": 16.0},
                        {"グループ名": "L-1"}])
        # 単重 = 3×1000×2000×2.68/1e6 = 16.08 は 単重MAX 16 を超える。SVC は「MAX未満 3」で板厚 3 を含まない
        self.assertEqual(work_lines.unit_weight(lot()), 16.08)
        self.assertEqual(work_lines.classify(t, lot())[0].name, "L-1")
        self.assertEqual(work_lines.classify(t, lot(製造板丈=1900.0))[0].name, "HVC", "15.28 は 16 以下")
        t = self.table([{"グループ名": "HVC", "単重MAX": 16.08}, {"グループ名": "L-1"}])
        self.assertEqual(work_lines.classify(t, lot())[0].name, "HVC", "MAX は両端を含む")

    def test_材質は数でも文字でも当たる_前方一致(self) -> None:
        t = self.table([{"グループ名": "ﾊﾞﾗﾝｻｰﾗｲﾝ(532H)", "材質": "532H"}, {"グループ名": "L-1", "材質": "5083,6*"}])
        self.assertEqual(work_lines.classify(t, lot(オーダー材質="532H"))[0].line, "バランサー")
        self.assertEqual(work_lines.classify(t, lot(オーダー材質="5083"))[0].name, "L-1")
        self.assertEqual(work_lines.classify(t, lot(オーダー材質="6061"))[0].name, "L-1")
        self.assertIsNone(work_lines.classify(t, lot(オーダー材質="7075"))[0])

    def test_板でなければ入らない(self) -> None:
        t = self.table([{"グループ名": "L-1"}])
        self.assertIsNone(work_lines.classify(t, lot(形状="30", 製造板丈=0.0))[0])

    def test_条件表が無ければ決めない(self) -> None:
        with self.assertRaises(work_lines.Unreadable):
            work_lines.load()
        d = work_lines.decide(sqlite3.connect(":memory:"), "A1")
        self.assertEqual((d.line, d.known), ("", False))
        self.assertIn(work_lines.FILE_NAME, d.why)


class WarehouseTests(SelectionWebTestCase):
    """倉庫連携: 作業予定ラインに選んでおき、倉庫へ送信で自動で入れる。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import warehouse as wh_routes
        original = wh_routes.get_db
        wh_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(wh_routes, "get_db", original))
        insert_pallet(self.conn, width=1100, length=2000, code="P9", unit="枚", symbol="SP-18")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(config, "master_db_dir", lambda: self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        work_lines._cache.clear()
        self.addCleanup(work_lines._cache.clear)
        write_table(self.dir, [{"グループ名": "丸徳ﾗｲﾝ", "板厚MAX": 1.0}, {"グループ名": "L-1", "板厚MIN": 2.0}])
        for no, t in (("7654321", 3.0), ("7654322", 0.5)):
            self.conn.execute(
                "INSERT INTO 仕掛ロット (ロット番号, オーダー材質, 製造材質, オーダー板厚, オーダー板幅, オーダー板丈,"
                " 製造板厚, 製造板幅, 製造板丈, 比重, 形状, VC) VALUES (?, '5052', '5052', ?, 1000, 2000, ?, 1000, 2000,"
                " 2.68, '10', '00')", (no, t, t))
        self.conn.commit()
        bg = mock.patch("packaging_tool.data_sync.write_back_in_background")
        bg.start()
        self.addCleanup(bg.stop)

    def send_lot(self, lot_no: str):
        self.post("/api/selection/pallet/pick", {"width": 1100, "length": 2000, "symbol": "SP-18"})
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})
        self.post("/api/selection/send")
        draft = work_context_draft()
        body = {k: v for k, v in draft.items() if not k.startswith("pallet_") and k not in ("no_lot", "notice")}
        body.update(from_draft=True, lot_no=lot_no, hatchu_suu="4", nounyusaki="納入先", confirm_duplicate=True)
        return self.client.post("/api/warehouse/send", json=body, headers=self.auth())

    def test_条件表で決まるラインを選んでおく(self) -> None:
        state = self.client.get("/api/warehouse/lot-plan?lot_no=7654321", headers=self.auth()).get_json()
        self.assertEqual(state["suggested_line"], "L-1", state)
        self.assertIn("L-1", state["suggest_note"])
        state = self.client.get("/api/warehouse/lot-plan?lot_no=7654322", headers=self.auth()).get_json()
        self.assertEqual(state["suggested_line"], "")
        self.assertIn("作業ラインではない", state["suggest_note"])

    def test_倉庫へ送信で予定に入っていなければ自動で入れる(self) -> None:
        res = self.send_lot("7654321")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIn("作業予定ライン L-1 に自動で入れました", res.get_json()["message"])
        self.assertEqual(lot_plans.current(self.conn, "7654321").line, "L-1")

    def test_作業ラインでなければ入れない(self) -> None:
        res = self.send_lot("7654322")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIsNone(lot_plans.current(self.conn, "7654322"))

    def test_もう予定に入っていれば触らない(self) -> None:
        lot_plans.send(self.conn, "7654321", "HVC", terminal="LEADER")
        res = self.send_lot("7654321")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertNotIn("自動で入れました", res.get_json()["message"])
        self.assertEqual(lot_plans.current(self.conn, "7654321").line, "HVC")

    def test_作業済みのロットにもう一度は知らせない(self) -> None:
        lot_plans.send(self.conn, "7654321", "L-1", terminal="LEADER")
        lot_plans.finish(self.conn, lot_plans.current(self.conn, "7654321").plan_id, lines=["L-1"], terminal="LINE1")
        self.send_lot("7654321")
        self.assertIsNone(lot_plans.current(self.conn, "7654321"))

    def test_条件表が無ければ入れず_送信は通る(self) -> None:
        (self.dir / work_lines.FILE_NAME).unlink()
        work_lines._cache.clear()
        res = self.send_lot("7654321")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIsNone(lot_plans.current(self.conn, "7654321"))


class SettingsTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(config, "master_db_dir", lambda: self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        work_lines._cache.clear()
        self.addCleanup(work_lines._cache.clear)

    def test_条件表を並べ_ロットNOでテストできる(self) -> None:
        from app.routes import settings as st_routes
        original = st_routes.get_db
        st_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(st_routes, "get_db", original))
        write_table(self.dir, [{"グループ名": "ﾄｯﾄ", "板厚MAX": 1.0}, {"グループ名": "生地その他"}])
        view = self.client.get("/api/settings/work-lines", headers=self.auth()).get_json()
        self.assertTrue(view["ok"], view)
        self.assertEqual([(g["name"], g["line"]) for g in view["groups"]], [("ﾄｯﾄ", "トット"), ("生地その他", "")])
        self.conn.execute("INSERT INTO 仕掛ロット (ロット番号, オーダー板厚, 製造板厚, 製造板幅, 製造板丈, 比重, 形状)"
                          " VALUES ('A1', 0.8, 0.8, 1000, 2000, 2.7, '10')")
        self.conn.commit()
        res = self.client.post("/api/settings/work-lines/test", json={"lot_no": "a1"}, headers=self.auth()).get_json()
        self.assertEqual((res["line"], res["group"]), ("トット", "ﾄｯﾄ"))
        self.assertTrue(res["steps"][0]["ok"])
        res = self.client.post("/api/settings/work-lines/test", json={"lot_no": "ZZ"}, headers=self.auth()).get_json()
        self.assertIn("仕掛にありません", res["why"])


def work_context_draft() -> dict:
    from packaging_tool import work_context
    return work_context.get_context().peek_pending_orders()[0]


if __name__ == "__main__":
    unittest.main()
