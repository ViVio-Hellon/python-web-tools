"""「表を持ってくる」: Access にしか無い発注だけを足す(資材パレット注文管理)。

現場の声:「影響のない列を丸々コピーするのもダメなんでしょうか？ 資材パレット注文管理だけでも」
→ 相談の結果「案A: Access にしか無い発注を、ツールに足す」。

資材パレット注文管理はこのツールが書き込む表(発注・確認・取消)なので、Access の中身で
入れ替えると、ツールから送った発注や確認の印が消える。ところが Access 側でも発注が
入り続けていて、それがツールに届かない。

【ここで守りたいこと】
1. Access にしか無い発注だけを足す。**今ある行は消しも書き換えもしない**
   (ツールから送った発注・確認の印・送信ID・送信端末・発注キー はそのまま)
2. 同じ発注かは 登録日時・LotNo・発注コード・厚・幅・丈 で見分ける。書き方の違い
   (2026/08/11 と 2026-08-11、140.000 と 140.0)は同じとみなす。管理番号では見分けない
   (Access と梱包資材マスタで別々に振られている)
3. 足した行の管理番号は梱包資材マスタの続き(MAX+1)。送信IDは空
4. 2回押しても二重に足さない
5. 同じ値の行が Access に2行・梱包資材マスタに1行なら、1行だけ足す
6. ほかのツールが書き込む表は、今までどおり入れ替えも足しもしない
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import source_db, table_bring  # noqa: E402

TABLE = "資材パレット注文管理"
ACCESS_COLS = ["管理番号", "登録日時", "LotNo", "品名", "発注コード", "発注数", "単位", "材質",
               "調質", "厚", "幅", "丈", "用途コード", "納入先", "取り消し済", "取り消し日時",
               "確認済み", "確認日時", "送信ID"]
SHARED_COLS = ACCESS_COLS + ["送信端末", "発注キー"]


def access_row(n: int, when: str, lot: str, code: str = "059551", thick: str = "8.000",
               **extra) -> dict:
    """Access(access_parser で変換したもの)の書き方: 日時は /、数は文字。"""
    row = {"管理番号": n, "登録日時": when, "LotNo": lot, "品名": "タイト　　1400x3150",
           "発注コード": code, "発注数": None, "単位": "台", "材質": "R61S", "調質": "T651",
           "厚": thick, "幅": "1385.0", "丈": "2770.5", "用途コード": "H197", "納入先": "ｺ-ﾐｷﾝｿﾞｸ",
           "取り消し済": None, "取り消し日時": None, "確認済み": None, "確認日時": None,
           "送信ID": None}
    row.update(extra)
    return row


def write_table(path: Path, columns: list[str], rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f'CREATE TABLE "{TABLE}" (' + ", ".join(f'"{c}"' for c in columns) + ")")
    for row in rows:
        conn.execute(f'INSERT INTO "{TABLE}" VALUES (' + ",".join("?" * len(columns)) + ")",
                     [row.get(c) for c in columns])
    conn.execute('CREATE TABLE "パレット入出庫履歴" ("id", "幅")')
    conn.commit()
    conn.close()


class AppendOrdersTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="append_"))
        self.access = base / "access.sqlite3"
        self.master = base / "梱包資材マスタ.sqlite3"
        # Access: 移行前からの2件 + 移行後に Access で入った2件
        write_table(self.access, ACCESS_COLS, [
            access_row(1, "2026/08/11 11:14:40", "H4083E0", thick="140.000"),
            access_row(2, "2026/08/12 07:50:04", "H6081D0", 取り消し済="1"),
            access_row(3, "2026/10/07 12:07:03", "R6501E0", thick="40.000"),
            access_row(4, "2026/10/07 12:08:10", "R6501C0"),
        ])
        # 梱包資材マスタ: 移行時に写した2件(書き方が違う) + ツールから送った1件
        write_table(self.master, SHARED_COLS, [
            access_row(1, "2026-08-11 11:14:40", "H4083E0", thick=140.0),
            access_row(2, "2026-08-12 07:50:04", "H6081D0", 取り消し済="1", 確認済み=None),
            access_row(3, "2026-10-01 09:00:00", "T0000A0", code="059999", thick=10.0,
                       確認済み="1", 確認日時="2026-10-01 10:00:00",
                       送信ID="op-tool-1", 送信端末="PC-01", 発注キー="K-1"),
        ])
        patcher = mock.patch.object(table_bring, "_dest", return_value=self.master)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(source_db.sweep_old_copies)

    def rows(self) -> list[dict]:
        return source_db.read_query(self.master, f'SELECT * FROM "{TABLE}" ORDER BY 管理番号')

    def candidate(self, plan: table_bring.Plan, name: str = TABLE) -> table_bring.Candidate:
        return next(c for c in plan.candidates if c.name == name)

    def test_中を見るとAccessにしか無い行の数が出る(self) -> None:
        plan = table_bring.plan(str(self.access))
        self.assertTrue(plan.ok, plan.message)
        found = self.candidate(plan)
        self.assertFalse(found.can_refresh)           # 入れ替えはしない
        self.assertTrue(found.can_append, found.append_why)
        self.assertEqual((found.append_rows, found.append_same), (2, 2))
        self.assertIn("Access にしか無い行 2 件", plan.message)
        shown = next(t for t in table_bring.plan_dict(plan)["tables"] if t["name"] == TABLE)
        self.assertTrue(shown["can_append"])
        self.assertEqual(shown["append_rows"], 2)

    def test_無い発注だけ足して今ある行には触らない(self) -> None:
        before = self.rows()
        result = table_bring.refresh(None, str(self.access), [TABLE])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.appended, [(TABLE, 2, 2)])
        self.assertIn("Access にしか無い行を足しました", result.message)
        after = self.rows()
        self.assertEqual(after[:3], before)           # 今ある3行は1文字も変わらない
        added = after[3:]
        self.assertEqual([r["LotNo"] for r in added], ["R6501E0", "R6501C0"])
        self.assertEqual([r["管理番号"] for r in added], [4, 5])     # 梱包資材マスタの続き
        self.assertEqual(added[0]["登録日時"], "2026-10-07 12:07:03")  # 取り込みと同じ書き方
        self.assertEqual(added[0]["厚"], "40.000")                    # 数は Access の値のまま
        self.assertEqual(added[0]["丈"], "2770.5")                    # 整数にしない(二重に足さないため)
        self.assertIsNone(added[0]["送信ID"])
        self.assertIsNone(added[0]["送信端末"])
        self.assertTrue(Path(result.backup).is_file())

    def test_2回押しても二重に足さない(self) -> None:
        self.assertTrue(table_bring.refresh(None, str(self.access), [TABLE]).ok)
        again = table_bring.refresh(None, str(self.access), [TABLE])
        self.assertTrue(again.ok, again.message)
        self.assertEqual(again.appended, [(TABLE, 0, 4)])
        self.assertEqual(len(self.rows()), 5)
        self.assertEqual(self.candidate(table_bring.plan(str(self.access))).append_rows, 0)

    def test_同じ値の行は数で突き合わせる(self) -> None:
        conn = sqlite3.connect(self.access)
        conn.execute(f'INSERT INTO "{TABLE}" (管理番号, 登録日時, LotNo, 発注コード, 厚, 幅, 丈)'
                     " SELECT 5, 登録日時, LotNo, 発注コード, 厚, 幅, 丈"
                     f' FROM "{TABLE}" WHERE 管理番号 = 1')
        conn.commit()
        conn.close()
        found = self.candidate(table_bring.plan(str(self.access)))
        self.assertEqual((found.append_rows, found.append_same), (3, 2))

    def test_見分ける列が無ければ足さない(self) -> None:
        conn = sqlite3.connect(self.access)
        conn.execute(f'ALTER TABLE "{TABLE}" RENAME COLUMN "LotNo" TO "ロット"')
        conn.commit()
        conn.close()
        found = self.candidate(table_bring.plan(str(self.access)))
        self.assertFalse(found.can_append)
        self.assertIn("LotNo", found.append_why)
        result = table_bring.refresh(None, str(self.access), [TABLE])
        self.assertFalse(result.ok)
        self.assertEqual(len(self.rows()), 3)

    def test_ほかのツールの表は今までどおり触らない(self) -> None:
        found = self.candidate(table_bring.plan(str(self.access)), "パレット入出庫履歴")
        self.assertFalse(found.can_refresh)
        self.assertFalse(found.can_append)
        result = table_bring.refresh(None, str(self.access), ["パレット入出庫履歴"])
        self.assertFalse(result.ok)
        self.assertIn("このツールが書き込む表", result.message)


class ScreenTests(unittest.TestCase):
    def test_画面に無い行だけ足すがある(self) -> None:
        js = (_ROOT / "app/static/js/views/settings.js").read_text("utf-8")
        self.assertIn('box.dataset.action = "append"', js)
        self.assertIn("無い行だけ足す", js)
        # 足すだけのときは「今の行は消えます」と言わない
        self.assertIn("今ある行は消しも書き換えもしません", js)


if __name__ == "__main__":
    unittest.main()
