"""ユーザー向け選定ログ (VBA `UserLog` / `ClearUserLog` / `SetUserLogBox` の移植)

VBA版は `mpMain` の3ページ目「選定ログ」にある `txtUserLog` テキストボックスに
`UserLog("...")` で1行ずつ追記し、なぜその結果になったのかを現場が
自分で追えるようにしていた(`DebugLog` は開発者向けのファイル出力で別物)。

Python版も同じ役割を持たせるが、サービス層が画面に依存しないよう
「行を貯めるバッファ + 追記通知」だけを持つクラスにしてある。
画面(`/log`)は差分だけを取りに来る。

対応関係:
    UserLog(msg, emphasize)  -> UserLog.log(text, emphasis=...)
    ClearUserLog             -> UserLog.clear()
    SetUserLogBox(tb)        -> UserLog.subscribe(callback)
"""
from __future__ import annotations

import logging
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional


def _identity() -> tuple[str, str]:
    """この端末の身元。**1回引いて覚える。**

    `access_control` を遅延importするのは、こちらを先に読むモジュールとの
    循環参照を避けるため。値は Windows が持っている事実で、動いている
    あいだ変わらないので、毎行引き直す必要がない。
    """
    global _IDENTITY
    if _IDENTITY is None:
        try:
            from . import access_control
            found = access_control.current_identity()
            _IDENTITY = (found.login_id, found.pc_name)
        except Exception:                         # noqa: BLE001 - ログのために止めない
            _IDENTITY = ("", "")
    return _IDENTITY


_IDENTITY: Optional[tuple[str, str]] = None

# 保持する最大行数。VBAのテキストボックスは無制限だったが、
# 長時間起動しっぱなしでもメモリを食い潰さないよう上限を設ける。
DEFAULT_MAX_ENTRIES = 5000


@dataclass(frozen=True)
class LogEntry:
    """1行分のログ。

    `emphasis` は VBA `UserLog` の第2引数 `True`(節目の行)に相当する。
    UI側で太字などの強調表示に使う。

    `seq` は 1 から始まる連番。Web版は画面を持たないサーバから
    ログを取りに行くので、「前回どこまで受け取ったか」を伝える手段が要る
    (`GET /api/log?since=<seq>`)。tkinter版は購読で受け取るため使わないが、
    どちらの版でも同じ `UserLog` を使うのでここに持たせる。
    """

    text: str
    emphasis: bool = False
    seq: int = 0
    # いつ・どの端末の誰が。**画面には大きく出さない**が、後から追える
    # ようにデータとしては持つ(「先週のあれ、なぜこうなったのか」に
    # 答えるには、どの端末の話なのかが要る)
    at: str = ""
    login_id: str = ""
    pc_name: str = ""

    def who(self) -> str:
        if self.login_id and self.pc_name:
            return f"{self.login_id} @ {self.pc_name}"
        return self.login_id or self.pc_name


Listener = Callable[[Optional[LogEntry]], None]


class UserLog:
    """選定ログのバッファ。

    `log()` で1行追記、`clear()` で全消去。いずれも購読者に通知する
    (追記なら追記された `LogEntry`、消去なら `None` を渡す)。
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._entries: list[LogEntry] = []
        self._listeners: list[Listener] = []
        self._last_seq = 0

    # -- 書き込み ---------------------------------------------------
    def log(self, text: str, *, emphasis: bool = False) -> None:
        self._last_seq += 1
        # 時刻と身元はここで1回だけ付ける。呼ぶ側(業務のコード)に
        # 「ログのために身元を持ち回る」をさせない
        login, pc = _identity()
        entry = LogEntry(text=text, emphasis=emphasis, seq=self._last_seq,
                         at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         login_id=login, pc_name=pc)
        self._entries.append(entry)
        if len(self._entries) > self.max_entries:
            # 古い行から捨てる。捨てた分は購読者側の表示とずれるため全再描画を促す
            del self._entries[: len(self._entries) - self.max_entries]
            self._notify(None)
            return
        self._notify(entry)

    def clear(self) -> None:
        """VBA `ClearUserLog` の移植。

        連番は戻さない。戻すと、消去前の番号を持っている取得側が
        「まだ新しい行が来ていない」と誤解して取りこぼす。
        """
        self._entries.clear()
        self._notify(None)

    # -- 読み出し ---------------------------------------------------
    @property
    def entries(self) -> list[LogEntry]:
        return list(self._entries)

    @property
    def text(self) -> str:
        return "\n".join(e.text for e in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def last_seq(self) -> int:
        """最後に振った連番。1行も出していなければ 0。"""
        return self._last_seq

    def entries_since(self, seq: int) -> list[LogEntry]:
        """`seq` より後の行だけを返す(Web版の差分取得用)。

        古い行が上限で捨てられていると、返る先頭の `seq` が
        `seq + 1` より大きくなる。取得側はその跳びを見て
        「取りこぼしたので全部描き直す」と判断できる。
        """
        return [e for e in self._entries if e.seq > seq]

    # -- 購読 -------------------------------------------------------
    def subscribe(self, listener: Listener) -> None:
        """追記/消去の通知を受け取る(VBA `SetUserLogBox` 相当)。"""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, entry: Optional[LogEntry]) -> None:
        for listener in list(self._listeners):
            # 購読者(UI)の例外でサービス層の処理を巻き添えにしない
            try:
                listener(entry)
            except Exception:  # noqa: BLE001
                pass


# 既定の上限件数(除外ログの上位何件を出すか)。
DEFAULT_REJECT_LIMIT = 10


class RejectLog:
    """「候補を1件ずつ却下していく」ログの上位N件だけを出し、残りは

    件数にまとめる(VBAには無い、Python版だけの追加)。

    現場の声:「パレット/ボードの候補を絞り込むたびに、除外の行が
    際限なく出て、本当に効いた判断(採用・カット等)が埋もれてしまう」。
    在庫やマスタの件数が多いほど、除外だけの行が数十〜数百行に伸びる。
    上位 `limit` 件だけそのまま出し、それ以降は件数だけ最後にまとめる。
    """

    def __init__(self, target: "UserLog", limit: int = DEFAULT_REJECT_LIMIT) -> None:
        self._target = target
        self.limit = limit
        self._shown = 0
        self._suppressed = 0

    def log(self, text: str) -> None:
        if self._shown < self.limit:
            self._target.log(text)
            self._shown += 1
        else:
            self._suppressed += 1

    def flush(self, label: str = "除外") -> None:
        """溜まった省略件数を1行にまとめて出す。呼んだあとは0に戻る。"""
        if self._suppressed:
            self._target.log(
                f"  …ほか{self._suppressed}件を{label}"
                f"(表示は先頭{self.limit}件まで)")
            self._suppressed = 0


# アプリ全体で共有するインスタンス(VBAのフォーム単位の txtUserLog に相当)。
# サービス層の関数は原則として引数で `UserLog` を受け取るが、
# 省略時はこれが使われる。
_shared = UserLog()


def get_user_log() -> UserLog:
    return _shared


# 日ごとのファイルに繋いだログ。`keep_on_disk` を2度呼んでも1度しか繋がない
_on_disk: "weakref.WeakSet[UserLog]" = weakref.WeakSet()


def keep_on_disk(target: Optional[UserLog] = None) -> None:
    """出した行を、日ごとのファイルにも残すようにする。

    **プロセスの中だけでは後から追えません。** 現場からの問い合わせは
    たいてい後日なので、終了で消えると確かめる手立てが無くなります。

    起動時に1回だけ呼びます。購読なので、業務のコードは何も変わりません
    (`UserLog.log()` を呼ぶだけで、残るところまで面倒を見る)。
    """
    from . import selection_log_store

    # **`target or _shared` と書いてはいけない。** `UserLog` は `__len__` を
    # 持つので、行が0本のログは偽になる ── 渡したはずのログではなく
    # 共有のログに繋がり、渡した側からは黙って効かないように見える
    target = _shared if target is None else target
    # 2度繋ぐと1行が2回書かれる。繋いだ相手を覚えておく
    # (弱参照なので、使い終わったログを掴んだままにはしない)
    if target in _on_disk:
        return

    def sink(entry: Optional[LogEntry]) -> None:
        # `None` は消去の合図。**ファイルは消さない** ── 画面を片付けたら
        # 過去も消える、では後から追えるようにした意味が無い
        if entry is not None:
            selection_log_store.append(entry)

    target.subscribe(sink)
    _on_disk.add(target)


# ==================================================================
# 業務ログの橋渡し
# ==================================================================
# 選定アルゴリズムへ渡す `UserLog` を引数で持ち回らない理由:
#
# `board_selection_algorithm` は40を超える純粋関数に分かれていて、
# 判断はその奥のほうで起きる(向き補正・却下・補填の打ち切り)。
# ログのためだけに全部の関数へ引数を1本足すと、業務の読み筋に
# ログの都合が混ざる ── しかもその行の文言は**すでに `log.debug()` に
# 現場の言葉で書いてある**(「却下(幅超過)」「[幅補填] ...」)。
#
# 同じ文言を2か所に持つと、片方だけ直された日にずれる(設計.md §1)。
# そこで文言はいまの場所に置いたまま、**選定が走っているあいだだけ**
# その出力をユーザーログへ流す。
_BRIDGED = "packaging_tool.board_selection_algorithm"

# 「却下」系の行だと判断する目印。この文言を含む行だけ上位N件に絞る。
# 「採用」「[丈カット]」等の**決まったことを伝える行**はここに当たらず、
# 何件出ても素通しする(埋もれさせたくないのはこちらの方だから)
_REJECT_MARKER = "却下"


class _Bridge(logging.Handler):
    """`logging` の1行を `UserLog` の1行にする。

    **却下系の行は上位N件だけ**にする(現場の声:「候補が多いと除外の
    行が際限なく出て、採用やカットの行が埋もれる」)。却下以外の行が
    来た時点(=次の段階に進んだ時点)で、それまでの省略件数を1行に
    まとめて出す。`RejectLog` と同じ考え方をロガー橋渡し側にも適用した形。
    """

    def __init__(self, target: "UserLog", limit: int = DEFAULT_REJECT_LIMIT) -> None:
        super().__init__(level=logging.DEBUG)
        self._target = target
        self._rejects = RejectLog(target, limit=limit)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if _REJECT_MARKER in message:
                self._rejects.log("  " + message)
                return
            self._rejects.flush("却下")
            self._target.log("  " + message)
        except Exception:                         # noqa: BLE001 - ログで止めない
            pass

    def flush_rejects(self) -> None:
        self._rejects.flush("却下")


@contextmanager
def bridge_from(target: "UserLog", *names: str):
    """`names` のロガーが出す行を、そのあいだ `target` にも流す。

    **付けたら必ず外す。** 外し忘れると、次の選定でも前のログへ
    流れ続ける(`UserLog` は要求をまたいで生きている)。
    """
    handler = _Bridge(target)
    loggers = [logging.getLogger(n) for n in (names or (_BRIDGED,))]
    for one in loggers:
        one.addHandler(handler)
    try:
        yield
    finally:
        # 最後の行が却下系だった場合、まとめずに終わらせない
        handler.flush_rejects()
        for one in loggers:
            one.removeHandler(handler)
