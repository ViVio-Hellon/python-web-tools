"""管理者以外停止モード(VER4.8.129。`packaging_tool/remote_stop.py`・`stop_keep.py`)

現場の決め:
    お願いを出せる      管理者パスワード かつ この端末の権限に Administrator
    止める範囲          お願いを出した PC 以外の、起動中の PC
    保存していない図・   帯で知らせて利用者に任せる。何も残っていなければ自動で止まる。5分で強制
    処理中               (保存していない図は控えに残し、次の起動で戻す)
    更新中               お願いを出した PC 以外は起動させない
    ランチャー           同時には使わない(お願いが出ているあいだ stop.bat は断る)
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import config, remote_stop, stop_keep  # noqa: E402


class _Base(unittest.TestCase):
    PC = "PC-ADMIN"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.share = Path(tmp.name) / "共有"
        self.share.mkdir()
        self.local = Path(tmp.name) / "ローカル"
        for patcher in (
                mock.patch.object(config, "master_db_dir", lambda: self.share),
                mock.patch.object(config, "DB_PATH", self.local / "packaging_tool.db"),
                mock.patch.object(remote_stop, "READ_RETRY_SEC", ()),
                mock.patch.dict(os.environ, {"PACKAGING_TOOL_PC_NAME": self.PC,
                                             "PACKAGING_TOOL_LOGIN_ID": "kanri"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        remote_stop.reset()
        self.addCleanup(remote_stop.reset)

    def allow(self, password_ok: bool = True, admin: bool = True):
        return (mock.patch("packaging_tool.admin_password.verify", return_value=password_ok),
                mock.patch("packaging_tool.access_control.is_administrator", return_value=admin))

    def issue(self, **kw):
        a, b = self.allow(**kw)
        with a, b:
            return remote_stop.issue("pw", None, "更新のため")

    def as_pc(self, name: str):
        return mock.patch.dict(os.environ, {"PACKAGING_TOOL_PC_NAME": name})


class IssueTests(_Base):
    def test_パスワードとAdministratorの両方が要る(self) -> None:
        self.assertEqual(self.issue(password_ok=False).reason, remote_stop.REFUSE_PASSWORD)
        self.assertEqual(self.issue(admin=False).reason, remote_stop.REFUSE_NOT_ADMIN)
        self.assertFalse(remote_stop.request_path().exists())

    def test_お願いを出す_出ているあいだは重ねて出さない_解除する(self) -> None:
        self.assertTrue(self.issue().ok)
        req = remote_stop.read_request()
        self.assertEqual((req["by_pc"], req["reason"], req["grace_sec"]), (self.PC, "更新のため", 300))
        self.assertEqual(self.issue().reason, remote_stop.REFUSE_ALREADY)
        a, b = self.allow()
        with a, b:
            self.assertTrue(remote_stop.clear("pw", None).ok)
            self.assertEqual(remote_stop.clear("pw", None).reason, remote_stop.REFUSE_NOT_ACTIVE)
        self.assertIsNone(remote_stop.read_request())

    def test_読めないお願いは上書きしない(self) -> None:
        remote_stop.folder().mkdir(parents=True)
        remote_stop.request_path().write_text("{壊れている", encoding="utf-8")
        self.assertEqual(self.issue().reason, remote_stop.REFUSE_UNREADABLE)
        self.assertEqual(remote_stop.request_path().read_text(encoding="utf-8"), "{壊れている")

    def test_共有のフォルダに届かなければ出せない(self) -> None:
        with mock.patch.object(config, "master_db_dir", lambda: self.share / "無い"):
            self.assertEqual(self.issue().reason, remote_stop.REFUSE_NO_FOLDER)


class StartupTests(_Base):
    def test_更新中はお願いを出したPC以外で起動しない(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            why = remote_stop.startup_block()
        self.assertIn("更新が終わるまで起動できません", why)
        self.assertIsNone(remote_stop.startup_block())          # お願いを出した PC は起動できる

    def test_読めなければ起動は止めない(self) -> None:
        remote_stop.folder().mkdir(parents=True)
        remote_stop.request_path().write_text("{壊れている", encoding="utf-8")
        with self.as_pc("PC-LINE1"):
            self.assertIsNone(remote_stop.startup_block())

    def test_起動の入口で_理由を出してしばらくして終わる(self) -> None:
        import start_app
        self.issue()
        srv = mock.Mock()
        with self.as_pc("PC-LINE1"), \
                mock.patch.object(remote_stop, "BLOCK_SHOW_SEC", 0.01), \
                mock.patch.object(start_app, "_hold_until_stopped") as hold:
            self.assertTrue(start_app._blocked_by_remote_stop(srv, mock.Mock()))
        srv.boot.mark_error.assert_called_once()
        self.assertIn("管理者が更新のため", srv.boot.mark_error.call_args.args[0])
        hold.assert_called_once()

    def test_お願いが無ければ起動の入口は素通り(self) -> None:
        import start_app
        srv = mock.Mock()
        self.assertFalse(start_app._blocked_by_remote_stop(srv, mock.Mock()))
        srv.boot.mark_error.assert_not_called()


class StopBatTests(_Base):
    def test_お願いが出ているあいだstop_batは断る(self) -> None:
        import process_manager
        self.issue()
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(process_manager, "stop") as stop:
            code = process_manager.main(["--all"])
        self.assertEqual(code, process_manager.EXIT_FAILED)
        self.assertTrue(out.getvalue().startswith("止められませんでした: 管理者以外停止モード中"))
        stop.assert_not_called()

    def test_お願いが無ければいつもどおり(self) -> None:
        self.assertIsNone(remote_stop.stop_bat_refusal())


class WatcherTests(_Base):
    def watcher(self, *, unsaved=None, busy=None, saved=None):
        self.now = 1000.0
        self.stopped = []
        self.unsaved = dict(unsaved or {})
        self.busy = list(busy or [])
        w = remote_stop.Watcher(lambda: self.stopped.append(True), lambda: self.unsaved, lambda: self.busy,
                                before_force=(lambda: saved or []), clock=lambda: self.now)
        return w

    def answer(self, w) -> dict:
        return json.loads(w.presence_path().read_text(encoding="utf-8"))

    def test_何も残っていなければ帯を見せてから止まる(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher()
            w.tick()
            self.assertEqual(w.state, remote_stop.STATE_NOTICE)
            self.assertIsNotNone(w.screen_state())
            self.assertEqual(self.stopped, [])
            self.now += remote_stop.NOTICE_SEC
            w.tick()
        self.assertEqual(self.stopped, [True])
        self.assertEqual(self.answer(w)["state"], remote_stop.STATE_STOPPED)
        w.leave()
        self.assertTrue(w.presence_path().exists(), "止まった答えは管理者が見るので残す")

    def test_保存していない図があれば待ち_保存したら止まる(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher(unsaved={"layout": "棚検索の配置図"})
            self.now += remote_stop.NOTICE_SEC + 10
            w.tick()
            self.assertEqual((w.state, w.detail), (remote_stop.STATE_WAITING, "棚検索の配置図"))
            self.assertEqual(self.stopped, [])
            self.unsaved.clear()
            w.tick()
        self.assertEqual(self.stopped, [True])

    def test_5分たっても止まらなければ図を控えてから強制で止める(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher(unsaved={"layout": "棚検索の配置図"}, busy=["取り込み"], saved=["棚検索の配置図"])
            w.tick()
            self.now += remote_stop.GRACE_SEC
            w.tick()
        self.assertEqual(self.stopped, [True])
        answer = self.answer(w)
        self.assertEqual(answer["state"], remote_stop.STATE_FORCED)
        self.assertIn("控えに残しました", answer["detail"])

    def test_お願いを出したPCは止めない(self) -> None:
        self.issue()
        w = self.watcher()
        w.tick()
        self.now += remote_stop.GRACE_SEC + 1
        w.tick()
        self.assertEqual((self.stopped, w.state), ([], remote_stop.STATE_RUNNING))
        self.assertIsNone(w.screen_state())

    def test_取り下げられたら止めずに続ける(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher(unsaved={"layout": "棚検索の配置図"})
            w.tick()
            a, b = self.allow()
            with a, b:
                remote_stop.clear("pw", None)
            w.tick()
        self.assertEqual((self.stopped, w.state), ([], remote_stop.STATE_RUNNING))
        self.assertTrue(any("取り下げ" in n for n in remote_stop.take_notes()))

    def test_お願いを読めなくなっても取り下げとは見ない(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher(unsaved={"layout": "棚検索の配置図"})
            w.tick()
            remote_stop.request_path().write_text("{書いている途中", encoding="utf-8")
            w.tick()
            self.assertEqual(w.state, remote_stop.STATE_WAITING)
            self.now += remote_stop.GRACE_SEC
            w.tick()
        self.assertEqual(self.stopped, [True])

    def test_ふだんの終わりなら知らせを消す(self) -> None:
        w = self.watcher()
        w.tick()
        self.assertTrue(w.presence_path().exists())
        w.leave()
        self.assertFalse(w.presence_path().exists())


class OverviewTests(_Base):
    def test_各PCの様子を並べる(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w1 = remote_stop.Watcher(lambda: None, lambda: {"x": "棚検索の配置図"}, lambda: [])
            w1.tick()
        view = remote_stop.overview()
        self.assertTrue(view.available)
        self.assertIn("PC-ADMIN(kanri)", view.request_text)
        rows = {r["pc"]: r for r in view.pcs}
        self.assertEqual(rows["PC-LINE1"]["label"], "利用者を待っています")
        self.assertEqual(rows["PC-LINE1"]["detail"], "棚検索の配置図")

    def test_知らせが途絶えたPCは応答なし(self) -> None:
        self.issue()
        remote_stop.presence_dir().mkdir(parents=True, exist_ok=True)
        (remote_stop.presence_dir() / "PC-OLD__a__1.json").write_text(json.dumps(
            {"pc": "PC-OLD", "login": "a", "beat_at": 1.0, "state": "running"}), encoding="utf-8")
        rows = {r["pc"]: r for r in remote_stop.overview().pcs}
        self.assertTrue(rows["PC-OLD"]["stale"])
        self.assertIn("応答なし", rows["PC-OLD"]["label"])


class KeepTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        from packaging_tool import layout_session, pallet_map_session
        for module in (layout_session, pallet_map_session):
            module.reset_session()
            self.addCleanup(module.reset_session)

    def test_保存していない図を控えに残し_次の起動で編集中として戻す(self) -> None:
        from packaging_tool import layout_session
        session = layout_session.get_session()
        session.plan.width = 1234.0
        session.dirty = True
        self.assertEqual(stop_keep.save_unsaved(), [layout_session.UNSAVED_LABEL])
        self.assertFalse(Path(config.DB_PATH.parent / "floor_plan.json").exists(), "図のファイルには書かない")

        layout_session.reset_session()                         # 止まって、起動し直した
        notes = stop_keep.restore()
        self.assertEqual(len(notes), 1)
        restored = layout_session.get_session()
        self.assertEqual((restored.plan.width, restored.dirty, restored.editing), (1234.0, True, True))
        self.assertEqual(stop_keep.restore(), [], "一度戻した控えはもう戻さない")
        self.assertTrue(any((stop_keep.keep_dir() / stop_keep.DONE_NAME).glob("floor_plan-*.json")))

    def test_保存していなければ何も書かない(self) -> None:
        self.assertEqual(stop_keep.save_unsaved(), [])
        self.assertFalse(stop_keep.keep_dir().exists())

    def test_壊れた控えは戻さず残す(self) -> None:
        stop_keep.keep_dir().mkdir(parents=True)
        bad = stop_keep.keep_dir() / "pallet_map-20260101-000000.json"
        bad.write_text("{壊れ", encoding="utf-8")
        self.assertEqual(stop_keep.restore(), [])
        self.assertTrue(bad.exists())


if __name__ == "__main__":
    unittest.main()
