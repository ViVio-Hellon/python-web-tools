#!/usr/bin/env python3
"""選定・配置・描画計画の結果をゴールデンファイルに固定する

選定・配置・描画計画は業務の心臓部で、**1行変えれば現場の発注が変わる**。
画面をいくら作り替えても、ここが変わっていないことを機械が言えなければ
安心して手を入れられない。

そこで実データ(`PalletPatterns` に残る41通りのパレット/製品サイズ)について

    候補ボード → 自動選定 → 自動配置 → 描画計画

を通し、結果をJSONに固定する。1バイトでも変われば落ちる。

【なぜ描画計画まで含めるか】
`placement_render.build_render_plan()` は「どこに何色で何を置くか」までを
決める純関数で、**描き方は知らない**。ここを固定しておけば、画面を
作り替えても図が変わっていないと言える ── 正しさを目で見比べる
必要がなくなる。

【使い方】

    python3 scripts/compare_ui.py            # ゴールデンと突き合わせる(差分があれば終了コード1)
    python3 scripts/compare_ui.py --update   # ゴールデンを作り直す
    python3 scripts/compare_ui.py --list     # 対象の41通りを表示する
    python3 scripts/compare_ui.py --case 3   # 1件だけ詳しく出す(調査用)

`--update` は**意図して結果を変えたときだけ**使う。差分が出たら、まず
「変えたつもりが無いのに変わっていないか」を疑うこと。

【再現性について】
- 候補ボードは `BoardMaster` の既定ボード種別(`list_board_types()` の先頭)を使う
- 疲労度・在庫考慮・プロテック・上下共用はすべてOFFの通常モードで通す
  (モード別の固定は、プレゼンタ抽出後に条件を増やして追加する)
- 浮動小数は丸めてから比較する(環境差でJSONが揺れないようにする)
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# これは診断用のスクリプトであって業務の実行ではないので、
# 走らせただけでリポジトリの `logs/` を書き換えないようにする。
# `config.LOG_DIR` は読み込み時に環境変数を1度だけ読むため、
# `packaging_tool` を import する前に決める必要がある。
# 出力先を自分で指定したい場合は `PACKAGING_TOOL_LOG_DIR` を設定する。
if not os.environ.get("PACKAGING_TOOL_LOG_DIR"):
    _LOG_DIR = tempfile.mkdtemp(prefix="compare_ui_logs_")
    os.environ["PACKAGING_TOOL_LOG_DIR"] = _LOG_DIR
    atexit.register(shutil.rmtree, _LOG_DIR, ignore_errors=True)

from packaging_tool import (  # noqa: E402
    board_selection_algorithm as alg,
    board_selection_service as svc,
    db,
    logging_utils,
    placement_algorithm as place,
    placement_render as render,
)
from packaging_tool.presenters import render_json  # noqa: E402

# 選定アルゴリズムはDEBUGを大量に出す(実データ41通りで19万文字)。
# このスクリプトの出力は比較結果なので、既定では業務ログでそれを埋めない。
# `--verbose` で戻せる(ログはどちらの場合も上のフォルダに残る)。
if "--verbose" not in sys.argv:
    logging_utils.silence_console()

# ゴールデンファイル。`tests/` の下に置いてテストからも読めるようにする
GOLDEN_PATH = Path(__file__).resolve().parent.parent / "tests" / "golden" / "selection_placement.json"

# 描画計画の論理キャンバス寸法と丸めは、Web版と共通の変換モジュールに従う
# (ここで別に定義すると、ゴールデンとWeb版の出力がずれる)。
LOGICAL_CANVAS_W = render_json.LOGICAL_CANVAS_W
LOGICAL_CANVAS_H = render_json.LOGICAL_CANVAS_H


# ------------------------------------------------------------------
# 対象の取り出し
# ------------------------------------------------------------------
def load_cases(conn: sqlite3.Connection) -> list[dict[str, int]]:
    """`PalletPatterns` から相異なるパレット/製品サイズの組み合わせを取る。

    製品サイズが 0 の行は、パレットサイズだけ保存された古い行なので除く
    (製品サイズが無いと選定を走らせられない)。
    並び順は固定する。ゴールデンの並びが実行のたびに変わると差分が読めない。
    """
    rows = db.fetch_all(
        conn,
        "SELECT DISTINCT パレット幅, パレット丈, 製品幅, 製品丈 FROM PalletPatterns "
        "WHERE 製品幅 > 0 AND 製品丈 > 0 "
        "ORDER BY パレット幅, パレット丈, 製品幅, 製品丈",
        caller_name="compare_ui.load_cases",
    ) or []
    return [
        {
            "pallet_width": row["パレット幅"], "pallet_length": row["パレット丈"],
            "product_width": row["製品幅"], "product_length": row["製品丈"],
        }
        for row in rows
    ]


def case_key(case: dict[str, int]) -> str:
    """ケースの見出し。差分を読むときに何のケースか分かる形にする。"""
    return (f"パレット{case['pallet_width']}x{case['pallet_length']}"
            f"_製品{case['product_width']}x{case['product_length']}")


# ------------------------------------------------------------------
# 1ケースの実行
# ------------------------------------------------------------------
def run_case(conn: sqlite3.Connection, case: dict[str, int],
             board_type: str) -> dict[str, Any]:
    """候補ボード → 自動選定 → 自動配置 → 描画計画 を通す。

    UIがやっていること(`SelectionView.do_auto_select_boards` /
    `do_auto_place` / `redraw_layout`)と同じ順・同じ引数で呼ぶ。
    ここがずれるとゴールデンの意味が無くなるので、引数を足すときは
    UI側と突き合わせること。
    """
    palette = svc.Palette(width=case["pallet_width"], length=case["pallet_length"])
    product = svc.ProductSize(width=case["product_width"], length=case["product_length"])

    available = svc.list_available_boards(conn, board_type)

    select = alg.auto_select_boards(available, palette, product)
    placement = place.auto_place_boards(
        select.lower, select.upper, palette, product,
        narrow_lower=select.lower_result.state.narrow_pallet,
        narrow_upper=select.upper_result.narrow_pallet,
    )

    return {
        **case,
        "narrow_lower": bool(select.lower_result.state.narrow_pallet),
        "narrow_upper": bool(select.upper_result.narrow_pallet),
        "selected_lower": [_selected(b) for b in select.lower],
        "selected_upper": [_selected(b) for b in select.upper],
        "cut_info": _sorted_dict(select.cut_info),
        "length_cut_info": _sorted_dict(select.length_cut_info),
        "length_cut_count": _sorted_dict(select.length_cut_count),
        "placed": [_placed(p) for p in placement.placed],
        "plans": {
            # 下用はパレット基準、上用は製品基準(VBA踏襲)
            "lower": _plan(placement.placed, render.CATEGORY_LOWER,
                           palette.width, palette.length,
                           select.lower_result.state.narrow_pallet),
            "upper": _plan(placement.placed, render.CATEGORY_UPPER,
                           product.width, product.length,
                           select.upper_result.narrow_pallet),
        },
    }


def _selected(board: svc.SelectedBoard) -> dict[str, Any]:
    return {"width": board.width, "length": board.length,
            "count": board.count, "tag": board.tag}


def _placed(board: Any) -> dict[str, Any]:
    """配置済みボード1枚。座標系はVBA踏襲で X=丈方向 / Y=幅方向。"""
    return {
        "category": board.board_category,
        "x": board.x, "y": board.y,
        "width": board.width, "length": board.length,
        "original_width": board.original_width,
        "original_length": board.original_length,
        "instance_id": board.instance_id,
        "is_fill_board": bool(board.is_fill_board),
    }


def _sorted_dict(source: dict) -> dict:
    """キー順を固定する。dictの並びで差分が出るのを防ぐ。"""
    return {str(k): source[k] for k in sorted(source, key=str)}


def _plan(placed: list, category: str, base_w: int, base_l: int,
          narrow: bool) -> dict[str, Any]:
    """描画計画。SVG化しても同じ図になることを、ここで固定する。

    変換は Web版と同じ `presenters.render_json` を通す。別々に書くと
    「ゴールデンは合っているのにWeb版の図が違う」という事故が起きる。
    """
    return render_json.build_render_plan_dict(
        placed, category, base_w, base_l, narrow=narrow)


# ------------------------------------------------------------------
# 全ケース
# ------------------------------------------------------------------
def build(conn: sqlite3.Connection) -> dict[str, Any]:
    board_types = svc.list_board_types(conn)
    if not board_types:
        raise SystemExit(
            "ボード種別が1つもありません。`BoardMaster` が空です。\n"
            "  python3 scripts/init_db.py --seed  でサンプルデータを入れるか、\n"
            "  python3 scripts/import_source.py で実データを取り込んでください。")
    board_type = board_types[0]

    cases = load_cases(conn)
    if not cases:
        raise SystemExit(
            "対象がありません。`PalletPatterns` に製品サイズ入りの行が必要です。")

    results = {}
    for case in cases:
        results[case_key(case)] = run_case(conn, case, board_type)

    return {
        "_about": "選定・配置・描画計画のゴールデン。scripts/compare_ui.py が生成する。",
        "_board_type": board_type,
        "_logical_canvas": [LOGICAL_CANVAS_W, LOGICAL_CANVAS_H],
        "_case_count": len(results),
        "cases": results,
    }


def dump(data: dict[str, Any]) -> str:
    """ゴールデンの書式。差分が行単位で読めるよう、必ず整形して書く。"""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


# ------------------------------------------------------------------
# 比較
# ------------------------------------------------------------------
def compare(current: dict[str, Any], golden: dict[str, Any]) -> list[str]:
    """差分を人が読める形で返す。空なら一致。"""
    problems: list[str] = []

    for key in ("_board_type", "_logical_canvas"):
        if current.get(key) != golden.get(key):
            problems.append(
                f"{key} が違います: 現在 {current.get(key)!r} / ゴールデン {golden.get(key)!r}")

    cur_cases, gold_cases = current.get("cases", {}), golden.get("cases", {})
    added = sorted(set(cur_cases) - set(gold_cases))
    removed = sorted(set(gold_cases) - set(cur_cases))
    for name in added:
        problems.append(f"ゴールデンに無いケースが増えています: {name}")
    for name in removed:
        problems.append(f"ゴールデンにあるケースが消えています: {name}")

    for name in sorted(set(cur_cases) & set(gold_cases)):
        problems.extend(
            f"{name}: {line}" for line in _diff(cur_cases[name], gold_cases[name], ""))
    return problems


def _diff(current: Any, golden: Any, path: str) -> list[str]:
    """入れ子をたどって、値が違う場所だけを列挙する。"""
    if isinstance(current, dict) and isinstance(golden, dict):
        out: list[str] = []
        for key in sorted(set(current) | set(golden)):
            here = f"{path}.{key}" if path else key
            if key not in current:
                out.append(f"{here} が消えています (ゴールデン: {golden[key]!r})")
            elif key not in golden:
                out.append(f"{here} が増えています (現在: {current[key]!r})")
            else:
                out.extend(_diff(current[key], golden[key], here))
        return out

    if isinstance(current, list) and isinstance(golden, list):
        out = []
        if len(current) != len(golden):
            out.append(f"{path} の件数が違います: 現在 {len(current)} / ゴールデン {len(golden)}")
        for i in range(min(len(current), len(golden))):
            out.extend(_diff(current[i], golden[i], f"{path}[{i}]"))
        return out

    if current != golden:
        return [f"{path}: 現在 {current!r} / ゴールデン {golden!r}"]
    return []


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="選定・配置・描画計画をゴールデンファイルと突き合わせる")
    parser.add_argument("--update", action="store_true",
                        help="ゴールデンを現在の結果で作り直す(意図して変えたときだけ)")
    parser.add_argument("--list", action="store_true", help="対象のケースを表示する")
    parser.add_argument("--case", type=int, metavar="N",
                        help="N番目のケースだけを詳しく出す(調査用)")
    parser.add_argument("--verbose", action="store_true",
                        help="選定アルゴリズムのログもコンソールに出す(既定は抑止)")
    args = parser.parse_args(argv)

    conn = db.get_connection()
    try:
        if args.list:
            for i, case in enumerate(load_cases(conn)):
                print(f"{i:3d}  {case_key(case)}")
            return 0

        if args.case is not None:
            cases = load_cases(conn)
            if not 0 <= args.case < len(cases):
                print(f"ケース番号は 0〜{len(cases) - 1} です", file=sys.stderr)
                return 2
            board_type = svc.list_board_types(conn)[0]
            print(dump(run_case(conn, cases[args.case], board_type)))
            return 0

        current = build(conn)

        if args.update:
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            existed = GOLDEN_PATH.exists()
            GOLDEN_PATH.write_text(dump(current), encoding="utf-8")
            print(f"{'更新' if existed else '作成'}しました: {GOLDEN_PATH}")
            print(f"  ケース数     : {current['_case_count']}")
            print(f"  ボード種別   : {current['_board_type']}")
            print(f"  論理キャンバス: {current['_logical_canvas']}")
            return 0

        if not GOLDEN_PATH.exists():
            print(f"ゴールデンがありません: {GOLDEN_PATH}\n"
                  f"  python3 scripts/compare_ui.py --update  で作成してください。",
                  file=sys.stderr)
            return 2

        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        problems = compare(current, golden)
        if not problems:
            print(f"一致しました ({current['_case_count']}ケース / "
                  f"ボード種別 {current['_board_type']})")
            return 0

        print(f"差分があります ({len(problems)}件):\n", file=sys.stderr)
        for line in problems[:60]:
            print(f"  {line}", file=sys.stderr)
        if len(problems) > 60:
            print(f"  … ほか {len(problems) - 60} 件", file=sys.stderr)
        print("\n意図した変更なら --update でゴールデンを作り直してください。",
              file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
