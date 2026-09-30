"""パレット自動選定 ── 16パス(2山積は20パス)の総当たり

業界カテゴリ(特定業界 → 一般 → タイト → 全面 [→ 丈2山])×
許容差(厳密 → ±5mm)×向き(通常 → 回転)の順に試し、**最初に候補が
1件以上出たパスの中で面積最小**のものを採る。

どのパスも該当しなければ、条件を大きく緩めた強制フォールバックを
1回だけ試す。

**落ちた理由をログに残すのが要点。** 「なぜこのパレットが出ないのか」を
現場が自分で追えるよう、パスごとに惜しかった上位3件と、2山積で桁数
だけが原因のものを全件出す。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import config, db
from . import special_packaging as spk
from .logging_utils import get_logger
from .pallet_common import (MAX_NEAR_MISS_LOGS, AutoSelectPalletResult,
                            PalletSizeRow, _build_pass_defs, _category_ok,
                            _fit_range_inverted, _fit_range_ok, _is_numeric,
                            _keta_ok, _pass_tag, _physically_fits,
                            _reject_reason, _row_to_pallet_size_row,
                            _search_dims, _size_ok, _thickness_5x10_ok,
                            _two_stack_ok, build_matrix_suppress_map,
                            is_suppressed_matrix, unit_allowed)
from .user_log import UserLog, tag_area

log = get_logger("board_selection.pallet_auto")

@tag_area("パレット")
def _listable(row: sqlite3.Row, *, ex_only_mode: bool, show_all: bool,
              last_hosozai: str) -> bool:
    """パレット一覧に出せる行か。**一覧に出せないパレットを自動で決めない。**

    VBA で直した不具合: 自動検索の候補選びは単位を見ておらず、一覧には
    出ない単位(上下共用でないときの「枚」など)のパレットを決めていた。
    一覧と同じ規則(EX と `unit_allowed`)で候補を絞る。
    """
    is_ex = "EX" in (row["記号"] or "").strip().upper()
    if ex_only_mode:
        return is_ex
    # 「EXまで表示」が外すのはEX除外だけ。**単位の条件は残す**(現場の指示)
    return (show_all or not is_ex) and unit_allowed(row["単位"], last_hosozai)


def auto_select_pallet(
    conn: sqlite3.Connection,
    *,
    product_width_text: str,
    product_length_text: str,
    two_stack: bool = False,
    show_all: bool = False,
    ex_only: bool = False,
    is_ex_order: bool = False,
    last_hosozai: str = "",
    manufactured_thickness: Optional[float] = None,
    user_log: Optional[UserLog] = None,
    is_1p1185_mode: bool = False,
) -> AutoSelectPalletResult:
    """VBA `btnAutoSelectPallet_Click`(製品サイズ入力済み時)の移植。

    業界カテゴリ(特定業界/一般/タイト/全面 [/2山積時は丈2山])×
    許容差(厳密/±5mm)×向き(通常/回転)の16パス(2山積モード時は+4パスの
    計20パス)を優先順位順に試し、最初に候補が1件以上見つかったパスの
    中で面積最小のものを採用する。どのパスも該当しなければ、フィルタを
    大幅に緩めた最終フォールバック探索を1回だけ試みる。

    【移植で変更した点】
        - 元VBAは「決定した組み合わせを再度リスト用に緩い許容差で
          再検索し、リストボックスの該当行を選択する」という追加処理を
          行っていたが、これはリストUIに行を事前選択させるためだけの
          処理であり、稀に決定値とリスト再検索結果が食い違う既知の
          不具合があった。Python版はリストボックスへの事前選択という
          概念自体が無い(呼び出し側が決定値をそのまま使う)ため、この
          処理ごと不要になり、不具合も自然に解消されている。
        - フォールバック探索時のパス名表示は、元VBAでは直前に失敗した
          パスの名前が誤って使い回されるバグがあったが、Python版では
          素直に「強制フォールバック」と表示する。
        - フォールバックで**どれを採るか**も変えてある。元VBAは条件に
          合った**最初の1件**(管理番号順)で打ち切っていた。こちらは
          本探索と同じく**面積最小**を採る ── マスタの並び順で結果が
          変わると、同じ製品サイズで昨日と違うパレットが出ても説明が
          付かない。
        - フォールバックの桁数条件も、元VBAはスカシを外していなかった。
          スカシで奇数桁を求めない理由(桁間隔が狭い)は本探索と同じなので、
          こちらは**フォールバックでもスカシを外す**。

    `is_1p1185_mode` がTrueのとき、探索対象を業界=タイト・幅丈=1300x1300
    のみに絞る(`special_packaging.reject_1p1185`)。この絞り込みは
    候補行そのものから外すので、フォールバック探索にも自然に及ぶ。
    """
    ulog = user_log if user_log is not None else UserLog()  # 未指定なら捨てバッファ

    if not (_is_numeric(product_width_text) and _is_numeric(product_length_text)):
        return AutoSelectPalletResult(False, "製品サイズが不正です")
    pw, pl = int(float(product_width_text)), int(float(product_length_text))
    if pw == 0 and pl == 0:
        return AutoSelectPalletResult(False, "製品サイズが不正です")

    ulog.clear()
    ulog.log("パレット検索を開始します..." + ("【2山モード】" if two_stack else ""), emphasis=True)
    ulog.log(f"製品サイズ: {pw} x {pl}")
    if two_stack:
        ulog.log(f"幅2山: 幅×2={pw * 2} (通常) / 丈×2={pl * 2} (回転) ※桁数奇数")
        ulog.log(f"丈2山: 丈×2={pl * 2} (通常) / 幅×2={pw * 2} (回転) ※スカシ限定")
    if manufactured_thickness is not None:
        ulog.log(f"製造板厚: {manufactured_thickness}")
    else:
        ulog.log("製造板厚: 未取得（Lot未検索）")

    rows = db.fetch_all(conn, "SELECT * FROM PalletMaster ORDER BY 管理番号", caller_name="auto_select_pallet") or []
    suppress_map = build_matrix_suppress_map(rows)
    candidate_rows = [
        r for r in rows if r["幅"] and r["丈"] and _fit_range_ok(r)
        and not spk.reject_1p1185(is_1p1185_mode, r["業界"], r["幅"], r["丈"])
        and not is_suppressed_matrix(suppress_map, r["幅"], r["丈"], r["コード"])
    ]
    if is_1p1185_mode:
        # 1P1185モード: 業界=タイト かつ 1300x1300 のみが探索対象になる。
        # 特定業界/一般/全面のパスは自然に候補ゼロになるので、VBAの
        # パス番号ジャンプ(9〜12,17〜20のみ実行)を再現する必要はない
        ulog.log("【1P1185モード】業界=タイト・1300x1300限定で検索します", emphasis=True)

    # 探索に入る前に落ちている行を先に知らせる(「なんで検索に乗らないの?」対策)。
    # ここはVBAには無い追加ログ。
    skipped = len(rows) - len(candidate_rows)
    ulog.log(f"パレットマスタ: {len(rows)}件中 {len(candidate_rows)}件を探索対象にします"
             + (f" (幅/丈または適合範囲が未設定の{skipped}件を除外)" if skipped else ""))
    inverted = sum(1 for r in candidate_rows if _fit_range_inverted(r))
    if inverted:
        ulog.log(f"  ※適合範囲がマスタ側で逆転している行が{inverted}件あります"
                 f"(この行は何を検索してもヒットしません。マスタ修正が必要)")

    ex_only_mode = is_ex_order and ex_only
    max_pass = 20 if two_stack else 16
    # 適合範囲と現物サイズが食い違う行。1回の検索で1サイズにつき1度だけ知らせる
    reported_bad_master: set[tuple] = set()

    for pass_def in _build_pass_defs(max_pass):
        search_w, search_l = _search_dims(pass_def.orientation, pw, pl, two_stack)
        tag = _pass_tag(pass_def, two_stack)
        ulog.log(f"Pass {pass_def.number}: {pass_def.label}{tag} "
                 f"(W={search_w} L={search_l} tol=±{pass_def.tolerance})")

        candidates: list[tuple[sqlite3.Row, bool]] = []
        rejects: list[tuple[int, str]] = []       # (惜しさ, 表示文字列)
        keta_rejects: list[str] = []              # 桁数偶数だけで落ちたもの(全件出す)

        for row in candidate_rows:
            industry = row["業界"] or "一般"
            symbol = (row["記号"] or "").strip()
            is_ex = "EX" in symbol.upper()

            # EXフィルタで落ちた分はVBA同様ログに出さない(件数が多すぎるため)
            if not _listable(row, ex_only_mode=ex_only_mode, show_all=show_all,
                             last_hosozai=last_hosozai):
                continue

            size_ok = _size_ok(row, search_w, search_l, pass_def.tolerance)
            type_ok = _category_ok(pass_def, industry)
            keta_ok = _keta_ok(pass_def, two_stack, industry, row["桁数"], row["脚数"])
            # 適合範囲が何と書いてあっても、現物に載らないものは候補にしない
            fits = _physically_fits(row, search_w, search_l)

            if size_ok and not fits:
                # 適合範囲が現物サイズより広い行。黙って落とすと
                # 「なぜ出ないのか」が分からないので理由を出す。
                # 同じサイズは何パスで当たっても1回だけ出す(ログが埋まるため)
                key = (row["幅"], row["丈"])
                if key not in reported_bad_master:
                    reported_bad_master.add(key)
                    ulog.log(f"  ×除外: {row['幅']}x{row['丈']} "
                             f"(製品{search_w}x{search_l}が現物からはみ出す"
                             f"→適合範囲が現物より広い。"
                             f"設定画面の「適合範囲を再計算」で直ります)")

            if not (size_ok and type_ok and keta_ok and fits):
                diff = abs(row["幅"] - search_w) + abs(row["丈"] - search_l)
                reason = _reject_reason(
                    row, pass_def, two_stack, industry,
                    size_ok=size_ok, type_ok=type_ok, keta_ok=keta_ok,
                    fits=fits, search_w=search_w, search_l=search_l)
                rejects.append((diff, f"{row['幅']}x{row['丈']} → {reason}"))
                # サイズ・属性はOKで桁数偶数**だけ**が原因のものは別枠で全件出す
                # (「惜しい」上位3件に埋もれて見えなくなるのを防ぐ)。
                #
                # **`keta_ok` も見る。** ここに来る行は size/type/keta/fits の
                # どれかが落ちている。size・typeがOKでも落ちた理由が
                # `fits`(現物に載らない)のことがあり、`keta_ok` を見ないと
                # その行にまで「桁数偶数のため2山不可」と書いてしまう ──
                # 桁数は奇数なのに桁数のせいだと読まされ、マスタの桁数を
                # 直しに行くことになる
                if (two_stack and pass_def.number <= 16
                        and size_ok and type_ok and not keta_ok):
                    keta_rejects.append(
                        f"{row['幅']}x{row['丈']} (桁数={row['桁数']}/偶数のため2山不可)")
                continue

            needs_warning = False
            if industry == "5×10":
                ok, needs_warning = _thickness_5x10_ok(symbol, manufactured_thickness)
                if not ok:
                    if manufactured_thickness is not None and manufactured_thickness <= 13.0:
                        ulog.log(f"  △スキップ: {row['幅']}x{row['丈']} "
                                 f"(5×10/板厚{manufactured_thickness}≦13.0 記号={symbol} "
                                 f"→ 強度UP以外は除外)")
                    else:
                        ulog.log(f"  △スキップ: {row['幅']}x{row['丈']} "
                                 f"(5×10/板厚{manufactured_thickness}>13.0 "
                                 f"→ 強度UPは薄板専用のため除外)")
                    continue

            candidates.append((row, needs_warning))
            ulog.log(f"  候補: {row['幅']}x{row['丈']} ({industry})")

        # 候補ゼロのときだけ「惜しかった」上位3件を出す
        if not candidates and rejects:
            for _, text in sorted(rejects, key=lambda r: r[0])[:MAX_NEAR_MISS_LOGS]:
                ulog.log(f"  ×惜しい: {text}")

        # 桁数偶数だけで落ちた分は候補の有無に関わらず全件出す
        if keta_rejects:
            ulog.log(f"  △桁数偶数のため2山除外({len(keta_rejects)}件):")
            for text in keta_rejects:
                ulog.log(f"    ×{text}")

        if not candidates:
            ulog.log("  → 該当なし")
            continue

        best_row, best_warning = min(candidates, key=lambda c: c[0]["幅"] * c[0]["丈"])
        industry = best_row["業界"] or ""
        detail = ""
        if two_stack and pass_def.number <= 16:
            detail = f" / 桁数={best_row['桁数']}"
        suffix = f" ← {len(candidates)}件中最小" if len(candidates) > 1 else ""
        ulog.log(f"  決定(最小面積): {best_row['幅']}x{best_row['丈']} "
                 f"({industry}{detail}){suffix}", emphasis=True)
        if best_warning and industry == "5×10":
            ulog.log("板厚13.0mmに注意（板厚情報なし）")
        rotated = pass_def.number % 2 == 0
        ulog.log(f"Pass {pass_def.number} で決定" + ("（製品回転）" if rotated else ""),
                 emphasis=True)
        # 2山の向きを記録する(Pass1〜16=幅2山 / Pass17〜20=丈2山)。
        # 回転して決まった場合も、回転後の製品サイズに対して同じ向きで
        # 2倍すれば、検索したときの大きさと一致する
        stack_dir = ""
        if two_stack:
            stack_dir = "幅" if pass_def.number <= 16 else "丈"
            ulog.log(f"2山の向き: {stack_dir}方向（製品サイズの「セット」で"
                     f"{stack_dir}を2倍して扱います）", emphasis=True)

        return AutoSelectPalletResult(
            ok=True,
            message=f"パレットを自動選定しました({pass_def.label})",
            width=best_row["幅"], length=best_row["丈"],
            industry=industry, symbol=(best_row["記号"] or "").strip(),
            pass_label=pass_def.label, rotated=rotated,
            needs_thickness_warning=best_warning,
            search_width=search_w, search_length=search_l,
            stack_dir=stack_dir,
        )

    # 強制フォールバック(元VBA仕様: フィルタ大幅緩和・1回のみ・回転フラグは立てない)
    ulog.log("適合なし → 強制入替えサイズ検索中...", emphasis=True)
    fb_w, fb_l = (pw * 2, pl) if two_stack else (pl, pw)
    fb_candidates = []
    for row in candidate_rows:
        industry = row["業界"] or "一般"
        # 一覧に出せないパレット(EX・単位)は、強制入替えでも決めない
        if not _listable(row, ex_only_mode=ex_only_mode, show_all=show_all,
                         last_hosozai=last_hosozai):
            continue
        if not (row["巾適合min"] <= fb_w <= row["巾適合max"] and row["丈適合min"] <= fb_l <= row["丈適合max"]):
            continue
        # 最終フォールバックでも現物に載らないものは出さない
        if not _physically_fits(row, fb_w, fb_l):
            continue
        if two_stack and industry != "スカシ":
            keta = row["桁数"]
            if not (keta > 0 and keta % 2 == 1):
                continue
        fb_candidates.append(row)

    if fb_candidates:
        best_row = min(fb_candidates, key=lambda r: r["幅"] * r["丈"])
        ulog.log(f"【決定】強制入替えで適合: {best_row['幅']} x {best_row['丈']}", emphasis=True)
        return AutoSelectPalletResult(
            ok=True, message="パレットを自動選定しました(強制フォールバック)",
            width=best_row["幅"], length=best_row["丈"],
            industry=best_row["業界"] or "", symbol=(best_row["記号"] or "").strip(),
            pass_label="強制フォールバック", rotated=False,
            search_width=fb_w, search_length=fb_l,
            # 強制入替えの2山は幅方向(検索条件が「製品幅×2 × 製品丈」のため)
            stack_dir="幅" if two_stack else "",
        )

    ulog.log("適合パレットなし", emphasis=True)
    return AutoSelectPalletResult(False, "適合するパレットが見つかりませんでした")
