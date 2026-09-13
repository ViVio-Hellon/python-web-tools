"""アングル(角当て)ボード選定・疲労度計算

VBA `modAngleSelect`(アングルボード選定ロジック v4)と、その中に同居する
`modFatigueCore`(疲労度計算式の唯一の定義場所とコメントされている一群の
Public Function)の移植。ソースは実際のVBAコードを1行ずつ確認して移植した
(优先順位・端数条件など、business-criticalなロジックのため要約からの
再構成ではなく逐語訳している)。

疲労度計算式(SSOT、元コメント引用):
    総疲労 = 距離スコア x1
           + 面積スコア x 枚数     (運搬は枚数分)
           + 幅カット   x 枚数     (丈方向に並べるため全数カット)
           + 丈カット   x1         (丈オーバーは最後の1枚のみカット)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from . import db
from .logging_utils import get_logger

log = get_logger("angle_service")

# ── SelectAngles ステップ制御 ──
ANGLE_STEP1_TOL_PCT = 0.05   # Step1 超過許容率(5%)
ANGLE_STEP1_MIN_TOL = 30     # Step1 最小許容mm
ANGLE_STEP2_MAX_LEN = 2500   # Step2 カット判定閾値mm
ANGLE_MULTI_THRESHOLD = 5000  # 3本以上切替閾値mm(Step3cの最大本数切替)
ANGLE_MAX_PIECES_LONG = 6     # 製品丈が閾値以上のときの最大本数
ANGLE_MAX_PIECES_SHORT = 3    # 製品丈が閾値未満のときの最大本数


def max_pieces(product_len: int) -> int:
    """アングルの最大本数(VBA `btnAngleAdd_Click` / `SelectAngles` Step3c)。

    **自動選定と手動追加の両方がここを見る。** VBAは同じ上限を2か所に
    別々に書いていて、片方だけ直せば静かに食い違う形だった。
    """
    return (ANGLE_MAX_PIECES_LONG if product_len >= ANGLE_MULTI_THRESHOLD
            else ANGLE_MAX_PIECES_SHORT)

# ── 疲労スコア計算(modFatigueCore) ──
CUT_FAT_MULT = 233.0     # frmLayout・MaterialMasterFormも参照。変更時はここだけ
FAT_MAX_DIST = 1000.0    # 距離スコア分母
FAT_MAX_LEN = 3000       # 長さスコア分母
FAT_SCORE_CAP = 50.0     # スコア上限
FAT_DIST_DEFAULT = 25.0  # 距離不明時デフォルト


@dataclass
class AngleResult:
    """VBA `AngleResult` (Public Type) の移植。"""

    count: int = 0
    angle1: int = 0
    angle2: int = 0
    angles: list[int] = field(default_factory=list)  # 3本以上のとき全本の丈
    total_len: int = 0
    overlap: int = 0  # 正=被り/超過(mm) 負=隙間(mm)
    need_cut: bool = False
    info: str = ""


def prepare_unique_angles(angles: Sequence[int]) -> list[int]:
    """VBA `PrepareUniqueAngles` の移植。正の値のみ重複排除し昇順ソートする。"""
    return sorted({a for a in angles if a > 0})


# ------------------------------------------------------------------
# modFatigueCore: 疲労度計算式(SSOT)
# ------------------------------------------------------------------
def fat_dist_score(dist: float) -> float:
    """距離スコア(1回分・キャップ付)。"""
    return min((dist / FAT_MAX_DIST) * FAT_SCORE_CAP, FAT_SCORE_CAP)


def fat_area_score1(w: float, l: float) -> float:
    """面積スコア(1枚分・キャップ付)。"""
    max_area = float(FAT_MAX_LEN) * float(FAT_MAX_LEN)
    return min((float(w) * float(l) / max_area) * FAT_SCORE_CAP, FAT_SCORE_CAP)


def fat_cut_score1(cut_line_length: float) -> float:
    """カットスコア(切断線1本分・キャップ付)。"""
    if cut_line_length <= 0:
        return 0.0
    return min((cut_line_length / FAT_MAX_LEN) * CUT_FAT_MULT, CUT_FAT_MULT)


def fat_width_cut_score(cut_line_length: float, sheet_cnt: int) -> float:
    """幅カット疲労: 全数カット(1枚分 x 枚数)。"""
    return fat_cut_score1(cut_line_length) * max(sheet_cnt, 1)


def fat_length_cut_score(cut_line_length: float) -> float:
    """丈カット疲労: 常に1枚のみ。"""
    return fat_cut_score1(cut_line_length)


def fat_combine(d: float, a1: float, wc1: float, lc1: float, sheet_cnt: int) -> float:
    """統一式による合算。d=距離(1回分) a1=面積(1枚分) wc1=幅カット(1枚分) lc1=丈カット(1回分)。"""
    return d + a1 * max(sheet_cnt, 1) + wc1 * max(sheet_cnt, 1) + lc1


def _agl_fat_score(a_len: int, cnt: int, need_cut: bool, cut_len: int, fat_map: Optional[Mapping[str, float]]) -> float:
    """1種アングル × cnt本のスコア(VBA `AglFatScore`)。"""
    ds = FAT_DIST_DEFAULT
    if fat_map is not None and str(a_len) in fat_map:
        ds = float(fat_map[str(a_len)])
    ls = min((a_len / 3000) * 50, 50)
    cs = 0.0
    if need_cut and cut_len > 0:
        cs = (50 / 3000) * CUT_FAT_MULT
    return ds + ls * cnt + cs


def _agl_fat_score_asym(a1: int, a2: int, fat_map: Optional[Mapping[str, float]]) -> float:
    """2本非対称のスコア(VBA `AglFatScoreAsym`)。"""
    ds1 = FAT_DIST_DEFAULT
    ds2 = FAT_DIST_DEFAULT
    if fat_map is not None:
        if str(a1) in fat_map:
            ds1 = float(fat_map[str(a1)])
        if str(a2) in fat_map:
            ds2 = float(fat_map[str(a2)])
    ls = min(((a1 + a2) / 3000) * 50, 50)
    return (ds1 + ds2) / 2 + ls


# ------------------------------------------------------------------
# SelectAngles v4 (対称配置版)
# ------------------------------------------------------------------
def select_angles(
    product_len: int,
    angles: Sequence[int],
    pallet_len: int,
    leg_count: int,
    *,
    fatigue_mode: bool = False,
    fat_map: Optional[Mapping[str, float]] = None,
) -> AngleResult:
    """VBA `SelectAngles` の移植。

    候補アングル丈(`angles`)から製品丈をカバーする本数・組み合わせを、
    以下の優先順位で選定する:
        [疲労優先モード(任意): F1 1本そのまま/F2 1本カット/F3 2本対称]
        -> Step1(1本ぴったり、超過5%以内)
        -> Step2(製品丈<2500mmなら1本カット)
        -> Step3(2本対称 → 準対称)
        -> Step3c(3本以上、奇数=両サイド先行+中央、偶数=均等割り)
        -> Step4(フォールバック、2本・被り最小)
    どの段階にも該当しなければ `count=0` の空結果を返す。
    """
    u = prepare_unique_angles(angles)
    if not u:
        return AngleResult()

    leg_spacing = 0
    min_len = 0
    if pallet_len > 0 and leg_count >= 2:
        leg_spacing = pallet_len // (leg_count + 1)
        min_len = leg_spacing * 2

    log.debug("select_angles: product_len=%s pallet_len=%s min_len=%s", product_len, pallet_len, min_len)

    # ── 疲労優先モード ──
    if fatigue_mode and pallet_len > 0:
        best_score = float("inf")
        best: Optional[AngleResult] = None

        # F1: 1本そのまま(製品丈以上 かつ パレット丈以下)
        for a in u:
            if a * 2 >= product_len and a <= pallet_len and (min_len == 0 or a >= min_len):
                score = _agl_fat_score(a, 1, False, 0, fat_map)
                if score < best_score:
                    best_score = score
                    best = AngleResult(
                        count=1, angle1=a, angles=[a], total_len=a,
                        overlap=a - product_len, need_cut=False,
                        info=f"[疲労]1本 {a}mm 超過{a - product_len}mm (score={score:.1f})",
                    )

        # F2: 1本カット(<2500mm)
        if product_len < ANGLE_STEP2_MAX_LEN:
            for a in u:
                if a >= product_len and (min_len == 0 or a >= min_len):
                    score = _agl_fat_score(a, 1, True, product_len, fat_map)
                    if score < best_score:
                        best_score = score
                        best = AngleResult(
                            count=1, angle1=a, angles=[a], total_len=a,
                            overlap=a - product_len, need_cut=True,
                            info=f"[疲労]1本カット {a}mm->{product_len}mm (score={score:.1f})",
                        )

        # F3: 2本対称(合計>=製品丈、疲労モードはパレット制約なし)
        for a in u:
            if a * 2 >= product_len and (min_len == 0 or a >= min_len):
                score = _agl_fat_score(a, 2, False, 0, fat_map)
                if score < best_score:
                    best_score = score
                    best = AngleResult(
                        count=2, angle1=a, angle2=a, angles=[a, a], total_len=a * 2,
                        overlap=a * 2 - product_len, need_cut=False,
                        info=f"[疲労]2本対称 {a}mm×2 被り{a * 2 - product_len}mm (score={score:.1f})",
                    )

        if best is not None:
            log.debug("select_angles fatigue 採用: %s", best.info)
            return best

    # ── Step1: 1本ぴったりカバー(超過 ≦ 5%) ──
    tolerance = max(int(product_len * ANGLE_STEP1_TOL_PCT), ANGLE_STEP1_MIN_TOL)
    best_single, best_single_ov = 0, 999999
    for a in u:
        if product_len <= a <= product_len + tolerance and (min_len == 0 or a >= min_len):
            ov = a - product_len
            if ov < best_single_ov:
                best_single_ov, best_single = ov, a
    if best_single > 0:
        return AngleResult(
            count=1, angle1=best_single, angles=[best_single], total_len=best_single,
            overlap=best_single_ov, need_cut=False,
            info=f"1本 {best_single}mm (超過{best_single_ov}mm)",
        )

    # ── Step2: 製品丈<2500mm → カット前提1本 ──
    if product_len < ANGLE_STEP2_MAX_LEN:
        best_cut, best_cut_ov = 0, 999999
        for a in u:
            if a >= product_len and (min_len == 0 or a >= min_len):
                ov = a - product_len
                if ov < best_cut_ov:
                    best_cut_ov, best_cut = ov, a
        if best_cut > 0:
            return AngleResult(
                count=1, angle1=best_cut, angles=[best_cut], total_len=best_cut,
                overlap=best_cut_ov, need_cut=True,
                info=f"1本カット {best_cut}mm → {product_len}mm",
            )

    # ── Step3①: 2本対称(製品丈÷2ターゲット、同サイズ2本) ──
    target2 = (product_len + 1) // 2  # 切り上げ
    if min_len > 0 and target2 < min_len:
        target2 = min_len
    sym_ang, sym_ov = 0, 999999
    for a in u:
        if a >= target2:
            ov = a - target2
            if ov < sym_ov:
                sym_ov, sym_ang = ov, a
    if sym_ang > 0:
        olap = sym_ang * 2 - product_len
        info = (
            f"2本対称 {sym_ang}mm×2  被りなし(差{olap}mm)" if olap <= 50
            else f"2本対称 {sym_ang}mm×2  被り{olap}mm"
        )
        return AngleResult(
            count=2, angle1=sym_ang, angle2=sym_ang, angles=[sym_ang, sym_ang],
            total_len=sym_ang * 2, overlap=olap, need_cut=False, info=info,
        )

    # ── Step3②: 異サイズ2本(被り最小 = 準対称) ──
    best_asym_ov = 999999
    asym1 = asym2 = 0
    found_asym = False
    for i, ui in enumerate(u):
        if min_len > 0 and ui < min_len:
            continue
        for uj in u[i:]:
            if min_len > 0 and uj < min_len:
                continue
            tot = ui + uj
            if tot < product_len:
                continue
            ov = tot - product_len
            if ov < best_asym_ov:
                best_asym_ov, asym1, asym2, found_asym = ov, uj, ui, True
    if found_asym:
        return AngleResult(
            count=2, angle1=asym1, angle2=asym2, angles=[asym1, asym2],
            total_len=asym1 + asym2, overlap=best_asym_ov, need_cut=False,
            info=f"2本 {asym1}+{asym2}mm  被り{best_asym_ov}mm",
        )

    # ── Step3c: 3本以上 対称配置(奇数=両サイド先行+中央、偶数=均等) ──
    max_pcs = max_pieces(product_len)
    for n_pcs in range(3, max_pcs + 1):
        pc_target = product_len // n_pcs
        if min_len > 0 and pc_target < min_len:
            pc_target = min_len

        pc_ang, pc_ov = 0, 999999
        for a in u:
            if a >= pc_target and (min_len == 0 or a >= min_len):
                ov = a - pc_target
                if ov < pc_ov:
                    pc_ov, pc_ang = ov, a
        if pc_ang == 0:
            continue

        if n_pcs % 2 == 1:
            sp_pairs = (n_pcs - 1) // 2
            sp_total = pc_ang * sp_pairs * 2
            c_need = product_len - sp_total
            if c_need <= 0:
                continue  # サイドだけでカバー済み

            c_ang, c_ov_best = 0, 999999
            for a in u:
                if a >= c_need and (min_len == 0 or a >= min_len):
                    ov = a - c_need
                    if ov < c_ov_best:
                        c_ov_best, c_ang = ov, a
            if c_ang == 0:
                continue

            arr = [pc_ang] * sp_pairs + [c_ang] + [pc_ang] * sp_pairs
            m_total = sp_total + c_ang
            m_ov = m_total - product_len
            if m_ov < 0:
                continue  # カバー不足

            info = f"{n_pcs}本(両サイド先行) " + " + ".join(f"{v}mm" for v in arr) + f"  被り{m_ov}mm"
            return AngleResult(
                count=n_pcs, angle1=arr[0], angle2=arr[1], angles=arr,
                total_len=m_total, overlap=m_ov, need_cut=False, info=info,
            )
        else:
            tot_even = pc_ang * n_pcs
            if tot_even < product_len:
                continue  # カバー不足
            ov_even = tot_even - product_len
            return AngleResult(
                count=n_pcs, angle1=pc_ang, angle2=pc_ang, angles=[pc_ang] * n_pcs,
                total_len=tot_even, overlap=ov_even, need_cut=False,
                info=f"{n_pcs}本(均等) {pc_ang}mm×{n_pcs}  被り{ov_even}mm",
            )

    # ── Step4: フォールバック(2本・最小被り) ──
    best_fb_ov = 999999
    fb1 = fb2 = 0
    found_fb = False
    for i, ui in enumerate(u):
        for uj in u[i:]:
            tot = ui + uj
            if tot < product_len:
                continue
            ov = tot - product_len
            if ov < best_fb_ov:
                best_fb_ov, fb1, fb2, found_fb = ov, uj, ui, True
    if found_fb:
        return AngleResult(
            count=2, angle1=fb1, angle2=fb2, angles=[fb1, fb2],
            total_len=fb1 + fb2, overlap=best_fb_ov, need_cut=False,
            info=f"2本(FB) {fb1}+{fb2}mm  被り{best_fb_ov}mm",
        )

    return AngleResult(info="該当なし")


# ------------------------------------------------------------------
# DB連携ヘルパー
# ------------------------------------------------------------------
def get_leg_count(conn: sqlite3.Connection, pallet_width: int, pallet_length: int) -> int:
    """VBA `GetLegCount` の移植。PalletMasterから幅+丈が一致する行の脚数を返す(無ければ0)。"""
    row = db.fetch_one(
        conn, "SELECT 脚数 FROM PalletMaster WHERE 幅 = ? AND 丈 = ? LIMIT 1",
        (pallet_width, pallet_length), caller_name="get_leg_count",
    )
    return int(row["脚数"]) if row and row["脚数"] is not None else 0


def load_all_angle_lengths(conn: sqlite3.Connection) -> list[int]:
    """VBA `LoadAllAngleLengths` の移植。CornerboardMasterの全アングル丈(重複排除・昇順)。

    該当データが無い場合はVBA版のフォールバック `{0}` を踏襲する。
    """
    rows = db.fetch_all(
        conn, "SELECT DISTINCT アングル丈 FROM CornerboardMaster WHERE アングル丈 > 0 ORDER BY アングル丈",
        caller_name="load_all_angle_lengths",
    )
    if not rows:
        return [0]
    return [int(r["アングル丈"]) for r in rows]
