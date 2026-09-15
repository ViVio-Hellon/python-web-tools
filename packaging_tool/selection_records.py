"""実績パターン・ボード使用実績と、管理者認証

`SelectionSession` の一部(mixin)。**貯めて、あとから引き出すもの**を
まとめてある。

  実績パターン   … そのパレット寸法でどう組んだかを1件保存する。
                   次に同じ寸法が来たら「読込」で丸ごと戻せる
  ボード使用実績 … 「使用する」を押した配置を数える。同じ配置を
                   二度積まないよう、配置の見分けを持つ
  管理者認証     … 実績パターンの保存と削除を通す関門。
                   **パスワードは持たないし返さない**(設計書 §3.6)

本体から読むもの: `palette` / `product` / `board_type` / `presenter` /
`selected` / `select_result` / `placement` / `admin` /
`usage_recorded_key` / `invalidate_placement()` / `_sync_ribbon()`
"""
from __future__ import annotations

from typing import Any

from . import board_usage, pattern_service
from . import board_selection_service as svc
from .logging_utils import get_logger
from .selection_common import (BoardOpResult, REFUSE_DENIED, REFUSE_FAILED,
                               REFUSE_NOT_FOUND, REFUSE_NO_CANDIDATES,
                               REFUSE_NEEDS_SIZES)

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

    def delete_pattern(self, pattern_id: int) -> BoardOpResult:
        """実績パターンの削除(VBA `frmPatterns.btnDelete_Click`)。

        **保存と同じで認証が要る。** 実績は端末をまたいで共有するもので、
        消えたことに気づけるのは次に使おうとした人だけ ── 保存に認証が
        要るなら、消すのにも要る。VBA側は消すほうに認証が無かったが、
        保存側だけ守っても意味がない。

        取り消せないので、訊くのは画面の役目(`ask` の窓)。ここは
        「消えたかどうか」だけを返す。
        """
        if not self.admin:
            return BoardOpResult(False, "管理者認証が必要です。", REFUSE_DENIED)
        if not pattern_service.delete_pattern_by_id(
                self.presenter.conn, pattern_id):
            # すでに誰かが消したか、番号が違う。どちらも「もう無い」
            return BoardOpResult(False, "そのパターンは見つかりませんでした。",
                                 REFUSE_NOT_FOUND)
        log.info("実績パターンを削除しました: No.%s", pattern_id)
        self.presenter.user_log.log(
            f"[実績パターン] No.{pattern_id} を削除しました", emphasis=True)
        return BoardOpResult(True, f"実績パターン No.{pattern_id} を削除しました")

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
