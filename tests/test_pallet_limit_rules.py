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


class OrderColumnTests(unittest.TestCase):
    """製造とオーダーの列を分ける(VER4.8.60)。現場の声:「オーダーで見る必要があれば製造を空、製造で見る必要があればオーダーを空」。"""

    def rules(self, **cols) -> tuple:
        conn = make_conn()
        conn.execute(f'DELETE FROM "{TABLE}"')
        names = ["包装仕様NO", "パレット幅", "パレット丈", *cols]
        conn.execute(f'INSERT INTO "{TABLE}" ({", ".join(names)}) VALUES ({", ".join("?" for _ in names)})',
                     ("1P1255", 1100, 2500, *cols.values()))
        conn.commit()
        return pallet_limit_rules.load(conn)

    def on(self, rules, w=0, l=0, order=(0, 0, 0)) -> bool:
        return bool(spk.check_1p1185_mode("1P1255", "", w, l, rules=rules, order_size=order))

    def test_オーダーで見る行_製造は空(self) -> None:
        rules = self.rules(**{"オーダー板厚_最小": 12.0, "オーダー板厚_最大": 12.0,
                              "オーダー板幅_最小": 1075, "オーダー板幅_最大": 1081,
                              "オーダー板丈_最小": 2332, "オーダー板丈_最大": 2338})
        self.assertTrue(self.on(rules, 1080, 2340, (12.0, 1075.0, 2332.0)))
        self.assertTrue(self.on(rules, 0, 0, (12.0, 1081.0, 2338.0)))          # 製造は見ない
        self.assertFalse(self.on(rules, 1080, 2340, (11.9, 1075.0, 2332.0)))   # ｵｰﾀﾞｰ厚が外れる
        self.assertFalse(self.on(rules, 1080, 2340, (12.0, 1074.5, 2332.0)))   # 小数のまま比べる
        self.assertEqual(rules[0].describe(),
                         "1P1255 ｵｰﾀﾞｰ厚 12 / ｵｰﾀﾞｰ幅 1075〜1081 / ｵｰﾀﾞｰ丈 2332〜2338 → 1100×2500")

    def test_製造で見る行_オーダーは空(self) -> None:
        rules = self.rules(**{"製造板幅_最小": 1080, "製造板幅_最大": 1080})
        self.assertTrue(self.on(rules, 1080.9, 0, (0, 0, 0)))                  # 製造は切り捨て
        self.assertFalse(self.on(rules, 1081, 0, (12.0, 1080.0, 0)))

    def test_両方書けば両方かつ(self) -> None:
        rules = self.rules(**{"製造板幅_最小": 1080, "製造板幅_最大": 1080,
                              "オーダー板幅_最小": 1075, "オーダー板幅_最大": 1075})
        self.assertTrue(self.on(rules, 1080, 0, (0, 1075.0, 0)))
        self.assertFalse(self.on(rules, 1080, 0, (0, 1076.0, 0)))
        self.assertFalse(self.on(rules, 1079, 0, (0, 1075.0, 0)))


class OldTableColumnsTests(unittest.TestCase):
    """前の版(VER4.8.58/59)で作った表にも、オーダーの列を足しに行く(手元・梱包資材マスタとも)。"""

    OLD_DDL = (f'CREATE TABLE "{TABLE}" (ID INTEGER PRIMARY KEY AUTOINCREMENT, 包装仕様NO TEXT NOT NULL,'
               " 取引先名称 TEXT NOT NULL DEFAULT '', 製造板幅_最小 REAL, 製造板幅_最大 REAL,"
               " 製造板丈_最小 REAL, 製造板丈_最大 REAL, 業界 TEXT NOT NULL DEFAULT '',"
               " パレット幅 INTEGER NOT NULL, パレット丈 INTEGER NOT NULL, 有効 INTEGER NOT NULL DEFAULT 1,"
               " 並び順 INTEGER NOT NULL DEFAULT 0, 備考 TEXT NOT NULL DEFAULT '',"
               " 更新日時 TEXT NOT NULL DEFAULT '')")

    def test_手元の古い表に列を足す(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(self.OLD_DDL)
        db.apply_schema(conn)
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{TABLE}")')}
        self.assertIn("オーダー板厚_最小", cols)
        self.assertIn("オーダー板丈_最大", cols)

    def test_梱包資材マスタの古い表に列を足して取り込める(self) -> None:
        from packaging_tool import config, data_sync
        d = Path(tempfile.mkdtemp(prefix="oldlimit_"))
        src = d / config.MATERIAL_DB_NAME
        c = sqlite3.connect(src)
        c.execute("CREATE TABLE PalletMaster (管理番号 INTEGER)")
        c.execute(self.OLD_DDL)
        c.execute(f'INSERT INTO "{TABLE}" (包装仕様NO, 取引先名称, パレット幅, パレット丈) VALUES (?,?,?,?)',
                  ("1P9000", "ﾃｽﾄ", 1000, 2000))
        c.commit()
        c.close()
        conn = make_conn()
        got = data_sync.import_master(conn, src)
        self.assertTrue(any("列を足しました" in n for n in got.notes), got.notes)
        c = sqlite3.connect(src)
        self.assertIn("オーダー板幅_最小", {r[1] for r in c.execute(f'PRAGMA table_info("{TABLE}")')})
        c.close()
        rules = pallet_limit_rules.load(conn)
        self.assertEqual([r.spec for r in rules], ["1P9000"])                   # 足した行が取り込まれた
        self.assertTrue(spk.check_1p1185_mode("1P9000", "ｶ)ﾃｽﾄ", 0, 0, rules=rules))
        c = sqlite3.connect(src)
        self.assertIn("納入先", {r[1] for r in c.execute(f'PRAGMA table_info("{TABLE}")')})   # VER4.8.71
        c.close()

    def test_手元の古い表に納入先を足す(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(self.OLD_DDL)
        conn.execute(f'INSERT INTO "{TABLE}" (包装仕様NO, 取引先名称, パレット幅, パレット丈) VALUES (?,?,?,?)',
                     ("1P9000", "ﾃｽﾄ", 1000, 2000))
        db.apply_schema(conn)
        self.assertEqual(conn.execute(f'SELECT 納入先 FROM "{TABLE}"').fetchone()[0], "")  # 空 = 見ない
        self.assertTrue(spk.check_1p1185_mode("1P9000", "ﾃｽﾄ", 0, 0, rules=pallet_limit_rules.load(conn)))


class DeliveryTests(unittest.TestCase):
    """納入先(VER4.8.71)。取引先名称と同じく「含む」。空欄なら見ない。"""

    def rules(self, **cols) -> tuple:
        conn = make_conn()
        self.addCleanup(conn.close)
        conn.execute(f'DELETE FROM "{TABLE}"')
        row = {"包装仕様NO": "1P2000", "パレット幅": 1100, "パレット丈": 1400, **cols}
        conn.execute(f'INSERT INTO "{TABLE}" ({", ".join(row)}) VALUES ({", ".join("?" for _ in row)})',
                     tuple(row.values()))
        conn.commit()
        return pallet_limit_rules.load(conn)

    def test_納入先は含む(self) -> None:
        rules = self.rules(納入先="ﾏﾂｼﾀ")
        on = lambda d: bool(spk.check_1p1185_mode("1P2000", "", 0, 0, rules=rules, delivery_name=d))  # noqa: E731
        self.assertTrue(on("ｶ)ﾏﾂｼﾀｼﾖｳﾃﾝ"))
        self.assertFalse(on("ｶ)ﾃｽﾄ"))
        self.assertFalse(on(""))
        self.assertFalse(on("ｶ)マツシタ"))                  # 全角カナは別の文字
        self.assertIn("納入先 ﾏﾂｼﾀ", rules[0].describe())

    def test_空欄なら見ない_取引先と納入先は両方かつ(self) -> None:
        self.assertTrue(spk.check_1p1185_mode("1P2000", "", 0, 0, rules=self.rules(),
                                              delivery_name="どこでも"))
        both = self.rules(取引先名称="ﾃｽﾄ", 納入先="ﾏﾂｼﾀ")
        self.assertTrue(spk.check_1p1185_mode("1P2000", "ｶ)ﾃｽﾄ", 0, 0, rules=both, delivery_name="ﾏﾂｼﾀ"))
        self.assertFalse(spk.check_1p1185_mode("1P2000", "ｶ)ﾃｽﾄ", 0, 0, rules=both, delivery_name="ﾎｶ"))
        self.assertFalse(spk.check_1p1185_mode("1P2000", "ﾎｶ", 0, 0, rules=both, delivery_name="ﾏﾂｼﾀ"))

    def test_ロットを開くと受注の納入先で判定する(self) -> None:
        from packaging_tool.presenters.selection import SelectionPresenter
        from packaging_tool.user_log import UserLog
        conn = make_conn()
        self.addCleanup(conn.close)
        conn.execute(f'UPDATE "{TABLE}" SET 納入先 = ?', ("ﾏﾂｼﾀ",))
        conn.commit()
        log = UserLog()
        p = SelectionPresenter(conn, user_log=log)
        p.check_and_set_1p1185_mode("1P1185", "ﾅﾒｶﾜｱﾙﾐ", 1245, 1245, delivery_name="ｶ)ﾎｶ")
        self.assertFalse(p.mode_1p1185.is_1p1185)
        p.check_and_set_1p1185_mode("1P1185", "ﾅﾒｶﾜｱﾙﾐ", 1245, 1245, delivery_name="ｶ)ﾏﾂｼﾀ")
        self.assertTrue(p.mode_1p1185.is_1p1185)
        self.assertIn("納入先=ｶ)ﾏﾂｼﾀ", log.text)


class DocExampleWebTests(SelectionWebTestCase):
    """`docs/モード一覧.md` の例: プロテックで、パレットも 1100×2500 に決め打つ(オーダーの列だけで書く)。"""

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
        self.conn.execute(
            f'INSERT INTO "{TABLE}" (包装仕様NO, オーダー板厚_最小, オーダー板厚_最大, オーダー板幅_最小,'
            ' オーダー板幅_最大, オーダー板丈_最小, オーダー板丈_最大, パレット幅, パレット丈)'
            ' VALUES (?,?,?,?,?,?,?,?,?)', ("1P1255", 12, 12, 1075, 1081, 2332, 2338, 1100, 2500))
        self.conn.commit()
        insert_lot(self.conn, **{"オーダー板厚": 12.0, "オーダー板幅": 1076.0, "オーダー板丈": 2335.0,
                                 "製造板幅": 1090.0, "製造板丈": 2350.0})
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100", 包装仕様NO="1P1255")
        self.client.get("/api/lot/1234567", headers=self.auth())

    def test_文書の例どおり(self) -> None:
        self.assertEqual(self.get()["banner"]["text"],
                         "【プロテックボードオーダー選択中】【1P1255モード 1100×2500 限定表示】")
        rows = self.post("/api/selection/pallet/list", {})["rows"]
        self.assertEqual({(r["width"], r["length"]) for r in rows}, {(1100, 2500)})
