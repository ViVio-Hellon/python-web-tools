"""決め打ちの値(DBで管理していない値)の一覧 `docs/決め打ちの値一覧.md` を作り直す。

【なぜ要るのか】
許容値などの数字はマスタ(DB)ではなくプログラムに書いてあり、
増えすぎてどれが何mmだったか思い出せなくなった(現場の声)。
説明書に手で写すと、プログラムを直した日にまた食い違う。

【どう作るか】
下の `SECTIONS` に「どのモジュールのどの名前が、何の値か」だけを書き、
**値はプログラムから読む**。値を直したらこのスクリプトを流す:

    python3 scripts/make_constants_doc.py

流し忘れは `tests/test_app_config.py` が見る(一覧がプログラムと食い違うと落ちる)。
新しい決め打ちの値を足したときは、ここにも1行足す。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "docs" / "決め打ちの値一覧.md"
sys.path.insert(0, str(ROOT))

# (見出し, 説明, [(モジュール, 名前, 何の値か, 単位)])
SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str, str, str], ...]], ...] = (
    ("パレット検索・一覧",
     "製品サイズに合うパレットを探すときの値。適合範囲(巾適合min〜max など)はマスタにあり、ここには無い。",
     (
         ("pallet_common", "PASS_LOOSE_TOLERANCE", "厳密で見つからないときに広げる許容差(「±5」のパス)。一覧の絞り込みも同じ", "mm"),
         ("pallet_common", "PHYSICAL_MARGIN", "製品がパレット現物からはみ出してよい量(適合範囲が現物より広い行を弾く)", "mm"),
         ("pallet_common", "PALETTE_OVERHANG_RATIO", "パレットの最大はみ出し係数(パレット寸法×この値)", "倍"),
         ("pallet_common", "LEN2_MIN_LEGS", "丈2山にできるパレットの脚数(これ以上)。幅2山は桁数が奇数(スカシは桁数を問わない)", "本"),
         ("pallet_common", "UNITS_DEFAULT", "一覧に出す単位(通常)。「EXまで表示」でも外さない", ""),
         ("pallet_common", "UNITS_WITH_HOSOZAI", "一覧に出す単位(上下共用=保護材がアングル以外のとき)", ""),
         ("config", "SEARCH_RANGE_TOLERANCE", "パレット寸法で直接探すときの前後の幅", "mm"),
     )),
    ("ボード選定(上用)",
     "上用は保護材なので製品幅より**プラス禁止**。",
     (
         ("board_scoring", "UPPER_WIDTH_TOLERANCE", "上用の幅マイナス許容(製品幅より最大これだけ小さくてよい。両端で半分ずつ)", "mm"),
         ("board_selection_upper", "UPPER_MAIN_MIN_WIDTH_RATIO", "上用の主ボードにできる最小の有効幅(製品幅に対する割合)", "倍"),
         ("board_selection_upper", "UPPER_MAIN_LENGTH_SLACK", "上用の主ボードの枚数を数えるとき製品丈から引く量(丈マイナス許容)", "mm"),
         ("placement_types", "X_OVERHANG_LIMIT", "上用の丈方向のはみ出し許容(配置)", "mm"),
         ("placement_types", "CUT_UPPER_MINUS", "上用を切るときの幅(製品幅からこれを引いた幅で切る)", "mm"),
     )),
    ("ボード選定(下用)",
     "",
     (
         ("board_scoring", "LOWER_WIDTH_TOLERANCE", "下用の幅プラス許容(パレット幅より最大これだけはみ出してよい。両端で半分ずつ)", "mm"),
         ("board_scoring", "LOWER_OVERHANG_Y", "下用を置ける幅の上限(パレット幅×この値)", "倍"),
         ("board_scoring", "PASS1_TOLERANCE", "下用PASS1で幅が合ったとみなす差。これ以下の幅不足は補填しない", "mm"),
     )),
    ("補填(上用・下用共通)",
     "",
     (
         ("board_scoring", "FILL_SIZE_30", "補填ボードの厚み(小)", "mm"),
         ("board_scoring", "FILL_SIZE_50", "補填ボードの厚み(中)", "mm"),
         ("board_scoring", "FILL_SIZE_100", "補填ボードの厚み(大)", "mm"),
         ("board_scoring", "MAX_WIDTH_STRIPS", "幅方向に並べる板の本数の上限(主ボードを含む)", "本"),
         ("board_scoring", "WIDTH_OVERSHOOT_LIMIT", "幅の超過をはじく閾値", "mm"),
         ("board_selection_common", "FILL_OVER_MARGIN", "補填で覆えるか見るときの超過許容幅", "mm"),
         ("board_selection_common", "GAP_FILLER_OVER_MARGIN", "残りの隙間を小型ボードで埋めるときに許す超過", "mm"),
         ("board_selection_common", "FILL_RETRY_LIMIT", "残りの隙間を埋め直す回数の上限", "回"),
         ("board_selection_common", "LENGTH_FILL_THRESHOLD", "丈補填: 丈の残りがこれを超えたら主ボードを1枚増やす(以下なら100mm補填)", "mm"),
         ("board_selection_common", "LENGTH_FILL_BOARD_W", "丈補填に使う板の厚み", "mm"),
         ("board_selection_common", "LENGTH_FILL_COUNT_CAP", "同じサイズを丈補填で積み増せる枚数の上限", "枚"),
         ("board_selection_common", "COVERED_TOLERANCE", "「覆えている」とみなす差", "mm"),
         ("board_selection_common", "MAX_MAIN_BOARD_KINDS", "主ボードとして選べる種類の上限", "種"),
         ("board_selection_common", "LENGTH_CUT_REMAIN_THRESHOLD", "丈カットで「残りはもう切らなくてよい」とみなす長さ", "mm"),
     )),
    ("業界シリーズの決め打ち(1×2・4×8・5×8)",
     "製品サイズがこの範囲なら、決まったボードをそのまま使う。",
     (
         ("board_selection_common", "SC_1X2_W_MIN", "1×2: 製品幅の下限", "mm"),
         ("board_selection_common", "SC_1X2_W_MAX", "1×2: 製品幅の上限", "mm"),
         ("board_selection_common", "SC_1X2_L_MIN", "1×2: 製品丈の下限", "mm"),
         ("board_selection_common", "SC_1X2_L_MAX", "1×2: 製品丈の上限", "mm"),
         ("board_selection_common", "SC_4X8_W_MIN", "4×8: 製品幅の下限", "mm"),
         ("board_selection_common", "SC_4X8_W_MAX", "4×8: 製品幅の上限", "mm"),
         ("board_selection_common", "SC_4X8_L_MIN", "4×8: 製品丈の下限", "mm"),
         ("board_selection_common", "SC_4X8_L_MAX", "4×8: 製品丈の上限", "mm"),
         ("board_selection_common", "SC_5X8_W_MIN", "5×8: 製品幅の下限", "mm"),
         ("board_selection_common", "SC_5X8_W_MAX", "5×8: 製品幅の上限", "mm"),
         ("board_selection_common", "SC_5X8_L_MIN", "5×8: 製品丈の下限", "mm"),
         ("board_selection_common", "SC_5X8_L_MAX", "5×8: 製品丈の上限", "mm"),
     )),
    ("プロテック",
     "",
     (
         ("board_selection_protec", "PROTEC_1P1216_TOLERANCE", "1P1216 の幅マイナス許容(製品幅より最大これだけ小さくてよい)", "mm"),
         ("board_selection_protec", "PROTEC_OTHER_TOLERANCE", "1P1216 以外のプロテックの幅マイナス許容", "mm"),
     )),
    ("狭幅パレット",
     "細い板を幅方向に並べて覆うとき(パレット以下・製品以上)。",
     (
         ("board_selection_narrow", "NARROW_MIN_SHORT_SIDE", "帯にできる板の最小の短辺", "mm"),
         ("board_selection_narrow", "NARROW_LENGTH_FILL_MAX_SHORT", "丈補填に使える薄物の短辺の上限", "mm"),
         ("board_selection_narrow", "NARROW_MAX_LOOPS", "帯を選ぶ回数の上限", "回"),
         ("board_selection_narrow", "WIDE_CUT_MIN_SHORT_SIDE", "カット前提選定で使える板の最小の短辺", "mm"),
     )),
    ("別案(候補変更・敷き詰め方式)",
     "上用(通常)は幅プラス禁止。上下共用の上蓋は下用と同じ扱い。",
     (
         ("tiling_algorithm", "UPPER_W_MINUS", "上用の幅マイナス許容(上限は製品幅−1)", "mm"),
         ("tiling_algorithm", "UPPER_L_MINUS", "上用の丈マイナス許容(業界シリーズと同じ値をシリーズの定数から求める)", "mm"),
         ("tiling_algorithm", "UPPER_L_PLUS", "上用の丈プラス許容", "mm"),
         ("tiling_algorithm", "LOWER_W_MINUS", "下用の幅マイナス許容(製品幅から)", "mm"),
         ("tiling_algorithm", "LOWER_W_PLUS", "下用の幅プラス許容(パレット幅から)", "mm"),
         ("tiling_algorithm", "LOWER_L_MINUS", "下用の丈マイナス許容(製品丈から)", "mm"),
         ("tiling_algorithm", "LOWER_L_RATIO", "下用の丈の上限(パレット丈×この値)", "倍"),
         ("tiling_algorithm", "MAX_BOARDS", "1案の総枚数の上限", "枚"),
         ("tiling_algorithm", "MAX_ROWS", "行数の上限", "行"),
         ("tiling_algorithm", "MAX_PER_ROW", "1行の板の枚数の上限", "枚"),
         ("tiling_algorithm", "MAX_THIN_ROW", "丈補填の行数の上限", "行"),
         ("tiling_algorithm", "MAX_THIN_PER", "1行の細い板の枚数の上限", "枚"),
         ("tiling_algorithm", "MAX_B_EXTRA", "B(種類最小)がA(枚数最小)より多くてよい枚数", "枚"),
         ("tiling_algorithm", "TIER1", "超過の段の区切り(1段目)", "mm"),
         ("tiling_algorithm", "TIER2", "超過の段の区切り(2段目)", "mm"),
     )),
    ("配置",
     "",
     (
         ("placement_types", "GRID_STEP", "置き場所を探す刻み", "mm"),
         ("placement_types", "SNAP_STEP", "既存の板の端に寄せて探す刻み(下用)", "mm"),
         ("placement_types", "FINE_RANGE", "最良位置のまわりを細かく探す範囲(±)", "mm"),
         ("placement_types", "FILL_SHORT_SIDE_LIMIT", "補填かどうかを推し量る短辺の上限", "mm"),
         ("placement_render", "CUT_TOLERANCE", "カット・超過・不足を表示しない差", "mm"),
         ("placement_render", "FILL_STRIP_MAX", "補填の色で塗る板の短辺の上限(これより太い補填は上用・下用の色)", "mm"),
     )),
    ("アングル",
     "",
     (
         ("angle_service", "ANGLE_STEP1_TOL_PCT", "1本で覆うとき許す超過(製品丈に対する割合)", "倍"),
         ("angle_service", "ANGLE_STEP1_MIN_TOL", "1本で覆うとき許す超過の最小", "mm"),
         ("angle_service", "ANGLE_STEP2_MAX_LEN", "カットして使うかどうかの境の長さ", "mm"),
         ("angle_service", "ANGLE_MULTI_THRESHOLD", "最大本数を切り替える製品丈", "mm"),
         ("angle_service", "ANGLE_MAX_PIECES_LONG", "製品丈が上の値以上のときの最大本数", "本"),
         ("angle_service", "ANGLE_MAX_PIECES_SHORT", "製品丈が上の値未満のときの最大本数", "本"),
     )),
    ("疲労度",
     "",
     (
         ("angle_service", "CUT_FAT_MULT", "カット1回の重み", ""),
         ("angle_service", "FAT_MAX_DIST", "距離スコアの分母", ""),
         ("angle_service", "FAT_MAX_LEN", "長さスコアの分母", "mm"),
         ("angle_service", "FAT_SCORE_CAP", "スコアの上限", ""),
         ("angle_service", "FAT_DIST_DEFAULT", "距離が分からないときの距離スコア", ""),
     )),
    ("1P0113 裸梱包",
     "",
     (
         ("special_packaging", "A_THR1", "松板の丈(A値): 製品丈がこれ以下なら A=製品丈", "mm"),
         ("special_packaging", "A_THR2", "松板の丈(A値): 製品丈がこれ以下なら A=下の A_VAL1", "mm"),
         ("special_packaging", "A_VAL1", "(上の範囲の)A値", "mm"),
         ("special_packaging", "A_THR3", "松板の丈(A値): 製品丈がこれ以下なら A=下の A_VAL2", "mm"),
         ("special_packaging", "A_VAL2", "(上の範囲の)A値", "mm"),
         ("special_packaging", "A_VAL3", "製品丈が A_THR3 を超えるときの A値", "mm"),
         ("special_packaging", "KAKUZAI_ATSU", "溝切角材の厚", "mm"),
         ("special_packaging", "KAKUZAI_HABA", "溝切角材の幅", "mm"),
         ("special_packaging", "KAKUZAI_MARGIN", "溝切角材の丈 = 製品幅 + これ", "mm"),
         ("special_packaging", "KAKUZAI_THR1", "角材の本数: 製品丈がこれ以下なら KAKUZAI_CNT1", "mm"),
         ("special_packaging", "KAKUZAI_THR2", "角材の本数: 製品丈がこれ以下なら KAKUZAI_CNT2、超えたら KAKUZAI_CNT3", "mm"),
         ("special_packaging", "KAKUZAI_CNT1", "角材の本数(短い)", "本"),
         ("special_packaging", "KAKUZAI_CNT2", "角材の本数(中)", "本"),
         ("special_packaging", "KAKUZAI_CNT3", "角材の本数(長い)", "本"),
         ("special_packaging", "MATSUITA_ATSU", "松板の厚", "mm"),
         ("special_packaging", "MATSUITA_HABA", "松板の幅", "mm"),
         ("special_packaging", "MATSUITA_CNT", "松板の枚数", "枚"),
     )),
)


def _fmt(value: object) -> str:
    if isinstance(value, (tuple, list)):
        return "・".join(str(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def build() -> str:
    lines = [
        "# 決め打ちの値一覧(DBで管理していない値)",
        "",
        "許容値などのうち、**マスタ(DB)ではなくプログラムに書いてある値**の一覧です。",
        "マスタ管理の画面からは変えられません。変えるときはプログラムを直します。",
        "",
        "この一覧は `scripts/make_constants_doc.py` が**プログラムから値を読んで**作っています",
        "(手で直さない)。プログラムの値を変えたら `python3 scripts/make_constants_doc.py` を",
        "流してください。流し忘れると試験が落ちます。",
        "",
        "「場所」はプログラムの中の名前です(`packaging_tool/モジュール.py` の `名前`)。",
        "",
    ]
    for title, note, rows in SECTIONS:
        lines += [f"## {title}", ""]
        if note:
            lines += [note, ""]
        lines += ["| 何の値か | 値 | 単位 | 場所 |", "|---|---:|---|---|"]
        for module, name, what, unit in rows:
            value = getattr(importlib.import_module(f"packaging_tool.{module}"), name)
            lines.append(f"| {what} | {_fmt(value)} | {unit} | `{module}.{name}` |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    text = build()
    old = PATH.read_text(encoding="utf-8") if PATH.exists() else ""
    if text == old:
        print("一覧は最新です")
        return 0
    PATH.write_text(text, encoding="utf-8")
    print("一覧を作り直しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
