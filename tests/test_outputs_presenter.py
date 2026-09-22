"""presenters/outputs.py の梱包数まわりのユニットテスト。

`_package_count` は計算不可(-1)のとき利用者ログへ理由を書く。以前は
理由を持ち帰らず「調整NO混在」と決め打ちしていたため、実際の原因が
比重・寸法未設定でも同じ文言が出て、現場が引当調整NOを探しに行っても
見つからない誤診断を招いていた。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import lot_service as svc
from packaging_tool.presenters import outputs
from packaging_tool.user_log import UserLog


def _result(*, rows, gravity=2.7, count=100, thickness=3.0,
           width=1000.0, length=2000.0, weights=None, counts=None):
    lot = svc.LotInfo(thickness=thickness, width=width, length=length,
                      final_process_count=count)
    odr = svc.OdrInfo(specific_gravity=gravity,
                      pack_unit_weight=weights or {}, pack_unit_count=counts or {})
    return svc.LotSearchResult(found=True, lot=lot, hiki=rows, odr=odr)


def _session(result):
    user_log = UserLog()
    presenter = SimpleNamespace(lot_result=result, user_log=user_log)
    return SimpleNamespace(presenter=presenter), user_log


class PackageCountLogTests(unittest.TestCase):
    def test_reports_the_actual_reason_when_adjusted(self):
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_ADJUSTED, display_value="5")]
        session, user_log = _session(_result(rows=rows))
        self.assertEqual(outputs._package_count(session), 0)
        self.assertIn(svc.REASON_ADJUSTED, user_log.entries[-1].text)

    def test_reports_the_actual_reason_when_weight_is_missing(self):
        """調整NOが原因でないときに、決め打ちの文言を出さない。"""
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="50")]
        result = _result(rows=rows, gravity=0.0, weights={"O1": 500.0})
        session, user_log = _session(result)
        self.assertEqual(outputs._package_count(session), 0)
        self.assertIn(svc.REASON_WEIGHT_UNKNOWN, user_log.entries[-1].text)
        self.assertNotIn(svc.REASON_ADJUSTED, user_log.entries[-1].text)

    def test_no_log_when_calculable(self):
        rows = [svc.HikiRow(order_no="O1", type_flag=svc.HIKI_QUANTITY, display_value="50")]
        session, user_log = _session(_result(rows=rows, counts={"O1": 20.0}))
        self.assertGreater(outputs._package_count(session), 0)
        self.assertEqual(len(user_log.entries), 0)


if __name__ == "__main__":
    unittest.main()
