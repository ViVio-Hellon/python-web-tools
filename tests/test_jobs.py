"""長時間処理の見張り (基盤仕様書 監視レベル2)

取り込みは数十秒かかる。tkinter版はボタンを押した画面が進捗を持っていたが、
Web版は**ブラウザを閉じても処理が続く**ので、進捗はプロセス側が持つ。
ここで確かめるのは「開き直しても状態が分かるか」と
「途中で落ちたことを正直に出せるか」。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import jobs  # noqa: E402


class Result:
    """`data_sync.ImportResult` の代わり(`summary()` と `ok` を持つ)。"""

    def __init__(self, ok: bool = True, summary: str = "3件") -> None:
        self.ok = ok
        self._summary = summary

    def summary(self) -> str:
        return self._summary


class JobTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="jobs_test_")
        self.store = Path(self.dir) / "jobs.json"
        self.registry = jobs.JobRegistry(store_path=self.store)

    def wait_idle(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.registry.is_busy():
                return
            time.sleep(0.01)
        self.fail("処理が終わりませんでした")


class RunTests(JobTestCase):
    def test_走らせて結果が残る(self) -> None:
        self.registry.start("import", "まとめて取り込み", lambda _p: Result())
        self.wait_idle()
        done = self.registry.recent()[0]
        self.assertEqual(done.state, jobs.STATE_DONE)
        self.assertTrue(done.ok)
        self.assertEqual(done.summary, "3件")

    def test_進捗が伝わる(self) -> None:
        seen = []

        def work(progress):
            for pct in (10, 50, 100):
                progress(pct, f"{pct}まで")
                seen.append(pct)
            return Result()

        self.registry.start("import", "取り込み", work)
        self.wait_idle()
        self.assertEqual(seen, [10, 50, 100])
        self.assertEqual(self.registry.recent()[0].pct, 100)

    def test_文言が空ならバーだけ進める(self) -> None:
        """仕掛台帳は3ファイルを続けて読む。切れ目で文言が消えると、
        止まったように見える(tkinter版と同じ約束)。"""
        started = threading.Event()
        release = threading.Event()

        def work(progress):
            progress(20, "SIKALOTを読んでいます")
            progress(40, "")
            started.set()
            release.wait(3)
            return Result()

        self.registry.start("import", "取り込み", work)
        self.assertTrue(started.wait(3))
        running = self.registry.running()
        self.assertEqual(running.pct, 40)
        self.assertEqual(running.message, "SIKALOTを読んでいます")
        release.set()
        self.wait_idle()

    def test_失敗しても止まらない(self) -> None:
        """例外で落ちると、何が起きたか誰にも分からなくなる。"""
        def work(_p):
            raise RuntimeError("共有フォルダに届きません")

        self.registry.start("import", "取り込み", work)
        self.wait_idle()
        failed = self.registry.recent()[0]
        self.assertEqual(failed.state, jobs.STATE_FAILED)
        self.assertIn("共有フォルダ", failed.error)

    def test_途中の段だけ失敗しても他の段は汚染されない(self) -> None:
        """レーンの各段の成否が、全体の結果と食い違わないこと(バグ修正)。

        1テーブルだけ読み込みに失敗しても、そのテーブルの段が最後で
        ない限り、次の段が始まった瞬間に(成否を記さないまま)閉じられ、
        "完了" のまま残っていた。全体は「失敗」なのにレーンのカードは
        1枚も失敗を指さない、という食い違いが起きていた。
        """
        def work(progress):
            progress(30, "甲を読み込み中...")
            progress(30, "甲を読み込み中...", ok=False)   # 甲だけ失敗
            progress(70, "乙を読み込み中...")
            progress(100, "")
            return Result(ok=False, summary="1/2テーブル")

        self.registry.start("import", "取り込み", work)
        self.wait_idle()
        job = self.registry.recent()[0]
        self.assertEqual(job.state, jobs.STATE_FAILED)
        self.assertEqual(len(job.steps), 2)
        self.assertFalse(job.steps[0].ok)   # 甲は失敗のまま
        self.assertTrue(job.steps[1].ok)    # 乙(最後の段)は汚染されない

    def test_例外で落ちると開いていた段が失敗になる(self) -> None:
        """段が自分の成否を報告する前に丸ごと落ちたときは、開いていた
        段を失敗と記す(段が何も言わないまま消えるほうが分かりにくい)。
        """
        def work(progress):
            progress(50, "甲を読み込み中...")
            raise RuntimeError("接続が切れました")

        self.registry.start("import", "取り込み", work)
        self.wait_idle()
        job = self.registry.recent()[0]
        self.assertEqual(job.state, jobs.STATE_FAILED)
        self.assertEqual(len(job.steps), 1)
        self.assertFalse(job.steps[0].ok)

    def test_okがFalseなら失敗扱い(self) -> None:
        """例外にならなくても、取り込めていなければ失敗。"""
        self.registry.start("import", "取り込み",
                            lambda _p: Result(ok=False, summary="0件"))
        self.wait_idle()
        self.assertEqual(self.registry.recent()[0].state, jobs.STATE_FAILED)

    def test_戻り値が無くても通る(self) -> None:
        """`recompute_fit_ranges` のように `summary()` を持たない処理もある。"""
        self.registry.start("recompute", "適合範囲の再計算", lambda _p: None)
        self.wait_idle()
        self.assertEqual(self.registry.recent()[0].state, jobs.STATE_DONE)

    def test_新しい順に並ぶ(self) -> None:
        for i in range(3):
            self.registry.start("import", f"取り込み{i}", lambda _p: Result())
            self.wait_idle()
        labels = [job.label for job in self.registry.recent()]
        self.assertEqual(labels, ["取り込み2", "取り込み1", "取り込み0"])

    def test_覚えておく件数には上限がある(self) -> None:
        registry = jobs.JobRegistry(store_path=self.store, max_recent=2)
        for i in range(4):
            registry.start("import", f"取り込み{i}", lambda _p: Result())
            deadline = time.monotonic() + 5
            while registry.is_busy() and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(len(registry.recent()), 2)


class BusyTests(JobTestCase):
    def test_同時に2つ走らせない(self) -> None:
        """取り込みも書き戻しも同じDBを書く。混ぜると壊れる。"""
        release = threading.Event()
        self.registry.start("import", "取り込み", lambda _p: release.wait(3))
        try:
            with self.assertRaises(jobs.JobBusy) as caught:
                self.registry.start("write_back", "Accessへ反映", lambda _p: None)
            self.assertEqual(caught.exception.running.label, "取り込み")
        finally:
            release.set()
        self.wait_idle()

    def test_終われば次を走らせられる(self) -> None:
        self.registry.start("import", "取り込み", lambda _p: Result())
        self.wait_idle()
        self.registry.start("write_back", "Accessへ反映", lambda _p: Result())
        self.wait_idle()
        self.assertEqual(len(self.registry.recent()), 2)

    def test_止めてよいかの判断に使える(self) -> None:
        """実行中に落とすと、DBが中途半端なまま残る(基盤仕様書 2.8)。"""
        release = threading.Event()
        self.registry.start("import", "まとめて取り込み", lambda _p: release.wait(3))
        try:
            self.assertEqual(self.registry.busy_labels(), ["まとめて取り込み"])
        finally:
            release.set()
        self.wait_idle()
        self.assertEqual(self.registry.busy_labels(), [])


class PersistTests(JobTestCase):
    def test_開き直しても結果が読める(self) -> None:
        """ブラウザを閉じても処理は続く。開き直したときに結果が要る。"""
        self.registry.start("import", "取り込み", lambda _p: Result())
        self.wait_idle()

        reopened = jobs.JobRegistry(store_path=self.store)
        self.assertEqual(reopened.recent()[0].label, "取り込み")
        self.assertEqual(reopened.recent()[0].summary, "3件")

    def test_落ちた分は中断として出す(self) -> None:
        """新しいプロセスの中でそれが走っていることはあり得ない。

        実行中のまま見せると、終わらない処理があるように見える。
        """
        self.store.write_text(json.dumps({
            "running": {"id": "abc", "kind": "import", "label": "取り込み",
                        "state": jobs.STATE_RUNNING, "pct": 40,
                        "started_at": time.time() - 30},
            "recent": [],
        }, ensure_ascii=False), encoding="utf-8")

        reopened = jobs.JobRegistry(store_path=self.store)
        self.assertIsNone(reopened.running())
        stale = reopened.recent()[0]
        self.assertEqual(stale.state, jobs.STATE_INTERRUPTED)
        self.assertIn("途中で止まって", stale.error)

    def test_保存が壊れていても起動する(self) -> None:
        """記録が読めないことを理由にアプリが開かないのは本末転倒。"""
        self.store.write_text("これはJSONではない", encoding="utf-8")
        reopened = jobs.JobRegistry(store_path=self.store)
        self.assertEqual(reopened.recent(), [])
        self.assertIsNone(reopened.running())

    def test_知らない項目があっても読める(self) -> None:
        """あとから項目を増やしたとき、古い記録で落ちないこと。"""
        self.store.write_text(json.dumps({
            "running": None,
            "recent": [{"id": "x", "kind": "import", "label": "取り込み",
                        "state": jobs.STATE_DONE, "未知の項目": 1}],
        }, ensure_ascii=False), encoding="utf-8")
        reopened = jobs.JobRegistry(store_path=self.store)
        self.assertEqual(reopened.recent()[0].label, "取り込み")

    def test_書きかけを読ませない(self) -> None:
        """置き換えで書くので、読み側は常に完全なJSONを見る。"""
        self.registry.start("import", "取り込み", lambda _p: Result())
        self.wait_idle()
        json.loads(self.store.read_text(encoding="utf-8"))
        self.assertFalse(self.store.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
