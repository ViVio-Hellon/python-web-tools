"""実績パターン・ボード使用実績と、管理者認証

`SelectionSession` の一部(mixin)。**貯めて、あとから引き出すもの**を
まとめてある。

  実績           … 配置を承認した画面の状態を**そのまま**保存する
                   (スナップショット。VBA `btnSavePattern_Click`)。
                   読込は計算し直さずに戻す(`LoadSinglePattern`)。
                   保存庫は `pattern_store`、共有は `pattern_sync`
  ボード使用実績 … 「使用する」を押した配置を数える。同じ配置を
                   二度積まないよう、配置の見分けを持つ
  管理者認証     … 実績パターンの保存と削除を通す関門。
                   **パスワードは持たないし返さない**(設計書 §3.6)

本体から読むもの: `palette` / `product` / `board_type` / `presenter` /
`selected` / `select_result` / `placement` / `admin` /
`usage_recorded_key` / `restored` / `placement_method` /
`placed_signature` / `product_rotated` / `pallet_row` / `tiling` /
`selected_angles` / `invalidate_angle_plan()` / `_sync_ribbon()`
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import board_usage
from . import board_selection_service as svc
from . import pattern_store as store
from . import placement_algorithm as place
from . import user_settings
from .board_selection_types import ProtecCutResult
from .models import PlacedBoardModel
from .pattern_store import METHOD_NORMAL
from .logging_utils import get_logger
from .selection_common import (BoardOpResult, REFUSE_DENIED, REFUSE_FAILED,
                               REFUSE_NOT_FOUND, REFUSE_NO_CANDIDATES)

log = get_logger("selection_session.records")


class RecordsMixin:
    """`SelectionSession` の実績と管理者の部分。"""

    def authenticate(self, password: str) -> BoardOpResult:
        """管理者認証(VBA `btnAuth_Click`)。

        **照合はサーバでしか行わない。** パスワードは応答に載せないし、
        画面へ渡すのは「認証したか」の1ビットだけ(設計書 §3.6)。
        """
        # 照合の出どころは `admin_password` ただ1つ。設定画面から
        # 変えられるようになったので、ここで `config` を直接読まない
        from . import admin_password
        ok = admin_password.verify(password)
        if bool(getattr(self, "admin", False)) != ok:
            # **変わったことを知らせる**(開いているほかの画面が描き直す)
            from . import auth_state
            auth_state.changed()
        self.admin = ok
        if not ok:
            log.warning("管理者認証に失敗しました")
            return BoardOpResult(False, "パスワードが正しくありません。",
                                 REFUSE_DENIED)
        log.info("管理者認証に成功しました")
        return BoardOpResult(True, "認証しました")

    # ------------------------------------------------------------------
    # 実績(スナップショット)── VBA `btnSavePattern_Click` / `LoadSinglePattern`
    # ------------------------------------------------------------------
    def narrow_flags(self) -> tuple[bool, bool]:
        """狭幅パレット選定が使われたか(下用, 上用)。

        自動選定の結果があればそこから、実績から戻したならその値。
        どちらも無ければ(手で増減した)いいえ。
        """
        if self.select_result is not None:
            return (self.select_result.lower_result.state.narrow_pallet,
                    self.select_result.upper_result.narrow_pallet)
        if self.restored is not None:
            return self.restored.narrow_lower, self.restored.narrow_upper
        return False, False

    def fixed_protec(self) -> Optional[ProtecCutResult]:
        """確定済みのプロテック確定値。無ければ None(呼び手が後付けする)。"""
        if self.select_result is not None:
            return self.select_result.lower_result.protec_result
        if self.restored is not None and self.restored.protec_result.valid:
            return self.restored.protec_result
        return None

    def cut_dicts(self) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        """(幅カット, 丈カット長, 丈カット枚数)。切断依頼書と実績保存が見る。"""
        if self.select_result is not None:
            r = self.select_result
            return (dict(r.cut_info), dict(r.length_cut_info),
                    dict(r.length_cut_count))
        if self.restored is not None:
            return (dict(self.restored.cut_info),
                    dict(self.restored.length_cut_info),
                    dict(self.restored.length_cut_count))
        return {}, {}, {}

    def build_placement_signature(self) -> str:
        """配置時点との比較用の文字列(VBA `BuildPlacementSignature`)。

        パレット・製品・上下リスト(幅, 丈, 枚数, タグ)を1本にする。
        """
        def rows(boards: list) -> str:
            return "".join(f"{b.width},{b.length},{b.count},{(b.tag or '').strip()};"
                           for b in boards)
        return (f"P:{self.palette.width}x{self.palette.length}"
                f"|S:{self.product.width}x{self.product.length}"
                f"|L:{rows(self.selected.lower)}|U:{rows(self.selected.upper)}")

    def record_placement(self, method: str) -> None:
        """配置が済んだときに、配置方式と配置時点の内容を控える。"""
        self.placement_method = method
        self.placed_signature = self.build_placement_signature()

    def save_refusal(self) -> str:
        """実績を保存できない理由。保存できるなら空。"""
        if not self.admin:
            return "管理者認証が必要です。"
        if self.placement is None or not self.placement.placed:
            return "先に配置を実行してください。"
        if self.build_placement_signature() != self.placed_signature:
            return ("配置後にパレット・製品サイズまたはボードが変更されています。"
                    "「ボード配置」または「候補変更」で配置し直してから保存してください。")
        return ""

    def save_confirm(self) -> dict[str, str]:
        """保存の前に見せる内容(VBA の確認 MsgBox)。**文はサーバが持つ。**"""
        if self.save_refusal():
            return {}
        return {
            "title": "この内容で実績を保存します",
            "body": "\n".join([
                f"パレット: {self.palette.width} × {self.palette.length}",
                f"製品: {self.product.width} × {self.product.length}",
                f"配置方式: {self.placement_method or METHOD_NORMAL}",
                f"ボード: 下用{len(self.selected.lower)}行 / "
                f"上用{len(self.selected.upper)}行"
                f"(配置{len(self.placement.placed)}枚)",
            ]),
        }

    def save_pattern(self) -> BoardOpResult:
        """画面のスナップショットを実績として保存する(VBA `btnSavePattern_Click`)。

        **認証していなければ保存できない。** 配置のあとでパレット・製品・
        ボードが変わっていたら止める ── 保存されるのは「配置を承認した
        画面の状態」なので、変わったあとの内容を黙って混ぜない。
        確認(保存してよいか)は画面が `save_confirm` の文で訊いてから呼ぶ。
        """
        refusal = self.save_refusal()
        if refusal:
            reason = (REFUSE_DENIED if not self.admin
                      else REFUSE_NO_CANDIDATES)
            return BoardOpResult(False, refusal, reason)

        cut_info, length_cut_info, length_cut_count = self.cut_dicts()
        # 配置のときに見つかった幅カット(丈補填の在庫が長いとき)も
        # 同じ「幅カット」に入れる(VBA は1つの `mCutInfo` に持つ)
        for key, value in self.placement.cut_info.items():
            cut_info.setdefault(key, value)

        protec = self.presenter.protec.is_protec
        header = {
            "パレット幅": self.palette.width, "パレット丈": self.palette.length,
            "製品幅": self.product.width, "製品丈": self.product.length,
            "製品回転": b2l(self.product_rotated),
            "ボード種別": self.board_type,
            "配置方式": self.placement_method or METHOD_NORMAL,
            "狭幅下": b2l(self.narrow_flags()[0]),
            "狭幅上": b2l(self.narrow_flags()[1]),
            "保護材": self.presenter.last_hosozai,
            "アングル": ",".join(str(a) for a in self.selected_angles),
            "プロテック確定値": (store.protec_to_text(self._protec_for_save())
                              if protec else ""),
            "LotNo": self.presenter.lot_no or "",
            "拠点": user_settings.get_position(default=""),
        }
        select_rows = (_list_rows(self.selected.lower, store.CATEGORY_LOWER)
                       + _list_rows(self.selected.upper, store.CATEGORY_UPPER))
        place_rows = [{
            "区分": pb.board_category, "順番": seq,
            "板ID": store.pt_long(pb.id),
            "インスタンスID": str(pb.instance_id)[:50],
            "座標X": pb.x, "座標Y": pb.y, "幅": pb.width, "丈": pb.length,
            "元幅": pb.original_width, "元丈": pb.original_length,
            "補填": b2l(pb.is_fill_board),
        } for seq, pb in enumerate(self.placement.placed, start=1)]
        cut_rows = (_dict_rows(cut_info, store.CUT_WIDTH)
                    + _dict_rows(length_cut_info, store.CUT_LENGTH)
                    + _dict_rows(length_cut_count, store.CUT_LENGTH_COUNT))

        try:
            pattern_id = store.save_pattern_snapshot(
                self.presenter.conn, header, select_rows, place_rows, cut_rows)
        except store.PatternStoreError as exc:
            log.exception("実績保存エラー")
            return BoardOpResult(False, f"保存エラー: {exc}", REFUSE_FAILED)

        self.presenter.user_log.log(
            f"[実績保存] ID {pattern_id}(配置方式: {header['配置方式']} / "
            f"配置{len(place_rows)}枚)", emphasis=True)
        # 共有(取り込み元)へは裏で送る。送れなくても手元の保存は済んでいる
        _push_later()
        return BoardOpResult(True, f"実績を保存しました(ID: {pattern_id})")

    def _protec_for_save(self) -> Optional[ProtecCutResult]:
        """保存するプロテック確定値。配置に使った値を優先する。"""
        if self.placement is not None and self.placement.protec_result.valid:
            return self.placement.protec_result
        return self.fixed_protec()

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
        # 共有へ送る。**人気度は全端末の合計**なので、手元に置いたままでは
        # ほかの端末の表に出ない。送れなくても手元の記録は済んでいる
        _push_later()
        return BoardOpResult(True, f"使用実績に{sheets}枚を記録しました。")

    def patterns(self) -> list[Any]:
        """いまのパレット寸法で登録されている実績(VBA `GetPatternList`)。

        **ほかの端末が保存した実績も出す。** 共有が変わっていれば、一覧を作る
        前に実績だけ取り込み直す(`pattern_sync.refresh_if_changed`)。
        """
        if not self.palette.is_set:
            return []
        from . import pattern_sync
        pattern_sync.refresh_if_changed(self.presenter.conn)
        return store.get_pattern_list(
            self.presenter.conn, self.palette.width, self.palette.length)

    def _stale_pattern(self, pattern_id: int, seen: Optional[dict]) -> str:
        """画面が見ていた実績と、いまその番号の実績が違えば理由を返す。

        実績の番号は、共有から取り込み直すと共有での番号に振り直される。
        古い一覧のまま押すと、**別の実績を読み込む・消す**ことになる。
        画面は見ていた実績の登録日時とボードの内訳を送ってくる(`seen`)。
        """
        if not seen:
            return ""
        now = next((p for p in store.get_pattern_list(self.presenter.conn)
                    if p.id == pattern_id), None)
        if now is None:
            return "その実績はもうありません(ほかの端末で消されたか、番号が振り直されました)。一覧を確かめてください。"
        if (str(seen.get("registered_at", "")) != now.registered_at
                or str(seen.get("boards", "")) != now.board_summary):
            return ("実績の一覧が新しくなっています(ほかの端末の保存・削除で番号が"
                    "振り直されました)。一覧を確かめてから、もう一度押してください。")
        return ""

    def delete_pattern(self, pattern_id: int,
                       seen: Optional[dict] = None) -> BoardOpResult:
        """実績の削除(VBA `DeletePatternByID`)。

        **保存と同じで認証が要る。** 実績は端末をまたいで共有するもので、
        消えたことに気づけるのは次に使おうとした人だけ ── 保存に認証が
        要るなら、消すのにも要る。VBA側は消すほうに認証が無かったが、
        保存側だけ守っても意味がない。

        取り消せないので、訊くのは画面の役目(`ask` の窓)。ここは
        「消えたかどうか」だけを返す。取り込み元からも消す(`pattern_sync`)。
        """
        if not self.admin:
            return BoardOpResult(False, "管理者認証が必要です。", REFUSE_DENIED)
        stale = self._stale_pattern(pattern_id, seen)
        if stale:
            return BoardOpResult(False, stale, REFUSE_NOT_FOUND)
        try:
            gone = store.delete_pattern_by_id(self.presenter.conn, pattern_id)
        except store.PatternStoreError as exc:
            return BoardOpResult(False, f"削除エラー: {exc}", REFUSE_FAILED)
        if not gone:
            # すでに誰かが消したか、番号が違う。どちらも「もう無い」
            return BoardOpResult(False, "その実績は見つかりませんでした。",
                                 REFUSE_NOT_FOUND)
        log.info("実績を削除しました: ID %s", pattern_id)
        self.presenter.user_log.log(
            f"[実績] ID {pattern_id} を削除しました", emphasis=True)
        _push_later()
        return BoardOpResult(True, f"実績 ID {pattern_id} を削除しました")

    def load_pattern(self, pattern_id: int,
                     seen: Optional[dict] = None) -> BoardOpResult:
        """実績を読み込む(VBA `LoadSinglePattern`)。**計算し直さない。**

        保存したときの画面の状態(選定リストとタグ・配置・カット・プロテック
        確定値・狭幅・アングル)をそのまま戻す。以前は寸法と枚数だけを戻して
        配置を計算し直していたので、保存したときと違う配置になることがあった。

        ロットから決まる状態(プロテックか・保護材)は**上書きしない**。
        いまのロットと食い違っていれば、読み込んだうえで知らせる。
        """
        stale = self._stale_pattern(pattern_id, seen)
        if stale:
            return BoardOpResult(False, stale, REFUSE_NOT_FOUND)
        try:
            snap = store.load_pattern_snapshot(self.presenter.conn, pattern_id)
        except store.PatternStoreError as exc:
            reason = (REFUSE_NOT_FOUND if "見つかりません" in str(exc)
                      else REFUSE_FAILED)
            return BoardOpResult(False, f"読込エラー: {exc}", reason)
        h = snap.header
        conn = self.presenter.conn

        # ① ボード種別(マスタにあるときだけ)
        saved_type = store.nv_str(h.get("ボード種別"))
        if saved_type and saved_type != self.board_type:
            if saved_type in svc.list_board_types(conn):
                self.board_type = saved_type

        # ② パレット・製品
        pw, pl = store.pt_long(h.get("パレット幅")), store.pt_long(h.get("パレット丈"))
        self.palette = svc.Palette(
            width=pw, length=pl, overhang_ratio=svc.PALETTE_OVERHANG_RATIO,
            max_width=pw * svc.PALETTE_OVERHANG_RATIO,
            max_length=pl * svc.PALETTE_OVERHANG_RATIO)
        # 一覧で選んでいた行(発注コード)が別の寸法なら手放す。**古い行の
        # ままだと、別のパレットの発注コードで倉庫へ送れてしまう**
        if (self.pallet_row is not None
                and (self.pallet_row.width, self.pallet_row.length) != (pw, pl)):
            self.pallet_row = None
        self.product = svc.ProductSize(width=store.pt_long(h.get("製品幅")),
                                       length=store.pt_long(h.get("製品丈")))
        self.product_rotated = store.pt_long(h.get("製品回転")) != 0
        # 保存済みの製品サイズは2山分を含むので、再度2倍しないよう向きを消す
        # (VBA `LoadSinglePattern`)。入力欄へは保存した値がそのまま戻る
        self.forget_stack_dir("実績を読み込んだ")
        self.forget_product_stack()

        # ③〜⑥ 選定リスト(タグ込み)・アングル・選定の結果
        lower, upper = [], []
        for row in snap.select_rows:
            board = svc.SelectedBoard(
                width=store.pt_long(row.get("幅")), length=store.pt_long(row.get("丈")),
                count=store.pt_long(row.get("枚数")), tag=store.nv_str(row.get("タグ")))
            (upper if store.nv_str(row.get("区分")) == store.CATEGORY_UPPER
             else lower).append(board)
        self.selected.lower, self.selected.upper = lower, upper
        self.selected_angles = [
            store.pt_long(a) for a in store.nv_str(h.get("アングル")).split(",")
            if a.strip() and store.pt_long(a) > 0]
        self.invalidate_angle_plan()

        facts = RestoredFacts(
            narrow_lower=store.pt_long(h.get("狭幅下")) != 0,
            narrow_upper=store.pt_long(h.get("狭幅上")) != 0,
            protec_result=store.protec_from_text(store.nv_str(h.get("プロテック確定値"))))
        for row in snap.cut_rows:
            target = {store.CUT_WIDTH: facts.cut_info,
                      store.CUT_LENGTH: facts.length_cut_info,
                      store.CUT_LENGTH_COUNT: facts.length_cut_count
                      }.get(store.nv_str(row.get("種別")))
            if target is not None:
                target[store.nv_str(row.get("キー"))] = store.pt_long(row.get("値"))
        self.select_result = None
        self.restored = facts
        self.tiling = None                      # VBA `InvalidateTileCache`

        # ⑦ 配置(再計算しない)
        ctx = place.PlacementContext(
            palette=self.palette, product=self.product,
            protec_result=facts.protec_result,
            is_protec_mode=self.presenter.protec.is_protec,
            is_1p1216=self.presenter.protec.is_1p1216)
        for row in snap.place_rows:
            ctx.placed.append(PlacedBoardModel(
                id=store.pt_long(row.get("板ID")),
                instance_id=store.nv_str(row.get("インスタンスID")),
                x=store.pt_long(row.get("座標X")), y=store.pt_long(row.get("座標Y")),
                width=store.pt_long(row.get("幅")), length=store.pt_long(row.get("丈")),
                original_width=store.pt_long(row.get("元幅")),
                original_length=store.pt_long(row.get("元丈")),
                board_category=store.nv_str(row.get("区分")),
                is_fill_board=store.pt_long(row.get("補填")) != 0))
        ctx.lower_order = list(lower)
        ctx.upper_order = list(upper)
        self.placement = ctx

        # ⑧ 状態
        self.record_placement(store.nv_str(h.get("配置方式")) or METHOD_NORMAL)
        self._sync_ribbon()

        # ⑨ ロット由来の状態は上書きせず、食い違いだけ知らせる
        warn = []
        saved_protec = bool(store.nv_str(h.get("プロテック確定値")))
        if saved_protec != self.presenter.protec.is_protec:
            warn.append("プロテック指定が現在のロットと異なります")
        saved_hoso = store.nv_str(h.get("保護材"))
        now_hoso = self.presenter.last_hosozai or ""
        if saved_hoso and now_hoso and saved_hoso != now_hoso:
            warn.append(f"保護材が異なります(保存時: {saved_hoso} / 現在: {now_hoso})")

        ulog = self.presenter.user_log
        ulog.log(f"[実績読込] ID {pattern_id}(配置方式: {self.placement_method} / "
                 f"配置{len(ctx.placed)}枚)── 計算し直さずに戻しました", emphasis=True)
        for line in warn:
            ulog.log(f"  確認してください: {line}", emphasis=True)
        log.info("実績を読み込みました: ID %s 方式=%s 配置=%s枚",
                 pattern_id, self.placement_method, len(ctx.placed))
        # 使用回数(+1)も共有へ足しに行く(VBA は共有を直接 +1 する)
        _push_later()
        return BoardOpResult(
            True, f"実績を読み込みました(配置方式: {self.placement_method})",
            notes=[f"確認してください: {line}" for line in warn])


@dataclass
class RestoredFacts:
    """実績から戻した「選定の結果」。自動選定の結果(`select_result`)の代わり。

    読込は計算し直さないので、配置し直し・切断依頼書・図が見る値を
    ここに持つ(VBA は `mNarrowPalette*` / `mCutInfo` / `mLengthCut*` /
    `mProtecCutResult` をそのまま書き戻している)。
    """

    narrow_lower: bool = False
    narrow_upper: bool = False
    protec_result: ProtecCutResult = field(default_factory=ProtecCutResult)
    cut_info: dict[str, int] = field(default_factory=dict)
    length_cut_info: dict[str, int] = field(default_factory=dict)
    length_cut_count: dict[str, int] = field(default_factory=dict)


def cut_facts_from_placement(upper: list, lower: list, placed: list,
                             product_length: int, palette_length: int) -> RestoredFacts:
    """別案で配置した後、カット辞書を実際の配置から作り直す
    (VBA `RebuildCutInfoFromPlacement` / `AddCutInfoForCategory`)。

    別案は選定を通らないので `select_result` が無く、以前は**直前の通常
    選定のカット辞書が残って**いた(課題表 1)。実績保存が見る値なので、
    別案の配置から作る。切断依頼書は辞書を使わず配置から直接集計する
    (`reports.get_cut_size_info`)。

    対象はカット前提のサイズで、実際に切られている板だけ。キーはリスト上の
    「幅x丈」(丈カットは上用 `U_` / 下用 `L_` を付ける)。
        幅カット … 値は切断線 = 板の丈方向の長さ
        丈カット … 値は切断線 = 板の幅方向の長さ。切る枚数も数える
    狭幅パレットの前提とプロテック確定値は持たない(別案には無い)。
    """
    from .placement_render import detect_board_cut
    from .reports import TAG_CUT_PREMISE, sku_key_min_max

    facts = RestoredFacts()
    for category, boards, base_l, prefix in (
            (place.CATEGORY_UPPER, upper, product_length, "U_"),
            (place.CATEGORY_LOWER, lower, palette_length, "L_")):
        cut_sku = {sku_key_min_max(b.width, b.length): f"{b.width}x{b.length}"
                   for b in boards if (b.tag or "").strip() == TAG_CUT_PREMISE}
        if not cut_sku:
            continue
        for pb in placed:
            if pb.board_category != category or pb.is_fill_board:
                continue
            list_key = cut_sku.get(sku_key_min_max(pb.original_width, pb.original_length))
            if list_key is None:
                continue
            cut = detect_board_cut(pb.original_width, pb.original_length,
                                   pb.width, pb.length)
            over_l = max(0, (pb.x + pb.length) - base_l)
            if cut.cut_width:
                facts.cut_info[list_key] = pb.length
            if over_l > 0 or cut.cut_length:
                facts.length_cut_info[prefix + list_key] = pb.width
                facts.length_cut_count[prefix + list_key] = (
                    facts.length_cut_count.get(prefix + list_key, 0) + 1)
    log.debug("cut_facts_from_placement: 幅カット=%s 丈カット=%s",
              len(facts.cut_info), len(facts.length_cut_info))
    return facts


def b2l(flag: bool) -> int:
    return 1 if flag else 0


def _list_rows(boards: list, category: str) -> list[dict[str, Any]]:
    """選定リストを保存用の行にする(VBA `CollectListRows`)。"""
    return [{"区分": category, "行順": i, "幅": b.width, "丈": b.length,
             "枚数": b.count, "タグ": (b.tag or "").strip()[:20]}
            for i, b in enumerate(boards, start=1)]


def _dict_rows(values: dict[str, int], kind: str) -> list[dict[str, Any]]:
    """カット辞書を保存用の行にする(VBA `CollectDictRows`)。"""
    return [{"種別": kind, "キー": str(k)[:50], "値": store.pt_long(v)}
            for k, v in values.items()]


def _push_later() -> None:
    """保存・削除を取り込み元へ裏で送る(失敗しても画面は止めない)。"""
    try:
        # 発注・受払と同じ入口から送る(`data_sync.write_back_in_background`)
        from . import data_sync
        data_sync.write_back_in_background()
    except Exception:                               # noqa: BLE001 - 裏の処理
        log.exception("実績の書き戻しを始められませんでした")
