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

【段ごとに分けてある】
1つの画面の状態なので**同じオブジェクト**だが、読むときに全部を
たどらなくてよいよう、まとまった段は mixin に分けてある。

    selection_common   断りの種別・区分・上限・`BoardOpResult`
    selection_tiling   候補変更(敷き詰め方式)
    selection_angle    アングルの候補・追加・自動選定・図
    selection_records  実績パターン・ボード使用実績・管理者認証
    ここ               寸法・モード・1P0113・ボード選定・配置・ロット確定

どの段が本体の何を読むかは、それぞれの mixin の説明に書いてある。
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from . import board_scoring
from . import board_selection_algorithm as alg
from . import board_selection_service as svc
from . import location_service, material_service
from . import placement_algorithm as place
from . import tiling_algorithm as tiling
from . import user_log as user_log_mod
from . import user_settings, work_context
from .logging_utils import get_logger
from .presenters.selection import SelectionPresenter
from .selection_common import (  # noqa: F401 - ここから外へも公開する
    BoardOpResult, CATEGORIES, CATEGORY_LOWER, CATEGORY_UPPER, LIST_ALL,
    LIST_DIRECT, LIST_PRODUCT, LIST_PRODUCT_LIVE, QTY_MAX, QTY_MIN,
    REFUSE_BAD_INPUT, REFUSE_DENIED, REFUSE_FAILED, REFUSE_NEEDS_SIZES,
    REFUSE_NOT_FOUND, REFUSE_NOT_LISTED, REFUSE_NO_CANDIDATES, TOGGLES,
    log_placed)
from .selection_angle import AngleMixin
from .pattern_store import METHOD_NORMAL
from .selection_records import RecordsMixin, RestoredFacts
from .selection_tiling import (  # noqa: F401 - ここから外へも公開する
    TilingMixin, TilingState, tiling_note)
from .user_log import get_user_log

log = get_logger("selection_session")


@dataclass
class SelectionSession(TilingMixin, AngleMixin, RecordsMixin):
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

    # --- 実績保存 (VBA `mPlacementMethod` / `mPlacedSignature`) ---
    # 配置方式(通常 / 別案A〜C)と、**配置した時点の内容**。保存するときに
    # 見比べ、配置のあとでパレット・製品・ボードが変わっていたら止める
    placement_method: str = ""
    placed_signature: str = ""
    # 製品を回転させて載せたか(VBA `mProductRotated`)。実績に残す
    product_rotated: bool = False
    # 実績から戻した「選定の結果」(狭幅・プロテック確定値・カット)。
    # 読込は計算し直さないので、自動選定の結果(`select_result`)の代わりに
    # これを見る。**`select_result` を入れ替える・捨てるときは一緒に捨てる**
    restored: Optional[RestoredFacts] = None

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
        self.product_rotated = bool(rotated)
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
        # 「EXまで表示」と「EXオンリー」は同時に立たない(VBA `chkShowAll_Click`
        # / `chkExOnly_Click`)。絞り込みは `ex_only` を先に見るので、
        # 両方ONだと「EXまで表示」は**押せるのに何も起きない**。
        # 効いていない印を画面に残さない
        if now and name in ("show_all", "ex_only"):
            other = "ex_only" if name == "show_all" else "show_all"
            setattr(self, other, False)
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
        なるため。**ロットを引き直すたびに作り直す**(`_refresh_stock_map`)
        ので、押しっぱなしでも古い在庫のまま点が付き続けることはない。
        """
        if self.stock_map is None:
            self.stock_map = material_service.build_board_stock_map(
                self.presenter.conn)
            log.info("在庫考慮ON: %s件", len(self.stock_map))
        else:
            self.stock_map = None
            log.info("在庫考慮OFF")
        return self.stock_map is not None

    def _refresh_stock_map(self) -> None:
        """在庫マップを引き直す(VBA `mpMain_Change` の `Set mStockMap = Nothing`)。

        **ONのときだけ作り直す。** マップの有無がそのままON/OFFなので、
        捨てるだけだと在庫考慮が黙って外れてしまう。

        原文は資材選択のページに入るたびに捨てて、次に使うときに
        作り直していた。こちらは画面を行き来しても同じセッションが
        残り続ける ── 1日開けっぱなしにすると、朝の在庫のまま点が
        付き続ける。ロットを引き直す所が、原文のページ切り替えに
        いちばん近い区切りになる。
        """
        if self.stock_map is None:
            return
        self.stock_map = material_service.build_board_stock_map(
            self.presenter.conn)
        log.info("在庫マップを引き直しました: %s件", len(self.stock_map))

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
                # **どこから測ったかを残す。** 疲労度優先の点はすべて
                # 拠点からの距離で決まるので、あとから「なぜこの選定に
                # なったか」を追うときに拠点が要る。既定を当てている
                # だけのときはそう書く(`get_position` は空を返さないので、
                # 名前だけ出すと人が選んだように読める)
                base_point = location_service.current_base_point()
                ulog.log(f"  疲労度マップ: 拠点 {base_point.name}"
                         + ("" if base_point.chosen else "(未登録のため既定)"))

        try:
            # **却下と補正の理由をそのまま流す**(`user_log.bridge_from`)。
            # 現場が要るのは決まったことではなく、そこへ至った経緯
            # `packaging_tool.board_selection` は段ごとのロガーの**親**。
            # 親に付けると子(下用・上用・補填・プロテック・狭幅)の行も
            # 受け取れるので、段を増やしてもここは増やさなくてよい
            with user_log_mod.bridge_from(
                    ulog, "packaging_tool.board_selection_algorithm",
                    "packaging_tool.board_selection",
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
        self.restored = None
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
        self.restored = None
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

        # 自動選定の結果、または実績から戻した結果(`narrow_flags`)
        narrow_lower, narrow_upper = self.narrow_flags()
        protec_result = self.fixed_protec()
        if protec_result is None and self.presenter.protec.is_protec:
            # 手で増減したあとは `select_result`(ProtecCutResultを含む)を
            # 捨ててあるので、配置直前にここで後付けする(VBA
            # `ApplyProtecRulesToLowerList`)。これをしないと、
            # プロテックのはずなのに「唯一の正解」が無いまま配置に入り、
            # 配置段階が独自に製品幅厳守を判定し直して静かに配置漏れを
            # 起こす
            protec_result = alg.apply_protec_rules_to_lower_list(
                self.selected.lower, self.product, self.palette,
                is_1p1216=self.presenter.protec.is_1p1216)
        if self.select_result is None and self.restored is None:
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
        log_placed(ulog, placed)

        # **置けなかったものは黙って飛ばさない。**
        # 手で「上用へ追加」したのに図に出てこず、ログにも何も残らないと、
        # 押した人には「このボタンは効かない」としか見えない
        # (現場の指摘:「追加しても配置すらしない」)
        notes = []
        for miss in self.placement.unplaced:
            ulog.log(f"  置けませんでした: {miss.label()}", emphasis=True)
            log.info("配置できず: %s", miss.label())
            notes.append(miss.label())

        ulog.log(f"ボード配置 完了: {len(placed)}枚", emphasis=True)
        # 実績保存用: 配置方式と配置時点の内容を控える(VBA `AutoPlaceBoards`)
        self.record_placement(METHOD_NORMAL)
        if notes and not placed:
            # 1枚も置けていないなら、それは成功ではない
            return BoardOpResult(
                False, "ボードを配置できませんでした。",
                REFUSE_NOT_FOUND, notes=notes)
        return BoardOpResult(True, f"ボードを配置しました({len(placed)}枚)",
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

    def clear_boards(self) -> BoardOpResult:
        """「クリア」(VBA `btnClearAll_Click` / tkinter `do_clear_selection`)。

        消すのは**ボードだけ**。選択アングルは残す(tkinter版と同じ)。
        アングルには専用の「削除」があり、そちらで外す。並行運用の
        あいだに片方だけ違う消え方をすると、どちらが正しいのか
        現場には確かめようがない。
        """
        self.selected = svc.SelectedBoards()
        self.select_result = None
        self.restored = None
        # 「候補変更」の回り位置も戻す。消したあとに押したら**Aから**
        self.tiling = None
        self.invalidate_placement()
        return BoardOpResult(True, "選定したボードをクリアしました")

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
        # 同じロットを引き直したときも作り直す。**引き直しは「ここから
        # やり直す」の合図**で、そのときに古い在庫で点が付いていると、
        # なぜ前と違う(あるいは同じ)結果なのかを説明できない
        self._refresh_stock_map()

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
        self.restored = None
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
