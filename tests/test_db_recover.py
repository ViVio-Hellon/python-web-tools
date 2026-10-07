"""作業用DBが壊れていても起動ごと止めない / 共有のマスタを掴みっぱなしにしない。

【ここで守りたいこと】
1. 作業用DBが壊れていたら、**退けて(消さずに)作り直す**。取り込み直せば中身は戻る
2. 開きかけで落ちた接続は**閉じる** ── Windows では開いているファイルの名前を変えられず、
   退けることも差し替えることもできない(この試験は Windows の自動試験でも流れる)
3. 掴まれている・混んでいる は「壊れている」と取り違えない(退けない)
4. 共有のマスタを読み書きしたあと、そのファイルを開いたままにしない
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import db, source_db  # noqa: E402


def open_files_under(folder: Path) -> list[str]:
    """このプロセスが開いている、そのフォルダの下のファイル(Linux だけ数えられる)。"""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        return []
    out = []
    for fd in fd_dir.iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith(str(folder)):
            out.append(target)
    return out


class CorruptWorkDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="workdb_"))
        self.path = self.dir / "packaging_tool.db"

    def broken(self) -> None:
        self.path.write_bytes(b"this is not sqlite at all" * 200)
        Path(str(self.path) + "-wal").write_bytes(b"stale wal")

    def test_壊れていたら退けて作り直し起動を続ける(self) -> None:
        self.broken()
        said = db.prepare(self.path)
        self.assertIn("作り直しました", said)
        moved = [p for p in self.dir.iterdir() if ".壊れていた_" in p.name]
        self.assertEqual(len([p for p in moved if p.suffix == ".db"]), 1, moved)
        # 古い -wal が新しいDBに当てられない(当たるとまた壊れる)
        fresh_wal = Path(str(self.path) + "-wal")
        self.assertFalse(fresh_wal.exists() and fresh_wal.read_bytes() == b"stale wal")
        # 退けたファイルは消さない(送れていない発注などを取り出せるように)
        self.assertIn(b"not sqlite", [p for p in moved if p.suffix == ".db"][0].read_bytes())
        with db.connect(self.path) as conn:                 # 新しいDBは使える
            conn.execute("SELECT COUNT(*) FROM 資材パレット注文管理").fetchone()

    def test_壊れていなければ何もしない(self) -> None:
        self.assertEqual(db.prepare(self.path), "")
        self.assertEqual(db.prepare(self.path), "")
        self.assertFalse([p for p in self.dir.iterdir() if ".壊れていた_" in p.name])

    def test_開きかけで落ちた接続は閉じる(self) -> None:
        """閉じないと Windows では名前を変えられず、退けられない。"""
        self.broken()
        with self.assertRaises(sqlite3.DatabaseError):
            with db.connect(self.path) as conn:
                conn.execute("SELECT * FROM sqlite_master").fetchall()
        with mock.patch.object(db.sqlite_toolkit, "enable_wal",
                               side_effect=sqlite3.DatabaseError("file is not a database")):
            with self.assertRaises(sqlite3.DatabaseError):
                db.get_connection(self.path)                # 開きかけで落ちる形
        self.assertEqual(open_files_under(self.dir), [])
        moved = db.quarantine(self.path)                    # Windows でもここで断られない
        self.assertTrue(moved.exists())

    def test_退けられなければ理由の分かる文で止まる(self) -> None:
        self.broken()
        with mock.patch.object(Path, "replace", side_effect=PermissionError("使用中")):
            with self.assertRaises(RuntimeError) as caught:
                db.prepare(self.path)
        self.assertIn("全部閉じてから開き直して", str(caught.exception))
        self.assertIn(str(self.path), str(caught.exception))

    def test_掴まれている混んでいるは壊れているとみなさない(self) -> None:
        self.assertFalse(db.is_corrupt(sqlite3.OperationalError("database is locked")))
        self.assertFalse(db.is_corrupt(sqlite3.OperationalError("unable to open database file")))
        self.assertTrue(db.is_corrupt(sqlite3.DatabaseError("file is not a database")))
        self.assertTrue(db.is_corrupt(sqlite3.DatabaseError("database disk image is malformed")))
        with mock.patch.object(db, "apply_schema",
                               side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                db.prepare(self.path)
        self.assertFalse([p for p in self.dir.iterdir() if ".壊れていた_" in p.name])


class SharedMasterHandleTests(unittest.TestCase):
    """共有のマスタを読み書きしたあと、そのファイルを開いたままにしない。

    Windows では開いているファイルを差し替えられない(ほかの人がマスタを入れ替えられない)。
    最後に名前を変えて戻すのは、Windows の自動試験で「開いたまま」を見つけるため。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="share_"))
        self.path = self.dir / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE BoardMaster (管理番号 INTEGER PRIMARY KEY, ボード幅 INTEGER)")
        conn.execute("INSERT INTO BoardMaster (ボード幅) VALUES (1100)")
        conn.commit()
        conn.close()
        self.addCleanup(source_db.sweep_old_copies)

    def test_読んで書いたあと共有のファイルを開いていない(self) -> None:
        source_db.list_tables(self.path)
        source_db.table_counts(self.path)
        source_db.columns(self.path, "BoardMaster")
        source_db.read_table(self.path, "BoardMaster")
        source_db.read_query(self.path, "SELECT * FROM BoardMaster")
        with source_db.connect(self.path) as src:
            with src.transaction() as tx:
                tx.insert("BoardMaster", {"ボード幅": 1200})
        self.assertEqual(open_files_under(self.dir), [])
        # Windows では開いたままだとここで断られる
        moved = self.path.with_name("差し替え.sqlite3")
        os.replace(self.path, moved)
        os.replace(moved, self.path)
        self.assertEqual(len(source_db.read_table(self.path, "BoardMaster")), 2)


if __name__ == "__main__":
    unittest.main()
