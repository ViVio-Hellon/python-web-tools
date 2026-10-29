"""画面の記録に何を出すか(`trouble`。VER4.8.120)と、共有の実績の守り(同じ実績は1件だけ)。

現場の声:「ログ出してないくせに探せとか言ってるの？」「いやログ出力は必要だから全部画面に出せばいいって
もんでもないよ」。

【ここで守りたいこと】
1. その日のログには**全部**書く。画面の記録(エラー・注意の記録)には、人が何かしないといけないものだけ
2. 1回きりで次に直った失敗は、画面に出さない。**続いたら1件だけ**(回数・最初の時刻つき)。直ったら「直った」も1件
3. ツールが自分で共有を変えたこと(送り直した・二重を止めた・重複を消した)は、同じ種類を1時間に1件まで
4. 共有の実績の表に「同じ実績は1件だけ」が付き、古い版・ツールを通さない書き込みからも写しが入らない
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from unittest import mock

from packaging_tool import logging_utils, trace_log, trouble
from packaging_tool import pattern_copies, pattern_sync as sync, source_db
from packaging_tool import pattern_store as ps
from tests.test_pattern_sync import H, SyncTestCase, save
from tests.test_trace_log import TraceTestCase, log

T0 = 1_800_000_000.0


class TroubleTestCase(TraceTestCase):
    def setUp(self) -> None:
        super().setUp()
        trouble.forget()
        self.addCleanup(trouble.forget)

    def shown(self) -> list[tuple[str, str]]:
        logging_utils.flush_logs(5.0)
        return [(i.kind, i.message) for i in reversed(trace_log.list_errors())]


class StreakTests(TroubleTestCase):
    def test_1回きりで直ったものは画面に出さない_ログには残る(self) -> None:
        log.warning("共有を読めませんでした: database is locked")      # 呼び手の1回ごとの行
        trouble.failed(log, "k", trouble.KIND_UNREADABLE, "実績の取り込み", "database is locked", now=T0)
        trouble.ok(log, "k", now=T0 + 20)
        self.assertEqual(self.shown(), [])
        self.assertIn("database is locked", self.day_log())

    def test_続いたら1件だけ_直ったら直ったも1件(self) -> None:
        for i in range(10):                                           # 15秒ごとに10回(2分あまり)
            trouble.failed(log, "k", trouble.KIND_UNREADABLE, "実績の取り込み", "locked", now=T0 + 15 * i)
        self.assertEqual(self.shown(), [])                            # まだ5分たっていない
        trouble.failed(log, "k", trouble.KIND_UNREADABLE, "実績の取り込み", "locked", now=T0 + 301)
        trouble.failed(log, "k", trouble.KIND_UNREADABLE, "実績の取り込み", "locked", now=T0 + 400)
        trouble.ok(log, "k", now=T0 + 600)
        [(kind1, text1), (kind2, text2)] = self.shown()
        self.assertEqual(kind1, trouble.KIND_UNREADABLE)
        self.assertIn("実績の取り込み が続いています: 11回", text1)
        self.assertIn("locked", text1)
        self.assertEqual(kind2, trouble.KIND_RECOVERED)
        self.assertIn("12回しくじっていました", text2)

    def test_続いたままなら6時間ごとにもう一度だけ言う(self) -> None:
        for at in (T0, T0 + 301, T0 + 3600, T0 + 301 + trouble.REMIND_SEC):
            trouble.failed(log, "k", trouble.KIND_UNSENDABLE, "発注の送信", "x", now=at)
        texts = [t for _k, t in self.shown()]
        self.assertEqual(len(texts), 2)
        self.assertIn("まだ続いています", texts[1])

    def test_ツールが共有を変えたことは1時間に1件_回数を添える(self) -> None:
        for i in range(3):
            trouble.once(log, "r", trouble.KIND_RESENT, "手元の%s件を送り直します", 2, now=T0 + i)
        trouble.once(log, "r", trouble.KIND_RESENT, "手元の%s件を送り直します", 2, now=T0 + 3601)
        texts = [t for _k, t in self.shown()]
        self.assertEqual(texts, ["手元の2件を送り直します", "手元の2件を送り直します(前に記録してから 2回)"])


class PatternImportTests(TroubleTestCase, SyncTestCase):
    """実績の取り込みが共有を読めないのが続いたら、画面に1件。読めたら「直った」。"""

    def setUp(self) -> None:
        TroubleTestCase.setUp(self)
        SyncTestCase.setUp(self)

    def test_共有を読めないのが続いたら1件_読めたら直った(self) -> None:
        save(self.a)
        self.push(self.a)
        real = source_db._opened

        def unreadable(path, *, read_only):
            if read_only:
                raise source_db.SourceError("database is locked")
            return real(path, read_only=read_only)
        clock = [T0]
        with mock.patch.object(trouble.time, "time", side_effect=lambda: clock[0]):
            with mock.patch.object(source_db, "_opened", side_effect=unreadable):
                for _ in range(30):                                   # 15秒ごとに7分あまり
                    sync.import_from(self.b, self.path)
                    clock[0] += 15
            sync.import_from(self.b, self.path)
        kinds = [k for k, _t in self.shown()]
        self.assertEqual(kinds, [trouble.KIND_UNREADABLE, trouble.KIND_RECOVERED])


class GuardTests(SyncTestCase):
    """共有の実績の表の「同じ実績は1件だけ」(VER4.8.120)。"""

    def setUp(self) -> None:
        super().setUp()
        pattern_copies.forget_guard()
        self.addCleanup(pattern_copies.forget_guard)

    def copy_row_directly(self, pid: int) -> None:
        """古い版・ツールを通さない書き込みの真似: 同じ実績をもう1件入れる。"""
        with closing(sqlite3.connect(self.path)) as raw, raw:
            cols = [r[1] for r in raw.execute(f'PRAGMA table_info("{H}")') if r[1] not in ("実績ID", "送信ID")]
            quoted = ", ".join(f'"{c}"' for c in cols)
            raw.execute(f'INSERT INTO "{H}" ({quoted}) SELECT {quoted} FROM "{H}" WHERE 実績ID = ?', (pid,))

    def test_送るときに付き_ツールを通さない写しを共有が断る(self) -> None:
        save(self.a)
        self.push(self.a)
        with self.assertRaises(sqlite3.IntegrityError):
            self.copy_row_directly(1)
        with source_db.connect(self.path) as src:
            self.assertEqual(pattern_copies.guard_state(src), "")

    def test_写しが残っていれば付けられない_理由に片付け方(self) -> None:
        save(self.a)
        self.push(self.a)
        with closing(sqlite3.connect(self.path)) as raw, raw:
            raw.execute(f'DROP INDEX "{pattern_copies.GUARD_INDEX}"')
        self.copy_row_directly(1)
        pattern_copies.forget_guard()
        with source_db.connect(self.path) as src:
            self.assertFalse(pattern_copies.ensure_guard(src))
            why = pattern_copies.guard_state(src)
        self.assertIn("同じ実績が 1組", why)
        self.assertIn("--fix を実行", why)

    def test_取り込みのときにも付ける(self) -> None:
        from packaging_tool import sync_writeback
        save(self.a)
        self.push(self.a)
        with closing(sqlite3.connect(self.path)) as raw, raw:
            raw.execute(f'DROP INDEX "{pattern_copies.GUARD_INDEX}"')
        pattern_copies.forget_guard()
        lines = sync_writeback.ensure_guards(self.path)
        self.assertTrue(any("同じ実績は1件だけ" in line for line in lines), lines)

    def test_明細だけ違う同じ実績は足さずにつなぐ_送れないままにしない(self) -> None:
        save(self.a)
        self.push(self.a)
        sync.import_from(self.b, self.path)
        with self.b:
            self.b.execute(f'UPDATE "{H}" SET 取込元実績ID = NULL, 送信ID = NULL')
            self.b.execute(f'UPDATE "{ps.DETAIL_TABLES[1][0]}" SET 座標X = 99')
        pushed = self.push(self.b)
        self.assertEqual((pushed.sent, pushed.errors), (1, []))
        self.assertEqual(self.source(f'SELECT COUNT(*) FROM "{H}"')[0][0], 1)
        self.assertEqual(sync.pending(self.b), {})


if __name__ == "__main__":
    import unittest
    unittest.main()
