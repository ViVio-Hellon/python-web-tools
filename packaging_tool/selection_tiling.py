"""候補変更(敷き詰め方式) ── 「もう1通りの組み方」を見せる段

**現行の選定・配置には触らない。** 押すたびに A→B→C と軸を回して、
別の敷き詰め方の候補を出すだけの段で、いま決まっているものを書き
換えるのは `_apply_tiling` で採用した瞬間だけ。

`SelectionSession` の一部(mixin)として使う。持っている状態は
`TilingState` 1つで、あとは本体の寸法・ボード種別・候補一覧を読む。

**作り直す条件を推し量らない。** 候補の中身を決めるものすべてを鍵
(`_tiling_key`)にして、1つでも変われば作り直す ── 古い候補を出すと、
いま入力してある寸法と何の関係も無いボードが選定されて、そのまま図に
なる。図と現物が食い違ったまま切断依頼や倉庫送信まで進むので、静かに
間違うたぐいの中でもいちばん重い。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import board_selection_service as svc
from . import placement_algorithm as place
from . import tiling_algorithm as tiling
from . import user_log as user_log_mod
from .logging_utils import get_logger
from .selection_common import (BoardOpResult, REFUSE_NOT_FOUND,
                               REFUSE_NO_CANDIDATES, log_placed)

log = get_logger("selection_session.tiling")



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


def tiling_note(cand: Optional[tiling.TileCand]) -> str:
    """選定ログに出す候補1件の要約。**決め手が読めるように出す。**"""
    if cand is None:
        return "なし"
    return (f" {cand.board_count}枚 {cand.type_count}種"
            f" カット{cand.cuts}枚 超過{cand.over_w + cand.over_l}mm")


class TilingMixin:
    """`SelectionSession` の「候補変更」の部分。

    本体から読むもの: `palette` / `product` / `board_type` /
    `stock_aware` / `presenter` / `candidates()` / `selected` /
    `placement` / `select_result` / `_require_sizes()`
    """

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
            note = " / ".join(f"{label}{tiling_note(cands[i])}"
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
        log_placed(ulog, ctx.placed)
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
