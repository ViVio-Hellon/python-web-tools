"""画面が居なくなったら終わる (基盤仕様書 2.8「自動終了」)

【なぜ要るのか】
このアプリに窓はありません。見えているのはブラウザのタブだけなので、
**タブを閉じたら終わったつもりになります。** ところが Python は動いた
ままで、次に起動すると多重起動の判定が「すでに起動しています」と答え、
古いプロセスのブラウザが開きます ── 入れ替えたはずの新しい版が、
いつまでも動きません。

【どう決めるか】
画面が一定の間隔で心拍(`POST /api/alive`)を送ります。**途切れたら
誰も見ていない**と判断して終わります。タブを閉じたことは
`sendBeacon` で即座に伝わるので、たいていは待たずに終わります。

【間違って落とさないための3つ】
1. **猶予を置く。** 画面の作り直し(再読込・画面遷移)でも心拍は
   一瞬途切れます。閉じた合図が来ても `GRACE_SEC` は待ち、そのあいだに
   心拍が戻れば取り消します
2. **処理中は落とさない。** 取り込みの途中で終わると、DBが中途半端な
   状態で残ります(`/api/shutdown` と同じ判断を使う)
3. **1度も繋がっていなければ落とさない。** `--no-browser` で立てて
   おく使い方(検証・並行運用)を巻き添えにしません
4. **画面が「閉じます」を言わずに消えたら、保存していない図があるうちは
   落とさない。** タブを閉じるときは「保存していない変更があります」を
   通るので(`unsaved.js`)、閉じた合図が来たのは本人が捨てると決めたとき。
   合図なしに心拍だけ途切れたのは、ブラウザが落ちた・外から閉じられた
   (業務ツール統合ランチャーは止める前に自分の画面を閉じ、10 秒で強制する)
   とき ── 誰も決めていないので、開き直して保存できるよう残しておく
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .logging_utils import get_logger

log = get_logger("idle_exit")

# 心拍が途切れてから終わるまで(秒)。画面は `HEARTBEAT_MS` ごとに送るので、
# 数回落としても持ちこたえる長さにする
IDLE_SEC = 90.0

# 「閉じました」を受けてから終わるまで(秒)。
# 再読込でも閉じた合図は飛ぶので、戻ってくるぶんを待つ
GRACE_SEC = 8.0

# 画面が心拍を送る間隔(ミリ秒)。画面へ渡す値の出どころはここ1つ
HEARTBEAT_MS = 20_000

# 見張る間隔(秒)
TICK_SEC = 2.0

# 見張りの間隔が壁時計でこれ以上空いたら、PCがスリープしていたとみなす(秒)
SLEEP_GAP_SEC = 30.0

# 本人が閉じた(閉じた合図が来た)ときの理由。これだけは保存していない図があっても止める
CLOSED = "画面が閉じられました"


@dataclass
class _Screen:
    """開いている画面(タブ)1つ。"""

    seen: float                              # 最後に何か届いた時刻(monotonic)
    hidden: bool = False                     # 裏に回っている(別のタブ・最小化)
    leaving_at: Optional[float] = None       # 閉じた合図を受けた時刻


class IdleWatch:
    """画面の生き死にを見て、居なくなったら止める。

    【裏に回った画面は、心拍が止まっても落とさない】
    ブラウザは裏に回ったタブのタイマーを間引きます(Chrome は5分を過ぎると
    1分に1回以下、Edge の「スリープ中のタブ」やメモリ節約では止めてしまう)。
    別のツールで、これで心拍が途切れて**使っている最中に終了**したことが
    ありました。画面は裏に回るときに「隠れます」を送る(`hidden`)ので、
    そう言った画面は**心拍が途切れても生きているものとして数えます。**
    閉じたときは別に「閉じました」が届くので、それでは落とせます。

    【画面ごとに見る】
    以前は画面を区別していなかったので、2つ開いているうちの1つを閉じると、
    もう1つが8秒以内に心拍を送らない限り落ちていました。

    【スリープから戻ったら、待ち直す】
    PCがスリープしているあいだは、画面もこのプロセスも止まります。
    Windows では `time.monotonic()` がスリープ中も進むので、戻った瞬間に
    「90秒 心拍が無い」が成り立ち、画面の最初の心拍より先に落ちていました。
    見張りの間隔が壁時計で大きく空いたら**スリープから戻った**とみなし、
    そこから `idle_sec` は待ちます。
    """

    def __init__(self, stop: Callable[[], None],
                 busy: Callable[[], bool],
                 *, unsaved: Optional[Callable[[], bool]] = None,
                 idle_sec: float = IDLE_SEC,
                 grace_sec: float = GRACE_SEC,
                 tick_sec: float = TICK_SEC,
                 sleep_gap_sec: float = SLEEP_GAP_SEC) -> None:
        self._stop = stop
        self._busy = busy
        self._unsaved = unsaved or (lambda: False)
        self._kept = False                      # 保存していない図のために残している
        self.idle_sec = idle_sec
        self.grace_sec = grace_sec
        self.tick_sec = tick_sec
        self.sleep_gap_sec = sleep_gap_sec

        self._lock = threading.Lock()
        self._screens: dict[str, _Screen] = {}
        self._ever = False                      # 1度でも繋がったか
        self._resumed_at: Optional[float] = None
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 画面から ------------------------------------------------------
    def beat(self, screen: str = "", *, visible: Optional[bool] = None,
             keep_leaving: bool = False) -> None:
        """画面が生きている。**閉じた合図も取り消す**(再読込のとき)。

        `visible` は画面が言ってきた表/裏。言っていなければ変えない
        (業務の要求は表か裏かを言わないので、前の状態のまま)。
        `keep_leaving` は「裏に回った」のとき(下の `hidden`)。
        """
        now = time.monotonic()
        with self._lock:
            self._ever = True
            s = self._screens.get(screen)
            if s is None:
                s = self._screens[screen] = _Screen(seen=now)
            s.seen = now
            if not keep_leaving:
                s.leaving_at = None
            if visible is not None and s.hidden == visible:
                s.hidden = not visible
                log.info("画面が%sになりました(%s)",
                         "表" if visible else "裏", screen or "-")

    def hidden(self, screen: str = "") -> None:
        """画面が裏に回った(別のタブ・最小化・固まった)。心拍が止まっても落とさない。

        **閉じた合図は取り消さない。** タブを閉じるとブラウザは「裏に回った」と
        「閉じた」を続けて送るが、`sendBeacon` は届く順番を約束しない。「裏に
        回った」が後から届いて閉じたことを打ち消すと、閉じたタブを裏にいる
        ものとして数え続け、いつまでも終わらなくなる。
        """
        self.beat(screen, visible=False, keep_leaving=True)

    def resumed(self, screen: str = "", *, gap_sec: float = 0.0,
                visible: bool = True) -> None:
        """画面が戻ってきた(表に出た・スリープから戻った)。

        スリープ明けに気づいたのが裏のタブなら `visible=False` のまま ──
        表に戻ったことにすると、また心拍の途切れで落とされる。
        """
        if gap_sec:
            log.info("画面がスリープなどから戻りました(%.0f秒 止まっていた / %s)",
                     gap_sec, screen or "-")
        self.beat(screen, visible=visible)

    def leaving(self, screen: str = "") -> None:
        """画面が閉じた(`sendBeacon`)。猶予のあとで、その画面を数えなくなる。"""
        with self._lock:
            if not self._ever:
                return                          # 1度も繋がっていない
            s = self._screens.get(screen)
            if s is None:
                s = self._screens[screen] = _Screen(seen=time.monotonic())
            s.leaving_at = time.monotonic()
            others = sum(1 for k, v in self._screens.items()
                         if k and k != screen and v.leaving_at is None)
        if others:
            log.info("画面が1つ閉じました(ほかに %d つ開いています)", others)
        else:
            log.info("画面が閉じました。%.0f秒 待って終了します", self.grace_sec)

    # -- 見張り --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="idle-watch",
                                        daemon=True)
        self._thread.start()
        log.info("自動終了の見張りを始めました(無通信 %.0f秒 / 閉じたら %.0f秒 / "
                 "裏に回った画面は落とさない)", self.idle_sec, self.grace_sec)

    def cancel(self) -> None:
        self._done.set()

    def _loop(self) -> None:
        last_wall = time.time()
        while not self._done.wait(self.tick_sec):
            now_wall = time.time()
            self.check_sleep(now_wall - last_wall)
            last_wall = now_wall
            why = self.overdue()
            if why is None:
                continue
            if self._busy():
                # **処理中は落とさない。** 取り込みの途中で終わると、
                # DBが中途半端な状態で残る。終わればまた見に来る
                log.info("誰も見ていませんが、処理中なので待ちます")
                continue
            if why != CLOSED and self._unsaved():
                # **画面が合図なしに消えた。** 保存していない図を黙って捨てない
                if not self._kept:
                    log.warning("画面が見えなくなりました(%s)が、保存していない図があるので"
                                "終了しません。開き直して保存するか、画面の「終了」で"
                                "閉じてください", why)
                    self._kept = True
                continue
            self._kept = False
            log.info("誰も見ていないので終了します(%s)", why)
            self._done.set()
            self._stop()
            return

    def check_sleep(self, elapsed_wall: float) -> bool:
        """見張りの間隔が壁時計で大きく空いた = スリープから戻った。待ち直す。"""
        if elapsed_wall < self.tick_sec + self.sleep_gap_sec:
            return False
        with self._lock:
            self._resumed_at = time.monotonic()
        log.info("スリープから戻ったようです(%.0f秒 止まっていた)。画面が戻るのを"
                 " %.0f秒 待ちます", elapsed_wall, self.idle_sec)
        return True

    def overdue(self) -> Optional[str]:
        """終わってよいか。よければ理由、まだなら `None`。"""
        now = time.monotonic()
        with self._lock:
            if not self._ever:
                # 1度も繋がっていない。`--no-browser` で立てておく使い方を
                # 巻き添えにしない
                return None
            if self._resumed_at is not None and now - self._resumed_at < self.idle_sec:
                return None                     # スリープから戻ったばかり
            closed = False
            # 番号の付いた画面が居るなら、番号の無い要求はその読み込みの橋渡し
            keyed = any(self._screens)
            for key, s in list(self._screens.items()):
                if s.leaving_at is not None:
                    if now - s.leaving_at < self.grace_sec:
                        return None             # 閉じた直後。再読込なら戻ってくる
                    closed = True
                    del self._screens[key]      # 閉じた画面はもう数えない
                    continue
                if s.hidden:
                    return None                 # 裏に回っている。心拍が止まっても待つ
                # 番号の無い要求(画面そのもの・静的ファイル)は、番号の付いた画面が
                # 居るなら、ページを読み込むあいだの橋渡しだけ。閉じたあとの猶予と
                # 同じ長さで切れる(でないと、全部閉じても 90秒 残る)
                limit = (self.idle_sec if key or not keyed
                         else min(self.idle_sec, self.grace_sec))
                if now - s.seen < limit:
                    return None                 # 表で心拍がある
        if closed:
            return CLOSED
        return f"{self.idle_sec:.0f}秒 心拍がありません"


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_watch: Optional[IdleWatch] = None
_lock = threading.Lock()


def install(stop: Callable[[], None], busy: Callable[[], bool],
            **kwargs) -> IdleWatch:
    """見張りを1つ立てる。2度呼んでも1つ。"""
    global _watch
    with _lock:
        if _watch is None:
            _watch = IdleWatch(stop, busy, **kwargs)
            _watch.start()
        return _watch


def get() -> Optional[IdleWatch]:
    return _watch


def reset() -> None:
    """テスト用。"""
    global _watch
    with _lock:
        if _watch is not None:
            _watch.cancel()
        _watch = None
