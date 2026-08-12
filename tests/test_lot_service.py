"""ロット検索(lot_service)のユニットテスト。

VBA `SearchLotInfo` / `SearchHikiAndOdr` / `SearchOdrInfo` の
業務ルール(BOX実績への差し替え、包装仕様NOの書き換え、EX判定等)を検証する。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import db, lot_service as svc


def insert_lot(conn, lot_no="1234567", **kw) -> None:
    values = {
        "ロット番号": lot_no, "用途コード": "K100", "用途名": "一般",
        "製造材質": "A5052", "製造調質": "H32",
        "製造板厚": 3.0, "製造板幅": 1000.0, "製造板丈": 2000.0,
        "オーダー板厚": 3.2, "オーダー板幅": 1010.0, "オーダー板丈": 2010.0,
        "設計_設備コース": "AAA", "実績_設備コース": "BBB",
        "BOX実績_板厚": 6.75, "BOX実績_板幅": 1200.0, "BOX実績_板丈": 2400.0,
        "BOX実績_枚本数": 42,
        # "S"/"T"はAdvanceCheckのflag2免除対象なので、既定では
        # 試験指示票が不要になる(製造板厚3.0・用途コードK100は
        # 本来flag1/flag3に該当してしまうため、免除面で打ち消す)
        "品質グレード_表面処理": "S",
    }
    values.update(kw)
    cols = ", ".join(f"[{c}]" for c in values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO 仕掛ロット ({cols}) VALUES ({marks})", tuple(values.values()))
    conn.commit()


def insert_hiki(conn, lot_no="1234567", order_no="ORD1", qty=10.0, adj="", no="60717001") -> None:
    conn.execute(
        "INSERT INTO 仕掛引当 (ロット番号, 受注番号, 引当数量, 引当調整NO, 引当番号) "
        "VALUES (?, ?, ?, ?, ?)", (lot_no, order_no, qty, adj, no))
    conn.commit()


def insert_odr(conn, order_no="ORD1", **kw) -> None:
    values = {
        "受注番号": order_no, "納入先名称": "A社", "包装仕様NO": "1P0001",
        "取引先名称": "B商事", "送り先名称": "C倉庫",
        "納送用コメント": "", "工場用コメント": "", "VC_表": "", "VC_裏": "",
        "EX_輸出区分": "", "材質_比重": 2.7,
        "梱包単位_重量": 500.0, "梱包単位_枚数": 20.0,
    }
    values.update(kw)
    cols = ", ".join(f"[{c}]" for c in values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO 仕掛受注 ({cols}) VALUES ({marks})", tuple(values.values()))
    conn.commit()


class FormatTests(unittest.TestCase):
    def test_thickness_uses_three_decimals(self):
        self.assertEqual(svc.format_thickness(6.75), "6.750")

    def test_dimension_uses_one_decimal(self):
        self.assertEqual(svc.format_dimension(1000.0), "1000.0")


class BoxCourseTests(unittest.TestCase):
    def test_detects_each_configured_course(self):
        for course in svc.BOX_COURSES:
            self.assertEqual(svc._detect_box_course(f"XX{course}YY"), course)

    def test_returns_empty_for_other_courses(self):
        self.assertEqual(svc._detect_box_course("AAA"), "")

    def test_empty_course_is_not_box(self):
        self.assertEqual(svc._detect_box_course(""), "")


class LotSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_blank_lot_no_is_rejected(self):
        result = svc.search_lot(self.conn, "   ")
        self.assertFalse(result.found)
        self.assertIn("入力", result.message)

    def test_unknown_lot_reports_not_found(self):
        result = svc.search_lot(self.conn, "9999999")
        self.assertFalse(result.found)
        self.assertIn("未発見", result.message)

    def test_non_box_course_uses_manufactured_dimensions(self):
        insert_lot(self.conn, 設計_設備コース="AAA")
        lot = svc.search_lot(self.conn, "1234567").lot
        self.assertFalse(lot.is_box)
        self.assertEqual((lot.thickness, lot.width, lot.length), (3.0, 1000.0, 2000.0))
        self.assertEqual(lot.dimension_source, "製造")

    def test_box_course_swaps_in_box_dimensions(self):
        insert_lot(self.conn, 設計_設備コース="X-GCT-1")
        lot = svc.search_lot(self.conn, "1234567").lot
        self.assertTrue(lot.is_box)
        self.assertEqual(lot.box_course, "GCT")
        self.assertEqual((lot.thickness, lot.width, lot.length), (6.75, 1200.0, 2400.0))
        self.assertEqual(lot.dimension_source, "BOX実績")

    def test_manufactured_thickness_is_kept_even_in_box_mode(self):
        # 包装仕様NOの判定は製造板厚を見るので、差し替え後も元の値が要る
        insert_lot(self.conn, 設計_設備コース="GFS", 製造板厚=3.0)
        lot = svc.search_lot(self.conn, "1234567").lot
        self.assertEqual(lot.thickness, 6.75)          # 表示はBOX実績
        self.assertEqual(lot.manufactured_thickness, 3.0)

    def test_order_dimensions_are_never_swapped(self):
        insert_lot(self.conn, 設計_設備コース="GSS")
        lot = svc.search_lot(self.conn, "1234567").lot
        self.assertEqual((lot.order_thickness, lot.order_width, lot.order_length),
                         (3.2, 1010.0, 2010.0))

    def test_lot_header_shows_box_marker_and_count(self):
        insert_lot(self.conn, 設計_設備コース="GCT", BOX実績_枚本数=7)
        result = svc.search_lot(self.conn, "1234567")
        self.assertIn("【BOX実績寸法】", result.lot_header)
        self.assertIn("前工程実績数: 7枚", result.lot_header)

    def test_lot_header_without_box_marker(self):
        insert_lot(self.conn, 設計_設備コース="AAA")
        result = svc.search_lot(self.conn, "1234567")
        self.assertNotIn("【BOX実績寸法】", result.lot_header)


class SpecOverrideTests(unittest.TestCase):
    """包装仕様NO → 1P0122 の書き換え(flag1〜flag5の全AND)。"""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn, 包装仕様NO="1P0001")

    def tearDown(self) -> None:
        self.conn.close()

    def _all_flags(self, **overrides):
        values = {
            "製造材質": "A7075S", "製造調質": "T6511", "用途コード": "K434",
            "製造板厚": 6.75, "設計_設備コース": "GFS",
        }
        values.update(overrides)
        insert_lot(self.conn, **values)
        return svc.search_lot(self.conn, "1234567")

    def test_all_flags_trigger_override(self):
        self.assertEqual(self._all_flags().odr.packaging_spec, "1P0122")

    def test_k435_also_qualifies(self):
        self.assertEqual(self._all_flags(用途コード="K435").odr.packaging_spec, "1P0122")

    def test_material_must_end_with_75s(self):
        self.assertEqual(self._all_flags(製造材質="A5052").odr.packaging_spec, "1P0001")

    def test_temper_must_start_with_t6(self):
        self.assertEqual(self._all_flags(製造調質="H32").odr.packaging_spec, "1P0001")

    def test_yoto_code_must_match(self):
        self.assertEqual(self._all_flags(用途コード="K100").odr.packaging_spec, "1P0001")

    def test_thickness_must_be_6_75(self):
        self.assertEqual(self._all_flags(製造板厚=3.0).odr.packaging_spec, "1P0001")

    def test_box_course_is_required(self):
        self.assertEqual(self._all_flags(設計_設備コース="AAA").odr.packaging_spec, "1P0001")


class HikiAndOdrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        insert_lot(self.conn, 設計_設備コース="AAA")

    def tearDown(self) -> None:
        self.conn.close()

    def test_hiki_rows_are_sorted_by_hiki_no(self):
        # 引当番号は8桁固定の番号。文字列で持っても昇順は数値順と一致する
        insert_hiki(self.conn, order_no="O3", no="60717003")
        insert_hiki(self.conn, order_no="O1", no="60717001")
        insert_hiki(self.conn, order_no="O2", no="60717002")
        insert_odr(self.conn, order_no="O1")
        result = svc.search_lot(self.conn, "1234567")
        self.assertEqual([h.order_no for h in result.hiki], ["O1", "O2", "O3"])

    def test_the_hiki_number_stays_a_plain_number_string(self):
        """数値で持つと 6.0717e+07 と指数表記になり、現場が読めなくなる。"""
        insert_hiki(self.conn, order_no="O1", no="60717000")
        insert_odr(self.conn, order_no="O1")
        result = svc.search_lot(self.conn, "1234567")
        self.assertEqual(result.hiki[0].hiki_no, "60717000")
        self.assertIsInstance(result.hiki[0].hiki_no, str)

    def test_a_leading_zero_in_the_hiki_number_survives(self):
        insert_hiki(self.conn, order_no="O1", no="00717000")
        insert_odr(self.conn, order_no="O1")
        result = svc.search_lot(self.conn, "1234567")
        self.assertEqual(result.hiki[0].hiki_no, "00717000")

    # -- 試験指示票の要否 (要 / 不要。OK/NGで表すものではない) ------------
    # VBA `AdvanceCheck`: flag2(表面がS/T以外) And (flag1(板厚3mm以下)
    # Or flag3(用途コード頭がT/D1/D2))。以前は試験NOという実際には
    # 判定に使われていない列で誤って判定していた
    def test_not_needed_when_the_surface_is_exempt(self):
        # setUpの既定値(品質グレード_表面処理="S", 製造板厚=3.0mm)は
        # flag1こそ真だが、flag2(表面がS/T以外)が偽なので不要
        result = svc.search_lot(self.conn, "1234567")
        self.assertFalse(result.lot.needs_test_slip)
        self.assertEqual(result.lot.test_slip_text, svc.TEST_SLIP_NOT_NEEDED)

    def test_needed_when_the_surface_is_not_exempt_and_thickness_is_thin(self):
        # 表面がS/T以外(flag2=真)で、板厚3mm以下(flag1=真)
        self.conn.execute(
            "UPDATE 仕掛ロット SET 品質グレード_表面処理 = 'N', 製造板厚 = 3.0")
        self.conn.commit()
        result = svc.search_lot(self.conn, "1234567")
        self.assertTrue(result.lot.needs_test_slip)
        self.assertEqual(result.lot.test_slip_text, svc.TEST_SLIP_NEEDED)

    def test_not_needed_when_thick_and_the_use_code_has_no_special_prefix(self):
        # 表面はS/T以外だが、板厚が3mm超(flag1=偽)かつ
        # 用途コードがT/D1/D2で始まらない(flag3=偽)ので不要
        self.conn.execute(
            "UPDATE 仕掛ロット SET 品質グレード_表面処理 = 'N', 製造板厚 = 5.0,"
            " 用途コード = 'K100'")
        self.conn.commit()
        result = svc.search_lot(self.conn, "1234567")
        self.assertFalse(result.lot.needs_test_slip)

    def test_needed_via_the_use_code_prefix_even_when_thick(self):
        # 板厚は3mm超で不要のはずだが、用途コードがD1で始まる(flag3=真)
        self.conn.execute(
            "UPDATE 仕掛ロット SET 品質グレード_表面処理 = 'N', 製造板厚 = 5.0,"
            " 用途コード = 'D1234'")
        self.conn.commit()
        result = svc.search_lot(self.conn, "1234567")
        self.assertTrue(result.lot.needs_test_slip)

    def test_exempt_surface_wins_even_when_thin_and_special_use_code(self):
        # flag1・flag3がどちらも真でも、表面がS/T(flag2=偽)なら不要
        self.conn.execute(
            "UPDATE 仕掛ロット SET 品質グレード_表面処理 = 'T', 製造板厚 = 1.0,"
            " 用途コード = 'T999'")
        self.conn.commit()
        result = svc.search_lot(self.conn, "1234567")
        self.assertFalse(result.lot.needs_test_slip)

    def test_no_hiki_gives_empty_order_info(self):
        result = svc.search_lot(self.conn, "1234567")
        self.assertEqual(result.hiki, [])
        self.assertEqual(result.odr.delivery_name, "")

    def test_odr_fields_are_loaded(self):
        insert_hiki(self.conn)
        insert_odr(self.conn, 納入先名称="A社", VC_表="V1", VC_裏="V2", 材質_比重=2.71)
        odr = svc.search_lot(self.conn, "1234567").odr
        self.assertEqual(odr.delivery_name, "A社")
        self.assertEqual((odr.vc_front, odr.vc_back), ("V1", "V2"))
        self.assertAlmostEqual(odr.specific_gravity, 2.71)

    def test_ex_flag_set_when_export_code_present(self):
        insert_hiki(self.conn)
        insert_odr(self.conn, EX_輸出区分="1")
        result = svc.search_lot(self.conn, "1234567")
        self.assertTrue(result.odr.is_ex)
        self.assertIn("EX", result.odr_header)

    def test_ex_flag_ignores_whitespace_only(self):
        insert_hiki(self.conn)
        insert_odr(self.conn, EX_輸出区分="   ")
        self.assertFalse(svc.search_lot(self.conn, "1234567").odr.is_ex)

    def test_odr_header_shows_box_course_without_ex(self):
        self.conn.execute("DELETE FROM 仕掛ロット")
        insert_lot(self.conn, 設計_設備コース="GSS")
        insert_hiki(self.conn)
        insert_odr(self.conn)
        result = svc.search_lot(self.conn, "1234567")
        self.assertIn("GSS", result.odr_header)
        self.assertNotIn("EX", result.odr_header)

    def test_pack_units_collected_per_order(self):
        insert_hiki(self.conn, order_no="O1", no=1.0)
        insert_hiki(self.conn, order_no="O2", no=2.0)
        insert_odr(self.conn, order_no="O1", 梱包単位_重量=500.0, 梱包単位_枚数=20.0)
        insert_odr(self.conn, order_no="O2", 梱包単位_重量=700.0, 梱包単位_枚数=30.0)
        odr = svc.search_lot(self.conn, "1234567").odr
        self.assertEqual(odr.pack_unit_weight, {"O1": 500.0, "O2": 700.0})
        self.assertEqual(odr.pack_unit_count, {"O1": 20.0, "O2": 30.0})

    def test_duplicate_order_numbers_are_deduplicated(self):
        insert_hiki(self.conn, order_no="O1", no=1.0)
        insert_hiki(self.conn, order_no="O1", no=2.0)
        insert_odr(self.conn, order_no="O1")
        result = svc.search_lot(self.conn, "1234567")
        self.assertEqual(len(result.hiki), 2)          # 引当行は両方残る
        self.assertEqual(result.odr.delivery_name, "A社")


class HasLotDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_false_when_not_imported(self):
        self.assertFalse(svc.has_lot_data(self.conn))

    def test_true_after_import(self):
        insert_lot(self.conn)
        self.assertTrue(svc.has_lot_data(self.conn))


if __name__ == "__main__":
    unittest.main()


class PackUnitTests(unittest.TestCase):
    def test_weight_takes_priority_over_count(self):
        odr = svc.OdrInfo(pack_unit_weight={"O1": 500.0}, pack_unit_count={"O1": 20.0})
        self.assertEqual(svc.pack_unit_of(odr, "O1"), ("kg", 500.0))

    def test_falls_back_to_sheet_count(self):
        odr = svc.OdrInfo(pack_unit_weight={"O1": 0.0}, pack_unit_count={"O1": 20.0})
        self.assertEqual(svc.pack_unit_of(odr, "O1"), ("mai", 20.0))

    def test_unknown_order_has_no_unit(self):
        self.assertEqual(svc.pack_unit_of(svc.OdrInfo(), "X"), ("", 0.0))


class FilterPalletSymbolTests(unittest.TestCase):
    def test_hidden_symbols_become_blank(self):
        for sym in svc.HIDDEN_PALLET_SYMBOLS:
            self.assertEqual(svc.filter_pallet_symbol(sym), "")

    def test_other_symbols_pass_through(self):
        self.assertEqual(svc.filter_pallet_symbol("EXタイト"), "EXタイト")

    def test_build_hinmei_drops_hidden_symbol(self):
        self.assertEqual(svc.build_hinmei("タイト", "G1", 1150, 2650), "タイト　　1150x2650")
        self.assertEqual(svc.build_hinmei("全面", "C1", 1150, 2650), "全面　C1　1150x2650")


class CalcTotalPackagesTests(unittest.TestCase):
    """VBA `CalcTotalPackages` の移植。"""

    def _result(self, *, rows, gravity=2.7, count=100, thickness=3.0,
                width=1000.0, length=2000.0, weights=None, counts=None):
        lot = svc.LotInfo(thickness=thickness, width=width, length=length,
                          prev_process_count=count)
        odr = svc.OdrInfo(specific_gravity=gravity,
                          pack_unit_weight=weights or {}, pack_unit_count=counts or {})
        return svc.LotSearchResult(found=True, lot=lot, hiki=rows, odr=odr)

    def test_adjusted_row_blocks_calculation(self):
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_ADJUSTED, display_value="5")]
        self.assertEqual(svc.calc_total_packages(self._result(rows=rows)),
                         svc.PACKAGES_UNKNOWN)

    def test_kg_rows_without_sheet_weight_block_calculation(self):
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="50")]
        result = self._result(rows=rows, gravity=0.0, weights={"O1": 500.0})
        self.assertEqual(svc.calc_total_packages(result), svc.PACKAGES_UNKNOWN)

    def test_sheet_count_packaging(self):
        # 梱包単位20枚、引当50枚 → ceil(50/20) = 3梱包
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="50")]
        result = self._result(rows=rows, counts={"O1": 20.0})
        self.assertEqual(svc.calc_total_packages(result), 3)

    def test_weight_packaging_uses_5_percent_tolerance(self):
        # 1枚重量 = 3*1000*2000*2.7/1e6 = 16.2kg
        # 梱包単位100kg → 上限105kg → 1梱包6枚 → 引当30枚で ceil(30/6)=5梱包
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="30")]
        result = self._result(rows=rows, weights={"O1": 100.0})
        self.assertEqual(svc.calc_total_packages(result), 5)

    def test_all_quantity_consumes_the_remaining_stock(self):
        # 全量 → 前工程実績数100枚を20枚ずつ → 5梱包
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_ALL, display_value="全量")]
        result = self._result(rows=rows, counts={"O1": 20.0}, count=100)
        self.assertEqual(svc.calc_total_packages(result), 5)

    def test_quantity_is_capped_by_remaining_stock(self):
        # 引当500枚でも前工程実績数は100枚 → ceil(100/20)=5梱包
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="500")]
        result = self._result(rows=rows, counts={"O1": 20.0}, count=100)
        self.assertEqual(svc.calc_total_packages(result), 5)

    def test_rows_share_the_stock_in_order(self):
        rows = [
            svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="60"),
            svc.HikiRow(order_no="O2", type_flag=svc.HIKI_QUANTITY, display_value="60"),
        ]
        # 在庫100 → 1行目60枚(3梱包)、2行目は残40枚(2梱包)
        result = self._result(rows=rows, counts={"O1": 20.0, "O2": 20.0}, count=100)
        self.assertEqual(svc.calc_total_packages(result), 5)

    def test_stops_once_stock_is_exhausted(self):
        rows = [
            svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="100"),
            svc.HikiRow(order_no="O2", type_flag=svc.HIKI_QUANTITY, display_value="100"),
        ]
        result = self._result(rows=rows, counts={"O1": 20.0, "O2": 20.0}, count=100)
        self.assertEqual(svc.calc_total_packages(result), 5)   # 2行目は在庫切れで処理しない

    def test_unit_suffixes_are_stripped(self):
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="50枚")]
        result = self._result(rows=rows, counts={"O1": 20.0})
        self.assertEqual(svc.calc_total_packages(result), 3)

    def test_row_without_pack_unit_counts_as_one(self):
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="50")]
        self.assertEqual(svc.calc_total_packages(self._result(rows=rows)), 1)

    def test_no_hiki_rows_gives_zero(self):
        self.assertEqual(svc.calc_total_packages(self._result(rows=[])), 0)


class HikiTypeFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        insert_lot(self.conn, 設計_設備コース="AAA")
        insert_odr(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_adjust_no_marks_the_row_adjusted(self):
        insert_hiki(self.conn, adj="7", qty=10.0)
        row = svc.search_lot(self.conn, "1234567").hiki[0]
        self.assertEqual(row.type_flag, svc.HIKI_ADJUSTED)
        self.assertEqual(row.display_value, "7")

    def test_zero_adjust_no_is_not_adjusted(self):
        insert_hiki(self.conn, adj="0", qty=10.0)
        row = svc.search_lot(self.conn, "1234567").hiki[0]
        self.assertEqual(row.type_flag, svc.HIKI_QUANTITY)

    def test_no_quantity_means_all(self):
        insert_hiki(self.conn, qty=0.0, adj="")
        row = svc.search_lot(self.conn, "1234567").hiki[0]
        self.assertEqual(row.type_flag, svc.HIKI_ALL)
        self.assertEqual(row.display_value, "全量")
        self.assertTrue(row.is_all)
