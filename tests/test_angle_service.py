"""アングル選定・疲労度計算(angle_service)のユニットテスト。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import angle_service as svc
from packaging_tool import db


class FatigueFormulaTests(unittest.TestCase):
    def test_fat_dist_score_caps_at_50(self):
        self.assertAlmostEqual(svc.fat_dist_score(500), 25.0)
        self.assertEqual(svc.fat_dist_score(999999), svc.FAT_SCORE_CAP)

    def test_fat_area_score1_caps(self):
        self.assertEqual(svc.fat_area_score1(3000, 3000), svc.FAT_SCORE_CAP)
        self.assertAlmostEqual(svc.fat_area_score1(1500, 1500), 12.5)

    def test_fat_cut_score1_zero_when_non_positive(self):
        self.assertEqual(svc.fat_cut_score1(0), 0.0)
        self.assertEqual(svc.fat_cut_score1(-5), 0.0)

    def test_fat_combine(self):
        # d + a1*sheetCnt + wc1*sheetCnt + lc1
        self.assertAlmostEqual(svc.fat_combine(10, 2, 3, 4, 5), 10 + 2 * 5 + 3 * 5 + 4)
        self.assertAlmostEqual(svc.fat_combine(10, 2, 3, 4, 0), 10 + 2 * 1 + 3 * 1 + 4)  # sheetCnt<1は1に矯正


class SelectAnglesTests(unittest.TestCase):
    def test_step1_exact_cover_within_tolerance(self):
        # productLen=1000, tolerance=max(50,30)=50 -> 1000~1050の範囲で最小超過を選ぶ
        result = svc.select_angles(1000, [1040, 1200, 900], 0, 0)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.angle1, 1040)
        self.assertFalse(result.need_cut)

    def test_step2_cut_when_short_product(self):
        # productLen=800 (<2500) だがStep1範囲(800~830)に候補が無ければStep2でカット
        result = svc.select_angles(800, [1200], 0, 0)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.angle1, 1200)
        self.assertTrue(result.need_cut)

    def test_step3_symmetric_pair(self):
        # productLen=3000(>=2500なのでStep2はスキップされる), target=1500。
        # 1500ぴったりの候補があれば対称2本
        result = svc.select_angles(3000, [1500, 5000], 0, 0)
        self.assertEqual(result.count, 2)
        self.assertEqual((result.angle1, result.angle2), (1500, 1500))
        self.assertEqual(result.overlap, 0)

    def test_step3_asymmetric_branch_is_unreachable(self):
        # 発見事項: target2=ceil(productLen/2)であるため、Step3②(異サイズ2本)が
        # 実際に採用されることは数学的にありえない
        # (どの候補もtarget2未満なら、2本合計もproductLenに届かないため)。
        # Step3①が失敗する入力(全候補<target2)を与えると、Step3②を素通りして
        # Step3c以降にフォールスルーすることを確認する。
        result = svc.select_angles(3000, [1400, 1200, 100], 0, 0)
        self.assertNotEqual(result.count, 0)
        self.assertNotIn("2本 ", result.info)  # Step3②(準対称)の文言ではないこと

    def test_step3c_even_pieces(self):
        # 5000mm以上なら最大6本まで試行。均等割りで見つかるケース
        result = svc.select_angles(6000, [1000], 0, 0)
        self.assertEqual(result.count, 6)
        self.assertEqual(result.total_len, 6000)
        self.assertEqual(result.overlap, 0)

    def test_step4_fallback_when_min_len_excludes_candidates(self):
        # 脚間隔から算出されるminLenにより、Step3①②③cは450/470mmの候補を
        # 全て除外して失敗するが、Step4(minLen制約なし)だけが450mm×2で成立する
        result = svc.select_angles(900, [450, 470], 1000, 3)
        self.assertEqual(result.count, 2)
        self.assertEqual((result.angle1, result.angle2), (450, 450))
        self.assertEqual(result.total_len, 900)
        self.assertEqual(result.overlap, 0)

    def test_no_candidates_returns_empty(self):
        result = svc.select_angles(1000, [], 0, 0)
        self.assertEqual(result.count, 0)

    def test_fatigue_mode_prefers_lowest_score(self):
        result = svc.select_angles(
            1000, [1040, 600], 2000, 0, fatigue_mode=True,
            fat_map={"600": 5.0, "1040": 5.0},
        )
        self.assertTrue(result.info.startswith("[疲労]"))

    def test_min_length_from_leg_spacing_excludes_short_pieces(self):
        # palletLen=3000, legCount=2 -> legSpacing=1000, minLen=2000
        # 候補500は短すぎて使えないはずなので、Step1は900mmで拾えない
        result = svc.select_angles(900, [500, 2100], 3000, 2)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.angle1, 2100)


class DbHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_get_leg_count_found_and_not_found(self):
        now = db.now_db_string()
        self.conn.execute(
            "INSERT INTO PalletMaster (幅,丈,巾適合min,巾適合max,丈適合min,丈適合max,業界,記号,位置,在庫数,リスト管理,桁数,脚数,コード,単位,備考,更新日時) "
            "VALUES (1000,2000,0,0,0,0,'一般','','',0,'',0,3,'','',?,?)",
            ("", now),
        )
        self.conn.commit()
        self.assertEqual(svc.get_leg_count(self.conn, 1000, 2000), 3)
        self.assertEqual(svc.get_leg_count(self.conn, 9999, 9999), 0)

    def test_load_all_angle_lengths_fallback_when_empty(self):
        self.assertEqual(svc.load_all_angle_lengths(self.conn), [0])
        self.conn.execute("INSERT INTO CornerboardMaster (アングル丈, データラベル) VALUES (900, ''), (600, ''), (900, '')")
        self.conn.commit()
        self.assertEqual(svc.load_all_angle_lengths(self.conn), [600, 900])


if __name__ == "__main__":
    unittest.main()
