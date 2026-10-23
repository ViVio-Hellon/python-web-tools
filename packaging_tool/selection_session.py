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
from . import special_packaging as spk
from . import placement_algorithm as place
from . import tiling_algorithm as tiling
from . import user_log as user_log_mod
from . import work_context
from .logging_utils import get_logger
from .presenters.selection import SelectionPresenter
from .selection_common import (  # noqa: F401 - ここから外へも公開する
    BoardOpResult, CATEGORIES, CATEGORY_LOWER, CATEGORY_UPPER, LIST_ALL,
    LIST_DIRECT, LIST_PRODUCT, LIST_PRODUCT_LIVE, QTY_MAX, QTY_MIN,
    REFUSE_BAD_INPUT, REFUSE_DENIED, REFUSE_FAILED, REFUSE_NEEDS_SIZES,
    REFUSE_NOT_FOUND, REFUSE_NOT_LISTED, REFUSE_NO_CANDIDATES, TOGGLES,
    log_placed)
from .selection_angle import AngleMixin
from .pallet_common import KIND_WIDTH2
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
    # 2山の向き(VBA `m_2YamaDir`)。"幅" / "丈" / ""(未決定=1山として扱う)。
    # パレット検索が決め、「セット」がこの向きで製品サイズを2倍する。
    # 2山積の切り替え・パレット検索のやり直し・ロット切替・実績読込で忘れる
    stack_dir: str = ""
    # 確定した製品サイズ(`product`)のうち、2山で2倍した辺("width" / "length")
    # と、その断り書き。**入力欄へ書き戻すときは1山分に戻す** ── 2倍のまま
    # 戻すと、次のパレット検索で4倍になる
    product_stack_axis: str = ""
    product_stack_note: str = ""
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
    # パレット 幅・丈の入力中の値(`LIST_PRODUCT_LIVE` 用)。**製品 幅・丈が空のときだけ**
    # パレット自身の寸法の前後50mmで探す(`live_search`)。これも確定ではない
    live_pallet_width: str = ""
    live_pallet_length: str = ""

    # 一覧で選んでいる行(VBA `pal_tree.selection()`)。
    # **発注コードと単位はここにしか無い** ── 幅と丈だけでは決まらず、
    # 同じ寸法でも記号違いで別のコードになる。倉庫送信と帳票が見る
    pallet_row: Optional[svc.PalletSizeRow] = None
    # 「蓋選択」(EX・上下共用のとき)で選んだ蓋(単位が枚の行)。**倉庫連携にしか効かない**(ボード選定は台で決まる)。
    # パレット(台)と蓋の両方があれば、1P0113 の角材と松板のように2行まとめて倉庫へ送る
    lid_row: Optional[svc.PalletSizeRow] = None
    # マスタ(表ごと)の変わった回数を、引き直したときに覚えておく(VER4.8.95〜)。
    # 変わっていれば `bind` でいまのマスタから引き直す(`_follow_master`)
    master_revs_seen: dict[str, int] = field(default_factory=dict)
    # マスタが変わって選んだパレット・蓋を引き直した/外したときの一言(画面がトーストで出す)。
    # 選び直したら消える
    master_note: str = ""

    # --- ボード (VBA `cboBoardType` / `lstSelectedBoards*`) ---
    board_type: str = ""
    # 出ているボードが疲労度優先で選ばれたか(`auto_select_boards` が決める)
    selected_with_fatigue: bool = False
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
        self._follow_master()

    # --- マスタが変わったら(VER4.8.95〜) --------------------------------------
    def _master_changed(self, table: str) -> bool:
        """その表が、前に見たときから変わったか(初めて見たときは覚えるだけで False)。"""
        from . import master_state
        rev = master_state.table_rev(table)
        seen = self.master_revs_seen.get(table)
        self.master_revs_seen[table] = rev
        return seen is not None and rev != seen

    def _follow_master(self) -> None:
        """マスタが変わっていたら、覚えている写し・判定をいまのマスタで引き直す。

        現場の声:「マスタを変更後にその内容が反映されていない動作描写をするのはおかしい」。
        変わったことを一言にまとめて `master_note` に残し(画面がトーストで出す)、倉庫連携の下書きも作り直す。
        """
        from . import config
        notes: list[str] = []
        if self._master_changed(config.TBL_PALLET_MASTER):
            notes += self._follow_pallet_master()
        if self._master_changed(config.TBL_MATSUZAI_KAKUZAI):
            notes += self._follow_1p0113_master()
        from . import import_specs
        if self._master_changed(import_specs.PROTEC_RULE_TABLE):
            notes += self._follow_protec_master()
        if self._master_changed(config.TBL_HOGOZAI):
            notes += self._follow_hosozai_master()
        if not notes:
            return
        self.master_note = " / ".join(notes)
        self.presenter.user_log.log("[マスタ] " + self.master_note, emphasis=True)
        self._rebuild_pending_orders()

    def _follow_protec_master(self) -> list[str]:
        """プロテック条件が変わったら、開いているロットでプロテックかを判定し直す(マスタ反映もれ #4。VER4.8.98)。

        判定はロットを開いたときだけで、条件を足しても直しても開き直すまで古い判定のまま、ボード種別・
        ボード選定・配置・切断依頼が動いていた。判定が変わったら、ボード種別を切り替え、前の判定で選んだ
        ボード・配置を外す(プロテック強制を押したときと同じ)。
        """
        presenter = self.presenter
        result = presenter.lot_result
        if result is None or not getattr(result, "found", False):
            return []
        was = presenter.protec.is_protec
        outcome = presenter.check_and_set_protec_mode(
            result.odr.packaging_spec, order_size=presenter.order_size(),
            available_board_types=self.board_types(), current_board_type=self.board_type)
        if presenter.protec.is_protec == was:
            return []
        if outcome.should_switch:
            self.board_type = outcome.target_board_type
        note = ("プロテック条件が変わったので、このロットは"
                + ("プロテックになりました" if presenter.protec.is_protec else "プロテックではなくなりました")
                + f"(ボード種別 {self.board_type})")
        if self._drop_boards_for_mode():
            note += "。前の判定で選んだボード・配置を外しました。選定し直してください"
        return [note]

    def _follow_hosozai_master(self) -> list[str]:
        """梱包保護材が変わったら、開いているロットの使用保護材を引き直す(マスタ反映もれ #5。VER4.8.99)。

        使用保護材はロットを開いたときだけ引いていたので、梱包保護材を直しても開き直すまで、アングルの
        出す/出さない・上下共用・パレット一覧の台/枚・蓋選択が古いままだった。
        """
        presenter = self.presenter
        result = presenter.lot_result
        if result is None or not getattr(result, "found", False):
            return []
        before = presenter.last_hosozai
        after = presenter.lookup_hosozai(result)
        if after == before:
            return []
        note = (f"梱包保護材が変わったので、このロットの使用保護材を {before or 'なし'} → {after or 'なし'} にしました")
        note += self._apply_hosozai_change(after)
        return [note]

    def _apply_hosozai_change(self, hosozai: str) -> str:
        """使用保護材を入れ替え、それで前提が変わったものを外す。外したことの一言を返す。"""
        presenter = self.presenter
        was_shared, was_angle = presenter.is_shared_board_mode, presenter.show_angle
        presenter.apply_hosozai(hosozai)
        extra = ""
        # 上下共用かどうかが変わると、上用の選び方が変わる(前の前提で選んだボード・配置は使えない)
        if presenter.is_shared_board_mode != was_shared and self._drop_boards_for_mode():
            extra += "。上下共用が変わったので、選んだボード・配置を外しました(選定し直してください)"
        # アングルを使わなくなったら、選んだアングルは外す
        if was_angle and not presenter.show_angle and self.selected_angles:
            self.selected_angles = []
            self.invalidate_angle_plan()
            extra += "。アングルを使わなくなったので、選んだアングルを外しました"
        # 蓋は蓋を選べるモード(EX・上下共用)のときだけ
        if not presenter.lid_mode and self.lid_row is not None:
            self.lid_row = None
            extra += "。蓋を選べなくなったので、選んだ蓋を外しました"
        return extra

    def _drop_boards_for_mode(self) -> bool:
        """モード(プロテック・上下共用など)が変わったので、前のモードで選んだボード・配置を外す。外したら True。"""
        if not (self.selected.lower or self.selected.upper):
            return False
        self.clear_boards()
        return True

    def _follow_1p0113_master(self) -> list[str]:
        """松板角材が変わったら、1P0113 の角材・松板を引き直す(マスタ反映もれ #3。VER4.8.97)。

        資材はロットを開いた・セットしたときに引いた写しで、松板角材の発注コード・丈の範囲を直しても
        倉庫送信は古いコードのまま、足した行があっても「資材マスタに該当がない」で止まり続けていた。
        製品サイズは引いたときのまま(乗数も変えない)。
        """
        presenter = self.presenter
        if not presenter.mode_1p0113:
            return []
        before = presenter.materials_1p0113
        try:
            presenter.load_1p0113_materials(before.product_width, before.product_length)
        except sqlite3.Error as exc:
            log.warning("1P0113 の資材を引き直せませんでした: %s", exc)
            return []
        after = presenter.materials_1p0113
        notes = []
        for label, old, new in (("角材", before.kakuzai, after.kakuzai), ("松板", before.matsuita, after.matsuita)):
            if (old.code, old.unit, old.is_ok, old.info) != (new.code, new.unit, new.is_ok, new.info):
                notes.append(f"{label}を松板角材のいまの中身にしました"
                             f"(コード {old.code or '---'}→{new.code or '---'}"
                             + ("" if new.is_ok else "。マスタに該当がありません") + ")")
        return notes

    def _follow_pallet_master(self) -> list[str]:
        """パレットマスタが変わっていたら、選んだパレット・蓋を**いまのマスタで**引き直す。

        現場の声:「マスタを変更後にその内容が反映されていない動作描写をするのはおかしい」。
        選んだ行は選んだときの写しで、マスタで発注コード・単位・脚数を直しても、倉庫送信・
        Lot印刷・アングル本数が古いままだった(選び直すまで)。行が無くなっていれば外して
        そう言う(倉庫送信は「一覧から選んでください」で止まる)。倉庫連携の下書きも作り直す。
        """
        from .pallet_list import refind_pallet_row
        notes: list[str] = []
        for attr, label in (("pallet_row", "パレット"), ("lid_row", "蓋")):
            old = getattr(self, attr)
            if old is None:
                continue
            try:
                fresh = refind_pallet_row(self.presenter.conn, old)
            except sqlite3.Error as exc:
                log.warning("選んだ%sをマスタから引き直せませんでした: %s", label, exc)
                continue
            setattr(self, attr, fresh)
            if fresh is None:
                notes.append(f"選んでいた{label}({old.width}×{old.length} {old.symbol} コード {old.code or '---'})が"
                             "パレットマスタに見つからなくなったので外しました。一覧から選び直してください")
            elif _row_differs(old, fresh):
                notes.append(f"選んでいた{label}をパレットマスタのいまの中身にしました"
                             f"({_row_diff_text(old, fresh)})")
        return notes

    def _rebuild_pending_orders(self) -> None:
        """倉庫連携の下書き(パレットを決めたときに作ったもの)を、いまの行で作り直す。送れなければ捨てる。"""
        from .presenters import outputs
        context = work_context.get_context()
        if not context.peek_pending_orders():
            return
        if outputs.send_refusal(self) is None:
            context.set_pending_orders(outputs.build_orders(self))
        else:
            context.drop_pending_orders()

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
            self.forget_product_stack()
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
        self.forget_product_stack()
        self.list_mode = LIST_ALL
        self.direct_width = self.direct_length = ""
        self.live_product_width = self.live_product_length = ""
        self.live_pallet_width = self.live_pallet_length = ""
        self.pallet_row = None
        self.lid_row = None
        self.invalidate_placement()
        self._sync_ribbon()

    def forget_pallet(self, reason: str) -> None:
        """決めたパレット・蓋を外す(製品サイズの**入力中の値と一覧の絞り込みは残す**)。

        ロット検索でオーダーを選び直したとき(`app/routes/lot._apply_picked_order`)。
        同じロットでもオーダーごとに資材が違うことがあるので、前のオーダーで決めたパレットのまま
        次のオーダーを発注させない(現場の声:「オーダー選びなおしたらパレットを一律外すほうが現状に合う」)。
        製品サイズの確定はパレットに載ることを確かめたものなので、パレットと一緒に外す。
        """
        if self.palette.is_set or self.pallet_row is not None or self.lid_row is not None:
            self.presenter.user_log.log(f"[パレット] {reason}ので、決めたパレット・蓋を外しました")
        self.palette = svc.Palette()
        self.product = svc.ProductSize()
        self.forget_product_stack()
        self.pallet_row = None
        self.lid_row = None
        self.invalidate_placement()
        self._sync_ribbon()

    def clear_special_modes(self) -> list[str]:
        """「やり直す」で、ロットから立った**特殊モード**を外す。外したモードの名前を返す。

        現場の声:「一度でも【1P1185モード タイトサイズ限定表示】のような特殊モードが発火すると
        やり直すでは消せない。ロット展開で消えるのは当たり前として、やり直すでも消せるように」。
        外すのは包装仕様NOで立つ3つ(1P1185・プロテック・1P0113。強制1P0113・強制プロテック も)。
        **EX受注と上下共用は外さない** ── どちらも注文そのものの性質(EX の発注の仕方・
        保護材で決まる組み方)で、外すと倉庫送信や上用の組み方を取り違える。
        もう一度ロットを展開すれば、包装仕様NOから判定し直して立つ。
        """
        presenter = self.presenter
        cleared: list[str] = []
        if presenter.mode_1p1185.is_1p1185:
            presenter.mode_1p1185 = spk.Mode1P1185State()
            cleared.append("1P1185")
        if presenter.protec.is_protec or presenter.force_protec:
            presenter.force_protec = False
            outcome = presenter.check_and_set_protec_mode(
                "", available_board_types=self.board_types(),
                current_board_type=self.board_type)
            if outcome.should_switch:
                self.board_type = outcome.target_board_type
            cleared.append("プロテック")
        if presenter.mode_1p0113 or presenter.force_1p0113:
            presenter.force_1p0113 = False
            self._reset_qty(presenter.apply_1p0113_mode(""))
            cleared.append(spk.HOSOSIYO_1P0113)
        if cleared:
            log.info("やり直す: 特殊モードを外しました: %s", "・".join(cleared))
            presenter.user_log.log(f"[やり直す] 特殊モードを外しました: {'・'.join(cleared)}"
                                   "(ロットを展開し直すと、包装仕様NOから判定し直します)",
                                   emphasis=True)
        self._sync_ribbon()
        return cleared

    def start_over(self) -> list[str]:
        """「やり直す」: **ロットの選択ごと外して**、何も選んでいない状態に戻す。外したモードの名前を返す。

        現場の声:「ロットは選択したままで寸法を消すのはおかしい」「やり直す時点で選択したロットも外せばよくない？」。
        以前はパレット・製品サイズ(と特殊モード)だけを消し、ロットは選んだままだった
        (VBA `btnClearPalProd_Click` もパレット・製品だけを消す。**ここは VBA と違う**)。

        外すもの: ロット・パレット・製品サイズ・選んだボードとアングル・帳票で直した内容(ロットを
        替えたときと同じ `clear_for_new_lot`)と、ロットから立ったモード全部(1P1185・プロテック・
        1P0113・EX受注・上下共用)。EX受注と上下共用も、ロットを外せば根拠が無くなるので外す。
        **押しっぱなしのモード(疲労度優先・在庫考慮・EXまで表示)は残す**(ロットを替えたときと同じ)。
        """
        cleared = self.clear_special_modes()
        presenter = self.presenter
        if presenter.is_ex_order:
            cleared.append("EX")
        if presenter.is_shared_board_mode:              # プロテックはもう外れている
            cleared.append("上下共用")
        self.clear_for_new_lot()
        presenter.lot_result = None
        presenter.lot_no = ""
        presenter.manufactured_thickness = None
        presenter.set_ex_order(False)
        presenter.last_hosozai = ""
        self.ex_only = False
        log.info("やり直す: ロットの選択ごと外しました(%s)", "・".join(cleared) or "モードなし")
        self._sync_ribbon()
        return cleared

    def pick_pallet_row(self, width: int, length: int, symbol: str,
                        row_id: Optional[int] = None) -> BoardOpResult:
        """一覧の行を選ぶ(VBA `_on_pallet_row_select`)。

        寸法を入力欄へ写すだけでなく**行そのものを覚える**。
        発注コードと単位は行にしか無く、同じ寸法でも記号違いで
        別のコードになるので、倉庫送信と帳票はこれを見る。
        """
        # **押した行そのもの**(管理番号)で引く。同じ寸法・記号でも発注コードの
        # 違う行があり、寸法だけで引くと一覧に出ていない行のコードで送っていた
        row = svc.find_pallet_row(self.presenter.conn, width, length, symbol,
                                  row_id=row_id, last_hosozai=self.presenter.last_hosozai,
                                  is_ex_order=self.presenter.is_ex_order)
        if row is None:
            return BoardOpResult(
                False, f"{width}×{length} {symbol} は一覧にありません。",
                REFUSE_NOT_LISTED)
        self.pallet_row = row
        self.master_note = ""
        return BoardOpResult(True, "")

    # --- EX: パレット(台)と蓋(枚) ------------------------------------
    def lid_pallet_refusal(self, width: int, length: int, symbol: str,
                          row_id: Optional[int] = None) -> Optional[BoardOpResult]:
        """蓋を選べるモード(EX・上下共用。`SelectionPresenter.lid_mode`)では、「パレット決定」で
        受け付けるのは**単位が台の行だけ**。枚の行は「蓋選択」で選ぶ。

        現場の声:「EXモードの時はパレット選択と蓋選択が押せるようにし、パレット選択で台を受け付ける、
        蓋選択で枚を受け付ける」「上下共用モードの時も蓋が有ることが前提」。
        寸法を変える前(`apply_pallet` の前)に確かめる。
        """
        if not self.presenter.lid_mode:
            return None
        row = svc.find_pallet_row(self.presenter.conn, width, length, symbol,
                                  row_id=row_id, last_hosozai=self.presenter.last_hosozai,
                                  is_ex_order=True)
        if row is None or _is_unit(row, svc.UNIT_DAI):
            return None
        hint = "「蓋選択」で選んでください。" if _is_unit(row, svc.UNIT_MAI) else "台の行を選んでください。"
        return BoardOpResult(
            False, f"{self._lid_mode_name()}のときの「パレット決定」は単位が台の行だけです(選んだ行は {row.unit or '---'})。{hint}",
            REFUSE_NOT_LISTED)

    def pick_lid(self, width: int, length: int, symbol: str,
                    row_id: Optional[int] = None) -> BoardOpResult:
        """「蓋選択」(EX・上下共用のとき)。**単位が枚の行だけ**を受け付ける。

        蓋は**倉庫連携にしか効かない**(パレットの寸法・製品サイズ・ボード選定は変えない)。
        パレット(台)と蓋の両方が決まっていれば、倉庫連携へ2行まとめて渡す(`outputs.lid_row`)。
        """
        if not self.presenter.lid_mode:
            return BoardOpResult(False, "蓋選択はEX受注か上下共用モードのときだけ使えます。",
                                 REFUSE_NOT_LISTED)
        row = svc.find_pallet_row(self.presenter.conn, width, length, symbol,
                                  row_id=row_id, last_hosozai=self.presenter.last_hosozai,
                                  is_ex_order=True)
        if row is None:
            return BoardOpResult(False, f"{width}×{length} {symbol} は一覧にありません。",
                                 REFUSE_NOT_LISTED)
        if not _is_unit(row, svc.UNIT_MAI):
            return BoardOpResult(
                False, f"「蓋選択」は単位が枚の行だけです(選んだ行は {row.unit or '---'})。"
                       "台の行は「パレット決定」で選んでください。", REFUSE_NOT_LISTED)
        self.lid_row = row
        self.master_note = ""
        self.presenter.user_log.log(
            f"[{self._lid_mode_name()}] 蓋: {row.width} x {row.length} {row.symbol} コード {row.code}",
            emphasis=True)
        return BoardOpResult(True, f"蓋 {row.width}×{row.length}(コード {row.code or '---'})にしました。"
                                   "倉庫連携へパレット(台)と2行まとめて渡します。")

    def _lid_mode_name(self) -> str:
        return "EX" if self.presenter.is_ex_order else "上下共用"

    def drop_lid(self) -> BoardOpResult:
        """「蓋を外す」。倉庫連携へはパレット(台)の1行だけを渡す。"""
        if self.lid_row is not None:
            self.presenter.user_log.log(f"[{self._lid_mode_name()}] 蓋を外しました")
        self.lid_row = None
        return BoardOpResult(True, "蓋を外しました(倉庫連携へはパレットの1行だけを渡します)。")

    def decide_stack_for_row(self, width: int, length: int, symbol: str,
                             product_width: int, product_length: int) -> bool:
        """一覧から選んだ行で、2山の向き(`stack_dir`)を決める。戻りは「回転して当たったか」。

        VBA は「パレット検索」が向きを決める。Web版の画面には検索ボタンが無く、
        **製品サイズで絞った一覧から行を選ぶ**のが検索の代わりなので、その行が
        どう当たったか(`list_pallets_for_product` の幅2山 / 丈2山・回転)を
        向きとして覚える。2山積でない・製品サイズが無い・一覧で当たらない
        ときは向きを忘れる(1山として扱う)。
        """
        self.forget_stack_dir("パレットを選び直した")
        if not (self.two_stack and product_width > 0 and product_length > 0):
            return False
        rows = svc.list_pallets_for_product(
            self.presenter.conn, product_width=product_width,
            product_length=product_length, two_stack=True,
            last_hosozai=self.presenter.last_hosozai,
            manufactured_thickness=self.presenter.manufactured_thickness,
            **self.flags())
        hit = next((r for r in rows if (r.width, r.length) == (width, length)
                    and (r.symbol or "").strip() == (symbol or "").strip()), None)
        if hit is None or not hit.two_stack:
            return False
        self.stack_dir = "幅" if hit.two_stack == KIND_WIDTH2 else "丈"
        self.presenter.user_log.log(
            f"2山の向き: {self.stack_dir}方向（{hit.two_stack}"
            + ("・回転" if hit.rotated else "")
            + f"で当たった行。製品サイズの{self.stack_dir}を2倍して扱います）",
            emphasis=True)
        return bool(hit.rotated)

    # --- 製品サイズ ---------------------------------------------------
    def apply_product(self, width_text: str, length_text: str, *,
                      searched_rotated: bool = False) -> tuple[svc.ApplyResult, bool]:
        """VBA `btnApplyProductSize_Click`。戻りは (結果, 回転したか)。

        2山積で向きが決まっていれば、その向きを2倍して確定する
        (`svc.apply_product_size`)。`searched_rotated` はパレット検索が
        製品を回転させて決めたこと ── VBA は回転Passで決まると入力欄の
        幅・丈を入れ替えてから「セット」する。2山は回転後の寸法に対して
        2倍しないと検索したときの大きさと合わないので、2山のときは
        先に入れ替える。
        """
        pre_rotated = bool(searched_rotated and self.two_stack and self.stack_dir)
        if pre_rotated:
            width_text, length_text = length_text, width_text
        result, product, rotated = svc.apply_product_size(
            width_text, length_text, self.palette,
            two_stack=self.two_stack, stack_dir=self.stack_dir)
        if not result.ok:
            self.presenter.user_log.log(
                f"[製品サイズ確定] 断りました: {result.message}")
            return result, False
        self.product = product
        # 2倍した辺。収まる向きへ入れ替えたら(`fit_rotated`)、2倍した辺も入れ替わる
        fit_rotated = bool(rotated)
        axis = ""
        if self.two_stack and self.stack_dir in ("幅", "丈"):
            axis = "width" if self.stack_dir == "幅" else "length"
            if fit_rotated:
                axis = "length" if axis == "width" else "width"
        self.product_stack_axis = axis
        rotated = fit_rotated != pre_rotated
        self.product_rotated = rotated
        self.product_stack_note = (svc.stack_note(
            self.two_stack, self.stack_dir,
            getattr(product, axis) // 2 if axis else 0))
        # 回転したかどうかは**現物の載せ方が変わる**ので必ず残す
        self.presenter.user_log.log(
            f"[製品サイズ確定] {product.width} x {product.length}"
            + ("(製品を回転させました)" if rotated else "")
            + self.product_stack_note, emphasis=True)
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
            # (2山は1山分に戻して ── 入力欄は1山分のまま)
            single = self.product_single
            context.product_width = single.width
            context.product_length = single.length

    @property
    def product_single(self) -> svc.ProductSize:
        """確定した製品サイズを**1山分**で(入力欄へ戻す値)。2山でなければ `product` そのもの。"""
        if self.product_stack_axis == "width":
            return svc.ProductSize(self.product.width // 2, self.product.length)
        if self.product_stack_axis == "length":
            return svc.ProductSize(self.product.width, self.product.length // 2)
        return self.product

    def forget_product_stack(self) -> None:
        """確定した製品サイズの2山の印を外す(製品サイズを捨てた・入れ直したとき)。"""
        self.product_stack_axis = ""
        self.product_stack_note = ""

    def forget_stack_dir(self, why: str) -> None:
        """2山の向きを忘れる(VBA `m_2YamaDir = ""`)。"""
        if self.stack_dir:
            log.debug("2山の向き[%s]を忘れます: %s", self.stack_dir, why)
        self.stack_dir = ""

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
            # 切り替えたら2山の向きは決め直し(パレット検索をやり直してもらう。
            # VBA `lblAutoToggle_Click`)
            self.forget_stack_dir("2山積を切り替えた")
            self.presenter.user_log.log(
                f"[2山積] {'ON' if now else 'OFF'}"
                "(2山の向きはパレット検索で決め直します)", emphasis=True)
        if name == "fatigue":
            # VBA `btnFatigueSelect_Click`。以降の**ボード選定とアングル
            # 自動選定の両方**がこの値を見るので、押したことを記録に残す。
            # 「なぜこの選定結果になったか」は後から選定ログでしか辿れない
            self.presenter.user_log.log(
                f"[疲労度優先] {'ON' if now else 'OFF'}", emphasis=True)
            # **出ている選定は選び直さない**(VBAも同じ)。ボタンの字は今の設定に
            # 変わるので、出ている選定がどちらで選ばれたかと食い違って見える。
            # 食い違うときだけ、そう言う(VER4.8.62)
            if (self.selected.lower or self.selected.upper) and \
                    self.selected_with_fatigue != now:
                self.presenter.user_log.log(
                    f"  ※出ているボードは疲労度{'ON' if self.selected_with_fatigue else 'OFF'}"
                    "で選んだものです。新しい設定で選ぶには、もう一度ボード選定を押してください")
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

    def toggle_force_protec(self) -> BoardOpResult:
        """「プロテック 強制」(1P0113 強制と同じく、**ロットを切り替えると解除**)。

        ボード種別がプロテックボードに切り替わり、ボード選定・配置・切断依頼が
        プロテックの挙動になる。**選定済みのボードと配置は外す** ── 前のモードの
        前提(上用の許容・上下共用)で選んだもので、そのまま配置・発注に進むと食い違う。
        """
        presenter = self.presenter
        had = bool(self.selected.lower or self.selected.upper)
        outcome = presenter.toggle_force_protec(
            available_board_types=self.board_types(), current_board_type=self.board_type)
        if outcome.should_switch:
            self.board_type = outcome.target_board_type
            log.info("プロテック強制のためボード種別を切り替えました: %s", self.board_type)
        if had:
            self.clear_boards()
            presenter.user_log.log("[プロテック強制] 選定したボードを外しました(選定し直してください)")
        now = presenter.force_protec
        message = f"プロテック強制: {'ON' if now else 'OFF'}"
        if not now and presenter.protec.is_protec:
            message += "(包装仕様NOの条件でプロテックのままです)"
        if outcome.missing_from_master:
            message += f"(ボード種別「{outcome.target_board_type}」がマスタにありません)"
        if had:
            message += "。選定したボードを外しました。選定し直してください"
        return BoardOpResult(True, message)

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
        base_point = location_service.current_base_point()
        if self.fatigue and not base_point.known:
            # **原点が配置図に無ければ距離が1つも測れない。** 黙って続けると、
            # 距離ぬきの点で「疲労度優先」を名乗る選定になる(棚検索は同じとき
            # 断っている)。通常選定に倒して、そう言う(VER4.8.62)
            notes.append(f"拠点「{base_point.name}」が棚の配置図に無いため距離を測れません。"
                         "通常選定で実行しました(設定画面の「動作」で拠点を選び直してください)。")
            ulog.log(f"  ※拠点 {base_point.name} が配置図に無いため、通常選定に切り替えました")
        elif self.fatigue:
            fatigue_lower, fatigue_upper = board_scoring.build_selection_fatigue_maps(
                self.presenter.conn, available, base_point.name,
                board_type=self.board_type,
                pallet_width=self.palette.width, pallet_length=self.palette.length,
                product_width=self.product.width, product_length=self.product.length,
                stock_map=self.stock_map)
            if fatigue_lower is not None:
                self._log_skipped_shelves(available)
            if fatigue_lower is None:
                notes.append("疲労度マップを取得できなかったため通常選定で実行しました。")
                ulog.log("  ※疲労度マップを取得できず、通常選定に切り替えました")
            else:
                # **どこから測ったかを残す。** 疲労度優先の点はすべて
                # 拠点からの距離で決まるので、あとから「なぜこの選定に
                # なったか」を追うときに拠点が要る。既定を当てている
                # だけのときはそう書く(`get_position` は空を返さないので、
                # 名前だけ出すと人が選んだように読める)
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
                # 在庫考慮(在庫薄を先に外す)と疲労度優先(通常と疲労度の選定を、
                # 在庫薄の枚数 → 切る回数 → 合計点 で比べる)の組み合わせは選定側が決める
                # (VER4.8.67)。マップが無ければ疲労度モードで走らせない(VER4.8.62)。
                # 包装仕様NOで決まったモードを選定へ渡す。プロテックと上下共用は
                # 「上用=下用と同サイズ」を強制する分岐に効く(プレゼンタが知っている)
                picked = alg.select_boards(
                    available, self.palette, self.product,
                    fatigue_map_lower=fatigue_lower, fatigue_map_upper=fatigue_upper,
                    stock_aware=self.stock_aware, **self.presenter.selection_flags())
                result = picked.result
                choice = picked.choice
        except Exception as exc:                      # noqa: BLE001 - 画面に出して継続
            log.exception("自動選定エラー")
            ulog.log(f"  → 自動選定エラー: {exc}", emphasis=True)
            return BoardOpResult(False, f"自動選定エラー: {exc}", REFUSE_FAILED)

        if picked.low_sizes:
            # 在庫考慮: 在庫薄を外して選べたか。使ってしまったならどれか
            if picked.avoided_low:
                ulog.log(f"  在庫考慮: 在庫薄の板 {len(picked.low_sizes)}種を外して選びました", emphasis=True)
            elif picked.low_used:
                ulog.log("  在庫考慮: 在庫薄の板を外すと選べないため、在庫薄も入れて選びました"
                         f"(使った在庫薄: {'・'.join(picked.low_used)})", emphasis=True)
        if choice is not None:
            # どちらを採ったかと、その理由(本当の合計点)を残す
            if choice.compared:
                ulog.log(f"  疲労度の比べ: 通常の選定 {choice.normal.text()}")
                ulog.log(f"               疲労度の選定 {choice.fatigue.text()}")
            ulog.log(f"  → {choice.why}", emphasis=True)
        self.select_result = result
        self.restored = None
        # いま出ている選定が疲労度で選ばれたか(切り替えたときに言うため)
        self.selected_with_fatigue = fatigue_lower is not None
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
        # そのままでは入らず、はみ出す分を切って置いたもの(選定どおりに置いた)
        for cut in getattr(self.placement, "trimmed", []):
            ulog.log(f"  はみ出す分を切って置きました: {cut.label()}", emphasis=True)
            log.info("切って配置: %s", cut.label())
            notes.append(f"切って置きました: {cut.label()}")

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

    def _skip_shelves(self, width: int, length: int) -> list[str]:
        """在庫考慮ONのとき、その板の看板が出ている(切れている)置き場(VER4.8.68)。"""
        out_for = getattr(self.stock_map, "out_for", None)
        if not out_for or not self.board_type:
            return []
        return sorted(out_for(self.board_type, width, length))

    def _log_skipped_shelves(self, available: list) -> None:
        """疲労度の距離から外した置き場を残す(看板の位置で疲労度が変わるので、あとから追えるように)。"""
        seen: set[tuple[int, int]] = set()
        parts: list[str] = []
        for board in available:
            size = (min(board.width, board.length), max(board.width, board.length))
            if size in seen:
                continue
            seen.add(size)
            skip = self._skip_shelves(board.width, board.length)
            if skip:
                numbers = "・".join(s.replace("lblItem", "") for s in skip)
                parts.append(f"{board.width}×{board.length}(棚{numbers})")
        if parts:
            self.presenter.user_log.log(
                "  在庫考慮: 看板が出ている置き場を外して距離を測ります: " + " / ".join(parts))

    def map_items(self) -> list[dict]:
        """棚検索へ渡す資材(旧版 `btnMap`)。

        **選んだものを並べるだけ。** どこに置いてあるか・どれだけ歩くかは
        棚検索が決める(`layout_session.show_selection`)。
        上用・下用・アングルを全部入れるのは、取りに行くのが1回の作業
        だからで、種類ごとに分けても現場は結局まとめて回る。
        """
        items: list[dict] = []
        # **同じ区分・同じサイズの行は合算する**(VBA `AddOrMergeSel`)。
        # 別案では同じサイズが「主」と「カット前提」の2行に分かれるが、
        # 取りに行くのは1か所なので、分けたままだと件数も疲労度も二重になる
        merged: dict[tuple[str, int, int], dict] = {}
        for category, boards in (("下用", self.selected.lower),
                                 ("上用", self.selected.upper)):
            for board in boards:
                key = (category, board.width, board.length)
                if key in merged:
                    merged[key]["count"] += board.count
                    continue
                # ボード種別も渡す: 棚検索が選定の疲労度と同じ種別の置き場で
                # 距離を測るため(同じ寸法の別種別の棚を光らせない。VER4.8.62)
                # 在庫考慮ONなら、看板が出ている置き場も渡す: 棚検索もそこを外して測る
                # (選定の疲労度と同じ置き場。VER4.8.68)
                merged[key] = {"kind": "board", "category": category,
                               "width": board.width, "length": board.length,
                               "count": board.count, "board_type": self.board_type,
                               "skip_shelves": self._skip_shelves(board.width, board.length)}
                items.append(merged[key])
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
        was_protec = self.presenter.protec.is_protec
        was_shared = self.presenter.is_shared_board_mode
        outcome = self.presenter.apply_lot(
            result, product_width=product_width, product_length=product_length,
            available_board_types=types, current_board_type=self.board_type)
        if outcome["protec"].should_switch:
            self.board_type = outcome["protec"].target_board_type
            log.info("プロテックのためボード種別を切り替えました: %s", self.board_type)
        # 同じロットを開き直してプロテックの判定が変わったら(条件を直した等)、前の判定で選んだボードは外す
        # (プロテック強制を押したときと同じ。VER4.8.98)
        if self.presenter.protec.is_protec != was_protec and self._drop_boards_for_mode():
            self.presenter.user_log.log("[プロテック] 判定が変わったので、選定したボード・配置を外しました(選定し直してください)")
        # 上下共用かどうかが変わったとき(保護材を直した等)も同じ(VER4.8.99)
        if self.presenter.is_shared_board_mode != was_shared and self._drop_boards_for_mode():
            self.presenter.user_log.log("[上下共用] 変わったので、選定したボード・配置を外しました(選定し直してください)")
        # EX受注ならEXオンリーを自動ON、非EXなら自動OFF
        # (VBA は `chkExOnly` を表示して自動でONにしていた。
        #  tkinter版 `_render_ex_order` も同じ)
        self.ex_only = self.presenter.is_ex_order
        # 蓋(枚)は蓋を選べるモード(EX・上下共用)のときだけのもの。外れたら持たない
        if not self.presenter.lid_mode:
            self.lid_row = None
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

    def forget_lot(self) -> None:
        """開こうとしたロットが見つからなかった。前のロットで決めたことも捨てる
        (`work_context.WorkContext.forget_lot` と対)。ロットが無い状態に戻すので、
        倉庫送信は「先にロット検索でロットを確定してください」で断られる。
        """
        if self.presenter.lot_result is None:
            return
        self.clear_for_new_lot()
        self.presenter.lot_result = None
        self.presenter.lot_no = ""

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
        self.forget_stack_dir("ロットが変わった")   # 2山の向きも忘れる
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

    def live_search(self) -> str:
        """入力中の一覧が何で探すか。`"product"`(製品 幅・丈)/ `"pallet"`(パレット 幅・丈)/ `""`。

        **製品 幅・丈が優先。** 製品の欄に幅か丈の数があれば製品で探し、パレットの欄は見ない。
        製品の欄が両方空(数でない)ときに、パレットの欄に数があればパレットの寸法で探す。
        """
        def number(text: str) -> bool:
            try:
                return float(str(text or "").strip()) > 0
            except ValueError:
                return False
        if number(self.live_product_width) or number(self.live_product_length):
            return "product"
        if number(self.live_pallet_width) or number(self.live_pallet_length):
            return "pallet"
        return ""

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
            # 真偽値として読める組。発火中は当たった条件の業界・パレット寸法で絞る(`reject_1p1185`)
            "is_1p1185_mode": self.presenter.mode_1p1185,
        }


def _is_unit(row: Optional[svc.PalletSizeRow], unit: str) -> bool:
    return row is not None and (row.unit or "").strip() == unit


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_session: Optional[SelectionSession] = None
_lock = threading.Lock()




# マスタを引き直したときに比べる欄(発注・帳票・アングル本数に効くもの)
_ROW_FIELDS = (("code", "コード"), ("unit", "単位"), ("industry", "業界"), ("symbol", "記号"),
               ("leg_count", "脚数"), ("keta", "桁数"), ("remark", "備考"))


def _row_differs(old: Any, new: Any) -> bool:
    return any(getattr(old, name) != getattr(new, name) for name, _ in _ROW_FIELDS)


def _row_diff_text(old: Any, new: Any) -> str:
    return "・".join(f"{label} {getattr(old, name) or '---'}→{getattr(new, name) or '---'}"
                    for name, label in _ROW_FIELDS if getattr(old, name) != getattr(new, name))


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
