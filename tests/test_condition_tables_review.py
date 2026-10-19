"""プロテック条件・パレット限定条件の通し点検(VER4.8.75)

現場の声:「プロテック条件とパレット限定条件もう一回見直せって。何回手直しするんだよ。
正しく反映されるか、抜けがないか、モードに反映されるか」

画面と同じ流れで確かめる:

    マスタ管理で書く(梱包資材マスタ = 取り込み元)→ 手元へ取り込む → ロットを開く(/api/lot)
    → モード(プロテック・パレット限定)・帯・ボード種別・パレット一覧・パレットの自動選定

1. 初期値どおりに効く(プロテック 1P1216・1P1255、パレット限定 1P1185)
2. 1行足す・直す(有効 0)・消す が、ロットを開き直すと効く/効かなくなる
3. 列ごとの条件(包装仕様NOの大小文字・空白、寸法の最小だけ/最大だけ、0 = 見ない、取引先名称・納入先名称は含む)
4. 行どうしは「または」、パレット限定は並び順の小さい行が先
5. 業界が空の行は寸法だけで絞る(業界を書けばその業界だけ)
6. オーダーを選び直すと、そのオーダーの取引先・納入先・包装仕様NOで決め直す / 別のロットを開くと消える
7. 2つの表は別々に当たる(両方当たれば帯は2つとも出す)
8. 空欄の行が足せる・ID が空にならない
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import config, master_admin, master_schema, pallet_limit_rules, protec_rules  # noqa: E402
from packaging_tool.presenters.selection import PALLET_COLUMNS  # noqa: E402

from tests.test_web_selection import SelectionWebTestCase, insert_pallet  # noqa: E402

PL = pallet_limit_rules.TABLE
PR = protec_rules.TABLE
INDUSTRY = [key for _label, key, _n in PALLET_COLUMNS].index("industry")


class ReviewTests(SelectionWebTestCase):

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        # 梱包資材マスタ(取り込み元)。条件の表は「取り込み元に作る」で初期値入り
        self.src = Path(tempfile.mkdtemp(prefix="review_")) / config.MATERIAL_DB_NAME
        sqlite3.connect(self.src).execute("CREATE TABLE PalletMaster (管理番号 INTEGER)").connection.close()
        self.session().admin = True
        for table in (PL, PR):
            got = master_admin.create_table(self.conn, table, path=self.src)
            self.assertTrue(got.ok, got.message)
        for w, l, ind in ((1300, 1300, "タイト"), (1300, 1300, "スカシ"), (1100, 2500, "タイト"),
                          (1100, 2500, "スカシ"), (1200, 2600, "タイト")):
            insert_pallet(self.conn, width=w, length=l, industry=ind, w_min=800, w_max=w,
                          l_min=1000, l_max=l)
        self.conn.executemany(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル) VALUES (?,?,?,'')",
            [(1000, 2000, "ハードボード"), (1000, 2000, "プロテックボード")])
        self.conn.commit()
        self.lots = 0

    # --- 道具 -----------------------------------------------------------
    def add(self, table: str, **values) -> None:
        got = master_admin.add_row(self.conn, table, {k: str(v) for k, v in values.items()}, path=self.src)
        self.assertTrue(got.ok, got.message)

    def rows(self, table: str) -> list[dict]:
        return master_admin.page(self.src, table).rows

    def open_lot(self, spec: str, *, customer: str = "B商事", delivery: str = "A社",
                 made=(1000.0, 2000.0), order=(3.2, 1010.0, 2010.0), orders=None) -> dict:
        """ロットを作って開く(画面の「ロットを開く」と同じ API)。"""
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        self.lots += 1
        lot_no = f"77{self.lots:05d}"
        insert_lot(self.conn, lot_no=lot_no, **{
            "製造板幅": made[0], "製造板丈": made[1],
            "オーダー板厚": order[0], "オーダー板幅": order[1], "オーダー板丈": order[2]})
        for i, (o_spec, o_customer, o_delivery) in enumerate(orders or [(spec, customer, delivery)]):
            order_no = f"{lot_no}-{i}"
            insert_hiki(self.conn, lot_no=lot_no, order_no=order_no, qty=10.0, no=f"{lot_no}{i}")
            insert_odr(self.conn, order_no=order_no, 包装仕様NO=o_spec, 取引先名称=o_customer,
                       納入先名称=o_delivery)
        res = self.client.get(f"/api/lot/{lot_no}", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.lot_no = lot_no
        return self.get()

    def protec(self) -> bool:
        return self.session().presenter.protec.is_protec

    def limit(self):
        return self.session().presenter.mode_1p1185

    def pallets(self) -> set:
        rows = self.post("/api/selection/pallet/list", {})["rows"]
        return {(r["width"], r["length"], r["values"][INDUSTRY]) for r in rows}

    # --- 1. 初期値 -------------------------------------------------------
    def test_初期値_プロテック1P1216はボード種別もプロテックボード(self) -> None:
        state = self.open_lot("1P1216")
        self.assertTrue(self.protec())
        self.assertEqual(self.session().board_type, "プロテックボード")
        self.assertIn("プロテックボードオーダー選択中", state["banner"]["text"])

    def test_初期値_プロテック1P1255はオーダー寸法が当たるときだけ(self) -> None:
        self.open_lot("1P1255", order=(12.0, 1076.0, 2335.0))
        self.assertTrue(self.protec())
        self.open_lot("1P1255", order=(12.0, 1090.0, 2335.0))         # 幅が外れる
        self.assertFalse(self.protec())
        self.assertEqual(self.session().board_type, "ハードボード")
        self.assertIn("ｵｰﾀﾞｰ寸法が当たりません", self.session().presenter.user_log.text)

    def test_初期値_パレット限定1P1185(self) -> None:
        state = self.open_lot("1P1185", customer="ｶ)ﾅﾒｶﾜｱﾙﾐ", made=(1245.0, 1249.0))
        self.assertTrue(self.limit())
        self.assertEqual(state["banner"]["text"], "【1P1185モード タイト 1300×1300 限定表示】")
        self.assertEqual(self.pallets(), {(1300, 1300, "タイト")})
        # 製造板幅は整数に切り捨てて比べる(1249.9 → 1249 で当たる、1250 は外れる)
        self.open_lot("1P1185", customer="ｶ)ﾅﾒｶﾜｱﾙﾐ", made=(1249.9, 1245.0))
        self.assertTrue(self.limit())
        self.open_lot("1P1185", customer="ｶ)ﾅﾒｶﾜｱﾙﾐ", made=(1250.0, 1245.0))
        self.assertFalse(self.limit())
        self.open_lot("1P1185", customer="ﾎｶ", made=(1245.0, 1245.0))   # 取引先が外れる
        self.assertFalse(self.limit())

    def test_パレットの自動選定も限定のパレットだけ(self) -> None:
        self.open_lot("1P1185", customer="ｶ)ﾅﾒｶﾜｱﾙﾐ", made=(1245.0, 1245.0))
        state = self.post("/api/selection/pallet/search",
                          {"product_width": "1200", "product_length": "1250"})
        rows = state.get("rows") or []
        self.assertTrue(rows)
        self.assertEqual({(r["width"], r["length"]) for r in rows}, {(1300, 1300)})

    # --- 2. 足す・直す・消す ----------------------------------------------
    def test_足した行が効く_有効0で止まる_消すと効かない(self) -> None:
        self.add(PR, 包装仕様NO="1p 3000", オーダー厚_最小=5, オーダー厚_最大=10)  # 小文字・空白でも 1P3000
        self.open_lot("1P3000", order=(6.0, 1000.0, 2000.0))
        self.assertTrue(self.protec())
        row = [r for r in self.rows(PR) if r["包装仕様NO"] == "1p 3000"][0]
        self.assertTrue(master_admin.save_row(self.conn, PR, row["__行"], {"有効": "0"}, path=self.src).ok)
        self.open_lot("1P3000", order=(6.0, 1000.0, 2000.0))
        self.assertFalse(self.protec())
        self.assertTrue(master_admin.save_row(self.conn, PR, row["__行"], {"有効": "1"}, path=self.src).ok)
        self.open_lot("1P3000", order=(6.0, 1000.0, 2000.0))
        self.assertTrue(self.protec())
        self.assertTrue(master_admin.delete_row(self.conn, PR, row["__行"], path=self.src).ok)
        self.open_lot("1P3000", order=(6.0, 1000.0, 2000.0))
        self.assertFalse(self.protec())

    def test_パレット限定の全部の列(self) -> None:
        """包装仕様NO・取引先名称・納入先名称・製造板幅/丈・オーダー板厚/幅/丈・業界・パレット幅/丈。"""
        self.add(PL, 包装仕様NO="1P4000", 取引先名称="ﾃｽﾄ", 納入先名称="ﾏﾂｼﾀ",
                 製造板幅_最小=990, 製造板幅_最大=1010, 製造板丈_最小=1990, 製造板丈_最大=2010,
                 オーダー板厚_最小=3, オーダー板厚_最大=4, オーダー板幅_最小=1000, オーダー板幅_最大=1020,
                 オーダー板丈_最小=2000, オーダー板丈_最大=2020, 業界="スカシ", パレット幅=1100, パレット丈=2500)
        good = dict(customer="ｶ)ﾃｽﾄ商事", delivery="ｶ)ﾏﾂｼﾀｼﾖｳﾃﾝ", made=(1000.0, 2000.0),
                    order=(3.2, 1010.0, 2010.0))
        state = self.open_lot("1P4000", **good)
        self.assertTrue(self.limit())
        self.assertEqual(state["banner"]["text"], "【1P4000モード スカシ 1100×2500 限定表示】")
        self.assertEqual(self.pallets(), {(1100, 2500, "スカシ")})
        for name, change in (("取引先", {"customer": "ﾎｶ"}), ("納入先", {"delivery": "ﾎｶ"}),
                             ("製造板幅", {"made": (1020.0, 2000.0)}), ("製造板丈", {"made": (1000.0, 1980.0)}),
                             ("ｵｰﾀﾞｰ厚", {"order": (5.0, 1010.0, 2010.0)}),
                             ("ｵｰﾀﾞｰ幅", {"order": (3.2, 1030.0, 2010.0)}),
                             ("ｵｰﾀﾞｰ丈", {"order": (3.2, 1010.0, 2030.0)})):
            self.open_lot("1P4000", **{**good, **change})
            self.assertFalse(self.limit(), name)

    def test_最小だけ_最大だけ_0は見ない(self) -> None:
        self.add(PR, 包装仕様NO="1P5000", オーダー幅_最小=1000, オーダー丈_最大=2000, オーダー厚_最小=0)
        self.open_lot("1P5000", order=(1.0, 1500.0, 1500.0))
        self.assertTrue(self.protec())
        self.open_lot("1P5000", order=(1.0, 999.0, 1500.0))
        self.assertFalse(self.protec())
        self.open_lot("1P5000", order=(1.0, 1500.0, 2001.0))
        self.assertFalse(self.protec())

    # --- 4. 行どうし ------------------------------------------------------
    def test_行どうしはまたは_パレット限定は並び順の小さい行(self) -> None:
        self.add(PL, 包装仕様NO="1P6000", 製造板幅_最小=900, 製造板幅_最大=1100,
                 パレット幅=1200, パレット丈=2600, 並び順=50)
        self.add(PL, 包装仕様NO="1P6000", 製造板幅_最小=900, 製造板幅_最大=1100, 業界="タイト",
                 パレット幅=1100, パレット丈=2500, 並び順=20)
        self.open_lot("1P6000", made=(1000.0, 2000.0))
        self.assertEqual(self.limit().pallet_text(), "タイト 1100×2500")
        self.open_lot("1P6000", made=(1200.0, 2000.0))                 # どちらにも当たらない
        self.assertFalse(self.limit())

    # --- 5. 業界 ----------------------------------------------------------
    def test_業界が空なら寸法だけ_書けばその業界だけ(self) -> None:
        self.add(PL, 包装仕様NO="1P7000", パレット幅=1100, パレット丈=2500)
        self.open_lot("1P7000")
        self.assertEqual(self.pallets(), {(1100, 2500, "タイト"), (1100, 2500, "スカシ")})

    # --- 6. オーダー・ロットを替える ---------------------------------------
    def test_オーダーを選び直すとそのオーダーで決め直す_別のロットで消える(self) -> None:
        self.add(PL, 包装仕様NO="1P8000", 納入先名称="ﾏﾂｼﾀ", パレット幅=1100, パレット丈=2500)
        self.open_lot("", orders=[("1P0001", "B商事", "ﾎｶ"), ("1P8000", "B商事", "ｶ)ﾏﾂｼﾀ")])
        self.assertFalse(self.limit())                                  # 先頭のオーダー
        res = self.client.get(f"/api/lot/{self.lot_no}/hiki/{self.lot_no}1?pick=1", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.limit())                                   # 選んだオーダー
        self.client.get(f"/api/lot/{self.lot_no}/hiki/{self.lot_no}0?pick=1", headers=self.auth())
        self.assertFalse(self.limit())
        self.open_lot("1P1216")
        self.assertFalse(self.limit())
        self.assertTrue(self.protec())
        self.open_lot("1P0001")
        self.assertFalse(self.protec())
        self.assertEqual(self.session().board_type, "ハードボード")

    # --- 7. 両方 ----------------------------------------------------------
    def test_両方当たれば帯は2つとも(self) -> None:
        self.add(PL, 包装仕様NO="1P1216", パレット幅=1200, パレット丈=2600)
        state = self.open_lot("1P1216")
        self.assertTrue(self.protec())
        self.assertTrue(self.limit())
        self.assertEqual(state["banner"]["text"],
                         "【プロテックボードオーダー選択中】【1P1216モード 1200×2600 限定表示】")

    # --- 8. 表の作り --------------------------------------------------------
    def test_空欄の行を足せる_IDは空にならない_パレット寸法は必須(self) -> None:
        self.add(PL, 包装仕様NO="1P9000", パレット幅=1100, パレット丈=2500)
        self.add(PR, 包装仕様NO="1P9000")
        for table in (PL, PR):
            self.assertTrue(all(str(r.get("ID") or "").strip() for r in self.rows(table)), table)
        got = master_admin.add_row(self.conn, PL, {"包装仕様NO": "1P9001"}, path=self.src)
        self.assertFalse(got.ok)
        self.assertIn("空にできません", got.message)

    def test_IDが自動で入らない作りの表でも空にしない(self) -> None:
        """Access から写した表などは ID が INTEGER PRIMARY KEY でない。足した行・取り込み時に埋める。"""
        with sqlite3.connect(self.src) as raw:
            raw.execute(f'ALTER TABLE "{PL}" RENAME TO 旧')
            raw.execute(f'CREATE TABLE "{PL}" AS SELECT * FROM 旧')        # ID はただの列になる
            raw.execute("DROP TABLE 旧")
            raw.execute(f'UPDATE "{PL}" SET ID = NULL')
        raw.close()
        self.assertTrue(master_schema.plain_id_column(
            [dict(zip(("cid", "name", "type", "notnull", "dflt_value", "pk"), r))
             for r in sqlite3.connect(self.src).execute(f'PRAGMA table_info("{PL}")')]))
        self.add(PL, 包装仕様NO="1P9100", パレット幅=1100, パレット丈=2500)
        ids = [r["ID"] for r in self.rows(PL)]
        self.assertTrue(all(i not in (None, "") for i in ids), ids)
        self.assertEqual(len(set(ids)), len(ids))


class AccessRefreshTests(unittest.TestCase):
    def test_Accessの最新に入れ替えても条件の表は上書きしない(self) -> None:
        """Access に同じ名前の表があると、入れ替えでマスタ管理の直しが消えていた(VER4.8.75)。"""
        from packaging_tool import kanban_shelves, table_bring
        found = type("C", (), {"suspect": 0, "columns": ["ID"]})()
        for table in (PL, PR, kanban_shelves.TABLE):
            self.assertIn("条件の表", table_bring.refresh_why(table, found), table)
        self.assertEqual(table_bring.refresh_why("BoardMaster", found), "")


if __name__ == "__main__":
    unittest.main()
