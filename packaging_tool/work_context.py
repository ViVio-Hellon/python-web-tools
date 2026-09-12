"""いま何の作業をしているか (画面をまたいで共有する状態)

tkinter版は1つのプロセスに全画面が居たので、ロット検索で選んだロットは
そのまま資材選択タブの `SelectionPresenter` に入っていた。
Web版は要求ごとに独立しているので、**同じものをプロセス側に置く**。

【なぜ必要か】
1. **ステータスリボンが用をなさない**。リボンは「いま何が決まっているか」を
   常時見せて、覚えさせないための帯(設計指針 §1.4)。画面を移るたびに
   消えるなら、そこに置く意味がない
2. **資材展開が渡せない**。ロット検索の「資材展開」は製造板幅・板丈を
   資材選択の製品サイズ欄へ渡す操作(VBA `Page1_OnBtnHBClick`)で、
   渡す先が別の要求になる以上、間に置き場が要る

【なぜプロセスに1つでよいか】
このアプリは**1台のPCを1人が使う**(複数ラインは切り替えて使う)。
現場モードと倉庫モードは別プロセスなので、混ざることもない。
ブラウザを開き直しても続きから作業できる ── これは長時間処理の記録
(`jobs.py`)と同じ考え方。

【保存しない】
落ちたら消える。tkinter版もアプリを閉じれば消えていたし、
作業の途中経過であって記録ではない。
"""
from __future__ import annotations

import threading
from datetime import date
from dataclasses import dataclass, field
from typing import Any, Optional

from .logging_utils import get_logger

log = get_logger("work_context")

# リボンに何も入っていないときの表示。空白にすると、決まっていないのか
# 表示が壊れているのか分からない
UNSET = "未設定"
NO_LOT = "—"


@dataclass
class WorkContext:
    """いま画面に載っている作業。"""

    # --- ロット検索から ---
    lot_no: str = ""
    is_ex: bool = False
    packaging_spec: str = ""
    # 製造板幅・板丈(資材展開で製品サイズへ渡す元の値)
    lot_width: float = 0.0
    lot_length: float = 0.0
    # そのロットを開いた日。**日付をまたいだかを言うためだけに持つ。**
    # ツールを開いたままにすると、昨日開いたロットが翌朝も帯に出たままで、
    # 「いまそれを作業中」と読めてしまう。
    # **消しはしない** ── 夜勤は日付をまたいで同じ作業を続けるので、
    # 0時に作業中の製品サイズを取り上げるほうが害が大きい
    lot_day: Optional[Any] = None

    # --- 資材選択から ---
    product_width: int = 0
    product_length: int = 0
    pallet_label: str = ""
    # 疲労度優先。**ロットを切り替えても解除されない**押しっぱなしのモードで、
    # 選定結果そのものが変わる。効いていることが画面のどこにも出ていないと、
    # 「なぜこの結果なのか」を後から誰も説明できない
    fatigue: bool = False

    # 資材展開を通ったか。資材選択が「渡されて来た」と分かるようにする
    expanded: bool = False

    # 資材選択の「倉庫送信」が組み立てた発注の下書き。
    # **まだ登録していない。** 倉庫連携の画面で内容を見せてから登録する
    # (tkinter版も `frmSendConfirm` に切り替えて確認させていた)。
    # 発注は取り消しに人手が要るので、送る前に画面で見せる
    pending_orders: list[dict[str, Any]] = field(default_factory=list)

    # 資材選択の「置き場をまとめて見る」が渡した資材。
    # **棚検索が読んで、置き場をまとめて光らせる**(旧版 `btnMap`)。
    #   {"kind": "board"/"angle", "width": int, "length": int, "count": int}
    map_items: list[dict[str, Any]] = field(default_factory=list)

    # 資材選択の「在庫を見る」が渡したパレット寸法(旧版 `btnUFMAP`)。
    # **簡易在庫が読んで、開いた時点で引いておく**。
    #   {"width": int, "length": int}
    stock_size: dict[str, int] = field(default_factory=dict)

    def clear(self) -> None:
        """新しいロットに移るときは前の作業を持ち越さない。

        VBA `ClearForNewLot` と同じ考え方。前のロットの製品サイズが
        残っていると、別のロットの資材を引いてしまう。

        **`fatigue` は消さない。** 押しっぱなしのモードは利用者が
        自分で切るまで効き続ける(tkinter版 `fatigue_mode` も同じ)。
        """
        self.product_width = 0
        self.product_length = 0
        self.pallet_label = ""
        self.expanded = False
        # 前のロットの発注を次のロットで登録させない
        self.pending_orders = []
        self.map_items = []
        self.stock_size = {}

    # --- ロット検索 -------------------------------------------------
    def apply_lot(self, result: Any) -> None:
        """ロットが決まった。

        `result` は `lot_service.LotSearchResult`。
        モードの判定(1P0113・プロテック)は資材選択の状態機械が持つので、
        ここでは**事実だけ**を持つ。判断を2か所に置かない。
        """
        if result is None or not result.found:
            return
        if result.lot.lot_no != self.lot_no:
            self.clear()
        self.lot_no = result.lot.lot_no
        self.is_ex = result.odr.is_ex
        self.packaging_spec = result.odr.packaging_spec
        self.lot_width = result.lot.width
        self.lot_length = result.lot.length
        self.lot_day = date.today()
        log.info("作業中のロット: %s (EX=%s)", self.lot_no, self.is_ex)

    # --- 資材選択 → 倉庫連携 -----------------------------------------
    def set_map_items(self, items: list[dict[str, Any]]) -> None:
        """棚検索へ渡す資材。**渡すだけで、探すのは棚検索の仕事**。"""
        self.map_items = list(items)
        log.info("置き場をまとめて見る: %s件 (Lot %s)",
                 len(self.map_items), self.lot_no)

    def set_stock_size(self, width: int, length: int) -> None:
        """簡易在庫へ渡すパレット寸法。**渡すだけで、探すのは在庫の仕事**。"""
        self.stock_size = {"width": int(width), "length": int(length)}
        log.info("在庫を見る: %s×%s (Lot %s)", width, length, self.lot_no)

    def set_pending_orders(self, orders: list[dict[str, Any]]) -> None:
        """倉庫送信の下書きを預かる。"""
        self.pending_orders = list(orders)
        log.info("倉庫送信の下書き: %s行 (Lot %s)",
                 len(self.pending_orders), self.lot_no)

    def take_pending_orders(self) -> list[dict[str, Any]]:
        """下書きを取り出して**空にする**。

        登録しても残っていると、画面を開き直すたびに同じ発注を
        出せてしまう。渡したら手放す。
        """
        orders, self.pending_orders = self.pending_orders, []
        return orders

    def can_expand(self) -> bool:
        """資材展開できるか(製造板幅・板丈が取れているか)。"""
        return bool(self.lot_no and self.lot_width and self.lot_length)

    def expand_materials(self) -> bool:
        """資材展開(VBA `Page1_OnBtnHBClick`)。

        製造板幅・板丈を製品サイズに入れる。**「セット」までは押さない**の
        がVBAの挙動で、パレットに収まるかの検証は利用者が自分で行う。
        """
        if not self.can_expand():
            return False
        self.product_width = int(round(self.lot_width))
        self.product_length = int(round(self.lot_length))
        self.expanded = True
        log.info("資材展開: Lot %s 製品サイズ %s×%s",
                 self.lot_no, self.product_width, self.product_length)
        return True

    # --- 表示 -------------------------------------------------------
    def ribbon(self) -> dict[str, Any]:
        """ステータスリボンに出す値。

        「決まっていない」ことも表示に出す。空白だと、決まっていないのか
        表示が壊れているのか区別できない。
        """
        product = (f"{self.product_width}×{self.product_length}"
                   if self.product_width and self.product_length else UNSET)
        return {
            "lot": self.lot_no or NO_LOT,
            "pallet": self.pallet_label or UNSET,
            "product": product,
            "modes": self.modes(),
        }

    def modes(self) -> list[dict[str, str]]:
        """常時見せておく必要のある状態。

        見落とすと手戻りになるものだけを出す。全部出すと、どれが
        効いているのか分からなくなる。

        疲労度優先をここに出すのは、**効いていることが画面のどこにも
        現れない**ため。在庫考慮は候補一覧に「(在庫考慮)」と出るので、
        効果の見える場所に置いてある(同じものを2か所に出さない)。
        """
        chips = []
        # 日付をまたいだロットは、そう分かるようにする。消さないかわりに
        # 「いつのものか」を出す(開いたままの端末のため)
        if self.lot_no and self.lot_day and self.lot_day != date.today():
            chips.append({"text": f"{self.lot_day.month}/{self.lot_day.day}から",
                          "kind": "info"})
        if self.is_ex:
            chips.append({"text": "EX", "kind": "ex"})
        if self.fatigue:
            chips.append({"text": "疲労度優先", "kind": "fatigue"})
        return chips

    def to_dict(self) -> dict[str, Any]:
        return {
            "lot_no": self.lot_no,
            "is_ex": self.is_ex,
            "packaging_spec": self.packaging_spec,
            "lot_width": self.lot_width,
            "lot_length": self.lot_length,
            "product_width": self.product_width,
            "product_length": self.product_length,
            "pallet_label": self.pallet_label,
            "fatigue": self.fatigue,
            "expanded": self.expanded,
            "ribbon": self.ribbon(),
        }


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_context: Optional[WorkContext] = None
_lock = threading.Lock()


def get_context() -> WorkContext:
    global _context
    with _lock:
        if _context is None:
            _context = WorkContext()
        return _context


def reset_context() -> WorkContext:
    """テスト用。作業をまっさらにする。"""
    global _context
    with _lock:
        _context = WorkContext()
        return _context
