"""現場 ⇔ 倉庫の受け渡し(2台と共有ファイル)の通し試験。

共有の表は**実物と同じ形**で作る ── Access から変換したもので、列に型が
無く、数も文字で入っている(`管理番号` も自動では振られない)。試験用に
型付きの表を作ると、実物でだけ起きる不具合(番号が空のまま足される等)
を見逃す。

手元の DB は端末ごとに1つ(A = 現場、B = 資材)。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from packaging_tool import config, data_sync, db, outbox_sync, source_db
from packaging_tool import pallet_service as ps
from packaging_tool import sync_writeback
from packaging_tool import warehouse_service as wh

ORDER = config.TBL_WAREHOUSE_ORDER
HISTORY = config.TBL_STOCK_HISTORY

ORDER_COLUMNS = ("管理番号", "登録日時", "LotNo", "品名", "発注コード", "発注数",
                 "単位", "材質", "調質", "厚", "幅", "丈", "用途コード", "納入先",
                 "取り消し済", "取り消し日時", "確認済み", "確認日時")
HISTORY_COLUMNS = ("管理番号", "幅", "丈", "業界", "記号", "位置", "数量", "区分",
                   "在庫数_更新後", "更新日時", "備考")
PALLET_COLUMNS = ("管理番号", "幅", "丈", "巾適合min", "巾適合max", "丈適合min",
                  "丈適合max", "業界", "記号", "位置", "在庫数", "更新日時",
                  "リスト管理", "桁数", "脚数", "コード", "単位", "備考")


def _untyped(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    conn.execute(f'CREATE TABLE "{table}" ({", ".join(f"{c!r}" for c in columns)})'
                 .replace("'", '"'))


def make_shared(path: Path) -> None:
    """変換した梱包資材マスタの形(型の無い列・文字の数・空の在庫数)。"""
    conn = sqlite3.connect(path)
    _untyped(conn, ORDER, ORDER_COLUMNS)
    _untyped(conn, HISTORY, HISTORY_COLUMNS)
    _untyped(conn, "PalletMaster", PALLET_COLUMNS)
    for no in (1, 2):
        conn.execute(
            f'INSERT INTO "{ORDER}" (管理番号, 登録日時, LotNo, 品名, 発注コード,'
            " 発注数, 単位, 厚, 幅, 丈) VALUES (?, '2026/09/24 14:23:54', ?,"
            " 'タイト', '060640', '3', '台', '111.200', '1362.0', '2534.0')",
            (no, f"L{no}"))
    # 在庫の無い型録の行(位置も在庫数も空)。実物は全行こうなっている
    conn.execute(
        'INSERT INTO "PalletMaster" (管理番号, 幅, 丈, 業界, 記号, 位置, 在庫数,'
        " 更新日時, リスト管理) VALUES (1, '1400', '2550', 'タイト', '', '', '',"
        " '2026/9/7 8:01', '')")
    conn.commit()
    conn.close()


def make_local() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


class HandoffBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="handoff_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        make_shared(self.src)
        self.a, self.b = make_local(), make_local()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        for conn in (self.a, self.b):
            data_sync.import_master(conn, self.src)

    def shared(self, sql: str, params=()) -> list[tuple]:
        conn = sqlite3.connect(self.src)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def send(self, conn: sqlite3.Connection) -> outbox_sync.WriteBackResult:
        return data_sync.write_back(conn, self.src)

    def refresh(self, conn: sqlite3.Connection) -> None:
        data_sync.refresh_orders(conn, self.src)

    @staticmethod
    def local_no(conn: sqlite3.Connection, lot: str) -> int:
        return int(conn.execute(
            f'SELECT 管理番号 FROM "{ORDER}" WHERE LotNo = ?', (lot,)).fetchone()[0])

    @staticmethod
    def status(conn: sqlite3.Connection, lot: str) -> tuple:
        row = conn.execute(
            f'SELECT COALESCE(確認済み, ""), COALESCE(取り消し済, "") FROM "{ORDER}"'
            " WHERE LotNo = ?", (lot,)).fetchone()
        return tuple(row) if row else ()


class MarkTests(HandoffBase):
    def test_取り消しを後から来た確認で消さない(self):
        """以前は印の列を4つとも手元の値で書き換えていた。確認した側の行は
        取消の列が空なので、**先に届いた取り消しを空で消し**、取り消した
        発注が「確認済み」で生き返っていた。"""
        wh.cancel_order(self.a, self.local_no(self.a, "L2"))
        self.send(self.a)
        self.assertEqual(self.shared(f'SELECT 取り消し済 FROM "{ORDER}" WHERE LotNo="L2"'),
                         [("1",)])

        wh.confirm_order(self.b, self.local_no(self.b, "L2"))     # Bはまだ古い一覧
        result = self.send(self.b)
        self.assertEqual(self.shared(
            f'SELECT COALESCE(確認済み,""), 取り消し済 FROM "{ORDER}" WHERE LotNo="L2"'),
            [("", "1")])
        self.assertTrue(any("先に「取り消し済」" in e for e in result.errors), result.errors)

        self.refresh(self.b)                  # 送れなかった印は下ろして、共有に揃う
        self.assertEqual(self.status(self.b, "L2"), ("", "1"))

    def test_確認済みを後から来た取り消しで消さない(self):
        wh.confirm_order(self.b, self.local_no(self.b, "L1"))
        self.send(self.b)
        wh.cancel_order(self.a, self.local_no(self.a, "L1"))      # Aはまだ古い一覧
        self.send(self.a)
        self.assertEqual(self.shared(
            f'SELECT 確認済み, COALESCE(取り消し済,"") FROM "{ORDER}" WHERE LotNo="L1"'),
            [("1", "")])
        self.refresh(self.a)
        self.assertEqual(self.status(self.a, "L1"), ("1", ""))

    def test_確認は現場に届き現場は取り消せなくなる(self):
        wh.confirm_order(self.b, self.local_no(self.b, "L1"))
        self.assertEqual(self.send(self.b).marked, {ORDER: 1})
        self.refresh(self.a)
        self.assertEqual(self.status(self.a, "L1"), ("1", ""))
        self.assertFalse(wh.cancel_order(self.a, self.local_no(self.a, "L1")).ok)


class RefusalTests(HandoffBase):
    def test_断る理由をいまの状態から言う(self):
        """以前は「見つからないか、確認済みか、取り消し済み」とまとめて言っていた。"""
        no = self.local_no(self.b, "L2")
        wh.cancel_order(self.b, no)
        self.assertIn("取り消し済み", wh.confirm_order(self.b, no).message)
        no = self.local_no(self.b, "L1")
        wh.confirm_order(self.b, no)
        self.assertIn("もう確認済み", wh.confirm_order(self.b, no).message)
        self.assertIn("確認済みのため、取り消せません", wh.cancel_order(self.b, no).message)
        self.assertIn("一覧にありません", wh.confirm_order(self.b, 99999).message)


class RouteTests(HandoffBase):
    """倉庫の画面から押したとき。押す直前に共有の変化を取り込む。"""

    def setUp(self) -> None:
        super().setUp()
        from unittest import mock

        from app.routes import warehouse as routes
        from packaging_tool import access_control, sync_sources
        from tests import _web

        _web.bind_db(self, routes, self.b)
        for target, value in ((sync_sources, "find_material_db"),
                              (data_sync, "write_back_in_background")):
            patcher = mock.patch.object(
                target, value,
                (lambda *a, **k: self.src) if value == "find_material_db"
                else (lambda *a, **k: None))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = _web.make_client(
            "material", port=8723, grant=access_control.grant_of("mode:material"))
        self.headers = _web.auth()

    def confirm(self, mgr_no: int):
        return self.client.post("/api/warehouse/confirm", json={"mgr_no": mgr_no},
                                headers=self.headers)

    def test_ほかの端末が取り消した発注は古い一覧から確認できない(self):
        stale = self.local_no(self.b, "L2")
        wh.cancel_order(self.a, self.local_no(self.a, "L2"))
        self.send(self.a)
        res = self.confirm(stale)
        self.assertEqual(res.status_code, 409)
        self.assertIn("取り消し済み", res.get_json()["error"]["message"])
        self.assertEqual(self.shared(
            f'SELECT COALESCE(確認済み,""), 取り消し済 FROM "{ORDER}" WHERE LotNo="L2"'),
            [("", "1")])

    def test_取り込みで番号が変わっても押した発注を確認する(self):
        stale = self.local_no(self.b, "L1")
        wh.create_order(self.a, lot_no="NEW", hinmei="テスト品", hatchu_code="X1",
                        tani="台", atu=1, haba=1000, take=2000, hatchu_suu=2)
        self.send(self.a)                            # 共有が変わる → 押す前に取り込み直す
        res = self.confirm(stale)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.status(self.b, "L1"), ("1", ""))
        self.assertEqual(self.status(self.b, "L2"), ("", ""))


class SentOrderTests(HandoffBase):
    def order(self, conn: sqlite3.Connection, lot: str) -> int:
        made = wh.create_order(conn, lot_no=lot, hinmei="テスト品", hatchu_code="X1",
                               tani="台", atu=1, haba=1000, take=2000, hatchu_suu=2)
        self.assertTrue(made.ok, made.message)
        return made.mgr_no

    def test_送った発注に共有の番号が振られる(self):
        """共有の 管理番号 は型の無い列で、自動では振られない。以前は空の
        まま足していたので、ほかの端末がその行を番号で指せなかった。"""
        self.order(self.a, "NEW1")
        self.order(self.a, "NEW2")
        self.send(self.a)
        self.assertEqual(self.shared(
            f'SELECT LotNo, 管理番号 FROM "{ORDER}" WHERE LotNo LIKE "NEW%" ORDER BY 1'),
            [("NEW1", 3), ("NEW2", 4)])

    def test_送った発注を倉庫が確認できる(self):
        self.order(self.a, "NEW")
        self.send(self.a)
        self.refresh(self.b)
        self.assertTrue(wh.confirm_order(self.b, self.local_no(self.b, "NEW")).ok)
        self.assertEqual(self.send(self.b).marked, {ORDER: 1})
        self.refresh(self.a)
        self.assertEqual(self.status(self.a, "NEW"), ("1", ""))

    def test_送信IDが消えた行にも取り消しが届く(self):
        """変換し直すと送信IDは空になる。その行は中身(登録日時・LotNo・品名)で探す。"""
        mgr_no = self.order(self.a, "NEW")
        self.send(self.a)
        conn = sqlite3.connect(self.src)
        conn.execute(f'UPDATE "{ORDER}" SET 送信ID = NULL')
        conn.commit()
        conn.close()
        wh.cancel_order(self.a, mgr_no)
        self.assertEqual(self.send(self.a).marked, {ORDER: 1})
        self.assertEqual(self.shared(f'SELECT 取り消し済 FROM "{ORDER}" WHERE LotNo="NEW"'),
                         [("1",)])

    def test_取り込み直しても同じ発注を指し直せる(self):
        """取り込みで手元の管理番号は振り直される。画面が持っている古い番号から
        同じ発注を探し直す(押す直前に取り込み直すため)。"""
        before_no = self.local_no(self.b, "L2")
        before = wh.identity(self.b, before_no)
        self.order(self.a, "NEW")
        self.send(self.a)
        self.refresh(self.b)
        after_no = wh.find_again(self.b, before_no, before)
        self.assertNotEqual(after_no, before_no)
        self.assertEqual(after_no, self.local_no(self.b, "L2"))


class SenderTests(HandoffBase):
    """取り消しは送った端末だけ。現場は複数台で使う。"""

    def order(self, conn, lot: str, terminal: str) -> None:
        made = wh.create_order(conn, lot_no=lot, hinmei="テスト品", hatchu_code="X1",
                               tani="台", atu=1, haba=1000, take=2000, hatchu_suu=1,
                               terminal=terminal)
        self.assertTrue(made.ok, made.message)

    def test_ほかの現場が送った発注は取り消せない(self):
        """以前は、現場Aから現場Bの発注を取り消せた(3台で確かめた)。"""
        self.order(self.a, "FROM_A", "PC-A")
        self.send(self.a)
        self.assertEqual(self.shared(f'SELECT 送信端末 FROM "{ORDER}" WHERE LotNo="FROM_A"'),
                         [("PC-A",)])                     # 共有に列を足して送る
        self.refresh(self.b)
        got = wh.cancel_order(self.b, self.local_no(self.b, "FROM_A"), terminal="PC-B")
        self.assertFalse(got.ok)
        self.assertIn("PC-A から送られたもの", got.message)
        self.assertEqual(self.status(self.b, "FROM_A"), ("", ""))
        # 送った端末からは取り消せる。取り込み直した後でも(記録は共有にある)
        self.refresh(self.a)
        self.assertTrue(wh.cancel_order(self.a, self.local_no(self.a, "FROM_A"),
                                        terminal="pc-a").ok)   # 大文字小文字は区別しない

    def test_送った端末が分からない発注はどの現場からも取り消せる(self):
        """この列を足す前の発注・VBAが入れた発注。誰にも取り消せなくはしない。"""
        self.assertTrue(wh.cancel_order(self.b, self.local_no(self.b, "L1"),
                                        terminal="PC-B").ok)

    def test_一覧は押せない理由を言う(self):
        from packaging_tool import modes
        from packaging_tool.presenters import warehouse as presenter
        self.order(self.a, "FROM_A", "PC-A")
        self.send(self.a)
        self.refresh(self.b)
        with mock.patch.object(wh, "this_terminal", lambda: "PC-B"):
            rows = {r.values["LotNo"]: r for r in
                    presenter.build(self.b, mode=modes.FIELD).rows}
        self.assertFalse(rows["FROM_A"].can_cancel)
        self.assertIn("PC-A が送った発注", rows["FROM_A"].why)
        self.assertEqual(rows["FROM_A"].values["送信端末"], "PC-A")
        self.assertTrue(rows["L1"].can_cancel)            # 送った端末が分からない行

    def test_共有に列を足せなくても発注は届く(self):
        """取り消しの絞り込みが効かないだけで、発注が止まるよりよい。"""
        self.order(self.a, "FROM_A", "PC-A")
        with mock.patch.object(sync_writeback, "ensure_shared_tables",
                               lambda *a, **k: []):
            result = self.send(self.a)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.shared(f'SELECT COUNT(*) FROM "{ORDER}" WHERE LotNo="FROM_A"'),
                         [(1,)])

    def test_共有に列が無くても取り込みで知らせない(self):
        result = data_sync.import_master(make_local(), self.src)
        self.assertFalse([w for w in result.warnings if "送信端末" in w], result.warnings)


class StockTests(HandoffBase):
    def stock(self, conn=None, position="A1") -> list[tuple]:
        sql = ("SELECT CAST(幅 AS INTEGER), CAST(丈 AS INTEGER), 位置,"
               " CAST(在庫数 AS INTEGER) FROM PalletMaster WHERE 位置 = ?")
        if conn is None:
            return self.shared(sql, (position,))
        return [tuple(r) for r in conn.execute(sql, (position,))]

    def test_受入の在庫数が共有へ届き取り込みで消えない(self):
        """以前は手元の PalletMaster だけを書き換えていた。共有へ届かず、
        ほかの端末から見えず、次の取り込み(総入れ替え)で手元からも消えた。"""
        ps.receive(self.a, width=1400, length=2550, qty=3, position="A1")
        self.send(self.a)
        self.assertEqual(self.stock(), [(1400, 2550, "A1", 3)])
        # 番号も振る(型の無い列なので自動では振られない)
        self.assertEqual(self.shared('SELECT 管理番号 FROM PalletMaster WHERE 位置="A1"'),
                         [(2,)])
        data_sync.import_master(self.b, self.src)
        self.assertEqual(self.stock(self.b), [(1400, 2550, "A1", 3)])
        data_sync.import_master(self.a, self.src)
        self.assertEqual(self.stock(self.a), [(1400, 2550, "A1", 3)])

    def test_2台が同じ棚へ入れたら両方の数が足される(self):
        ps.receive(self.a, width=1000, length=1000, qty=3, position="A1")
        ps.receive(self.b, width=1000, length=1000, qty=2, position="A1")
        self.send(self.a)
        self.send(self.b)
        self.assertEqual(self.stock(), [(1000, 1000, "A1", 5)])
        self.refresh(self.a)                    # 開いたままの端末にも出る
        self.assertEqual(self.stock(self.a), [(1000, 1000, "A1", 5)])

    def test_払い出して0なら共有からも消える(self):
        ps.receive(self.a, width=1000, length=1000, qty=2, position="A1")
        ps.issue(self.a, width=1000, length=1000, position="A1", qty=2)
        self.send(self.a)
        self.assertEqual(self.stock(), [])

    def test_リスト管理が要なら0でも残る(self):
        ps.receive(self.a, width=1000, length=1000, qty=2, position="A1")
        self.send(self.a)
        conn = sqlite3.connect(self.src)
        conn.execute('UPDATE PalletMaster SET リスト管理 = "要" WHERE 位置 = "A1"')
        conn.commit()
        conn.close()
        ps.issue(self.a, width=1000, length=1000, position="A1", qty=2)
        self.send(self.a)
        self.assertEqual(self.stock(), [(1000, 1000, "A1", 0)])

    def test_同じ送信IDで送り直しても在庫は1回しか動かない(self):
        ps.receive(self.a, width=1000, length=1000, qty=4, position="A1")
        self.send(self.a)
        spec = next(s for s in sync_writeback.WRITEBACK_SPECS if s.sqlite_table == HISTORY)
        ps.issue(self.a, width=1000, length=1000, position="A1", qty=1)
        rows, op_ids = outbox_sync.claim_rows(self.a, spec)
        # 届いたのに「送信中」のまま落ちた、を作る
        # 送るときと同じく、手元だけの列(作成元)は外す
        values = {k: rows[0][k] for k in rows[0].keys() if k not in ("id", spec.origin_column)}
        values["送信ID"] = op_ids[rows[0]["id"]]
        with source_db.connect(self.src) as src, src.transaction() as tx:
            tx.insert(HISTORY, values)
            sync_writeback.apply_stock(tx, values)
        self.a.execute("UPDATE Access同期記録 SET 同期日時 ="
                       " datetime('now','localtime','-1 hour') WHERE 状態 = '送信中'")
        self.a.commit()
        result = self.send(self.a)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.stock(), [(1000, 1000, "A1", 3)])

    def test_2台が最後の1台を払い出したら共有の履歴に残す(self):
        """現場が複数台だと、互いの払出が共有に届く前に同じ棚から出せる。
        在庫はマイナスにしないが、黙って0にすると後から追えない。"""
        ps.receive(self.a, width=1000, length=1000, qty=1, position="A1")
        self.send(self.a)
        self.refresh(self.b)
        ps.issue(self.a, width=1000, length=1000, position="A1", qty=1)
        ps.issue(self.b, width=1000, length=1000, position="A1", qty=1)
        self.send(self.a)
        self.send(self.b)
        self.assertEqual(self.stock(), [])
        notes = [r[0] for r in self.shared(
            f'SELECT COALESCE(備考, "") FROM "{HISTORY}" WHERE 区分 = "払出" ORDER BY rowid')]
        self.assertEqual(notes[0], "")
        self.assertIn("共有の在庫が足りませんでした: 在庫0・払出1", notes[1])

    def test_履歴を送れていないうちはPalletMasterを入れ替えない(self):
        ps.receive(self.a, width=1000, length=1000, qty=1, position="A1")
        unsent = sync_writeback._unsent_writeback_tables(self.a)
        self.assertIn(HISTORY, unsent)
        self.assertIn("PalletMaster", unsent)


if __name__ == "__main__":
    unittest.main()
