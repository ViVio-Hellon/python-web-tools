"""変更履歴(docs/変更履歴.md)の先頭に、項目別の目次を作り直す。

【なぜ要るのか】
変更履歴は新しい版から順に並んでいて、190版を超えると7,000行になる。
「実績の保存・呼び出しを直したのはどの版か」を探すのに、900行下まで
読み進めることになっていた(現場の声:「前に渡した気がするけど修正履歴に
載ってない」── 載っていたが見つけられなかった)。

【どう作るか】
各版の節見出し(`### …`)の言葉で、項目(`TOPICS`)に振り分ける。
1つの版が複数の項目に入ってよい。どの項目にも入らない版は「その他」に置かず
黙って外す(目次は探す入口で、全部の一覧は本文がすでに持っている)。

目次は `<!-- 目次ここから -->` と `<!-- 目次ここまで -->` のあいだだけを
書き換える。版を足したら、このスクリプトを流す:

    python3 scripts/make_changelog_index.py

流し忘れは `tests/test_app_config.py` が見る(目次が本文と食い違うと落ちる)。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "docs" / "変更履歴.md"
BEGIN = "<!-- 目次ここから -->"
END = "<!-- 目次ここまで -->"

# (項目, 見出しに含まれていれば拾う言葉, 本文に含まれていれば拾う言葉)。
# 本文は**決まった言い回しだけ**を見る(本文の言葉は広すぎて何にでも当たる)
TOPICS: tuple[tuple[str, str, str], ...] = (
    ("VBA の修正・仕様更新を反映した版",
     r"VBA ?の(修正|仕様)|VBA ?側|更新後の ?VBA|VBA ?が|VBA ?と同じ|VBAの",
     r"VBA ?の(仕様更新|修正)|VBA ?側で[^。]*(合わせ|ので|移した|更新)|更新後の ?VBA"),
    ("実績パターン(保存・呼び出し・共有)",
     r"実績パターン|実績を|実績の表|実績が|スナップショット", ""),
    ("ボード使用実績・人気度", r"使用実績|人気度", ""),
    ("ボード選定・配置・候補変更・補填",
     r"ボード選定|配置(?!図|編集)|候補変更|敷き詰め|補填|Y積み|主ボード|手動追加", ""),
    ("切断依頼・カット", r"切断|カット", ""),
    ("パレット選定・閾値・単位", r"パレット(選定|検索|一覧|閾値)|閾値|適合|単位", ""),
    ("アングル", r"アングル", ""),
    ("倉庫連携(発注・確認・取り消し・コメント)",
     r"倉庫|発注|取り消し|確認・取|コメント|受け渡し", ""),
    ("簡易在庫", r"在庫", ""),
    ("棚検索・配置図・置き場", r"棚検索|配置図|配置編集|置き場", ""),
    ("ロット検索・仕掛台帳", r"ロット|Lot|仕掛|SIKA", ""),
    ("取り込み・書き戻し・共有", r"取り込|書き戻|共有|取り込み元|書き込む先|書き先", ""),
    ("表を持ってくる・マスタ管理",
     r"表を持ってくる|マスタ管理|表を消|表まわり|Access|入れ替え|作り直", ""),
    ("権限・モード・認証", r"権限|モード|認証|パスワード", ""),
    ("帳票・印刷", r"印刷|帳票|紙", ""),
    ("デスクトップ版(Tauri)・ポート", r"デスクトップ|Tauri|ポート", ""),
    ("ログ・エラーの後追い", r"ログ(?!イン)|エラー番号|後から追|後追い", ""),
    ("起動・終了・タブ・配布", r"起動|終了|タブ|画面を?2枚|多重|配布|README", ""),
)


def _anchor(version: str) -> str:
    """GitHub の見出しの飛び先(`## VER3.5.0` → `#ver350`)。"""
    return "#" + re.sub(r"[^0-9a-z]", "", version.lower())


def sections(text: str) -> list[tuple[str, list[str], str]]:
    """(版, その版の節見出し, その版の本文)を新しい順に。"""
    out: list[tuple[str, list[str], list[str]]] = []
    for line in text.splitlines():
        if line.startswith("## VER"):
            out.append((line[3:].strip(), [], []))
        elif not out:
            continue
        elif line.startswith("### "):
            out[-1][1].append(line[4:].strip())
        else:
            out[-1][2].append(line)
    return [(v, heads, "\n".join(body)) for v, heads, body in out]


def build_index(text: str) -> str:
    found = sections(text)
    lines = [BEGIN, "",
             "## 項目別の目次",
             "",
             "探している直しが**どの版か**を、項目ごとに引けます(新しい版から順)。",
             "版を押すとその節へ飛びます。版の中身は本文の節を読んでください。",
             "この目次は `scripts/make_changelog_index.py` が節見出しから作っています"
             "(手で直さない)。",
             ""]
    for topic, pattern, body_pattern in TOPICS:
        rx = re.compile(pattern)
        body_rx = re.compile(body_pattern) if body_pattern else None
        hits = [version for version, heads, body in found
                if any(rx.search(h) for h in heads)
                or (body_rx is not None and body_rx.search(body))]
        if not hits:
            continue
        links = "・".join(f"[{v}]({_anchor(v)})" for v in hits)
        lines.append(f"- **{topic}**({len(hits)}版): {links}")
    lines += ["", END]
    return "\n".join(lines)


def apply(text: str) -> str:
    """目次を差し替えた本文。目次がまだ無ければ最初の版の節の前に置く。"""
    index = build_index(text)
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        _old, tail = rest.split(END, 1)
        return head + index + tail
    at = text.index("\n## VER") + 1
    return text[:at] + index + "\n\n" + text[at:]


def main() -> int:
    text = PATH.read_text(encoding="utf-8")
    new = apply(text)
    if new == text:
        print("目次は最新です")
        return 0
    PATH.write_text(new, encoding="utf-8")
    print("目次を作り直しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
