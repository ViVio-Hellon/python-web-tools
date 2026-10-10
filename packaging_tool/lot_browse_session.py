"""ロット一覧の絞り込み状態

いま何で絞っているか・どう並べているか・何件出しているかを持ちます。
**画面は持ちません** ── 持たせると、サーバが条件を外したことが画面に
伝わらず、「条件が出ているのに効いていない」状態を作れてしまいます
(設計書 §1 の5番)。

【よく使う条件は保存する】
現場は同じ絞り込みを毎日します。毎回組み直させるのは覚えさせるのと
同じことなので、名前を付けて `user_settings` に残します(拠点設定と
同じ置き場)。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from . import lot_query as q
from . import user_settings
from .logging_utils import get_logger

log = get_logger("lot_browse_session")

# 保存フィルタの置き場(`user_settings` のキー)
KEY_SAVED = "lot_filters"

# 保存できる数。増やしすぎると探すほうが手間になる
SAVED_MAX = 20
NAME_MAX = 24


# 全角の英数字・記号(！〜～)と全角の空白 → 半角
_HALF = {**{c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}, 0x3000: 0x20}


@dataclass
class OpResult:
    ok: bool = True
    message: str = ""
    reason: str = ""


@dataclass
class LotBrowseSession:
    """一覧の絞り込み。プロセスに1つ。"""

    conditions: list[q.Condition] = field(default_factory=list)
    text: str = ""
    sort: str = q.DEFAULT_SORT
    descending: bool = False
    page_size: int = q.DEFAULT_PAGE_SIZE

    # ------------------------------------------------------------------
    # 条件
    # ------------------------------------------------------------------
    def add(self, column: str, op: str, value: Any) -> OpResult:
        cond, refusal = q.make_condition(column, op, value)
        if refusal is not None:
            return OpResult(False, refusal.message, refusal.reason)
        if cond in self.conditions:
            # 同じ条件を2つ並べても結果は変わらない。増えるのは
            # 「なぜこの件数か」を読む手間だけ
            return OpResult(True, f"{cond.label} は既に入っています")
        self.conditions.append(cond)
        log.info("条件を追加: %s", cond.label)
        return OpResult(True, f"{cond.label} で絞りました")

    def remove(self, index: int) -> OpResult:
        if not 0 <= index < len(self.conditions):
            # 別のタブで先に外されている。黙って別の条件を外すと、
            # 外したつもりのないものが外れる
            return OpResult(False, "その条件はもうありません。表示を取り直してください。",
                            q.REFUSE_BAD_INPUT)
        cond = self.conditions.pop(index)
        return OpResult(True, f"{cond.label} を外しました")

    def clear(self) -> OpResult:
        self.conditions = []
        self.text = ""
        return OpResult(True, "条件をすべて外しました")

    def set_text(self, text: str) -> OpResult:
        # 全角の英数字・記号は半角に(画面の `halfwidth.js` と同じ。貼り付けた文字にも効かせる)。
        # 照合は大文字・小文字を区別しないので、ここでは大文字にしない
        self.text = (text or "").translate(_HALF).strip()
        return OpResult(True, "")

    # ------------------------------------------------------------------
    # 並べ替えと件数
    # ------------------------------------------------------------------
    def set_sort(self, column: str) -> OpResult:
        """同じ列をもう一度指すと昇順と降順が入れ替わる(表の作法)。"""
        if column not in q.BY_KEY:
            return OpResult(False, f"「{column}」は一覧にない列です。",
                            q.REFUSE_NOT_LISTED)
        if column == self.sort:
            self.descending = not self.descending
        else:
            self.sort, self.descending = column, False
        return OpResult(True, "")

    def set_page_size(self, size: Any) -> OpResult:
        try:
            value = int(size)
        except (TypeError, ValueError):
            return OpResult(False, "表示件数には数値を指定してください。",
                            q.REFUSE_BAD_INPUT)
        if value not in q.PAGE_SIZES:
            return OpResult(
                False,
                f"表示件数は {' / '.join(str(n) for n in q.PAGE_SIZES)} から選んでください。",
                q.REFUSE_NOT_LISTED)
        self.page_size = value
        return OpResult(True, f"表示件数: {value}")

    # ------------------------------------------------------------------
    # よく使う条件
    # ------------------------------------------------------------------
    @staticmethod
    def saved() -> dict[str, list[dict[str, str]]]:
        """保存済みの絞り込み。壊れていても落とさず、読めた分だけ返す。"""
        raw = user_settings.get(KEY_SAVED)
        if not isinstance(raw, dict):
            return {}
        out: dict[str, list[dict[str, str]]] = {}
        for name, items in raw.items():
            if isinstance(name, str) and isinstance(items, list):
                out[name] = [i for i in items if isinstance(i, dict)]
        return out

    def save_current(self, name: str) -> OpResult:
        name = (name or "").strip()
        if not name:
            return OpResult(False, "名前を入れてください。", q.REFUSE_BAD_INPUT)
        if len(name) > NAME_MAX:
            return OpResult(False, f"名前は{NAME_MAX}文字までにしてください。",
                            q.REFUSE_BAD_INPUT)
        if not self.conditions:
            # 空を保存できると、押したのに何も起きない条件ができる
            return OpResult(False, "先に条件を1つ以上足してください。",
                            q.REFUSE_BAD_INPUT)

        store = self.saved()
        if name not in store and len(store) >= SAVED_MAX:
            return OpResult(
                False, f"よく使う条件は{SAVED_MAX}件までです。要らないものを消してください。",
                q.REFUSE_BAD_INPUT)
        store[name] = [{"column": c.column, "op": c.op, "value": c.value}
                       for c in self.conditions]
        user_settings.save(KEY_SAVED, store)
        log.info("よく使う条件を保存: %s (%d件)", name, len(self.conditions))
        return OpResult(True, f"「{name}」を保存しました")

    def load_saved(self, name: str) -> OpResult:
        """保存した条件を**いまの条件に足す**(置き換えない)。

        置き換えにすると、いま絞ってある内容が黙って消えます。
        まっさらから始めたいときは「全解除」を押せば済みます。
        """
        items = self.saved().get(name)
        if items is None:
            return OpResult(False, f"「{name}」は登録されていません。",
                            q.REFUSE_NOT_LISTED)
        added = 0
        for item in items:
            result = self.add(item.get("column", ""), item.get("op", ""),
                              item.get("value", ""))
            if not result.ok:
                # 保存したあとに列の構成が変わった等。読めた分だけ足す
                log.warning("保存条件を復元できません: %s (%s)", item, result.message)
                continue
            added += 1
        if not added:
            return OpResult(False, f"「{name}」の条件を復元できませんでした。",
                            q.REFUSE_NOT_LISTED)
        return OpResult(True, f"「{name}」を足しました")

    def delete_saved(self, name: str) -> OpResult:
        store = self.saved()
        if name not in store:
            return OpResult(False, f"「{name}」は登録されていません。",
                            q.REFUSE_NOT_LISTED)
        del store[name]
        user_settings.save(KEY_SAVED, store)
        return OpResult(True, f"「{name}」を消しました")

    # ------------------------------------------------------------------
    def rows(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return q.fetch(conn, self.conditions, text=self.text, sort=self.sort,
                       descending=self.descending, limit=self.page_size)

    def total(self, conn: sqlite3.Connection) -> int:
        return q.count(conn, self.conditions, self.text)


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_session: Optional[LotBrowseSession] = None


def get_session() -> LotBrowseSession:
    global _session
    if _session is None:
        _session = LotBrowseSession()
    return _session


def reset_session() -> LotBrowseSession:
    """テスト用。プロセス共通の状態を試験の間で漏らさない。"""
    global _session
    _session = LotBrowseSession()
    return _session
