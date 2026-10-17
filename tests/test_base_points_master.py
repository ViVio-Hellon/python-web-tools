"""拠点をマスタ(BoardMaster のデータラベル)で足す(VER4.8.63)。

現場の声:「拠点ラベルの追加を BoardMaster に設定し(データラベル列: 拠点L-1, 拠点LVC, 拠点HVC。
例: 追加するとして 拠点大板 と入力)、最寄り置き場の検索の拠点のコンボボックスに追加、
配置編集にも拠点の赤色ラベルで出現し配置できる」。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import (board_selection_service, board_usage, floor_plan,  # noqa: E402
                            layout_session, location_service as svc, user_settings)
from packaging_tool.presenters import layout as layout_presenter  # noqa: E402
from packaging_tool.presenters import settings as settings_presenter  # noqa: E402

from tests import _web  # noqa: E402
from tests.test_handoff import insert_board  # noqa: E402


def insert_base_row(conn, label: str) -> None:
    """拠点だけの行(ボード幅・丈 0、ボードタイプ 拠点)。"""
    conn.execute("INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
                 " VALUES (0, 0, '拠点', ?)", (label,))
    conn.commit()


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"
        self.addCleanup(lambda: setattr(floor_plan, "USER_PATH", original))
        before = user_settings.get_position()
        self.addCleanup(lambda: user_settings.set_position(before))
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        insert_board(self.conn, width=1100, length=2000, label="lblItem1")
        insert_base_row(self.conn, "拠点L-1,拠点LVC,拠点HVC")
        insert_base_row(self.conn, "拠点大板")


class NamesTests(_Case):

    def test_マスタの拠点が足される_図と同じものは図の名前で1つ(self):
        self.assertEqual(svc.master_base_points(self.conn), ["L-1", "LVC", "HVC", "大板"])
        names = svc.base_point_names(self.conn)
        self.assertEqual(sorted(names), sorted(["LVC", "L1", "HVC", "大板"]))
        self.assertEqual(svc.unplaced_base_points(self.conn), ["大板"])

    def test_全角やハイフンが違っても同じ拠点(self):
        self.assertEqual(svc.base_key("Ｌ－１"), svc.base_key("L1"))
        self.assertEqual(svc.base_key("l-1"), svc.base_key("L1"))

    def test_拠点の印は置き場として数えない(self):
        self.assertNotIn("拠点大板", svc.unplaced_data_labels(self.conn))
        self.assertNotIn("拠点大板", svc.list_all_data_labels(self.conn))

    def test_拠点だけの行はボードの候補にも種別にも出ない(self):
        boards = board_selection_service.list_available_boards(self.conn)
        self.assertEqual([(b.width, b.length) for b in boards], [(1100, 2000)])
        self.assertNotIn("拠点", board_selection_service.list_board_types(self.conn))
        self.assertNotIn("拠点", board_usage.board_types(self.conn))

    def test_マスタに無ければ図の拠点だけ(self):
        conn = _web.memory_db()
        self.addCleanup(conn.close)
        self.assertEqual(svc.base_point_names(conn), list(floor_plan.base_points()))


class LayoutTests(_Case):

    def setUp(self) -> None:
        super().setUp()
        layout_session.reset_session()
        self.addCleanup(layout_session.reset_session)
        self.session = layout_session.get_session()

    def test_コンボボックスに出る(self):
        view = layout_presenter.build(self.conn, self.session)
        self.assertIn("大板", view.base_points)
        self.assertIn("L1", view.base_points)
        self.assertNotIn("L-1", view.base_points)

    def test_置く前でも選べるが_距離は測れないと言う(self):
        result = self.session.set_base_point("大板", self.conn)
        self.assertTrue(result.ok)
        self.assertIn("まだ図に置いていない", result.message)
        self.assertEqual(user_settings.get_position(), "大板")
        self.assertFalse(svc.current_base_point().known)

    def test_マスタにも図にも無い拠点は断る(self):
        self.assertFalse(self.session.set_base_point("どこか", self.conn).ok)

    def test_配置編集に入ると図に出て_動かして保存すると拠点になる(self):
        result = self.session.set_editing(True, self.conn)
        self.assertIn("大板", result.message)
        view = layout_presenter.build(self.conn, self.session)
        self.assertIn("大板", [b.name for b in view.bases])          # 赤い拠点の札で出る
        self.assertTrue(self.session.move("大板", 500, 300).ok)
        self.assertTrue(self.session.set_editing(False, self.conn).ok)  # 終えると保存
        plan = floor_plan.load()
        self.assertEqual((plan.base_point("大板").x, plan.base_point("大板").y), (500, 300))
        self.assertEqual(svc.unplaced_base_points(self.conn), [])
        # 置いたので距離が測れる
        user_settings.set_position("大板")
        self.assertTrue(svc.current_base_point().known)
        self.assertIsNotNone(plan.distance("lblItem1", "大板"))
        # 2回目の編集では重ねて出さない
        self.assertNotIn("大板", self.session.set_editing(True, self.conn).message)


class SettingsTests(_Case):

    def test_設定画面でも選べる(self):
        result = settings_presenter.save(position="大板", conn=self.conn)
        self.assertTrue(result.ok, result.message)
        self.assertFalse(settings_presenter.save(position="どこか", conn=self.conn).ok)


if __name__ == "__main__":
    unittest.main()
