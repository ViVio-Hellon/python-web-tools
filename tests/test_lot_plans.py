"""予定ロット・作業ライン・配置編集の拠点を追加(VER4.8.85)。

現場の声:
    「新規発注フレームの下余白に作業予定ライン選択を設けてください」
    「ロット検索欄に予定ロット欄を設けてください」
    「作業予定ラインで設定されたロットは、アクセス権限でのライン名と一致したPCのロット検索で通知が出ます。
     通知確認すると作業予定ラインされたロット情報が自動で開きます」
    「通知確認ボタンを設けて見なくても通知は消せるようにして下さい」
    「マスタに拠点を追加して、配置編集で拠点追加できるようにしてください。現在の L-1、HVC、LVC は
     ハードコードのデフォルト拠点として残してください」
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import (access_control, config, data_sync, db, floor_plan,  # noqa: E402
                            import_specs, layout_session, location_service as svc, lot_plans,
                            master_schema, modes, sync_writeback, user_settings)
from tests import _web  # noqa: E402

PC = "LINE-PC-01"
ME = {access_control.ENV_LOGIN: "worker", access_control.ENV_HOST: PC}


def access_row(conn, *, pc: str = PC, login: str = "", permission: str = "mode:field",
               line: str = "", enabled: int = 1) -> None:
    conn.execute('INSERT INTO "アクセス権限" (ログインID, PC名, 権限, 有効, ライン名)'
                 " VALUES (?, ?, ?, ?, ?)", (login, pc, permission, enabled, line))
    conn.commit()


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)

    def send(self, lot: str, line: str):
        return lot_plans.send(self.conn, lot, line, terminal="LEADER", sender="cho")

    def test_入れる_同じラインなら何もしない_別のラインへ入れ直す_外す(self) -> None:
        first = self.send(" n7120d0 ", "L-1")
        self.assertTrue(first.ok and first.changed)
        self.assertEqual(lot_plans.current(self.conn, "N7120D0").line, "L-1")
        again = self.send("N7120D0", "L1")                 # L-1 と L1 は同じライン
        self.assertTrue(again.ok)
        self.assertFalse(again.changed)
        moved = self.send("N7120D0", "HVC")
        self.assertIn("入れ直しました", moved.message)
        self.assertEqual(lot_plans.current(self.conn, "N7120D0").line, "HVC")
        gone = lot_plans.cancel(self.conn, "N7120D0", terminal="LEADER")
        self.assertTrue(gone.ok)
        self.assertIsNone(lot_plans.current(self.conn, "N7120D0"))
        self.assertFalse(lot_plans.cancel(self.conn, "N7120D0", terminal="LEADER").ok)

    def test_ロットもラインも無ければ断る(self) -> None:
        self.assertEqual(self.send("", "L1").reason, lot_plans.REFUSE_NO_LOT)
        self.assertEqual(self.send("A1", " ").reason, lot_plans.REFUSE_NO_LINE)

    def test_この端末のラインの分だけ_未確認を先に(self) -> None:
        self.send("A1", "L1")
        self.send("A2", "HVC")
        self.send("A3", "Ｌ－１")                           # 全角でも同じライン
        self.send("A4", "L1")
        lot_plans.cancel(self.conn, "A4", terminal="LEADER")
        items = lot_plans.for_lines(self.conn, ["L-1"])
        self.assertEqual(sorted(p.lot_no for p in items), ["A1", "A3"])
        self.assertEqual(lot_plans.unseen_count(self.conn, ["L1"]), 2)
        self.assertEqual(lot_plans.for_lines(self.conn, []), [])
        first = lot_plans.current(self.conn, "A1")
        self.assertTrue(lot_plans.mark_seen(self.conn, first.plan_id))
        items = lot_plans.for_lines(self.conn, ["L1"])
        self.assertEqual([p.lot_no for p in items], ["A3", "A1"])     # 未確認が先
        self.assertEqual(lot_plans.unseen_count(self.conn, ["L1"]), 1)

    def test_入れ直されたらもう一度知らせる(self) -> None:
        self.send("A1", "L1")
        lot_plans.mark_seen(self.conn, lot_plans.current(self.conn, "A1").plan_id, opened=True)
        self.assertEqual(lot_plans.unseen_count(self.conn, ["L1"]), 0)
        self.send("A1", "HVC")
        self.send("A1", "L1")
        self.assertEqual(lot_plans.unseen_count(self.conn, ["L1"]), 1)

    def test_古い予定は出さない(self) -> None:
        self.send("A1", "L1")
        later = lot_plans.datetime.now() + lot_plans.timedelta(days=lot_plans.LIST_DAYS + 1)
        self.assertEqual(lot_plans.for_lines(self.conn, ["L1"], now=later), [])

    def test_無い予定は確認できない(self) -> None:
        self.assertFalse(lot_plans.mark_seen(self.conn, "nothing"))

    def test_表が無い古い手元DBでも落ちない(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertIsNone(lot_plans.current(conn, "A1"))
        self.assertEqual(lot_plans.for_lines(conn, ["L1"]), [])


class LineTests(unittest.TestCase):
    """アクセス権限の「ライン名」がこの端末のライン。"""

    def setUp(self) -> None:
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.me = access_control.Identity(login_id="worker", pc_name=PC)

    def test_この端末の行のライン名(self) -> None:
        access_row(self.conn, line="L1")
        access_row(self.conn, pc="OTHER-PC", line="HVC")
        access_row(self.conn, login="WORKER", pc="", permission="other:tool", line="LVC, 大板")
        access_row(self.conn, line="HVC", enabled=0)                  # 無効の行は見ない
        access_row(self.conn, pc="", login="", line="HVC")           # 条件の無い行は誰にも効かない
        self.assertEqual(access_control.lines(self.conn, self.me), ["L1", "LVC", "大板"])

    def test_登録が無ければ空(self) -> None:
        self.assertEqual(access_control.lines(self.conn, self.me), [])
        self.assertEqual(access_control.lines(None, self.me), [])

    def test_前の版の手元DBにも列を足す(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE "アクセス権限" (管理番号 INTEGER PRIMARY KEY AUTOINCREMENT,'
                     " ログインID TEXT NOT NULL DEFAULT '', PC名 TEXT NOT NULL DEFAULT '',"
                     " 権限 TEXT NOT NULL, 有効 INTEGER NOT NULL DEFAULT 1,"
                     " 備考 TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT INTO アクセス権限 (PC名, 権限) VALUES (?, 'mode:field')", (PC,))
        self.assertEqual(access_control.lines(conn, self.me), [])     # 列が無くても落ちない
        db.apply_schema(conn)
        have = {r[1] for r in conn.execute('PRAGMA table_info("アクセス権限")')}
        self.assertIn("ライン名", have)

    def test_取り込みでは無くてよい列(self) -> None:
        self.assertIn("ライン名", import_specs.OPTIONAL_COLUMNS["アクセス権限"])
        self.assertIn(("ライン名", "ライン名"),
                      [(c, s) for c, s, _ in import_specs.IMPORT_SPECS["アクセス権限"]])

    def test_共有のアクセス権限に列を足す_あれば触らない(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="plan_src_"))
        path = folder / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE "アクセス権限" (ログインID TEXT, PC名 TEXT, 権限 TEXT, 有効 INTEGER, 備考 TEXT)')
        conn.commit()
        conn.close()
        notes = master_schema._add_access_line_column(path)
        self.assertTrue(any("ライン名" in n for n in notes), notes)
        conn = sqlite3.connect(path)
        have = {r[1] for r in conn.execute('PRAGMA table_info("アクセス権限")')}
        conn.close()
        self.assertIn("ライン名", have)
        self.assertEqual(master_schema._add_access_line_column(path), [])


class SyncSpecTests(unittest.TestCase):
    """書き戻し・取り込みの配線(パレット在庫引当と同じ形)。"""

    def test_書き戻しと取り込みの定義がそろっている(self) -> None:
        spec = next(s for s in sync_writeback.WRITEBACK_SPECS
                    if s.sqlite_table == config.TBL_LOT_PLAN)
        self.assertEqual(spec.identity_columns, ("予定ID",))
        self.assertIn(config.TBL_LOT_PLAN, import_specs.OPTIONAL_TABLES)
        ddl = sync_writeback.SHARED_TABLE_DDL[config.TBL_LOT_PLAN]
        conn = sqlite3.connect(":memory:")
        conn.execute(ddl)
        shared = [r[1] for r in conn.execute(f'PRAGMA table_info("{config.TBL_LOT_PLAN}")')]
        conn.close()
        local = _web.memory_db()
        self.addCleanup(local.close)
        mine = [r[1] for r in local.execute(f'PRAGMA table_info("{config.TBL_LOT_PLAN}")')]
        self.assertEqual(shared, [c for c in mine if c != sync_writeback.ORIGIN_COLUMN])
        imported = [c for c, _s, _f in import_specs.IMPORT_SPECS[config.TBL_LOT_PLAN]]
        self.assertEqual(imported, [c for c in shared if c != "管理番号"])
        # 確認(知らせを消した)は端末ごと。共有へは送らない
        self.assertNotIn(config.TBL_LOT_PLAN_SEEN,
                         {s.sqlite_table for s in sync_writeback.WRITEBACK_SPECS})


class HandoffTests(unittest.TestCase):
    """2台で: 長の端末が入れる → 共有へ → ラインの端末が見張りで受け取る。"""

    def setUp(self) -> None:
        from tests.test_warehouse_handoff import make_local, make_shared
        self.dir = Path(tempfile.mkdtemp(prefix="plan_handoff_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        make_shared(self.src)
        self.leader, self.line = make_local(), make_local()
        self.addCleanup(self.leader.close)
        self.addCleanup(self.line.close)
        for conn in (self.leader, self.line):
            data_sync.import_master(conn, self.src)
        lot_plans.forget_seen_stamp()
        self.addCleanup(lot_plans.forget_seen_stamp)

    def test_共有へ送り_ラインの端末が受け取る(self) -> None:
        lot_plans.send(self.leader, "N7120D0", "L1", terminal="LEADER", sender="cho")
        result = data_sync.write_back(self.leader, self.src)
        self.assertEqual(result.sent.get(config.TBL_LOT_PLAN), 1, result.errors)
        self.assertTrue(lot_plans.refresh_if_changed(self.line, self.src))
        items = lot_plans.for_lines(self.line, ["L-1"])
        self.assertEqual([(p.lot_no, p.line, p.terminal) for p in items],
                         [("N7120D0", "L1", "LEADER")])
        # 変わっていなければ読みに行かない
        self.assertFalse(lot_plans.refresh_if_changed(self.line, self.src))
        # 2度送っても共有には1行
        data_sync.write_back(self.leader, self.src)
        conn = sqlite3.connect(self.src)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{config.TBL_LOT_PLAN}"').fetchone()[0], 1)


# ======================================================================
# 画面
# ======================================================================
class WarehouseCardTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import warehouse as routes
        from packaging_tool import work_context
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        self.conn = _web.bind_db(self, routes, _web.memory_db())
        field = access_control.grant_of("mode:field")
        both = access_control.grant_of("mode:field", "mode:material")
        self.field = _web.make_client(modes.FIELD, port=8713, grant=field)
        self.material = _web.make_client(modes.MATERIAL, port=8723, grant=both)
        patcher = mock.patch("packaging_tool.data_sync.write_back_in_background")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_新規発注の下にカードがある_資材モードには無い(self) -> None:
        html = self.field.get("/warehouse").get_data(as_text=True)
        self.assertIn('id="lotPlanCard"', html)
        self.assertLess(html.index("新規発注"), html.index('id="lotPlanCard"'))
        for name in floor_plan.DEFAULT_BASE_POINTS:
            self.assertIn(f'<option value="{name}"', html)
        self.assertNotIn('id="lotPlanCard"', self.material.get("/warehouse").get_data(as_text=True))

    def test_入れる_外す(self) -> None:
        res = self.field.post("/api/warehouse/lot-plan", json={"lot_no": "N7120D0", "line": "HVC"},
                              headers=_web.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body["plan"]["line"], "HVC")
        self.assertIn("HVC の予定に入れました", body["message"])
        self.assertTrue(body["unsent"])
        got = self.field.get("/api/warehouse/lot-plan?lot_no=N7120D0", headers=_web.auth()).get_json()
        self.assertEqual(got["plan"]["lot_no"], "N7120D0")
        res = self.field.post("/api/warehouse/lot-plan/cancel", json={"lot_no": "N7120D0"},
                              headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.get_json()["plan"])

    def test_ハイフンが違っても選択肢の名前で入れ_選んだ形で出す(self) -> None:
        lot_plans.send(self.conn, "A9", "L-1", terminal="OTHER")       # 前に「L-1」で入った
        got = self.field.get("/api/warehouse/lot-plan?lot_no=A9", headers=_web.auth()).get_json()
        self.assertEqual(got["selected_line"], "L1")
        body = self.field.post("/api/warehouse/lot-plan", json={"lot_no": "A8", "line": "ｈｖｃ"},
                               headers=_web.auth()).get_json()
        self.assertEqual(body["plan"]["line"], "HVC")

    def test_選べないラインは断る(self) -> None:
        res = self.field.post("/api/warehouse/lot-plan", json={"lot_no": "A1", "line": "どこか"},
                              headers=_web.auth())
        self.assertEqual(res.status_code, 400)
        self.assertIsNone(lot_plans.current(self.conn, "A1"))

    def test_資材モードからは入れられない(self) -> None:
        res = self.material.post("/api/warehouse/lot-plan", json={"lot_no": "A1", "line": "L1"},
                                 headers=_web.auth())
        self.assertIn(res.status_code, (403, 404))


class LotPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        import app as app_package
        from app.routes import lot as routes
        from packaging_tool import selection_session, work_context
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        self.conn = _web.bind_db(self, routes, _web.memory_db())
        # レールの印(どの画面でも出す)も同じ接続で
        patcher = mock.patch.object(app_package, "get_db", lambda: self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, ME)
        env.start()
        self.addCleanup(env.stop)
        lot_plans.forget_seen_stamp()
        self.client = _web.make_client(modes.FIELD, ready=False)

    def test_ラインが無ければどこに書けばよいかを言う(self) -> None:
        body = self.client.get("/api/lot/plans", headers=_web.auth()).get_json()
        self.assertEqual(body["lines"], [])
        self.assertIn("ライン名", body["why"])
        self.assertIn(PC, body["why"])

    def test_知らせ_開いて確認_確認だけ(self) -> None:
        access_row(self.conn, line="L1")
        lot_plans.send(self.conn, "A1", "L-1", terminal="LEADER")
        lot_plans.send(self.conn, "A2", "L1", terminal="LEADER")
        lot_plans.send(self.conn, "A3", "HVC", terminal="LEADER")
        html = self.client.get("/lot").get_data(as_text=True)
        self.assertIn('id="planCard"', html)
        self.assertIn("この端末のライン: L1", html)
        self.assertIn("予2", html)                                   # レールの印
        body = self.client.get("/api/lot/plans?refresh=1", headers=_web.auth()).get_json()
        self.assertEqual(body["unseen"], 2)
        self.assertEqual(sorted(i["lot_no"] for i in body["items"]), ["A1", "A2"])
        first = next(i for i in body["items"] if i["lot_no"] == "A1")
        res = self.client.post(f"/api/lot/plans/{first['plan_id']}/seen", json={"opened": True},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        after = res.get_json()
        self.assertEqual(after["unseen"], 1)
        self.assertTrue(next(i for i in after["items"] if i["lot_no"] == "A1")["opened"])
        second = next(i for i in after["items"] if i["lot_no"] == "A2")
        after = self.client.post(f"/api/lot/plans/{second['plan_id']}/seen", json={},
                                 headers=_web.auth()).get_json()
        self.assertEqual(after["unseen"], 0)
        self.assertNotIn("予", self.client.get("/lot").get_data(as_text=True).split('class="rail')[1]
                         .split("</nav>")[0].replace("予定", ""))
        res = self.client.post("/api/lot/plans/nothing/seen", json={}, headers=_web.auth())
        self.assertEqual(res.status_code, 404)

    def test_画面の作り(self) -> None:
        root = Path(__file__).resolve().parent.parent
        view = (root / "app/static/js/lotplans_view.js").read_text(encoding="utf-8")
        self.assertIn("/api/lot/plans?refresh=1", view)
        self.assertIn('label: "開く"', view)
        self.assertIn("確認", view)
        lot_js = (root / "app/static/js/views/lot.js").read_text(encoding="utf-8")
        self.assertIn("startPlans(options.plans, openLot)", lot_js)


class AddBaseTests(unittest.TestCase):
    """配置編集で拠点を足す(マスタに足して図の中央に置く)。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"
        self.addCleanup(lambda: setattr(floor_plan, "USER_PATH", original))
        before = user_settings.get_position()
        self.addCleanup(lambda: user_settings.set_position(before))
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        layout_session.reset_session()
        self.addCleanup(layout_session.reset_session)
        self.session = layout_session.get_session()

    def fake_master(self, ok: bool = True, reason: str = ""):
        """マスタに書いたことにする(書いた行は手元に入れる = 取り込み直したのと同じ)。"""
        from packaging_tool import master_admin
        calls = []

        def add_row(conn, table, values, **_kw):
            calls.append((table, values))
            if not ok:
                return master_admin.Result(False, "管理者パスワードが要ります", reason)
            conn.execute("INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
                         " VALUES (?, ?, ?, ?)", (values["ボード幅"], values["ボード丈"],
                                                  values["ボードタイプ"], values["データラベル"]))
            return master_admin.Result(True, "BoardMasterに1行足しました")
        return mock.patch.object(master_admin, "add_row", side_effect=add_row), calls

    def test_マスタに足して図の中央に置く_ほかの端末にも選択肢として出る(self) -> None:
        self.session.set_editing(True, self.conn)
        patcher, calls = self.fake_master()
        with patcher:
            result = self.session.add_base("拠点大板", self.conn)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(calls, [("BoardMaster", {"ボード幅": "0", "ボード丈": "0",
                                                  "ボードタイプ": "拠点", "データラベル": "拠点大板"})])
        self.assertIn("大板", self.session.plan.base_point_names)
        self.assertIn("大板", svc.master_base_points(self.conn))
        self.assertIn("大板", svc.base_point_names(self.conn))
        self.assertTrue(self.session.set_editing(False, self.conn).ok)      # 終えると保存
        self.assertIn("大板", floor_plan.load().base_point_names)

    def test_既定の拠点ともうある拠点は足さない(self) -> None:
        self.session.set_editing(True, self.conn)
        patcher, calls = self.fake_master()
        with patcher:
            for name in ("L-1", "ＨＶＣ", "lvc"):
                result = self.session.add_base(name, self.conn)
                self.assertFalse(result.ok, name)
                self.assertIn("既定の拠点", result.message)
            self.assertFalse(self.session.add_base("", self.conn).ok)
            self.assertFalse(self.session.add_base("A,B", self.conn).ok)
        self.assertEqual(calls, [])

    def test_マスタに書けなければ図にも置かない(self) -> None:
        self.session.set_editing(True, self.conn)
        patcher, _calls = self.fake_master(ok=False, reason="not_allowed")
        with patcher:
            result = self.session.add_base("大板", self.conn)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, layout_session.REFUSE_NOT_ALLOWED)
        self.assertIn("管理者パスワード", result.message)
        self.assertNotIn("大板", self.session.plan.base_point_names)

    def test_編集中でなければ断る(self) -> None:
        self.assertFalse(self.session.add_base("大板", self.conn).ok)

    def test_既定の拠点はいつも選べる(self) -> None:
        self.assertEqual(floor_plan.DEFAULT_BASE_POINTS, ("L1", "HVC", "LVC"))
        with mock.patch.object(floor_plan, "load", return_value=floor_plan.FloorPlan()):
            self.assertEqual(set(floor_plan.base_points()), {"L1", "HVC", "LVC"})

    def test_画面に欄がある(self) -> None:
        root = Path(__file__).resolve().parent.parent
        html = (root / "app/templates/layout.html").read_text(encoding="utf-8")
        self.assertIn('id="newBase"', html)
        self.assertIn('id="addBase"', html)
        js = (root / "app/static/js/views/layout.js").read_text(encoding="utf-8")
        self.assertIn('"/api/layout/base/add"', js)
        self.assertIn("renderBaseOptions(next.base_points)", js)


if __name__ == "__main__":
    unittest.main()
