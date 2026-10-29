"""共有フォルダを見に行く仕事を、**画面の要求の中で待ちすぎない**(VER4.8.115)

【何が起きていたか】
現場の声:「資材と現場を切り替えると時々フリーズする。一度でもフリーズすると起動しなおしても
設定タブが開けなくなる。現場資材を切り替えれなくなる。設定も開けなくなるのでログすらみれない」

画面を出す要求(設定・簡易在庫・倉庫連携の見張り・送る前の確かめ)が、その場で共有フォルダの
取り込み直しをしていた。共有が遅い・一時的に応答しないと、

    その要求が返らない → 同じ画面の次の要求も同じ取り込みを始める → 受付のスレッド(8本)が
    全部ふさがる → モード切替・心拍・設定画面まで返らない(フリーズ)

起動し直しても、最初の1回は「まだ一度も取り込んでいない = 変わった」と数えるので、設定を
開いた最初の1回で必ず重い取り込みが走る。共有が遅いあいだは、何度開き直しても同じところで止まる。

【どう直すか】
仕事は**背景の1本だけ**で走らせ、画面の要求は**決めた秒数だけ**待つ。

    間に合った     … いままでどおり、取り込んだ結果で画面を出す
    間に合わない   … 手元の値で画面を出す。仕事は背景で続け、終わったら画面へ知らせる
                     (`master_state` の世代が進み、開いている画面が読み直す)
    走っている最中 … 2本目は始めない。走っている1本の終わりを同じく待つだけ

共有が戻らないまま止まっていても、ふさがるのは背景の1本だけ。画面は必ず返る。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .logging_utils import get_logger

log = get_logger("bounded")


@dataclass
class Outcome:
    """待った結果。"""

    done: bool                     # 待っているあいだに終わったか
    value: Any = None              # 仕事の返した値(終わっていなければ None)
    error: Optional[BaseException] = None
    # 前の回で、待つのをやめたあとに終わっていた結果(誰にもまだ渡していない)
    late: Any = None
    waited: float = 0.0            # 待った秒数


class Flight:
    """同じ仕事を**同時に2本走らせない**。待つのは上限まで。

    `work` は引数なしで呼ぶ。**自分の接続を開いて閉じる**こと(要求の接続はスレッドをまたげない)。
    """

    def __init__(self, name: str, work: Callable[[], Any]) -> None:
        self.name = name
        self.work = work
        self._lock = threading.Lock()
        self._done: Optional[threading.Event] = None
        self._value: Any = None
        self._error: Optional[BaseException] = None
        self._started = 0.0
        # 待つのをやめた人がいる回か(終わったら late として次の人へ渡す)
        self._abandoned = False
        self._late: Any = None
        self._has_late = False

    # ------------------------------------------------------------------
    def running(self) -> bool:
        with self._lock:
            return self._done is not None and not self._done.is_set()

    def running_for(self) -> float:
        """走っている1本が始まってからの秒数(走っていなければ 0)。"""
        with self._lock:
            if self._done is None or self._done.is_set():
                return 0.0
            return time.monotonic() - self._started

    def start(self) -> threading.Event:
        """走っていなければ始める。走っている(始めた)1本の「終わった」を返す。"""
        with self._lock:
            if self._done is not None and not self._done.is_set():
                return self._done
            done = threading.Event()
            self._done = done
            self._value, self._error = None, None
            self._abandoned = False
            self._started = time.monotonic()
        thread = threading.Thread(target=self._run, args=(done,), daemon=True,
                                  name=f"bg-{self.name}")
        thread.start()
        return done

    def run(self, wait: float, *, wait_if_running: Optional[float] = None) -> Outcome:
        """始めて(走っていればそれに乗って)、`wait` 秒まで待つ。

        `wait_if_running` … もう走っていた(前の人が待ちきれなかった = 止まっているかもしれない)
        ときに待つ秒数。省略すれば `wait` と同じ。
        """
        begin = time.monotonic()
        late = self.take_late()
        already = self.running()
        done = self.start()
        if already and wait_if_running is not None:
            wait = wait_if_running
        finished = done.wait(max(0.0, wait))
        waited = time.monotonic() - begin
        with self._lock:
            if not finished:
                self._abandoned = True
                return Outcome(False, late=late, waited=waited)
            if self._done is done:
                value, error = self._value, self._error
            else:                                  # pragma: no cover - 次の回が始まっていた
                value, error = None, None
        return Outcome(True, value, error, late=late, waited=waited)

    def take_late(self) -> Any:
        """待つのをやめたあとに終わった結果を受け取る(1度だけ)。無ければ None。"""
        with self._lock:
            if not self._has_late:
                return None
            self._has_late = False
            late, self._late = self._late, None
            return late

    def wait_idle(self, timeout: float) -> bool:
        """試験用。走っている1本が終わるまで待つ。"""
        with self._lock:
            done = self._done
        return done is None or done.wait(timeout)

    # ------------------------------------------------------------------
    def _run(self, done: threading.Event) -> None:
        value: Any = None
        error: Optional[BaseException] = None
        try:
            value = self.work()
        except BaseException as exc:              # noqa: BLE001 - 背景の仕事は握って残す
            error = exc
            log.warning("%s: 背景の仕事が失敗しました: %s", self.name, exc, exc_info=True)
        took = time.monotonic() - self._started
        with self._lock:
            self._value, self._error = value, error
            if self._abandoned and error is None:
                self._late, self._has_late = value, True
            done.set()
        if self._abandoned:
            log.info("%s: 背景で %.1f秒 かかって終わりました(画面は待たずに出しています)",
                     self.name, took)


# ------------------------------------------------------------------
# 手元のDBを使う仕事
# ------------------------------------------------------------------
def db_file(conn: Any) -> Optional[str]:
    """その接続のDBファイル。メモリ上のDB(試験)なら None ── ほかのスレッドからは開けない。"""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except Exception:                             # noqa: BLE001 - 分からなければ、その場で走らせる
        return None
    path = row[2] if row is not None else ""
    return str(path) if path else None


class FileFlights:
    """**手元のDBファイルごとに** `Flight` を持つ。背景の1本はそのファイルを自分で開き直す。

    画面の要求の接続(`conn`)はスレッドをまたげないので、同じファイルを別の接続で開く。
    メモリ上のDB(試験)はほかのスレッドから開けないので、その場で走らせる(待つ上限は効かない)。
    """

    def __init__(self, name: str, work: Callable[[Any], Any]) -> None:
        self.name = name
        self.work = work
        self._lock = threading.Lock()
        self._flights: dict[str, Flight] = {}

    def _flight(self, conn: Any) -> Optional[Flight]:
        from . import config
        path = db_file(conn) if conn is not None else str(config.DB_PATH)
        if path is None:
            return None
        with self._lock:
            flight = self._flights.get(path)
            if flight is None:
                flight = Flight(self.name, lambda: self._open_and_work(path))
                self._flights[path] = flight
            return flight

    def _open_and_work(self, path: str) -> Any:
        from pathlib import Path

        from . import db
        conn = db.get_connection(Path(path))
        try:
            return self.work(conn)
        finally:
            conn.close()

    def run(self, conn: Any, wait: float, *, wait_if_running: Optional[float] = None) -> Outcome:
        flight = self._flight(conn)
        if flight is None:
            try:
                return Outcome(True, self.work(conn))
            except Exception as exc:              # noqa: BLE001 - 呼ぶ側が決める
                return Outcome(True, error=exc)
        return flight.run(wait, wait_if_running=wait_if_running)

    def take_late(self, conn: Any) -> Any:
        flight = self._flight(conn)
        return flight.take_late() if flight is not None else None

    def wait_idle(self, timeout: float) -> bool:
        """試験用。走っている分が終わるまで待つ。"""
        with self._lock:
            flights = list(self._flights.values())
        return all(f.wait_idle(timeout) for f in flights)
