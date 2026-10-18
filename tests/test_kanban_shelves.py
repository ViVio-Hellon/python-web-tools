"""在庫は置き場ごとに見る: 看板と置き場の対応(表 看板置き場)の試験(VER4.8.68)

現場の声:「ライン毎ではなく置き場の関係性なので(拠点と近いか遠いか置き場が複数あるか)。
置き場が複数あって(1030*1520など)看板がライン毎にあったとしても、L-1でだされたとしても
LVCは出していないであればLVCにとりにいくことになる」
「ハードボード(大板棚)は ハードボード です。置き場が大板ですが看板は L-1 でだします」

【ここで守りたいこと】
1. 看板の1行は、その資材が**その看板の置き場に置いてあるとき**だけ、その置き場の在庫を言う
2. 欲 = 〇 の置き場は切れている。疲労度の距離は切れている置き場を外して測る(在庫考慮ONのとき)
3. 置いてある置き場が**全部**切れているときだけ在庫薄
4. 「ハードボード(大板棚)」は資材ハードボード・置き場は大板(14・15)
5. 対応が当たらない板(細板の 4 番など)は前と同じ(看板全体で判断)
6. 対応はマスタ管理で直せる(取り込み元に作ると初期値・最後の1行は消せない)
7. 資材選択のログ・棚検索の「まとめて見る」も同じ置き場を外す
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import (board_scoring, import_specs, kanban_shelves,  # noqa: E402
                            layout_session, location_service, master_common,
                            material_service, user_settings, work_context)
from packaging_tool.presenters.selection import SelectionPresenter  # noqa: E402
from packaging_tool.user_log import UserLog  # noqa: E402

from tests.test_cut_vs_carry import _RealDataCase  # noqa: E402

TABLE = kanban_shelves.TABLE
HB = "ハードボード"


class SplitTests(unittest.TestCase):

    def test_資材名の後ろの括弧は置き場の指定(self):
        self.assertEqual(kanban_shelves.split_material("ハードボード(大板棚)"), (HB, "大板棚"))
        self.assertEqual(kanban_shelves.split_material("ハードボード（大板棚）"), (HB, "大板棚"))
        self.assertEqual(kanban_shelves.split_material(HB), (HB, ""))
        self.assertEqual(kanban_shelves.split_material(""), ("", ""))

    def test_初期値は現場で了承した対応(self):
        rules = kanban_shelves.DEFAULT_RULES

        def nums(kanban, note=""):
            return sorted(int(s.replace("lblItem", ""))
                          for s in kanban_shelves.shelves_of(rules, kanban, note))
        self.assertEqual(nums("看板_L1"), [1, 2, 3, 30, 31])
        self.assertEqual(nums("看板_LVC"), [17, 18, 19, 32])
        self.assertEqual(nums("看板_HVC"), [7, 8, 35])
        self.assertEqual(nums("看板_大板小板"), [9, 11, 12, 21, 22, 23])
        self.assertEqual(nums("看板_L1", "大板棚"), [14, 15])
        self.assertIsNone(kanban_shelves.shelves_of(rules, "看板_AIM", ""))


class _KanbanCase(_RealDataCase):
    """実データの写し(BoardMaster)に看板の行を足す。"""

    def kanban(self, table: str, material: str, size: str, wanted: bool) -> None:
        self.conn.execute(f'INSERT INTO "{table}" (資材, サイズ, 欲, 不) VALUES (?,?,?,?)',
                          (material, size, "〇" if wanted else "", "" if wanted else "〇"))
        self.conn.commit()

    def all_in_stock_1030(self, *, l1_wanted: bool) -> None:
        """1030x1520 は 1・7・19・23 番。L1 だけ看板を出した(欲)。"""
        self.kanban("看板_L1", HB, "1030 × 1520 : 100枚", l1_wanted)
        self.kanban("看板_HVC", HB, "1030 × 1520 : 100枚", False)
        self.kanban("看板_LVC", HB, "1030 × 1520 : 100枚", False)
        self.kanban("看板_大板小板", HB, "1030 × 1520 : 100枚", False)

    def stock(self) -> material_service.BoardStockMap:
        return material_service.build_board_stock_map(self.conn)


class ShelfStockTests(_KanbanCase):

    def test_L1が看板を出してもほかの置き場にあれば在庫薄ではない_L1の置き場だけ外す(self):
        self.all_in_stock_1030(l1_wanted=True)
        stock = self.stock()
        self.assertFalse(material_service.is_board_low(stock, HB, 1030, 1520))
        self.assertEqual(stock.out_for(HB, 1030, 1520), frozenset({"lblItem1"}))
        self.assertEqual(stock.out_for(HB, 1520, 1030), frozenset({"lblItem1"}))   # 向きは問わない

    def test_置いてある置き場が全部切れたら在庫薄(self):
        for table in ("看板_L1", "看板_HVC", "看板_LVC", "看板_大板小板"):
            self.kanban(table, HB, "1030 × 1520 : 100枚", True)
        self.assertTrue(material_service.is_board_low(self.stock(), HB, 1030, 1520))

    def test_ほかのラインに在庫ありでも_その板の置き場が切れていれば在庫薄(self):
        """全ライン合わせると「どこかに不があれば在庫あり」だった。1000x1600 は 23 番だけ。"""
        self.kanban("看板_大板小板", HB, "1000 × 1600 : 100枚", True)
        self.kanban("看板_L1", HB, "1000 × 1600 : 100枚", False)       # L1 の置き場には無い板
        self.assertTrue(material_service.is_board_low(self.stock(), HB, 1000, 1600))

    def test_大板棚はL1の看板で大板の置き場(self):
        """1000x1200 は 15 番(大板)だけ。L1 の(大板棚)が欲なら、大板小板の看板が不でも在庫薄。"""
        self.kanban("看板_L1", "ハードボード(大板棚)", "1000 × 1200 : 100枚", True)
        self.kanban("看板_大板小板", HB, "1000 × 1200 : 100枚", False)
        stock = self.stock()
        self.assertTrue(material_service.is_board_low(stock, HB, 1000, 1200))
        self.assertIn("看板_大板小板 ハードボード 1000×1200", stock.unplaced)
        # 660x1050 は 9(中板・小板)と 15(大板)。大板棚が切れても 9 にある
        self.kanban("看板_L1", "ハードボード(大板棚)", "660 × 1050 : 100枚", True)
        self.kanban("看板_大板小板", HB, "660 × 1050 : 100枚", False)
        stock = self.stock()
        self.assertFalse(material_service.is_board_low(stock, HB, 660, 1050))
        self.assertEqual(stock.out_for(HB, 660, 1050), frozenset({"lblItem15"}))

    def test_対応が当たらない板は前と同じ_看板全体で判断(self):
        """50x1600 は 4 番(どの看板の置き場でもない)。L1 で欲・ほかに不が無い → 在庫薄。"""
        self.kanban("看板_L1", HB, "50 × 1600 : 50枚", True)
        stock = self.stock()
        self.assertTrue(material_service.is_board_low(stock, HB, 50, 1600))
        self.assertNotIn(f"{HB}|50|1600", stock.shelf_based)
        self.kanban("看板_HVC", HB, "50 × 1600 : 50枚", False)
        self.assertFalse(material_service.is_board_low(self.stock(), HB, 50, 1600))

    def test_疲労度の距離は切れている置き場を外して測る(self):
        """L1 から 1030x1520 の最寄りは 1 番。1 番の看板が出ていれば、次に近い置き場まで歩く。"""
        self.all_in_stock_1030(l1_wanted=True)
        stock = self.stock()
        plain = location_service.find_nearest_shelf_for_board(self.conn, 1030, 1520, "L1", HB)
        self.assertEqual(plain.shelf_name, "lblItem1")
        skipped = location_service.find_nearest_shelf_for_board(
            self.conn, 1030, 1520, "L1", HB, skip=stock.out_for(HB, 1030, 1520))
        self.assertNotEqual(skipped.shelf_name, "lblItem1")
        self.assertEqual(skipped.skipped, ("lblItem1",))
        self.assertGreater(skipped.distance, plain.distance)

        board = type("B", (), {"width": 1030, "length": 1520})()
        off = board_scoring.build_fatigue_map(self.conn, [board], "L1", board_type=HB)
        on = board_scoring.build_fatigue_map(self.conn, [board], "L1", board_type=HB, stock_map=stock)
        self.assertGreater(on["1030x1520"].dist, off["1030x1520"].dist)

    def test_全部切れていれば外さない(self):
        """どこかで取るしかない(在庫薄として選定が避ける)。距離はいちばん近い置き場。"""
        found = location_service.find_nearest_shelf_for_board(
            self.conn, 1000, 1000, "L1", HB, skip={"lblItem1"})
        self.assertEqual(found.shelf_name, "lblItem1")
        self.assertEqual(found.skipped, ())

    def test_LVCから測れば1番が切れていても距離は同じ(self):
        """拠点の近くの置き場が切れていなければ、ほかの置き場の看板は効かない。"""
        self.all_in_stock_1030(l1_wanted=True)
        stock = self.stock()
        plain = location_service.find_nearest_shelf_for_board(self.conn, 1030, 1520, "LVC", HB)
        skipped = location_service.find_nearest_shelf_for_board(
            self.conn, 1030, 1520, "LVC", HB, skip=stock.out_for(HB, 1030, 1520))
        self.assertEqual(plain.shelf_name, skipped.shelf_name)
        self.assertAlmostEqual(plain.distance, skipped.distance)


class TableTests(_KanbanCase):

    def test_表を直すとすぐ効く_有効0は見ない(self):
        self.all_in_stock_1030(l1_wanted=True)
        self.conn.execute(f'UPDATE "{TABLE}" SET 有効 = 0 WHERE 看板 = ?', ("看板_L1",))
        self.conn.commit()
        stock = self.stock()
        self.assertEqual(stock.out_for(HB, 1030, 1520), frozenset())
        self.conn.execute(f'INSERT INTO "{TABLE}" (看板, 資材の注記, 置き場) VALUES (?,?,?)',
                          ("看板_L1", "", "lblItem7"))
        self.conn.commit()
        self.assertEqual(self.stock().out_for(HB, 1030, 1520), frozenset({"lblItem7"}))

    def test_マスタ管理で直せる_作ると初期値_最後の1行は消せない(self):
        from packaging_tool import config, master_admin, master_schema, selection_session
        self.assertIn(TABLE, master_common.BY_TABLE)
        self.assertIn(TABLE, import_specs.OPTIONAL_TABLES)
        self.assertIn(TABLE, master_schema.AUTO_CREATE_TABLES)
        self.assertTrue(master_schema.can_create(TABLE))

        folder = Path(tempfile.mkdtemp(prefix="kanban_shelf_"))
        src = folder / config.MATERIAL_DB_NAME
        sqlite3.connect(src).execute("CREATE TABLE PalletMaster (管理番号 INTEGER)").connection.close()
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        selection_session.get_session(self.conn).admin = True

        self.assertTrue(master_admin.create_table(self.conn, TABLE, path=src).ok)
        rows = master_admin.page(src, TABLE).rows
        self.assertEqual(len(rows), len(kanban_shelves.SEED))
        for row in rows[:-1]:
            self.assertTrue(master_admin.delete_row(self.conn, TABLE, row["__行"], path=src).ok)
        last = master_admin.delete_row(self.conn, TABLE, rows[-1]["__行"], path=src)
        self.assertFalse(last.ok)
        self.assertIn("有効」を 0", last.message)


class SessionTests(_KanbanCase):
    """資材選択(在庫考慮ON・疲労度ON)のログと、棚検索の「まとめて見る」。"""

    def setUp(self) -> None:
        super().setUp()
        from packaging_tool.selection_session import SelectionSession

        before = user_settings.get_position()
        self.addCleanup(lambda: user_settings.set_position(before))
        user_settings.set_position("L1")
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        layout_session.reset_session()
        self.addCleanup(layout_session.reset_session)
        self.all_in_stock_1030(l1_wanted=True)
        self.log = UserLog()
        self.session = SelectionSession(presenter=SelectionPresenter(self.conn, user_log=self.log))
        self.session.apply_pallet("1050", "1550")
        self.session.apply_product("1030", "1520")
        self.session.board_type = HB

    def turn(self, name: str, on: bool) -> None:
        if getattr(self.session, name) != on:
            self.session.toggle(name)

    def test_在庫考慮ONなら外した置き場をログに残し_棚検索も外す(self):
        self.turn("stock_aware", True)
        self.turn("fatigue", True)
        self.assertTrue(self.session.auto_select_boards().ok)
        self.assertIn("看板が出ている置き場を外して距離を測ります: 1030×1520(棚1)", self.log.text)

        from packaging_tool import board_selection_service as svc
        self.session.selected.lower = [svc.SelectedBoard(1030, 1520, 1)]
        self.session.selected.upper = []
        items = self.session.map_items()
        self.assertEqual(items[0]["skip_shelves"], ["lblItem1"])
        result = layout_session.get_session().show_selection(self.conn, items)
        self.assertTrue(result.ok)
        self.assertNotIn("lblItem1", layout_session.get_session().highlight)
        self.assertIn("看板が出ている置き場は外しました: 1030×1520(棚1)", result.message)

    def test_在庫考慮OFFなら外さない(self):
        self.turn("stock_aware", False)
        self.turn("fatigue", True)
        self.assertTrue(self.session.auto_select_boards().ok)
        self.assertNotIn("看板が出ている置き場", self.log.text)
        from packaging_tool import board_selection_service as svc
        self.session.selected.lower = [svc.SelectedBoard(1030, 1520, 1)]
        self.session.selected.upper = []
        self.assertEqual(self.session.map_items()[0]["skip_shelves"], [])


if __name__ == "__main__":
    unittest.main()
