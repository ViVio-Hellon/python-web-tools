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
