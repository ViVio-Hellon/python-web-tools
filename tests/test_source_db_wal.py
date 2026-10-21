"""WAL 形式の取り込み元を読んでも、共有に付き添いのファイルを残さない(VER4.8.81)。

all-tools と共通の不具合 ③:
    共有の梱包資材マスタが WAL 形式だと、読んだあとに -wal / -shm が共有に残る。
    中身の入った -wal が残った隣でマスタを差し替えると、新しいマスタに古い中身が当たる
    (全体なら古いマスタに戻る、一部なら索引が壊れる)。

ここでは「共有」を一時フォルダで代える。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from packaging_tool import source_db  # noqa: E402


def sidecars(path: Path) -> list[str]:
    return sorted(e for e in ("-wal", "-shm", "-journal") if Path(str(path) + e).exists())


class WalBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="wal_"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make(self, name: str, rows: int, *, wal: bool, tag: str = "A") -> Path:
        path = self.dir / name
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA journal_mode = {'WAL' if wal else 'DELETE'}")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"{tag}{i}",) for i in range(rows)])
        conn.commit()
        conn.close()
        self.assertEqual(sidecars(path), [])
        return path


class WalMasterTests(WalBase):
    def test_WALを読んでも付き添いを残さない(self) -> None:
        path = self.make("梱包資材マスタ.sqlite3", 5, wal=True)
        self.assertTrue(source_db.is_wal_file(path))
        self.assertEqual(len(source_db.read_table(path, "t")), 5)
        found = source_db.probe(path)
        self.assertTrue(found.ok)
        self.assertEqual(found.opened_by, source_db.WAY_COPY)
        self.assertEqual(sidecars(path), [])

    def test_読み取り専用では共有の上で開かない(self) -> None:
        path = self.make("m.sqlite3", 1, wal=True)
        with self.assertRaises(sqlite3.OperationalError):
            source_db._try_uri(path, read_only=True)
        self.assertEqual(sidecars(path), [])

    def test_まだ本体に入っていない行も読む(self) -> None:
        """ほかの端末が開いたまま(-wal に行があり、本体にはまだ無い)。前は本体だけ写して取りこぼした。"""
        path = self.make("m.sqlite3", 1, wal=True)
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO t (v) VALUES ('NEW')")
        writer.commit()
        values = [r["v"] for r in source_db.read_table(path, "t")]
        self.assertIn("NEW", values)

    def test_ふだんの形式は前のまま鍵を取って写す(self) -> None:
        path = self.make("m.sqlite3", 3, wal=False)
        self.assertFalse(source_db.is_wal_file(path))
        self.assertEqual(len(source_db.read_table(path, "t")), 3)
        self.assertEqual(sidecars(path), [])


class StaleWalTests(WalBase):
    """本体はふだんの形式なのに、隣に前の本体の -wal(中身入り)が残っている。"""

    def leave_old_wal(self, target: Path) -> None:
        old = self.dir / "old.sqlite3"
        conn = sqlite3.connect(old)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"OLD{i}",) for i in range(300)])
        conn.commit()
        shutil.copyfile(str(old) + "-wal", str(target) + "-wal")   # 前の本体の -wal が残った
        conn.close()

    def test_残骸を当てずに新しい本体を読む(self) -> None:
        path = self.make("梱包資材マスタ.sqlite3", 50, wal=False, tag="NEW")
        self.leave_old_wal(path)
        self.assertTrue(source_db.has_stale_wal(path))
        before = path.read_bytes()
        values = [r["v"] for r in source_db.read_table(path, "t")]
        self.assertEqual(len(values), 50)
        self.assertTrue(all(v.startswith("NEW") for v in values))
        self.assertEqual(path.read_bytes(), before)            # 本体は触っていない

    def test_書く接続では開かない(self) -> None:
        path = self.make("梱包資材マスタ.sqlite3", 50, wal=False, tag="NEW")
        self.leave_old_wal(path)
        before = path.read_bytes()
        with self.assertRaises(source_db.SourceError) as caught:
            source_db.connect(path)
        self.assertIn("古い", str(caught.exception))
        self.assertIn("-wal", str(caught.exception))
        self.assertEqual(path.read_bytes(), before)

    def test_空の残骸は害が無いので止めない(self) -> None:
        path = self.make("m.sqlite3", 2, wal=False)
        Path(str(path) + "-wal").write_bytes(b"")
        self.assertFalse(source_db.has_stale_wal(path))
        with source_db.connect(path) as conn:
            self.assertEqual(conn.table_names(), ["t"])


if __name__ == "__main__":
    unittest.main()
