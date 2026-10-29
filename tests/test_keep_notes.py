"""あとから開ける「注意」の記録と、権限の表を読めなかったときの扱い(VER4.8.118)。

現場の声:「その操作はこの端末では使えません…となる。権限は付与されている。そもそもこういうものは
ログを出すべきではないのか？」「何のログも無いが検索一つに15分とかかかる事があるようです。その時
そのタイミングに立ち会えないのでログを見るしかないが出ていない。機能が生かし切れていない」。

【ここで守りたいこと】
1. 断った操作(権限・モード・混んでいる)・時間のかかった操作・返らない操作は、設定の「ログ」の面
   (エラー・注意の記録)に**種類つきで**残る。ふつうの WARNING は残さない(記録を埋めない)
2. 権限の表を**読めない**ことを「権限が無い」にしない。前に読めた権限で続け、そのことを残す
3. 資材モードの権限で断るときは、どの端末として見て・何が使えるかを言う
"""
from __future__ import annotations

import sqlite3
import time
import unittest
from unittest import mock

from packaging_tool import access_control as ac
from packaging_tool import logging_utils, modes, stall_watch, trace_log
from tests.test_trace_log import TraceTestCase, log

ME = ac.Identity(login_id="yamada", pc_name="NLM-PC-001")


def access_db(*rows: tuple[str, str, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute('CREATE TABLE "アクセス権限" (管理番号 INTEGER PRIMARY KEY, ログインID TEXT, PC名 TEXT,'
                 ' 権限 TEXT, 有効 INTEGER, 備考 TEXT)')
    for login, pc, permission in rows:
        conn.execute('INSERT INTO "アクセス権限" (ログインID, PC名, 権限, 有効, 備考) VALUES (?,?,?,1,"")',
                     (login, pc, permission))
    return conn


class _Busy:
    """アクセス権限の表を読むと「混んでいる」で断る接続(取り込みの最中の手元のDBの真似)。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.busy = True

    def execute(self, sql, *args):
        if self.busy and "アクセス権限" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self.conn.execute(sql, *args)


class KeptNoteTests(TraceTestCase):
    def kinds(self) -> list[tuple[str, str]]:
        logging_utils.flush_logs(5.0)
        return [(i.kind, i.message) for i in trace_log.list_errors()]

    def test_残すと決めた注意だけが記録に入る(self) -> None:
        log.warning("ふつうの注意(残さない)")
        log.warning("断った: 資材モードの権限が無い", extra={"keep": True, "kind": "断った操作"})
        log.error("思わぬエラー")
        self.assertEqual(self.kinds(), [("エラー", "思わぬエラー"),
                                        ("断った操作", "断った: 資材モードの権限が無い")])

    def test_注意にも番号が付いて開ける_種類でも絞り込める(self) -> None:
        log.warning("時間のかかった操作: GET /selection", extra={"keep": True, "kind": "時間のかかった操作"})
        [item] = trace_log.list_errors(query="時間のかかった")
        self.assertTrue(item.ref)
        self.assertIsNotNone(trace_log.find_error(item.ref))

    def test_指定したログの出力先にも種類つきで出る(self) -> None:
        """現場の声:「指定したパスには出さないんですか？」── 出す。その日のログにも、
        エラー・注意の記録(jsonl)にも、設定で指定した `<出力先>/packagingtool/` に書く。"""
        from datetime import date
        from tests.test_trace_log import PW
        shared = self.root / "指定した出力先"
        self.assertTrue(trace_log.set_log_dir(str(shared), PW).ok)
        log.warning("時間のかかった操作: GET /selection 45000ms",
                    extra={"keep": True, "kind": "時間のかかった操作"})
        logging_utils.flush_logs(5.0)
        folder = shared / "packagingtool"
        day = (folder / f"packagingtool_LINE-3_{date.today():%Y%m%d}.log").read_text("utf-8")
        self.assertIn("時間のかかった操作: GET /selection", day)
        [kept] = self.records(folder)
        self.assertEqual(kept["kind"], "時間のかかった操作")

    def test_返らない操作はどこで待っていたかと操作ごと残る(self) -> None:
        operation = {"id": "op-1", "method": "GET", "path": "/selection"}
        token = logging_utils.begin_operation(operation)
        try:
            stall_watch.begin("GET /selection")
        finally:
            logging_utils.end_operation(token)
        try:
            self.assertEqual(stall_watch.check(time.monotonic() + stall_watch.STALL_SEC + 1), 1)
        finally:
            stall_watch.end()
        logging_utils.flush_logs(5.0)
        [item] = [i for i in trace_log.list_errors() if i.kind == "返らない操作"]
        row = trace_log.find_error(item.ref)
        self.assertEqual(row["operation"]["path"], "/selection")
        self.assertIn("いまここにいます", row["detail"])


class UnreadableRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        ac._last_good.clear()
        self.addCleanup(ac._last_good.clear)
        patcher = mock.patch.object(ac, "READ_RETRY_SEC", (0.0,))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = access_db(("yamada", "", "mode:field"), ("yamada", "", "mode:material"))
        self.addCleanup(self.conn.close)

    def test_読めないときは前に読めた権限で続ける(self) -> None:
        self.assertTrue(ac.resolve(self.conn, ME).allows_mode(modes.MATERIAL))
        busy = _Busy(self.conn)
        grant = ac.resolve(busy, ME)
        self.assertTrue(grant.allows_mode(modes.MATERIAL))           # 現場だけに落とさない
        self.assertIn("読めなかった", grant.reason)

    def test_一度も読めていなければ現場だけ_理由に読めなかったことを書く(self) -> None:
        grant = ac.resolve(_Busy(self.conn), ME)
        self.assertEqual(grant.allowed_modes(), (modes.FIELD,))
        self.assertIn("database is locked", grant.reason)

    def test_表が無いのは今までどおり未取り込み(self) -> None:
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        grant = ac.resolve(empty, ME)
        self.assertIn("取り込まれていません", grant.reason)

    def test_少し待って読めればそのまま読む(self) -> None:
        busy = _Busy(self.conn)
        real_sleep = time.sleep

        def free_after_wait(seconds):
            busy.busy = False
            real_sleep(0)
        with mock.patch.object(ac, "READ_RETRY_SEC", (0.01,)), \
                mock.patch.object(time, "sleep", side_effect=free_after_wait):
            grant = ac.resolve(busy, ME)
        self.assertTrue(grant.allows_mode(modes.MATERIAL))
        self.assertEqual(grant.reason, "")


class ExplainTests(unittest.TestCase):
    def test_断るときにどの端末として見て何が使えるかを言う(self) -> None:
        conn = access_db(("Xfa-Ngykonpo", "NLM-NGY-232010", "mode:field"),
                         ("Xfa-Ngykonpo", "NLM-NGY-232010", "mode:fullaccess"))
        self.addCleanup(conn.close)
        me = ac.Identity(login_id="Xfa-Ngykonpo", pc_name="NLM-NGY-232010")
        text = ac.explain(ac.resolve(conn, me), conn)
        self.assertIn("PC NLM-NGY-232010", text)
        self.assertIn("使えるモード: 現場", text)
        # ほかのツールの権限かもしれないので誤りとは言わず、このツールでは読まないことだけ言う
        self.assertIn("mode:fullaccess はこのツールでは読みません", text)
        self.assertEqual(ac.problems(conn), [])


class RequestNoteTests(unittest.TestCase):
    """要求の足跡(`app._footprint`)が、断り・時間のかかった操作を残すと決めているか。"""

    def setUp(self) -> None:
        from tests import _web
        self.client = _web.make_client("field", grant=ac.grant_of("mode:field"))
        self.auth = _web.auth()

    def keeps(self, call) -> list[dict]:
        seen: list[dict] = []
        real = logging_utils.get_logger("app").warning

        def capture(msg, *args, **kwargs):
            extra = kwargs.get("extra") or {}
            if extra.get("keep"):
                seen.append({"kind": extra.get("kind"), "message": msg % args})
            return real(msg, *args, **kwargs)
        import app as app_module
        with mock.patch.object(app_module.log, "warning", side_effect=capture):
            call()
        return seen

    def test_資材モードの権限で断った操作を残す(self) -> None:
        got = self.keeps(lambda: self.client.post("/api/warehouse/confirm", json={"id": 1}, headers=self.auth))
        self.assertEqual([g["kind"] for g in got], ["断った操作"])
        self.assertIn("404", got[0]["message"])

    def test_時間のかかった操作を残す(self) -> None:
        import app as app_module
        with mock.patch.object(app_module, "SLOW_KEEP_MS", 0):
            got = self.keeps(lambda: self.client.get("/api/settings/state", headers=self.auth))
        self.assertIn("時間のかかった操作", [g["kind"] for g in got])

    def test_断りの文言にどの端末として見たかが入る(self) -> None:
        res = self.client.post("/api/warehouse/confirm", json={"id": 1}, headers=self.auth)
        self.assertEqual(res.status_code, 404)
        message = res.get_json()["error"]["message"]
        self.assertIn("この端末: ID", message)
        self.assertIn("使えるモード: 現場", message)


if __name__ == "__main__":
    unittest.main()
