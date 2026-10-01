"""後から追えるログ(VER3.6.0)

現場の声:「エラー等の後追いが現状できないと感じている。ログを残し
なぜなぜで分析できるようにしておいてほしい。ログ出力は設定でパス指定
できるようにする」。

- 操作番号: 1つの操作の間に出た行に同じ `[番号]` が付く
- エラー記録: ERROR 以上はエラー番号付きで、操作・直前の動き・中身ごと残る
- 出力先: 設定画面で指定(端末名のフォルダ)、書けなければ断る/既定へ戻す
- 画面: 思わぬ例外・画面のエラー・処理の失敗に、エラー番号が出る
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402,F401
from packaging_tool import (config, logging_utils, trace_log,  # noqa: E402
                            user_settings)

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

PW = config.ADMIN_PASSWORD
log = logging_utils.get_logger("試験.trace")


class TraceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.default = self.root / "既定"
        self.default.mkdir()
        for target, attr, value in (
                (config, "LOG_DIR", self.default),
                (config, "LOG_DIR_FROM_ENV", False),
                (config, "_active_log_dir", None),
                (config, "USER_CONFIG_PATH", self.root / "user_config.json"),
                (logging_utils, "fallback_why", "")):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # 端末名・利用者は**ログに付ける所だけ**決める(本物の身元は画面も使う)
        for target, attr, value in (
                (trace_log, "_pc_name", lambda: "LINE-3"),
                (logging_utils, "_who",
                 lambda: {"pc": "LINE-3", "login": "yamada", "version": "9.9.9"})):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def records(self, folder: Path | None = None) -> list[dict]:
        folder = folder or config.log_dir()
        rows = []
        for path in folder.glob("エラー記録_*.jsonl"):
            rows += [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        return rows

    def day_log(self, folder: Path | None = None) -> str:
        folder = folder or config.log_dir()
        return logging_utils.log_path_for(date.today()).read_text("utf-8") \
            if folder == config.log_dir() else ""


# ==================================================================
# 書く側
# ==================================================================
class ErrorRecordTests(TraceTestCase):
    def test_エラーは番号付きで操作と直前の動きと中身ごと残る(self) -> None:
        token = logging_utils.begin_operation(
            {"id": "abc123", "method": "POST", "path": "/api/x", "input": '{"qty": 3}'})
        try:
            log.info("ボードを選びます")
            try:
                1 / 0
            except ZeroDivisionError:
                log.exception("選べませんでした")
        finally:
            logging_utils.end_operation(token)
        [row] = self.records()
        self.assertRegex(row["ref"], r"^E\d{6}-\d{6}-[0-9a-f]{2}$")
        self.assertEqual(row["message"], "選べませんでした")
        self.assertIn("ZeroDivisionError", row["detail"])
        self.assertEqual(row["operation"]["id"], "abc123")
        self.assertEqual(row["operation"]["input"], '{"qty": 3}')
        self.assertTrue(any("ボードを選びます" in line for line in row["recent"]))
        self.assertEqual((row["pc"], row["login"]), ("LINE-3", "yamada"))
        self.assertIn("version", row)

    def test_同じ操作の行には同じ番号が付く(self) -> None:
        token = logging_utils.begin_operation({"id": "op0001"})
        try:
            log.debug("細かい判断")
            log.info("決めました")
        finally:
            logging_utils.end_operation(token)
        log.info("操作の外")
        text = self.day_log()
        self.assertIn("[op0001] 細かい判断", text)
        self.assertIn("[op0001] 決めました", text)
        self.assertNotIn("[op0001] 操作の外", text)

    def test_ログファイルの行にもエラー番号が入る(self) -> None:
        """番号で grep すれば、ログファイルからも同じ1件に行ける。"""
        log.error("壊れました")
        [row] = self.records()
        self.assertIn(f"<{row['ref']}> 壊れました", self.day_log())

    def test_直前の動きにはトレースバックを入れず1件1行(self) -> None:
        try:
            raise ValueError("前のエラー")
        except ValueError:
            log.exception("前の失敗")
        log.error("次の失敗")
        last = self.records()[-1]
        self.assertFalse(any("Traceback" in line for line in last["recent"]))


# ==================================================================
# 出力先
# ==================================================================
class LogDirTests(TraceTestCase):
    def test_指定したフォルダの下の端末名のフォルダに書く(self) -> None:
        shared = self.root / "共有"
        result = trace_log.set_log_dir(str(shared), PW)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(config.log_dir(), shared / "LINE-3")
        log.error("共有へ")
        self.assertEqual(len(self.records(shared / "LINE-3")), 1)
        self.assertEqual(trace_log.state()["source"], trace_log.SOURCE_SETTING)

    def test_出力先を変えたら次の行から新しい場所へ(self) -> None:
        log.info("前の場所")
        trace_log.set_log_dir(str(self.root / "共有"), PW)
        log.info("新しい場所")
        new = (self.root / "共有" / "LINE-3"
               / f"packaging_tool_{date.today():%Y%m%d}.log").read_text("utf-8")
        old = (self.default / f"packaging_tool_{date.today():%Y%m%d}.log").read_text("utf-8")
        self.assertIn("新しい場所", new)
        self.assertNotIn("新しい場所", old)
        # 前の場所にも、移ったことを残す
        self.assertIn("ログの出力先を変えます", old)
        self.assertIn("ログの出力先をここに変えました", new)

    def test_パスワードが要る(self) -> None:
        result = trace_log.set_log_dir(str(self.root / "共有"), "ちがう")
        self.assertEqual(result.reason, trace_log.REFUSE_NEED_PASSWORD)
        self.assertEqual(config.log_dir(), self.default)

    def test_書けないフォルダは保存しない(self) -> None:
        blocker = self.root / "ファイル"
        blocker.write_text("x", encoding="utf-8")       # フォルダを作れない
        result = trace_log.set_log_dir(str(blocker), PW)
        self.assertFalse(result.ok)
        self.assertIn("保存していません", result.message)
        self.assertIsNone(user_settings.get(config.KEY_LOG_DIR))

    def test_相対の書き方は断る(self) -> None:
        self.assertFalse(trace_log.set_log_dir("logs", PW).ok)

    def test_空にすると既定へ戻る(self) -> None:
        trace_log.set_log_dir(str(self.root / "共有"), PW)
        self.assertTrue(trace_log.set_log_dir("", PW).ok)
        self.assertEqual(config.log_dir(), self.default)

    def test_起動時に書けなければ既定に書き理由を出す(self) -> None:
        """共有に届かない日でもログは残す(追いたいのはまさにその日)。"""
        blocker = self.root / "届かない"
        blocker.write_text("x", encoding="utf-8")
        user_settings.save(config.KEY_LOG_DIR, str(blocker))
        trace_log.apply_log_dir()
        self.assertEqual(config.log_dir(), self.default)
        self.assertIn("書けません", trace_log.state()["warn"])

    def test_環境変数で決めたときは変えられない(self) -> None:
        with mock.patch.object(config, "LOG_DIR_FROM_ENV", True):
            self.assertFalse(trace_log.set_log_dir(str(self.root / "共有"), PW).ok)
            self.assertTrue(trace_log.state()["locked"])

    def test_途中で書けなくなったら既定へ戻して書き続ける(self) -> None:
        trace_log.set_log_dir(str(self.root / "共有"), PW)
        handler = next(h for h in logging.getLogger("packaging_tool").handlers
                       if isinstance(h, logging_utils.DailyFileHandler))
        real_emit = logging.FileHandler.emit
        calls = []

        def flaky(self_, record):
            calls.append(record)
            if config._active_log_dir is not None:
                self_.handleError(record)       # 共有が切れた
                return
            real_emit(self_, record)

        with mock.patch.object(logging.FileHandler, "emit", flaky):
            log.info("切れたあとの行")
        self.assertIsNone(config._active_log_dir)
        self.assertIn("書けなくなった", trace_log.state()["warn"])
        self.assertIn("切れたあとの行",
                      (self.default / f"packaging_tool_{date.today():%Y%m%d}.log")
                      .read_text("utf-8"))
        self.assertIs(handler, handler)


class PruneTests(TraceTestCase):
    def test_古いログだけ消す(self) -> None:
        today = date(2026, 10, 1)
        for name in ("packaging_tool_20260101.log", "packaging_tool_20260920.log",
                     "取り込み診断_20260101.log", "エラー記録_202401.jsonl",
                     "エラー記録_202609.jsonl", "メモ.txt"):
            (self.default / name).write_text("x", encoding="utf-8")
        removed = trace_log.prune(today)
        self.assertEqual(set(removed), {"エラー記録_202401.jsonl",
                                        "取り込み診断_20260101.log",
                                        "packaging_tool_20260101.log"})
        self.assertTrue((self.default / "メモ.txt").exists())
        self.assertTrue((self.default / "エラー記録_202609.jsonl").exists())


# ==================================================================
# 読む側
# ==================================================================
class ReadTests(TraceTestCase):
    def test_一覧は新しい順で絞り込める(self) -> None:
        log.error("ボードが選べない")
        log.error("在庫が合わない")
        items = trace_log.list_errors()
        self.assertEqual([i.message for i in items], ["在庫が合わない", "ボードが選べない"])
        self.assertEqual([i.message for i in trace_log.list_errors(query="在庫")],
                         ["在庫が合わない"])
        ref = items[0].ref
        self.assertEqual([i.ref for i in trace_log.list_errors(query=ref)], [ref])

    def test_ほかの端末のぶんも見られる(self) -> None:
        shared = self.root / "共有"
        trace_log.set_log_dir(str(shared), PW)
        log.error("この端末")
        other = shared / "LINE-1"
        other.mkdir()
        (other / "エラー記録_202610.jsonl").write_text(json.dumps(
            {"ref": "E261001-090000-aa", "at": "2026/10/01 09:00:00",
             "message": "あちらの端末", "pc": "LINE-1"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        self.assertEqual([i.message for i in trace_log.list_errors()], ["この端末"])
        self.assertEqual({i.pc for i in trace_log.list_errors(everyone=True)},
                         {"LINE-3", "LINE-1"})

    def test_1件の中身に同じ操作番号の行が付く(self) -> None:
        token = logging_utils.begin_operation({"id": "fe12ab", "method": "POST",
                                               "path": "/api/selection/run"})
        try:
            log.debug("候補 1100×2000 を除外: 幅が足りない")
            log.error("候補がありません")
        finally:
            logging_utils.end_operation(token)
        log.info("別の操作")
        ref = trace_log.list_errors()[0].ref
        found = trace_log.find_error(ref)
        self.assertEqual(found["screen"], "POST /api/selection/run")
        self.assertTrue(any("幅が足りない" in line for line in found["operation_lines"]))
        self.assertFalse(any("別の操作" in line for line in found["operation_lines"]))
        self.assertIsNone(trace_log.find_error("E000000-000000-00"))

    def test_なぜなぜの下書き(self) -> None:
        token = logging_utils.begin_operation({"id": "aa0001", "method": "POST",
                                               "path": "/api/x", "input": '{"w": 1}'})
        try:
            log.error("失敗")
        finally:
            logging_utils.end_operation(token)
        text = trace_log.why_why_text(trace_log.find_error(trace_log.list_errors()[0].ref))
        for part in ("■ エラー番号", "■ どの端末で: LINE-3", "■ どの操作で: POST /api/x",
                     '■ そのときの入力: {"w": 1}', "■ 直前の動き", "なぜ1:", "なぜ5:",
                     "対策:", "■ エラーの中身"):
            self.assertIn(part, text)

    def test_壊れた行は飛ばす(self) -> None:
        log.error("ちゃんとした行")
        path = next(config.log_dir().glob("エラー記録_*.jsonl"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"ref": "書きかけ\n')
        self.assertEqual(len(trace_log.list_errors()), 1)


class MaskTests(unittest.TestCase):
    def test_パスワードは伏せ_長いものは縮める(self) -> None:
        text = trace_log.describe_input({"password": "秘密", "admin_pw": "x",
                                         "image": "data:" + "A" * 500, "qty": 3})
        self.assertNotIn("秘密", text)
        self.assertIn("(伏せます)", text)
        self.assertIn("(505文字)", text)
        self.assertIn('"qty": 3', text)


# ==================================================================
# 画面
# ==================================================================
@unittest.skipUnless(HAS_WEB, "flask が無い")
class WebTests(TraceTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app.routes import settings as routes
        from tests import _web
        self._web = _web
        _web.bind_db(self, routes)
        self.client = _web.make_client()
        self.app = self.client.application

    def test_思わぬ例外はエラー番号付きの500で返り記録に残る(self) -> None:
        @self.app.post("/api/試験/こわれる")
        def broken():                                   # noqa: ANN202
            raise KeyError("どこにも無い")
        res = self.client.post("/api/試験/こわれる", json={"password": "秘密", "qty": 2},
                               headers={**self._web.auth(), "X-Tool-Page": "/selection"})
        self.assertEqual(res.status_code, 500)
        ref = res.get_json()["error"]["ref"]
        self.assertIn(ref, res.get_json()["error"]["message"])
        found = trace_log.find_error(ref)
        self.assertIn("KeyError", found["detail"])
        self.assertEqual(found["operation"]["page"], "/selection")
        self.assertNotIn("秘密", json.dumps(found, ensure_ascii=False))

    def test_断ったときは理由を足跡に残す(self) -> None:
        res = self.client.post("/api/trace/log-dir", json={"path": "", "password": "x"},
                               headers=self._web.auth())
        self.assertEqual(res.status_code, 403)
        text = logging_utils.log_path_for(date.today()).read_text("utf-8")
        self.assertIn("断り POST /api/trace/log-dir → 403 need_password", text)
        self.assertIn("管理者パスワードが要ります", text)

    def test_操作の足跡は1要求1行_見張りは書かない(self) -> None:
        self.client.get("/api/trace/state", headers=self._web.auth())
        self.client.get("/api/jobs", headers=self._web.auth())
        text = logging_utils.log_path_for(date.today()).read_text("utf-8")
        self.assertIn("操作 GET /api/trace/state → 200", text)
        self.assertNotIn("操作 GET /api/jobs", text)

    def test_画面のエラーを受けて番号を返す(self) -> None:
        res = self.client.post("/api/client-error", json={
            "message": "x is not a function", "source": "/static/js/views/selection.js",
            "line": 12, "column": 3, "stack": "at foo", "page": "/selection"},
            headers=self._web.auth())
        ref = res.get_json()["ref"]
        found = trace_log.find_error(ref)
        self.assertEqual(found["screen"], "画面: /selection")
        self.assertEqual(found["detail"], "at foo")
        self.assertIn("selection.js:12:3", found["message"])

    def test_一覧と中身とその無いもの(self) -> None:
        log.error("試しのエラー")
        body = self.client.get("/api/trace/errors", headers=self._web.auth()).get_json()
        ref = body["items"][0]["ref"]
        detail = self.client.get(f"/api/trace/errors/{ref}", headers=self._web.auth())
        self.assertIn("なぜ1:", detail.get_json()["why_why"])
        missing = self.client.get("/api/trace/errors/E000000-000000-00",
                                  headers=self._web.auth())
        self.assertEqual(missing.status_code, 404)

    def test_設定画面にログの面がある(self) -> None:
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('id="panel-logs"', html)
        self.assertIn('id="logDir"', html)
        self.assertIn('id="errRows"', html)


class JobTests(TraceTestCase):
    def test_処理の失敗にもエラー番号(self) -> None:
        from packaging_tool import jobs
        import time
        registry = jobs.JobRegistry(store_path=self.root / "jobs.json")
        job = registry.start("試験", "試しの処理", lambda progress: 1 / 0)
        for _ in range(300):
            if registry.find(job.id).state != jobs.STATE_RUNNING:
                break
            time.sleep(0.01)
        error = registry.find(job.id).error
        self.assertRegex(error, r"エラー番号 E\d{6}-\d{6}-[0-9a-f]{2}")
        ref = error.split("エラー番号 ")[1].rstrip(")")
        found = trace_log.find_error(ref)
        self.assertEqual(found["screen"], "処理: 試しの処理")


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
