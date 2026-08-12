"""選定ログを日ごとのファイルに残す仕組みのテスト

【何を確かめるか】
1. **書いたものが後から読める**(プロセスが終わっても消えない)
2. **日付でまとまる**(「先週のあれ」に答えられる)
3. **壊れた行があっても、その行だけ飛ばす**(1行のせいで全部読めない、を防ぐ)
4. **画面のクリアでファイルは消えない**(片付けたら過去も消える、では困る)
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import selection_log_store as store
from packaging_tool.user_log import LogEntry, UserLog


def entry(text: str, at: str, *, login: str = "yamada",
          pc: str = "LINE1-PC", emphasis: bool = False) -> LogEntry:
    return LogEntry(text=text, emphasis=emphasis, seq=1, at=at,
                    login_id=login, pc_name=pc)


class StoreTests(unittest.TestCase):
    """ファイルに残す/読む。**本物の置き場所には書かない。**"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.object(store, "directory", lambda: self.root)
        patch.start()
        self.addCleanup(patch.stop)

    # -- 書く ---------------------------------------------------------
    def test_書いた行がそのまま読める(self):
        store.append(entry("ボード選定: 3枚", "2026-08-09 10:00:00"))
        rows = store.read_day("2026-08-09")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "ボード選定: 3枚")
        self.assertEqual(rows[0]["login_id"], "yamada")
        self.assertEqual(rows[0]["pc_name"], "LINE1-PC")

    def test_日付ごとに別のファイルへ入る(self):
        store.append(entry("きのう", "2026-08-08 09:00:00"))
        store.append(entry("きょう", "2026-08-09 09:00:00"))
        self.assertEqual([r["text"] for r in store.read_day("2026-08-08")],
                         ["きのう"])
        self.assertEqual([r["text"] for r in store.read_day("2026-08-09")],
                         ["きょう"])

    def test_日は新しい順に並ぶ(self):
        for date in ("2026-08-07", "2026-08-09", "2026-08-08"):
            store.append(entry("x", f"{date} 09:00:00"))
        self.assertEqual(store.days(),
                         ["2026-08-09", "2026-08-08", "2026-08-07"])

    def test_書けなくても例外を投げない(self):
        """ログが書けないことと、選定ができないことは別。"""
        with mock.patch("builtins.open", side_effect=OSError("読み取り専用")):
            store.append(entry("x", "2026-08-09 09:00:00"))   # 落ちなければよい

    # -- 読む ---------------------------------------------------------
    def test_壊れた行はその行だけ飛ばす(self):
        store.append(entry("まえ", "2026-08-09 09:00:00"))
        with open(store.path_for("2026-08-09"), "a", encoding="utf-8") as fh:
            fh.write("{ここで壊れている\n")
        store.append(entry("あと", "2026-08-09 09:00:01"))
        self.assertEqual([r["text"] for r in store.read_day("2026-08-09")],
                         ["まえ", "あと"])

    def test_日付の形をしていないものは読まない(self):
        """ファイル名を要求から組み立てさせない(別の場所を読ませない)。"""
        self.assertEqual(store.read_day("../../etc/passwd"), [])
        self.assertEqual(store.read_day(""), [])

    def test_無い日は空(self):
        self.assertEqual(store.read_day("2020-01-01"), [])

    def test_上限まで読んだら新しいほうを残す(self):
        for i in range(5):
            store.append(entry(f"行{i}", f"2026-08-09 09:00:0{i}"))
        rows = store.read_day("2026-08-09", limit=2)
        self.assertEqual([r["text"] for r in rows], ["行3", "行4"])

    # -- まとめる -----------------------------------------------------
    def test_日ごとのまとめは件数と人と時刻を出す(self):
        store.append(entry("a", "2026-08-09 09:00:00", login="yamada"))
        store.append(entry("b", "2026-08-09 17:30:00", login="suzuki"))
        found = store.summarize("2026-08-09")
        self.assertEqual(found.lines, 2)
        self.assertEqual(found.people, ["yamada @ LINE1-PC", "suzuki @ LINE1-PC"])
        self.assertEqual(found.first_at, "2026-08-09 09:00:00")
        self.assertEqual(found.last_at, "2026-08-09 17:30:00")

    def test_同じ人は1度だけ数える(self):
        for i in range(3):
            store.append(entry("x", f"2026-08-09 09:00:0{i}"))
        self.assertEqual(store.summarize("2026-08-09").people,
                         ["yamada @ LINE1-PC"])

    def test_身元が無い行でもまとめられる(self):
        store.append(entry("x", "2026-08-09 09:00:00", login="", pc=""))
        found = store.summarize("2026-08-09")
        self.assertEqual(found.lines, 1)
        self.assertEqual(found.people, [])

    def test_中身までは返さない(self):
        """まとめは開く前の目安。全文は開いてから読む。"""
        store.append(entry("秘密の行", "2026-08-09 09:00:00"))
        self.assertNotIn("秘密の行", json.dumps(
            store.summarize("2026-08-09").to_dict(), ensure_ascii=False))

    def test_空の日は0件で返る(self):
        self.assertEqual(store.summarize("2026-08-09").lines, 0)

    # -- 片付け -------------------------------------------------------
    def test_古い日から消す(self):
        for day in range(1, 6):
            store.append(entry("x", f"2026-08-0{day} 09:00:00"))
        removed = store.prune(keep_days=2)
        self.assertEqual(removed, ["2026-08-03", "2026-08-02", "2026-08-01"])
        self.assertEqual(store.days(), ["2026-08-05", "2026-08-04"])

    def test_残す日数に足りなければ何も消さない(self):
        store.append(entry("x", "2026-08-09 09:00:00"))
        self.assertEqual(store.prune(keep_days=60), [])
        self.assertEqual(store.days(), ["2026-08-09"])


class KeepOnDiskTests(unittest.TestCase):
    """`UserLog` に繋いだときの振る舞い。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.object(store, "directory", lambda: self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.log = UserLog()
        from packaging_tool import user_log
        user_log.keep_on_disk(self.log)

    def test_出した行がファイルにも残る(self):
        self.log.log("ボード選定を始めます")
        rows = store.read_day(store.today())
        self.assertEqual([r["text"] for r in rows], ["ボード選定を始めます"])

    def test_画面を消してもファイルは消えない(self):
        """片付けたら過去も消える、では後から追える意味が無い。"""
        self.log.log("残るはず")
        self.log.clear()
        self.assertEqual(len(self.log), 0)
        self.assertEqual([r["text"] for r in store.read_day(store.today())],
                         ["残るはず"])

    def test_節目の行は節目として残る(self):
        self.log.log("ふつう")
        self.log.log("節目", emphasis=True)
        rows = store.read_day(store.today())
        self.assertEqual([r["emphasis"] for r in rows], [False, True])

    def test_渡したログに繋ぐ_共有のログではなく(self):
        """`UserLog` は `__len__` を持つので、**空のログは偽になる**。

        `target or _shared` と書くと、繋いだつもりのログではなく共有の
        ログに繋がる ── 起動直後は必ず0行なので、常にそうなる。
        黙って効かないので、気づけるのは「残っていない」と言われたとき。
        """
        from packaging_tool import user_log

        other = UserLog()
        user_log.keep_on_disk(other)
        other.log("こちらに繋がっているはず")
        self.assertEqual([r["text"] for r in store.read_day(store.today())],
                         ["こちらに繋がっているはず"])
        # 共有のログには繋がっていない
        user_log.get_user_log().log("共有のログ")
        self.assertNotIn("共有のログ",
                         [r["text"] for r in store.read_day(store.today())])

    def test_2度繋いでも1行は1回だけ(self):
        from packaging_tool import user_log

        user_log.keep_on_disk(self.log)
        self.log.log("1回だけ")
        self.assertEqual([r["text"] for r in store.read_day(store.today())],
                         ["1回だけ"])


class IdentityTests(unittest.TestCase):
    """行に時刻と身元が付くか(`user_log`)。"""

    def setUp(self) -> None:
        from packaging_tool import user_log

        # 開発機の Windows 環境で結果が変わってはいけない
        patch = mock.patch.object(user_log, "_IDENTITY", ("tanaka", "LINE2-PC"))
        patch.start()
        self.addCleanup(patch.stop)
        self.log = UserLog()

    def test_行に時刻が付く(self):
        self.log.log("x")
        at = self.log.entries[0].at
        self.assertRegex(at, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_行に身元が付く(self):
        self.log.log("x")
        found = self.log.entries[0]
        self.assertEqual(found.login_id, "tanaka")
        self.assertEqual(found.pc_name, "LINE2-PC")
        self.assertEqual(found.who(), "tanaka @ LINE2-PC")

    def test_身元が取れなくてもログは出せる(self):
        """身元が分からないことと、選定ができないことは別。"""
        from packaging_tool import user_log

        with mock.patch.object(user_log, "_IDENTITY", ("", "")):
            self.log.log("x")
        found = self.log.entries[0]
        self.assertEqual(found.who(), "")
        self.assertEqual(found.text, "x")


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
