#!/usr/bin/env python3
"""デザイントークンのコントラスト比を検査する

`app/static/css/tokens.css` を読み、**3つのテーマすべて**について
「地と文字」「地と部品の輪郭」の組み合わせを計算する。
WCAG 2.2 を下回ったら失敗させる。

    1.4.3 文字のコントラスト        4.5:1 以上
    1.4.11 文字以外のコントラスト   3:1 以上(部品の輪郭・状態の表示)

【なぜ必要か】
現行のtkinter版では、いちばん見落として困る表示ほど読みにくくなっていた
(1P0113強制ON が 2.33:1、「未設定」帯が 3.37:1)。目視では気づけない。
色を足したり調整したりするたびに機械が確かめれば、同じ取りこぼしは残らない。

【使い方】

    python3 scripts/check_contrast.py            # 検査(不足があれば終了コード1)
    python3 scripts/check_contrast.py --list     # 全ペアの比を表示
    python3 scripts/check_contrast.py --tokens   # 読み取ったトークンを表示

依存は標準ライブラリだけ。`tests/test_contrast.py` からも呼ばれる。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

TOKENS_PATH = (Path(__file__).resolve().parent.parent
               / "app" / "static" / "css" / "tokens.css")

# WCAG 2.2 の下限
MIN_TEXT = 4.5          # 1.4.3 文字
MIN_UI = 3.0            # 1.4.11 部品の輪郭・状態

# テーマの名前。tokens.css の3つのブロックに対応する
THEME_LIGHT = "ライト"
THEME_DARK_SYSTEM = "ダーク(OS設定)"
THEME_DARK_EXPLICIT = "ダーク(明示)"


# ------------------------------------------------------------------
# 検査する組み合わせ
#
# ここが仕様そのもの。「この文字はこの地の上に出る」という対応を
# 書き下してある。部品を足したらここにも足す。
# ------------------------------------------------------------------
TEXT_PAIRS: tuple[tuple[str, str, str], ...] = (
    # (地, 文字, 説明)
    ("ground", "ink", "ページの地に本文"),
    ("surface", "ink", "カードの地に本文"),
    ("surface", "muted", "カードの地に補足"),
    ("sunken", "muted", "沈み地に補足"),
    ("ground", "muted", "ページの地に補足"),
    ("surface", "accent", "カードの地にリンク・強調"),
    ("accent-soft", "accent", "選択行の地に強調"),

    # 操作の色(塗りのボタン)
    ("act-nav", "act-nav-fg", "画面遷移ボタン"),
    ("act-run", "act-run-fg", "実行ボタン"),
    ("act-print", "act-print-fg", "印刷ボタン"),
    ("act-select", "act-select-fg", "選定ボタン"),
    ("act-commit", "act-commit-fg", "保存ボタン"),
    ("act-on", "act-on-fg", "トグルON"),
    ("act-fatigue", "act-fatigue-fg", "疲労度優先"),
    ("act-idle", "act-idle-fg", "中立・トグルOFF"),

    # 操作の色(枠線のボタン。文字は地の上に直接乗る)
    ("surface", "act-find", "検索ボタン(枠線)"),
    ("surface", "act-danger", "取消ボタン(枠線)"),
    # 「上用へ追加」「下用へ追加」。見出しと同じ色を枠線ボタンに使う
    ("surface", "mat-upper", "上用へ追加(枠線)"),
    ("surface", "mat-lower", "下用へ追加(枠線)"),

    # 沈み地。設定画面の進捗欄はカードの中でもう1段くぼませる
    ("sunken", "ink", "沈み地に本文(進捗欄)"),

    # 暗い面。ログ・取り込み結果を読む場所。**明暗どちらのテーマでも
    # 同じ濃さ**なので、対も1組でよい
    ("night", "night-ink", "暗い面の本文"),
    ("night-warn-bg", "night-warn", "暗い面の警告行"),
    ("night-error-bg", "night-error", "暗い面の失敗行"),
    ("night", "night-ok", "暗い面の成功行"),

    # 上の帯。文字はグラデーション(--bar-a → --bar-b)の上に乗るので、
    # **明るいほうの端**で見る。そこが通れば全体が通る
    ("bar-b", "bar-ink", "帯の値(Lot・パレット・製品・拠点)"),
    ("bar-b", "bar-muted", "帯の項目名"),
    ("bar-b", "bar-alert", "帯の「未設定」"),
    # アプリ名と版のバッジ。**どの画面でも消えない**ので、
    # 帯のどちら端に載っても読めること
    ("bar-b", "bar-ink", "帯のアプリ名と版のバッジ"),
    ("bar-a", "bar-alert", "帯の「未設定」(濃いほうの端)"),
    ("bar-a", "bar-ink", "帯の値(濃いほうの端)"),
    ("bar-a", "bar-muted", "帯の項目名(濃いほうの端)"),
    # モード切替の選ばれているほう。白地に帯の色を抜いて出す
    ("bar-ink", "bar-a", "選んでいるモード"),

    # 状態
    ("warn-bg", "warn-fg", "「未設定」の帯"),
    ("ok-bg", "state-ok", "確定の帯"),
    ("surface", "state-warn", "注意の文字"),
    ("surface", "state-error", "エラーの文字"),
    ("surface", "state-info", "情報の文字"),

    # 保管位置マップ。棚の名前(S1/A3…)が塗りの上に乗る。
    # 図から棚を読めなければ、この画面の値打ちが無い
    ("map-idle", "map-idle-fg", "棚の名前(在庫あり)"),
    ("map-empty", "map-empty-fg", "棚の名前(空)"),
    ("map-hit", "map-hit-fg", "棚の名前(検索で当たった)"),
    ("map-active", "map-active-fg", "棚の名前(いま見ている)"),

    # 資材の意味(見出し)
    ("surface", "mat-upper", "【上用ボード配置】"),
    ("surface", "mat-lower", "【下用ボード配置】"),
    ("surface", "mat-angle", "【アングル配置】"),
    ("surface", "mat-pallet", "【パレットサイズ】"),
    ("surface", "mat-product", "【製品サイズ】"),

    # 配置図のキャプション(ボードの塗りの上に寸法を書く)
    ("mat-upper-fill", "ink", "上用ボードの寸法"),
    ("mat-lower-fill", "ink", "下用ボードの寸法"),
    ("mat-fill-fill", "ink", "補填ボードの寸法"),
    ("mat-upper-bg", "ink", "上用キャンバスの地"),
    ("mat-lower-bg", "ink", "下用キャンバスの地"),
    ("mat-angle-bg", "ink", "アングルキャンバスの地"),

    # アングル配置図。バーには丈(「2510mm」)が乗る。
    # 読めなければ、どのアングルがどこに来るのか図から分からない
    ("angle-bar-1", "angle-bar-fg", "アングルバーの丈 (1)"),
    ("angle-bar-2", "angle-bar-fg", "アングルバーの丈 (2)"),
    ("angle-bar-3", "angle-bar-fg", "アングルバーの丈 (3)"),
    ("angle-bar-4", "angle-bar-fg", "アングルバーの丈 (4)"),
    ("angle-bar-5", "angle-bar-fg", "アングルバーの丈 (5)"),
    ("angle-bar-6", "angle-bar-fg", "アングルバーの丈 (6)"),
    ("angle-waste", "angle-waste-fg", "廃材の寸法"),
    ("mat-angle-bg", "cut-line", "「カット N->Mmm」の文字"),
    ("mat-angle-bg", "state-error", "「ｱﾝｸﾞﾙ被りNmm」の文字"),
)

UI_PAIRS: tuple[tuple[str, str, str], ...] = (
    # (地, 部品の色, 説明) — 1.4.11 は「部品を識別できるか」なので 3:1
    ("surface", "act-idle-line", "中立ボタンの輪郭"),
    ("surface", "field-line", "入力欄の輪郭"),
    ("surface", "act-find", "検索ボタンの輪郭"),
    ("surface", "act-danger", "取消ボタンの輪郭"),
    ("surface", "mat-upper", "上用へ追加ボタンの輪郭"),
    ("surface", "mat-lower", "下用へ追加ボタンの輪郭"),
    # 在庫が薄い印(▲薄)。記号も併記するが、記号だけが頼りにならないよう
    # 文字としても読める濃さを保つ
    ("surface", "state-warn", "在庫薄の印"),
    ("surface", "focus", "フォーカスリング"),
    ("ground", "focus", "フォーカスリング(ページの地)"),
    ("bar-b", "bar-focus", "帯の中のフォーカスリング"),
    # 「いま動いているもの」の脈打つ点と進み具合。**点は動きだけでなく
    # 色でも読める**必要があり、横棒は伸びた分の境目で進捗を読む
    ("bar-b", "bar-focus", "帯の「動いています」の点"),
    ("bar-a", "bar-focus", "帯の「動いています」の点(濃いほうの端)"),
    # 暗い面の輪郭。**暗いテーマでは面の濃さで分かれない**ので、
    # 地から分けているのはこの線。明るいテーマでは濃さが先に効く
    ("surface", "night-line", "暗い面が地から分かれること"),
    ("bar-a", "bar-focus", "帯の中のフォーカスリング(濃いほうの端)"),
    # ボードの輪郭は「カードの地」ではなく、**実際に接する色**の上で見る。
    # 隣り合うボードを見分けるのは塗りの上の輪郭、空き領域と見分けるのは
    # キャンバス地の上の輪郭。塗り自体は上用/下用の明度差を優先して
    # 淡くしてあるので、区切りは輪郭が担う
    ("mat-upper-fill", "mat-upper-line", "上用ボードの輪郭(隣と)"),
    ("mat-lower-fill", "mat-lower-line", "下用ボードの輪郭(隣と)"),
    ("mat-upper-bg", "mat-upper-line", "上用ボードの輪郭(空き領域と)"),
    ("mat-lower-bg", "mat-lower-line", "下用ボードの輪郭(空き領域と)"),
    # 補填ボード(幅補填/丈補填)の専用色。上用/下用どちらの図にも出る
    ("mat-fill-fill", "mat-fill-line", "補填ボードの輪郭(隣と)"),
    ("mat-upper-bg", "mat-fill-line", "補填ボードの輪郭(上用の空き領域と)"),
    ("mat-lower-bg", "mat-fill-line", "補填ボードの輪郭(下用の空き領域と)"),
    # はみ出したボードの輪郭。**カット線と同じ色**にそろえた(はみ出しは
    # 「ここを切る」という結論そのもの)。隣り合う地との組を確かめる
    ("mat-upper-fill", "cut-line", "はみ出しボードの輪郭(上用ボードと)"),
    ("mat-lower-fill", "cut-line", "はみ出しボードの輪郭(下用ボードと)"),
    ("mat-fill-fill", "cut-line", "はみ出しボードの輪郭(補填ボードと)"),
    ("mat-upper-bg", "cut-line", "はみ出しボードの輪郭(上用の空き領域と)"),
    ("mat-lower-bg", "cut-line", "はみ出しボードの輪郭(下用の空き領域と)"),
    ("surface", "cut-line", "カット線"),
    ("mat-lower-bg", "cut-line", "カット線(下用の図の上)"),
    ("mat-upper-bg", "cut-line", "カット線(上用の図の上)"),
    # 切り落とす帯はカット線と同じ色相にそろえた(カットは1つの事実)。
    #
    # **帯と線の組は見ない。** 同じ色相で、地からも互いからも 3:1 を
    # 取ることは算術的に成り立たない(明るい地から離すほど線に寄る)。
    # 帯と線を分けているのは色ではなく**斜線の模様とキャプション**
    # (`|||` / `===`)で、色は「カットの話だ」とだけ言う役目。
    # 地から見えることのほうが要るので、そちらだけを守る。
    ("mat-lower-bg", "cut-zone", "切り落とす帯(下用の図の上)"),
    ("mat-upper-bg", "cut-zone", "切り落とす帯(上用の図の上)"),
    ("warn-bg", "state-error", "「未設定」帯の左の帯"),
    # 細くて文字が入らないボードは色で示す(30/50/100mm ほか)。
    # 図の地と見分けられなければ意味がない。凡例の見本も同じ色なので、
    # カードの地の上でも見分けられる必要がある
    *[(ground, f"coded-{kind}", f"細ボードの色分け {kind} ({where})")
      for kind in ("30", "50", "100", "tiny", "small", "other")
      for ground, where in (("mat-lower-bg", "下用"), ("mat-upper-bg", "上用"),
                            ("surface", "凡例"))],
    # アングル図
    ("mat-angle-bg", "faint", "アングル図の脚"),
    ("mat-angle-bg", "angle-pallet", "パレットの帯"),
    ("mat-angle-bg", "angle-edge", "製品の端の印"),
    ("mat-angle-bg", "angle-waste", "廃材"),
    *[("mat-angle-bg", f"angle-bar-{i}", f"アングルバー ({i})")
      for i in range(1, 7)],
    # 進捗バー。どこまで進んだかは塗りの境目で読むので、
    # 溝の色と 3:1 離れていないと進み具合が分からない。
    # 枠(`--rule`)は装飾なのでここには入れない ── 進捗欄そのものは
    # 沈み地との明度差で区切り、線は補助にとどめる
    # 棚の輪郭。隣の棚と見分けられないと、図としての用をなさない
    ("map-idle", "map-line", "棚の輪郭(在庫あり)"),
    ("map-empty", "map-line", "棚の輪郭(空)"),
    ("surface", "map-hit", "検索で当たった棚(地との差)"),
    ("surface", "map-active", "いま見ている棚(地との差)"),
    ("map-idle", "map-hit", "当たった棚とそうでない棚の差"),

    ("act-idle", "accent", "進捗バーの伸びた分"),
    ("act-idle", "state-ok", "進捗バー(完了)"),
    ("act-idle", "state-error", "進捗バー(失敗・中断)"),
)


# ------------------------------------------------------------------
# 意図して下限を満たさない組み合わせ
#
# WCAG 1.4.11 は「その情報が他の視覚的手段でも得られる場合」を除外している。
# 該当するものをここに**理由つきで**書き出す。黙って検査から外すと、
# 補う手がかりを後から削っても誰も気づかなくなる。
# 検査結果には毎回表示され、値は参考として計算される。
# ------------------------------------------------------------------
DOCUMENTED_EXCEPTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("mat-lower-bg", "cut-zone", "切り落とし部(下用)",
     "境界は --cut-line(3:1以上)の赤い線が引く。さらに `|||`/`===` の"
     "ハッチと「←Nカット」の文字が付くので、塗り単独では識別しない。"
     "淡いのは「捨てる部分」を主役にしないため"),
    ("mat-upper-bg", "cut-zone", "切り落とし部(上用)",
     "下用と同じ理由。境界は --cut-line の赤い線とハッチ、"
     "および「←Nカット」の文字が担う。塗りは補助でしかない"),
    ("surface", "accent-line", "レールの現在地の輪郭",
     "現在地の手がかりは先に4つある ── 面の色(--accent-soft)・"
     "番号バッジの反転・名前の太字・名前の色。輪郭はそれを縁取る補助で、"
     "濃くすると項目が7つの箱に見えて並びが読めなくなる"),
)


# ------------------------------------------------------------------
# 色の計算
# ------------------------------------------------------------------
def parse_hex(value: str) -> Optional[tuple[int, int, int]]:
    """`#rrggbb` / `#rgb` を RGB にする。それ以外は None。"""
    value = value.strip()
    match = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value)
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG の相対輝度。"""
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ------------------------------------------------------------------
# tokens.css の読み取り
# ------------------------------------------------------------------
def parse_tokens(css: str) -> dict[str, dict[str, str]]:
    """テーマごとのトークン表を作る。

    3つのブロックを拾う:
        `:root{...}`                                     … ライト(既定)
        `@media (prefers-color-scheme: dark){ :root:not(...){...} }` … OSダーク
        `:root[data-theme="dark"]{...}`                   … 明示ダーク

    ダーク側は**上書き分しか書かれていない**ので、ライトの値に重ねる。
    実際のCSSカスケードと同じ扱いにしないと、検査結果が実物とずれる。
    """
    light = _declarations(_block(css, r":root\s*\{"))
    if not light:
        raise ValueError(":root ブロックが見つかりません")

    dark_system = dict(light)
    block = _block(css, r':root:not\(\[data-theme="light"\]\)\s*\{')
    dark_system.update(_declarations(block))

    dark_explicit = dict(light)
    block = _block(css, r':root\[data-theme="dark"\]\s*\{')
    dark_explicit.update(_declarations(block))

    return {
        THEME_LIGHT: light,
        THEME_DARK_SYSTEM: dark_system,
        THEME_DARK_EXPLICIT: dark_explicit,
    }


def _block(css: str, opener: str) -> str:
    """セレクタの `{` から対応する `}` までを取り出す。"""
    match = re.search(opener, css)
    if not match:
        return ""
    start = match.end()
    depth = 1
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start:i]
    return css[start:]


def _declarations(block: str) -> dict[str, str]:
    """`--name: value;` を拾う。コメントは先に落とす。"""
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return {name: value.strip()
            for name, value in re.findall(r"--([\w-]+)\s*:\s*([^;]+);", block)}


# ------------------------------------------------------------------
# 検査
# ------------------------------------------------------------------
class Finding:
    def __init__(self, theme: str, bg: str, fg: str, label: str,
                 ratio: Optional[float], required: float, note: str = "") -> None:
        self.theme, self.bg, self.fg = theme, bg, fg
        self.label, self.ratio, self.required, self.note = label, ratio, required, note

    @property
    def ok(self) -> bool:
        return self.ratio is not None and self.ratio >= self.required

    def __str__(self) -> str:
        if self.ratio is None:
            return (f"[{self.theme}] {self.label}: "
                    f"--{self.bg} / --{self.fg} — {self.note}")
        mark = "OK " if self.ok else "不足"
        return (f"[{self.theme}] {mark} {self.ratio:5.2f}:1 "
                f"(必要 {self.required}:1) {self.label} "
                f"(--{self.bg} / --{self.fg})")


def exception_report(themes: dict[str, dict[str, str]]) -> list[str]:
    """意図的な例外を、実際の比つきで並べる。"""
    lines = []
    for bg, fg, label, reason in DOCUMENTED_EXCEPTIONS:
        tokens = themes[THEME_LIGHT]
        ratio = ""
        if bg in tokens and fg in tokens:
            a, b = parse_hex(tokens[bg]), parse_hex(tokens[fg])
            if a and b:
                ratio = f"{contrast_ratio(a, b):.2f}:1 "
        lines.append(f"  {label} (--{bg} / --{fg}) {ratio}\n      理由: {reason}")
    return lines


def check(themes: dict[str, dict[str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for theme, tokens in themes.items():
        for pairs, required in ((TEXT_PAIRS, MIN_TEXT), (UI_PAIRS, MIN_UI)):
            for bg, fg, label in pairs:
                findings.append(_check_pair(theme, tokens, bg, fg, label, required))
    return findings


def _check_pair(theme: str, tokens: dict[str, str], bg: str, fg: str,
                label: str, required: float) -> Finding:
    for name in (bg, fg):
        if name not in tokens:
            return Finding(theme, bg, fg, label, None, required,
                           f"トークン --{name} が定義されていません")
    bg_rgb, fg_rgb = parse_hex(tokens[bg]), parse_hex(tokens[fg])
    for name, rgb in ((bg, bg_rgb), (fg, fg_rgb)):
        if rgb is None:
            return Finding(theme, bg, fg, label, None, required,
                           f"--{name} が16進の色ではありません: {tokens[name]}")
    return Finding(theme, bg, fg, label,
                   contrast_ratio(bg_rgb, fg_rgb), required)  # type: ignore[arg-type]


def unused_tokens(themes: dict[str, dict[str, str]]) -> list[str]:
    """検査対象に入っていない色トークン。

    見落としを防ぐための補助。色なのに一度も検査されていないものを挙げる
    (地の色や、文字が乗らない装飾は対象外でよい)。
    """
    checked = {name for pairs in (TEXT_PAIRS, UI_PAIRS)
               for bg, fg, _ in pairs for name in (bg, fg)}
    checked |= {name for bg, fg, _, _ in DOCUMENTED_EXCEPTIONS for name in (bg, fg)}
    light = themes[THEME_LIGHT]
    return sorted(name for name, value in light.items()
                  if parse_hex(value) is not None and name not in checked)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="デザイントークンのコントラスト比を検査する")
    parser.add_argument("--list", action="store_true", help="全ペアの比を表示する")
    parser.add_argument("--tokens", action="store_true", help="読み取ったトークンを表示する")
    parser.add_argument("--path", default=str(TOKENS_PATH), help="tokens.css の場所")
    args = parser.parse_args(list(argv) if argv is not None else None)

    path = Path(args.path)
    try:
        themes = parse_tokens(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"見つかりません: {path}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"読み取れません: {exc}", file=sys.stderr)
        return 2

    if args.tokens:
        for theme, tokens in themes.items():
            print(f"\n=== {theme} ({len(tokens)}件) ===")
            for name in sorted(tokens):
                print(f"  --{name}: {tokens[name]}")
        return 0

    findings = check(themes)
    if args.list:
        for finding in findings:
            print(f"  {finding}")
        print()

    failed = [f for f in findings if not f.ok]
    if failed:
        print(f"コントラストが不足しています ({len(failed)}件 / 全{len(findings)}件):\n",
              file=sys.stderr)
        for finding in failed:
            print(f"  {finding}", file=sys.stderr)
        print("\napp/static/css/tokens.css の値を調整してください。", file=sys.stderr)
        return 1

    print(f"すべて満たしています ({len(findings)}件 / "
          f"{len(themes)}テーマ / 文字 {MIN_TEXT}:1 · 部品 {MIN_UI}:1)")

    if DOCUMENTED_EXCEPTIONS:
        print(f"\n意図して下限を満たさない組み合わせ ({len(DOCUMENTED_EXCEPTIONS)}件)")
        for line in exception_report(themes):
            print(line)

    unused = unused_tokens(themes)
    if unused:
        print(f"\n参考: 検査対象に入っていない色トークン ({len(unused)}件)")
        print("  " + ", ".join(f"--{n}" for n in unused))
        print("  文字が乗る、または輪郭に使うものがあれば "
              "TEXT_PAIRS / UI_PAIRS に足してください。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:                       # `| head` などで打ち切られた場合
        # 後始末で再度 BrokenPipeError が出ないよう、標準出力を切り離す
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
