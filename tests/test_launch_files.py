r"""起動ファイル(.bat / .vbs)の作りを固定する

**Linux上で編集して、Windowsで実行する**という組み合わせなので、
文字コードと改行はここで機械的に押さえておかないと静かに壊れる。
壊れたことは現場で「ダブルクリックしても何も起きない」として現れ、
原因にたどり着くまでに時間がかかる。

【なぜ CP932 なのか】
- `.bat` は **cmd.exe がコンソールのコードページで解釈する**。
  日本語Windowsの既定は932なので、ファイルもCP932でなければならない。
- `.vbs` は **WSH がシステムANSIコードページで読む**。同じく932。
  (UTF-16LE+BOM でも読めるが、gitがバイナリ扱いにするので採らない)

【CP932 で特に危ないもの】
CP932の2バイト目は 0x40〜0x7E / 0x80〜0xFC。つまり `\`(0x5C)や
`@`(0x40)がそのまま2バイト目に現れる。コードページがずれた状態で
読まれると、`表`(0x95 5C)や`ソ`(0x83 5C)が「?\」のように化けて、
表示だけでなく**解釈まで変わる**。だから `chcp 932` を、
非ASCIIが出てくるより前に置く。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 起動ファイルはこの3本だけ。役割が違うものだけを残している:
#
#   Start.vbs … 通常起動。**利用者がダブルクリックするのはこれ1つ**
#   start.bat … 診断起動(コンソールを開いたまま経過を出す)
#   stop.bat  … 停止
#
# 資材モード用の `Start_資材.vbs` は廃止した。どのモードで開くかは
# `アクセス権限` マスタが端末ごとに決めるので、押すものを分ける必要が
# 無い ── 分けていたころは、資材の端末で現場のほうを押して
# 「発注一覧が出ない」を権限の問題として調べることになっていた。
# **入口を増やすほど、同じ不具合が別の経路に残る。**
BATCH_FILES = ("start.bat", "stop.bat")
VBS_FILES = ("Start.vbs",)
LAUNCH_FILES = BATCH_FILES + VBS_FILES

# どのファイルにも必ず入っている語。CP932として読めることの確認に使う
# (UTF-8で保存されていると、CP932で読んだときに化けて一致しない)
MARKER = "梱包資材総合ツール"

# バッチのコードページ指定。非ASCIIより前に無ければ意味がない
CHCP = "chcp 932"

# 2バイト目がバッチの特殊文字になるCP932の文字。
# コードページがずれると、この文字が特殊文字として解釈されうる
_TRAIL_META = {0x5C: "\\", 0x5E: "^", 0x7C: "|", 0x26: "&", 0x3C: "<",
               0x3E: ">", 0x25: "%", 0x22: '"', 0x28: "(", 0x29: ")",
               0x40: "@"}


def read_bytes(name: str) -> bytes:
    return (_ROOT / name).read_bytes()


def read_text(name: str) -> str:
    return read_bytes(name).decode("cp932")


def first_non_ascii(data: bytes) -> int:
    """最初の非ASCIIバイトの位置。全部ASCIIなら `len(data)`。"""
    for i, byte in enumerate(data):
        if byte > 0x7F:
            return i
    return len(data)


def _commands(text: str) -> list[str]:
    """実行される行だけ。注釈(`rem`)・空行・ラベルは外す。

    注釈で「`cd /d` は使わない」と書くことがあるので、
    文面の検査は実行される行に限る。
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if stripped.lower().startswith("rem") or stripped.startswith("::"):
            continue
        lines.append(stripped)
    return lines


def risky_chars(data: bytes) -> list[tuple[str, str]]:
    """2バイト目がバッチの特殊文字になる文字を拾う。"""
    found, i = [], 0
    while i < len(data):
        lead = data[i]
        if (0x81 <= lead <= 0x9F) or (0xE0 <= lead <= 0xFC):
            if i + 1 < len(data) and data[i + 1] in _TRAIL_META:
                found.append((data[i:i + 2].decode("cp932", "replace"),
                              _TRAIL_META[data[i + 1]]))
            i += 2
        else:
            i += 1
    return found


# ==================================================================
# 文字コードと改行
# ==================================================================
class EncodingTests(unittest.TestCase):
    def test_全部そろっている(self) -> None:
        for name in LAUNCH_FILES:
            with self.subTest(name=name):
                self.assertTrue((_ROOT / name).exists(), f"{name} がありません")

    def test_起動ファイルはこれだけ(self) -> None:
        """入口が増えていないか。

        ダブルクリックできるファイルが並ぶほど、現場は「どれを押すか」を
        覚えることになる(基盤仕様書 2.1 が入口を絞れと言っている理由)。
        増やすときは、ここに足して意図を明示する。
        """
        found = {p.name for p in _ROOT.glob("*.bat")} | {p.name for p in _ROOT.glob("*.vbs")}
        self.assertEqual(found, set(LAUNCH_FILES))

    def test_CP932で読める(self) -> None:
        for name in LAUNCH_FILES:
            with self.subTest(name=name):
                try:
                    read_bytes(name).decode("cp932")
                except UnicodeDecodeError as exc:
                    self.fail(f"{name} はCP932ではありません: {exc}")

    def test_CP932として意味が通る(self) -> None:
        """UTF-8で保存し直されていないか。

        UTF-8のままだとCP932で読めてしまうことがあるが、中身は化ける。
        「読めるか」ではなく「正しい語になるか」で見る。
        """
        for name in LAUNCH_FILES:
            with self.subTest(name=name):
                self.assertIn(MARKER, read_text(name),
                              f"{name} がUTF-8等で保存されていませんか")

    def test_BOMを付けない(self) -> None:
        """cmd.exe は先頭のBOMをコマンドとして読もうとして失敗する。"""
        for name in LAUNCH_FILES:
            with self.subTest(name=name):
                data = read_bytes(name)
                self.assertNotEqual(data[:3], b"\xef\xbb\xbf", "UTF-8 BOM があります")
                self.assertNotIn(data[:2], (b"\xff\xfe", b"\xfe\xff"),
                                 "UTF-16 BOM があります")

    def test_改行はCRLF(self) -> None:
        """LFだけだと、`goto` のラベル解決などで挙動が変わることがある。"""
        for name in LAUNCH_FILES:
            with self.subTest(name=name):
                data = read_bytes(name)
                lone_lf = data.count(b"\n") - data.count(b"\r\n")
                self.assertEqual(lone_lf, 0, f"{name} に単独のLFがあります")
                lone_cr = data.count(b"\r") - data.count(b"\r\n")
                self.assertEqual(lone_cr, 0, f"{name} に単独のCRがあります")

    def test_gitが改行を変えない(self) -> None:
        """`.gitattributes` が無いと、`core.autocrlf` の設定次第で
        取り出したファイルがLFになる端末が出る。"""
        attrs = (_ROOT / ".gitattributes")
        self.assertTrue(attrs.exists(), ".gitattributes がありません")
        text = attrs.read_text(encoding="utf-8")
        for pattern in ("*.bat", "*.vbs"):
            with self.subTest(pattern=pattern):
                self.assertRegex(text, rf"{re.escape(pattern)}\s+text\s+eol=crlf")


# ==================================================================
# バッチファイル
# ==================================================================
class BatchTests(unittest.TestCase):
    def test_日本語より前にコードページを決める(self) -> None:
        """`chcp` が後ろにあると、そこまでの行が化けたまま解釈される。"""
        for name in BATCH_FILES:
            with self.subTest(name=name):
                data = read_bytes(name)
                position = data.find(CHCP.encode("ascii"))
                self.assertNotEqual(position, -1, f"{name} に {CHCP} がありません")
                self.assertLess(position, first_non_ascii(data),
                                f"{name} の {CHCP} が日本語より後ろにあります")

    def test_危ない文字があることを踏まえている(self) -> None:
        """`表` や `ソ` は2バイト目が `\\`。この試験は「危ない文字が
        入っていないこと」ではなく、**入っていても chcp で守れている**
        ことを確かめる(日本語を書くなとは言えないので)。"""
        for name in BATCH_FILES:
            with self.subTest(name=name):
                data = read_bytes(name)
                if not risky_chars(data):
                    continue
                self.assertLess(data.find(CHCP.encode("ascii")),
                                first_non_ascii(data))

    def test_共有フォルダでも移動できる(self) -> None:
        """`cd /d` は UNCパス(\\\\サーバ\\...)を現在地にできない。

        アプリ本体を共有フォルダに置く運用があるので `pushd` を使う。
        """
        for name in BATCH_FILES:
            with self.subTest(name=name):
                text = read_text(name)
                self.assertIn('pushd "%~dp0"', text)
                # 注釈で `cd /d` に触れることはあるので、実行される行だけ見る
                for lineno, line in enumerate(_commands(text), 1):
                    self.assertNotIn("cd /d", line,
                                     f"{name}: cd /d ではUNCパスで動きません")

    def test_pushdとpopdの数が合う(self) -> None:
        """割り当てたドライブ文字を放置しない。"""
        for name in BATCH_FILES:
            with self.subTest(name=name):
                text = read_text(name)
                pushd = len(re.findall(r"^\s*pushd\b", text, re.MULTILINE))
                popd = len(re.findall(r"^\s*popd\b", text, re.MULTILINE))
                self.assertGreaterEqual(popd, pushd,
                                        f"{name}: popd が足りません")

    def test_括弧の中に括弧を書かない(self) -> None:
        """`if ( ... )` の中の `)` はブロックの終わりとして読まれる。

        日本語の全角括弧は無害だが、半角を混ぜると途中で切れる。
        """
        for name in BATCH_FILES:
            with self.subTest(name=name):
                depth = 0
                for lineno, line in enumerate(read_text(name).splitlines(), 1):
                    stripped = line.strip()
                    if stripped.lower().startswith("rem"):
                        continue
                    if depth > 0 and stripped.lower().startswith("echo "):
                        body = stripped[5:]
                        self.assertNotIn("(", body, f"{name}:{lineno}")
                        self.assertNotIn(")", body, f"{name}:{lineno}")
                        continue
                    depth += line.count("(") - line.count(")")
                    depth = max(depth, 0)

    def test_失敗したら理由を読ませてから閉じる(self) -> None:
        """`pause` が無いと、窓が一瞬で消えて何も読めない。"""
        for name in BATCH_FILES:
            with self.subTest(name=name):
                self.assertIn("pause", read_text(name))

    def test_呼び出す先が実在する(self) -> None:
        for name, target in (("start.bat", "start_app.py"),
                             ("stop.bat", "process_manager.py")):
            with self.subTest(name=name):
                self.assertIn(target, read_text(name))
                self.assertTrue((_ROOT / target).exists())

    def test_起動するモジュールが実在する(self) -> None:
        """`python -m ...` の綴りを間違えても、実行するまで気づけない。"""
        import importlib.util
        for name in BATCH_FILES:
            for module in re.findall(r"^python -m ([\w.]+)",
                                     read_text(name), re.MULTILINE):
                with self.subTest(name=name, module=module):
                    self.assertIsNotNone(importlib.util.find_spec(module),
                                         f"{module} がありません")

    def test_モードの対応表を二重に持たない(self) -> None:
        """バッチ側でモードを解釈しない。

        以前は `start.bat` が `material` / `warehouse` を自前で読み替えて
        `--mode` を組み立てていた。**同じ対応表が2か所にある**ので、
        片方だけ直すと資材モードが黙って現場モードで起動する。
        いまは引数をそのまま `start_app.py` へ渡すだけにしてある。
        """
        text = read_text("start.bat")
        self.assertNotIn("set MODE", text)
        self.assertIn("python start_app.py %*", text)


# ==================================================================
# VBScript
# ==================================================================
class VbsTests(unittest.TestCase):
    def test_Option_Explicitがある(self) -> None:
        """綴り間違いを実行時に見つけられるようにする。"""
        for name in VBS_FILES:
            with self.subTest(name=name):
                self.assertIn("Option Explicit", read_text(name))

    def test_宣言した変数を全部使う(self) -> None:
        """使わない `Dim` は、直し忘れの跡であることが多い。"""
        for name in VBS_FILES:
            with self.subTest(name=name):
                text = read_text(name)
                declared = re.search(r"^Dim (.+)$", text, re.MULTILINE)
                self.assertIsNotNone(declared)
                for var in (v.strip() for v in declared.group(1).split(",")):
                    uses = len(re.findall(rf"\b{re.escape(var)}\b", text))
                    self.assertGreater(uses, 1, f"{name}: {var} が使われていません")

    def test_本体の有無を確かめる(self) -> None:
        """pythonw はコンソールを持たない。本体が無いと**本当に無音**になる。

        このファイルだけをデスクトップにコピーする使い方が実際にあるので、
        そこで気づけるようにしておく。
        """
        for name in VBS_FILES:
            with self.subTest(name=name):
                text = read_text(name)
                self.assertIn("FileExists", text)
                self.assertIn("start_app.py", text)

    def test_pythonwの有無も確かめる(self) -> None:
        """python はあるのに pythonw が無い入れ方がある。"""
        for name in VBS_FILES:
            with self.subTest(name=name):
                self.assertIn("cmd /c pythonw", read_text(name))

    def test_絶対パスで渡す(self) -> None:
        """共有フォルダから実行されると、作業フォルダ頼みでは見つからない。"""
        for name in VBS_FILES:
            with self.subTest(name=name):
                text = read_text(name)
                self.assertIn("BuildPath", text)
                self.assertIn("Chr(34)", text, "空白を含むパスを引用していません")

    def test_失敗したら止まる(self) -> None:
        for name in VBS_FILES:
            with self.subTest(name=name):
                text = read_text(name)
                self.assertEqual(text.count("WScript.Quit 1"),
                                 text.count("MsgBox "),
                                 f"{name}: 知らせたのに続行している箇所があります")

    def test_通常起動はモードを指定しない(self) -> None:
        """**どのモードで開くかは端末が決める**(`アクセス権限` マスタ)。

        ここで `--mode` を渡すと、権限とショートカットという2つの
        出どころができる。資材の端末で現場のほうを押したときに、
        「発注一覧が出ない」を権限の問題として調べることになる。
        """
        text = read_text("Start.vbs")
        self.assertNotIn("--mode", text)
        self.assertNotIn("--role", text)


# ==================================================================
# pythonw で動かしたとき
# ==================================================================
class NoConsoleTests(unittest.TestCase):
    """`pythonw.exe` では `sys.stdout` / `sys.stderr` が `None` になる。

    Start.vbs はコンソールを出さないために pythonw を使うので、
    この状態が**通常の起動経路**になる。
    """

    def test_コンソールが無ければ画面用ハンドラを付けない(self) -> None:
        """`StreamHandler()` は `stream = None` を抱え、1行ごとに
        `None.write` で例外を起こす(`logging` が握りつぶすので
        表には出ない)。選定は実データ1件で数千行のDEBUGを出す。
        """
        import logging
        from packaging_tool import logging_utils

        original = sys.stderr
        sys.stderr = None
        try:
            self.assertFalse(logging_utils._has_console())
        finally:
            sys.stderr = original

        # 実際のハンドラ構成でも確かめる
        logger = logging.getLogger("packaging_tool")
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                    handler, logging.FileHandler):
                self.assertIsNotNone(handler.stream,
                                     "書き先の無いハンドラが付いています")

    def test_コンソールがあれば判定は真(self) -> None:
        from packaging_tool import logging_utils
        self.assertTrue(logging_utils._has_console())


if __name__ == "__main__":
    unittest.main()
