"""ボード選定で受け渡す型 ── 選定の**答えの形**だけを持つ

ここには判断を置かない。置くのは「選定が何を返すか」「パス間で何を
受け渡すか」だけで、どう選ぶかは `board_selection_algorithm` 以下が
持つ。

**型をここへ分けた理由。** 下用・上用・プロテック・狭幅は互いの結果型を
参照し合っていて(下用の結果がプロテック確定値を持ち、狭幅が上用の結果型
を返す、など)、処理ごとにファイルを分けると型の取り合いで輪ができる。
型だけを先に切り出すと、処理側は一方向に並べられる。

    型(ここ) → 共通の寸法合わせ → プロテック / 狭幅 → 下用 / 上用 → ハブ
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .board_selection_service import SelectedBoard


@dataclass
class PassState:
    """選定パス間で受け渡す可変状態(VBA のByRef引数群 + モジュール変数)。"""

    remaining_len: int
    selected_count: int = 0
    pass1_done: bool = False
    pass15_used: bool = False  # 上記のとおりVBAでは常にFalse
    narrow_pallet: bool = False
    cut_info: dict[str, int] = field(default_factory=dict)


@dataclass
class ProtecCutResult:
    """プロテック専用の選定確定値(VBA `ProtecCutResult` / `mProtecCutResult`)。

    選定(`select_protec_lower_boards`)が決めた**唯一の正解**を保持する。
    後続の処理(丈カット判定・配置・カット依頼書)はここに書かれた値を
    そのまま使い、再計算しない ── 以前は配置やカット依頼書がそれぞれ
    独自に「製品幅を超えてよいか」を判定し直しており、選定結果と
    食い違うことがあった(プロテックの上用ボードが製品幅を超過すると
    配置段階が静かに弾いてしまい、`placedBoards` に一切登録されない
    という不具合の原因)。この型が「唯一の正解」の置き場になることで、
    再計算そのものを起こさせない。

    `valid` が False のときは他フィールドを見ない(まだプロテック確定
    値が無い、または通常選定にフォールバックした状態)。
    """

    valid: bool = False
    orig_width: int = 0     # 元の在庫サイズ(幅)
    orig_length: int = 0    # 元の在庫サイズ(丈)
    is_rotated: bool = False
    eff_width_before_cut: int = 0  # 向きを決めた後、カットする前の実効幅
    cut_eff_width: int = 0  # 幅カット後の実効幅(カット不要ならeff_width_before_cutと同じ)
    eff_length: int = 0     # 丈方向の実効サイズ(フルサイズ側の1枚あたりの丈)
    need_cut: bool = False  # 幅カットが必要か
    need_length_cut: bool = False   # 丈カットが必要か(最後の1枚だけ)
    length_cut_eff: int = 0         # 丈カット後の、最後の1枚の丈
    count: int = 1          # 枚数(丈カットする最後の1枚も含む)
    len_normal_cnt: int = 0  # 丈カットしない枚数
    len_cut_cnt: int = 0     # 丈カットする枚数(0か1)

    # 「パレット丈には収まるが製品丈は超えている」状態。
    # **カットは必須ではないが、切る余地はある。** 切断依頼書で
    # 押した人に訊く(`reports.protec_cut_size_info`)。
    # `need_length_cut=False` のままここが立つので、配置図にカット線は
    # 出ない ── 図は「切らない」姿を描き、依頼書だけが「もし切るなら」の
    # 内訳を出す
    len_cut_optional: bool = False
    len_opt_normal_cnt: int = 0  # 切る場合の通常枚数
    len_opt_cut_cnt: int = 0     # 切る場合の丈カット枚数
    len_opt_cut_eff: int = 0     # 切る場合の丈カット後サイズ


@dataclass
class LowerSelectionResult:
    boards: list[SelectedBoard]
    state: PassState
    post_fill_max_w: int = 0
    needs_wide_cut: bool = False   # カット前提選定(SelectBoardsForWideLower)が必要
    needs_narrow: bool = False     # 狭幅パレット選定が必要
    # カット前提選定(`select_boards_for_wide_lower`)が記録した丈カット情報。
    # `recalc_length_cut_info` は「カット前提」タグのボードを判定対象外に
    # するので(通常モードの再判定と二重に扱わないため)、その代わりに
    # ここへ記録された情報をそのまま結果へマージする
    length_cut_info: dict[str, int] = field(default_factory=dict)
    # プロテック確定値(唯一の正解)。プロテックでない、またはプロテック
    # 選定が使える在庫を見つけられなかったときは valid=False のまま
    protec_result: ProtecCutResult = field(default_factory=lambda: ProtecCutResult())


@dataclass
class ProtecLengthCut:
    """プロテックの丈方向をどう作るか。`ProtecCutResult` に写して使う。"""

    count: int = 1
    need_cut: bool = False
    normal_cnt: int = 0
    cut_cnt: int = 0
    cut_eff: int = 0
    # 「切らなくてもパレットには収まるが、製品丈は超えている」
    optional: bool = False
    opt_normal_cnt: int = 0
    opt_cut_cnt: int = 0
    opt_cut_eff: int = 0


@dataclass
class UpperSelectionResult:
    boards: list[SelectedBoard]
    mode: str = "normal"           # "normal"/"共用"/"プロテック"
    narrow_pallet: bool = False
    needs_wide_cut: bool = False   # SelectUpperBoardsWideCut が必要
    cut_info: dict[str, int] = field(default_factory=dict)
    length_cut_info: dict[str, int] = field(default_factory=dict)
    # プロテック確定値(下用選定と共有する「唯一の正解」)。mode="プロテック"
    # のときだけ valid=True になる
    protec_result: ProtecCutResult = field(default_factory=lambda: ProtecCutResult())


@dataclass
class WideCutResult:
    boards: list[SelectedBoard]
    cut_info: dict[str, int] = field(default_factory=dict)
    # 丈カット(VBA `mLengthCutInfo`)。キーは "U_幅x丈"
    length_cut_info: dict[str, int] = field(default_factory=dict)
    used_fallback: bool = False


@dataclass
class AutoSelectResult:
    lower: list[SelectedBoard]
    upper: list[SelectedBoard]
    lower_result: LowerSelectionResult
    upper_result: UpperSelectionResult
    cut_info: dict[str, int] = field(default_factory=dict)
    length_cut_info: dict[str, int] = field(default_factory=dict)
    length_cut_count: dict[str, int] = field(default_factory=dict)
