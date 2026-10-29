"""モード切替のフリーズ・起動し直しても設定が開けない(VER4.8.115)

現場の声:「資材と現場を切り替えると時々フリーズする。一度でもフリーズすると起動しなおしても
設定タブが開けなくなる。現場資材を切り替えれなくなる。設定も開けなくなるのでログすらみれない」

見つかった出どころと直し:
1. 画面の要求の中で共有フォルダを取り込み直していた(設定・簡易在庫・倉庫連携・送る前の確かめ)
   → 背景の1本で走らせ、画面は決めた秒数だけ待つ(`bounded`)
2. 設定画面が開くたびに取り込み元を開いて確かめていた → 背景で、待つのは上限まで(`shared_facts`)
3. ログを1行ごとにその場でファイルへ書いていた(出力先が共有だと、共有が遅いだけで全部止まる)
   → 書くのは別のスレッド(`logging_utils._AsyncWriter`)
4. 書く要求が前の要求をいつまでも待った → 上限で断る(`WRITE_WAIT_SEC`)
5. 起動し直したとき、固まった前のプロセス・モードを切り替えたプロセスを「別物」と見て2つ目を
   立ち上げていた → 自分と分かれば合流、固まっていれば終わらせて立て直す(`launch_guard`)
6. 両方の権限がある端末は現場から開く
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import launch_guard  # noqa: E402
from packaging_tool import (access_control, bounded, config, data_sync, db,  # noqa: E402
                            log_names, logging_utils, stall_watch)
from packaging_tool.presenters import settings as presenter  # noqa: E402
from tests import _web  # noqa: E402


# ==================================================================
# 1. 背景の1本・待つのは上限まで
# ==================================================================
class FlightTests(unittest.TestCase):
    def test_待つのは上限まで_走っている間は2本目を始めない(self) -> None:
        calls = []
        release = threading.Event()

        def work():
            calls.append(1)
            release.wait(5)
            return "済"

        flight = bounded.Flight("試験", work)
        began = time.monotonic()
        first = flight.run(0.1)
        second = flight.run(0.1)
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertFalse(first.done)
        self.assertFalse(second.done)
        self.assertEqual(len(calls), 1, "走っている最中に2本目を始めています")
        release.set()
        self.assertTrue(flight.wait_idle(5))

    def test_待つのをやめたあとに終わった結果は次の人へ渡す(self) -> None:
        release = threading.Event()
        flight = bounded.Flight("試験", lambda: (release.wait(5), "届いた")[1])
        self.assertFalse(flight.run(0.05).done)
        release.set()
        flight.wait_idle(5)
        nxt = flight.run(2.0)
        self.assertEqual(nxt.late, "届いた")
        self.assertIsNone(flight.take_late(), "同じ結果を2度渡しています")

    def test_失敗は握って返す(self) -> None:
        def boom():
            raise OSError("共有が無い")
        outcome = bounded.Flight("試験", boom).run(2.0)
        self.assertTrue(outcome.done)
        self.assertIsInstance(outcome.error, OSError)

    def test_メモリ上のDBはその場で走らせる(self) -> None:
        conn = _web.memory_db()
        self.addCleanup(conn.close)
        seen = []
        flights = bounded.FileFlights("試験", lambda c: seen.append(c) or 1)
        self.assertEqual(flights.run(conn, 0.1).value, 1)
        self.assertIs(seen[0], conn)

    def test_ファイルのDBは背景で開き直す(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="flight_"))
        path = tmp / "手元.db"
        conn = db.get_connection(path)
        self.addCleanup(conn.close)
        seen = []
        flights = bounded.FileFlights("試験", lambda c: seen.append((c, threading.get_ident())) or 1)
        self.assertEqual(flights.run(conn, 5.0).value, 1)
        self.assertIsNot(seen[0][0], conn)
        self.assertNotEqual(seen[0][1], threading.get_ident())


class CatchUpTests(unittest.TestCase):
    """発注の取り込み直し。**共有が遅くても画面は返る**。"""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="catchup_"))
        self.conn = db.get_connection(tmp / "手元.db")
        self.addCleanup(self.conn.close)

    def test_共有が遅いと手元の値で返す(self) -> None:
        release = threading.Event()

        def slow(conn, **kwargs):
            release.wait(5)
            return data_sync.OrderRefresh(looked=True)

        with mock.patch.object(data_sync, "refresh_orders", side_effect=slow):
            began = time.monotonic()
            got = data_sync.catch_up(0.2, conn=self.conn)
            took = time.monotonic() - began
            release.set()
            data_sync._flight(False).wait_idle(5)
        self.assertIsNone(got)
        self.assertLess(took, 2.0)

    def test_間に合えば結果を返す(self) -> None:
        done = data_sync.OrderRefresh(looked=True, changed=True, tables=1)
        with mock.patch.object(data_sync, "refresh_orders", return_value=done) as fake:
            got = data_sync.catch_up(5.0, conn=self.conn)
        self.assertIs(got, done)
        self.assertTrue(fake.call_args.kwargs["only_if_changed"])

    def test_待てなかった取り込みが入れば次の見張りで描き直す(self) -> None:
        release = threading.Event()
        changed = data_sync.OrderRefresh(looked=True, changed=True, tables=1)

        def slow(conn, **kwargs):
            release.wait(5)
            return changed

        with mock.patch.object(data_sync, "refresh_orders", side_effect=slow):
            self.assertIsNone(data_sync.catch_up(0.1, conn=self.conn))
            release.set()
            data_sync._flight(False).wait_idle(5)
        quiet = data_sync.OrderRefresh(looked=True)
        with mock.patch.object(data_sync, "refresh_orders", return_value=quiet):
            got = data_sync.catch_up(5.0, conn=self.conn)
        self.assertTrue(got.updated, "背景で入った発注を描き直していません")

    def test_間に合わなかったと画面に言う(self) -> None:
        got = data_sync.pending_refresh()
        self.assertTrue(got.pending)
        self.assertTrue(got.looked, "見に行けていないと言うと、共有が見えない帯が出ます")
        self.assertIn("背景で", got.message())


# ==================================================================
# 2. 設定画面は待たずに開く
# ==================================================================
class SettingsOpensTests(unittest.TestCase):
    def setUp(self) -> None:
        presenter.forget_shared_facts()
        self.addCleanup(presenter.forget_shared_facts)
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)

    def test_共有が遅くても開く_終わったら出し直す(self) -> None:
        release = threading.Event()
        real = presenter._find_material

        def slow():
            release.wait(5)
            return real()

        with mock.patch.object(presenter, "_find_material", side_effect=slow), \
             mock.patch.object(presenter, "STATUS_WAIT_SEC", 0.2):
            began = time.monotonic()
            view = presenter.build(self.conn)
            self.assertLess(time.monotonic() - began, 2.0)
            self.assertTrue(view.status_pending)
            self.assertEqual(view.sections[0].title, "共有フォルダの確認")
            self.assertTrue(view.can_import, "確かめ終わる前に「できません」と言っています")
            self.assertTrue(presenter.to_dict(view)["status_pending"])
            release.set()
            presenter._status_flight.wait_idle(60)
        # 2回目は**待つ上限を長くして**確かめる。Windows の試験機は既定の置き場所(届かない
        # UNC)を見に行くだけで数秒かかるので、0.2秒のままだと「確かめています」が正しく出る
        with mock.patch.object(presenter, "STATUS_WAIT_SEC", 60.0):
            again = presenter.build(self.conn)
        self.assertFalse(again.status_pending)
        self.assertIn("梱包資材マスタ", [s.title for s in again.sections])

    def test_間に合えばいままでどおり(self) -> None:
        with mock.patch.object(presenter, "STATUS_WAIT_SEC", 60.0):   # 試験機の UNC 待ちを見込む
            view = presenter.build(self.conn)
        self.assertFalse(view.status_pending)
        titles = [s.title for s in view.sections]
        self.assertEqual(titles[:4], ["梱包資材マスタ", "看板マスタ", "パレット閾値マスタ", "仕掛台帳"])

    def test_設定の画面が共有の取り込み直しを待ちすぎない(self) -> None:
        src = (_ROOT / "app" / "routes" / "settings.py").read_text(encoding="utf-8")
        self.assertNotIn("refresh_orders(conn", src)
        self.assertIn("data_sync.catch_up(data_sync.SETTINGS_WAIT_SEC, conn=conn)", src)
        self.assertIn("settings_presenter.start_shared_check()", src)


class NoInlineRefreshTests(unittest.TestCase):
    def test_画面の要求の中で取り込み直さない(self) -> None:
        """要求の中で `refresh_orders` を直接呼ぶと、共有が遅いとそこで受付がふさがる。"""
        for name in ("settings", "inventory", "warehouse", "selection", "lot"):
            src = (_ROOT / "app" / "routes" / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("data_sync.refresh_orders(", src, name)
            self.assertNotIn("lot_plans.refresh_if_changed(", src, name)
            self.assertNotIn("access_control.resync(get_db", src, name)
            self.assertNotIn("sync_sources.find_material_db() is None", src, name)
        src = (_ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("access_control.resync(get_db", src)


class SingleGrantScreenTests(unittest.TestCase):
    """権限が1つの端末(ラインの端末の多く)は、要求のたびに取り込み元の姿を見に行っていた。"""

    def setUp(self) -> None:
        access_control.reset_screen_resync()
        self.addCleanup(access_control.reset_screen_resync)
        tmp = Path(tempfile.mkdtemp(prefix="screen_"))
        self.conn = db.get_connection(tmp / "手元.db")
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)

    def test_共有が応答しなくても画面の要求は止まらない(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def hang():
            release.wait(10)
            return None

        with mock.patch.object(access_control, "_source_stamp", side_effect=hang), \
             mock.patch.object(access_control, "RESYNC_WAIT_SEC", 0.2):
            began = time.monotonic()
            first = access_control.resolve_for_screen(self.conn)
            took_first = time.monotonic() - began
            began = time.monotonic()
            for _ in range(5):
                access_control.resolve_for_screen(self.conn)
            took_rest = time.monotonic() - began
        self.assertLess(took_first, 2.0)
        self.assertLess(took_rest, 0.5, "止まっている見張りを要求のたびに待っています")
        self.assertEqual(first.allowed_modes(), ("field",))


class BoardUsageTests(unittest.TestCase):
    def test_資材選択は使用実績の取り込みを待ちすぎない(self) -> None:
        from packaging_tool import board_usage
        tmp = Path(tempfile.mkdtemp(prefix="usage_"))
        conn = db.get_connection(tmp / "手元.db")
        self.addCleanup(conn.close)
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(board_usage, "refresh_if_changed",
                               side_effect=lambda c: release.wait(10)):
            began = time.monotonic()
            self.assertFalse(board_usage.refresh_bounded(conn, 0.2))
            self.assertFalse(board_usage.refresh_bounded(conn, 0.2))   # 止まっている間は待たない
            self.assertLess(time.monotonic() - began, 0.6)
        src = (_ROOT / "packaging_tool" / "presenters" / "selection.py").read_text(encoding="utf-8")
        self.assertIn("board_usage.refresh_bounded(session.presenter.conn)", src)


class ReachableTests(unittest.TestCase):
    def test_共有が応答しなければ届いていないと言う_待つのは上限まで(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(data_sync.sync_sources, "find_material_db",
                               side_effect=lambda: release.wait(10)):
            began = time.monotonic()
            self.assertFalse(data_sync.shared_reachable(0.2))
            self.assertLess(time.monotonic() - began, 2.0)
            self.assertEqual(data_sync.queued_note.__name__, "queued_note")

    def test_見えれば届いたと言う(self) -> None:
        with mock.patch.object(data_sync.sync_sources, "find_material_db",
                               return_value=Path("共有/梱包資材マスタ.sqlite3")):
            data_sync._reach_flight and data_sync._reach_flight.wait_idle(10)
            self.assertTrue(data_sync.shared_reachable(5.0))
            self.assertEqual(data_sync.queued_note(), "")


# ==================================================================
# 3. ログは別のスレッドが書く
# ==================================================================
class AsyncLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="asynclog_"))
        for target, attr, value in ((config, "LOG_DIR", self.dir / "既定"),
                                    (config, "_active_log_dir", None),
                                    (logging_utils, "fallback_why", ""),
                                    (log_names, "pc_name", lambda: "LINE-3")):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.dir / "既定").mkdir()
        self.addCleanup(self.close)
        self.log = logging_utils.get_logger("試験.非同期")

    def close(self) -> None:
        logging_utils.flush_logs(5.0)
        handler = logging_utils.file_handler()
        if handler is not None:
            handler.close()

    def test_書き込みが遅くてもログを書く側は待たない(self) -> None:
        """**これが本題。** 出力先が共有で遅いと、ログを1行書くスレッドがぜんぶ止まっていた。"""
        logging_utils.flush_logs(5.0)
        real = logging_utils.DailyFileHandler.emit

        def slow(self_, record):
            time.sleep(0.5)
            real(self_, record)

        with mock.patch.object(logging_utils.DailyFileHandler, "emit", slow):
            began = time.monotonic()
            for i in range(5):
                self.log.info("遅い共有へ %s", i)
            took = time.monotonic() - began
            self.assertTrue(logging_utils.flush_logs(10.0))
        self.assertLess(took, 0.5, "ログを書く側が書き込みを待っています")
        text = logging_utils.log_path_for(date.today()).read_text("utf-8")
        for i in range(5):
            self.assertIn(f"遅い共有へ {i}", text)

    def test_1行に時間がかかりすぎたらローカルへ切り替える(self) -> None:
        shared = self.dir / "共有"
        shared.mkdir()
        logging_utils.flush_logs(5.0)
        config._active_log_dir = shared
        real = logging.FileHandler.emit

        def slow(self_, record):
            if str(shared) in self_.baseFilename:          # 共有へ書くところだけ遅い
                time.sleep(0.3)
            real(self_, record)

        with mock.patch.object(logging.FileHandler, "emit", slow), \
             mock.patch.object(logging_utils, "SLOW_WRITE_SEC", 0.2):
            self.log.info("1行目(遅い)")
            logging_utils.flush_logs(5.0)
            self.log.info("2行目(ローカルへ)")
            logging_utils.flush_logs(5.0)
        self.assertIsNone(config._active_log_dir)
        self.assertIn("かかったので", logging_utils.fallback_why)
        local = (config.LOG_DIR / log_names.day_log(date.today())).read_text("utf-8")
        self.assertIn("2行目(ローカルへ)", local)

    def test_共有への書き込みが返らなければ新しい書き手でローカルへ書き続ける(self) -> None:
        """共有が応答しないと、1行の書き込みが何十秒も戻らない。そのあいだのログを失わない。"""
        shared = self.dir / "共有"
        shared.mkdir()
        logging_utils.flush_logs(5.0)
        config._active_log_dir = shared
        release = threading.Event()
        real = logging.FileHandler.emit

        def hang(self_, record):
            if str(shared) in self_.baseFilename:
                release.wait(10)                     # 共有が返らない
            real(self_, record)

        try:
            with mock.patch.object(logging.FileHandler, "emit", hang), \
                 mock.patch.object(logging_utils, "STUCK_SEC", 0.3):
                self.log.info("共有で止まる行")
                time.sleep(0.6)
                began = time.monotonic()
                self.log.info("替わったあとの行")
                self.assertLess(time.monotonic() - began, 0.3)
                self.assertTrue(logging_utils.flush_logs(5.0))
        finally:
            release.set()
        self.assertIsNone(config._active_log_dir)
        self.assertIn("返らないので", logging_utils.fallback_why)
        local = (config.LOG_DIR / log_names.day_log(date.today())).read_text("utf-8")
        self.assertIn("替わったあとの行", local)

    def test_エラー記録は起きたときの操作と直前の動きを持つ(self) -> None:
        token = logging_utils.begin_operation({"id": "op9999", "method": "POST", "path": "/api/x"})
        try:
            self.log.info("直前の動き")
            self.log.error("壊れました")
        finally:
            logging_utils.end_operation(token)
        logging_utils.flush_logs(5.0)
        import json
        path = config.LOG_DIR / log_names.error_log(__import__("datetime").datetime.now())
        rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        row = next(r for r in rows if r["message"] == "壊れました")
        self.assertEqual(row["operation"]["id"], "op9999", "書くスレッドの操作(空)を入れています")
        self.assertTrue(any("直前の動き" in line for line in row["recent"]))

    def test_コンソールも別のスレッド(self) -> None:
        """デスクトップ版では標準エラーが外枠への管。詰まっても書いたスレッドを止めない。"""
        src = (_ROOT / "packaging_tool" / "logging_utils.py").read_text(encoding="utf-8")
        self.assertIn('_AsyncWriter(console_targets, name="log-console", console=True, takeover=False)', src)
        rust = (_ROOT / "src-tauri" / "src" / "bridge.rs").read_text(encoding="utf-8")
        self.assertIn("read_until(b'\\n'", rust)
        self.assertIn("from_utf8_lossy", rust)
        self.assertNotIn("lines().map_while(Result::ok)", rust,
                         "UTF-8 でない行で読むのをやめると管が詰まります")


# ==================================================================
# 4. 書く要求は上限まで待つ
# ==================================================================
class WriteLockTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import settings as routes
        _web.bind_db(self, routes)
        self.client = _web.make_client()

    def test_前の書く要求が終わらなければ断る(self) -> None:
        import app as app_module
        held = threading.Event()
        release = threading.Event()

        def hold():
            with app_module._WRITE_LOCK:
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        held.wait(5)
        try:
            with mock.patch.object(app_module, "WRITE_WAIT_SEC", 0.2):
                res = self.client.post("/api/theme", json={"theme": "dark"},
                                       headers={"X-Tool-Token": _web.TOKEN})
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.get_json()["error"]["code"], "busy")

    def test_モードの切替は錠を待たない(self) -> None:
        import app as app_module
        self.assertTrue("/api/mode".startswith(app_module._NO_LOCK_PREFIXES))


# ==================================================================
# 5. 返らない要求はどこで止まっているかを残す
# ==================================================================
class StallWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        stall_watch.forget()
        self.addCleanup(stall_watch.forget)

    def test_返らない要求は呼び出しの並びごと1度だけ書く(self) -> None:
        stall_watch.begin("GET /settings")
        self.addCleanup(stall_watch.end)
        later = time.monotonic() + stall_watch.STALL_SEC + 1
        with self.assertLogs("packaging_tool.stall_watch", level="WARNING") as seen:
            self.assertEqual(stall_watch.check(later), 1)
        self.assertIn("GET /settings", seen.output[0])
        self.assertIn("いまここにいます", seen.output[0])
        self.assertEqual(stall_watch.check(later), 0, "同じ要求を何度も書いています")

    def test_すぐ返る要求は書かない(self) -> None:
        stall_watch.begin("GET /api/health")
        stall_watch.end()
        self.assertEqual(stall_watch.check(time.monotonic() + 999), 0)


# ==================================================================
# 6. 起動し直したとき
# ==================================================================
def _lock(mode: str, pid: int = 4242, port: int = 8715) -> launch_guard.LockInfo:
    return launch_guard.LockInfo(app_id="x", mode=mode, pid=pid, port=port,
                                 url=f"http://127.0.0.1:{port}/", started_at=1.0)


class RelaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app_id = mock.patch.object(launch_guard.app_config, "app_id", return_value="x")
        self.app_id.start()
        self.addCleanup(self.app_id.stop)
        self.version = mock.patch.object(launch_guard.app_config, "version", return_value="9.9.9")
        self.version.start()
        self.addCleanup(self.version.stop)

    def test_モードを切り替えたプロセスも自分と分かる(self) -> None:
        """資材で起動して現場へ切り替えたプロセスは、資材のロックのまま「現場」と答える。"""
        health = {"app_id": "x", "mode": "field", "pid": 4242}
        self.assertTrue(launch_guard.is_our_app(health, "material", pid=4242))
        self.assertFalse(launch_guard.is_our_app(health, "material", pid=1))
        self.assertFalse(launch_guard.is_our_app(health, "material"))
        self.assertFalse(launch_guard.is_our_app({**health, "app_id": "y"}, "field"))

    def test_切り替えたプロセスには2つ目を立てずに合流する(self) -> None:
        info = _lock("material")
        with mock.patch.object(launch_guard, "read_lock", return_value=info), \
             mock.patch.object(launch_guard, "is_process_alive", return_value=True), \
             mock.patch.object(launch_guard, "probe_health",
                               return_value={"app_id": "x", "mode": "field", "pid": 4242,
                                             "version": "9.9.9"}), \
             mock.patch.object(launch_guard, "remove_lock") as removed:
            guard = launch_guard.check_existing("material")
        self.assertFalse(guard.should_start)
        removed.assert_not_called()

    def test_固まった自分のプロセスは終わらせてから立て直す(self) -> None:
        """前は「応答しない = 別物」と見てロックだけ消し、固まったまま2つ目を立ち上げていた。"""
        info = _lock("field")
        alive = iter([True, True, False])
        with mock.patch.object(launch_guard, "read_lock", return_value=info), \
             mock.patch.object(launch_guard, "is_process_alive", side_effect=lambda pid: next(alive, False)), \
             mock.patch.object(launch_guard, "probe_health", return_value=None), \
             mock.patch.object(launch_guard, "is_port_accepting", return_value=True), \
             mock.patch.object(launch_guard, "looks_like_our_process", return_value=True), \
             mock.patch.object(launch_guard, "PATIENT_PROBES", (0.01,)), \
             mock.patch.object(launch_guard, "terminate_tree") as killed, \
             mock.patch.object(launch_guard, "remove_lock") as removed:
            guard = launch_guard.check_existing("field")
        self.assertTrue(guard.should_start)
        killed.assert_called_once_with(4242)
        removed.assert_called_once()
        self.assertIn("終わらせました", guard.reason)

    def test_自分でないプロセスは終わらせない(self) -> None:
        info = _lock("field")
        with mock.patch.object(launch_guard, "read_lock", return_value=info), \
             mock.patch.object(launch_guard, "is_process_alive", return_value=True), \
             mock.patch.object(launch_guard, "probe_health", return_value=None), \
             mock.patch.object(launch_guard, "is_port_accepting", return_value=True), \
             mock.patch.object(launch_guard, "looks_like_our_process", return_value=False), \
             mock.patch.object(launch_guard, "PATIENT_PROBES", (0.01,)), \
             mock.patch.object(launch_guard, "terminate_tree") as killed, \
             mock.patch.object(launch_guard, "remove_lock"):
            guard = launch_guard.check_existing("field")
        self.assertTrue(guard.should_start)
        killed.assert_not_called()

    def test_忙しいだけなら聞き直して合流する(self) -> None:
        info = _lock("field")
        answers = iter([None, {"app_id": "x", "mode": "field", "pid": 4242, "version": "9.9.9"}])
        with mock.patch.object(launch_guard, "read_lock", return_value=info), \
             mock.patch.object(launch_guard, "is_process_alive", return_value=True), \
             mock.patch.object(launch_guard, "probe_health", side_effect=lambda *a, **k: next(answers)), \
             mock.patch.object(launch_guard, "is_port_accepting", return_value=True), \
             mock.patch.object(launch_guard, "terminate_tree") as killed:
            guard = launch_guard.check_existing("field")
        self.assertFalse(guard.should_start)
        killed.assert_not_called()

    def test_権限で決める起動はほかのモードのロックにも合流する(self) -> None:
        info = _lock("material")
        locks = {"material": info, "field": None}
        with mock.patch.object(launch_guard, "read_lock", side_effect=lambda m: locks.get(m)), \
             mock.patch.object(launch_guard, "is_process_alive", return_value=True), \
             mock.patch.object(launch_guard, "probe_health",
                               return_value={"app_id": "x", "mode": "field", "pid": 4242,
                                             "version": "9.9.9"}):
            guard = launch_guard.running_elsewhere("field")
        self.assertIsNotNone(guard)
        self.assertEqual(guard.url, info.url)

    def test_起動の入口がほかのモードのロックを見る(self) -> None:
        src = (_ROOT / "start_app.py").read_text(encoding="utf-8")
        self.assertIn("launch_guard.running_elsewhere(mode)", src)


class DefaultModeTests(unittest.TestCase):
    def test_両方の権限があれば現場から開く(self) -> None:
        both = access_control.grant_of("mode:field", "mode:material")
        self.assertEqual(both.startup_mode(), "field")
        self.assertEqual(access_control.grant_of("mode:material").startup_mode(), "material")


if __name__ == "__main__":
    unittest.main()
