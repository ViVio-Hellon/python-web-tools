"""包装仕様NOで切り替わる特殊モード(1P0113 / プロテック)のテスト。"""
from __future__ import annotations

import sqlite3
import unittest

from packaging_tool import db, special_packaging as spk


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def insert_material(conn, 品名, 厚, 幅, 丈min, 丈max, コード="", 単位="", 備考=""):
    conn.execute(
        "INSERT INTO 松板角材 (品名, 厚, 幅, 丈min, 丈max, コード, 単位, 備考)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (品名, 厚, 幅, 丈min, 丈max, コード, 単位, 備考))
    conn.commit()


class ModeDetectionTests(unittest.TestCase):
    def test_1p0113_is_detected(self):
        self.assertTrue(spk.is_1p0113("1P0113"))
        self.assertTrue(spk.is_1p0113("  1P0113  "))
        self.assertFalse(spk.is_1p0113("1P0122"))
        self.assertFalse(spk.is_1p0113(""))

    def test_protec_specs(self):
        for spec in ("1P1216", "1P1125", "1P1211"):
            self.assertTrue(spk.protec_state(spec).is_protec, spec)
        self.assertFalse(spk.protec_state("1P0113").is_protec)

    def test_only_1p1216_gets_the_tighter_tolerance(self):
        """幅許容-5mmは1P1216(ｺｰﾐ金属)限定。他のプロテックは-80mm。"""
        self.assertTrue(spk.protec_state("1P1216").is_1p1216)
        self.assertFalse(spk.protec_state("1P1125").is_1p1216)

    def test_board_type_switches_with_the_mode(self):
        self.assertEqual(spk.protec_state("1P1216").board_type, spk.PROTEC_BOARD_TYPE)
        self.assertEqual(spk.protec_state("1P0100").board_type, spk.DEFAULT_BOARD_TYPE)


class AValueTests(unittest.TestCase):
    """松板の丈(A値)は製品丈の帯で決まる(VBA `Load1P0113Materials`)。"""

    def test_short_products_use_their_own_length(self):
        self.assertEqual(spk.calc_a_value(1), 1)
        self.assertEqual(spk.calc_a_value(949), 949)

    def test_bands_above_949(self):
        self.assertEqual(spk.calc_a_value(950), 950)
        self.assertEqual(spk.calc_a_value(1800), 950)
        self.assertEqual(spk.calc_a_value(1801), 1600)
        self.assertEqual(spk.calc_a_value(2400), 1600)
        self.assertEqual(spk.calc_a_value(2401), 2330)
        self.assertEqual(spk.calc_a_value(9999), 2330)


class CountTests(unittest.TestCase):
    def test_kakuzai_count_by_length(self):
        self.assertEqual(spk.calc_kakuzai_count(1800), 2)
        self.assertEqual(spk.calc_kakuzai_count(1801), 3)
        self.assertEqual(spk.calc_kakuzai_count(2400), 3)
        self.assertEqual(spk.calc_kakuzai_count(2401), 4)

    def test_kakuzai_length_adds_the_margin(self):
        self.assertEqual(spk.calc_kakuzai_length(1200), 1210)

    def test_matsuita_is_always_two(self):
        self.assertEqual(spk.MATSUITA_CNT, 2)


class FindMaterialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()
        insert_material(self.conn, "溝切角材", 75, 75, 0, 1000, "251022", "本")
        insert_material(self.conn, "溝切角材", 75, 75, 1001, 1999, "251023", "本")
        insert_material(self.conn, "松板", 24, 120, 0, 1000, "355043", "枚")

    def tearDown(self) -> None:
        self.conn.close()

    def test_picks_the_band_containing_the_target(self):
        hit = spk.find_material(self.conn, name="溝切角材", atsu=75, haba=75,
                                target_length=1200)
        self.assertTrue(hit.is_ok)
        self.assertEqual(hit.code, "251023")
        self.assertEqual(hit.info, "75x75 丈:1001-1999")

    def test_boundaries_are_inclusive(self):
        for target, code in ((1000, "251022"), (1001, "251023"), (1999, "251023")):
            self.assertEqual(
                spk.find_material(self.conn, name="溝切角材", atsu=75, haba=75,
                                  target_length=target).code, code)

    def test_no_band_means_not_found(self):
        hit = spk.find_material(self.conn, name="溝切角材", atsu=75, haba=75,
                                target_length=5000)
        self.assertFalse(hit.is_ok)
        self.assertEqual(hit.info, spk.INFO_NOT_FOUND)

    def test_wrong_dimensions_do_not_match(self):
        """厚・幅は固定値でしか引かない(60x60の角材が混ざっても拾わない)。"""
        insert_material(self.conn, "溝切角材", 60, 60, 0, 1000, "251015", "本")
        self.assertEqual(
            spk.find_material(self.conn, name="溝切角材", atsu=75, haba=75,
                              target_length=500).code, "251022")

    def test_biko_is_appended(self):
        insert_material(self.conn, "松板", 24, 120, 2000, 2999, "355045", "枚", "要確認")
        hit = spk.find_material(self.conn, name="松板", atsu=24, haba=120,
                                target_length=2500)
        self.assertEqual(hit.info, "24x120 丈:2000-2999 (要確認)")

    def test_duplicate_bands_resolve_by_management_number(self):
        """丈帯が重複する実データがあるので、必ず管理番号の若い方を採る。"""
        insert_material(self.conn, "松板", 24, 120, 4000, 4999, "355047", "枚")
        insert_material(self.conn, "松板", 24, 120, 4000, 4999, "355054", "枚")
        self.assertEqual(
            spk.find_material(self.conn, name="松板", atsu=24, haba=120,
                              target_length=4500).code, "355047")


class LoadMaterialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()
        insert_material(self.conn, "溝切角材", 75, 75, 1001, 1999, "251023", "本")
        insert_material(self.conn, "松板", 24, 120, 0, 1000, "355043", "枚")

    def tearDown(self) -> None:
        self.conn.close()

    def test_labels_show_the_actual_dimensions(self):
        m = spk.load_materials(self.conn, 1200, 1500)
        # 角材は「製品幅+10mm」、松板はA値
        self.assertEqual(m.kakuzai_label, "1210mm")
        self.assertEqual(m.matsuita_label, "950mm")

    def test_unset_size_skips_the_lookup(self):
        m = spk.load_materials(self.conn, 0, 0)
        self.assertEqual(m.kakuzai_label, "製品ｻｲｽﾞ未設定")
        self.assertEqual(m.matsuita_label, "製品ｻｲｽﾞ未設定")
        self.assertFalse(m.kakuzai.is_ok)

    def test_miss_shows_the_reason_instead_of_a_dimension(self):
        m = spk.load_materials(self.conn, 5000, 1500)
        self.assertEqual(m.kakuzai_label, spk.INFO_NOT_FOUND)
        self.assertEqual(m.matsuita_label, "950mm")

    def test_counts_scale_with_the_multiplier(self):
        m = spk.load_materials(self.conn, 1200, 1500)
        self.assertEqual((m.kakuzai_count(1), m.matsuita_count(1)), (2, 2))
        self.assertEqual((m.kakuzai_count(3), m.matsuita_count(3)), (6, 6))

    def test_tip_shows_both_codes(self):
        m = spk.load_materials(self.conn, 1200, 1500)
        self.assertEqual(m.tip_text, "角材 CD:251023  松板 CD:355043  単位:枚")


class TextRoundTripTests(unittest.TestCase):
    """表示文字列 → 倉庫送信の値、という経路の往復を確かめる。"""

    def setUp(self) -> None:
        self.conn = make_conn()
        insert_material(self.conn, "溝切角材", 75, 75, 1001, 1999, "251023", "本")
        insert_material(self.conn, "松板", 24, 120, 0, 1000, "355043", "枚")
        self.m = spk.load_materials(self.conn, 1200, 1500)

    def tearDown(self) -> None:
        self.conn.close()

    def test_extract_count_reads_back_the_caption(self):
        self.assertEqual(spk.extract_count(spk.kakuzai_count_caption(6)), "6")
        self.assertEqual(spk.extract_count(spk.matsuita_count_caption(12)), "12")

    def test_extract_count_on_garbage(self):
        self.assertEqual(spk.extract_count("---"), "")

    def test_parse_info_pulls_the_order_code_and_unit(self):
        info = self.m.kakuzai.full_info
        self.assertEqual(spk.parse_info(info, "CD"), "251023")
        self.assertEqual(spk.parse_info(info, "単位"), "本")

    def test_parse_info_returns_the_master_band_for_length(self):
        """品名に載る丈はマスタの帯であって実際の切断長ではない(VBA踏襲)。"""
        self.assertEqual(spk.parse_info(self.m.kakuzai.full_info, "丈"), "1001-1999")
        self.assertEqual(self.m.kakuzai_length, 1210)

    def test_parse_info_handles_newlines(self):
        self.assertEqual(spk.parse_info(self.m.matsuita.cell_text, "コード"), "355043")

    def test_parse_info_missing_key(self):
        self.assertEqual(spk.parse_info(self.m.kakuzai.full_info, "存在しない"), "")

    def test_cell_text_falls_back_to_one_line_on_miss(self):
        miss = spk.load_materials(self.conn, 5000, 1500)
        self.assertEqual(miss.kakuzai.cell_text, f"溝切角材 {spk.INFO_NOT_FOUND}")


if __name__ == "__main__":
    unittest.main()
