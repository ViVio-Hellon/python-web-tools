"""1P1185モード(パレットを決め打つ): 発火条件と決め打つパレットの表(パレット限定条件)の試験

現場の声:「1P1185モードの 包装仕様NO=1P1185・取引先名称に ﾅﾒｶﾜｱﾙﾐ を含む・製造板幅 1242〜1249・製造板丈 1242〜1249
も、マスタ編集で条件行を足すことで発火条件を外から足せるように。発火後のパレットサイズを固定値で決め打つサイズも決めさせて」

【ここで守りたいこと】
1. 初期値は前の決め打ちと同じ(1P1185・ﾅﾒｶﾜｱﾙﾐ・1242〜1249・1242〜1249 → タイト 1300×1300)
2. 1行の中は「かつ」、行と行は「または」。空欄は見ない。製造板幅・丈は整数に切り捨てて比べる(前と同じ)
3. 発火したら、当たった行の 業界(空なら見ない)・パレット幅・丈 のパレットだけ(一覧・自動選定とも)
4. マスタ管理で直せる・取り込み元に作ると初期値が入る・足した行がすぐ効く・最後の1行は消せない
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import db, import_specs, master_common, pallet_limit_rules  # noqa: E402
from packaging_tool import special_packaging as spk  # noqa: E402

TABLE = pallet_limit_rules.TABLE


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


class RuleTests(unittest.TestCase):
    def test_初期値は前の決め打ちと同じ(self) -> None:
        self.assertEqual([r.describe() for r in pallet_limit_rules.DEFAULT_RULES], [
            "1P1185 取引先 ﾅﾒｶﾜｱﾙﾐ / 製造板幅 1242〜1249 / 製造板丈 1242〜1249 → タイト 1300×1300"])
        state = spk.check_1p1185_mode("1P1185", "ｶ)ﾅﾒｶﾜｱﾙﾐ", 1249.9, 1242)   # 切り捨てて 1249
        self.assertTrue(state)
        self.assertEqual((state.industry, state.pallet_width, state.pallet_length), ("タイト", 1300, 1300))

    def test_表に足した行_業界は空なら見ない(self) -> None:
        conn = make_conn()
        conn.execute(f'INSERT INTO "{TABLE}" (包装仕様NO, 取引先名称, 製造板幅_最小, 製造板幅_最大,'
                     ' 業界, パレット幅, パレット丈) VALUES (?,?,?,?,?,?,?)',
                     ("1P2000", "", 900, 1000, "", 1100, 1400))
        conn.commit()
        rules = pallet_limit_rules.load(conn)
        state = spk.check_1p1185_mode("1P2000", "どこでも", 950, 0, rules=rules)   # 丈は見ない
        self.assertTrue(state)
        self.assertEqual(state.pallet_text(), "1100×1400")
        self.assertFalse(spk.reject_1p1185(state, "スカシ", 1100, 1400))          # 業界は見ない
        self.assertTrue(spk.reject_1p1185(state, "タイト", 1300, 1300))
        self.assertFalse(spk.check_1p1185_mode("1P2000", "", 1001, 0, rules=rules))

    def test_パレット寸法の無い行は使わない_有効0は見ない(self) -> None:
        conn = make_conn()
        conn.execute(f'INSERT INTO "{TABLE}" (包装仕様NO, パレット幅, パレット丈) VALUES (?,?,?)',
                     ("1P3000", 0, 0))
        conn.execute(f'INSERT INTO "{TABLE}" (包装仕様NO, パレット幅, パレット丈, 有効) VALUES (?,?,?,?)',
                     ("1P4000", 1000, 1000, 0))
        conn.commit()
        rules = pallet_limit_rules.load(conn)
        self.assertFalse(spk.check_1p1185_mode("1P3000", "", 0, 0, rules=rules))
        self.assertFalse(spk.check_1p1185_mode("1P4000", "", 0, 0, rules=rules))
        self.assertTrue(spk.check_1p1185_mode("1P1185", "ﾅﾒｶﾜｱﾙﾐ", 1245, 1245, rules=rules))

    def test_真偽値で渡してもタイト1300(self) -> None:
        self.assertFalse(spk.reject_1p1185(True, "タイト", 1300, 1300))
        self.assertTrue(spk.reject_1p1185(True, "タイト", 1300, 1400))
        self.assertFalse(spk.reject_1p1185(False, "スカシ", 1, 1))


class ListTests(unittest.TestCase):
    """パレットの一覧・自動選定が、当たった行の寸法だけになる。"""

    def setUp(self) -> None:
        from tests.test_web_selection import insert_pallet
        self.conn = make_conn()
        for w, l, ind in ((1100, 1400, "タイト"), (1300, 1300, "タイト"), (1100, 1400, "スカシ")):
            insert_pallet(self.conn, width=w, length=l, industry=ind, w_min=900, w_max=w,
                          l_min=1000, l_max=l)
        self.conn.execute(f'INSERT INTO "{TABLE}" (包装仕様NO, 業界, パレット幅, パレット丈) VALUES (?,?,?,?)',
                          ("1P2000", "タイト", 1100, 1400))
        self.conn.commit()

    def test_一覧は決め打ちの寸法と業界だけ(self) -> None:
        from packaging_tool import pallet_list
        state = spk.check_1p1185_mode("1P2000", "", 0, 0, rules=pallet_limit_rules.load(self.conn))
        rows = pallet_list.list_pallet_sizes(self.conn, show_all=True, is_1p1185_mode=state)
        self.assertEqual({(r.width, r.length, r.industry) for r in rows}, {(1100, 1400, "タイト")})

    def test_自動選定も同じ(self) -> None:
        from packaging_tool import pallet_auto_select
        from packaging_tool.user_log import UserLog
        log = UserLog()
        state = spk.check_1p1185_mode("1P2000", "", 0, 0, rules=pallet_limit_rules.load(self.conn))
        got = pallet_auto_select.auto_select_pallet(
            self.conn, product_width_text="1000", product_length_text="1300",
            is_1p1185_mode=state, user_log=log)
        self.assertIn("【1P2000モード】タイト 1100×1400 限定で検索します", log.text)
        if got.ok:
            self.assertEqual((got.width, got.length), (1100, 1400))


class MasterTests(unittest.TestCase):
    def setUp(self) -> None:
        from packaging_tool import config, master_admin, selection_session
        self.admin = master_admin
        self.dir = Path(tempfile.mkdtemp(prefix="limit_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        sqlite3.connect(self.src).execute("CREATE TABLE PalletMaster (管理番号 INTEGER)").connection.close()
        self.conn = make_conn()
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        selection_session.get_session(self.conn).admin = True

    def test_直せる表_作ると初期値_足すとすぐ効く_最後の1行は消せない(self) -> None:
        from packaging_tool import master_columns, master_schema
        self.assertIn(TABLE, master_common.BY_TABLE)
        self.assertIn(TABLE, import_specs.OPTIONAL_TABLES)
        self.assertTrue(master_schema.can_create(TABLE))
        cols = {c.name: c for c in master_columns.columns(self.conn, TABLE)}
        self.assertTrue(cols["パレット幅"].required)
        self.assertFalse(cols["取引先名称"].required)
        self.assertIn("含む", cols["取引先名称"].note)

        self.assertTrue(self.admin.create_table(self.conn, TABLE, path=self.src).ok)
        self.assertEqual(len(self.admin.page(self.src, TABLE).rows), 1)
        got = self.admin.add_row(self.conn, TABLE, {
            "包装仕様NO": "1P2000", "取引先名称": "ﾃｽﾄ", "製造板幅_最小": "900", "製造板幅_最大": "1000",
            "業界": "タイト", "パレット幅": "1100", "パレット丈": "1400"}, path=self.src)
        self.assertTrue(got.ok, got.message)
        self.assertIn("次にロットを開いたときから効きます", got.message)
        state = spk.check_1p1185_mode("1P2000", "ｶ)ﾃｽﾄ商事", 950, 0,
                                      rules=pallet_limit_rules.load(self.conn))
        self.assertEqual(state.pallet_text(), "タイト 1100×1400")

        rows = self.admin.page(self.src, TABLE).rows
        self.assertTrue(self.admin.delete_row(self.conn, TABLE, rows[0]["__行"], path=self.src).ok)
        last = self.admin.delete_row(self.conn, TABLE, rows[1]["__行"], path=self.src)
        self.assertFalse(last.ok)
        self.assertIn("有効」を 0", last.message)


class PresenterTests(unittest.TestCase):
    def test_帯の文に決め打ちのパレットが出る(self) -> None:
        from packaging_tool.presenters.selection import SelectionPresenter
        from packaging_tool.user_log import UserLog
        conn = make_conn()
        log = UserLog()
        p = SelectionPresenter(conn, user_log=log)
        p.check_and_set_1p1185_mode("1P1185", "ﾅﾒｶﾜｱﾙﾐ", 1245, 1245)
        self.assertEqual(p.mode_banner().text, "【1P1185モード タイト 1300×1300 限定表示】")
        self.assertIn("条件 1P1185 取引先 ﾅﾒｶﾜｱﾙﾐ", log.text)


if __name__ == "__main__":
    unittest.main()


from tests.test_web_selection import SelectionWebTestCase, insert_pallet  # noqa: E402


class CombinedWebTests(SelectionWebTestCase):
    """プロテック条件 と パレット限定条件 の両方に当たるロット(現場の質問の例)。

    2つの表は**別々に**当たる: プロテックは SIKALOT の**オーダー**厚・幅・丈、パレット限定は
    取引先・**製造**板幅・丈 を見る。両方当たれば、プロテック(ボード)かつ パレットはその寸法だけ。
    """

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        for w, l in ((1100, 2500), (1200, 2600)):
            insert_pallet(self.conn, width=w, length=l, industry="タイト", w_min=900, w_max=w,
                          l_min=2000, l_max=l)
        self.conn.execute(f'INSERT INTO "{TABLE}" (包装仕様NO, 製造板幅_最小, 製造板幅_最大,'
                          ' 製造板丈_最小, 製造板丈_最大, パレット幅, パレット丈) VALUES (?,?,?,?,?,?,?)',
                          ("1P1255", 1080, 1080, 2340, 2340, 1100, 2500))
        self.conn.commit()
        insert_lot(self.conn, **{"オーダー板厚": 12.0, "オーダー板幅": 1075.0, "オーダー板丈": 2332.0,
                                 "製造板幅": 1080.0, "製造板丈": 2340.0})
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100", 包装仕様NO="1P1255")
        self.client.get("/api/lot/1234567", headers=self.auth())

    def test_両方当たればプロテックかつパレットはその寸法だけ(self) -> None:
        presenter = self.session().presenter
        self.assertTrue(presenter.protec.is_protec)                     # オーダー 12 / 1075 / 2332
        self.assertTrue(presenter.mode_1p1185)                          # 製造 1080 / 2340
        state = self.get()
        self.assertEqual(state["banner"]["text"],
                         "【プロテックボードオーダー選択中】【1P1255モード 1100×2500 限定表示】")
        rows = self.post("/api/selection/pallet/list", {})["rows"]
        self.assertEqual({(r["width"], r["length"]) for r in rows}, {(1100, 2500)})
