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
                            import_specs, layout_session, line_names, location_service as svc, lot_plans,
                            master_schema, modes, sync_writeback, user_settings)
from tests import _web  # noqa: E402

PC = "LINE-PC-01"
ME = {access_control.ENV_LOGIN: "worker", access_control.ENV_HOST: PC}


def access_row(conn, *, pc: str = PC, login: str = "", permission: str = "mode:field",
               enabled: int = 1) -> None:
    """アクセス権限の1行。ラインは「権限」の列に正規の呼び名で書く(ほかのツールと同じ)。"""
    conn.execute('INSERT INTO "アクセス権限" (ログインID, PC名, 権限, 有効)'
                 " VALUES (?, ?, ?, ?)", (login, pc, permission, enabled))
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
        again = self.send("N7120D0", "Ｌ－１")              # 全角でも同じライン
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
        self.assertEqual(self.send("", "L-1").reason, lot_plans.REFUSE_NO_LOT)
        self.assertEqual(self.send("A1", " ").reason, lot_plans.REFUSE_NO_LINE)

    def test_この端末のラインの分だけ_未確認を先に(self) -> None:
        self.send("A1", "L-1")
        self.send("A2", "HVC")
        self.send("A3", "Ｌ－１")                           # 全角でも同じライン
        self.send("A4", "L-1")
        lot_plans.cancel(self.conn, "A4", terminal="LEADER")
        items = lot_plans.for_lines(self.conn, ["L-1"])
        self.assertEqual(sorted(p.lot_no for p in items), ["A1", "A3"])
        self.assertEqual(lot_plans.unseen_count(self.conn, ["L-1"]), 2)
        self.assertEqual(lot_plans.for_lines(self.conn, []), [])
        first = lot_plans.current(self.conn, "A1")
        self.assertTrue(lot_plans.mark_seen(self.conn, first.plan_id))
        items = lot_plans.for_lines(self.conn, ["L-1"])
        self.assertEqual([p.lot_no for p in items], ["A3", "A1"])     # 未確認が先
        self.assertEqual(lot_plans.unseen_count(self.conn, ["L-1"]), 1)

    def test_入れ直されたらもう一度知らせる(self) -> None:
        self.send("A1", "L-1")
        lot_plans.mark_seen(self.conn, lot_plans.current(self.conn, "A1").plan_id, opened=True)
        self.assertEqual(lot_plans.unseen_count(self.conn, ["L-1"]), 0)
        self.send("A1", "HVC")
        self.send("A1", "L-1")
        self.assertEqual(lot_plans.unseen_count(self.conn, ["L-1"]), 1)

    def test_古い予定は出さない(self) -> None:
        self.send("A1", "L-1")
        later = lot_plans.datetime.now() + lot_plans.timedelta(days=lot_plans.LIST_DAYS + 1)
        self.assertEqual(lot_plans.for_lines(self.conn, ["L-1"], now=later), [])

    def test_無い予定は確認できない(self) -> None:
        self.assertFalse(lot_plans.mark_seen(self.conn, "nothing"))

    def test_表が無い古い手元DBでも落ちない(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertIsNone(lot_plans.current(conn, "A1"))
        self.assertEqual(lot_plans.for_lines(conn, ["L-1"]), [])


class LineTests(unittest.TestCase):
    """この端末のラインは、アクセス権限の「権限」の列に書いたライン(ほかのツールと同じ読み方)。"""

    def setUp(self) -> None:
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.me = access_control.Identity(login_id="worker", pc_name=PC)

    def test_この端末の行の権限に書いたライン(self) -> None:
        access_row(self.conn, permission="mode:field")
        access_row(self.conn, permission="L-1")
        access_row(self.conn, permission="作業長")                       # ラインではない
        access_row(self.conn, pc="OTHER-PC", permission="HVC")         # ほかの端末
        access_row(self.conn, login="WORKER", pc="", permission="ＬＶＣ、mode:field")
        access_row(self.conn, permission="NS1", enabled=0)              # 無効の行は見ない
        access_row(self.conn, pc="", login="", permission="HVC")       # 条件の無い行は誰にも効かない
        self.assertEqual(access_control.lines(self.conn, self.me), ["L-1", "LVC"])

    def test_古い書き方は読まない(self) -> None:
        """「L1 やら LS やら当時の設定が生きてしまっていて申し訳ないが L1 ではなく L-1 なんです」。"""
        for text in ("L1", "LS", "大板小板", "l-1", "L 1"):
            access_row(self.conn, permission=text)
        self.assertEqual(access_control.lines(self.conn, self.me), [])
        self.assertEqual(line_names.OFFICIAL, ("L-1", "LVC", "HVC", "機側", "AIM", "コイル",
                                               "トット", "バランサー", "NS1"))

    def test_登録が無ければ空(self) -> None:
        self.assertEqual(access_control.lines(self.conn, self.me), [])
        self.assertEqual(access_control.lines(None, self.me), [])

    def test_Administratorは作業長とすべてのライン(self) -> None:
        """「Administrator は作業長権限とラインの権限を併せ持たせてください(テスト用を兼ねてます)」(VER4.8.89)。"""
        access_row(self.conn, permission="Administrator")
        self.assertEqual(access_control.lines(self.conn, self.me), list(line_names.OFFICIAL))
        self.assertTrue(access_control.is_leader(self.conn, self.me))
        self.assertTrue(access_control.is_administrator(self.conn, self.me))
        # 書き方はほかのツールと同じ(全角半角・大文字小文字・前後の空白は問わない。並べてもよい)
        for text in ("administrator", " ＡＤＭＩＮＩＳＴＲＡＴＯＲ ", "AIM、Administrator"):
            self.conn.execute('DELETE FROM "アクセス権限"')
            access_row(self.conn, permission=text)
            self.assertEqual(access_control.lines(self.conn, self.me), list(line_names.OFFICIAL), text)
            self.assertTrue(access_control.is_leader(self.conn, self.me), text)
        # 同じ端末に AIM の行があっても、すべてのライン(重ならない)
        self.conn.execute('DELETE FROM "アクセス権限"')
        access_row(self.conn, permission="Administrator")
        access_row(self.conn, permission="AIM")
        self.assertEqual(access_control.lines(self.conn, self.me), list(line_names.OFFICIAL))

    def test_表ぜんたいの登録ライン(self) -> None:
        """「いまの状態: 登録ラインを出してください」(VER4.8.90)。"""
        access_row(self.conn, pc="PC-A", permission="L-1")
        access_row(self.conn, pc="PC-B", permission="L-1、HVC")
        access_row(self.conn, pc="PC-C", permission="作業長")
        access_row(self.conn, pc="PC-D", permission="Administrator")
        access_row(self.conn, pc="PC-E", permission="NS1", enabled=0)       # 無効は数えない
        table = access_control.registered_lines(self.conn)
        self.assertEqual(table["L-1"], ["PC-A", "PC-B"])
        self.assertEqual(table["HVC"], ["PC-B"])
        self.assertEqual(table["NS1"], [])
        self.assertEqual(table["作業長"], ["PC-C"])
        self.assertEqual(table["Administrator"], ["PC-D"])
        from packaging_tool.presenters import settings as presenter
        with mock.patch.dict(os.environ, ME):
            checks = {c.label: c for c in presenter._access_section(self.conn).checks}
        self.assertEqual(checks["登録ライン"].value, "2 / 9 ライン")
        self.assertIn("L-1: PC-A, PC-B", checks["登録ライン"].detail)
        self.assertIn("NS1: なし", checks["登録ライン"].detail)

    def test_ほかの端末のAdministratorは効かない(self) -> None:
        access_row(self.conn, pc="OTHER-PC", permission="Administrator")
        access_row(self.conn, permission="Administrator", enabled=0)
        access_row(self.conn, permission="Admin")                       # 似ているだけ
        access_row(self.conn, permission="L-1")
        self.assertEqual(access_control.lines(self.conn, self.me), ["L-1"])
        self.assertFalse(access_control.is_leader(self.conn, self.me))
        self.assertFalse(access_control.is_administrator(self.conn, self.me))
        self.assertFalse(access_control.is_administrator(None, self.me))

    def test_Administratorでもモードは変わらない(self) -> None:
        """モード(mode:field / mode:material)は今までどおり。Administrator はモードを足さない。"""
        access_row(self.conn, permission="Administrator")
        access_row(self.conn, permission="mode:field")
        grant = access_control.resolve(self.conn, self.me)
        self.assertEqual(grant.allowed_modes(), (modes.FIELD,))

    def test_アクセス権限に列は足さない(self) -> None:
        """「列足さなくてよくね? ほかのツールものこれで読んでるからいけるはず」(VER4.8.86)。"""
        have = {r[1] for r in self.conn.execute('PRAGMA table_info("アクセス権限")')}
        self.assertEqual(have, {"管理番号", "ログインID", "PC名", "権限", "有効", "備考"})
        self.assertEqual([c for c, _s, _f in import_specs.IMPORT_SPECS["アクセス権限"]],
                         ["ログインID", "PC名", "権限", "有効", "備考"])
        self.assertFalse(hasattr(master_schema, "_add_access_line_column"))


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
        lot_plans.send(self.leader, "N7120D0", "L-1", terminal="LEADER", sender="cho")
        result = data_sync.write_back(self.leader, self.src)
        self.assertEqual(result.sent.get(config.TBL_LOT_PLAN), 1, result.errors)
        self.assertTrue(lot_plans.refresh_if_changed(self.line, self.src))
        items = lot_plans.for_lines(self.line, ["L-1"])
        self.assertEqual([(p.lot_no, p.line, p.terminal) for p in items],
                         [("N7120D0", "L-1", "LEADER")])
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
        env = mock.patch.dict(os.environ, ME)
        env.start()
        self.addCleanup(env.stop)
        # 送れるのは作業長の端末だけ(VER4.8.88)
        access_row(self.conn, permission="作業長")

    def test_作業長でなければ送れない_カードは見るだけ(self) -> None:
        """「作業長だけが作業予定ラインを送れるようにしてください」(VER4.8.88)。"""
        self.conn.execute('DELETE FROM "アクセス権限"')
        access_row(self.conn, permission="L-1")                       # ラインの端末(作業長ではない)
        lot_plans.send(self.conn, "A5", "HVC", terminal="LEADER")
        res = self.field.post("/api/warehouse/lot-plan", json={"lot_no": "A6", "line": "HVC"},
                              headers=_web.auth())
        self.assertEqual(res.status_code, 403)
        self.assertIn("作業長", res.get_json()["error"]["message"])
        self.assertIsNone(lot_plans.current(self.conn, "A6"))
        res = self.field.post("/api/warehouse/lot-plan/cancel", json={"lot_no": "A5"}, headers=_web.auth())
        self.assertEqual(res.status_code, 403)
        self.assertIsNotNone(lot_plans.current(self.conn, "A5"))
        got = self.field.get("/api/warehouse/lot-plan?lot_no=A5", headers=_web.auth()).get_json()
        self.assertFalse(got["can_send"])
        self.assertEqual(got["plan"]["line"], "HVC")                   # 見ることはできる
        html = self.field.get("/warehouse").get_data(as_text=True)
        self.assertIn('id="lotPlanSend" disabled', html)

    def test_Administratorは送れる(self) -> None:
        self.conn.execute('DELETE FROM "アクセス権限"')
        access_row(self.conn, permission="Administrator")
        res = self.field.post("/api/warehouse/lot-plan", json={"lot_no": "A7", "line": "NS1"},
                              headers=_web.auth())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(lot_plans.current(self.conn, "A7").line, "NS1")
        self.assertTrue(self.field.get("/api/warehouse/lot-plan?lot_no=A7",
                                       headers=_web.auth()).get_json()["can_send"])

    def test_作業長の書き方(self) -> None:
        me = access_control.Identity(login_id="worker", pc_name=PC)
        self.assertTrue(access_control.is_leader(self.conn, me))
        self.conn.execute('DELETE FROM "アクセス権限"')
        access_row(self.conn, permission="L-1、作業長")                 # 並べて書いてもよい
        self.assertTrue(access_control.is_leader(self.conn, me))
        self.conn.execute('DELETE FROM "アクセス権限"')
        access_row(self.conn, pc="OTHER-PC", permission="作業長")       # ほかの端末の作業長
        access_row(self.conn, permission="作業長", enabled=0)           # 無効
        self.assertFalse(access_control.is_leader(self.conn, me))
        self.assertFalse(access_control.is_leader(None, me))

    def test_新規発注の下にカードがある_資材モードには無い(self) -> None:
        html = self.field.get("/warehouse").get_data(as_text=True)
        self.assertIn('id="lotPlanCard"', html)
        self.assertLess(html.index("新規発注"), html.index('id="lotPlanCard"'))
        for name in line_names.OFFICIAL:                               # 選べるのは正規の呼び名
            self.assertIn(f'<option value="{name}"', html)
        self.assertNotIn('<option value="L1"', html)
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

    def test_全角でも選択肢の名前で入れ_選んだ形で出す(self) -> None:
        lot_plans.send(self.conn, "A9", "Ｌ-1", terminal="OTHER")       # 全角で入った
        got = self.field.get("/api/warehouse/lot-plan?lot_no=A9", headers=_web.auth()).get_json()
        self.assertEqual(got["selected_line"], "L-1")
        body = self.field.post("/api/warehouse/lot-plan", json={"lot_no": "A8", "line": "ＨＶＣ"},
                               headers=_web.auth()).get_json()
        self.assertEqual(body["plan"]["line"], "HVC")
        res = self.field.post("/api/warehouse/lot-plan", json={"lot_no": "A7", "line": "L1"},
                              headers=_web.auth())
        self.assertEqual(res.status_code, 400)                          # 古い書き方は選べない

    def test_選べないラインは断る(self) -> None:
        res = self.field.post("/api/warehouse/lot-plan", json={"lot_no": "A1", "line": "どこか"},
                              headers=_web.auth())
        self.assertEqual(res.status_code, 400)
        self.assertIsNone(lot_plans.current(self.conn, "A1"))

    def test_資材モードからは入れられない(self) -> None:
        res = self.material.post("/api/warehouse/lot-plan", json={"lot_no": "A1", "line": "L-1"},
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

    def test_Administratorにはどのラインの知らせも届く(self) -> None:
        access_row(self.conn, permission="Administrator")
        for lot, line in (("B1", "L-1"), ("B2", "NS1"), ("B3", "バランサー")):
            lot_plans.send(self.conn, lot, line, terminal="LEADER")
        body = self.client.get("/api/lot/plans", headers=_web.auth()).get_json()
        self.assertEqual(body["lines"], list(line_names.OFFICIAL))
        self.assertEqual(sorted(i["lot_no"] for i in body["items"]), ["B1", "B2", "B3"])
        self.assertEqual(body["unseen"], 3)

    def test_いまの状態は強いもの優先(self) -> None:
        """「未登録になる。読んでいないのでは?」「2つ以上付与していたら強いもの優先」(VER4.8.90)。"""
        def label():
            return self.client.get("/api/lot/plans", headers=_web.auth()).get_json()
        self.assertEqual(label()["label"], "この端末のライン: 未登録")
        access_row(self.conn, permission="L-1")
        self.assertEqual(label()["label"], "この端末のライン: L-1")
        access_row(self.conn, permission="作業長")
        self.assertEqual(label()["label"], "この端末: 作業長 / ライン L-1")
        access_row(self.conn, permission="Administrator")
        body = label()
        self.assertEqual(body["label"], "この端末: Administrator(作業長・すべてのライン)")
        self.assertEqual(body["role"], "admin")
        # 作業長だけ(ラインなし)は「未登録」と言わない
        self.conn.execute('DELETE FROM "アクセス権限"')
        access_row(self.conn, permission="作業長")
        body = label()
        self.assertEqual(body["label"], "この端末: 作業長(ラインなし)")
        self.assertIn("作業長の端末です", body["why"])
        html = self.client.get("/lot").get_data(as_text=True)
        self.assertIn("この端末: 作業長(ラインなし)", html)

    def test_ログインIDが違う行はなぜ効かないかを言う(self) -> None:
        """実データ: どの行にもログインID(Xfa-Ngykonpo)が入っている。別のIDでログインすると当たらない。"""
        access_row(self.conn, login="Xfa-Ngykonpo", permission="L-1")
        body = self.client.get("/api/lot/plans", headers=_web.auth()).get_json()
        self.assertEqual(body["label"], "この端末のライン: 未登録")
        self.assertIn("Xfa-Ngykonpo", body["why"])
        self.assertIn("worker", body["why"])
        self.assertIn("「L-1」", body["why"])

    def test_ラインが無ければどこに書けばよいかを言う(self) -> None:
        body = self.client.get("/api/lot/plans", headers=_web.auth()).get_json()
        self.assertEqual(body["lines"], [])
        self.assertIn("権限", body["why"])
        self.assertIn("L-1", body["why"])
        self.assertIn(PC, body["why"])

    def test_知らせ_開いて確認_確認だけ(self) -> None:
        access_row(self.conn, permission="L-1")
        lot_plans.send(self.conn, "A1", "L-1", terminal="LEADER")
        lot_plans.send(self.conn, "A2", "Ｌ-1", terminal="LEADER")
        lot_plans.send(self.conn, "A3", "HVC", terminal="LEADER")
        html = self.client.get("/lot").get_data(as_text=True)
        self.assertIn('id="planCard"', html)
        self.assertIn("この端末のライン: L-1", html)
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
        self.assertEqual(floor_plan.DEFAULT_BASE_POINTS, ("L-1", "HVC", "LVC"))
        with mock.patch.object(floor_plan, "load", return_value=floor_plan.FloorPlan()):
            self.assertEqual(set(floor_plan.base_points()), {"L-1", "HVC", "LVC"})


class BaseL1RenameTests(unittest.TestCase):
    """拠点の表示を L-1 にそろえた(VER4.8.87)。前の版で保存した図・設定の「L1」もそのまま使える。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"
        self.addCleanup(lambda: setattr(floor_plan, "USER_PATH", original))
        before = user_settings.get(user_settings.KEY_POSITION)
        self.addCleanup(lambda: user_settings.save(user_settings.KEY_POSITION, before or ""))

    def test_前に保存した図のL1はL_1として読み_座標も距離も変わらない(self) -> None:
        plan = floor_plan.load()
        data = plan.to_dict()
        for base in data["base_points"]:
            if base["name"] == "L-1":
                base["name"] = "L1"                                # 前の版で保存した図
        from packaging_tool import map_data
        map_data.write_json(floor_plan.USER_PATH, data)
        loaded = floor_plan.load()
        self.assertIn("L-1", loaded.base_point_names)
        self.assertNotIn("L1", loaded.base_point_names)
        self.assertEqual((loaded.base_point("L-1").x, loaded.base_point("L-1").y),
                         (plan.base_point("L-1").x, plan.base_point("L-1").y))
        item = loaded.item_names[0]
        self.assertEqual(loaded.distance(item, "L1"), plan.distance(item, "L-1"))

    def test_前に保存した設定のL1はL_1として読む(self) -> None:
        user_settings.set_position("L1")
        self.assertEqual(user_settings.get_position(), "L-1")
        self.assertEqual(user_settings.get_position_label(), "L-1")
        self.assertTrue(svc.current_base_point().known)
        self.assertEqual(user_settings.DEFAULT_POSITION, "L-1")

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
