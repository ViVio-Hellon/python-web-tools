"""起動時の自己診断・重複の検知・書き込みの直列化 (②③④)

【何のための3つか】
どれも「**黙って壊れているのが一番わるい**」への手当てです。

②  取り込み元が読めているかを起動のたびに1度確かめ、ログに残す。
    「マスタが読めません」の問い合わせが来たとき、これがあればログを
    見るだけで原因が分かる ── 無ければ端末に行って同じことをやり直す
③  同じ行が2回入っていることを設定画面に出す。総入れ替えの取り込みと
    書き戻しの記録がずれると起きるもので、気づけるのは**発注が倍**に
    なってから、では遅い
④  書く要求を1つずつ通す。SQLite は1本しか書けないので、重なると
    「データベースがロックされています」で片方が落ちる
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import data_sync  # noqa: E402
# 差し替えの当て先は**持ち主のモジュール**。ハブ(`data_sync`)へ
# 当てても、持ち主から呼んでいる側には効かない
from packaging_tool import sync_import as imports
from packaging_tool import sync_sources as sources
from packaging_tool import sync_writeback as writeback
from tests import _web  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"


# ==================================================================
# ③ 重複の検知
# ==================================================================
class DuplicateCountTests(unittest.TestCase):
    """`data_sync.duplicate_count()` — 中身が同じ行を数える。"""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE 発注 (管理番号 INTEGER PRIMARY KEY, 品名 TEXT,"
            " 数量 INTEGER, 送信ID TEXT)")

    def add(self, name: str, qty: int, send_id: str = "") -> None:
        self.conn.execute(
            "INSERT INTO 発注 (品名, 数量, 送信ID) VALUES (?,?,?)",
            (name, qty, send_id))
        self.conn.commit()

    def count(self) -> int:
        return data_sync.duplicate_count(self.conn, "発注", "管理番号")

    def test_重複が無ければ0(self):
        self.add("ボードA", 3)
        self.add("ボードB", 3)
        self.assertEqual(self.count(), 0)

    def test_同じ行が2回入っていれば1(self):
        """1件目は数えない。**余分な数**が知りたい。"""
        self.add("ボードA", 3)
        self.add("ボードA", 3)
        self.assertEqual(self.count(), 1)

    def test_3回なら2(self):
        for _ in range(3):
            self.add("ボードA", 3)
        self.assertEqual(self.count(), 2)

    def test_管理番号が違っても中身が同じなら重複(self):
        """総入れ替えの取り込みでは番号が振り直される。番号では見分けない。"""
        self.add("ボードA", 3)
        self.add("ボードA", 3)
        numbers = [r[0] for r in self.conn.execute("SELECT 管理番号 FROM 発注")]
        self.assertNotEqual(numbers[0], numbers[1])
        self.assertEqual(self.count(), 1)

    def test_送信IDが違っても中身が同じなら重複(self):
        """送信IDは送るときに振られる番号。中身の違いではない。"""
        self.add("ボードA", 3, "S001")
        self.add("ボードA", 3, "S002")
        self.assertEqual(self.count(), 1)

    def test_中身が違えば別の行(self):
        self.add("ボードA", 3)
        self.add("ボードA", 4)
        self.assertEqual(self.count(), 0)

    def test_複数の組が重なっていれば合計する(self):
        self.add("ボードA", 3)
        self.add("ボードA", 3)
        self.add("ボードB", 5)
        self.add("ボードB", 5)
        self.add("ボードB", 5)
        self.assertEqual(self.count(), 3)

    def test_無い表は0(self):
        """設定画面を組み立てるだけで落ちない。"""
        self.assertEqual(data_sync.duplicate_count(self.conn, "無い表", "管理番号"), 0)

    def test_空の表は0(self):
        self.assertEqual(self.count(), 0)


# ==================================================================
# ② 起動時の自己診断
# ==================================================================
class DiagnoseTests(unittest.TestCase):
    """`start_app._diagnose_sources()` — 開いてみて、結果をログに残す。"""

    def setUp(self) -> None:
        import start_app

        self.start_app = start_app
        self.logger = mock.Mock()
        patch = mock.patch.object(start_app, "log", lambda: self.logger)
        patch.start()
        self.addCleanup(patch.stop)

    def messages(self) -> str:
        """出したログを1つの文字列にまとめる(どの段で出たかは問わない)。"""
        out = []
        for call in (list(self.logger.info.call_args_list)
                     + list(self.logger.warning.call_args_list)
                     + list(self.logger.exception.call_args_list)):
            out.append(str(call))
        return "\n".join(out)

    def test_1つも見つからなければ探した場所を書く(self):
        """「見つかりません」だけでは、どこを見ればよいのか分からない。"""
        with mock.patch.object(sources, "find_material_db", return_value=None), \
             mock.patch.object(sources, "find_lot_dbs", return_value={}):
            self.start_app._diagnose_sources()
        self.assertTrue(self.logger.warning.called)
        self.assertIn("取り込み元が1つも見つかりません", self.messages())

    def test_開けたものは開き方と表の数を残す(self):
        """後から原因を追えるように、事実を全部書く。"""
        from packaging_tool import source_db

        path = Path(self.make_db())
        with mock.patch.object(sources, "find_material_db", return_value=path), \
             mock.patch.object(sources, "find_lot_dbs", return_value={}):
            self.start_app._diagnose_sources()
        text = self.messages()
        self.assertIn("開けました", text)
        # 読むときは手元への写しが先(共有を開いたままにしない)
        self.assertIn(source_db.WAY_COPY, text)

    def test_開けないものは理由まで残す(self):
        """「読めません」だけでは端末に行くことになる。"""
        broken = Path(self.make_broken())
        with mock.patch.object(sources, "find_material_db", return_value=broken), \
             mock.patch.object(sources, "find_lot_dbs", return_value={}):
            self.start_app._diagnose_sources()
        self.assertTrue(self.logger.warning.called)
        self.assertIn("開けません", self.messages())

    def test_診断で起動を止めない(self):
        """読めないことと、アプリが使えないことは別。"""
        with mock.patch.object(sources, "find_material_db",
                               side_effect=OSError("共有フォルダに届かない")):
            self.start_app._diagnose_sources()      # 例外が出なければよい

    # -- 土台 ---------------------------------------------------------
    def make_db(self) -> str:
        import tempfile

        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "material.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE PalletMaster (パレット幅 INTEGER)")
        conn.commit()
        conn.close()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        return str(path)

    def make_broken(self) -> str:
        import tempfile

        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "broken.db"
        path.write_text("これは sqlite3 ではありません", encoding="utf-8")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        return str(path)


# ==================================================================
# ④ 書き込みの直列化
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class WriteLockTests(unittest.TestCase):
    """`app._is_write()` — 何を1つずつ通すか。"""

    def request(self, method: str, path: str):
        return mock.Mock(method=method, path=path)

    def test_読むだけのものは待たせない(self):
        import app

        for method in ("GET", "HEAD", "OPTIONS"):
            self.assertFalse(app._is_write(self.request(method, "/api/lot/search")),
                             method)

    def test_書くものは1つずつ通す(self):
        import app

        for path in ("/api/inventory/receive", "/api/selection/boards/place",
                     "/api/settings/import", "/api/master/update"):
            self.assertTrue(app._is_write(self.request("POST", path)), path)

    def test_進捗と切替とログは待たせない(self):
        """長い処理の**最中に**呼ばれるもの。待たせると進捗が止まる。"""
        import app

        for path in ("/api/jobs/123", "/api/mode", "/api/log/clear"):
            self.assertFalse(app._is_write(self.request("POST", path)), path)

    def test_経路ごとの表を持たない(self):
        """画面を1つ足すたびに更新が要る表を作らない(忘れたぶん穴が開く)。"""
        import app

        self.assertTrue(app._is_write(self.request("POST", "/api/まだ無い画面")))


@unittest.skipUnless(HAS_WEB, _SKIP)
class WriteLockOrderTests(unittest.TestCase):
    """実際に重ねてみて、1つずつしか通らないことを確かめる。"""

    def setUp(self) -> None:
        from app.routes import inventory as routes

        _web.bind_db(self, routes)
        self.client = _web.make_client(port=8741)

    def test_同時に書きに来ても重ならない(self):
        """SQLite は1本しか書けない。重なると片方が落ちる。"""
        import app

        inside = []
        peak = [0]
        lock = threading.Lock()

        def hold():
            with app._WRITE_LOCK:
                with lock:
                    inside.append(1)
                    peak[0] = max(peak[0], len(inside))
                time.sleep(0.01)
                with lock:
                    inside.pop()

        threads = [threading.Thread(target=hold) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(peak[0], 1)

    def test_同じスレッドなら入れ子で取れる(self):
        """要求の途中で書き戻しを呼んでも止まらない(再入可能な錠)。"""
        import app

        with app._WRITE_LOCK:
            with app._WRITE_LOCK:
                pass                              # 止まらなければよい

    def test_応答が返ったら放してある(self):
        """放し忘れると、次の書き込みが永久に待つ。"""
        import app

        res = self.client.post("/api/inventory/receive",
                               json={"width": "x"}, headers=_web.auth())
        self.assertEqual(res.status_code, 400)
        # 断られた要求でも錠は返っている
        self.assertTrue(app._WRITE_LOCK.acquire(blocking=False))
        app._WRITE_LOCK.release()

    def test_例外で終わっても放してある(self):
        """500 で終わった要求が錠を握ったままだと、以後すべて止まる。"""
        import app

        from app.routes import inventory as routes
        with mock.patch.object(routes.pallet_service, "receive",
                               side_effect=RuntimeError("わざと")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/api/inventory/receive",
                    json={"width": 1100, "length": 1100, "qty": 1,
                          "position": "A-1"},
                    headers=_web.auth())
        self.assertTrue(app._WRITE_LOCK.acquire(blocking=False))
        app._WRITE_LOCK.release()


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
