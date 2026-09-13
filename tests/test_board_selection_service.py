"""資材選択(board_selection_service)のユニットテスト。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import board_selection_service as svc
from packaging_tool import db
from packaging_tool.user_log import UserLog


def insert_pallet(conn, *, width, length, w_min, w_max, l_min, l_max, industry="一般",
                   symbol="", leg=2, keta=3, unit="台", code=""):
    now = db.now_db_string()
    conn.execute(
        "INSERT INTO PalletMaster "
        "(幅, 丈, 巾適合min, 巾適合max, 丈適合min, 丈適合max, 業界, 記号, 位置, "
        " 在庫数, リスト管理, 桁数, 脚数, コード, 単位, 備考, 更新日時) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, '', ?, ?, ?, ?, '', ?)",
        (width, length, w_min, w_max, l_min, l_max, industry, symbol, keta, leg, code, unit, now),
    )
    conn.commit()


class BoardSelectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()


# ------------------------------------------------------------------
# パレット/製品サイズ確定
# ------------------------------------------------------------------
class ApplyPalletSizeTests(unittest.TestCase):
    def test_success_sets_overhang(self):
        result, palette = svc.apply_pallet_size("1100", "1300")
        self.assertTrue(result.ok)
        self.assertEqual((palette.width, palette.length), (1100, 1300))
        self.assertAlmostEqual(palette.max_width, 1100 * 1.10)
        self.assertAlmostEqual(palette.max_length, 1300 * 1.10)

    def test_non_numeric_rejected(self):
        result, _ = svc.apply_pallet_size("abc", "1300")
        self.assertFalse(result.ok)

    def test_non_positive_rejected(self):
        result, _ = svc.apply_pallet_size("0", "1300")
        self.assertFalse(result.ok)


class ApplyProductSizeTests(unittest.TestCase):
    def test_requires_pallet_first(self):
        result, _, _ = svc.apply_product_size("500", "600", svc.Palette())
        self.assertFalse(result.ok)

    def test_normal_fit(self):
        _, palette = svc.apply_pallet_size("1100", "1300")
        result, product, rotated = svc.apply_product_size("1000", "1200", palette)
        self.assertTrue(result.ok)
        self.assertEqual((product.width, product.length), (1000, 1200))
        self.assertFalse(rotated)

    def test_auto_rotate_when_only_rotated_fits(self):
        _, palette = svc.apply_pallet_size("1100", "1300")
        # 幅1200/丈1000は通常向きでは収まらないが、回転すれば収まる
        result, product, rotated = svc.apply_product_size("1200", "1000", palette)
        self.assertTrue(result.ok)
        self.assertTrue(rotated)
        self.assertEqual((product.width, product.length), (1000, 1200))

    def test_rejected_when_neither_orientation_fits(self):
        _, palette = svc.apply_pallet_size("1100", "1300")
        result, _, _ = svc.apply_product_size("2000", "2000", palette)
        self.assertFalse(result.ok)


# ------------------------------------------------------------------
# パレット一覧フィルタ
# ------------------------------------------------------------------
class ListPalletSizesTests(BoardSelectionTestCase):
    def test_excludes_ex_symbol_by_default(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, symbol="EXﾀｲﾄ", unit="台")
        insert_pallet(self.conn, width=1100, length=2100, w_min=1000, w_max=1200, l_min=2000, l_max=2200, symbol="", unit="台")
        rows = svc.list_pallet_sizes(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].width, 1100)

    def test_show_all_includes_ex(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, symbol="EXﾀｲﾄ", unit="枚")
        rows = svc.list_pallet_sizes(self.conn, show_all=True)
        self.assertEqual(len(rows), 1)

    def test_unit_filter_default_only_dai(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, unit="枚")
        rows = svc.list_pallet_sizes(self.conn)
        self.assertEqual(len(rows), 0)

    def test_unit_filter_relaxed_with_hosozai(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, unit="組")
        self.assertEqual(len(svc.list_pallet_sizes(self.conn)), 0)
        rows = svc.list_pallet_sizes(self.conn, last_hosozai="厚紙")
        self.assertEqual(len(rows), 1)

    def test_ex_only_mode_requires_is_ex_order(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, symbol="EXﾀｲﾄ", unit="台")
        # is_ex_order=Falseならex_only指定でも通常フィルタが適用される(EX除外)
        rows = svc.list_pallet_sizes(self.conn, ex_only=True, is_ex_order=False)
        self.assertEqual(len(rows), 0)
        rows = svc.list_pallet_sizes(self.conn, ex_only=True, is_ex_order=True)
        self.assertEqual(len(rows), 1)

    def test_zero_size_rows_excluded(self):
        insert_pallet(self.conn, width=0, length=2000, w_min=0, w_max=0, l_min=0, l_max=0, unit="台")
        self.assertEqual(len(svc.list_pallet_sizes(self.conn, show_all=True)), 0)

    def test_1p1185_mode_only_shows_tight_1300x1300(self):
        insert_pallet(self.conn, width=1300, length=1300, w_min=1200, w_max=1400,
                      l_min=1200, l_max=1400, industry="タイト", unit="台")
        insert_pallet(self.conn, width=1150, length=2650, w_min=1000, w_max=1200,
                      l_min=2500, l_max=2800, industry="タイト", unit="台")
        insert_pallet(self.conn, width=1300, length=1300, w_min=1200, w_max=1400,
                      l_min=1200, l_max=1400, industry="一般", unit="台")
        rows = svc.list_pallet_sizes(self.conn, is_1p1185_mode=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].width, rows[0].length, rows[0].industry), (1300, 1300, "タイト"))

    def test_1p1185_mode_off_shows_everything(self):
        insert_pallet(self.conn, width=1150, length=2650, w_min=1000, w_max=1200,
                      l_min=2500, l_max=2800, industry="タイト", unit="台")
        self.assertEqual(len(svc.list_pallet_sizes(self.conn, is_1p1185_mode=False)), 1)

    def test_matrix_row_suppressed_when_real_code_exists_for_same_size(self):
        insert_pallet(self.conn, width=1100, length=2000, w_min=1000, w_max=1200,
                      l_min=1900, l_max=2100, code="単価表のマトリックス", unit="台")
        insert_pallet(self.conn, width=1100, length=2000, w_min=1000, w_max=1200,
                      l_min=1900, l_max=2100, code="058901", unit="台")
        rows = svc.list_pallet_sizes(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "058901")

    def test_matrix_row_kept_when_no_real_code_exists(self):
        insert_pallet(self.conn, width=1100, length=2000, w_min=1000, w_max=1200,
                      l_min=1900, l_max=2100, code="単価表のマトリックス", unit="台")
        rows = svc.list_pallet_sizes(self.conn)
        self.assertEqual(len(rows), 1)


# ------------------------------------------------------------------
# ボード一覧
# ------------------------------------------------------------------
class BoardListTests(BoardSelectionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn.execute("INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル) VALUES (100, 200, 'ハードボード', '')")
        self.conn.execute("INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル) VALUES (150, 250, 'IKボード', '')")
        self.conn.execute("INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル) VALUES (300, 400, 'ハードボード', '')")
        self.conn.commit()

    def test_list_board_types_first_occurrence_order(self):
        self.assertEqual(svc.list_board_types(self.conn), ["ハードボード", "IKボード"])

    def test_filter_by_type(self):
        rows = svc.list_available_boards(self.conn, "ハードボード")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.board_type == "ハードボード" for r in rows))

    def test_no_filter_returns_all(self):
        self.assertEqual(len(svc.list_available_boards(self.conn)), 3)


# ------------------------------------------------------------------
# パレット自動選定
# ------------------------------------------------------------------
class AutoSelectPalletTests(BoardSelectionTestCase):
    def test_invalid_product_size(self):
        result = svc.auto_select_pallet(self.conn, product_width_text="abc", product_length_text="200")
        self.assertFalse(result.ok)

    def test_special_industry_pass_wins_over_general(self):
        # 特定業界(5×10)にヒットする行と、一般カテゴリの行の両方が製品サイズに合致する場合、
        # 優先順位が高い特定業界パスが勝つ
        insert_pallet(self.conn, width=2000, length=3000, w_min=900, w_max=1100, l_min=1900, l_max=2100, industry="5×10")
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, industry="一般")
        result = svc.auto_select_pallet(self.conn, product_width_text="1000", product_length_text="2000")
        self.assertTrue(result.ok)
        self.assertEqual(result.industry, "5×10")

    def test_min_area_within_same_pass(self):
        insert_pallet(self.conn, width=1500, length=2500, w_min=900, w_max=1100, l_min=1900, l_max=2100, industry="一般")
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, industry="一般")
        result = svc.auto_select_pallet(self.conn, product_width_text="1000", product_length_text="2000")
        self.assertTrue(result.ok)
        self.assertEqual((result.width, result.length), (1000, 2000))  # 面積最小

    def test_rotated_orientation_detected(self):
        # 通常向き(製品そのまま)では収まらないが、回転(パス2)なら収まるケース。
        # パレットは回転後の寸法(2000x1000)を**現物として載せられる**大きさに
        # しておく。適合範囲だけ辻褄を合わせて現物が小さいままだと、
        # 実際には載らない組み合わせをテストが通してしまう
        insert_pallet(self.conn, width=2000, length=1000, w_min=1900, w_max=2100, l_min=900, l_max=1100, industry="一般")
        result = svc.auto_select_pallet(self.conn, product_width_text="1000", product_length_text="2000")
        self.assertTrue(result.ok)
        self.assertTrue(result.rotated)

    def test_no_match_returns_error(self):
        result = svc.auto_select_pallet(self.conn, product_width_text="9999", product_length_text="9999")
        self.assertFalse(result.ok)

    def test_5x10_thickness_filter_thin_requires_kyodo_up(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100,
                      industry="5×10", symbol="強度UP")
        insert_pallet(self.conn, width=1010, length=2010, w_min=900, w_max=1100, l_min=1900, l_max=2100,
                      industry="5×10", symbol="")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1000", product_length_text="2000", manufactured_thickness=10.0,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.symbol, "強度UP")  # 板厚13mm以下は強度UPのみ許可

    def test_5x10_thickness_filter_thick_excludes_kyodo_up(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100,
                      industry="5×10", symbol="強度UP")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1000", product_length_text="2000", manufactured_thickness=20.0,
        )
        self.assertFalse(result.ok)  # 強度UPしか無いが板厚13mm超のため除外される

    def test_two_stack_requires_odd_keta_for_general(self):
        # 2山積は幅方向に2つ並べるので、探す幅は製品幅の2倍(2000)。
        # パレットもそれを現物として載せられる大きさでなければならない
        insert_pallet(self.conn, width=2000, length=1000, w_min=1900, w_max=2100, l_min=900, l_max=1100,
                      industry="一般", keta=4)  # 偶数桁 -> 2山積不可
        insert_pallet(self.conn, width=2010, length=1010, w_min=1900, w_max=2100, l_min=900, l_max=1100,
                      industry="一般", keta=3)  # 奇数桁 -> OK
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1000", product_length_text="1000", two_stack=True,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.width, 2010)

    def test_桁数のせいでないものを桁数のせいにしない(self):
        """**落ちた理由を取り違えない。**

        「△桁数偶数のため2山除外」の別枠は、サイズ・属性はOKで
        **桁数だけ**が原因のものを全件出すためのもの。ここに、桁数は
        奇数なのに現物に載らなくて落ちた行まで混ざると、マスタの桁数を
        直しに行くことになる。
        """
        # 適合範囲は通るが現物が小さい(壊れた行)。桁数は奇数
        insert_pallet(self.conn, width=1500, length=900,
                      w_min=1900, w_max=2100, l_min=900, l_max=1100,
                      industry="一般", keta=3)
        log = UserLog()
        svc.auto_select_pallet(
            self.conn, product_width_text="1000", product_length_text="1000",
            two_stack=True, user_log=log)
        text = "\n".join(e.text for e in log.entries)
        self.assertNotIn("桁数偶数", text)
        # 落ちた本当の理由はちゃんと出る
        self.assertIn("現物に載らない", text)

    # -- 現物に載らないパレットは選ばない ------------------------------
    #
    # 実マスタは 適合max = 現物サイズ - 10(一部 -20)で入っており、
    # 「製品はパレットより小さい」が常に守られている。だが適合範囲が
    # 逆転している行が実際に70件あるように、マスタは壊れうる。
    # 壊れた1行のせいで製品より小さいパレットが選定されないことを固定する。
    def test_a_pallet_smaller_than_the_product_is_never_chosen(self):
        # 適合範囲だけ見れば通ってしまう「壊れた行」。現物は製品より小さい
        insert_pallet(self.conn, width=1490, length=2970,
                      w_min=1441, w_max=1540, l_min=2641, l_max=3090, industry="タイト")
        insert_pallet(self.conn, width=1550, length=3100,
                      w_min=1441, w_max=1540, l_min=2641, l_max=3090, industry="タイト")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1528", product_length_text="3053")
        self.assertTrue(result.ok)
        # 面積最小なら1490x2970だが、製品が現物からはみ出すので選ばれない
        self.assertEqual((result.width, result.length), (1550, 3100))

    def test_it_says_why_a_broken_master_row_was_dropped(self):
        from packaging_tool import user_log as ulog_mod

        insert_pallet(self.conn, width=1490, length=2970,
                      w_min=1441, w_max=1540, l_min=2641, l_max=3090, industry="タイト")
        log = ulog_mod.UserLog()
        svc.auto_select_pallet(
            self.conn, product_width_text="1528", product_length_text="3053",
            user_log=log)
        self.assertIn("1490x2970", log.text)
        self.assertIn("現物からはみ出す", log.text)

    def test_nothing_is_chosen_when_every_row_is_too_small(self):
        insert_pallet(self.conn, width=1490, length=2970,
                      w_min=1441, w_max=1540, l_min=2641, l_max=3090, industry="タイト")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1528", product_length_text="3053")
        self.assertFalse(result.ok)

    def test_the_fallback_also_refuses_a_pallet_that_is_too_small(self):
        """最終フォールバックは条件を大きく緩めるが、現物だけは譲らない。"""
        insert_pallet(self.conn, width=500, length=500,
                      w_min=1, w_max=99999, l_min=1, l_max=99999,
                      industry="一般", symbol="EX")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="2000", product_length_text="2000")
        self.assertFalse(result.ok)

    def test_an_exact_fit_is_still_allowed(self):
        """製品とパレットが同寸のときは載る扱い(端数の丸め分だけ許容)。"""
        insert_pallet(self.conn, width=1000, length=2000,
                      w_min=900, w_max=1100, l_min=1900, l_max=2100, industry="一般")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1000", product_length_text="2000")
        self.assertTrue(result.ok)

    def test_rows_without_a_physical_size_are_left_to_the_fit_range(self):
        """現物サイズが未入力の行は判定できないので従来どおり適合範囲で見る。"""
        self.assertTrue(svc._physically_fits(
            {"幅": 0, "丈": 0}, 1528, 3053))

    def test_fallback_used_when_no_pass_matches(self):
        # EX記号の行は通常パス(EX除外)には一切引っかからないが、
        # フォールバック探索はEXフィルタを行わないため拾われる
        insert_pallet(self.conn, width=500, length=500, w_min=1, w_max=99999, l_min=1, l_max=99999,
                      industry="一般", symbol="EX")
        result = svc.auto_select_pallet(self.conn, product_width_text="500", product_length_text="500")
        self.assertTrue(result.ok)
        self.assertEqual(result.pass_label, "強制フォールバック")
        self.assertFalse(result.rotated)

    def test_1p1185_mode_only_matches_tight_1300x1300(self):
        # 特定業界の1300x1300は幅丈こそ合うが業界がタイトでないため
        # 1P1185モードでは選ばれない
        insert_pallet(self.conn, width=1300, length=1300, w_min=1200, w_max=1400,
                      l_min=1200, l_max=1400, industry="1×2")
        insert_pallet(self.conn, width=1300, length=1300, w_min=1200, w_max=1400,
                      l_min=1200, l_max=1400, industry="タイト")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1245", product_length_text="1245",
            is_1p1185_mode=True)
        self.assertTrue(result.ok)
        self.assertEqual((result.width, result.length, result.industry), (1300, 1300, "タイト"))

    def test_1p1185_mode_excludes_tight_pallets_of_other_sizes(self):
        # 業界=タイトだが1300x1300ではないので候補にならない
        insert_pallet(self.conn, width=1150, length=2650, w_min=1000, w_max=1200,
                      l_min=2500, l_max=2800, industry="タイト")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1245", product_length_text="1245",
            is_1p1185_mode=True)
        self.assertFalse(result.ok)

    def test_1p1185_mode_applies_to_the_fallback_too(self):
        # EX記号のため通常パス(EX除外)には引っかからずフォールバックで
        # 拾われる行でも、1P1185の絞り込み(候補行そのものから除外)は
        # フォールバックにも及んでいることを確認する
        insert_pallet(self.conn, width=1300, length=1300, w_min=1, w_max=9999,
                      l_min=1, l_max=9999, industry="タイト", symbol="EX")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1245", product_length_text="1245",
            is_1p1185_mode=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.pass_label, "強制フォールバック")
        self.assertEqual(result.industry, "タイト")

    def test_1p1185_mode_removes_non_matching_rows_before_the_fallback(self):
        # 一般業界のEX行はフォールバックなら本来拾われるはずだが、
        # 1P1185モードでは候補行から完全に除外されるため拾われない
        insert_pallet(self.conn, width=500, length=500, w_min=1, w_max=99999,
                      l_min=1, l_max=99999, industry="一般", symbol="EX")
        result = svc.auto_select_pallet(
            self.conn, product_width_text="500", product_length_text="500",
            is_1p1185_mode=True)
        self.assertFalse(result.ok)


class SearchPalletDirectTests(BoardSelectionTestCase):
    def test_within_tolerance(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, unit="台")
        insert_pallet(self.conn, width=1200, length=2200, w_min=1100, w_max=1300, l_min=2100, l_max=2300, unit="台")
        rows = svc.search_pallet_direct(self.conn, pallet_width_text="1020", pallet_length_text="2010")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].width, 1000)

    def test_blank_axis_is_unconstrained(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, unit="台")
        insert_pallet(self.conn, width=1000, length=5000, w_min=900, w_max=1100, l_min=4900, l_max=5100, unit="台")
        rows = svc.search_pallet_direct(self.conn, pallet_width_text="1000", pallet_length_text="")
        self.assertEqual(len(rows), 2)

    def test_ex_only_not_checked_only_show_all_matters(self):
        insert_pallet(self.conn, width=1000, length=2000, w_min=900, w_max=1100, l_min=1900, l_max=2100, symbol="EX", unit="台")
        rows = svc.search_pallet_direct(self.conn, pallet_width_text="1000", pallet_length_text="2000")
        self.assertEqual(len(rows), 0)  # show_all=False -> EX除外
        rows = svc.search_pallet_direct(self.conn, pallet_width_text="1000", pallet_length_text="2000", show_all=True)
        self.assertEqual(len(rows), 1)

    def test_1p1185_mode_only_matches_tight_1300x1300(self):
        insert_pallet(self.conn, width=1300, length=1300, w_min=1200, w_max=1400,
                      l_min=1200, l_max=1400, industry="タイト", unit="台")
        insert_pallet(self.conn, width=1150, length=2650, w_min=1000, w_max=1200,
                      l_min=2500, l_max=2800, industry="タイト", unit="台")
        rows = svc.search_pallet_direct(
            self.conn, pallet_width_text="1300", pallet_length_text="1300",
            show_all=True, is_1p1185_mode=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].width, rows[0].length), (1300, 1300))


class ListPalletsForProductTests(BoardSelectionTestCase):
    """パレット検索後、検索結果だけをリストに出すための絞り込み。"""

    def test_matches_when_product_is_within_the_fit_range(self):
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700, unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].width, 1150)

    def test_単位で絞る(self):
        """現場の声:「単位"台"での絞り込みのはずが、サイズでヒットする

        ものすべて表示している」。**この関数だけ単位の規則が抜けて
        いた** ── 全件一覧と直接検索には入っていたのに、製品サイズを
        入れたあとの一覧(いちばんよく使う経路)には無かった。
        """
        for unit in ("台", "枚", "組", "本"):
            insert_pallet(self.conn, width=1150, length=2650,
                          w_min=900, w_max=1200, l_min=2400, l_max=2700,
                          unit=unit, code=unit)
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500)
        self.assertEqual([r.unit for r in rows], ["台"])

    def test_保護材が決まっていれば枚と組も出す(self):
        """上蓋は台で数えないものがある。既定のままだと現物が出ない。"""
        for unit in ("台", "枚", "組", "本"):
            insert_pallet(self.conn, width=1150, length=2650,
                          w_min=900, w_max=1200, l_min=2400, l_max=2700,
                          unit=unit, code=unit)
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500,
            last_hosozai="上蓋")
        self.assertEqual(sorted(r.unit for r in rows), ["台", "枚", "組"])

    def test_アングルなら台だけに戻る(self):
        """保護材がアングル=別に用意するもの。パレットの単位は緩めない。"""
        for unit in ("台", "枚"):
            insert_pallet(self.conn, width=1150, length=2650,
                          w_min=900, w_max=1200, l_min=2400, l_max=2700,
                          unit=unit, code=unit)
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500,
            last_hosozai="アングル")
        self.assertEqual([r.unit for r in rows], ["台"])

    def test_全件表示なら単位で絞らない(self):
        for unit in ("台", "本"):
            insert_pallet(self.conn, width=1150, length=2650,
                          w_min=900, w_max=1200, l_min=2400, l_max=2700,
                          unit=unit, code=unit)
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500, show_all=True)
        self.assertEqual(len(rows), 2)

    def test_excludes_pallets_outside_the_fit_range(self):
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700, unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1900, product_length=2500)
        self.assertEqual(rows, [])

    def test_matches_the_rotated_orientation_too(self):
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700, unit="台")
        # 幅・丈を入れ替えても(回転すれば)当たる
        rows = svc.list_pallets_for_product(
            self.conn, product_width=2500, product_length=1000)
        self.assertEqual(len(rows), 1)

    def test_当たり方を行に持たせる(self):
        """**同じ「候補」でも当たり方が違えば現物の扱いが違う。**

        判定そのものは前からしていたのに捨てていたため、押した人には
        回さないと載らない行なのか、許容差でようやく入った行なのかを
        見分けようが無かった(現場の声、2回目の指摘)。
        """
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700, unit="台")

        # 通常向きで、適合範囲にそのまま収まる
        row = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500)[0]
        self.assertFalse(row.rotated)
        self.assertTrue(row.exact)
        self.assertEqual(row.tolerance, 0)

        # 幅・丈を入れ替えないと当たらない = 回転
        row = svc.list_pallets_for_product(
            self.conn, product_width=2500, product_length=1000)[0]
        self.assertTrue(row.rotated)
        self.assertTrue(row.exact)

    def test_許容差で当たった行はそう分かる(self):
        """適合範囲を少し外れ、±5mmでようやく入った行。

        現物には載る(`_physically_fits`)が、マスタの適合範囲からは
        はみ出している ── 「候補に出たのに、なぜか厳密ではない」行。
        """
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2500, unit="台")
        # 丈2503 は l_max=2500 を超えるが、許容5mm以内なので当たる
        row = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2503)[0]
        self.assertFalse(row.exact)
        self.assertEqual(row.tolerance, 5)

    def test_2山積で当たりが変わる(self):
        """**押しても何も変わらない、を作らない。**

        2山積は一覧の判定に一切入っておらず、押しても結果が同じだった
        ため「検索しなおさない」と受け取られていた(現場の声)。
        `auto_select_pallet` の `_search_dims` と同じく片側2倍で当たる。
        """
        # 製品1252x2502。幅2山(1252×2=2504)でだけ適合範囲に入る。
        # 桁数を奇数にして幅2山を許す(`_keta_ok`)
        insert_pallet(self.conn, width=2600, length=2600,
                      w_min=2400, w_max=2560, l_min=2400, l_max=2560,
                      unit="台", keta=3)
        self.assertEqual(
            svc.list_pallets_for_product(
                self.conn, product_width=1252, product_length=2502), [])
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1252, product_length=2502, two_stack=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].two_stack, svc.KIND_WIDTH2)

    def test_2山積は通常寸法を混ぜない(self):
        """**任意で2山を選ぶことに意味がある。**

        `auto_select_pallet` は2山モードで全パスの寸法を片側2倍に
        差し替えるので、通常寸法は1パスも走らない。一覧で通常の候補まで
        並べると「結果が増えて邪魔」になる(現場の指摘)。
        """
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      unit="台", keta=3)
        # 通常なら当たる製品サイズ
        self.assertEqual(
            len(svc.list_pallets_for_product(
                self.conn, product_width=1000, product_length=2500)), 1)
        # 2山にすると、この行は2倍寸法では適合しないので出ない
        self.assertEqual(
            svc.list_pallets_for_product(
                self.conn, product_width=1000, product_length=2500,
                two_stack=True), [])

    def test_2山積の条件に合わないパレットは出さない(self):
        """幅2山は桁数奇数かスカシ、丈2山はスカシ/タイトで脚数3以上
        (`_keta_ok` / `_category_ok`)。積めないパレットを候補にしない。"""
        # 桁数が偶数・業界も空なので、幅2山は許されない
        insert_pallet(self.conn, width=2600, length=2600,
                      w_min=2400, w_max=2560, l_min=2400, l_max=2560,
                      unit="台", keta=4)
        self.assertEqual(
            svc.list_pallets_for_product(
                self.conn, product_width=1252, product_length=2502,
                two_stack=True), [])

    def test_外した理由を選定ログに残す(self):
        """**決定事項は画面を見れば分かる。要るのは経緯**(現場の声)。

        サイズを打った時点で自動で走る検索なので、押した覚えのないまま
        候補が減る ── なぜその行が消えたのかは記録にしか残らない。
        """
        from packaging_tool.user_log import UserLog

        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700, unit="台")
        insert_pallet(self.conn, width=9000, length=9000,
                      w_min=8000, w_max=8500, l_min=8000, l_max=8500, unit="台")
        log = UserLog()
        svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500, user_log=log)
        text = log.text
        self.assertIn("○候補: 1150x2650", text)
        self.assertIn("×除外: 9000x9000", text)
        self.assertIn("適合範囲外", text)

    def test_excludes_ex_symbol_by_default(self):
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      symbol="EXﾀｲﾄ", unit="台")
        self.assertEqual(
            svc.list_pallets_for_product(
                self.conn, product_width=1000, product_length=2500),
            [])
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500, show_all=True)
        self.assertEqual(len(rows), 1)

    def test_5x10_thickness_filter_thin_requires_kyodo_up(self):
        """5×10業界は板厚フィルタも一覧に適用する(バグ修正)。

        自動選定(`auto_select_pallet`)は板厚≤13.0のとき強度UP以外を
        除外するが、一覧側にはこの判定が無く、自動選定なら出ないはずの
        行が一覧には並んでいた。
        """
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="5×10", symbol="通常", unit="台")
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="5×10", symbol="強度UP", unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500,
            manufactured_thickness=5.0)
        self.assertEqual([r.symbol for r in rows], ["強度UP"])

    def test_5x10_thickness_filter_thick_excludes_kyodo_up(self):
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="5×10", symbol="通常", unit="台")
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="5×10", symbol="強度UP", unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500,
            manufactured_thickness=40.0)
        self.assertEqual([r.symbol for r in rows], ["通常"])

    def test_5x10_thickness_unknown_lets_both_through(self):
        """板厚が分からないときは自動選定と同じく絞り込まない。"""
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="5×10", symbol="通常", unit="台")
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="5×10", symbol="強度UP", unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500,
            manufactured_thickness=None)
        self.assertEqual({r.symbol for r in rows}, {"通常", "強度UP"})

    def test_non_5x10_is_unaffected_by_thickness(self):
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="一般", symbol="強度UP", unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500,
            manufactured_thickness=40.0)
        self.assertEqual(len(rows), 1)

    def test_1p1185_mode_only_matches_tight_1300x1300(self):
        insert_pallet(self.conn, width=1300, length=1300,
                      w_min=1200, w_max=1400, l_min=1200, l_max=1400,
                      industry="タイト", unit="台")
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="タイト", unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1245, product_length=1245,
            is_1p1185_mode=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].width, rows[0].length), (1300, 1300))


class ListPalletsByProductDimsTests(BoardSelectionTestCase):
    """製品 幅・丈の入力中(未確定)の一覧絞り込み。"""

    def test_width_only_matches_by_tolerance(self):
        insert_pallet(self.conn, width=1020, length=2000,
                      w_min=900, w_max=1100, l_min=1900, l_max=2100, unit="台")
        insert_pallet(self.conn, width=1500, length=2000,
                      w_min=1400, w_max=1600, l_min=1900, l_max=2100, unit="台")
        rows = svc.list_pallets_by_product_dims(
            self.conn, product_width_text="1000", product_length_text="")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].width, 1020)

    def test_length_only_matches_by_tolerance(self):
        insert_pallet(self.conn, width=1000, length=2010,
                      w_min=900, w_max=1100, l_min=1900, l_max=2100, unit="台")
        insert_pallet(self.conn, width=1000, length=5000,
                      w_min=900, w_max=1100, l_min=4900, l_max=5100, unit="台")
        rows = svc.list_pallets_by_product_dims(
            self.conn, product_width_text="", product_length_text="2000")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].length, 2010)

    def test_both_dims_uses_the_precise_fit_check(self):
        """両方そろえば、単純な近似ではなく「載るか」の厳密判定を使う。"""
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700, unit="台")
        # 近似(±50mm)なら当たらない寸法でも、適合範囲に収まっていれば
        # 「載るパレット」として出る
        rows = svc.list_pallets_by_product_dims(
            self.conn, product_width_text="1000", product_length_text="2500")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].width, 1150)

    def test_neither_dim_returns_the_full_list(self):
        insert_pallet(self.conn, width=1000, length=2000,
                      w_min=900, w_max=1100, l_min=1900, l_max=2100, unit="台")
        rows = svc.list_pallets_by_product_dims(
            self.conn, product_width_text="", product_length_text="")
        self.assertEqual(len(rows), 1)

    def test_thickness_is_passed_through_when_both_dims_given(self):
        """入力中の一覧でも、両方そろえば5×10板厚フィルタが効く。"""
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="5×10", symbol="通常", unit="台")
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      industry="5×10", symbol="強度UP", unit="台")
        rows = svc.list_pallets_by_product_dims(
            self.conn, product_width_text="1000", product_length_text="2500",
            manufactured_thickness=5.0)
        self.assertEqual([r.symbol for r in rows], ["強度UP"])

    def test_a_pallet_smaller_than_the_product_is_excluded_even_if_the_fit_range_allows_it(self):
        # 適合範囲が壊れていて製品サイズを許してしまっていても、現物サイズが
        # 製品より小さければ載らない(board_selection_service の
        # 物理サイズ突き合わせと同じ考え方)
        insert_pallet(self.conn, width=900, length=2000,
                      w_min=200, w_max=2000, l_min=200, l_max=3000, unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500)
        self.assertEqual(rows, [])

    # -- EXオンリー (list_pallet_sizesと同じ意味づけ) -----------------------
    def test_ex_only_keeps_only_ex_rows_when_the_order_is_ex(self):
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      symbol="EXﾀｲﾄ", unit="台")
        insert_pallet(self.conn, width=1160, length=2660,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      symbol="ﾀｲﾄ", unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500,
            ex_only=True, is_ex_order=True)
        self.assertEqual([r.width for r in rows], [1150])

    def test_ex_only_has_no_effect_when_the_order_is_not_ex(self):
        """VBAのex_only_mode = is_ex_order and ex_only と同じ:
        EX受注でなければEXオンリーは無視する。"""
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      symbol="EXﾀｲﾄ", unit="台")
        insert_pallet(self.conn, width=1160, length=2660,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      symbol="ﾀｲﾄ", unit="台")
        rows = svc.list_pallets_for_product(
            self.conn, product_width=1000, product_length=2500,
            ex_only=True, is_ex_order=False)
        # EXオンリーは効かないが、EX記号自体はshow_all=False(既定)なので除外される
        self.assertEqual([r.width for r in rows], [1160])


class FindPalletRowTests(BoardSelectionTestCase):
    def test_finds_the_matching_row_regardless_of_display_filters(self):
        insert_pallet(self.conn, width=1150, length=2650,
                      w_min=900, w_max=1200, l_min=2400, l_max=2700,
                      symbol="EXﾀｲﾄ", unit="枚", code="C123")
        # 単位フィルタ・EX除外の対象になる行でも直接引ける
        row = svc.find_pallet_row(self.conn, 1150, 2650, "EXﾀｲﾄ")
        self.assertIsNotNone(row)
        self.assertEqual(row.code, "C123")
        self.assertEqual(row.unit, "枚")

    def test_returns_none_when_nothing_matches(self):
        self.assertIsNone(svc.find_pallet_row(self.conn, 1150, 2650, ""))


if __name__ == "__main__":
    unittest.main()
