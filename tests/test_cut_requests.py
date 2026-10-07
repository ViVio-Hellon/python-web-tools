"""切断依頼を倉庫へ送る(`packaging_tool/cut_requests.py`)のテスト。

【ここで守りたいこと】
1. **送った紙面は固まる。** あとで資材選択をやり直しても中身は変わらず、倉庫では直せない
2. **状態はどの端末でも同じ答え。** 状態の行を同じ規則で古い順に読む(同時に押しても食い違わない)
3. **取り消しは送った端末だけ、倉庫が受け取る前だけ。** 間に合わなかったら、押した端末にそう言う
4. **同じロットを黙って2枚にしない。** 倉庫が開く前なら差し替え、受け取ったあとなら「もう1枚」を確かめる
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import cut_requests as cr, db, printing  # noqa: E402

FIELD = "GENBA-1"
SOUKO = "SOUKO"


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def paper(text: str = "幅カットのみ 1030×1500 3枚") -> printing.Report:
    report = printing.Report(title="ハードボード切断依頼書",
                             setup=printing.PageSetup(orientation=printing.PORTRAIT))
    report.add_sheet(f'<h1>切断依頼</h1><p>{text}</p>'
                     f'<p>担当 {printing.editable("tantou", "山田")}</p>')
    return report


def event(conn, rid: str, state: str, terminal: str, at: str) -> None:
    """ほかの端末が押した行(取り込みで届いたもの)。"""
    with conn:
        conn.execute(f"INSERT INTO {cr.EVENT_TABLE} (依頼ID, 状態, 端末, 側, 日時)"
                     " VALUES (?, ?, ?, ?, ?)", (rid, state, terminal, "", at))


class CutRequestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def send(self, lot: str = "1234567", terminal: str = FIELD, **kw) -> cr.Result:
        return cr.send(self.conn, kw.pop("report", paper()), lot_no=lot,
                       summary="ハードボード / 製品 3×1100×2000", terminal=terminal, **kw)

    def sent(self, **kw) -> str:
        result = self.send(**kw)
        self.assertTrue(result.ok, result.message)
        return result.request_id


class SendTests(CutRequestTestCase):
    def test_送ると未読で並ぶ(self) -> None:
        rid = self.sent()
        [req] = cr.recent(self.conn)
        self.assertEqual((req.request_id, req.lot_no, req.state, req.terminal),
                         (rid, "1234567", cr.SENT, FIELD))
        self.assertEqual(cr.unread_count(self.conn), 1)

    def test_紙面は送ったときのまま開き直せる(self) -> None:
        rid = self.sent()
        report = cr.report_of(self.conn, rid)
        self.assertEqual(report.title, "ハードボード切断依頼書")
        self.assertEqual(report.setup.orientation, printing.PORTRAIT)
        self.assertIn("幅カットのみ 1030×1500 3枚", report.sheets[0])
        self.assertIn("山田", report.sheets[0])

    def test_送った紙面は倉庫で直せない形にする(self) -> None:
        rid = self.sent()
        self.assertNotIn("contenteditable", cr.report_of(self.conn, rid).sheets[0])

    def test_同じロットを倉庫が開く前に送り直すと訊く(self) -> None:
        first = self.sent()
        again = self.send(report=paper("直した紙面"))
        self.assertFalse(again.ok)
        self.assertEqual(again.reason, cr.REFUSE_NEED_CONFIRM)
        self.assertIn("差し替えますか", again.message)
        self.assertEqual(again.request_id, first)
        self.assertEqual(len(cr.recent(self.conn)), 1)           # 黙って2枚にしない

    def test_差し替えると前のものは取り消して新しいほうだけが生きる(self) -> None:
        first = self.sent()
        second = self.sent(report=paper("直した紙面"), replace=True)
        by_id = {r.request_id: r for r in cr.recent(self.conn)}
        self.assertEqual(by_id[first].state, cr.CANCELLED)
        self.assertEqual(by_id[first].replaced_by, second)
        self.assertEqual(by_id[second].replaces, first)
        self.assertEqual([r.request_id for r in cr.open_for_lot(self.conn, "1234567")], [second])
        self.assertIn("直した紙面", cr.report_of(self.conn, second).sheets[0])

    def test_ほかの端末が送ったものは差し替えられない(self) -> None:
        self.sent(terminal="GENBA-2")
        result = self.send(replace=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, cr.REFUSE_NOT_MINE)

    def test_ほかの端末が送ったものがあれば差し替えでなくもう1枚かを訊く(self) -> None:
        """通し点検(デスクトップ版): 「差し替えますか？」→ 送る →「ここからは差し替え
        られません」と、できないことを訊いてから断っていた。"""
        self.sent(terminal="GENBA-2")
        result = self.send()
        self.assertEqual(result.reason, cr.REFUSE_NEED_CONFIRM)
        self.assertIn("別にもう1枚送りますか", result.message)
        self.assertNotIn("差し替えますか", result.message)
        self.assertTrue(self.send(again=True).ok)
        states = sorted((r.terminal, r.state) for r in cr.recent(self.conn))
        self.assertEqual(states, sorted([("GENBA-2", cr.SENT), (FIELD, cr.SENT)]))   # 前のものは残る

    def test_倉庫が受け取ったあとはもう1枚かを訊く(self) -> None:
        first = self.sent()
        event(self.conn, first, cr.RECEIVED, SOUKO, "2099-01-01 10:00:00")
        result = self.send()
        self.assertEqual(result.reason, cr.REFUSE_NEED_CONFIRM)
        self.assertIn("もう受け取っています", result.message)
        self.assertTrue(self.send(again=True).ok)
        states = sorted(r.state for r in cr.recent(self.conn))
        self.assertEqual(states, sorted([cr.RECEIVED, cr.SENT]))   # 前のものはそのまま

    def test_切り終えたロットはそのまま送れる(self) -> None:
        first = self.sent()
        event(self.conn, first, cr.CUT, SOUKO, "2099-01-01 10:00:00")
        self.assertTrue(self.send().ok)

    def test_別のロットは訊かずに送れる(self) -> None:
        self.sent()
        self.assertTrue(self.send(lot="7654321").ok)


class StateTests(CutRequestTestCase):
    def test_受け取って切る(self) -> None:
        rid = self.sent()
        self.assertTrue(cr.mark(self.conn, rid, cr.RECEIVED, terminal=SOUKO, side="倉庫").ok)
        self.assertEqual(cr.get(self.conn, rid).state, cr.RECEIVED)
        self.assertEqual(cr.unread_count(self.conn), 0)
        self.assertTrue(cr.mark(self.conn, rid, cr.CUT, terminal=SOUKO, side="倉庫").ok)
        req = cr.get(self.conn, rid)
        self.assertEqual(req.state, cr.CUT)
        self.assertEqual(req.by(cr.CUT)[0], SOUKO)
        self.assertTrue(cr.state_text(req).startswith("切った(倉庫 SOUKO "))

    def test_開かずに切ったら受け取ったことにもなる(self) -> None:
        rid = self.sent()
        self.assertTrue(cr.mark(self.conn, rid, cr.CUT, terminal=SOUKO, side="倉庫").ok)
        req = cr.get(self.conn, rid)
        self.assertEqual(req.state, cr.CUT)
        self.assertEqual(req.by(cr.RECEIVED)[0], SOUKO)

    def test_受け取りは何度開いても1回(self) -> None:
        rid = self.sent()
        for _ in range(3):
            cr.mark(self.conn, rid, cr.RECEIVED, terminal=SOUKO, side="倉庫")
        self.assertEqual(self.conn.execute(
            f"SELECT COUNT(*) FROM {cr.EVENT_TABLE}").fetchone()[0], 1)

    def test_送った端末は受け取られる前なら取り消せる(self) -> None:
        rid = self.sent()
        self.assertTrue(cr.mark(self.conn, rid, cr.CANCELLED, terminal=FIELD, side="現場").ok)
        self.assertEqual(cr.get(self.conn, rid).state, cr.CANCELLED)
        self.assertEqual(cr.unread_count(self.conn), 0)

    def test_ほかの端末は取り消せない(self) -> None:
        rid = self.sent()
        result = cr.mark(self.conn, rid, cr.CANCELLED, terminal="GENBA-2", side="現場")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, cr.REFUSE_NOT_MINE)
        self.assertEqual(cr.get(self.conn, rid).state, cr.SENT)

    def test_受け取られたあとは取り消せない(self) -> None:
        rid = self.sent()
        event(self.conn, rid, cr.RECEIVED, SOUKO, "2099-01-01 10:00:00")
        result = cr.mark(self.conn, rid, cr.CANCELLED, terminal=FIELD, side="現場")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, cr.REFUSE_STATE)
        self.assertIn("SOUKO", result.message)

    def test_取り消したものは切ったにできない(self) -> None:
        rid = self.sent()
        cr.mark(self.conn, rid, cr.CANCELLED, terminal=FIELD, side="現場")
        result = cr.mark(self.conn, rid, cr.CUT, terminal=SOUKO, side="倉庫")
        self.assertFalse(result.ok)
        self.assertEqual(cr.get(self.conn, rid).state, cr.CANCELLED)

    def test_同時に押されたら先に押したほうが効きどの端末でも同じ答え(self) -> None:
        """倉庫が開いたのと同じころ現場が取り消した。**日時の順に読む**ので、

        どの端末で読んでも同じ状態になる。間に合わなかった取り消しは、押した端末に言う。
        """
        rid = self.sent()
        event(self.conn, rid, cr.CANCELLED, FIELD, "2099-01-01 10:00:05")   # 少し遅れた
        event(self.conn, rid, cr.RECEIVED, SOUKO, "2099-01-01 10:00:01")
        req = cr.get(self.conn, rid)
        self.assertEqual(req.state, cr.RECEIVED)
        mine = cr.to_dict(req, terminal=FIELD, material=False)
        self.assertIn("先に倉庫が受け取っていた", mine["late"])
        self.assertFalse(mine["can_cancel"])
        self.assertEqual(cr.to_dict(req, terminal=SOUKO, material=True)["late"], "")

    def test_取り消しが先なら倉庫が開いても取り消しのまま(self) -> None:
        rid = self.sent()
        event(self.conn, rid, cr.RECEIVED, SOUKO, "2099-01-01 10:00:05")
        event(self.conn, rid, cr.CANCELLED, FIELD, "2099-01-01 10:00:01")
        self.assertEqual(cr.get(self.conn, rid).state, cr.CANCELLED)


class ViewTests(CutRequestTestCase):
    def test_できることはサーバが決める(self) -> None:
        rid = self.sent()
        req = cr.get(self.conn, rid)
        field = cr.to_dict(req, terminal=FIELD, material=False)
        self.assertEqual((field["can_cancel"], field["can_cut"], field["mine"]), (True, False, True))
        other = cr.to_dict(req, terminal="GENBA-2", material=False)
        self.assertFalse(other["can_cancel"])
        souko = cr.to_dict(req, terminal=SOUKO, material=True)
        self.assertEqual((souko["can_cancel"], souko["can_cut"]), (False, True))
        self.assertEqual(souko["state_text"], "倉庫はまだ開いていません")

    def test_終わっていないものを先に並べる(self) -> None:
        done = self.sent(lot="1111111")
        cr.mark(self.conn, done, cr.CUT, terminal=SOUKO, side="倉庫")
        live = self.sent(lot="2222222")
        self.assertEqual([r.request_id for r in cr.recent(self.conn)], [live, done])

    def test_表が無い古い手元DBでも落ちない(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.assertEqual(cr.recent(conn), [])
        self.assertIsNone(cr.report_of(conn, "x"))
        self.assertEqual(cr.unread_count(conn), 0)


if __name__ == "__main__":
    unittest.main()
