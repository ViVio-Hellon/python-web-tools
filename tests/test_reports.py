"""帳票(Lot貼付用 / 切断依頼書)のテスト。"""
from __future__ import annotations

import unittest
from datetime import date

from packaging_tool import board_selection_algorithm as alg
from packaging_tool import reports
from packaging_tool.board_selection_algorithm import SelectedBoard


class FormatTests(unittest.TestCase):
    def test_thickness_always_two_decimals(self):
        self.assertEqual(reports.format_thickness(50), "50.00")
        self.assertEqual(reports.format_thickness(6.75), "6.75")

    def test_side_drops_the_decimal_when_whole(self):
        """VBA `IIf(v = Int(v), Format(v,"0"), Format(v,"0.0"))`。"""
        self.assertEqual(reports.format_side(1252), "1252")
        self.assertEqual(reports.format_side(2502.5), "2502.5")

    def test_product_size_text_matches_the_sample_sheet(self):
        self.assertEqual(reports.product_size_text(50.0, 1252.0, 2502.5),
                         "50.00 × 1252 × 2502.5")

    def test_position_l1_is_hyphenated(self):
        self.assertEqual(reports.format_position("L1"), "L-1")
        self.assertEqual(reports.format_position("HVC"), "HVC")

    def test_cut_request_title(self):
        self.assertEqual(reports.cut_request_title("L1", "プロテックボード"),
                         "L-1 プロテックボード切断依頼書")

    def test_cut_request_title_falls_back_to_board(self):
        self.assertEqual(reports.cut_request_title("L1", ""), "L-1 ボード切断依頼書")

    def test_hiki_header_depends_on_the_row_type(self):
        self.assertEqual(reports.hiki_header_for("adj"), "引当調整NO")
        self.assertEqual(reports.hiki_header_for("qty"), "引当数量")
        self.assertEqual(reports.hiki_header_for("zen"), "引当数量")
        self.assertEqual(reports.hiki_header_for(""), "引当数量/調整NO")


class HikiTextTests(unittest.TestCase):
    def test_each_row_is_three_lines_separated_by_a_blank_line(self):
        text = reports.build_hiki_text(
            "引当数量", [("4", "60629096", "06013015"), ("2", "60629097", "06015820")])
        self.assertEqual(text.split("\n\n"), [
            "引当数量：4\n引当番号：60629096\n受注番号：06013015",
            "引当数量：2\n引当番号：60629097\n受注番号：06015820",
        ])

    def test_no_rows(self):
        self.assertEqual(reports.build_hiki_text("引当数量", []), "(引当なし)")


def _label(**kw) -> reports.LabelData:
    base = dict(
        lot_no="H4085H0", thickness=50.0, width=1252.0, length=2502.5,
        yoto_code="H197", yoto_name="ｼﾔ-ｼ", zaishitsu="N17S", choshitsu="T351",
        delivery_name="納入先", customer_name="取引先", ship_to_name="送り先",
    )
    base.update(kw)
    return reports.LabelData(**base)


class LabelSheetTests(unittest.TestCase):
    def test_dimensions_and_lot_are_on_the_sheet(self):
        html = reports.build_label_sheet(_label())
        self.assertIn("50.00 × 1252 × 2502.5", html)
        self.assertIn("H4085H0", html)

    def test_wvc_only_when_front_and_back_match(self):
        self.assertIn("WVC", reports.build_label_sheet(
            _label(vc_front="V325NYE", vc_back="V325NYE")))
        self.assertNotIn("WVC", reports.build_label_sheet(
            _label(vc_front="V325NYE", vc_back="OTHER")))

    def test_circle_k_when_no_vc(self):
        """VC表が空なら「K」の丸印を描く(VBA `DrawCircleK`)。"""
        html = reports.build_label_sheet(_label(vc_front=""))
        self.assertIn("circle-k", html)
        self.assertNotIn("circle-k", reports.build_label_sheet(_label(vc_front="V1")))

    def test_unknown_caption_is_treated_as_blank(self):
        """VBA `NvCaption`: "---" は未取得なので空扱い → 丸印になる。"""
        self.assertIn("circle-k", reports.build_label_sheet(_label(vc_front="---")))

    def test_pallet_line_has_industry_and_symbol(self):
        html = reports.build_label_sheet(_label(
            pallet_width="1265", pallet_length="2515",
            pallet_industry="タイト", pallet_symbol="C1"))
        self.assertIn("タイト C1", html)
        self.assertIn("1265 × 2515", html)

    def test_pallet_line_without_industry(self):
        html = reports.build_label_sheet(_label(
            pallet_width="1265", pallet_length="2515"))
        self.assertIn("1265 × 2515", html)

    def test_package_count_uses_the_pallet_unit(self):
        html = reports.build_label_sheet(_label(total_packages=4, pallet_unit="組"))
        self.assertIn("4組", html)

    def test_zero_packages_is_not_printed(self):
        self.assertNotIn('class="pkg"',
                         reports.build_label_sheet(_label(total_packages=0)))

    def test_1p0113_replaces_the_pallet_line_and_hides_the_count(self):
        html = reports.build_label_sheet(_label(
            is_1p0113=True, total_packages=4,
            kakuzai_count_caption="溝切角材: 16本", kakuzai_size="1262mm",
            matsuita_count_caption="松板: 8枚", matsuita_size="2330mm",
            kakuzai_cell="溝切角材\n75x75 丈:1001-1999\nコード:251023\n単位:本",
            matsuita_cell="松板\n24x120 丈:2000-2999\nコード:355045\n単位:枚"))
        self.assertIn("裸梱包", html)
        self.assertIn("溝切角材: 16本 (1262mm)", html)
        self.assertIn("松板: 8枚 (2330mm)", html)
        # 梱包数の行は出さない
        self.assertNotIn('class="pkg"', html)
        # 右半分は単位/コードではなく資材の明細になる
        self.assertNotIn("単位:</td>", html)
        self.assertIn("コード:251023", html)

    def test_normal_mode_shows_unit_and_code(self):
        html = reports.build_label_sheet(_label(pallet_unit="台", pallet_code="058901"))
        self.assertIn("058901", html)

    def test_hiki_font_shrinks_past_two_rows(self):
        two = reports.build_label_sheet(_label(
            hiki_rows=[("1", "a", "b"), ("2", "c", "d")]))
        three = reports.build_label_sheet(_label(
            hiki_rows=[("1", "a", "b"), ("2", "c", "d"), ("3", "e", "f")]))
        self.assertIn("font-size:9pt", two)
        self.assertIn("font-size:7pt", three)

    def test_comments_are_colour_coded(self):
        html = reports.build_label_sheet(_label(
            delivery_comment="納送", factory_comment="工場"))
        self.assertIn('class="val deliv"', html)
        self.assertIn('class="val factory"', html)

    def test_values_are_escaped(self):
        html = reports.build_label_sheet(_label(factory_comment="<script>"))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_report_is_landscape_with_no_margin(self):
        report = reports.build_label_report(_label())
        css = report.setup.to_css()
        self.assertIn("A4 landscape", css)
        self.assertIn("margin: 0.0mm", css)


class GetCutSizeInfoTests(unittest.TestCase):
    """VBA `GetCutSizeInfo`。幅カット/丈カットの記録から切断依頼の行を作る。"""

    def setUp(self) -> None:
        self.board = SelectedBoard(width=1250, length=2500, count=2, tag="")

    def test_no_cut_records_means_nothing_to_request(self):
        out = reports.get_cut_size_info([self.board], "上用", 1221, 2440, {}, {}, {})
        self.assertEqual(out.size_width_only, "")
        self.assertEqual(out.size_both, "")

    def test_width_cut_only(self):
        out = reports.get_cut_size_info(
            [self.board], "上用", 1221, 2440, {"1250x2500": 2500}, {}, {})
        # 幅は製品幅まで落とす。丈は配置時の有効丈をそのまま使う
        self.assertEqual(out.size_width_only, "1221x2500")
        self.assertEqual(out.count_width_only, 2)
        self.assertEqual(out.orig_width_only, "1250×2500")
        self.assertEqual(out.size_both, "")

    def test_length_cut_only(self):
        out = reports.get_cut_size_info(
            [self.board], "上用", 1221, 2440,
            {}, {"U_1250x2500": 1250}, {"U_1250x2500": 1})
        # 幅カットが無いので切断幅は有効幅のまま。丈は はみ出し分だけ短く
        # (2500x2枚=5000 - 製品丈2440 = 2560 はみ出し → 2500-2560 は負なので0)
        self.assertEqual(out.size_both, "1250x0")
        self.assertEqual(out.count_both, 1)
        # 幅カットが無いので「幅カットのみ」枠は空のまま
        self.assertEqual(out.size_width_only, "")

    def test_both_cuts_split_the_count(self):
        board = SelectedBoard(width=1250, length=2500, count=1, tag="")
        out = reports.get_cut_size_info(
            [board], "上用", 1221, 2440,
            {"1250x2500": 2500}, {"U_1250x2500": 1250}, {"U_1250x2500": 1})
        # 1枚しかないので全部が丈カット行、幅カットのみ枠は0枚で出さない
        self.assertEqual(out.count_both, 1)
        self.assertEqual(out.count_width_only, 0)
        self.assertEqual(out.size_both, "1221x2440")

    def test_fill_boards_are_skipped(self):
        fill = SelectedBoard(width=50, length=2500, count=1, tag="幅補填")
        out = reports.get_cut_size_info(
            [fill], "上用", 1221, 2440, {"50x2500": 2500}, {}, {})
        self.assertEqual(out.size_width_only, "")

    def test_lower_uses_the_l_prefix(self):
        out = reports.get_cut_size_info(
            [self.board], "下用", 1265, 2515,
            {}, {"L_1250x2500": 1250}, {"L_1250x2500": 1})
        self.assertEqual(out.count_both, 1)
        # 上用のキーでは引っかからない
        self.assertEqual(
            reports.get_cut_size_info([self.board], "下用", 1265, 2515,
                                      {}, {"U_1250x2500": 1250}, {}).count_both, 0)

    def test_first_match_of_each_kind_wins(self):
        b1 = SelectedBoard(width=1250, length=2500, count=2, tag="")
        b2 = SelectedBoard(width=1000, length=2000, count=3, tag="")
        out = reports.get_cut_size_info(
            [b1, b2], "上用", 900, 2440,
            {"1250x2500": 2500, "1000x2000": 2000}, {}, {})
        self.assertEqual(out.orig_width_only, "1250×2500")

    def test_use_len_cut_false_folds_length_cut_into_width_only(self):
        # 丈カットなし指定: 丈カットせず全枚数を幅カットのみへ寄せる。
        # サイズは丈カット前の実効丈(2500)のまま
        board = SelectedBoard(width=1250, length=2500, count=1, tag="")
        out = reports.get_cut_size_info(
            [board], "上用", 1221, 2440,
            {"1250x2500": 2500}, {"U_1250x2500": 1250}, {"U_1250x2500": 1},
            use_len_cut=False)
        self.assertEqual(out.size_both, "")
        self.assertEqual(out.count_both, 0)
        self.assertEqual(out.size_width_only, "1221x2500")
        self.assertEqual(out.count_width_only, 1)

    def test_use_len_cut_false_without_width_cut_needs_no_request(self):
        # 幅カットも無ければ丈カットをしない以上、カット自体が不要
        board = SelectedBoard(width=1250, length=2500, count=1, tag="")
        out = reports.get_cut_size_info(
            [board], "上用", 1221, 2440,
            {}, {"U_1250x2500": 1250}, {"U_1250x2500": 1}, use_len_cut=False)
        self.assertEqual(out.size_width_only, "")
        self.assertEqual(out.count_width_only, 0)

    def test_use_len_cut_false_merges_matching_sizes_across_boards(self):
        board1 = SelectedBoard(width=1250, length=2500, count=2, tag="")
        board2 = SelectedBoard(width=1250, length=2500, count=1, tag="")
        out = reports.get_cut_size_info(
            [board1, board2], "上用", 1221, 2440,
            {"1250x2500": 2500}, {"U_1250x2500": 1250}, {"U_1250x2500": 1},
            use_len_cut=False)
        self.assertEqual(out.size_width_only, "1221x2500")
        self.assertEqual(out.count_width_only, 3)


class ProtecCutSizeTests(unittest.TestCase):
    """`protec_cut_size_info` は `ProtecCutResult`(選定の確定値)を

    そのまま表示するだけで、ここでは幅カット・丈カットの判定を
    やり直さない(全面書き換え。以前は `placedBoards` から寸法を
    逆算し、`GetBestOrientation` で独自に再判定していた)。
    """

    def test_width_cut_uses_the_1p1216_tolerance(self):
        pr = alg.decide_protec_orientation(1250, 1250, 1100, is_1p1216=True)
        pr.count = 2
        out = reports.protec_cut_size_info(pr)
        # 有効幅1250 > 製品幅1100 → 幅カット。1100 - 10 = 1090
        self.assertTrue(out.size_width_only.startswith("1090x"))
        self.assertEqual(out.count_width_only, 2)

    def test_other_protec_uses_80mm(self):
        pr = alg.decide_protec_orientation(1250, 1250, 1100, is_1p1216=False)
        pr.count = 2
        out = reports.protec_cut_size_info(pr)
        self.assertTrue(out.size_width_only.startswith("1020x"))

    def test_no_cut_needed_when_width_fits(self):
        """有効幅が製品幅以内なら幅カット自体が発生しない。"""
        pr = alg.decide_protec_orientation(1090, 1250, 1100, is_1p1216=True)
        pr.count = 2
        out = reports.protec_cut_size_info(pr)
        self.assertFalse(pr.need_cut)
        self.assertEqual(out.size_width_only, "")

    def test_no_length_cut_when_not_flagged(self):
        """`need_length_cut=False`(選定が丈カット不要と判定済み)なら

        `size_both` は空のまま。
        """
        pr = alg.decide_protec_orientation(1250, 1250, 1100, is_1p1216=True)
        pr.count = 3
        out = reports.protec_cut_size_info(pr)
        self.assertEqual(out.size_both, "")
        self.assertEqual(out.count_both, 0)

    def test_length_cut_appears_in_size_both(self):
        """丈カットが要る(`need_length_cut=True`)なら、最後の1枚は

        `size_both` に、残りの枚数は `size_width_only` に振り分ける
        (`get_cut_size_info` [通常モード]と同じ2枠構成)。MAP画面の
        カット線(`recalc_length_cut_info`)と同じ確定値を見るので、
        画面と帳票が食い違わない。
        """
        pr = alg.ProtecCutResult(
            valid=True, orig_width=1090, orig_length=1000, cut_eff_width=1090,
            eff_length=1000, need_cut=False, need_length_cut=True,
            length_cut_eff=910, count=3)
        out = reports.protec_cut_size_info(pr)
        self.assertEqual(out.size_both, "1090x910")
        self.assertEqual(out.count_both, 1)
        # 幅カットは不要なので、残り2枚は「幅カットのみ」欄には出さない
        self.assertEqual(out.size_width_only, "")

    def test_width_and_length_cut_both_appear(self):
        """幅カットと丈カットが両方要るとき、両方の欄が埋まる。"""
        pr = alg.ProtecCutResult(
            valid=True, orig_width=1250, orig_length=1000, cut_eff_width=1020,
            eff_length=1000, need_cut=True, need_length_cut=True,
            length_cut_eff=910, count=3)
        out = reports.protec_cut_size_info(pr)
        self.assertEqual(out.size_both, "1020x910")
        self.assertEqual(out.count_both, 1)
        self.assertEqual(out.size_width_only, "1020x1000")
        self.assertEqual(out.count_width_only, 2)

    def test_invalid_result_yields_empty_info(self):
        """`valid=False`(適合する在庫が無い)なら何も出さない。"""
        out = reports.protec_cut_size_info(alg.ProtecCutResult(valid=False))
        self.assertEqual(out.size_both, "")
        self.assertEqual(out.size_width_only, "")

    def test_use_len_cut_false_merges_length_cut_into_width_only(self):
        # 丈カットなし指定: 丈カット分(1枚)を幅カットのみの枚数へ合算する
        pr = alg.ProtecCutResult(
            valid=True, orig_width=1250, orig_length=1000, cut_eff_width=1020,
            eff_length=1000, need_cut=True, need_length_cut=True,
            length_cut_eff=910, count=3)
        out = reports.protec_cut_size_info(pr, use_len_cut=False)
        self.assertEqual(out.size_both, "")
        self.assertEqual(out.count_both, 0)
        self.assertEqual(out.size_width_only, "1020x1000")
        self.assertEqual(out.count_width_only, 3)

    def test_use_len_cut_false_without_width_cut_needs_no_request(self):
        # 幅カットも無ければ丈カットをしない以上、カット自体が不要
        pr = alg.ProtecCutResult(
            valid=True, orig_width=1090, orig_length=1000, cut_eff_width=1090,
            eff_length=1000, need_cut=False, need_length_cut=True,
            length_cut_eff=910, count=3)
        out = reports.protec_cut_size_info(pr, use_len_cut=False)
        self.assertEqual(out.size_width_only, "")
        self.assertEqual(out.count_width_only, 0)
        self.assertEqual(out.size_both, "")

    def test_use_len_cut_true_is_unaffected(self):
        pr = alg.ProtecCutResult(
            valid=True, orig_width=1250, orig_length=1000, cut_eff_width=1020,
            eff_length=1000, need_cut=True, need_length_cut=True,
            length_cut_eff=910, count=3)
        out = reports.protec_cut_size_info(pr, use_len_cut=True)
        self.assertEqual(out.size_both, "1020x910")
        self.assertEqual(out.count_both, 1)
        self.assertEqual(out.size_width_only, "1020x1000")
        self.assertEqual(out.count_width_only, 2)


def _cut(**kw) -> reports.CutRequestData:
    base = dict(lot_no="H4085H0", position="L1", board_type="ハードボード",
                thickness=50.0, width=1252.0, length=2502.5,
                pallet_width="1265", pallet_length="2515", total_packages=4,
                request_date=date(2026, 7, 28))
    base.update(kw)
    return reports.CutRequestData(**base)


class CutRequestSheetTests(unittest.TestCase):
    def test_headings_match_the_sample_sheet(self):
        html = reports.build_cut_request_sheet(_cut())
        for caption in ("ロットNO", "担当者", "製品サイズ", "パレットサイズ",
                        "切断サイズ", "枚数", "依頼日", "期限日"):
            self.assertIn(caption, html)

    def test_values_are_placed(self):
        html = reports.build_cut_request_sheet(_cut())
        self.assertIn("H4085H0", html)
        self.assertIn("50.00 × 1252 × 2502.5", html)
        self.assertIn("1265 × 2515", html)
        self.assertIn("2026/07/28", html)

    def test_normal_mode_labels_upper_and_lower(self):
        html = reports.build_cut_request_sheet(_cut())
        self.assertIn(">上</td>", html)
        self.assertIn(">下</td>", html)

    def test_protec_mode_has_no_upper_lower_labels(self):
        html = reports.build_cut_request_sheet(_cut(is_protec=True))
        self.assertNotIn(">上</td>", html)
        self.assertNotIn(">下</td>", html)

    def test_total_multiplies_by_the_package_count(self):
        html = reports.build_cut_request_sheet(_cut(
            upper=reports.CutSizeInfo(size_width_only="1221x2500", count_width_only=2)))
        self.assertIn("1梱2枚", html)
        self.assertIn(">8</div>", html)   # 2枚 × 4梱包

    def test_total_falls_back_to_the_count_without_packages(self):
        html = reports.build_cut_request_sheet(_cut(
            total_packages=0,
            upper=reports.CutSizeInfo(size_width_only="1221x2500", count_width_only=2)))
        self.assertIn(">2</div>", html)

    def test_original_size_is_shown_only_in_normal_mode(self):
        info = reports.CutSizeInfo(size_width_only="1221x2500", count_width_only=2,
                                   orig_width_only="1250×2500")
        self.assertIn("（元: 1250×2500）",
                      reports.build_cut_request_sheet(_cut(upper=info)))
        self.assertNotIn("（元:",
                         reports.build_cut_request_sheet(_cut(upper=info, is_protec=True)))

    def test_report_is_portrait_with_1cm_margins(self):
        css = reports.build_cut_request_report(_cut()).setup.to_css()
        self.assertIn("A4 portrait", css)
        self.assertIn("margin: 10.0mm", css)


if __name__ == "__main__":
    unittest.main()
