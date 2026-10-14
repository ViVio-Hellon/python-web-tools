"""倉庫2台・現場1台で「確認済みにする」「取り消す」「確認を外す」を織り交ぜる試験

現場の声:「倉庫複数台で『確認済みにする』、現場が『取り消す』、倉庫で『確認を外す』を織り交ぜたテストを」

【組み方】
- 共有の梱包資材マスタは1つ。端末ごとに手元のDBを持つ(W1・W2 = 倉庫、F1 = 送った現場)
- 「押す」は画面のボタンと同じ手順(`app/routes/warehouse._action`):
  押す前に共有から取り込み直す → 操作 → すぐ共有へ送る
- 「届かない端末」は共有に繋がらない状態。押しても手元だけが変わり、繋がったとき(`sync`)に送る
- 日時は1秒ずつ進む時計にする(同じ秒に確認し直すと、どの確認か見分けられないため)

各段のあとで、共有と全端末の手元の印(確認済み・取り消し済)と、ボタンの出方を見る。
"""
from __future__ import annotations

import itertools
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from packaging_tool import config, data_sync, db, modes, order_comments, order_export
from packaging_tool import warehouse_service as svc
from packaging_tool.presenters import warehouse as presenter
from tests import test_data_sync
from tests.test_data_sync import make_conn

TABLE = config.TBL_WAREHOUSE_ORDER
PENDING, CONFIRMED, CANCELLED = svc.STATUS_PENDING, svc.STATUS_CONFIRMED, svc.STATUS_CANCELLED


class Terminal:
    """1台の端末(手元のDBと、共有に繋がっているか)。"""

    def __init__(self, name: str, src: Path, *, material: bool) -> None:
        self.name, self.src, self.material = name, src, material
        self.online = True
        self.conn = make_conn()
        data_sync.import_master(self.conn, src)

    # -- 操作 ---------------------------------------------------------------
    def press(self, action: str, key: str) -> svc.ActionResult:
        """ボタンを押す。画面と同じく、押す前に取り込み直し、押したらすぐ送る。"""
        if self.online:
            data_sync.refresh_orders(self.conn, self.src, only_if_changed=False)
        no = self.no(key)
        if action == "確認済みにする":
            result = svc.confirm_order(self.conn, no)
        elif action == "取り消す":
            result = svc.cancel_order(self.conn, no, terminal=self.name)
        elif action == "確認を外す":
            result = svc.unconfirm_order(self.conn, no, terminal=self.name)
        else:
            raise ValueError(action)
        if self.online:
            data_sync.write_back(self.conn, self.src)
        return result

    def sync(self) -> list[str]:
        """繋がった。送れていないものを送ってから、共有に合わせる。"""
        self.online = True
        errors = list(data_sync.write_back(self.conn, self.src).errors)
        data_sync.refresh_orders(self.conn, self.src, only_if_changed=False)
        return errors

    # -- 見る ---------------------------------------------------------------
    def no(self, key: str) -> int:
        return int(self.conn.execute(f'SELECT 管理番号 FROM "{TABLE}" WHERE 発注キー = ?',
                                     (key,)).fetchone()[0])

    def status(self, key: str) -> str:
        row = self.conn.execute(f'SELECT * FROM "{TABLE}" WHERE 発注キー = ?', (key,)).fetchone()
        return svc._status_of(row)

    def buttons(self, key: str) -> list[str]:
        with mock.patch.object(svc, "this_terminal", return_value=self.name):
            view = presenter.build(self.conn, mode=modes.MATERIAL if self.material else modes.FIELD,
                                   include_cancelled=True)
        row = next(r for r in view.rows if r.mgr_no == self.no(key))
        return [label for label, on in (("確認済みにする", row.can_confirm),
                                        ("取り消す", row.can_cancel),
                                        ("確認を外す", row.can_unconfirm)) if on]


class OrderMarksMultiTerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="marks_multi_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.src = self.dir / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(self.src)
        cols = ", ".join(f'"{c}"' for c in test_data_sync.ImportDoesNotEchoBackTests.COLUMNS + ("発注キー", "送信端末"))
        conn.execute(f'CREATE TABLE "{TABLE}" (管理番号 INTEGER PRIMARY KEY, {cols})')
        conn.commit()
        conn.close()
        # 1秒ずつ進む時計
        start = datetime(2026, 10, 14, 9, 0, 0)
        ticks = (start + timedelta(seconds=i) for i in itertools.count())
        clock = mock.patch.object(db, "now_db_string",
                                  side_effect=lambda: next(ticks).strftime("%Y-%m-%d %H:%M:%S"))
        clock.start()
        self.addCleanup(clock.stop)

        self.W1 = Terminal("W1", self.src, material=True)
        self.W2 = Terminal("W2", self.src, material=True)
        self.F1 = Terminal("F1", self.src, material=False)
        self.log: list[str] = []

    # -- 道具 -------------------------------------------------------------------
    def send(self) -> str:
        """F1 が発注を出す。発注キーを返す。"""
        made = svc.create_order(self.F1.conn, lot_no="L1", hinmei="タイト　　1150x2650",
                                hatchu_code="058901", tani="台", atu=220, haba=1122, take=2502,
                                hatchu_suu=1, terminal="F1")
        self.F1.conn.commit()
        data_sync.write_back(self.F1.conn, self.src)
        key = self.F1.conn.execute(f'SELECT 発注キー FROM "{TABLE}" WHERE 管理番号 = ?',
                                   (made.mgr_no,)).fetchone()[0]
        for t in (self.W1, self.W2):
            data_sync.refresh_orders(t.conn, self.src, only_if_changed=False)
        return key

    def shared(self, key: str) -> dict:
        conn = sqlite3.connect(self.src)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute(f'SELECT * FROM "{TABLE}" WHERE 発注キー = ?', (key,)).fetchone())
        finally:
            conn.close()

    def shared_status(self, key: str) -> str:
        row = self.shared(key)
        return svc._status_of({k: (v or "") for k, v in row.items()})

    def step(self, who: Terminal, action: str, key: str, *, ok: bool) -> svc.ActionResult:
        result = who.press(action, key)
        self.log.append(f"{who.name}{'(届かない)' if not who.online else ''} {action}: "
                        f"{'○' if result.ok else '×'} {result.message} → 共有 {self.shared_status(key)}")
        self.assertEqual(result.ok, ok, f"{who.name} {action}: {result.message}")
        return result

    def everyone(self, key: str, expected: str) -> None:
        """共有と、繋がっている端末の手元がみな同じ状態か(手元は取り込み直してから見る)。"""
        self.assertEqual(self.shared_status(key), expected, "共有")
        for t in (self.W1, self.W2, self.F1):
            if t.online:
                data_sync.refresh_orders(t.conn, self.src, only_if_changed=False)
                self.assertEqual(t.status(key), expected, t.name)

    def comments(self, key: str) -> list[str]:
        data_sync.refresh_orders(self.F1.conn, self.src, only_if_changed=False)
        got = order_comments.comments_for(self.F1.conn, self.F1.no(key), terminal="F1",
                                          side=order_comments.SIDE_FIELD)
        return [c.text for c in got]

    def tearDown(self) -> None:
        for line in self.log:
            print(line)

    # -- 1. ひととおり(みな繋がっている) ------------------------------------------
    def test_確認_取消を断る_外す_取り消す_確認も外すも断る(self) -> None:
        key = self.send()
        self.assertEqual(self.W1.buttons(key), ["確認済みにする"])
        self.assertEqual(self.F1.buttons(key), ["取り消す"])

        self.step(self.W1, "確認済みにする", key, ok=True)
        self.everyone(key, CONFIRMED)
        self.assertEqual(self.W2.buttons(key), ["確認を外す"])        # ほかの倉庫からも外せる
        self.assertEqual(self.F1.buttons(key), [])                     # 現場は取り消せない

        result = self.step(self.F1, "取り消す", key, ok=False)
        self.assertIn("倉庫が確認済み", result.message)

        self.step(self.W2, "確認を外す", key, ok=True)                 # 確認した W1 でなく W2 が外す
        self.everyone(key, PENDING)
        self.assertEqual(self.F1.buttons(key), ["取り消す"])
        self.assertEqual(self.W1.buttons(key), ["確認済みにする"])
        self.assertEqual(len(self.comments(key)), 1)

        self.step(self.F1, "取り消す", key, ok=True)
        self.everyone(key, CANCELLED)
        result = self.step(self.W1, "確認済みにする", key, ok=False)
        self.assertIn("取り消し済み", result.message)
        result = self.step(self.W2, "確認を外す", key, ok=False)
        self.assertIn("取り消し済み", result.message)
        self.everyone(key, CANCELLED)
        self.assertEqual(self.W1.buttons(key), [])
        # 共有の印の列: 確認は空、取消が付いている
        row = self.shared(key)
        self.assertEqual((row["確認済み"] or None, row["確認日時"] or None, row["取り消し済"]),
                         (None, None, "1"))

    def test_確認と外すを何度も繰り返しても最後の操作どおり(self) -> None:
        key = self.send()
        for who_confirm, who_unconfirm in ((self.W1, self.W2), (self.W2, self.W1), (self.W1, self.W1)):
            self.step(who_confirm, "確認済みにする", key, ok=True)
            self.everyone(key, CONFIRMED)
            self.step(who_unconfirm, "確認を外す", key, ok=True)
            self.everyone(key, PENDING)
        self.step(self.W2, "確認済みにする", key, ok=True)
        self.everyone(key, CONFIRMED)
        self.assertEqual(len(self.comments(key)), 3)                   # 外した3回ぶん
        # 発注データの集計は確認済みだけを数える(最後は確認済み)
        rows = self.F1.conn.execute(f'SELECT * FROM "{TABLE}"').fetchall()
        self.assertEqual(order_export.summarize(rows).unconfirmed, 0)

    # -- 2. 古い画面のまま押す(押す前の取り込み直しが効く) ------------------------
    def test_ほかの倉庫が外したあと古い画面から確認しても二重にならない(self) -> None:
        key = self.send()
        self.step(self.W1, "確認済みにする", key, ok=True)
        # W2 は「確認済み」と見ている画面のまま。W1 が外す
        self.step(self.W1, "確認を外す", key, ok=True)
        # W2 が古い画面の「確認を外す」を押す → 押す前に取り込み直すので、もう未確認と分かる
        result = self.step(self.W2, "確認を外す", key, ok=False)
        self.assertIn("もう未確認です", result.message)
        self.everyone(key, PENDING)

    def test_ほかの倉庫が確認したあと古い画面から確認しても断る(self) -> None:
        key = self.send()
        self.step(self.W1, "確認済みにする", key, ok=True)
        result = self.step(self.W2, "確認済みにする", key, ok=False)
        self.assertIn("もう確認済み", result.message)
        self.everyone(key, CONFIRMED)

    # -- 3. 共有に届かない端末が混ざる(本当に重なる) -------------------------------
    def test_外したのが届く前は現場は取り消せず届いたら取り消せる(self) -> None:
        key = self.send()
        self.step(self.W1, "確認済みにする", key, ok=True)
        self.W1.online = False
        self.step(self.W1, "確認を外す", key, ok=True)                  # W1 の手元だけ未確認
        self.assertEqual(self.shared_status(key), CONFIRMED)
        result = self.step(self.F1, "取り消す", key, ok=False)          # 共有はまだ確認済み
        self.assertIn("倉庫が確認済み", result.message)
        self.assertEqual(self.W1.sync(), [])                            # 繋がって届く
        self.everyone(key, PENDING)
        self.step(self.F1, "取り消す", key, ok=True)
        self.everyone(key, CANCELLED)

    def test_外したのが届く前にほかの倉庫が外して確認し直したらその確認を残す(self) -> None:
        key = self.send()
        self.step(self.W1, "確認済みにする", key, ok=True)
        self.W1.online = False
        self.step(self.W1, "確認を外す", key, ok=True)                  # 9:00 台の確認を外した(まだ届かない)
        self.step(self.W2, "確認を外す", key, ok=True)
        self.step(self.W2, "確認済みにする", key, ok=True)              # 新しい確認
        stamp = self.shared(key)["確認日時"]
        errors = self.W1.sync()                                          # W1 の古い「外す」が届く
        self.assertTrue(any("付け直されていた" in e for e in errors), errors)
        self.everyone(key, CONFIRMED)
        self.assertEqual(self.shared(key)["確認日時"], stamp)            # W2 の確認は消えていない
        self.assertEqual(self.W1.status(key), CONFIRMED)                 # W1 の手元も共有に合わせた

    def test_届かない倉庫が確認するあいだに現場が取り消したら取消が勝つ(self) -> None:
        key = self.send()
        self.W1.online = False
        self.step(self.W1, "確認済みにする", key, ok=True)              # W1 の手元だけ確認済み
        self.step(self.F1, "取り消す", key, ok=True)                    # 共有は取消
        errors = self.W1.sync()
        self.assertTrue(any("取り消し済" in e for e in errors), errors)  # 取消の発注に確認を付けない
        self.everyone(key, CANCELLED)

    def test_届かない現場が取り消すあいだに倉庫が確認したら確認が勝つ(self) -> None:
        key = self.send()
        self.F1.online = False
        self.step(self.F1, "取り消す", key, ok=True)                    # F1 の手元だけ取消
        self.step(self.W1, "確認済みにする", key, ok=True)
        errors = self.F1.sync()
        self.assertTrue(any("確認済み" in e for e in errors), errors)   # 確認済みを取り消さない
        self.everyone(key, CONFIRMED)

    def test_届かない倉庫が外してすぐ確認し直したら新しい確認が届く(self) -> None:
        key = self.send()
        self.step(self.W1, "確認済みにする", key, ok=True)
        old = self.shared(key)["確認日時"]
        self.W1.online = False
        self.step(self.W1, "確認を外す", key, ok=True)
        self.step(self.W1, "確認済みにする", key, ok=True)
        self.assertEqual(self.W1.sync(), [])
        self.everyone(key, CONFIRMED)
        self.assertNotEqual(self.shared(key)["確認日時"], old)

    def test_届かない倉庫2台がどちらも外したら一度だけ未確認になる(self) -> None:
        key = self.send()
        self.step(self.W1, "確認済みにする", key, ok=True)
        self.W2.sync()                                                  # W2 も一覧で確認済みを見てから
        self.W1.online = self.W2.online = False
        self.step(self.W1, "確認を外す", key, ok=True)
        self.step(self.W2, "確認を外す", key, ok=True)
        self.assertEqual(self.W1.sync(), [])
        self.assertEqual(self.W2.sync(), [])                            # もう外れている → 何もしない
        self.everyone(key, PENDING)
        self.assertEqual(len(self.comments(key)), 2)                    # コメントはそれぞれ残る

    def test_外したのが届く前に同じ倉庫が現場の取消も見て最後は取消(self) -> None:
        """W1 が届かないまま外す → 繋がって届く → 現場が取り消す → W1 が古い画面で確認を押す。"""
        key = self.send()
        self.step(self.W1, "確認済みにする", key, ok=True)
        self.W1.online = False
        self.step(self.W1, "確認を外す", key, ok=True)
        self.W1.sync()
        self.step(self.F1, "取り消す", key, ok=True)
        result = self.step(self.W1, "確認済みにする", key, ok=False)
        self.assertIn("取り消し済み", result.message)
        self.everyone(key, CANCELLED)


if __name__ == "__main__":
    unittest.main()
