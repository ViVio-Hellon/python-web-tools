"""重複の決め方を1つにする(`packaging_tool.dedupe`)。

以前は「同じ」の決め方が3通りあった(設定の警告・共有の片付け・注文の片付け)。警告は
出るのに片付けで消えない、片付けたのにまだ残る、が起きた。

【ここで守りたいこと】
1. 取り込みと同じ形にそろえてから比べる ── Access から来た行(`2026/08/11`・`140.000`・
   空欄 NULL)とツールが送った行(`2026-08-11`・`140.0`・`''`)は同じ
2. 値が1つでも違えば別の行(丈 2502.5 と 2502、確認の印の有り無し)。**消さない**
3. 共有では先に入った行(番号の小さい行)を残す
4. 手元では、もう共有と行き来が済んだ行(取り込んだ行・送った行)を残し、まだ送っていない
   写しのほうを消す(逆にすると、残した写しがもう一度送られる)
5. 共有に表が無い手元の重複(発注コメント閲覧)も、手元の片付けで消える
6. 設定の警告(`duplicate_count`)と片付けが、同じ数え方になる
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import config, data_sync, dedupe, outbox_sync  # noqa: E402
from tests.test_warehouse_handoff import ORDER, ORDER_COLUMNS, make_local, make_shared  # noqa: E402

SEEN = config.TBL_ORDER_COMMENT_SEEN


def _order(**values) -> dict:
    row = {"登録日時": "2026-08-11 09:05:00", "LotNo": "H5084X0", "品名": "タイト",
           "発注コード": "060640", "単位": "台", "厚": 140.0, "幅": 1047, "丈": 2502.5,
           "発注数": 3}
    row.update(values)
    return row


class SharedRuleTests(unittest.TestCase):
    """共有(取り込み元の形)を数える。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="dedupe_rule_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        make_shared(self.src)
        with sqlite3.connect(self.src) as raw:
            raw.execute(f'DELETE FROM "{ORDER}"')

    def put(self, **values) -> None:
        row = _order(**values)
        names = ", ".join(f'"{c}"' for c in row)
        with sqlite3.connect(self.src) as raw:
            raw.execute(f'INSERT INTO "{ORDER}" ({names}) VALUES '
                        f'({", ".join("?" for _ in row)})', list(row.values()))

    def count(self) -> dedupe.TableCount:
        [counted] = dedupe.count_path(self.src, [ORDER])
        return counted

    def shared_numbers(self) -> list[int]:
        with sqlite3.connect(self.src) as raw:
            return [r[0] for r in raw.execute(f'SELECT rowid FROM "{ORDER}" ORDER BY rowid')]

    def test_Accessの書き方とツールの書き方は同じ行とみなす(self) -> None:
        # Access から変換した行: 日付は / 区切り・時が1桁、厚は文字の 140.000、空欄は NULL
        self.put(登録日時="2026/08/11 9:05:00", 厚="140.000", 幅="1047", 丈="2502.5",
                 発注数="3", 確認済み=None)
        # ツールが送った行: - 区切り・数・空文字
        self.put(確認済み="")
        counted = self.count()
        self.assertEqual((counted.total, len(counted.drop)), (2, 1))
        self.assertEqual(counted.drop, [2])                  # 先に入った行を残す

    def test_値が違えば別の行_丈の小数(self) -> None:
        """丈 2502.5 と 2502 は値が違う。切り捨てた写しかもしれないが、機械では決めない。"""
        self.put(丈=2502.5)
        self.put(丈=2502)
        self.assertEqual(self.count().drop, [])

    def test_確認の印が違えば別の行(self) -> None:
        """どちらの印が正しいかは機械では決めない。消さない。"""
        self.put()
        self.put(確認済み="済", 確認日時="2026-08-12 10:00:00")
        self.assertEqual(self.count().drop, [])

    def test_番号と送信IDの違いは比べない(self) -> None:
        with sqlite3.connect(self.src) as raw:
            raw.execute(f'ALTER TABLE "{ORDER}" ADD COLUMN 送信ID TEXT')
        self.put(送信ID="aaa")
        self.put(送信ID="bbb")
        self.assertEqual(len(self.count().drop), 1)

    def test_片付けは控えを取ってから先に入った行を残す(self) -> None:
        self.put()
        self.put(丈=2502)
        self.put()
        self.put()
        result = dedupe.fix_shared(self.src, [ORDER])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.removed, {ORDER: 2})
        self.assertEqual(self.shared_numbers(), [1, 2])      # 値の違う 2502 は残る
        copy = Path(result.backup)
        with sqlite3.connect(copy) as raw:
            self.assertEqual(raw.execute(f'SELECT COUNT(*) FROM "{ORDER}"').fetchone()[0], 4)

    def test_消すものが無ければ書かない_控えも取らない(self) -> None:
        self.put()
        result = dedupe.fix_shared(self.src, [ORDER])
        self.assertEqual((result.ok, result.total, result.backup), (True, 0, ""))
        self.assertEqual(sorted(self.dir.glob("*.bak-*")), [])


class LocalRuleTests(unittest.TestCase):
    """手元の作業用DB(手元の列名)を数える・消す。"""

    def setUp(self) -> None:
        self.conn = make_local()
        self.addCleanup(self.conn.close)
        outbox_sync.ensure_sync_table(self.conn)

    def put(self, origin: str = outbox_sync.ORIGIN_LOCAL, **values) -> int:
        row = _order(**values)
        row["作成元"] = origin
        cur = self.conn.execute(
            f'INSERT INTO "{ORDER}" ({", ".join(row)}) VALUES ({", ".join("?" for _ in row)})',
            list(row.values()))
        self.conn.commit()
        return cur.lastrowid

    def numbers(self, table: str = ORDER) -> list[int]:
        return [r[0] for r in self.conn.execute(f'SELECT rowid FROM "{table}" ORDER BY rowid')]

    def test_警告と片付けが同じ数え方(self) -> None:
        """共有での番号(取込元管理番号)・作成元が違っても中身が同じなら重複。"""
        self.put(取込元管理番号=7, origin=outbox_sync.ORIGIN_IMPORTED)
        self.put(取込元管理番号=187, origin=outbox_sync.ORIGIN_IMPORTED)
        self.put()
        self.assertEqual(data_sync.duplicate_count(self.conn, ORDER, "管理番号"), 2)
        self.assertEqual(dedupe.local_duplicates(self.conn), {ORDER: 2})

    def test_取り込んだ行を残し_まだ送っていない写しを消す(self) -> None:
        unsent = self.put()                                  # 番号は小さいが、まだ送っていない
        imported = self.put(取込元管理番号=7, origin=outbox_sync.ORIGIN_IMPORTED)
        self.assertEqual(dedupe.fix_local(self.conn), {ORDER: 1})
        self.assertEqual(self.numbers(), [imported])
        self.assertNotIn(unsent, self.numbers())

    def test_送った行を残し_まだ送っていない写しを消す(self) -> None:
        unsent = self.put()
        sent = self.put()
        self.conn.execute(
            f"INSERT INTO [{outbox_sync.SYNC_LOG_TABLE}] (テーブル名, 行ID, 状態) VALUES (?, ?, ?)",
            (ORDER, sent, outbox_sync.SYNC_DONE))
        self.conn.commit()
        dedupe.fix_local(self.conn)
        self.assertEqual(self.numbers(), [sent])
        self.assertNotIn(unsent, self.numbers())

    def test_値が違う行は手元でも消さない(self) -> None:
        self.put(丈=2502.5)
        self.put(丈=2502)
        self.assertEqual(dedupe.fix_local(self.conn), {})
        self.assertEqual(len(self.numbers()), 2)

    def test_共有に表が無い閲覧の重複も消える(self) -> None:
        """発注コメント閲覧は取り込みで入れ替わらないことがある(共有に表が無い)。"""
        for _ in range(3):
            self.conn.execute(
                f'INSERT INTO "{SEEN}" (コメントID, 見た端末, 見た側, 見た日時, 作成元)'
                " VALUES ('c1', 'PC1', '現場', '2026-08-11 10:00:00', ?)",
                (outbox_sync.ORIGIN_IMPORTED,))
        self.conn.commit()
        self.assertEqual(dedupe.fix_local(self.conn), {SEEN: 2})
        self.assertEqual(self.numbers(SEEN), [1])
        self.assertEqual(dedupe.local_duplicates(self.conn), {})


class RunFixTests(unittest.TestCase):
    """`--fix` 1回分: 共有を片付ける → 取り込み直す → 手元に残った重複も消す。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="dedupe_run_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        make_shared(self.src)
        with sqlite3.connect(self.src) as raw:              # L1 が4回入った形
            for _ in range(3):
                raw.execute(f'INSERT INTO "{ORDER}" SELECT NULL, '
                            + ", ".join(f'"{c}"' for c in ORDER_COLUMNS[1:])
                            + f' FROM "{ORDER}" WHERE 管理番号 = 1')
        self.conn = make_local()
        self.addCleanup(self.conn.close)
        data_sync.import_master(self.conn, self.src)

    def test_共有も手元も重複が無くなる(self) -> None:
        self.assertEqual(dedupe.local_duplicates(self.conn), {ORDER: 3})
        run = dedupe.run_fix(self.conn, self.src)
        # この共有は BoardMaster などを持たない作りなので、取り込みの成否は見ない
        self.assertTrue(run.fixed.ok, run.summary())
        self.assertIsNotNone(run.imported)
        self.assertEqual(run.local_left, {})
        with sqlite3.connect(self.src) as raw:
            self.assertEqual([r[0] for r in raw.execute(
                f'SELECT 管理番号 FROM "{ORDER}" ORDER BY 1')], [1, 2])
        self.assertEqual(dedupe.local_duplicates(self.conn), {})
        self.assertIn("3件 消しました", run.summary())
        self.assertIn("この端末の重複も無くなりました", run.summary())

    def test_共有を消せなければ取り込み直さない(self) -> None:
        missing = self.dir / "無い" / config.MATERIAL_DB_NAME
        run = dedupe.run_fix(self.conn, missing)
        self.assertFalse(run.ok)
        self.assertIsNone(run.imported)
        self.assertIn("取り込み直しはしていません", run.summary())


if __name__ == "__main__":
    unittest.main()
