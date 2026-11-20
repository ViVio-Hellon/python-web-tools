"""メモページ(VER4.9.0。`packaging_tool/memo_store.py`・`app/routes/memo.py`)

現場の決め: 保存先はこの PC の手元の DB / 現場モードだけ / シートは誰でも消せる(確認あり)。

【ここで守りたいこと】
1. 変わったものだけを1回で保存できる(1つの取引。読めない内容なら何も書かない)
2. 行・列の足し引きでセル・列幅・行高がずれる
3. 別の窓が先に書いた同じセルは黙って上書きしない(ぶつかりを返す)。違うセルなら重ねる
4. シートの追加・名前変更・複製・削除(すぐには消さず戻せる)・並べ替え
5. 画面・API・印刷の窓が出る。レールは現場モードだけ、いちばん後ろ(F1〜F7 の割り当てを変えない)
"""
from __future__ import annotations

import unittest

from packaging_tool import memo_store as m
from tests import _web


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.sheet = m.list_sheets(self.conn)[0]

    def save(self, ops, rev=None):
        return m.save(self.conn, self.sheet.id, self.sheet.rev if rev is None else rev, ops)

    def cells(self):
        return {(c["r"], c["c"]): c for c in m.get_sheet(self.conn, self.sheet.id).cells}

    def test_初めて開くとSheet1がある(self) -> None:
        self.assertEqual([s.name for s in m.list_sheets(self.conn)], ["Sheet1"])

    def test_まとめて保存し_空にすると消える(self) -> None:
        r = self.save([{"op": "cells", "cells": [{"r": 0, "c": 0, "v": "品名", "f": {"b": True, "bg": "#ff0", "x": 1}},
                                                  {"r": 2, "c": 3, "v": "12"}]}])
        self.assertEqual(r["rev"], 2)
        got = self.cells()
        self.assertEqual(got[(0, 0)], {"r": 0, "c": 0, "v": "品名", "f": {"b": True, "bg": "#ff0"}}, "知らない書式は捨てる")
        self.save([{"op": "cells", "cells": [{"r": 2, "c": 3, "v": ""}]}], rev=2)
        self.assertNotIn((2, 3), self.cells())

    def test_読めない内容なら何も書かない(self) -> None:
        with self.assertRaises(m.MemoError):
            self.save([{"op": "cells", "cells": [{"r": 0, "c": 0, "v": "a"}, {"r": "x", "c": 0, "v": "b"}]}])
        self.assertEqual(self.cells(), {})
        self.assertEqual(m.get_sheet(self.conn, self.sheet.id).rev, 1)

    def test_大量のセルを1回で(self) -> None:
        cells = [{"r": r, "c": c, "v": f"{r}-{c}"} for r in range(1000) for c in range(10)]
        r = self.save([{"op": "cells", "cells": cells}])
        self.assertEqual(r["rows"], 1000)
        self.assertEqual(len(self.cells()), 10000)

    def test_行と列の足し引きでずれる(self) -> None:
        self.save([{"op": "cells", "cells": [{"r": 0, "c": 0, "v": "A1"}, {"r": 1, "c": 1, "v": "B2"}]},
                   {"op": "size", "col_widths": {"1": 150}, "row_heights": {"1": 40}}])
        self.sheet = m.get_sheet(self.conn, self.sheet.id)
        self.save([{"op": "insert_rows", "at": 1, "n": 2}, {"op": "insert_cols", "at": 0, "n": 1}])
        s = m.get_sheet(self.conn, self.sheet.id)
        self.assertEqual({(c["r"], c["c"]): c["v"] for c in s.cells}, {(0, 1): "A1", (3, 2): "B2"})
        self.assertEqual((s.col_widths, s.row_heights, s.rows, s.cols), ({"2": 150}, {"3": 40}, 102, 27))
        self.sheet = s
        self.save([{"op": "delete_rows", "at": 0, "n": 1}, {"op": "delete_cols", "at": 0, "n": 1}])
        s = m.get_sheet(self.conn, self.sheet.id)
        self.assertEqual({(c["r"], c["c"]): c["v"] for c in s.cells}, {(2, 1): "B2"})
        self.assertEqual((s.col_widths, s.row_heights), ({"1": 150}, {"2": 40}))

    def test_別の窓が同じセルを変えていたらぶつかりを返す_違うセルなら重ねる(self) -> None:
        self.save([{"op": "cells", "cells": [{"r": 0, "c": 0, "v": "窓1"}]}])          # 版2
        with self.assertRaises(m.Conflict) as caught:
            self.save([{"op": "cells", "cells": [{"r": 0, "c": 0, "v": "窓2"}]}], rev=1)
        self.assertEqual(caught.exception.cells[0]["v"], "窓1")
        r = self.save([{"op": "cells", "cells": [{"r": 5, "c": 5, "v": "窓2"}]}], rev=1)
        self.assertEqual(r["rev"], 3)
        self.assertEqual(self.cells()[(0, 0)]["v"], "窓1")
        with self.assertRaises(m.Conflict):
            self.save([{"op": "insert_rows", "at": 0, "n": 1}], rev=1)

    def test_シートの管理(self) -> None:
        a = self.sheet
        b = m.create_sheet(self.conn)
        self.assertEqual(b.name, "Sheet2")
        m.save(self.conn, a.id, a.rev, [{"op": "cells", "cells": [{"r": 0, "c": 0, "v": "中身"}]}])
        with self.assertRaises(m.MemoError):
            m.rename_sheet(self.conn, b.id, "Sheet1")
        m.rename_sheet(self.conn, b.id, "在庫:メモ")
        self.assertEqual(m.get_sheet(self.conn, b.id).name, "在庫メモ", "Excel で使えない字は外す")
        c = m.duplicate_sheet(self.conn, a.id)
        self.assertEqual((c.name, [x["v"] for x in c.cells]), ("Sheet1 のコピー", ["中身"]))
        self.assertEqual([s.name for s in m.list_sheets(self.conn)], ["Sheet1", "Sheet1 のコピー", "在庫メモ"])
        m.delete_sheet(self.conn, c.id)
        self.assertEqual([t["name"] for t in m.trashed(self.conn)], ["Sheet1 のコピー"])
        back = m.restore_sheet(self.conn, c.id)
        self.assertEqual([x["v"] for x in back.cells], ["中身"])
        m.move_sheet(self.conn, b.id, 0)
        self.assertEqual(m.list_sheets(self.conn)[0].name, "在庫メモ")

    def test_最後の1枚は消せない_古いゴミ箱は片付く(self) -> None:
        with self.assertRaises(m.MemoError):
            m.delete_sheet(self.conn, self.sheet.id)
        b = m.create_sheet(self.conn)
        m.delete_sheet(self.conn, b.id)
        from datetime import datetime, timedelta
        self.assertEqual(m.purge_old(self.conn, now=datetime.now() + timedelta(days=m.TRASH_DAYS + 1)), 1)
        self.assertEqual(m.trashed(self.conn), [])


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import memo as routes
        self.conn = _web.bind_db(self, routes)
        self.client = _web.make_client()

    def test_画面とAPI(self) -> None:
        res = self.client.get("/memo", headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        self.assertIn("mGrid", res.get_data(as_text=True))
        sid = self.client.get("/api/memo/sheets", headers=_web.auth()).get_json()["sheets"][0]["id"]
        body = self.client.post(f"/api/memo/sheets/{sid}/save", json={"rev": 1, "ops": [
            {"op": "cells", "cells": [{"r": 0, "c": 0, "v": "x"}]}]}, headers=_web.auth()).get_json()
        self.assertEqual(body["rev"], 2)
        res = self.client.post(f"/api/memo/sheets/{sid}/save", json={"rev": 1, "ops": [
            {"op": "cells", "cells": [{"r": 0, "c": 0, "v": "y"}]}]}, headers=_web.auth())
        self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (409, "conflict"))
        made = self.client.post("/api/memo/sheets", json={}, headers=_web.auth()).get_json()
        res = self.client.post(f"/api/memo/sheets/{made['sheet']['id']}/delete", json={}, headers=_web.auth())
        self.assertEqual(len(res.get_json()["sheets"]), 1)
        page = self.client.get(f"/memo/print?sheet={sid}&range=0,0,1,1", headers=_web.auth()).get_data(as_text=True)
        self.assertIn("MEMO_PRINT", page)
        self.assertIn('"v": "x"', page.replace('"v":"x"', '"v": "x"'))

    def test_レールは現場モードだけ_いちばん後ろ(self) -> None:
        from app import shell
        field = [k for k, *_ in shell.FIELD_NAV]
        self.assertEqual(field[-1], "memo")
        self.assertEqual(field[6], "settings", "F7 は設定のまま")
        self.assertNotIn("memo", [k for k, *_ in shell.MATERIAL_NAV])
        self.assertIn("memo", shell.READY_SCREENS)


if __name__ == "__main__":
    unittest.main()
