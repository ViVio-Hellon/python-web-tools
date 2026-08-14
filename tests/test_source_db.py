"""取り込み元(sqlite3)の開き方のテスト。

**共有フォルダ(UNC)の上で開けること**が主題です。取り込み元は
マスタも仕掛台帳も全部共有フォルダに置くので、ここが1か所折れると
現場では**1件も読めません**(実際に踏みました)。

    BoardMaster: 梱包資材マスタ.sqlite3 を開けませんでした:
    invalid uri authority: nlmsrvngy03

原因は URI の書き方です。Windows の UNC を `Path.as_uri()` に掛けると
`file://サーバ/共有/…` になり、SQLite はこの `//サーバ` を **authority**
として読みます。SQLite が受け付ける authority は空か `localhost` だけです。

Windows でしか起きない事象ですが、`PureWindowsPath` を使えば
どの端末でも同じように確かめられます。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock
from urllib.parse import urlsplit

from packaging_tool import source_db

# 現場のエラーに出ていた実物(本番の共有・開発の共有・仕掛台帳)
本番 = PureWindowsPath(
    r"\\nlmfangyshrd\各課共有\0130_日軽稲沢\梱包課\AIM"
    r"\【■】_参照用ファイル\梱包資材マスタ.sqlite3")
開発 = PureWindowsPath(
    r"\\nlmsrvngy03\工場内共有\検査データ\ファイル共有\梱包資材マスタ.sqlite3")
仕掛 = PureWindowsPath(r"\\nlmsrvngy03\工場内共有\台帳\SIKALOTNOW.sqlite3")


class UncUriTests(unittest.TestCase):
    """`to_uri` が **authority を空のままにする**ことだけを見る。"""

    def test_サーバ名がauthorityに出ない(self) -> None:
        """これが今回の不具合そのもの。共有フォルダ全部に効く。"""
        for path in (本番, 開発, 仕掛):
            with self.subTest(path=str(path)):
                self.assertEqual(urlsplit(source_db.to_uri(path)).netloc, "")

    def test_サーバ名はパスとして残る(self) -> None:
        """authority から外すだけ。**どのファイルを指すかは変えない。**"""
        self.assertTrue(
            source_db.to_uri(仕掛).startswith("file:////nlmsrvngy03/"))

    def test_スラッシュ区切りのUNCも同じ(self) -> None:
        """設定画面に `//サーバ/…` と打たれることがある。"""
        self.assertEqual(source_db.to_uri(PureWindowsPath("//srv/共有/x.sqlite3")),
                         source_db.to_uri(PureWindowsPath(r"\\srv\共有\x.sqlite3")))

    def test_符号化はas_uriと同じ(self) -> None:
        """日本語・空白・記号の書き換え方は標準に合わせる。

        違うのは authority を空にした点**だけ**、と言い切れる形。
        """
        for path in (本番, 開発, 仕掛):
            with self.subTest(path=str(path)):
                self.assertEqual(source_db.to_uri(path),
                                 "file://" + path.as_uri()[len("file:"):])

    def test_共有でない道は標準のまま(self) -> None:
        """ドライブ名の道・Linux の道は今までどおり。"""
        self.assertEqual(source_db.to_uri(PureWindowsPath(r"C:\data\x.sqlite3")),
                         "file:///C:/data/x.sqlite3")
        self.assertEqual(source_db.to_uri(Path("/tmp/x.sqlite3")),
                         "file:///tmp/x.sqlite3")


class SqliteAcceptsTests(unittest.TestCase):
    """SQLite 本体に**通るかどうか**を直接聞く。文言合わせではない。"""

    def test_直した形は解釈してもらえる(self) -> None:
        for path in (本番, 開発, 仕掛):
            with self.subTest(path=str(path)):
                self.assertNotIn("invalid uri authority",
                                 _open_error(source_db.to_uri(path) + "?mode=ro"))

    def test_直す前の形は断られる(self) -> None:
        """この試験が落ちたら、`to_uri` の逃がしは要らなくなったということ。"""
        self.assertIn("invalid uri authority: nlmsrvngy03",
                      _open_error(開発.as_uri() + "?mode=ro"))


def _open_error(uri: str) -> str:
    """その URI で開こうとしたときの言い分(開けたら空)。"""
    try:
        sqlite3.connect(uri, uri=True).close()
    except sqlite3.Error as exc:
        return str(exc)
    return ""                                    # pragma: no cover - 手元に無い


class ConnectTests(unittest.TestCase):
    """本物のファイルで、開き方の約束を確かめる。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="srcuri_"))
        self.path = self.dir / "取り込み元.sqlite3"
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE 表 (名前 TEXT)")
        conn.execute("INSERT INTO 表 VALUES ('あ')")
        conn.commit()
        conn.close()

    def test_読み取り専用で開く(self) -> None:
        """取り込みのつもりで**うっかり書き換えない**。"""
        with source_db._connect(self.path, read_only=True) as conn:
            self.assertEqual(conn.execute("SELECT 名前 FROM 表").fetchone()[0], "あ")
            with self.assertRaises(sqlite3.Error):
                conn.execute("INSERT INTO 表 VALUES ('い')")

    def test_URIで開けなくても素のパスで開く(self) -> None:
        """URI の書き方は環境で当たり外れが出うる。止めない。"""
        conn = source_db._try_plain(self.path, read_only=True)
        with conn:
            self.assertEqual(conn.execute("SELECT 名前 FROM 表").fetchone()[0], "あ")
            # 逃げ道でも読み取り専用は譲らない
            with self.assertRaises(sqlite3.Error):
                conn.execute("INSERT INTO 表 VALUES ('い')")

    def test_CP932のまま書かれたTEXT列でも表ごと落とさない(self) -> None:
        """**UTF-8で読めない列があっても、その表を捨てない。**

        Access から変換したファイルには、文字を Shift-JIS(CP932)の
        バイトのまま TEXT 列へ書いたものが混ざる。Python の sqlite3 は
        既定でそこに当たると `OperationalError` を投げる ── bytes では
        返らないので、値を受け取ってから直すことはできない。
        現場の「仕掛かり一覧の文字化け」「マスタが表示されない」は
        どちらもこれが出どころだった。
        """
        path = self.dir / "CP932混じり.sqlite3"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE 表 (用途名 TEXT)")
        raw.execute("INSERT INTO 表 VALUES (CAST(? AS TEXT))",
                    ("JISN製品".encode("cp932"),))
        raw.execute("INSERT INTO 表 VALUES ('シャーシ①')")   # 正しいUTF-8
        raw.commit()
        raw.close()

        # 既定のまま開くと、この列に触れた瞬間に例外が飛ぶ(=表ごと失われる)
        plain = sqlite3.connect(path)
        with self.assertRaises(sqlite3.OperationalError):
            plain.execute("SELECT 用途名 FROM 表").fetchall()
        plain.close()

        with source_db._connect(path, read_only=True) as conn:
            got = [r[0] for r in conn.execute("SELECT 用途名 FROM 表")]
        self.assertEqual(got, ["JISN製品", "シャーシ①"])

    def test_CP932がUTF8としても読めてしまう行で静かに化けない(self) -> None:
        """**値ごとに読み方を当てにいってはいけない。**

        CP932 の `燿　` は `e0 a0 81 40`。これは**正しいUTF-8としても
        読める**ので、1値ずつ「まずUTF-8」と試すと例外も `�` も出さずに
        `ࠁ@` になる ── 気づけないまま手元へ入る。現場から2度届いた
        「文字化けが治っていない」の残りはこれだった。

        ファイル単位で入れ方を決めれば取りこぼさない
        (本物のUTF-8ファイルには、UTF-8として読めない値が1つも無い)。
        """
        path = self.dir / "静かに化ける.sqlite3"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE 表 (用途名 TEXT)")
        for text in ("燿　", "JISN製品①", "シャーシ"):
            raw.execute("INSERT INTO 表 VALUES (CAST(? AS TEXT))",
                        (text.encode("cp932"),))
        raw.commit()
        raw.close()

        # 単体で見ると、この4バイトはUTF-8としても妥当(だから危ない)
        self.assertEqual("燿　".encode("cp932").decode("utf-8"), "ࠁ@")

        with source_db._connect(path, read_only=True) as conn:
            got = [r[0] for r in conn.execute("SELECT 用途名 FROM 表")]
        self.assertEqual(got, ["燿　", "JISN製品①", "シャーシ"])

    def test_UTF8で作られたファイルはそのまま読む(self) -> None:
        """CP932側へ倒しすぎない。正しく変換された取り込み元を壊さない。"""
        path = self.dir / "正しいUTF8.sqlite3"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE 表 (用途名 TEXT)")
        for text in ("燿　", "JISN製品①", "シャーシ"):
            raw.execute("INSERT INTO 表 VALUES (?)", (text,))
        raw.commit()
        raw.close()

        with source_db._connect(path, read_only=True) as conn:
            self.assertEqual(source_db.sniff_encoding(conn),
                             source_db.ENCODING_UTF8)
            got = [r[0] for r in conn.execute("SELECT 用途名 FROM 表")]
        self.assertEqual(got, ["燿　", "JISN製品①", "シャーシ"])

    def test_壊れた値が1つあってもUTF8のファイルを巻き添えにしない(self) -> None:
        """**1つの値で決めない。**

        UTF-8で作られたファイルに読めない値が1件混ざっているとき、
        そこで「CP932だ」と決めてしまうと、その1件を助けるために
        **残り全部を化けさせる**ことになる。日本語を含む値の多数決で決める。
        """
        path = self.dir / "1件だけ壊れたUTF8.sqlite3"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE 表 (用途名 TEXT)")
        for text in ("シャーシ", "チョウセイドプレート", "イワンイタ",
                     "JISN製品①", "アングル"):
            raw.execute("INSERT INTO 表 VALUES (?)", (text,))
        raw.execute("INSERT INTO 表 VALUES (CAST(? AS TEXT))", (b"\x90\xbb",))
        raw.commit()
        raw.close()

        with source_db._connect(path, read_only=True) as conn:
            self.assertEqual(source_db.sniff_encoding(conn),
                             source_db.ENCODING_UTF8)
            got = [r[0] for r in conn.execute("SELECT 用途名 FROM 表")]
        self.assertEqual(got[:5], ["シャーシ", "チョウセイドプレート",
                                   "イワンイタ", "JISN製品①", "アングル"])

    def test_CP932のファイルはASCII行に引きずられない(self) -> None:
        """ロット番号のようなASCIIばかりの列は判断材料にしない。"""
        path = self.dir / "ASCII多め.sqlite3"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE 表 (ﾛｯﾄ番号 TEXT, 用途名 TEXT)")
        for i in range(20):
            raw.execute("INSERT INTO 表 VALUES (?, CAST(? AS TEXT))",
                        (f"410278{i}", "シャーシ".encode("cp932")))
        raw.commit()
        raw.close()

        with source_db._connect(path, read_only=True) as conn:
            self.assertEqual(source_db.sniff_encoding(conn),
                             source_db.ENCODING_CP932)
            got = {r[0] for r in conn.execute("SELECT 用途名 FROM 表")}
        self.assertEqual(got, {"シャーシ"})

    def test_CP932のファイルでも表名と列名はUTF8で読む(self) -> None:
        """**SQLiteの識別子は必ずUTF-8。**

        中のデータがCP932でも、表名・列名はUTF-8で保持されている。
        `sqlite_master.name` と `PRAGMA table_info` は識別子を**値として**
        返すので、CP932で読む `text_factory` を掛けたままだと
        `ﾛｯﾄ番号` が `ﾛｯﾄ逡ｪ蜿ｷ` になる ── その名前では引けず、
        表が丸ごと空で取り込まれる。
        """
        path = self.dir / "識別子.sqlite3"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE 仕掛 (ﾛｯﾄ番号 TEXT, 用途名 TEXT)")
        for i in range(10):
            raw.execute("INSERT INTO 仕掛 VALUES (?, CAST(? AS TEXT))",
                        (f"410278{i}", "シャーシ".encode("cp932")))
        raw.commit()
        raw.close()

        self.assertEqual(source_db.list_tables(path), ["仕掛"])
        self.assertEqual(source_db.columns(path, "仕掛"), ["ﾛｯﾄ番号", "用途名"])
        # 取り込みが見る辞書の鍵も、元の列名のまま
        rows = source_db.read_table(path, "仕掛")
        self.assertEqual(set(rows[0]), {"ﾛｯﾄ番号", "用途名"})
        self.assertEqual(rows[0]["用途名"], "シャーシ")

    def test_列名を見失わない(self) -> None:
        """判定のために `text_factory` を bytes にすると、`PRAGMA table_info`
        の列名と型まで bytes になる。そのままだとTEXT列が1つも見つからず、
        判定が素通りする(実際に踏んだ)。"""
        path = self.dir / "列名.sqlite3"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE 表 (用途名 TEXT)")
        raw.execute("INSERT INTO 表 VALUES (CAST(? AS TEXT))",
                    ("シャーシ".encode("cp932"),))
        raw.commit()
        raw.close()
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(source_db.sniff_encoding(conn),
                         source_db.ENCODING_CP932)

    def test_どちらでも読めないバイトは読めた字だけ残す(self) -> None:
        """壊れた行のために表ぜんぶを捨てるよりはまし。"""
        self.assertEqual(source_db.decode_text(b"A\xff\xfeB").count("A"), 1)

    def test_開けなければ最初の理由を返す(self) -> None:
        """逃げ道も駄目だったときは、原因に近い**最初の**理由を出す。"""
        with self.assertRaises(source_db.SourceError) as caught:
            source_db._connect(self.dir, read_only=True)
        self.assertIn(self.dir.name, str(caught.exception))

    def test_無いファイルは開かずに断る(self) -> None:
        """素のパスで開くと**空のファイルを作ってしまう**ので先に見る。"""
        missing = self.dir / "無い.sqlite3"
        with self.assertRaises(source_db.SourceError):
            source_db._connect(missing, read_only=True)
        self.assertFalse(missing.exists())

    @unittest.skipIf(os.name == "nt", "Windows では //tmp が本物の共有名になる")
    def test_二重スラッシュの道から本当に読める(self) -> None:
        """UNC と同じ枝を、**本物のファイルで**通す。

        Linux では `//tmp/…` も `/tmp/…` と同じ場所を指すので、
        Windows が無くても「UNC の書き方で開けるか」を実地で試せる。
        """
        双 = Path("/" + str(self.path))          # //tmp/… (UNC と同じ枝)
        self.assertTrue(source_db.to_uri(Path(os.path.abspath(双)))
                        .startswith("file:////"))
        with source_db._connect(双, read_only=True) as conn:
            self.assertEqual(conn.execute("SELECT 名前 FROM 表").fetchone()[0], "あ")

    def test_開けたかどうかは引けたかどうかで決める(self) -> None:
        """`sqlite3.connect()` はファイルを触らない。

        **開けたつもりのまま返ってきて、最初の問い合わせで落ちます。**
        それでは次の手に移れないので、開くところで1文引いて確かめる。
        中身が別物なら、読みに行く前にここで断ること。
        """
        bogus = self.dir / "偽物.sqlite3"
        bogus.write_text("これは sqlite3 ではありません", encoding="utf-8")
        with self.assertRaises(source_db.SourceError) as caught:
            source_db._connect(bogus, read_only=True)
        # 断り文に**試した手と理由**が入っている
        self.assertIn("not a database", str(caught.exception))

    def test_その場で開けなければ手元へ写して読む(self) -> None:
        """共有の上の WAL は読むだけでも開けない。写せば読める。"""
        with mock.patch.object(source_db, "_try_uri",
                               side_effect=sqlite3.OperationalError("shm 不可")), \
             mock.patch.object(source_db, "_try_plain",
                               side_effect=sqlite3.OperationalError("shm 不可")):
            attempts: list = []
            conn, way = source_db._open(self.path, read_only=True,
                                        attempts=attempts)
            with conn:
                self.assertEqual(
                    conn.execute("SELECT 名前 FROM 表").fetchone()[0], "あ")
                # 写しでも読み取り専用は譲らない
                with self.assertRaises(sqlite3.Error):
                    conn.execute("INSERT INTO 表 VALUES ('い')")
        self.assertEqual(way, source_db.WAY_COPY)
        # **試した順が残る。** どこで折れたかを画面に出せる
        self.assertEqual([n for n, _why in attempts],
                         [source_db.WAY_URI, source_db.WAY_PLAIN,
                          source_db.WAY_COPY])

    def test_書くときは写しへ逃がさない(self) -> None:
        """写しへ書くと、書いたものがどこにも残らない。"""
        with mock.patch.object(source_db, "_try_uri",
                               side_effect=sqlite3.OperationalError("だめ")), \
             mock.patch.object(source_db, "_try_plain",
                               side_effect=sqlite3.OperationalError("だめ")):
            with self.assertRaises(source_db.SourceError):
                source_db._connect(self.path, read_only=False)

    def test_同じファイルを何度も写さない(self) -> None:
        """取り込みは表ごとに開き直す。19回写すと往復だけで待たされる。"""
        first = source_db._copy_of(self.path)
        again = source_db._copy_of(self.path)
        self.assertEqual(first, again)

    def test_中身が変われば写し直す(self) -> None:
        first = source_db._copy_of(self.path)
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO 表 VALUES ('う')")
        conn.commit()
        conn.close()
        source_db._copy_of(self.path)
        got = sqlite3.connect(str(first)).execute(
            "SELECT COUNT(*) FROM 表").fetchone()[0]
        self.assertEqual(got, 2)

    def test_絶対の道にするのに共有へ問い合わせない(self) -> None:
        """`resolve()` は実体を開きに行く。共有の上では往復が増える。"""
        with mock.patch.object(Path, "resolve",
                               side_effect=AssertionError("問い合わせた")):
            with source_db._connect(self.path, read_only=True) as conn:
                conn.execute("SELECT 1").fetchone()


class ProbeTests(unittest.TestCase):
    """**なぜ開けないのか**を、そのまま画面に出せる形で返す。

    「sqlite3 として読めません」だけでは、現場も直せません。
    原因はだいたい3つ ── 中身が別物 / 誰かが掴んでいる / WAL のまま。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="srcprobe_"))

    def make(self, name: str, *, wal: bool = False) -> Path:
        path = self.dir / name
        conn = sqlite3.connect(path)
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE 表 (名前 TEXT)")
        conn.execute("INSERT INTO 表 VALUES ('あ')")
        conn.commit()
        conn.close()
        return path

    def test_読めたときは中身まで分かる(self) -> None:
        found = source_db.probe(self.make("ふつう.sqlite3"))
        self.assertTrue(found.ok)
        self.assertEqual(found.opened_by, source_db.WAY_URI)
        self.assertEqual(found.tables, ["表"])
        self.assertTrue(found.is_sqlite)
        self.assertGreater(found.size, 0)

    def test_書かれ方も分かる(self) -> None:
        """WAL かどうかは、共有の上で読めるかどうかを分ける。"""
        self.assertEqual(
            source_db.probe(self.make("wal.sqlite3", wal=True)).journal.lower(),
            "wal")

    def test_sqlite3でなければ印で分かる(self) -> None:
        bogus = self.dir / "偽物.sqlite3"
        bogus.write_text("これは sqlite3 ではありません", encoding="utf-8")
        found = source_db.probe(bogus)
        self.assertFalse(found.ok)
        self.assertFalse(found.is_sqlite)
        self.assertTrue(found.error)

    def test_無いファイルでも例外は投げない(self) -> None:
        """診断が落ちると、診断が要る場面で何も分からなくなる。"""
        found = source_db.probe(self.dir / "無い.sqlite3")
        self.assertFalse(found.ok)
        self.assertFalse(found.exists)

    def test_試した手が全部残る(self) -> None:
        with mock.patch.object(source_db, "_try_uri",
                               side_effect=sqlite3.OperationalError("だめ")):
            found = source_db.probe(self.make("ふつう.sqlite3"))
        self.assertTrue(found.ok)
        self.assertEqual(found.opened_by, source_db.WAY_PLAIN)
        self.assertEqual(found.attempts[0][0], source_db.WAY_URI)
        self.assertIn("だめ", found.attempts[0][1])


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()


class CheckEncodingScriptTests(unittest.TestCase):
    """`scripts/check_encoding.py` ── **壊れているのか読み方か**を判別する。

    文字化けの原因は2つに1つで、直し方がまったく違う。画面の文字を
    見比べても区別が付かない(どちらも同じように化けて見える)ので、
    バイトを見て言い切れるようにする。

        (A) 読み方の問題       … 取り込み直せば直る
        (B) 元のファイルが壊れている … 取り込み直しても直らない
    """

    def setUp(self) -> None:
        import importlib.util

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "check_encoding", root / "scripts" / "check_encoding.py")
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)
        self.dir = Path(tempfile.mkdtemp(prefix="checkenc_"))

    def make(self, name: str, values: list) -> Path:
        path = self.dir / name
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE 仕掛 (用途名 TEXT)")
        for value in values:
            if isinstance(value, bytes):
                conn.execute("INSERT INTO 仕掛 VALUES (CAST(? AS TEXT))", (value,))
            else:
                conn.execute("INSERT INTO 仕掛 VALUES (?)", (value,))
        conn.commit()
        conn.close()
        return path

    def test_健全なCP932は壊れていないと言う(self) -> None:
        path = self.make("健全.sqlite3",
                         [s.encode("cp932")
                          for s in ("JISN製品①", "シャーシ", "燿　")])
        self.assertFalse(self.script.inspect(path))

    def test_焼き付いた文字化けを壊れていると言う(self) -> None:
        """`�` が**元のファイルに入っている**なら、取り込み直しても直らない。

        変換の時点で読めなかった字は、そこでバイトが失われている。
        「取り込み直してください」と案内してはいけない場面。
        """
        damaged = [s.encode("cp932").decode("utf-8", "replace")
                   for s in ("JISN製品①", "シャーシ")]
        path = self.make("壊れている.sqlite3", damaged)
        self.assertTrue(self.script.inspect(path))

    def test_正しいUTF8も壊れていないと言う(self) -> None:
        path = self.make("UTF8.sqlite3", ["JISN製品①", "シャーシ", "燿　"])
        self.assertFalse(self.script.inspect(path))
