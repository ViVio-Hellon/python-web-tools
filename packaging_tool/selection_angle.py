"""アングル(コーナーボード)の選定 ── 候補・手動追加・自動選定・図

`SelectionSession` の一部(mixin)。アングルはボードと別の資材で、
選び方も別:

  * 丈だけで決まる(幅を持たない)
  * 本数の上限は製品丈で変わる(5000mm以上なら6本、未満は3本)
  * 図は「アングル配置」を押すまで描かない ── 選んだ時点で描くと、
    まだ確かめていない組み合わせが図になる

本体から読むもの: `palette` / `product` / `presenter` / `fatigue` /
`selected_angles` / `angle_need_cut` / `angle_drawn` /
`invalidate_angle_plan()`
"""
from __future__ import annotations

from . import angle_service, location_service, user_settings
from . import user_log as user_log_mod
from .logging_utils import get_logger
from .selection_common import (BoardOpResult, REFUSE_BAD_INPUT,
                               REFUSE_NEEDS_SIZES, REFUSE_NOT_FOUND,
                               REFUSE_NOT_LISTED, REFUSE_NO_CANDIDATES)

log = get_logger("selection_session.angle")


class AngleMixin:
    """`SelectionSession` のアングルの部分。"""

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


    # ------------------------------------------------------------------
    # アングル
    # ------------------------------------------------------------------
    def pallet_leg_count(self) -> int:
        """決めたパレットの脚数。**一覧で選んだ行そのものの脚数**を使う。

        同じ寸法でも脚数の違うパレットがある(510×770 は 3 と 2、1350×1350 は 2 と 3)。
        寸法だけで引くと表の先頭の行の脚数になり、選んだパレットとアングルの本数が
        食い違う。行を選ばずに寸法だけ入れたときは、今までどおり寸法で引く。
        """
        row = getattr(self, "pallet_row", None)
        if (row is not None and (row.width, row.length)
                == (self.palette.width, self.palette.length)):
            return int(row.leg_count or 0)
        return angle_service.get_leg_count(
            self.presenter.conn, self.palette.width, self.palette.length)

    def angle_candidates(self) -> list[int]:
        """候補アングル丈。`load_all_angle_lengths` は未登録でも `[0]` を返すので落とす。"""
        return [length for length
                in angle_service.load_all_angle_lengths(self.presenter.conn)
                if length]

    def add_angle(self, length: int) -> BoardOpResult:
        """アングル追加(VBA `btnAngleAdd_Click`)。候補にある丈だけ受け付ける。

        **本数の上限は自動選定と同じ**(`angle_service.max_pieces`)。
        手で足すときだけ無制限だと、自動では出せない本数の選定結果が
        でき上がり、あとから「なぜこうなったか」を説明できない。
        """
        if length not in self.angle_candidates():
            return BoardOpResult(
                False, f"アングル丈 {length} は候補一覧にありません。一覧から選んでください。",
                REFUSE_NOT_LISTED)
        limit = angle_service.max_pieces(self.product.length)
        if len(self.selected_angles) >= limit:
            # 上限そのものが製品丈で変わるので、いまの丈も添える
            return BoardOpResult(
                False,
                f"アングルは最大{limit}本までです"
                f"(製品丈 {self.product.length}mm のとき)。"
                "外してから足してください。",
                REFUSE_BAD_INPUT)
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
            leg_count = self.pallet_leg_count()
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
