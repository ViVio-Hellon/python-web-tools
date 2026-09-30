"""パレット一覧の絞り込み ── 何を出すかを決める3つの経路

    list_pallet_sizes        全件(単位とEXの絞り込みだけ)
    search_pallet_direct     パレット寸法の前後50mm(製品サイズが空のとき)
    list_pallets_for_product 製品サイズに収まるもの(「載るか」を見る)

**3つとも同じ判断(`pallet_common`)を使う。** 書き写していたせいで、
製品サイズを入れた後の一覧だけ単位の規則が抜け落ちていたことがある。

`list_pallets_by_product_dims` は、製品 幅・丈を**打っている最中**の
振り分け。両方そろえば「載るか」、片方だけなら寸法の近似、どちらも
空なら全件。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import config, db, material_service
from . import special_packaging as spk
from .logging_utils import get_logger
from .pallet_common import (KIND_LEN2, KIND_WIDTH2, MAX_NEAR_MISS_LOGS,
                            PASS_LOOSE_TOLERANCE,
                            UNITS_DEFAULT, UNITS_WITH_HOSOZAI,
                            AutoSelectPalletResult,
                            PalletSizeRow, _build_pass_defs, _category_ok,
                            _fit_range_inverted, _fit_range_ok, _is_numeric,
                            _keta_ok, _pass_tag, _physically_fits,
                            _reject_reason, _row_to_pallet_size_row,
                            _search_dims, _size_ok, _thickness_5x10_ok,
                            _two_stack_ok, build_matrix_suppress_map,
                            is_suppressed_matrix, unit_allowed)
from .user_log import RejectLog, UserLog, tag_area

log = get_logger("board_selection.pallet_list")

def find_pallet_row(conn: sqlite3.Connection, width: int, length: int, symbol: str) -> Optional[PalletSizeRow]:
    """幅・丈・記号からPalletMasterの1行を引く(コード・単位を知りたいだけの単純参照)。

    一覧表示側のフィルタ(単位/EX/製品サイズ適合)には関係なく、画面に
    表示されている行の実体をそのまま引けるようにする(クリック時の
    単位・コード表示 = VBA `DynamicTip` 用)。
    """
    rows = db.fetch_all(
        conn, "SELECT * FROM PalletMaster WHERE 幅 = ? AND 丈 = ?",
        (width, length), caller_name="find_pallet_row") or []
    for row in rows:
        if (row["記号"] or "").strip() == symbol:
            return _row_to_pallet_size_row(row)
    return None

def list_pallet_sizes(
    conn: sqlite3.Connection,
    *,
    show_all: bool = False,
    ex_only: bool = False,
    is_ex_order: bool = False,
    last_hosozai: str = "",
    is_1p1185_mode: bool = False,
) -> list[PalletSizeRow]:
    """VBA `InitializePalletSizeList`/`FilterPalletList` の移植(統合版)。

    フィルタ規則(元VBAと同一):
        - 幅・丈が0の行は常に除外
        - EXオンリーモード(is_ex_order かつ ex_only)は 記号にEXを含む行のみ
        - それ以外は「単位フィルタ」を必ず適用し、show_all=False なら「EX除外」も:
            単位フィルタ: `unit_allowed`(保護材が確定していれば
              台/枚、それ以外は台のみ)。**「EXまで表示」でも外さない**
              (現場の指示。以前は show_all=True で単位の条件まで外れていた)
            EX除外: 記号に"EX"を含む行(大文字小文字問わず)は除外
        - is_1p1185_mode=Trueなら業界=タイト・幅=1300・丈=1300以外を除外
        - 幅|丈が同じで実コードを持つ行がある「単価表のマトリックス」行は除外
    """
    rows = db.fetch_all(conn, "SELECT * FROM PalletMaster ORDER BY 幅", caller_name="list_pallet_sizes") or []
    ex_only_mode = is_ex_order and ex_only
    suppress_map = build_matrix_suppress_map(rows)

    result: list[PalletSizeRow] = []
    for row in rows:
        w, l = row["幅"], row["丈"]
        if not w or not l:
            continue
        if spk.reject_1p1185(is_1p1185_mode, row["業界"], w, l):
            continue
        if is_suppressed_matrix(suppress_map, w, l, row["コード"]):
            continue
        symbol = (row["記号"] or "").strip()
        is_ex = "EX" in symbol.upper()

        if ex_only_mode:
            if not is_ex:
                continue
        elif not unit_allowed(row["単位"], last_hosozai) or (is_ex and not show_all):
            # 「EXまで表示」が外すのはEX除外だけ。単位の条件は残す
            continue

        result.append(_row_to_pallet_size_row(row))
    return result

def search_pallet_direct(
    conn: sqlite3.Connection,
    *,
    pallet_width_text: str = "",
    pallet_length_text: str = "",
    show_all: bool = False,
    last_hosozai: str = "",
    is_1p1185_mode: bool = False,
) -> list[PalletSizeRow]:
    """VBA `btnAutoSelectPallet_Click`の「直接検索モード」の移植。

    製品サイズが未入力のときに使う、パレット自身の幅・丈を対象とした
    ±50mmの単純検索。EXオンリーモードは(元VBA仕様どおり)考慮しない。
    """
    pw = int(float(pallet_width_text)) if _is_numeric(pallet_width_text) else 0
    pl = int(float(pallet_length_text)) if _is_numeric(pallet_length_text) else 0

    rows = db.fetch_all(conn, "SELECT * FROM PalletMaster ORDER BY 管理番号", caller_name="search_pallet_direct") or []
    suppress_map = build_matrix_suppress_map(rows)
    result: list[PalletSizeRow] = []
    for row in rows:
        w, l = row["幅"], row["丈"]
        if not w or not l:
            continue
        if spk.reject_1p1185(is_1p1185_mode, row["業界"], w, l):
            continue
        if is_suppressed_matrix(suppress_map, w, l, row["コード"]):
            continue
        if pw and abs(w - pw) > config.SEARCH_RANGE_TOLERANCE:
            continue
        if pl and abs(l - pl) > config.SEARCH_RANGE_TOLERANCE:
            continue

        symbol = (row["記号"] or "").strip()
        is_ex = "EX" in symbol.upper()
        # 「EXまで表示」が外すのはEX除外だけ。単位の条件は残す
        if not unit_allowed(row["単位"], last_hosozai) or (is_ex and not show_all):
            continue

        result.append(_row_to_pallet_size_row(row))
    return result


@tag_area("パレット")
def list_pallets_for_product(
    conn: sqlite3.Connection,
    *,
    product_width: int,
    product_length: int,
    show_all: bool = False,
    ex_only: bool = False,
    is_ex_order: bool = False,
    two_stack: bool = False,
    last_hosozai: str = "",
    manufactured_thickness: Optional[float] = None,
    user_log: Optional[UserLog] = None,
    is_1p1185_mode: bool = False,
) -> list[PalletSizeRow]:
    """製品サイズが適合範囲に収まるパレットだけを返す(検索結果リスト表示用)。

    `auto_select_pallet` は最終的に1件を決めるだけで、リスト表示用の
    候補集合は返さない(VBAの「決定値でリストを選択し直す」処理が
    不安定だったため、意図的に持たせていない)。ただし検索結果として
    「どれが候補なのか」を画面のリストに映すこと自体は別の話なので、
    ここで独立に候補集合を作る(通常向き・回転の両方をあたる)。

    `ex_only`/`is_ex_order` は `list_pallet_sizes` と同じ意味づけ
    (EXオンリーはEX受注のときだけ効く)。この2つを見ていなかったため、
    EX受注でEXオンリーを付けたままパレット検索すると、絞り込み結果に
    EX以外の行が混ざってしまっていた。

    【バグ修正】5×10業界の板厚フィルタ(`_thickness_5x10_ok`、
    `auto_select_pallet` は適用済み)がここには無く、自動選定なら
    除外されるはずの強度UP(または通常5×10)が一覧には出てしまって
    いた。自動選定の結果と一覧の中身が食い違わないよう、ここにも
    同じ判定を掛ける。
    【2山積】`two_stack` が立っているときは、製品を2つ積む前提の寸法
    (`_search_dims` と同じく片側を2倍にしたもの)でも当たりを見る。
    これを見ていなかったため、**2山積を押しても一覧が何も変わらず**、
    「押しても検索しなおさない」と受け取られていた(現場の声)。

    【ログ】`user_log` を渡すと、**外した行とその理由**を書く。
    決まったことは画面を見れば分かるが、「なぜこのパレットが候補から
    外れたのか」は記録にしか残らない(現場の声:「決定事項は見れば
    わかる。必要なのは経緯」)。

    【除外ログの上限】マスタの件数が多いと「×除外」の行が際限なく
    伸び、本当に必要な「○候補」の行が埋もれてしまう(現場の声)。
    上位`RejectLog`の既定件数だけそのまま出し、残りは件数にまとめる。
    """
    # auto_select_pallet の各パスは厳密/±5mmの許容差を使う。ここも同じ
    # 5mmにして、決定されたパレットが検索結果から漏れないようにする
    tol = PASS_LOOSE_TOLERANCE
    ulog = user_log if user_log is not None else UserLog()   # 未指定なら捨てバッファ
    rejects = RejectLog(ulog)
    rows = db.fetch_all(conn, "SELECT * FROM PalletMaster ORDER BY 管理番号",
                        caller_name="list_pallets_for_product") or []
    ex_only_mode = is_ex_order and ex_only
    suppress_map = build_matrix_suppress_map(rows)

    ulog.log(f"[パレット絞り込み] 製品 {product_width} x {product_length}"
             + ("【2山積】" if two_stack else ""), emphasis=True)
    ulog.log(f"  マスタ {len(rows)}件 / 許容差 ±{tol}mm"
             f" / EX表示={'する' if show_all else 'しない'}"
             + ("  EXオンリー" if ex_only_mode else ""))

    # 当たりを見る向き。**上から順に見て、最初に当たったものを採る** ──
    # 順番を変えると「回さなくても載る行」が回転扱いになる。
    #
    # **2山積のときは通常寸法を見ない。** `auto_select_pallet` は2山モードで
    # パス1〜16の寸法そのものを片側2倍に差し替える(`_search_dims`)ので、
    # 通常寸法は1パスも走らない。ここで通常寸法も混ぜると、**任意で2山を
    # 選んだのに通常の候補まで並んで邪魔になる**(現場の声)。
    # 組み合わせも `_search_dims` にそろえる(幅2山=片側2倍、丈2山=丈2倍)。
    if two_stack:
        tries: list[tuple[int, int, bool, str]] = [
            (product_width * 2, product_length, False, KIND_WIDTH2),
            (product_length * 2, product_width, True, KIND_WIDTH2),
            (product_width, product_length * 2, False, KIND_LEN2),
            (product_length, product_width * 2, True, KIND_LEN2),
        ]
    else:
        tries = [
            (product_width, product_length, False, ""),      # 通常
            (product_length, product_width, True, ""),       # 回転
        ]

    result: list[PalletSizeRow] = []
    for row in rows:
        w, l = row["幅"], row["丈"]
        label = f"{w}x{l}"
        if not w or not l:
            continue                       # 寸法が入っていない行は数えない
        if spk.reject_1p1185(is_1p1185_mode, row["業界"], w, l):
            rejects.log(f"  ×除外: {label} 1P1185モード対象外です")
            continue
        if is_suppressed_matrix(suppress_map, w, l, row["コード"]):
            rejects.log(f"  ×除外: {label} 単価表のマトリックス(実コード行と重複)です")
            continue
        if not _fit_range_ok(row) or _fit_range_inverted(row):
            rejects.log(f"  ×除外: {label} 適合範囲がマスタ側で不正です")
            continue

        industry = row["業界"] or ""
        # **2山積は誰にでも積めるわけではない。** 幅2山は桁が奇数か
        # スカシ、丈2山はスカシ/タイトで脚数3以上 ── `auto_select_pallet`
        # の `_category_ok` / `_keta_ok` と同じ条件。ここに無かったため、
        # 積めないパレットまで2山の候補に出ていた(現場の指摘)
        allowed = [(sw, sl, rot, kind) for sw, sl, rot, kind in tries
                   if _two_stack_ok(kind, industry, row["桁数"], row["脚数"])]
        if two_stack and not allowed:
            rejects.log(f"  ×除外: {label} 2山積の条件に合いません"
                     f"(業界 {industry or '(なし)'} /"
                     f" 桁数 {row['桁数']} / 脚数 {row['脚数']})")
            continue

        hit = next(((sw, sl, rot, kind) for sw, sl, rot, kind in allowed
                    if _size_ok(row, sw, sl, tol)), None)
        if hit is None:
            rejects.log(f"  ×除外: {label} 適合範囲外"
                     f"(巾{row['巾適合min']}〜{row['巾適合max']} /"
                     f" 丈{row['丈適合min']}〜{row['丈適合max']})")
            continue
        search_w, search_l, rotated, stacked = hit

        if not _physically_fits(row, search_w, search_l):
            # 適合範囲には入るのに現物には載らない。**マスタの適合範囲が
            # 現物より広い**行で起きる ── 見分けが付かないと直せない
            rejects.log(f"  ×除外: {label} 現物に載りません"
                     f"(製品 {search_w}x{search_l})")
            continue

        symbol = (row["記号"] or "").strip()
        is_ex = "EX" in symbol.upper()
        if ex_only_mode:
            if not is_ex:
                rejects.log(f"  ×除外: {label} EXオンリーですがEXではありません")
                continue
        elif not show_all and is_ex:
            rejects.log(f"  ×除外: {label} EX({symbol})なので既定では出しません")
            continue

        # **単位で絞る(`unit_allowed`)。** ここだけこの規則が抜けており、
        # 製品サイズを入れたあとの一覧は、サイズで当たる行が単位に
        # 関わらず全部出ていた(現場の声:「単位"台"での絞り込みのはずが
        # サイズでヒットするものすべて表示している」)
        # 「EXまで表示」でも外さない(現場の指示)。
        if not unit_allowed(row["単位"], last_hosozai):
            rejects.log(f"  ×除外: {label} 単位が{row['単位'] or '(なし)'}です"
                        f"(出すのは{'/'.join(UNITS_WITH_HOSOZAI if last_hosozai and last_hosozai not in (material_service.HOSOZAI_ANGLE, '一致なし') else UNITS_DEFAULT)})")
            continue

        if industry == "5×10":
            ok, _needs_warning = _thickness_5x10_ok(symbol, manufactured_thickness)
            if not ok:
                rejects.log(f"  ×除外: {label} 5×10の板厚条件に合いません"
                         f"(記号 {symbol} / 板厚 {manufactured_thickness})")
                continue

        exact = _size_ok(row, search_w, search_l, 0)
        found = _row_to_pallet_size_row(row)
        found.rotated = rotated
        found.exact = exact
        found.tolerance = 0 if exact else tol
        found.two_stack = stacked
        result.append(found)
        ulog.log(f"  ○候補: {label}"
                 + ("(回転)" if rotated else "")
                 + (f"({stacked})" if stacked else "")
                 + ("(厳密)" if exact else f"(+{tol}mm)"))

    rejects.flush()
    ulog.log(f"[パレット絞り込み] {len(result)}件が候補です", emphasis=True)
    return result


def list_pallets_by_product_dims(
    conn: sqlite3.Connection,
    *,
    product_width_text: str = "",
    product_length_text: str = "",
    show_all: bool = False,
    ex_only: bool = False,
    is_ex_order: bool = False,
    two_stack: bool = False,
    last_hosozai: str = "",
    manufactured_thickness: Optional[float] = None,
    user_log: Optional[UserLog] = None,
    is_1p1185_mode: bool = False,
) -> list[PalletSizeRow]:
    """製品 幅・丈を**入力している最中**の一覧絞り込み(確定前)。

    「載るか」の判定(`list_pallets_for_product`)は幅・丈の両方が
    無いと成り立たない(1辺だけでは向きも当たりも決められない)。
    片方しか打っていない段階では、代わりに**パレット自身の幅・丈が
    その値に近いもの**を見せる(`search_pallet_direct` と同じ
    ±{SEARCH_RANGE_TOLERANCE}mm の単純近似)。両方そろえば厳密な
    「載るか」判定に切り替わる。どちらも空なら通常の全件一覧。
    """
    has_width = _is_numeric(product_width_text)
    has_length = _is_numeric(product_length_text)
    if has_width and has_length:
        return list_pallets_for_product(
            conn, product_width=int(float(product_width_text)),
            product_length=int(float(product_length_text)),
            show_all=show_all, ex_only=ex_only, is_ex_order=is_ex_order,
            two_stack=two_stack, last_hosozai=last_hosozai,
            manufactured_thickness=manufactured_thickness, user_log=user_log,
            is_1p1185_mode=is_1p1185_mode)
    if has_width or has_length:
        return search_pallet_direct(
            conn, pallet_width_text=product_width_text,
            pallet_length_text=product_length_text,
            show_all=show_all, last_hosozai=last_hosozai,
            is_1p1185_mode=is_1p1185_mode)
    return list_pallet_sizes(
        conn, show_all=show_all, ex_only=ex_only, is_ex_order=is_ex_order,
        last_hosozai=last_hosozai, is_1p1185_mode=is_1p1185_mode)
