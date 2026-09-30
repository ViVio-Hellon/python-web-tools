"""帳票(Lot貼付用 / 切断依頼書)のテスト。"""
from __future__ import annotations

import unittest
from datetime import date

from packaging_tool import board_selection_algorithm as alg
from packaging_tool import printing, reports
from packaging_tool.board_selection_algorithm import SelectedBoard
from packaging_tool.models import PlacedBoardModel


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
        self.assertEqual(reports.cut_request_title("プロテックボード"),
                         "プロテックボード切断依頼書")

    def test_cut_request_title_falls_back_to_board(self):
        self.assertEqual(reports.cut_request_title(""), "ボード切断依頼書")

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
        self.assertIn(">4</span>組", html)

    def test_zero_packages_leaves_a_blank_to_write_in(self):
        """**台数が確定できないことがある。**

        以前は0なら欄ごと出していなかったが、それだと現場が手で
        書き足す場所も無い(現場の声:「台数が確定できないとき」に
        直せるようにしてほしい)。空の欄と単位だけ出す。
        """
        html = reports.build_label_sheet(_label(total_packages=0))
        self.assertIn('class="pkg"', html)
        self.assertIn('data-edit="pkg"', html)
        self.assertIn('data-edit="pkg" data-placeholder="—"></span>台', html)

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

    def test_report_is_landscape_inside_the_printable_area(self):
        """VBA は余白0。プリンターは縁から約4mmに印刷できず、縁が欠けていた。"""
        report = reports.build_label_report(_label())
        css = report.setup.to_css()
        self.assertIn("A4 landscape", css)
        self.assertIn(f"margin: {printing.SAFE_MARGIN_MM}mm", css)
        self.assertGreaterEqual(printing.SAFE_MARGIN_MM, 5.0)


def _pb(w, l, x=0, y=0, *, ow=None, ol=None, cat="上用", fill=False):
    """配置された板。`ow`/`ol` は元寸法(省略時は配置寸法=切っていない)。"""
    return PlacedBoardModel(width=w, length=l, x=x, y=y, board_category=cat,
                            original_width=w if ow is None else ow,
                            original_length=l if ol is None else ol,
                            is_fill_board=fill)


class GetCutSizeInfoTests(unittest.TestCase):
    """VBA `GetCutSizeInfo`(全面差し替え版)。

    対象はカット前提のサイズだけ、枚数と寸法は**実際の配置**から数える。
    以前は選定のカット辞書から計算し直していたので、別案で配置すると
    直前の通常選定のカットが載っていた(課題表 1・2)。
    """

    def setUp(self) -> None:
        self.cut = SelectedBoard(width=1250, length=2500, count=2, tag="カット前提")
        self.main = SelectedBoard(width=1000, length=2000, count=1, tag="主")

    def test_カット前提が無ければ何も載らない(self):
        placed = [_pb(1201, 2440, ow=1250, ol=2500)]
        out = reports.get_cut_size_info([self.main], placed, "上用", 1221, 2440)
        self.assertEqual((out.size_width_only, out.size_both), ("", ""))

    def test_幅は配置された板の実幅(self):
        # 上用の定義は 製品幅−20 = 1201。配置側が切った幅がそのまま載る
        placed = [_pb(1201, 2440, ow=1250, ol=2440)]
        cut = SelectedBoard(width=1250, length=2440, count=1, tag="カット前提")
        out = reports.get_cut_size_info([cut], placed, "上用", 1221, 2440)
        self.assertEqual(out.size_width_only, "1201x2440")
        self.assertEqual(out.count_width_only, 1)
        self.assertEqual(out.orig_width_only, "1250×2440")

    def test_同じサイズでも切らない板は数えない(self):
        # 別案では同じサイズでも切るのは行の最後の1枚だけ(課題表 5)
        placed = [_pb(1000, 2440, ow=1000, ol=2440),
                  _pb(201, 2440, y=1000, ow=1000, ol=2440)]
        cut = SelectedBoard(width=1000, length=2440, count=1, tag="カット前提")
        out = reports.get_cut_size_info([cut], placed, "上用", 1221, 2440)
        self.assertEqual(out.size_width_only, "201x2440")
        self.assertEqual(out.count_width_only, 1)

    def test_丈はみ出しは丈カットあり枠(self):
        # 2枚目が 2500..5000 に置かれ、基準丈 4000 を 1000 はみ出す
        placed = [_pb(1201, 2500, ow=1250, ol=2500),
                  _pb(1201, 2500, x=2500, ow=1250, ol=2500)]
        out = reports.get_cut_size_info([self.cut], placed, "上用", 1221, 4000)
        self.assertEqual((out.size_width_only, out.count_width_only), ("1201x2500", 1))
        self.assertEqual((out.size_both, out.count_both), ("1201x1500", 1))

    def test_丈カットなし指定は丈を切らず幅カットのみへ(self):
        placed = [_pb(1201, 2500, ow=1250, ol=2500),
                  _pb(1201, 2500, x=2500, ow=1250, ol=2500)]
        out = reports.get_cut_size_info([self.cut], placed, "上用", 1221, 4000,
                                        use_len_cut=False)
        self.assertEqual((out.size_width_only, out.count_width_only), ("1201x2500", 2))
        self.assertEqual(out.size_both, "")

    def test_丈カットなし指定で幅も切らなければ載らない(self):
        placed = [_pb(1250, 2500, ow=1250, ol=2500),
                  _pb(1250, 2500, x=2500, ow=1250, ol=2500)]
        out = reports.get_cut_size_info([self.cut], placed, "上用", 1300, 4000,
                                        use_len_cut=False)
        self.assertEqual((out.size_width_only, out.size_both), ("", ""))

    def test_補填ボードと別の区分は数えない(self):
        placed = [_pb(1201, 2500, ow=1250, ol=2500, fill=True),
                  _pb(1201, 2500, ow=1250, ol=2500, cat="下用")]
        out = reports.get_cut_size_info([self.cut], placed, "上用", 1221, 2500)
        self.assertEqual(out.size_width_only, "")

    def test_向きが違っても同じサイズとして扱う(self):
        # 選定リストは 1250×2500、配置は回転して置かれている
        # (元寸法が 2500×1250 の向きで記録されている)
        placed = [_pb(1201, 2500, ow=2500, ol=1250)]
        out = reports.get_cut_size_info([self.cut], placed, "上用", 1221, 2500)
        self.assertEqual(out.size_width_only, "1201x2500")
        self.assertEqual(out.orig_width_only, "1250×2500")

    def test_枠に別サイズが来たら警告して最初だけ載せる(self):
        c2 = SelectedBoard(width=1000, length=2500, count=1, tag="カット前提")
        placed = [_pb(1201, 2500, ow=1250, ol=2500),
                  _pb(901, 2500, y=1201, ow=1000, ol=2500)]
        warnings: list[str] = []
        out = reports.get_cut_size_info([self.cut, c2], placed, "上用", 1221, 2500,
                                        warn=warnings.append)
        self.assertEqual((out.size_width_only, out.count_width_only), ("1201x2500", 1))
        self.assertEqual(len(warnings), 1)
        self.assertIn("幅カットのみに別サイズの板があります", warnings[0])
        self.assertIn("1201x2500 だけ載ります", warnings[0])


class CheckCutTargetsTests(unittest.TestCase):
    """VBA `CheckCutTargets`。対象を数え、対象外のはみ出しをログ用に並べる(課題表 4)。"""

    def test_カット前提で切る板だけが対象(self):
        cut = SelectedBoard(width=1250, length=2500, count=1, tag="カット前提")
        placed = [_pb(1201, 2500, ow=1250, ol=2500)]
        t = reports.check_cut_targets([cut], placed, "上用", 1221, 2500)
        self.assertEqual((t.count, t.others), (1, []))

    def test_カット前提でも切らない板は対象外にも入らない(self):
        cut = SelectedBoard(width=1250, length=2500, count=1, tag="カット前提")
        placed = [_pb(1250, 2500)]
        t = reports.check_cut_targets([cut], placed, "上用", 1300, 2500)
        self.assertEqual((t.count, t.others), (0, []))

    def test_カット前提でないはみ出しは同じ内容ごとにまとめる(self):
        main = SelectedBoard(width=1300, length=2000, count=2, tag="主")
        placed = [_pb(1300, 2000), _pb(1300, 2000, x=2000)]
        t = reports.check_cut_targets([main], placed, "上用", 1221, 4000)
        self.assertEqual(t.count, 0)
        self.assertEqual(t.others, [
            "[切断依頼対象外] 上用 1300x2000 2枚: 幅はみ出し79mm"
            "(カット前提ではないため依頼しません)"])

    def test_補填ボードは数えない(self):
        placed = [_pb(1300, 2000, fill=True)]
        t = reports.check_cut_targets([], placed, "上用", 1221, 1000)
        self.assertEqual((t.count, t.others), (0, []))


class SkuKeyTests(unittest.TestCase):
    def test_短辺x長辺(self):
        self.assertEqual(reports.sku_key_min_max(2500, 1250), "1250x2500")
        self.assertEqual(reports.sku_key_min_max(1250, 2500), "1250x2500")


class ProtecCutSizeTests(unittest.TestCase):
    """`protec_cut_size_info` は `ProtecCutResult`(選定の確定値)を

    そのまま表示するだけで、ここでは幅カット・丈カットの判定を
    やり直さない(全面書き換え。以前は `placedBoards` から寸法を
    逆算し、`GetBestOrientation` で独自に再判定していた)。

    **枚数だけは2倍になる**(`reports.PROTEC_SETS`)。プロテックは
    上用=下用と同サイズを強制コピーする仕様で、確定値が持っているのは
    1セット分だから ── 現物は上下2セット切り出すことになる。
    """

    def test_width_cut_uses_the_1p1216_tolerance(self):
        pr = alg.decide_protec_orientation(1250, 1250, 1100, is_1p1216=True)
        pr.count = 2
        out = reports.protec_cut_size_info(pr)
        # 有効幅1250 > 製品幅1100 → 幅カット。1100 - 10 = 1090
        self.assertTrue(out.size_width_only.startswith("1090x"))
        self.assertEqual(out.count_width_only, 4, "上下2セット分")

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
        self.assertEqual(out.count_both, 2, "上下2セット分")
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
        self.assertEqual(out.count_both, 2, "上下2セット分")
        self.assertEqual(out.size_width_only, "1020x1000")
        self.assertEqual(out.count_width_only, 4, "上下2セット分")

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
        self.assertEqual(out.count_width_only, 6, "3枚 × 上下2セット")

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
        self.assertEqual(out.count_both, 2, "上下2セット分")
        self.assertEqual(out.size_width_only, "1020x1000")
        self.assertEqual(out.count_width_only, 4, "上下2セット分")

    # --- 任意カット(製品丈超・パレット内) -------------------------
    def _optional(self) -> alg.ProtecCutResult:
        """製品丈は超えるが、パレット丈には収まる状態。

        通常2枚(=切らない姿)。切るなら 通常1枚 + 800mmへ切る1枚。
        """
        return alg.ProtecCutResult(
            valid=True, orig_width=1250, orig_length=1000, cut_eff_width=1020,
            eff_length=1000, need_cut=True, need_length_cut=False, count=2,
            len_normal_cnt=2, len_cut_cnt=0,
            len_cut_optional=True, len_opt_normal_cnt=1, len_opt_cut_cnt=1,
            len_opt_cut_eff=800)

    def test_optional_cut_yes_uses_the_optional_breakdown(self):
        """「切る」を選んだら、依頼書だけがカットありの内訳になる。

        確定値(`need_length_cut=False`)はそのままなので、**配置図は
        切らない姿のまま**。切るかどうかは現場が選ぶことで、図の
        前提ではない。
        """
        out = reports.protec_cut_size_info(self._optional(), use_len_cut=True)
        self.assertEqual(out.size_both, "1020x800")
        self.assertEqual(out.count_both, 2, "1枚 × 上下2セット")
        self.assertEqual(out.size_width_only, "1020x1000")
        self.assertEqual(out.count_width_only, 2, "1枚 × 上下2セット")

    def test_optional_cut_no_keeps_the_full_length(self):
        """「切らない」を選んだら、全枚数が幅カットのみの欄に出る。"""
        out = reports.protec_cut_size_info(self._optional(), use_len_cut=False)
        self.assertEqual(out.size_both, "")
        self.assertEqual(out.count_both, 0)
        self.assertEqual(out.size_width_only, "1020x1000")
        self.assertEqual(out.count_width_only, 4, "2枚 × 上下2セット")

    def test_breakdown_falls_back_to_the_count(self):
        """内訳を持たない確定値でも枚数が0にならない。

        依頼書の枚数が黙って0になるのは、**カットが要らないのと
        見分けが付かない**いちばん危ない壊れ方(`reports._len_split`)。
        """
        pr = alg.ProtecCutResult(
            valid=True, orig_width=1250, orig_length=1000, cut_eff_width=1020,
            eff_length=1000, need_cut=True, need_length_cut=False, count=3)
        out = reports.protec_cut_size_info(pr)
        self.assertEqual(out.count_width_only, 6, "3枚 × 上下2セット")


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
        self.assertIn(">8</span>", html)   # 2枚 × 4梱包(直せる欄の中)

    def test_total_falls_back_to_the_count_without_packages(self):
        html = reports.build_cut_request_sheet(_cut(
            total_packages=0,
            upper=reports.CutSizeInfo(size_width_only="1221x2500", count_width_only=2)))
        self.assertIn(">2</span>", html)

    def test_original_size_is_shown_in_both_modes(self):
        """切断サイズには元のサイズ(切る前の板)も書く。プロテックでも(現場の声)。

        以前はプロテックだけ出していなかった。"""
        info = reports.CutSizeInfo(size_width_only="1257x1030", count_width_only=4,
                                   orig_width_only="1030×1520")
        for is_protec in (False, True):
            with self.subTest(is_protec=is_protec):
                html = reports.build_cut_request_sheet(_cut(upper=info, is_protec=is_protec))
                self.assertIn("（元: ", html)
                self.assertIn("1030×1520", html)

    def test_original_size_sits_right_after_the_cut_size(self):
        """元のサイズは下の行ではなく、切断サイズの後ろ(同じ行)に書く。"""
        info = reports.CutSizeInfo(size_width_only="1257x1030", count_width_only=4,
                                   orig_width_only="1030×1520")
        html = reports.build_cut_request_sheet(_cut(upper=info))
        line = html[html.index("1257x1030"):]
        line = line[:line.index("</div>")]          # 同じ1行(.cutline)の中
        self.assertIn("1030×1520", line)
        self.assertIn("1梱4枚", line)

    def test_original_size_can_be_edited_and_added_on_empty_rows(self):
        """「幅x丈」の空欄(手で足す行)は残す。元のサイズも手で書き添えられる。"""
        info = reports.CutSizeInfo(size_width_only="1257x1030", count_width_only=4,
                                   orig_width_only="1030×1520")
        html = reports.build_cut_request_sheet(_cut(upper=info))
        self.assertIn('data-edit="cut0_w_orig"', html)
        self.assertIn('data-edit="cut0_b_size" data-placeholder="幅x丈"', html)
        self.assertIn('data-edit="cut0_b_orig" data-placeholder="元のサイズ"', html)
        # 直した元のサイズが紙面に出る
        edited = reports.build_cut_request_sheet(
            _cut(upper=info, edits={"cut0_w_orig": "1030×1600"}))
        self.assertIn("1030×1600", edited)

    def test_protec_count_says_upper_and_lower(self):
        """プロテックは上下2セットぶんなので「上下1梱N枚」(VBA の紙面と同じ)。"""
        info = reports.CutSizeInfo(size_width_only="1257x1030", count_width_only=4,
                                   orig_width_only="1030×1520")
        self.assertIn("上下1梱4枚", reports.build_cut_request_sheet(_cut(upper=info, is_protec=True)))
        self.assertNotIn("上下1梱", reports.build_cut_request_sheet(_cut(upper=info)))

    def test_report_is_portrait_with_1cm_margins(self):
        css = reports.build_cut_request_report(_cut()).setup.to_css()
        self.assertIn("A4 portrait", css)
        self.assertIn("margin: 10.0mm", css)


if __name__ == "__main__":
    unittest.main()


class CutFactsFromPlacementTests(unittest.TestCase):
    """VBA `AddCutInfoForCategory`。別案の配置からカット辞書を作り直す。"""

    def test_丈カットは区分の接頭辞と枚数を持つ(self):
        from packaging_tool.selection_records import cut_facts_from_placement
        upper = [SelectedBoard(width=1250, length=2500, count=2, tag="カット前提")]
        lower = [SelectedBoard(width=1250, length=2500, count=2, tag="主")]
        placed = [_pb(1201, 2500, ow=1250, ol=2500),
                  _pb(1201, 2500, x=2500, ow=1250, ol=2500),
                  _pb(1250, 2500, x=2500, cat="下用")]      # 主は対象外
        facts = cut_facts_from_placement(upper, lower, placed, 4000, 4000)
        self.assertEqual(facts.cut_info, {"1250x2500": 2500})
        self.assertEqual(facts.length_cut_info, {"U_1250x2500": 1201})
        self.assertEqual(facts.length_cut_count, {"U_1250x2500": 1})
        self.assertFalse(facts.narrow_lower or facts.narrow_upper)
