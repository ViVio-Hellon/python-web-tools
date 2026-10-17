"""疲労度のON/OFF・拠点(ライン)を替えたときの食い違いの試験(VER4.8.62)。

現場の声:「疲労度オンオフで不整合が起きないかチェックしてください。ラインを変えて
距離の関係も不整合が起きないかチェックしてください」。点検で見つけたもの:

    1. 疲労度マップが作れないとき「通常選定で実行しました」と言いながら、
       疲労度モードの経路を点0で走らせていた(実データで544通り中18通りが通常と違った)
    2. 拠点が配置図に無いとき、距離ぬきの点で黙って「疲労度優先」していた
    3. 置き場が未登録の板は距離0点(いちばん近い扱い)。アングルは25点で扱いが違った
    4. 棚検索で拠点を替えても、「まとめて見る」の合計疲労度が前の拠点のまま残った
    5. 棚検索の「まとめて見る」がボード種別を見ず、同じ寸法の別種別の棚で距離を測った
    6. 疲労度を切り替えても出ているボードは前の設定のまま(言わないと食い違って見える)
"""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import (board_scoring, db, floor_plan, layout_session,  # noqa: E402
                            location_service, user_settings, work_context)
from packaging_tool import board_selection_algorithm as alg  # noqa: E402
from packaging_tool.presenters.selection import SelectionPresenter  # noqa: E402
from packaging_tool.user_log import UserLog  # noqa: E402

from tests import _web  # noqa: E402,F401  (設定ファイルの置き場を試験用へ)
from tests.test_handoff import insert_board  # noqa: E402


def total_of(message: str) -> float:
    return float(re.search(r"合計疲労度=([\d.]+)", message).group(1))


class _PlanCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"
        self.addCleanup(lambda: setattr(floor_plan, "USER_PATH", original))
        before = user_settings.get_position()
        self.addCleanup(lambda: user_settings.set_position(before))
        user_settings.set_position("L1")


class SelectionFallbackTests(_PlanCase):
    """1・2・6: 資材選択のボード選定。"""

    def setUp(self) -> None:
        super().setUp()
        from packaging_tool.selection_session import SelectionSession

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)
        for w, l in [(1100, 1000), (1100, 2000), (100, 2000), (50, 1600)]:
            self.conn.execute(
                "INSERT INTO BoardMaster (ボード幅,ボード丈,ボードタイプ,データラベル)"
                " VALUES (?,?,?,?)", (w, l, "ハードボード", "lblItem1"))
        self.conn.commit()
        self.log = UserLog()
        self.session = SelectionSession(
            presenter=SelectionPresenter(self.conn, user_log=self.log))
        self.session.apply_pallet("1100", "2000")
        self.session.apply_product("1080", "1980")
        self.session.board_type = "ハードボード"
        # 疲労度優先は作業状態(プロセスに1つ)が持つ押しっぱなしのモード。OFF から始める
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        if self.session.fatigue:
            self.session.toggle("fatigue")

        # 選定に渡った疲労度モードを捕まえる
        self.modes: list[bool] = []
        original = alg.auto_select_boards

        def spy(*args, **kwargs):
            self.modes.append(kwargs.get("fatigue_mode"))
            return original(*args, **kwargs)
        alg.auto_select_boards = spy
        self.addCleanup(lambda: setattr(alg, "auto_select_boards", original))

    def test_fatigue_mode_is_on_when_the_maps_are_built(self):
        self.session.toggle("fatigue")
        self.assertTrue(self.session.auto_select_boards().ok)
        # 通常の選定と疲労度の選定を両方作って比べる(VER4.8.65)
        self.assertEqual(self.modes, [False, True])
        self.assertIn("疲労度の比べ", self.log.text)

    def test_a_missing_map_really_runs_the_normal_selection(self):
        """1. 「通常選定で実行しました」なら、本当に通常選定で走らせる。"""
        original = board_scoring.build_selection_fatigue_maps
        board_scoring.build_selection_fatigue_maps = lambda *a, **k: (None, None)
        self.addCleanup(lambda: setattr(board_scoring, "build_selection_fatigue_maps", original))
        self.session.toggle("fatigue")
        result = self.session.auto_select_boards()
        self.assertTrue(result.ok)
        self.assertIn("通常選定で実行しました", " ".join(result.notes))
        self.assertEqual(self.modes, [False])

    def test_an_unknown_base_point_falls_back_and_says_so(self):
        """2. 拠点が配置図に無いと距離が測れない。黙って疲労度優先しない。"""
        user_settings.set_position("どこにも無い拠点")
        self.session.toggle("fatigue")
        result = self.session.auto_select_boards()
        self.assertTrue(result.ok)
        self.assertIn("どこにも無い拠点", " ".join(result.notes))
        self.assertEqual(self.modes, [False])

    def test_toggling_after_a_selection_says_which_mode_chose_the_boards(self):
        """6. 出ているボードは選び直さない。食い違うときはそう言う。"""
        self.session.toggle("fatigue")
        self.assertTrue(self.session.auto_select_boards().ok)
        self.session.toggle("fatigue")              # OFF にした
        self.assertIn("出ているボードは疲労度ONで選んだもの", self.log.text)


class UnknownShelfDistanceTests(unittest.TestCase):
    """3. 置き場が分からない板は、ボードもアングルも同じ既定の距離。"""

    def test_boards_use_the_same_default_as_angles(self):
        conn = _web.memory_db()
        self.addCleanup(conn.close)
        board = type("B", (), {"width": 999, "length": 999})()
        entry = board_scoring.build_fatigue_map(conn, [board], "L1")["999x999"]
        self.assertEqual(entry.dist, board_scoring.angle_service.FAT_DIST_DEFAULT)


class LayoutBaseSwitchTests(_PlanCase):
    """4・5: 棚検索の「置き場をまとめて見る」。"""

    def setUp(self) -> None:
        super().setUp()
        layout_session.reset_session()
        self.addCleanup(layout_session.reset_session)
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.session = layout_session.get_session()
        insert_board(self.conn, width=1100, length=2000, label="lblItem1")
        # 同じ寸法の別種別。L1 以外の拠点からはこちらのほうが近い
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
            " VALUES (1100, 2000, 'プロテックボード', 'lblItem20')")
        self.conn.commit()
        self.items = [{"kind": "board", "width": 1100, "length": 2000, "count": 2,
                       "board_type": "ハードボード"}]

    def expected(self, base: str) -> float:
        nearest = location_service.find_nearest_shelf_for_board(
            self.conn, 1100, 2000, base, "ハードボード")
        return location_service.score_board_pick(nearest.distance, 1100, 2000, 2)

    def test_switching_the_base_point_recomputes_the_total(self):
        """4. 以前は前の拠点の合計疲労度が残っていた。"""
        first = self.session.show_selection(self.conn, self.items)
        self.assertAlmostEqual(total_of(first.message), self.expected("L1"), places=1)
        self.session.set_base_point("HVC", self.conn)
        self.assertAlmostEqual(total_of(self.session.result), self.expected("HVC"), places=1)
        self.assertNotAlmostEqual(self.expected("L1"), self.expected("HVC"), places=1)

    def test_switching_after_a_search_still_redoes_the_search(self):
        self.session.search(self.conn, "board", "1100", "2000")
        self.session.set_base_point("HVC", self.conn)
        self.assertIn("最寄り", self.session.result)
        self.assertNotIn("合計疲労度", self.session.result)

    def test_the_board_type_of_the_selection_is_used(self):
        """5. 同じ寸法の別種別(プロテックボード)の棚で測らない。"""
        for base in ("L1", "HVC", "LVC"):
            user_settings.set_position(base)
            result = self.session.show_selection(self.conn, self.items)
            self.assertEqual(self.session.highlight, {"lblItem1"}, base)
            self.assertAlmostEqual(total_of(result.message), self.expected(base), places=1)


if __name__ == "__main__":
    unittest.main()
