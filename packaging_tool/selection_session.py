"""資材選択の作業状態

パレットを「適用」すると `self.palette` に入り、次にボードを選ぶときに
それを見る ── 要求は1本ずつ独立しているので、**画面が覚えていた
ことをプロセス側に置く**(`work_context` / `jobs` と同じ考え方)。

【`work_context` と分けてある理由】
`work_context` が持つのは**リボンに出る事実**と、画面をまたぐ受け渡し
(資材展開)。こちらが持つのは資材選択の作業そのもの ── まだ確定して
いない候補や、押しっぱなしのモード。

とくに製品サイズは、2つの意味を持ち分ける必要がある:

    work_context.product_width  … **入力欄の値**(資材展開が入れる。
                                   利用者が打ち替えることもある)
    session.product             … **「セット」で確定した値**
                                   (パレットに収まることを確かめてある)

`presenters/selection.py` の冒頭にあるとおり、1P0113の資材引きは
確定値ではなく入力欄の値を見るのが正しい。ここを1つにまとめると
その区別が消える。

【保存しない】
落ちたら消える。作業中の候補を持ち越す意味は無い。
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from . import angle_service
from . import board_scoring
from . import board_selection_algorithm as alg
from . import board_selection_service as svc
from . import location_service, material_service, pattern_service
from . import placement_algorithm as place
from . import tiling_algorithm as tiling
from . import user_log as user_log_mod
from . import user_settings, work_context
from .logging_utils import get_logger
from .presenters.selection import SelectionPresenter
from .user_log import get_user_log

log = get_logger("selection_session")

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


@dataclass
class TilingState:
    """敷き詰め方式(「候補変更」)が出した3つの候補と、いま出している軸。

    `key` は**この候補を作ったときの前提**。パレット・製品サイズ・
    ボード種別・在庫考慮・上下共用が変われば候補は作り直しになるので、
    押されるたびに突き合わせます ── これをしないと、寸法を変えたあとに
    押しても前の寸法で作った候補が出ます(VBA が `mTileReady` の
    置きっぱなしで踏んだ落とし穴と同じもの)。

    `axis` が -1 なら「まだ一度も出していない」。
    """

    key: tuple = ()
    lower: tuple[Optional[tiling.TileCand], ...] = (None, None, None)
    upper: tuple[Optional[tiling.TileCand], ...] = (None, None, None)
    axis: int = -1


@dataclass
class SelectionSession:
    """資材選択画面で決まっていること。"""

    presenter: SelectionPresenter

    # --- 確定した寸法 (VBA `self.palette` / `self.product`) ---
    palette: svc.Palette = field(default_factory=svc.Palette)
    product: svc.ProductSize = field(default_factory=svc.ProductSize)

    # --- 押しっぱなしのモード ---
    show_all: bool = False      # EXまで表示
    ex_only: bool = False       # EXオンリー(EX受注のときだけ効く)
    two_stack: bool = False     # 2山積
    # 疲労度優先は `work_context` が持つ(下の property)

    # 一覧が何を出しているか(上の3種)と、直接検索の条件。
    # 絞り込みの経緯を持っていないと、EX絞り込みを触った瞬間に
    # 全件へ戻ってしまう(tkinter版 `_on_pallet_filter_toggled` と同じ問題)
    list_mode: str = LIST_ALL
    direct_width: str = ""
    direct_length: str = ""
    # 製品 幅・丈の入力中の値(`LIST_PRODUCT_LIVE` 用)。まだ「パレット決定」
    # を押していない=確定していないので、`work_context` の製品サイズとは
    # 別に持つ ── 打っている途中の値でボード選定など他の画面の前提を
    # 動かしてはいけない
    live_product_width: str = ""
    live_product_length: str = ""

    # 一覧で選んでいる行(VBA `pal_tree.selection()`)。
    # **発注コードと単位はここにしか無い** ── 幅と丈だけでは決まらず、
    # 同じ寸法でも記号違いで別のコードになる。倉庫送信と帳票が見る
    pallet_row: Optional[svc.PalletSizeRow] = None

    # --- ボード (VBA `cboBoardType` / `lstSelectedBoards*`) ---
    board_type: str = ""
    # 在庫考慮。**マップの有無がそのままON/OFF**(tkinter版 `_stock_map` と同じ)。
    # 別にbool を持つと、片方だけ立った状態を作れてしまう
    stock_map: Optional[dict[str, str]] = None
    selected: svc.SelectedBoards = field(default_factory=svc.SelectedBoards)
    # 自動選定の結果まるごと。狭幅パレット判定とカット情報を配置が見る。
    # 手動で増減したら捨てる ── 自動選定時の前提が成り立たなくなるため
    select_result: Optional[alg.AutoSelectResult] = None
    # 敷き詰め方式(「候補変更」)。**現行の選定・配置には一切影響しません。**
    # 押されたときだけ作られ、押されなければ最後まで None のまま
    tiling: Optional[TilingState] = None

    # --- 1P0113 裸梱包 (VBA `spn1P0113Qty`) ---
    # 乗数。角材本数・松板枚数にそのまま掛かる。製品サイズが変わったときだけ
    # 総梱包数で入れ直す(利用者が手で変えた値をサイズ据え置きの再読込で潰さない)
    qty_1p0113: int = 1

    # --- 管理者 (VBA `btnAuth_Click`) ---
    # 認証したかどうかだけ。**パスワードは持たないし返さない**
    admin: bool = False

    # --- 配置 (VBA `placedBoards`) ---
    # 選定内容や寸法が変わったら捨てる。古い図が残ると、いま選んでいる
    # ボードと画面の図が食い違ったまま発注へ進める
    placement: Optional[place.PlacementContext] = None

    # --- ボード使用実績 (VBAには無い機能) ---
    # 「使用する」で記録した配置の見分け。**同じ配置を二度積まない**
    # ためだけに持つ ── 押した手応えが無いと人はもう一度押すもので、
    # そのたびに数が増えると実績が実態より多くなる。配置や寸法が
    # 変われば見分けも変わるので、次の配置はまた記録できる
    usage_recorded_key: str = ""

    # --- 帳票を紙面で直した内容 (VBA はシートを直せた) ---
    # 帳票の名前 → {欄の名前: 文字}。**別のロットを検索するまで**残す
    # (`clear_for_new_lot`)。VBA版は帳票がExcelシートで出ていたので、
    # 気に入らなければシートを直してから印刷できた。台数が決まらない・
    # 寸法を微調整したい・拠点名を頭に入れたい・期日を書きたい ──
    # どれも紙に出す前に人が決めることで、選定の計算とは別物
    report_edits: dict[str, dict[str, str]] = field(default_factory=dict)

    # --- アングル (VBA `lstSelectedAngles`) ---
    selected_angles: list[int] = field(default_factory=list)
    angle_need_cut: bool = False
    # アングル図は「アングル配置」を押すまで描かない(VBA `btnAngleDraw_Click`)。
    # 選んだ時点で描くと、まだ確かめていない組み合わせが図になる
    angle_drawn: bool = False

    # ------------------------------------------------------------------
    @property
    def stock_aware(self) -> bool:
        return self.stock_map is not None

    @property
    def fatigue(self) -> bool:
        """疲労度優先(VBA `mFatigueMode`)。

        **持ち主は `work_context`**。リボンに常時出す必要があり、
        リボンを組み立てるのはDB接続を持たない `shell_context()` なので、
        画面をまたいで読める場所に置くしかない。

        ここに写しを持たないのは、状態を2か所に持つと必ずどちらかが
        古くなるため。
        """
        return work_context.get_context().fatigue

    @fatigue.setter
    def fatigue(self, value: bool) -> None:
        work_context.get_context().fatigue = bool(value)

    # ------------------------------------------------------------------
    def bind(self, conn: sqlite3.Connection) -> None:
        """この要求の接続を状態機械に持たせる。

        `sqlite3` の接続はスレッドをまたげないので、接続そのものを
        セッションに持たせたままにはできない。要求ごとに差し替える。
        """
        self.presenter.conn = conn

    # ------------------------------------------------------------------
    def invalidate_placement(self) -> None:
        """配置図を無効にする(tkinter版 `_invalidate_placement`)。

        選定内容や基準の寸法が変わったら、前の配置は成り立たない。
        古い図を残したままにすると、いま選んでいるボードと画面の図が
        食い違ったまま発注まで進めてしまう。
        """
        self.placement = None

    def invalidate_angle_plan(self) -> None:
        """アングル図を無効にする。

        図は「アングル配置」を押すまで描かない、という不変条件
        (`angle_drawn` の宣言を参照)は、**押したあとの変更にも**
        効かなければ意味がありません。追加・削除・自動選定のあとも
        `angle_drawn` が True のままだと、押していない組み合わせが
        そのまま図になります(tkinter版はキャンバスを描き直さないので、
        押すまで前の図のままです)。
        """
        self.angle_drawn = False

    # --- パレット ---------------------------------------------------
    @user_log_mod.tag_area("パレット")
    def apply_pallet(self, width_text: str, length_text: str) -> svc.ApplyResult:
        """VBA `btnApplyPalette_Click`。"""
        result, palette = svc.apply_pallet_size(width_text, length_text)
        if not result.ok:
            self.presenter.user_log.log(
                f"[パレット確定] 断りました: {result.message}")
            return result
        before_width = self.palette.width if self.palette.is_set else None
        before_length = self.palette.length if self.palette.is_set else None
        self.palette = palette
        # **決めた瞬間を残す。** ここが残っていないと、あとから
        # 「いつこの寸法になったのか」が分からない
        self.presenter.user_log.log(
            f"[パレット確定] {palette.width} x {palette.length}", emphasis=True)
        # 選んでいた行と寸法が食い違ったら、その行はもう「いま使う
        # パレット」を指していない。持ち越すと**別の寸法の発注コード**を
        # 倉庫へ送ることになる
        if (self.pallet_row is not None
                and (self.pallet_row.width, self.pallet_row.length)
                != (palette.width, palette.length)):
            log.debug("寸法が変わったので選択行を外します: %s×%s",
                      self.pallet_row.width, self.pallet_row.length)
            self.pallet_row = None
        # 下用の基準枠はパレット寸法そのもの。変われば図は作り直し
        self.invalidate_placement()
        # 製品サイズはパレットに収まることを確かめて確定したもの。
        # パレットが**変われば**その確認は無効になる。
        # 同じ寸法で押し直しただけなら確認は生きているので外さない
        # (外していたころは、パレット検索を押し直すだけで製品サイズの
        # 確定が飛び、ボード選定が押せなくなっていた)
        if self.product.is_set and (before_width, before_length) != (
                palette.width, palette.length):
            log.debug("パレットが %s×%s から %s×%s へ変わったので"
                      "製品サイズの確定を解除します",
                      before_width, before_length, palette.width, palette.length)
            self.product = svc.ProductSize()
            # **黙って解除しない。** 押した人は製品サイズが生きている
            # つもりでいるので、ボード選定が押せない理由が分からなくなる
            self.presenter.user_log.log(
                f"  ※パレットが {before_width} x {before_length} から変わったため、"
                "製品サイズの確定を解除しました")
        self._sync_ribbon()
        return result

    def clear_sizes(self) -> None:
        """VBA `btnClearPalProd_Click`。"""
        self.palette = svc.Palette()
        self.product = svc.ProductSize()
        self.list_mode = LIST_ALL
        self.direct_width = self.direct_length = ""
        self.live_product_width = self.live_product_length = ""
        self.pallet_row = None
        self.invalidate_placement()
        self._sync_ribbon()

    def pick_pallet_row(self, width: int, length: int,
                        symbol: str) -> BoardOpResult:
        """一覧の行を選ぶ(VBA `_on_pallet_row_select`)。

        寸法を入力欄へ写すだけでなく**行そのものを覚える**。
        発注コードと単位は行にしか無く、同じ寸法でも記号違いで
        別のコードになるので、倉庫送信と帳票はこれを見る。
        """
        row = svc.find_pallet_row(self.presenter.conn, width, length, symbol)
        if row is None:
            return BoardOpResult(
                False, f"{width}×{length} {symbol} は一覧にありません。",
                REFUSE_NOT_LISTED)
        self.pallet_row = row
        return BoardOpResult(True, "")

    # --- 製品サイズ ---------------------------------------------------
    def apply_product(self, width_text: str,
                      length_text: str) -> tuple[svc.ApplyResult, bool]:
        """VBA `btnApplyProductSize_Click`。戻りは (結果, 回転したか)。"""
        result, product, rotated = svc.apply_product_size(
            width_text, length_text, self.palette)
        if not result.ok:
            self.presenter.user_log.log(
                f"[製品サイズ確定] 断りました: {result.message}")
            return result, False
        self.product = product
        # 回転したかどうかは**現物の載せ方が変わる**ので必ず残す
        self.presenter.user_log.log(
            f"[製品サイズ確定] {product.width} x {product.length}"
            + ("(製品を回転させました)" if rotated else ""), emphasis=True)
        # 上用の基準枠は製品寸法そのもの。変われば図は作り直し
        self.invalidate_placement()
        self._sync_ribbon()
        return result, rotated

    # --- リボン -------------------------------------------------------
    def _sync_ribbon(self) -> None:
        """決まったことをリボンへ映す。

        リボンの出どころは `work_context` 1つ。ここで書き戻さないと、
        画面を移った先のリボンが古いままになる。
        """
        context = work_context.get_context()
        context.pallet_label = (f"{self.palette.width}×{self.palette.length}"
                                if self.palette.is_set else "")
        if self.product.is_set:
            # 「セット」は回転を含む確定操作。入力欄側もそろえる
            context.product_width = self.product.width
            context.product_length = self.product.length

    def toggle(self, name: str) -> bool:
        """押しっぱなしのモードの切り替え。知らない名前なら `KeyError`。"""
        if name not in TOGGLES:
            raise KeyError(name)
        if name == "stock_aware":
            return self._toggle_stock_aware()

        setattr(self, name, not getattr(self, name))
        now = bool(getattr(self, name))
        if name == "two_stack":
            # 2山積はパレット一覧の当たり方そのものを変える
            # (`list_pallets_for_product` が片側2倍の寸法も見る)。
            # 押したのに何も起きていないように見える、を作らない
            self.presenter.user_log.log(
                f"[2山積] {'ON' if now else 'OFF'}", emphasis=True)
        if name == "fatigue":
            # VBA `btnFatigueSelect_Click`。以降の**ボード選定とアングル
            # 自動選定の両方**がこの値を見るので、押したことを記録に残す。
            # 「なぜこの選定結果になったか」は後から選定ログでしか辿れない
            self.presenter.user_log.log(
                f"[疲労度優先] {'ON' if now else 'OFF'}", emphasis=True)
        return now

    def _toggle_stock_aware(self) -> bool:
        """在庫考慮のON/OFF(VBA `lblStockToggle_Click`)。

        ONにした時点でかんばん6表を読んでマップを作る(tkinter版と同じ)。
        毎回引き直さないのは、候補一覧を描くたびに6表を走査することに
        なるため。取り込み直したあとは押し直すと最新になる。
        """
        if self.stock_map is None:
            self.stock_map = material_service.build_board_stock_map(
                self.presenter.conn)
            log.info("在庫考慮ON: %s件", len(self.stock_map))
        else:
            self.stock_map = None
            log.info("在庫考慮OFF")
        return self.stock_map is not None

    # ------------------------------------------------------------------
    # 1P0113 裸梱包
    # ------------------------------------------------------------------
    def product_input(self) -> tuple[int, int]:
        """状態機械へ渡す**入力欄の値**(`presenters/selection.py` 冒頭)。

        1P0113 はパレットが無く「セット」を通せないので、確定値ではなく
        入力欄の値で資材を引くのが正しい。
        """
        context = work_context.get_context()
        return context.product_width, context.product_length

    def set_qty_1p0113(self, qty: int) -> BoardOpResult:
        """乗数の変更(VBA `spn1P0113Qty`: Min=1 Max=10)。"""
        if not QTY_MIN <= qty <= QTY_MAX:
            return BoardOpResult(
                False, f"数量は{QTY_MIN}〜{QTY_MAX}で指定してください。", REFUSE_BAD_INPUT)
        self.qty_1p0113 = qty
        return BoardOpResult(True, f"数量: ×{qty}")

    def toggle_force_1p0113(self) -> BoardOpResult:
        """強制1P0113(VBA `lblForce1P0113_Click`)。

        包装仕様NOに関わらず裸梱包として扱う。**ロットを切り替えると
        解除される**(VBA `ClearForNewLot`)ので、押しっぱなしの
        モード群とは別扱いにしてある。
        """
        width, length = self.product_input()
        change = self.presenter.toggle_force_1p0113(
            product_width=width, product_length=length)
        self._reset_qty(change)
        now = self.presenter.force_1p0113
        return BoardOpResult(
            True, f"1P0113強制: {'ON' if now else 'OFF'}")

    def reload_1p0113_materials(self) -> None:
        """資材を引き直す。製品サイズが変わったときは乗数も入れ直す。"""
        width, length = self.product_input()
        self._reset_qty_value(
            self.presenter.load_1p0113_materials(width, length))

    def _reset_qty(self, change: Any) -> None:
        self._reset_qty_value(getattr(change, "reset_qty_to", None))

    def _reset_qty_value(self, value: Optional[int]) -> None:
        if value is not None:
            self.qty_1p0113 = max(QTY_MIN, min(QTY_MAX, value))

    # ------------------------------------------------------------------
    # ボード種別と候補
    # ------------------------------------------------------------------
    def board_types(self) -> list[str]:
        """選べるボード種別。未選択なら先頭を選ぶ。

        tkinter版 `refresh_board_types` の `current(0)` にあたる。
        **いま選んでいる種別がマスタから消えていたら選び直す**のは、
        残っていない種別のままだと候補が0件になり、「選定できない」のが
        種別のせいだと画面から読み取れないため(取り込み直しで起きる)。
        """
        types = svc.list_board_types(self.presenter.conn)
        if self.board_type not in types:
            self.board_type = types[0] if types else ""
        return types

    def candidates(self) -> list[svc.BoardRow]:
        """いまのボード種別の候補一覧(在庫考慮ONなら▲薄の印つき)。"""
        return svc.list_available_boards(
            self.presenter.conn, self.board_type, stock_map=self.stock_map)

    def set_board_type(self, board_type: str) -> BoardOpResult:
        """種別の切り替え(VBA `cboBoardType` の変更)。

        選定済みのボードはそのまま残す(tkinter版も候補一覧を
        引き直すだけ)。種別を見比べるために切り替えることがある。
        """
        types = self.board_types()
        if board_type not in types:
            return BoardOpResult(
                False, f"ボード種別 '{board_type}' はマスタにありません。",
                REFUSE_NOT_LISTED)
        self.board_type = board_type
        return BoardOpResult(True, f"ボード種別: {board_type}")

    # ------------------------------------------------------------------
    # ボード選定
    # ------------------------------------------------------------------
    def _require_sizes(self) -> Optional[BoardOpResult]:
        """tkinter版 `_require_sizes`。足りないほうを名指しで言う。"""
        if not self.palette.is_set:
            return BoardOpResult(False, "先にパレットサイズを設定してください。",
                                 REFUSE_NEEDS_SIZES)
        if not self.product.is_set:
            return BoardOpResult(False, "先に製品サイズを設定してください。",
                                 REFUSE_NEEDS_SIZES)
        return None

    @user_log_mod.tag_area("ボード")
    def auto_select_boards(self) -> BoardOpResult:
        """自動選定(VBA `btnAutoSelect_Click` / tkinter `do_auto_select_boards`)。

        疲労度を使うかは押しっぱなしの `fatigue` が決める。
        """
        ulog = self.presenter.user_log
        ulog.log("ボード自動選定を開始します", emphasis=True)
        ulog.log(f"  パレット: {self.palette.width} x {self.palette.length}"
                 f" / 製品: {self.product.width} x {self.product.length}")
        ulog.log(f"  ボード種別: {self.board_type or '(未指定)'}"
                 f" / 疲労度={'ON' if self.fatigue else 'OFF'}"
                 f" / 在庫考慮={'ON' if self.stock_aware else 'OFF'}")

        refusal = self._require_sizes()
        if refusal is not None:
            ulog.log(f"  → 中止: {refusal.message}", emphasis=True)
            return refusal

        available = self.candidates()
        if not available:
            ulog.log("  → 中止: 候補ボードが0件です", emphasis=True)
            return BoardOpResult(
                False, "候補ボードがありません。ボード種別を確認してください。",
                REFUSE_NO_CANDIDATES)
        ulog.log(f"  候補ボード: {len(available)}件")

        notes: list[str] = []
        fatigue_lower = fatigue_upper = None
        if self.fatigue:
            fatigue_lower, fatigue_upper = board_scoring.build_selection_fatigue_maps(
                self.presenter.conn, available, user_settings.get_position(),
                board_type=self.board_type,
                pallet_width=self.palette.width, pallet_length=self.palette.length,
                product_width=self.product.width, product_length=self.product.length)
            if fatigue_lower is None:
                notes.append("疲労度マップを取得できなかったため通常選定で実行しました。")
                ulog.log("  ※疲労度マップを取得できず、通常選定に切り替えました")
            else:
                ulog.log(f"  疲労度マップ: 拠点 {user_settings.get_position() or '(未設定)'}")

        try:
            # **却下と補正の理由をそのまま流す**(`user_log.bridge_from`)。
            # 現場が要るのは決まったことではなく、そこへ至った経緯
            with user_log_mod.bridge_from(
                    ulog, "packaging_tool.board_selection_algorithm",
                    "packaging_tool.board_scoring"):
                result = alg.auto_select_boards(
                    available, self.palette, self.product,
                    fatigue_map_lower=fatigue_lower, fatigue_map_upper=fatigue_upper,
                    fatigue_mode=self.fatigue, stock_aware=self.stock_aware,
                    # 包装仕様NOで決まったモードを選定へ渡す。プロテックと
                    # 上下共用は「上用=下用と同サイズ」を強制する分岐に効く。
                    # どのモードを渡すかはプレゼンタが知っている
                    **self.presenter.selection_flags())
        except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
            log.exception("自動選定エラー")
            ulog.log(f"  → 自動選定エラー: {exc}", emphasis=True)
            return BoardOpResult(False, f"自動選定エラー: {exc}", REFUSE_FAILED)

        self.select_result = result
        self.selected.lower = list(result.lower)
        self.selected.upper = list(result.upper)
        # 現行の選定に戻ったのだから、「候補変更」の回り位置も戻す。
        # 残しておくと、次に押したときにAではなくBから出てきて
        # 「同じ操作で違うものが出る」ことになる
        self.tiling = None
        self.invalidate_placement()
        # **何が選ばれたのかを1枚ずつ残す。** 「上用2種類」とだけ言われても、
        # あとから「なぜこの寸法になったのか」を追えない(現場の声)
        for label, boards in (("上用", result.upper), ("下用", result.lower)):
            if not boards:
                ulog.log(f"  {label}: なし")
                continue
            for board in boards:
                ulog.log(f"  {label}: {board.width} x {board.length}"
                         f" × {board.count}枚")
        log.info("自動選定: 上用%s種 下用%s種 (疲労度=%s 在庫考慮=%s)",
                 len(result.upper), len(result.lower), self.fatigue, self.stock_aware)
        ulog.log(f"ボード自動選定 完了: 上用 {len(result.upper)}種 / "
                 f"下用 {len(result.lower)}種", emphasis=True)
        return BoardOpResult(
            True,
            f"ボードを自動選択しました。上用 {len(result.upper)}種類 / "
            f"下用 {len(result.lower)}種類",
            notes=notes)

    @user_log_mod.tag_area("ボード")
    def add_board(self, category: str, width: int, length: int,
                  count: int) -> BoardOpResult:
        """手動追加(VBA `btnAddBoardUpper/Lower_Click`)。

        **候補一覧にある寸法しか受け付けない。** tkinter版は一覧から
        選ばせることでこれを保証していた(強制選択機能)。Web版は値が
        そのまま送られてくるので、サーバ側で確かめる ── そうしないと
        マスタに無いボードを選定に載せられてしまう。
        """
        if category not in CATEGORIES:
            return BoardOpResult(False, f"上用/下用のどちらかを指定してください: {category}",
                                 REFUSE_BAD_INPUT)
        if not any(row.width == width and row.length == length
                   for row in self.candidates()):
            return BoardOpResult(
                False, f"{width}×{length} は候補一覧にありません。一覧から選んでください。",
                REFUSE_NOT_LISTED)

        target = (self.selected.upper if category == CATEGORY_UPPER
                  else self.selected.lower)
        result = svc.add_selected_board(target, width, length, count)
        if not result.ok:
            return BoardOpResult(False, result.message, REFUSE_BAD_INPUT)

        # 手動追加は自動選定の結果ではないので狭幅フラグを引き継がない
        # (tkinter版 `do_add_board` と同じ)
        self.select_result = None
        self.invalidate_placement()
        label = "上用" if category == CATEGORY_UPPER else "下用"
        # 手で足した1枚も残す。**自動と手動の区別が付かないログは、
        # あとから「なぜこうなったか」を説明できない**
        self.presenter.user_log.log(
            f"[手動] {label}に追加: {width} x {length} × {count}枚")
        return BoardOpResult(True, f"{label}に {width}×{length} を{count}枚 追加しました")

    @user_log_mod.tag_area("ボード")
    def remove_board(self, category: str, index: int) -> BoardOpResult:
        """選定済みから1行外す(tkinter版 `_remove_selected`)。

        **`select_result` は捨てない。** 追加とは非対称で、tkinter版も
        削除では `_select_result` を持ち越す。ここは
        (a) 切断依頼のカット記録 (`cut_info` / `length_cut_info` /
        `length_cut_count`) と (b) 配置の狭幅パレット判定の**唯一の
        出どころ**なので、捨てると1行外しただけで切断依頼が
        「カットは不要です」に変わり、配置図の前提も変わる。
        """
        if category not in CATEGORIES:
            return BoardOpResult(False, f"上用/下用のどちらかを指定してください: {category}",
                                 REFUSE_BAD_INPUT)
        target = (self.selected.upper if category == CATEGORY_UPPER
                  else self.selected.lower)
        if not 0 <= index < len(target):
            # 別のタブで先に消されている。押した行が消えたのに黙って
            # 別の行を消すと、消したつもりのないものが消える
            return BoardOpResult(False, "その行はもうありません。表示を取り直してください。",
                                 REFUSE_BAD_INPUT)
        removed = target.pop(index)
        self.invalidate_placement()
        label = "上用" if category == CATEGORY_UPPER else "下用"
        self.presenter.user_log.log(
            f"[手動] {label}から除外: {removed.width} x {removed.length}")
        return BoardOpResult(
            True, f"{label}から {removed.width}×{removed.length} を外しました")

    @user_log_mod.tag_area("ボード")
    def place_boards(self) -> BoardOpResult:
        """「ボード配置」(VBA `btnAutoPlace_Click` / tkinter `do_auto_place`)。

        狭幅パレット選定が使われたかどうかは**自動選定の結果**が持っている。
        手で増減したあとは `select_result` を捨ててあるので通常配置になる ──
        当時の前提のまま並べると、手で足したボードが図の外へ出る。
        """
        ulog = self.presenter.user_log
        ulog.log("ボード配置を開始します", emphasis=True)

        refusal = self._require_sizes()
        if refusal is not None:
            ulog.log(f"  → 中止: {refusal.message}", emphasis=True)
            return refusal
        if not (self.selected.lower or self.selected.upper):
            ulog.log("  → 中止: ボードが選定されていません", emphasis=True)
            return BoardOpResult(False, "先にボードを選定してください。",
                                 REFUSE_NO_CANDIDATES)

        narrow_lower = narrow_upper = False
        protec_result = None
        if self.select_result is not None:
            narrow_lower = self.select_result.lower_result.state.narrow_pallet
            narrow_upper = self.select_result.upper_result.narrow_pallet
            protec_result = self.select_result.lower_result.protec_result
        elif self.presenter.protec.is_protec:
            # 手で増減したあとは `select_result`(ProtecCutResultを含む)を
            # 捨ててあるので、配置直前にここで後付けする(VBA
            # `ApplyProtecRulesToLowerList`)。これをしないと、
            # プロテックのはずなのに「唯一の正解」が無いまま配置に入り、
            # 配置段階が独自に製品幅厳守を判定し直して静かに配置漏れを
            # 起こす
            protec_result = alg.apply_protec_rules_to_lower_list(
                self.selected.lower, self.product, self.palette,
                is_1p1216=self.presenter.protec.is_1p1216)
            ulog.log("  ※手で増減したため、狭幅パレットの前提は引き継ぎません")
        else:
            # 手で増減したあとは当時の前提を引き継がない。**なぜ配置が
            # 変わったのか**を追えるよう、その事実を残す
            ulog.log("  ※手で増減したため、狭幅パレットの前提は引き継ぎません")
        ulog.log(f"  狭幅パレット: 下用={'はい' if narrow_lower else 'いいえ'}"
                 f" / 上用={'はい' if narrow_upper else 'いいえ'}")

        try:
            with user_log_mod.bridge_from(
                    ulog, "packaging_tool.placement_algorithm"):
                self.placement = place.auto_place_boards(
                    self.selected.lower, self.selected.upper,
                    self.palette, self.product,
                    narrow_lower=narrow_lower, narrow_upper=narrow_upper,
                    protec_result=protec_result,
                    is_protec_mode=self.presenter.protec.is_protec,
                    is_1p1216=self.presenter.protec.is_1p1216)
        except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
            log.exception("配置エラー")
            self.placement = None
            ulog.log(f"  → 配置エラー: {exc}", emphasis=True)
            return BoardOpResult(False, f"配置エラー: {exc}", REFUSE_FAILED)

        placed = self.placement.placed
        log.info("配置完了: %s枚", len(placed))
        _log_placed(ulog, placed)
        ulog.log(f"ボード配置 完了: {len(placed)}枚", emphasis=True)
        return BoardOpResult(True, f"ボードを配置しました({len(placed)}枚)")

    # ------------------------------------------------------------------
    # 候補変更(敷き詰め方式) ── 現行の選定・配置には触らない
    # ------------------------------------------------------------------
    def _tiling_key(self, available: list) -> tuple:
        """候補を作った前提。**1つでも変われば作り直す。**

        古い候補を出してしまうと、**いま入力してある寸法と何の関係も
        無いボードが選定され、そのまま図になる**。図と現物が食い違った
        まま切断依頼や倉庫送信まで進めるので、静かに間違うたぐいの中でも
        いちばん重い。

        だから「作り直す条件」を推し量らず、**候補の中身を決めるもの
        すべて**を鍵にする。

            パレット・製品サイズ … 許容範囲そのもの
            ボード種別・在庫考慮 … 候補ボードの引き方
            上下共用            … 上用の許容範囲が下用と同じになる
            候補ボードの寸法    … 取り込み直しで**寸法が増減する**。
                                  これを入れないと、マスタを入れ直した
                                  あとに「もう無い寸法」が出続ける

        VBA側は寸法の4つだけを文字列にして持っている(`mTileKey`)。
        残り3つも変われば候補は変わるので、そちらも足すのが正しい。
        """
        return (self.palette.width, self.palette.length,
                self.product.width, self.product.length,
                self.board_type, self.stock_aware,
                self.presenter.is_shared_board_mode,
                tuple(sorted((row.width, row.length) for row in available)))

    def _axis_signature(self, state: TilingState, axis: int) -> tuple:
        """その軸で画面に出るもの。上下そろって同じなら「同じ候補」。"""
        return (tiling.cand_signature(state.lower[axis]),
                tiling.cand_signature(state.upper[axis]))

    def _next_tiling_axis(self, state: TilingState) -> tuple[int, list[str]]:
        """次に出す軸。**中身が変わる軸だけ**を回る(VBA `NextValidAxis`)。

        飛ばすものが2つあります。

            空の軸       … その軸に候補が無い(要カット0の解が無い等)
            同じ中身の軸 … いま出ているものと**選定も図も同じ**

        2つ目が要るのは、A(枚数最小)とB(種類最小)がしばしば同じ候補に
        なるためです。「3枚1種」のように枚数でも種類でも最適なものが
        1つしか無ければ両方の1位が一致し、全件検証でも3軸すべて同じに
        なるのが上用15.3% / 下用42.4%。**押しても画面が変わらないと、
        現場からは壊れているように見えます**(空の軸だけを飛ばして
        いたころは実際にそうなっていました)。

        初回も**中身のある軸から**始めます。VBA は初回を必ず A に
        していたため、Aが空の品では押してもリストが空になっていました。

        【上下そろう軸を先に出す】
        敷き詰めで解が出るかは上用(製品幅-1まで)と下用(パレット幅+50
        まで)で別々に決まります。「上用は要カット0では作れないが、
        カット1枚まで許せば作れる」ということが実際にあり、そのとき
        A(要カット0・枚数最小)を出すと**下用だけ入れ替わって上用が空**に
        なります(現場のログ: 下用198件/上用69件、A・Bは上用なし、
        Cだけ上用あり)。片側だけの答えは使えないので、**両方そろう軸を
        先に選び**、どれもそろわないときだけ片側だけの軸を出します。

        戻りは `(次の軸, 飛ばした軸の名前)`。飛ばしたことは**画面にも
        出します** ── 選定ログを開かないと分からないのでは、押した回数と
        出た候補が合わない理由に気づけません。
        """
        ulog = self.presenter.user_log
        first = state.axis < 0
        current = None if first else self._axis_signature(state, state.axis)
        start = 0 if first else state.axis + 1
        skipped: list[str] = []
        complete: list[int] = []
        partial: list[int] = []
        # **見に行くのは残りの2つだけ。** まだ何も出していないとき
        # (初回)は3つとも見る。出したあとで3周ぶん回すと、3周目は
        # いま出している軸そのものに戻ってきて、「候補Aは候補Aと同じ
        # 内容のため飛ばしました」という読めないログが1行出る
        for i in range(3 if first else 2):
            axis = (start + i) % 3
            if state.lower[axis] is None and state.upper[axis] is None:
                log.debug("候補変更: 軸%s は空なので飛ばします",
                          tiling.AXIS_NAMES[axis])
                continue
            if current is not None and self._axis_signature(state, axis) == current:
                # **黙って飛ばさない。** 押した回数と出た候補が合わないと、
                # 「押し損ねたのか、同じものが出たのか」が分からなくなる
                name = tiling.AXIS_NAMES[axis]
                skipped.append(name)
                ulog.log(f"  候補{name} は候補{tiling.AXIS_NAMES[state.axis]}"
                         " と同じ内容のため飛ばしました")
                log.info("候補変更: 軸%s は軸%s と同じ内容なので飛ばしました",
                         name, tiling.AXIS_NAMES[state.axis])
                continue
            (complete if not self._missing_sides(state, axis) else partial).append(axis)

        for axis in complete:
            return axis, skipped
        for axis in partial:
            log.info("候補変更: 軸%s は片側だけですが、両方そろう軸が"
                     "ありません", tiling.AXIS_NAMES[axis])
            return axis, skipped
        return -1, skipped

    def _missing_sides(self, state: TilingState, axis: int) -> list[str]:
        """その軸で**空になる側**。画面に出さない側は数えない。

        上下共用のときは上用の欄そのものが無いので、上用が空でも
        「片側だけ」にはなりません。
        """
        share = self.presenter.is_shared_board_mode
        return [label for label, cand, shown in
                (("下用", state.lower[axis], True),
                 ("上用", state.upper[axis], not share))
                if shown and cand is None]

    @user_log_mod.tag_area("ボード")
    def change_candidate(self) -> BoardOpResult:
        """「候補変更」(VBA `TileChangeCandidate`)。

        敷き詰め方式で作った A(枚数最小)→B(種類最小)→C(カット1枚許容)
        を押すたびに切り替え、**選定リストの入れ替えと配置までここで
        完結させます。** 現行の「ボード選定」「ボード配置」の経路には
        一切入りません ── VBA では配置側に分岐を入れたために、現行で
        選定し直しても図だけが古い敷き詰め候補を描く事故が起きました。
        """
        ulog = self.presenter.user_log
        refusal = self._require_sizes()
        if refusal is not None:
            return refusal
        if self.presenter.protec.is_protec:
            # カット前提の別ロジックで、在庫が3種/1種しかなく敷き詰めが
            # 成立しない(成立率 上用0.8% / IK 0.1%)。現行のプロテック
            # 選定を使う
            return BoardOpResult(
                False, "プロテックは「候補変更」の対象外です。"
                       "「ボード選定」を使ってください。",
                REFUSE_NO_CANDIDATES)

        available = self.candidates()
        if not available:
            return BoardOpResult(
                False, "候補ボードがありません。ボード種別を確認してください。",
                REFUSE_NO_CANDIDATES)

        # **前提が変わっていたら候補を捨てて作り直す。** 押すたびに
        # 突き合わせる ── 「変わったときに捨てる」を変更のたびに
        # 書き足していく形にすると、いつか1か所書き忘れる
        key = self._tiling_key(available)
        if self.tiling is None or self.tiling.key != key:
            if self.tiling is not None:
                ulog.log("[候補変更] 前提が変わったので候補を作り直します")
                log.info("候補変更: 前提が変わったので候補を捨てます")
            self.tiling = self._build_tiling(available, key)
        axis, skipped = self._next_tiling_axis(self.tiling)
        if axis < 0:
            # **どちらなのかを言い分ける。** 「候補が作れなかった」のと
            # 「作れたが全部同じ内容だった」のとでは、次にすることが違う
            # (前者は在庫や寸法を疑う。後者はこれが唯一の答え)
            if self.tiling.axis >= 0:
                name = tiling.AXIS_NAMES[self.tiling.axis]
                ulog.log(f"[候補変更] ほかの軸は候補{name} と同じ内容でした",
                         emphasis=True)
                return BoardOpResult(
                    False, f"ほかに別の候補はありません。"
                           f"A/B/C とも候補{name} と同じ内容になりました。",
                    REFUSE_NOT_FOUND)
            ulog.log("[候補変更] 別候補が見つかりませんでした", emphasis=True)
            return BoardOpResult(False, "別候補が見つかりませんでした。",
                                 REFUSE_NOT_FOUND)

        self.tiling.axis = axis
        return self._apply_tiling(axis, skipped)

    def _build_tiling(self, available: list, key: tuple) -> TilingState:
        """3つの軸を作る(VBA `BuildTileCandidates`)。"""
        ulog = self.presenter.user_log
        stock = tiling.build_stock(available)
        share = self.presenter.is_shared_board_mode
        ulog.log("[候補変更] 敷き詰め方式で候補を作ります", emphasis=True)
        ulog.log(f"  パレット: {self.palette.width} x {self.palette.length}"
                 f" / 製品: {self.product.width} x {self.product.length}"
                 f" / 在庫: {len(stock)}種")

        def solve(is_upper: bool) -> tuple[Optional[tiling.TileCand], ...]:
            bounds = tiling.get_bounds(is_upper, share, self.product, self.palette)
            cands = tiling.solve_tiling(stock, bounds)
            ulog.log(f"  {'上用' if is_upper else '下用'}"
                     f" 許容幅[{bounds.w_lo},{bounds.w_hi}]"
                     f" 許容丈[{bounds.l_lo},{bounds.l_hi}]"
                     f" → 候補 {len(cands)}件")
            return tiling.pick_axes(cands)

        lower = solve(False)
        # 上下共用は「上用=下用と同サイズ」。別々に解くと、たまたま同点の
        # 別候補が選ばれて上下がずれることがあるので下用をそのまま使う
        upper = lower if share else solve(True)
        state = TilingState(key=key, lower=lower, upper=upper)

        # **同じ内容になった軸をここで名指ししておく。** 押す前から
        # 「A と B は同じ」と分かっていれば、押しても変わらないことに
        # 驚かずに済む(全件検証では3軸すべて同じが上用15.3%/下用42.4%)
        seen: dict[tuple, int] = {}
        for i, name in enumerate(tiling.AXIS_NAMES):
            note = " / ".join(f"{label}{_tiling_note(cands[i])}"
                              for label, cands in (("下用", lower), ("上用", upper)))
            if lower[i] is None and upper[i] is None:
                ulog.log(f"  候補{name}: なし")
                continue
            sig = self._axis_signature(state, i)
            same = seen.get(sig)
            if same is None:
                seen[sig] = i
                ulog.log(f"  候補{name}: {note}")
            else:
                ulog.log(f"  候補{name}: {note}"
                         f" ※候補{tiling.AXIS_NAMES[same]} と同じ内容")
        return state

    def _apply_tiling(self, axis: int,
                      skipped: list[str]) -> BoardOpResult:
        """選んだ軸を選定リストと配置に反映する。

        `skipped` は「いま出ているものと同じ内容だったので飛ばした軸」。
        画面にも出す ── A と B が同じ候補になるのはよくあることで
        (全件検証で3軸すべて同じが上用15.3%/下用42.4%)、黙って飛ばすと
        「A の次が C なのはなぜか」が読めません。
        """
        assert self.tiling is not None
        ulog = self.presenter.user_log
        share = self.presenter.is_shared_board_mode
        lower_cand = self.tiling.lower[axis]
        upper_cand = self.tiling.upper[axis]
        name = tiling.AXIS_NAMES[axis]

        # **先に置いてみてから差し替える。** いまの選定を消したあとで
        # 「1枚も置けませんでした」になると、押しただけで選定が消えた
        # ように見える(現場の声:「候補変更で何も配置されないことがある」)
        ctx = place.PlacementContext(palette=self.palette, product=self.product)
        if lower_cand is not None:
            tiling.place_tiling_boards(ctx, lower_cand, place.CATEGORY_LOWER)
        if upper_cand is not None and not share:
            tiling.place_tiling_boards(ctx, upper_cand, place.CATEGORY_UPPER)
        if not ctx.placed:
            ulog.log(f"[候補変更] 候補{name} は1枚も置けませんでした", emphasis=True)
            log.info("候補変更: 軸=%s で配置0枚。選定は変えません", name)
            return BoardOpResult(
                False, f"候補{name} は配置できませんでした。"
                       "いまの選定はそのままにしています。",
                REFUSE_NOT_FOUND)

        self.selected.lower = (tiling.cand_to_selected(lower_cand)
                               if lower_cand is not None else [])
        self.selected.upper = (tiling.cand_to_selected(upper_cand)
                               if upper_cand is not None else [])
        # 狭幅パレットの前提は持ち込まない(VBA も `NarrowLower/Upper` を
        # False に戻す)。敷き詰めは狭幅の専用経路を行構成の数え上げに
        # 吸収しているので、当時の前提を引き継ぐ意味が無い
        self.select_result = None
        self.placement = ctx

        ulog.log(f"[候補変更] 候補{name} に切り替えました", emphasis=True)
        for label, boards in (("上用", self.selected.upper),
                              ("下用", self.selected.lower)):
            if not boards:
                ulog.log(f"  {label}: なし")
                continue
            for board in boards:
                ulog.log(f"  {label}: {board.width} x {board.length}"
                         f" × {board.count}枚 [{board.tag}]")
        # **置いた結果も残す。** 現行の経路と同じ形で書く ── 図がおかしい
        # という声が届いたときに、選んだものだけ分かっても座標が分からず
        # 追えない(現場に「選定ログを見せてください」と頼んだら、候補変更の
        # ぶんには配置の行が1つも無かった)
        _log_placed(ulog, ctx.placed)
        log.info("候補変更: 軸=%s 上用%s種 下用%s種 配置%s枚",
                 name, len(self.selected.upper), len(self.selected.lower),
                 len(ctx.placed))

        notes = ([f"候補{'・'.join(skipped)} は同じ内容だったので飛ばしました。"]
                 if skipped else [])
        # **片側だけ空になったら必ず言う。** 両方そろう軸を先に選ぶので
        # (`_next_tiling_axis`)ここに来るのは「どの軸でもそろわない」
        # ときだけ。それでも黙って空にすると「押したら消えた」ように
        # しか見えないので、理由を出す
        missing = self._missing_sides(self.tiling, axis)
        if missing:
            text = (f"{'・'.join(missing)}は敷き詰めで置ける組み合わせが"
                    "どの候補にもありませんでした(その分は空になります)。")
            notes.append(text)
            ulog.log(f"  ※{text}")
        return BoardOpResult(
            True, f"候補{name} に切り替えました。"
                  f"上用 {len(self.selected.upper)}種類 / "
                  f"下用 {len(self.selected.lower)}種類",
            notes=notes)

    def edits_for(self, report: str) -> dict[str, str]:
        """その帳票で直した内容。無ければ空。"""
        return dict(self.report_edits.get(report, {}))

    def set_report_edits(self, report: str, edits: dict) -> BoardOpResult:
        """紙面で直した内容を覚える(別のロットを検索するまで)。

        **文字だけを覚える。** HTMLをそのまま持つと、次に出すときに
        何が書かれるか分からなくなる(帳票は印刷して現場に配るもの)。
        """
        clean = {str(k): str(v) for k, v in (edits or {}).items()
                 if isinstance(k, str) and k}
        self.report_edits[report] = clean
        log.info("帳票を直しました: %s %s件", report, len(clean))
        return BoardOpResult(True, "保存しました")

    def map_items(self) -> list[dict]:
        """棚検索へ渡す資材(旧版 `btnMap`)。

        **選んだものを並べるだけ。** どこに置いてあるか・どれだけ歩くかは
        棚検索が決める(`layout_session.show_selection`)。
        上用・下用・アングルを全部入れるのは、取りに行くのが1回の作業
        だからで、種類ごとに分けても現場は結局まとめて回る。
        """
        items: list[dict] = []
        for board in list(self.selected.lower) + list(self.selected.upper):
            items.append({"kind": "board", "width": board.width,
                          "length": board.length, "count": board.count})
        for length in self.selected_angles:
            items.append({"kind": "angle", "width": 0,
                          "length": length, "count": 1})
        return items

    def stock_size(self) -> dict:
        """簡易在庫へ渡すパレット寸法(旧版 `btnUFMAP`)。

        決まっていなければ空。**在庫が有るかどうかは見ない** ── それは
        簡易在庫が引いて答えることで、ここで先に引くと同じ問い合わせが
        2か所に育つ。
        """
        if not self.palette.is_set:
            return {}
        return {"width": int(self.palette.width),
                "length": int(self.palette.length)}

    def draw_angles(self) -> BoardOpResult:
        """「アングル配置」(VBA `btnAngleDraw_Click`)。

        図を描くだけで、選んだ本数は変えない。製品丈が要るのは、
        アングルが何をカバーするのかが決まらないと図にならないため。
        """
        if not self.product.is_set:
            return BoardOpResult(False, "先に製品サイズを設定してください。",
                                 REFUSE_NEEDS_SIZES)
        if not self.selected_angles:
            return BoardOpResult(False, "先にアングルを選んでください。",
                                 REFUSE_NO_CANDIDATES)
        self.angle_drawn = True
        return BoardOpResult(True, f"アングルを配置しました({len(self.selected_angles)}本)")

    def clear_boards(self) -> BoardOpResult:
        """「クリア」(VBA `btnClearAll_Click` / tkinter `do_clear_selection`)。

        消すのは**ボードだけ**。選択アングルは残す(tkinter版と同じ)。
        アングルには専用の「削除」があり、そちらで外す。並行運用の
        あいだに片方だけ違う消え方をすると、どちらが正しいのか
        現場には確かめようがない。
        """
        self.selected = svc.SelectedBoards()
        self.select_result = None
        # 「候補変更」の回り位置も戻す。消したあとに押したら**Aから**
        self.tiling = None
        self.invalidate_placement()
        return BoardOpResult(True, "選定したボードをクリアしました")

    # ------------------------------------------------------------------
    # アングル
    # ------------------------------------------------------------------
    def angle_candidates(self) -> list[int]:
        """候補アングル丈。`load_all_angle_lengths` は未登録でも `[0]` を返すので落とす。"""
        return [length for length
                in angle_service.load_all_angle_lengths(self.presenter.conn)
                if length]

    def add_angle(self, length: int) -> BoardOpResult:
        """アングル追加(VBA `btnAngleAdd_Click`)。候補にある丈だけ受け付ける。"""
        if length not in self.angle_candidates():
            return BoardOpResult(
                False, f"アングル丈 {length} は候補一覧にありません。一覧から選んでください。",
                REFUSE_NOT_LISTED)
        self.selected_angles.append(length)
        # 手動追加はカット前提の情報を持たない(tkinter版 `do_add_angle`)
        self.angle_need_cut = False
        self.invalidate_angle_plan()
        return BoardOpResult(True, f"アングル {length} を追加しました")

    def remove_angle(self, index: int) -> BoardOpResult:
        """アングル削除(VBA `btnAngleRemove_Click`)。"""
        if not 0 <= index < len(self.selected_angles):
            return BoardOpResult(False, "その行はもうありません。表示を取り直してください。",
                                 REFUSE_BAD_INPUT)
        removed = self.selected_angles.pop(index)
        self.angle_need_cut = False
        self.invalidate_angle_plan()
        return BoardOpResult(True, f"アングル {removed} を外しました")

    def auto_select_angles(self) -> BoardOpResult:
        """アングル自動選定(VBA `btnAngleAuto_Click`)。

        パレットが未設定でも動く(その場合は脚数を考慮しない)。
        製品丈だけは要る ── 何をカバーするのかが決まらないため。
        """
        ulog = self.presenter.user_log
        ulog.log("アングル自動選定を開始します", emphasis=True)

        if not self.product.is_set:
            ulog.log("  → 中止: 製品サイズが未設定です", emphasis=True)
            return BoardOpResult(False, "先に製品サイズを設定してください。",
                                 REFUSE_NEEDS_SIZES)

        conn = self.presenter.conn
        angles = self.angle_candidates()
        if not angles:
            ulog.log("  → 中止: アングルデータが0件です", emphasis=True)
            return BoardOpResult(False, "アングルデータがありません。",
                                 REFUSE_NO_CANDIDATES)
        ulog.log(f"  製品丈: {self.product.length} / 候補: {len(angles)}件")

        pallet_len = leg_count = 0
        if self.palette.is_set:
            pallet_len = self.palette.length
            leg_count = angle_service.get_leg_count(
                conn, self.palette.width, self.palette.length)
            ulog.log(f"  パレット丈: {pallet_len} / 脚数: {leg_count}")
        else:
            # 脚数を見ずに選んだことを残す。**同じ製品でも本数が変わる**
            ulog.log("  パレット未設定のため、脚数を考慮せずに選定します")

        notes: list[str] = []
        fat_map = None
        if self.fatigue:
            try:
                fat_map = location_service.build_angle_fatigue_map(
                    conn, angles, user_settings.get_position())
            except Exception as exc:                  # noqa: BLE001 - 補助情報
                # 棚データが未登録でもアングル選定自体は続けられる
                log.warning("アングル疲労度の算出に失敗: %s", exc)
            if not fat_map:
                message = "[疲労度優先] アングルの棚が引けないため通常の優先順位で選定します"
                self.presenter.user_log.log(message)
                notes.append(message)

        result = angle_service.select_angles(
            self.product.length, angles, pallet_len, leg_count,
            fatigue_mode=bool(fat_map), fat_map=fat_map)
        self.angle_need_cut = result.need_cut
        # 選び直した時点で、いま出ている図は別の組み合わせのもの
        self.invalidate_angle_plan()

        if result.count == 0:
            self.selected_angles = []
            ulog.log("  → 適合するアングルがありませんでした", emphasis=True)
            return BoardOpResult(False, "適合するアングルが見つかりませんでした。",
                                 REFUSE_NOT_FOUND, notes=notes)

        if result.count <= 2:
            self.selected_angles = [result.angle1]
            if result.count == 2:
                self.selected_angles.append(result.angle2)
        else:
            self.selected_angles = list(result.angles)

        # **選ばれた1本ずつを残す。** 「3本」とだけでは、あとから
        # 現物と突き合わせられない
        for length in self.selected_angles:
            ulog.log(f"  アングル: {length}mm")
        if result.need_cut:
            ulog.log("  ※切断が必要です")
        self.presenter.user_log.log(
            f"アングル選定完了: {result.count}本 {result.info}"
            " → アングル配置ボタンで描画してください", emphasis=True)
        return BoardOpResult(True, f"アングルを{result.count}本 選定しました: {result.info}",
                             notes=notes)

    # ------------------------------------------------------------------
    # 管理者と実績パターン
    # ------------------------------------------------------------------
    def authenticate(self, password: str) -> BoardOpResult:
        """管理者認証(VBA `btnAuth_Click`)。

        **照合はサーバでしか行わない。** パスワードは応答に載せないし、
        画面へ渡すのは「認証したか」の1ビットだけ(設計書 §3.6)。
        """
        # 照合の出どころは `admin_password` ただ1つ。設定画面から
        # 変えられるようになったので、ここで `config` を直接読まない
        from . import admin_password
        ok = admin_password.verify(password)
        self.admin = ok
        if not ok:
            log.warning("管理者認証に失敗しました")
            return BoardOpResult(False, "パスワードが正しくありません。",
                                 REFUSE_DENIED)
        log.info("管理者認証に成功しました")
        return BoardOpResult(True, "認証しました")

    def save_pattern(self) -> BoardOpResult:
        """実績パターンの保存(VBA `btnSavePattern_Click`)。

        **認証していなければ保存できない。** tkinter版は保存ボタンを
        無効にしてこれを保っていたが、Web版は要求がそのまま届くので
        経路の側で断る(倉庫連携の確認と同じ考え方)。
        """
        if not self.admin:
            return BoardOpResult(False, "管理者認証が必要です。", REFUSE_DENIED)
        if self.placement is None or not self.placement.placed:
            return BoardOpResult(False, "先に配置を実行してください。",
                                 REFUSE_NO_CANDIDATES)

        def rows(boards: list, usage: str) -> list:
            # VBA同様、保存されるのは 幅/丈/枚数/用途 のみ(選定タグは保存しない)
            return [pattern_service.PatternBoard(
                width=b.width, length=b.length, count=b.count, usage=usage)
                for b in boards]

        try:
            pattern_id = pattern_service.save_new_pattern(
                self.presenter.conn,
                pallet_width=self.palette.width, pallet_length=self.palette.length,
                boards_lower=rows(self.selected.lower, pattern_service.USAGE_LOWER),
                boards_upper=rows(self.selected.upper, pattern_service.USAGE_UPPER),
                product_width=self.product.width,
                product_length=self.product.length)
        except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
            log.exception("パターン保存エラー")
            return BoardOpResult(False, f"保存エラー: {exc}", REFUSE_FAILED)

        log.info("実績パターンを保存しました: No.%s", pattern_id)
        return BoardOpResult(True, f"実績パターンを保存しました(No.{pattern_id})")

    # ------------------------------------------------------------------
    # ボード使用実績
    # ------------------------------------------------------------------
    def _usage_key(self) -> str:
        """いまの配置の見分け。同じものを二度積まないためだけに使う。

        **寸法の並びまで含める。** ロットとパレットだけで見分けると、
        同じロットで候補を替えて置き直したときに「もう積んである」と
        断ってしまう ── 実際に使ったのは置き直したあとのほうです。
        """
        if self.placement is None or not self.placement.placed:
            return ""
        boards = sorted((b.width, b.length) for b in self.placement.placed)
        return "|".join([
            self.presenter.lot_no or "",
            self.board_type,
            f"{self.palette.width}x{self.palette.length}",
            f"{self.product.width}x{self.product.length}",
            ";".join(f"{w}x{l}" for w, l in boards),
        ])

    def usage_refusal(self) -> str:
        """「使用する」が押せない理由。押せるなら空。

        条件は**配置してあること**だけです(現場の指示:
        配置済み かつ ボタン押し)。ロットや製品寸法が無くても、
        置いたものを使ったという事実は記録できます ── 分からない値は
        0で残るので、後から「このぶんは寸法が分からない」と読めます。
        """
        if self.placement is None or not self.placement.placed:
            return "先にボードを配置してください。"
        return ""

    @property
    def usage_done(self) -> bool:
        """いまの配置をもう記録してあるか。"""
        key = self._usage_key()
        return bool(key) and key == self.usage_recorded_key

    def record_usage(self) -> BoardOpResult:
        """配置したボードを「使った」として記録する。

        数える入口は**ここだけ**です。以前は配置図を印刷したときに
        積んでいましたが、確かめるために印刷しても積まれ、印刷せずに
        使えば積まれないので、押した人の意図と一致しませんでした。
        """
        why = self.usage_refusal()
        if why:
            return BoardOpResult(False, why, REFUSE_NO_CANDIDATES)
        if self.usage_done:
            # **断るが、失敗ではない。** すでに望んだ状態になっている
            return BoardOpResult(
                True, "この配置はもう記録してあります(二重には積みません)。")

        from . import board_usage
        sheets = board_usage.record_usage(
            self.presenter.conn, self.placement.placed, self.board_type,
            product=(self.product.width, self.product.length),
            palette=(self.palette.width, self.palette.length),
            lot=self.presenter.lot_no or "")
        self.usage_recorded_key = self._usage_key()
        self.presenter.user_log.log(
            f"[使用実績] {self.board_type} {sheets}枚を記録しました",
            emphasis=True)
        return BoardOpResult(True, f"使用実績に{sheets}枚を記録しました。")

    def patterns(self) -> list[Any]:
        """いまのパレット寸法で登録されている実績(VBA `btnLoadPattern_Click`)。"""
        if not self.palette.is_set:
            return []
        return pattern_service.get_pattern_list(
            self.presenter.conn, self.palette.width, self.palette.length)

    def load_pattern(self, pattern_id: int) -> BoardOpResult:
        """実績パターンの読み込み(VBA `LoadSinglePattern`)。"""
        if not self.palette.is_set:
            return BoardOpResult(False, "先にパレットサイズを適用してください。",
                                 REFUSE_NEEDS_SIZES)
        detail = pattern_service.load_pattern_by_id(
            self.presenter.conn, pattern_id)
        if detail is None:
            return BoardOpResult(False, "そのパターンは見つかりませんでした。",
                                 REFUSE_NOT_FOUND)

        self.palette = svc.Palette(
            width=detail.pallet_width, length=detail.pallet_length,
            overhang_ratio=svc.PALETTE_OVERHANG_RATIO,
            max_width=detail.pallet_width * svc.PALETTE_OVERHANG_RATIO,
            max_length=detail.pallet_length * svc.PALETTE_OVERHANG_RATIO,
        )
        if detail.product_width > 0 and detail.product_length > 0:
            self.product = svc.ProductSize(
                width=detail.product_width, length=detail.product_length)

        # 保存時にタグは失われている。配置側の `GetEffectiveTag` が推測する
        self.selected.lower = [
            svc.SelectedBoard(width=b.width, length=b.length, count=b.count)
            for b in detail.boards_lower]
        self.selected.upper = [
            svc.SelectedBoard(width=b.width, length=b.length, count=b.count)
            for b in detail.boards_upper]
        self.select_result = None
        self.invalidate_placement()
        self._sync_ribbon()
        log.info("実績パターンを読み込みました: No.%s", pattern_id)
        return BoardOpResult(
            True, f"実績パターン No.{pattern_id} を読み込みました")

    # ------------------------------------------------------------------
    # ロット確定 (VBA `SearchAndDisplay` 末尾)
    # ------------------------------------------------------------------
    def apply_lot(self, result: Any, *, product_width: int = 0,
                  product_length: int = 0) -> dict[str, Any]:
        """状態機械に通し、**ボード種別の切り替えまで反映する**。

        順序依存の判断はプレゼンタが持つ(`SelectionPresenter.apply_lot`)。
        ここは「切り替えろ」と言われた種別を実際に選ぶところだけ。
        プレゼンタが自分で切り替えないのは、種別の一覧を持っているのが
        画面/セッション側だから(tkinter版はコンボの values)。
        """
        # **別のロットに移るなら、前のロットの作業は持ち越さない**
        # (VBA `ClearForNewLot`)。`work_context` は同じ入口で自分の分を
        # 捨てているので、ここで捨てないとリボンが「未設定」なのに
        # カードは「設定済」という食い違いが残り、前のロットのパレット
        # 寸法と発注コードのまま新しいロット番号で倉庫送信まで通る
        if self._is_new_lot(result):
            self.clear_for_new_lot()

        types = self.board_types()
        outcome = self.presenter.apply_lot(
            result, product_width=product_width, product_length=product_length,
            available_board_types=types, current_board_type=self.board_type)
        if outcome["protec"].should_switch:
            self.board_type = outcome["protec"].target_board_type
            log.info("プロテックのためボード種別を切り替えました: %s", self.board_type)
        # EX受注ならEXオンリーを自動ON、非EXなら自動OFF
        # (VBA は `chkExOnly` を表示して自動でONにしていた。
        #  tkinter版 `_render_ex_order` も同じ)
        self.ex_only = self.presenter.is_ex_order
        # 1P0113 の乗数は総梱包数で入れ直す(VBA `Load1P0113Materials`)
        self._reset_qty(outcome["mode_change"])
        return outcome

    def _is_new_lot(self, result: Any) -> bool:
        """いま持っているロットと別物か。

        `work_context.lot_no` を見ないのは、**同じ事実を2か所で判断
        しない**ため。切替の入口では `work_context.apply_lot()` が先に
        走って番号を書き換えてしまうので、そちらを見ると常に
        「同じロット」に見える。
        """
        if result is None or not getattr(result, "found", False):
            return False
        current = self.presenter.lot_result
        if current is None:
            return False
        return current.lot.lot_no != result.lot.lot_no

    def clear_for_new_lot(self) -> None:
        """前のロットで決めたことを捨てる(VBA `ClearForNewLot`)。

        **押しっぱなしのモードは消さない。** 疲労度優先・在庫考慮・
        全件表示は利用者が自分で切るまで効き続ける(`work_context.clear()`
        が `fatigue` を残しているのと同じ考え方)。
        強制1P0113 だけは別で、ロット切替で解除されるのが VBA の挙動
        なので、状態機械側(`SelectionPresenter.apply_lot`)が落とす。
        """
        log.info("ロットが変わったので資材選択の作業を捨てます")
        self.clear_sizes()
        self.selected = svc.SelectedBoards()
        self.select_result = None
        self.tiling = None
        # 紙面で直した内容も捨てる。**別のロットの帳票に前のロットの
        # 書き込みが残るのがいちばん困る**(担当者名・期日・台数)
        self.report_edits = {}
        self.selected_angles = []
        self.angle_drawn = False
        self.angle_need_cut = False

    def flags(self) -> dict[str, Any]:
        """一覧の絞り込みに渡す値。

        EXオンリーは**EX受注のときだけ**効く(`list_pallet_sizes` の
        `ex_only_mode = is_ex_order and ex_only`)。押せてしまうと
        効いていないのに効いたつもりになるので、画面側でも同じ条件で
        押せるかどうかを決める。
        """
        return {
            "show_all": self.show_all,
            "ex_only": self.ex_only,
            "is_ex_order": self.presenter.is_ex_order,
            "is_1p1185_mode": self.presenter.mode_1p1185.is_1p1185,
        }


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_session: Optional[SelectionSession] = None
_lock = threading.Lock()


def _log_placed(ulog: Any, placed: list) -> None:
    """置いた結果を選定ログに残す。**経路で形を変えない。**

    現行の「ボード配置」も「候補変更」もここを通す。図がおかしいという
    声が届いたときに読むのはこの行で、書き方が経路ごとに違うと、
    現場から送られてきたログのどこを見ればよいかが毎回変わる。

    座標系はVBA踏襲で X=丈方向 / Y=幅方向。
    """
    for item in placed:
        ulog.log(f"  配置: [{item.board_category or '?'}]"
                 f" {item.width} x {item.length} @ ({item.x}, {item.y})")


def _tiling_note(cand: Optional[tiling.TileCand]) -> str:
    """選定ログに出す候補1件の要約。**決め手が読めるように出す。**"""
    if cand is None:
        return "なし"
    return (f" {cand.board_count}枚 {cand.type_count}種"
            f" カット{cand.cuts}枚 超過{cand.over_w + cand.over_l}mm")


def get_session(conn: sqlite3.Connection) -> SelectionSession:
    """作業中のセッション。無ければ作る。

    選定ログは**プロセスに1つ**のものを使う(`get_user_log`)。
    パレット検索が何を試したかは「選定ログ」画面で読むためのもので、
    セッションごとに別のバッファへ書くと画面に出てこない。
    """
    global _session
    with _lock:
        if _session is None:
            _session = SelectionSession(
                presenter=SelectionPresenter(conn, user_log=get_user_log()))
            log.info("資材選択のセッションを開始しました")
        _session.bind(conn)
        return _session


def reset_session() -> None:
    """テスト用。プロセスに1つという前提を壊さずに作り直す。"""
    global _session
    with _lock:
        _session = None
