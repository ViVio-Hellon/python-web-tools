"""倉庫の「確認を外す」(確認済みの発注を未確認に戻す)の試験

現場の声:「倉庫モードで確認済みを取り消す機能を追加したい。互いに取り消しで DB 内がどうなるかの精査も」
決めたこと: どの資材端末からでも外せる / 外したら発注にコメントを残し現場が気づけるようにする /
「確認を外しますか？ 未確認に戻り、現場は取り消せるようになります」と訊く

【DB の中で起きること(ここで固定する)】
    未確認   … 確認済み 空 / 確認日時 空 / 取り消し済 空
    確認済み … 確認済み 1 / 確認日時 押した日時
    確認を外す … 確認済み・確認日時 を空(NULL)に戻す = 送った直後と同じ。発注は生きたまま
    取り消し(現場) … 取り消し済 1 / 取り消し日時。発注を無効にする(別の操作)

共有へは、印は「付けるだけ」として空を送らないので、外したことは 印未反映 に
「外す:<外した確認の日時>」として覚え、共有ではその日時の確認のときだけ空に戻す。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from packaging_tool import config, data_sync, db, order_comments, outbox_sync
from packaging_tool import warehouse_service as svc
from tests.test_data_sync import ImportDoesNotEchoBackTests, make_conn


class UnconfirmDbTests(unittest.TestCase):
    TABLE = config.TBL_WAREHOUSE_ORDER
    COLUMNS = ImportDoesNotEchoBackTests.COLUMNS + ("発注キー",)

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="unconfirm_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.src = self.dir / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(self.src)
        cols = ", ".join(f'"{c}"' for c in self.COLUMNS)
        conn.execute(f'CREATE TABLE "{self.TABLE}" (管理番号 INTEGER PRIMARY KEY, {cols})')
        for no in (41, 42):                       # 共有の番号は手元とずらす
            conn.execute(
                f'INSERT INTO "{self.TABLE}" (管理番号, 登録日時, LotNo, 品名, 発注コード, 単位,'
                ' 厚, 幅, 丈, 発注数, 発注キー)'
                " VALUES (?, '2026-10-13 09:00:00', ?, 'パレット', 'P9', '台', 0.0, 0, 0, 1, ?)",
                (no, f"L{no}", f"K{no}"))
        conn.commit()
        conn.close()
        self.conn = make_conn()
        self.addCleanup(self.conn.close)
        data_sync.import_master(self.conn, self.src)

    # -- 覗き見の道具 --------------------------------------------------
    def shared(self, lot: str) -> dict:
        conn = sqlite3.connect(self.src)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute(f'SELECT * FROM "{self.TABLE}" WHERE LotNo = ?', (lot,)).fetchone())
        finally:
            conn.close()

    def local(self, lot: str) -> dict:
        row = self.conn.execute(f'SELECT * FROM "{self.TABLE}" WHERE LotNo = ?', (lot,)).fetchone()
        return dict(row) if row else {}

    def no(self, lot: str) -> int:
        return int(self.local(lot)["管理番号"])

    def flags(self, row: dict) -> tuple:
        """印の4列。空は None にそろえる(手元は取り込みで空を '' にして持つ。どちらも「空」)。"""
        return tuple(row[c] or None for c in ("確認済み", "確認日時", "取り消し済", "取り消し日時"))

    def confirmed_and_sent(self, lot: str = "L41") -> str:
        """確認して共有へ届けた状態にする。確認日時を返す。"""
        self.assertTrue(svc.confirm_order(self.conn, self.no(lot)).ok)
        data_sync.write_back(self.conn, self.src)
        stamp = self.shared(lot)["確認日時"]
        self.assertEqual(self.shared(lot)["確認済み"], "1")
        return stamp

    def set_shared(self, lot: str, **values) -> None:
        conn = sqlite3.connect(self.src)
        sets = ", ".join(f'"{k}" = ?' for k in values)
        conn.execute(f'UPDATE "{self.TABLE}" SET {sets} WHERE LotNo = ?', (*values.values(), lot))
        conn.commit()
        conn.close()

    # -- 手元 ----------------------------------------------------------
    def test_外すと手元は送った直後と同じ姿になる(self) -> None:
        stamp = self.confirmed_and_sent()
        result = svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.flags(self.local("L41")), (None, None, None, None))
        # 共有へまだ送っていない印として覚える(外した確認の日時も一緒に)
        self.assertEqual(self.local("L41")["印未反映"], outbox_sync.UNMARK_PREFIX + stamp)
        # 発注の印と、現場へ知らせるコメントの2つが送り待ち
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn),
                         {self.TABLE: 1, config.TBL_ORDER_COMMENT: 1})

    def test_未確認や取消済みは外せない(self) -> None:
        result = svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        self.assertFalse(result.ok)
        self.assertIn("もう未確認です", result.message)
        svc.cancel_order(self.conn, self.no("L42"), terminal="")
        result = svc.unconfirm_order(self.conn, self.no("L42"), terminal="WH-1")
        self.assertFalse(result.ok)
        self.assertIn("取り消し済み", result.message)

    def test_現場が気づけるようコメントを残す(self) -> None:
        stamp = self.confirmed_and_sent()
        result = svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        self.assertIn("コメントを残しました", result.message)
        comments = order_comments.comments_for(self.conn, self.no("L41"), terminal="FIELD-1",
                                               side=order_comments.SIDE_FIELD)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].side, order_comments.SIDE_MATERIAL)
        self.assertIn("倉庫が確認を外しました", comments[0].text)
        self.assertIn(stamp.replace("T", " ")[:16], comments[0].text)
        # 現場から見ると未読(「新」で出る)
        summary = order_comments.summaries(self.conn, terminal="FIELD-1",
                                           side=order_comments.SIDE_FIELD)["K41"]
        self.assertEqual(summary.unread, 1)

    # -- 共有へ届く -----------------------------------------------------
    def test_外したことが共有へ届き取り込み直しても戻らない(self) -> None:
        """いちばん大事なところ。以前の作りでは空を送らないので、次の取り込みで確認が戻っていた。"""
        self.confirmed_and_sent()
        svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        result = data_sync.write_back(self.conn, self.src)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.flags(self.shared("L41")), (None, None, None, None))
        self.assertEqual(self.flags(self.shared("L42")), (None, None, None, None))   # 隣は触らない
        data_sync.import_master(self.conn, self.src)
        self.assertEqual(self.flags(self.local("L41")), (None, None, None, None))
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn), {})

    def test_送らずに取り込んでも先に送ってから入れ替える(self) -> None:
        self.confirmed_and_sent()
        svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        data_sync.import_master(self.conn, self.src)              # 書き戻さずに
        self.assertIsNone(self.shared("L41")["確認済み"])
        self.assertFalse(self.local("L41")["確認済み"])

    def test_ほかの端末が確認し直していたらそれは消さない(self) -> None:
        """外したあと共有へ送る前に、別の資材端末が(取り込んで)確認し直した。"""
        self.confirmed_and_sent()
        svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        self.set_shared("L41", 確認済み="1", 確認日時="2026-10-14 08:00:00")
        result = data_sync.write_back(self.conn, self.src)
        self.assertTrue(any("付け直されていた" in e for e in result.errors), result.errors)
        self.assertEqual(self.shared("L41")["確認日時"], "2026-10-14 08:00:00")
        data_sync.import_master(self.conn, self.src)             # 手元は共有に合わせる
        self.assertEqual(self.local("L41")["確認済み"], "1")

    def test_共有でもう外れていれば何もしない(self) -> None:
        self.confirmed_and_sent()
        svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        self.set_shared("L41", 確認済み=None, 確認日時=None)
        result = data_sync.write_back(self.conn, self.src)
        self.assertEqual(result.errors, [])
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn), {})

    def test_外してすぐ確認し直したら新しい確認が届く(self) -> None:
        self.confirmed_and_sent()
        svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        self.assertTrue(svc.confirm_order(self.conn, self.no("L41")).ok)
        # 外した覚えは残したまま、付け直しも待つ
        self.assertTrue(self.local("L41")["印未反映"].startswith(outbox_sync.UNMARK_PREFIX))
        new_stamp = self.local("L41")["確認日時"]
        result = data_sync.write_back(self.conn, self.src)
        self.assertEqual(result.errors, [])
        self.assertEqual((self.shared("L41")["確認済み"], self.shared("L41")["確認日時"]),
                         ("1", new_stamp))

    def test_外したあと現場は取り消せて共有にも届く(self) -> None:
        """外す → 共有へ届く → 現場が取り込む → 送った端末が取り消す。"""
        self.confirmed_and_sent()
        svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        data_sync.write_back(self.conn, self.src)
        field = make_conn()
        self.addCleanup(field.close)
        data_sync.import_master(field, self.src)
        no = int(field.execute(f'SELECT 管理番号 FROM "{self.TABLE}" WHERE LotNo = ?',
                               ("L41",)).fetchone()[0])
        self.assertTrue(svc.cancel_order(field, no, terminal="").ok)
        data_sync.write_back(field, self.src)
        row = self.shared("L41")
        self.assertEqual((row["確認済み"], row["取り消し済"]), (None, "1"))

    def test_外す前の確認がまだ共有へ届いていなくても困らない(self) -> None:
        """確認して、送る前に外した。共有はずっと未確認のまま。"""
        svc.confirm_order(self.conn, self.no("L41"))
        svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        result = data_sync.write_back(self.conn, self.src)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.flags(self.shared("L41")), (None, None, None, None))
        self.assertEqual(data_sync._unsent_writeback_tables(self.conn), {})

    def test_送れないあいだは覚えたまま(self) -> None:
        self.confirmed_and_sent()
        svc.unconfirm_order(self.conn, self.no("L41"), terminal="WH-1")
        self.src.rename(self.dir / "見えない.sqlite3")
        try:
            data_sync.write_back(self.conn, self.src)
        except Exception:                          # noqa: BLE001 - 見えないときの返し方は問わない
            pass
        self.assertTrue(self.local("L41")["印未反映"].startswith(outbox_sync.UNMARK_PREFIX))


class UnconfirmWebTests(unittest.TestCase):
    """画面とURL。確認と同じく資材モードだけ。"""

    def setUp(self) -> None:
        from tests import test_web_warehouse as web
        self.web = web
        case = web.WarehouseWebTestCase("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case = case

    def post(self, mode: str, path: str, mgr_no: int):
        return self.case.clients[mode].post(path, json={"mgr_no": mgr_no}, headers=self.case.auth())

    def rows(self, mode: str) -> list:
        return self.case.clients[mode].get("/api/warehouse/orders",
                                           headers=self.case.auth()).get_json()["rows"]

    def test_資材は確認済みの行で外せて現場には出ない(self) -> None:
        mgr_no = self.case.send().get_json()["mgr_no"]
        self.assertFalse(any(r["can_unconfirm"] for r in self.rows("material")))   # 未確認では出ない
        self.assertEqual(self.post("material", "/api/warehouse/confirm", mgr_no).status_code, 200)
        self.assertTrue(any(r["can_unconfirm"] for r in self.rows("material")))
        self.assertFalse(any(r["can_unconfirm"] for r in self.rows("field-allowed")))
        res = self.post("material", "/api/warehouse/unconfirm", mgr_no)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIn("確認を外しました", res.get_json()["message"])
        row = next(r for r in self.rows("field-allowed") if r["mgr_no"] == mgr_no)
        self.assertEqual(row["status"], svc.STATUS_PENDING)
        self.assertTrue(row["can_cancel"])                     # 現場はまた取り消せる

    def test_現場からは外せない(self) -> None:
        mgr_no = self.case.send().get_json()["mgr_no"]
        self.post("material", "/api/warehouse/confirm", mgr_no)
        self.assertEqual(self.post("field", "/api/warehouse/unconfirm", mgr_no).status_code, 404)
        self.assertEqual(self.post("field-allowed", "/api/warehouse/unconfirm", mgr_no).status_code, 403)

    def test_二度押しは断る(self) -> None:
        mgr_no = self.case.send().get_json()["mgr_no"]
        self.post("material", "/api/warehouse/confirm", mgr_no)
        self.post("material", "/api/warehouse/unconfirm", mgr_no)
        res = self.post("material", "/api/warehouse/unconfirm", mgr_no)
        self.assertEqual(res.status_code, 409)
        self.assertIn("もう未確認です", res.get_json()["error"]["message"])

    def test_訊く文(self) -> None:
        from packaging_tool.presenters import warehouse as presenter
        ask = presenter.ORDER_ASK["unconfirm"]
        self.assertEqual(f"{ask['title']} {ask['why']}",
                         "確認を外しますか？ 未確認に戻り、現場は取り消せるようになります。")


if __name__ == "__main__":
    unittest.main()
