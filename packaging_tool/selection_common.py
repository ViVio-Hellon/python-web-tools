"""資材選択の共通の言葉 ── 断りの種別・区分・上限と、結果の型

**画面(`SelectionSession`)と、その段(候補変更など)の両方が使う。**
段ごとのファイルから本体を読みに行くと輪ができるので、共通の言葉は
ここに置いて、双方がここを見る。

断りの種別を文言から推し量らないのが要点 ── 文言を直した日に区別が
壊れる。呼び出し側はこの種別でHTTPのステータスを決める。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 押しっぱなしのモード。どれもロットが変わっても持ち越す。
#   show_all / ex_only / two_stack … パレット一覧の絞り込み
#       (VBA `chkShowAll` / `chkExOnly` / `lblAutoToggle`)
#   fatigue                        … 疲労度優先(VBA `btnFatigueSelect_Click`)
#   stock_aware                    … 在庫考慮(VBA `lblStockToggle_Click`)
TOGGLES = ("show_all", "ex_only", "two_stack", "fatigue", "stock_aware")

# 上用 / 下用。VBA の `lstSelectedBoardsUpper` / `...Lower` に対応する
CATEGORY_UPPER = "upper"
CATEGORY_LOWER = "lower"
CATEGORIES = (CATEGORY_UPPER, CATEGORY_LOWER)

# 断りの種類。**文言から推し量らない** ── 文言を直した日に区別が壊れる
# (`board_selection_service.ApplyResult.reason` と同じ考え方)
REFUSE_BAD_INPUT = "bad_input"          # 数字でない・範囲外 → 400
REFUSE_NOT_LISTED = "not_listed"        # 一覧に無いものを指した → 400
REFUSE_NEEDS_SIZES = "needs_sizes"      # パレット/製品サイズが未確定 → 422
REFUSE_NO_CANDIDATES = "no_candidates"  # 候補が1件も無い → 422
REFUSE_NOT_FOUND = "not_found"          # 探したが見つからなかった → 422
REFUSE_DENIED = "denied"                # 管理者認証が要る → 403
REFUSE_FAILED = "failed"                # 選定そのものが落ちた → 500

# 1P0113 の乗数(VBA `spn1P0113Qty`: Min=1 Max=10)
QTY_MIN = 1
QTY_MAX = 10

# 一覧が何を出しているか。**行そのものは持たない**で、この種別と
# 検索条件だけを覚える。行を抱えると、EXの絞り込みを触ったときに
# 古い行が残る(tkinter版で実際に起きていた)
LIST_ALL = "all"          # マスタ全件(単位・EXのフィルタ適用)
LIST_PRODUCT = "product"  # 製品サイズに収まる候補(「パレット決定」後の確定値で)
LIST_DIRECT = "direct"    # パレット寸法の ±50mm 検索(直接検索モード)
# 製品 幅・丈を入力している最中の一覧(未確定)。片方だけでも絞り込む
# ── まだ確定していない値なので `work_context` の製品サイズには触らない
LIST_PRODUCT_LIVE = "product_live"


@dataclass
class BoardOpResult:
    """ボード・アングルの操作1回の結果。

    `reason` は**呼び出し側がHTTPステータスを決めるための種別**。
    `notes` は結果そのものではないが伝えるべきこと(疲労度マップが
    引けずに通常選定へ倒した、など)。黙って倒すと、疲労度優先を
    ONにしたのに効いていないことに誰も気づけない。
    """

    ok: bool = True
    message: str = ""
    reason: str = ""
    notes: list[str] = field(default_factory=list)


def log_placed(ulog: Any, placed: list) -> None:
    """置いた結果を選定ログに残す。**経路で形を変えない。**

    現行の「ボード配置」も「候補変更」もここを通す。図がおかしいという
    声が届いたときに読むのはこの行で、書き方が経路ごとに違うと、
    現場から送られてきたログのどこを見ればよいかが毎回変わる。

    座標系はVBA踏襲で X=丈方向 / Y=幅方向。
    """
    for item in placed:
        ulog.log(f"  配置: [{item.board_category or '?'}]"
                 f" {item.width} x {item.length} @ ({item.x}, {item.y})")


