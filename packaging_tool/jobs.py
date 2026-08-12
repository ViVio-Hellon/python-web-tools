"""長時間処理の受け付けと見張り (基盤仕様書 監視レベル2)

取り込みと書き戻しは数十秒かかる。tkinter版はボタンを押した画面が
そのまま進捗を持っていたが、Web版では**ブラウザを閉じても処理は続く**
(基盤仕様書 2.9: 「ブラウザを閉じてもバックエンドは終了しない」)。

そのため進捗はプロセス側が持ち、画面はそれを見に行くだけにする。
開き直しても、別のタブから見ても、同じものが見える。

【この層が引き受けること】
- いま走っているものと、最近終わったものを覚えておく
- 同時に2つ走らせない(どちらもDBを書くので、混ぜると壊れる)
- 状態をファイルに残す。**プロセスが落ちても最後の結果が読める**
- 落ちる前に「実行中」だった記録は、読み直したときに **「中断」** に倒す。
  新しいプロセスの中でそれが走っていることはあり得ないので、
  実行中のまま見せると永久に終わらない処理があるように見える
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import app_config
from .logging_utils import get_logger

log = get_logger("jobs")

# 状態。画面の文言はこの値から引く(文言をここに書かない)
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"   # プロセスが落ちて結果が分からない

# 覚えておく「最近終わったもの」の件数。
# 画面に出すのは直近数件だが、続けて取り込み直したときの経緯が
# 追えるくらいには残す
MAX_RECENT = 20


@dataclass
class Step:
    """処理の中の1段。

    **段は処理が自分で名乗ったもの**です。作業スレッドが
    `progress(pct, "仕掛ロットを読み込んでいます")` と伝えてくるたび、
    文言が変わった時点を段の切れ目とみなします。こちらで
    「取り込みは6段のはず」と決め打ちしません ── 決め打ちすると、
    処理を1つ足したときに画面だけが古いままになります。

    横棒1本では「どの段まで終わったか」が読めないので、これを出します。
    """

    label: str
    started_at: float = 0.0
    finished_at: float = 0.0
    ok: bool = True

    @property
    def running(self) -> bool:
        return self.finished_at == 0.0

    @property
    def elapsed_sec(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)


@dataclass
class Job:
    """1回の長時間処理。"""

    id: str
    kind: str                    # "import" / "write_back" / "recompute"
    label: str                   # 画面に出す名前(「まとめて取り込み」など)
    state: str = STATE_RUNNING
    pct: int = 0
    message: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    ok: bool = False
    summary: str = ""
    error: str = ""
    # 通ってきた段。**新しい文言が来るたびに1つ増える**
    steps: list["Step"] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return self.state == STATE_RUNNING

    @property
    def elapsed_sec(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)


class JobBusy(RuntimeError):
    """すでに何か走っているのに、もう1つ始めようとした。"""

    def __init__(self, running: Job) -> None:
        super().__init__(f"{running.label}を実行中です")
        self.running = running


class JobRegistry:
    """プロセスに1つ。走っているものと終わったものを持つ。"""

    def __init__(self, store_path: Optional[Path] = None,
                 max_recent: int = MAX_RECENT) -> None:
        self._lock = threading.RLock()
        self._running: Optional[Job] = None
        self._recent: list[Job] = []
        self._max_recent = max_recent
        self._store = store_path
        self._restore()

    # --------------------------------------------------------------
    # 参照
    # --------------------------------------------------------------
    def running(self) -> Optional[Job]:
        with self._lock:
            return self._running

    def recent(self) -> list[Job]:
        """新しい順。"""
        with self._lock:
            return list(self._recent)

    def find(self, job_id: str) -> Optional[Job]:
        with self._lock:
            if self._running is not None and self._running.id == job_id:
                return self._running
            for job in self._recent:
                if job.id == job_id:
                    return job
        return None

    def is_busy(self) -> bool:
        return self.running() is not None

    def busy_labels(self) -> list[str]:
        """停止してよいかの判断に使う(`POST /api/shutdown`)。"""
        job = self.running()
        return [job.label] if job is not None else []

    # --------------------------------------------------------------
    # 実行
    # --------------------------------------------------------------
    def start(self, kind: str, label: str,
              work: Callable[[Callable[[int, str], None]], Any]) -> Job:
        """別スレッドで走らせて、`Job` をすぐ返す。

        `work` には進捗を伝える関数 `progress(pct, message)` が渡る。
        これは `data_sync` の `Progress` と同じ形なので、既存の処理を
        そのまま渡せる。

        すでに何か走っていれば `JobBusy`。取り込みも書き戻しも同じDBを
        書くので、**種類が違っても同時には走らせない**。
        """
        with self._lock:
            if self._running is not None:
                raise JobBusy(self._running)
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label,
                      started_at=time.time(), message=f"{label}を準備しています")
            self._running = job
            self._persist()

        log.info("開始: %s (%s) id=%s", label, kind, job.id)
        threading.Thread(target=self._run, args=(job, work),
                         name=f"job-{kind}", daemon=True).start()
        return job

    def _run(self, job: Job, work) -> None:
        try:
            result = work(lambda pct, message: self._progress(job, pct, message))
        except Exception as exc:                  # noqa: BLE001 - 画面に出して続ける
            log.exception("失敗: %s id=%s", job.label, job.id)
            self._finish(job, STATE_FAILED, ok=False, summary="", error=str(exc))
        else:
            summary = (result.summary() if hasattr(result, "summary")
                       else ("" if result is None else str(result)))
            ok = bool(getattr(result, "ok", True))
            self._finish(job, STATE_DONE if ok else STATE_FAILED,
                         ok=ok, summary=summary, error="")

    def _progress(self, job: Job, pct: int, message: str) -> None:
        """作業スレッドから呼ばれる。

        `message` が空のときは**バーだけ**進める。仕掛台帳は3ファイルを
        続けて読むので、ファイルの切れ目で文言が消えないようにする
        (tkinter版 `DataView.set_progress` と同じ約束)。
        """
        with self._lock:
            job.pct = max(0, min(100, int(pct)))
            if message and message != job.message:
                # 文言が変わった = 段が変わった。前の段を閉じて次を開く
                now = time.time()
                if job.steps and job.steps[-1].running:
                    job.steps[-1].finished_at = now
                job.steps.append(Step(label=message, started_at=now))
            if message:
                job.message = message
            # 毎回ファイルに書くと、1件ごとに進捗が来る処理で書き込みが
            # 増えすぎる。区切りのよいところだけ残す(落ちたときに
            # 「どのあたりだったか」が分かれば足りる)
            if job.pct % 10 == 0:
                self._persist()

    def _finish(self, job: Job, state: str, *, ok: bool,
                summary: str, error: str) -> None:
        with self._lock:
            job.state = state
            job.ok = ok
            job.summary = summary
            job.error = error
            job.finished_at = time.time()
            job.pct = 100 if ok else job.pct
            job.message = ""
            # 最後の段を閉じる。**失敗したのはその段**なので、そう記す
            if job.steps and job.steps[-1].running:
                job.steps[-1].finished_at = job.finished_at
                job.steps[-1].ok = ok
            self._recent.insert(0, job)
            del self._recent[self._max_recent:]
            if self._running is job:
                self._running = None
            self._persist()
        log.info("終了: %s id=%s state=%s %.1f秒",
                 job.label, job.id, state, job.elapsed_sec)

    # --------------------------------------------------------------
    # 保存と復元
    # --------------------------------------------------------------
    def _store_path(self) -> Path:
        if self._store is not None:
            return self._store
        return app_config.local_dir("cache") / "jobs.json"

    def _persist(self) -> None:
        """呼び出し側で `_lock` を取っていること。

        書きかけを読ませないよう、別名で書いてから置き換える。
        """
        path = self._store_path()
        payload = {
            "running": asdict(self._running) if self._running else None,
            "recent": [asdict(job) for job in self._recent],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            # 保存できなくても処理そのものは続ける。ここで落とすと
            # 「記録が取れないので取り込みができない」ことになる
            log.warning("進捗の保存に失敗: %s", exc)

    def _restore(self) -> None:
        path = self._store_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("進捗の読み直しに失敗: %s", exc)
            return

        recent = [self._to_job(row) for row in payload.get("recent") or []]
        self._recent = [job for job in recent if job is not None][:self._max_recent]

        # 落ちる前に走っていたものは、**この**プロセスでは走っていない。
        # 実行中のまま見せると、終わらない処理があるように見える
        stale = self._to_job(payload.get("running"))
        if stale is not None:
            stale.state = STATE_INTERRUPTED
            stale.message = ""
            stale.error = "アプリが終了したため、途中で止まっています"
            self._recent.insert(0, stale)
            del self._recent[self._max_recent:]
            log.warning("前回の %s は中断扱いにしました id=%s", stale.label, stale.id)
        self._running = None

    @staticmethod
    def _to_job(row: Optional[dict]) -> Optional[Job]:
        if not isinstance(row, dict):
            return None
        try:
            allowed = {f for f in Job.__dataclass_fields__}   # noqa: SLF001
            job = Job(**{k: v for k, v in row.items() if k in allowed})
        except TypeError:
            return None
        # 段は辞書のまま戻ってくる。**組み直しておく** ── そのままだと
        # `step.running` を呼んだところで初めて落ちる
        steps = []
        for raw in job.steps:
            if isinstance(raw, Step):
                steps.append(raw)
            elif isinstance(raw, dict):
                keys = {f for f in Step.__dataclass_fields__}   # noqa: SLF001
                try:
                    steps.append(Step(**{k: v for k, v in raw.items() if k in keys}))
                except TypeError:
                    continue
        job.steps = steps
        return job


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_registry: Optional[JobRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> JobRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = JobRegistry()
        return _registry


def reset_registry(registry: Optional[JobRegistry] = None) -> JobRegistry:
    """テスト用に差し替える。"""
    global _registry
    with _registry_lock:
        _registry = registry if registry is not None else JobRegistry()
        return _registry
