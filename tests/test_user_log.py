"""選定ログ(user_log)とパレット検索の除外ログのユニットテスト。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

from packaging_tool import board_selection_service as svc, db
from packaging_tool.user_log import LogEntry, RejectLog, UserLog, bridge_from


class UserLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = UserLog()

    def test_log_appends_entries(self):
        self.log.log("a")
        self.log.log("b", emphasis=True)
        self.assertEqual([e.text for e in self.log.entries], ["a", "b"])
        self.assertFalse(self.log.entries[0].emphasis)
        self.assertTrue(self.log.entries[1].emphasis)

    def test_text_joins_with_newline(self):
        self.log.log("a")
        self.log.log("b")
        self.assertEqual(self.log.text, "a\nb")

    def test_clear_empties_and_notifies_none(self):
        received: list = []
        self.log.subscribe(received.append)
        self.log.log("a")
        self.log.clear()
        self.assertEqual(len(self.log), 0)
        self.assertIsInstance(received[0], LogEntry)
        self.assertIsNone(received[1])

    def test_subscriber_receives_each_entry(self):
        received: list = []
        self.log.subscribe(received.append)
        self.log.log("x")
        self.assertEqual(received[-1].text, "x")

    def test_unsubscribe_stops_notifications(self):
        received: list = []
        self.log.subscribe(received.append)
        self.log.unsubscribe(received.append)
        self.log.log("x")
        self.assertEqual(received, [])

    def test_subscribing_twice_notifies_once(self):
        received: list = []
        self.log.subscribe(received.append)
        self.log.subscribe(received.append)
        self.log.log("x")
        self.assertEqual(len(received), 1)

    def test_listener_exception_does_not_break_logging(self):
        def boom(_entry):
            raise RuntimeError("listener failure")

        self.log.subscribe(boom)
        self.log.log("x")           # 例外が外に漏れないこと
        self.assertEqual(len(self.log), 1)

    def test_trimming_notifies_full_redraw(self):
        log = UserLog(max_entries=3)
        received: list = []
        log.subscribe(received.append)
        for i in range(4):
            log.log(str(i))
        self.assertEqual([e.text for e in log.entries], ["1", "2", "3"])
        self.assertIsNone(received[-1])   # 巻き戻しは全再描画を促す


class RejectLogTests(unittest.TestCase):
    """現場の声:「候補が多いと除外の行が際限なく出る」への対応。"""

    def setUp(self) -> None:
        self.log = UserLog()

    def test_shows_up_to_the_limit(self):
        rej = RejectLog(self.log, limit=3)
        for i in range(3):
            rej.log(f"除外{i}")
        self.assertEqual(len(self.log), 3)

    def test_suppresses_beyond_the_limit(self):
        rej = RejectLog(self.log, limit=3)
        for i in range(10):
            rej.log(f"除外{i}")
        # 上位3件だけが実際にログへ出ている(まだflush前)
        self.assertEqual(len(self.log), 3)

    def test_flush_summarizes_suppressed_count(self):
        rej = RejectLog(self.log, limit=3)
        for i in range(10):
            rej.log(f"除外{i}")
        rej.flush()
        self.assertEqual(len(self.log), 4)  # 上位3件 + まとめ1行
        self.assertIn("ほか7件", self.log.entries[-1].text)

    def test_flush_is_noop_when_nothing_suppressed(self):
        rej = RejectLog(self.log, limit=10)
        rej.log("除外0")
        rej.flush()
        self.assertEqual(len(self.log), 1)  # まとめ行は追加されない

    def test_limit_is_cumulative_across_flushes(self):
        """`flush()` は省略件数の集計だけをリセットする(表示上限は

        セッション全体で1つ)。パスをまたいで際限なく出続けないように
        するのが目的なので、パスごとに上限が復活してしまうと意味が無い。
        """
        rej = RejectLog(self.log, limit=1)
        for i in range(5):
            rej.log(f"A除外{i}")
        rej.flush()
        for i in range(5):
            rej.log(f"B除外{i}")
        rej.flush()
        # 表示は最初の1件だけ。まとめ行は1回目(A側4件)・2回目(B側5件)の2行
        shown = [e.text for e in self.log.entries if "除外" in e.text and "ほか" not in e.text]
        self.assertEqual(shown, ["A除外0"])
        summaries = [e.text for e in self.log.entries if "ほか" in e.text]
        self.assertEqual(summaries, ["  …ほか4件を除外(表示は先頭1件まで)",
                                     "  …ほか5件を除外(表示は先頭1件まで)"])


class BridgeFromRejectCappingTests(unittest.TestCase):
    """`bridge_from` を通る「却下」系のログが上位N件に絞られること。

    board_selection_algorithm の `log.debug()` は、候補ボードが多いと
    却下の行を何十行も出しうる。採用やカットの行が埋もれないよう、
    却下系だけ上位N件に絞り、それ以外の行はそのまま素通しする。
    """

    def setUp(self) -> None:
        self.log = UserLog()
        self.logger = logging.getLogger("packaging_tool.board_selection_algorithm")

    def test_reject_lines_are_capped(self):
        with bridge_from(self.log):
            for i in range(30):
                self.logger.debug("却下(幅超過): %sx1000 effW=%s", i, i)
        reject_lines = [e for e in self.log.entries if "却下" in e.text]
        # 上位10件(既定) + まとめの1行 = 11行に収まる
        self.assertLessEqual(len(reject_lines), 11)
        self.assertTrue(any("ほか" in e.text for e in reject_lines))

    def test_non_reject_lines_always_pass_through(self):
        with bridge_from(self.log):
            for i in range(30):
                self.logger.debug("却下(幅超過): %sx1000", i)
            self.logger.debug("採用: 900x600 3枚")
        self.assertTrue(any("採用" in e.text for e in self.log.entries))

    def test_summary_appears_before_the_next_non_reject_line(self):
        # 却下の束のすぐあとに続く「採用」より前に、まとめ行が入ること
        # (どの段階の却下がまとまったのか、順番から追えるようにする)
        with bridge_from(self.log):
            for i in range(15):
                self.logger.debug("却下(幅超過): %sx1000", i)
            self.logger.debug("採用: 900x600 3枚")
        texts = [e.text for e in self.log.entries]
        summary_idx = next(i for i, t in enumerate(texts) if "ほか" in t)
        adopt_idx = next(i for i, t in enumerate(texts) if "採用" in t)
        self.assertLess(summary_idx, adopt_idx)

    def test_trailing_rejects_are_flushed_on_exit(self):
        # 最後の行が却下系のまま with を抜けても、まとめ行が出ること
        with bridge_from(self.log):
            for i in range(15):
                self.logger.debug("却下(幅超過): %sx1000", i)
        self.assertTrue(any("ほか" in e.text for e in self.log.entries))


def _pallet(conn, **kw) -> None:
    values = {
        "幅": 1150, "丈": 2650, "巾適合min": 1100, "巾適合max": 1200,
        "丈適合min": 2600, "丈適合max": 2700, "業界": "一般", "記号": "",
        "脚数": 3, "桁数": 3, "コード": "", "単位": "台",
        "位置": "", "更新日時": "2026-01-01 00:00:00",
    }
    values.update(kw)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO PalletMaster ({cols}) VALUES ({marks})", tuple(values.values()))
    conn.commit()


class PalletSearchLogTests(unittest.TestCase):
    """「なんでこれ検索に乗らないの?」を追えるだけのログが出るか。"""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.log = UserLog()

    def tearDown(self) -> None:
        self.conn.close()

    def _search(self, w="1150", l="2650", **kw) -> svc.AutoSelectPalletResult:
        return svc.auto_select_pallet(
            self.conn, product_width_text=w, product_length_text=l,
            user_log=self.log, **kw)

    def _texts(self) -> list[str]:
        return [e.text for e in self.log.entries]

    def test_header_reports_product_size(self):
        _pallet(self.conn)
        self._search()
        self.assertIn("製品サイズ: 1150 x 2650", self._texts())

    def test_two_stack_header_explains_the_rules(self):
        _pallet(self.conn)
        self._search(w="600", l="1200", two_stack=True)
        joined = "\n".join(self._texts())
        self.assertIn("【2山モード】", joined)
        self.assertIn("幅2山: 幅×2=1200", joined)
        self.assertIn("丈2山: 丈×2=2400", joined)

    def test_master_summary_counts_skipped_rows(self):
        _pallet(self.conn)
        _pallet(self.conn, 巾適合min=0)   # 適合範囲未設定 → 探索対象外
        self._search()
        self.assertTrue(any("2件中 1件を探索対象" in t for t in self._texts()))

    def test_inverted_range_is_called_out_separately(self):
        # min>max の行は何を検索しても当たらない。単なる「範囲外」と区別する
        _pallet(self.conn, 巾適合min=300, 巾適合max=-8)
        self._search(w="37", l="41")
        joined = "\n".join(self._texts())
        self.assertIn("適合範囲がマスタ側で逆転している行が1件", joined)
        self.assertIn("適合範囲がマスタ側で逆転(W:300~-8", joined)

    def test_reject_reason_shows_the_fit_range(self):
        _pallet(self.conn, 巾適合min=1100, 巾適合max=1200)
        self._search(w="500", l="500")
        self.assertTrue(any("範囲外(W:1100~1200 L:2600~2700)" in t for t in self._texts()))

    def test_reject_reason_shows_industry_mismatch(self):
        _pallet(self.conn, 業界="タイト")
        self._search()
        self.assertTrue(any("属性(タイト)" in t for t in self._texts()))

    def test_near_miss_is_capped_at_three(self):
        for i in range(6):
            _pallet(self.conn, 幅=500 + i, 巾適合min=400, 巾適合max=450)
        self._search(w="9999", l="9999")
        near = [t for t in self._texts() if "×惜しい" in t]
        # パスごとに最大3件
        self.assertTrue(near)
        self.assertLessEqual(len(near), svc.MAX_NEAR_MISS_LOGS * 16)

    def test_even_keta_rejections_are_listed_in_full(self):
        # サイズ・属性はOKで桁数偶数だけが理由の行は、上位3件に埋もれないよう全件出す
        for i in range(5):
            _pallet(self.conn, 幅=1150 + i, 桁数=4, 業界="一般",
                    巾適合min=1100, 巾適合max=1300, 丈適合min=1100, 丈適合max=1300)
        self._search(w="600", l="1200", two_stack=True)
        joined = "\n".join(self._texts())
        self.assertIn("△桁数偶数のため2山除外(5件):", joined)
        self.assertIn("(桁数=4/偶数のため2山不可)", joined)

    def test_leg_shortage_reason_for_length_two_stack(self):
        # 丈2山(Pass17〜)は脚3本以上が条件。幅2山(Pass1〜16)では当たらない
        # 寸法にして、丈2山パスまで到達させる
        _pallet(self.conn, 業界="スカシ", 脚数=2, 桁数=3,
                巾適合min=100, 巾適合max=200, 丈適合min=1000, 丈適合max=1100)
        self._search(w="150", l="500", two_stack=True)
        self.assertTrue(any("脚数不足(2本)" in t for t in self._texts()))

    def test_decision_line_reports_pass_and_count(self):
        _pallet(self.conn, 幅=1150, 丈=2650)
        _pallet(self.conn, 幅=1200, 丈=2700)
        self._search()
        joined = "\n".join(self._texts())
        self.assertIn("決定(最小面積): 1150x2650", joined)
        self.assertIn("← 2件中最小", joined)
        self.assertTrue(any(t.startswith("Pass ") and "で決定" in t for t in self._texts()))

    def test_rotation_is_reported_on_the_decision_line(self):
        # 回転後の寸法(2650x1150)を現物として載せられるパレットにする。
        # 適合範囲だけ辻褄を合わせて現物を小さいままにすると、
        # 実際には載らない組み合わせでテストが通ってしまう
        _pallet(self.conn, 幅=2650, 丈=1150,
                巾適合min=2600, 巾適合max=2700, 丈適合min=1100, 丈適合max=1200)
        result = self._search()
        self.assertTrue(result.rotated)
        self.assertTrue(any("で決定（製品回転）" in t for t in self._texts()))

    def test_no_match_reports_fallback_then_failure(self):
        _pallet(self.conn, 巾適合min=1, 巾適合max=2, 丈適合min=1, 丈適合max=2)
        result = self._search(w="9999", l="9999")
        self.assertFalse(result.ok)
        texts = self._texts()
        self.assertIn("適合なし → 強制入替えサイズ検索中...", texts)
        self.assertIn("適合パレットなし", texts)

    def test_each_pass_is_logged_with_search_dims(self):
        _pallet(self.conn, 巾適合min=1, 巾適合max=2, 丈適合min=1, 丈適合max=2)
        self._search(w="9999", l="8888")
        self.assertTrue(any("Pass 1: 通常(特定業界/厳密) (W=9999 L=8888 tol=±0)" in t
                            for t in self._texts()))

    def test_log_is_cleared_at_the_start_of_each_search(self):
        _pallet(self.conn)
        self.log.log("前回の残り")
        self._search()
        self.assertNotIn("前回の残り", self._texts())

    def test_search_works_without_a_user_log(self):
        _pallet(self.conn)
        result = svc.auto_select_pallet(
            self.conn, product_width_text="1150", product_length_text="2650")
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()


class SequenceTests(unittest.TestCase):
    """連番(Web版の差分取得に使う)。"""

    def setUp(self) -> None:
        self.log = UserLog()

    def test_1から順に振られる(self) -> None:
        self.assertEqual(self.log.last_seq, 0)
        self.log.log("a")
        self.log.log("b")
        self.assertEqual([e.seq for e in self.log.entries], [1, 2])
        self.assertEqual(self.log.last_seq, 2)

    def test_指定より後だけ返す(self) -> None:
        for text in ("a", "b", "c"):
            self.log.log(text)
        self.assertEqual([e.text for e in self.log.entries_since(0)], ["a", "b", "c"])
        self.assertEqual([e.text for e in self.log.entries_since(2)], ["c"])
        self.assertEqual(self.log.entries_since(3), [])

    def test_消去しても連番は戻さない(self) -> None:
        """戻すと、消去前の番号を持つ取得側が取りこぼす。"""
        self.log.log("a")
        self.log.clear()
        self.log.log("b")
        self.assertEqual(self.log.entries[0].seq, 2)
        self.assertEqual([e.text for e in self.log.entries_since(1)], ["b"])

    def test_上限で捨てられると連番が跳ぶ(self) -> None:
        """跳びを見て、取得側は「全部描き直す」と判断できる。"""
        log = UserLog(max_entries=2)
        for text in ("a", "b", "c"):
            log.log(text)
        remaining = log.entries_since(0)
        self.assertEqual([e.text for e in remaining], ["b", "c"])
        self.assertGreater(remaining[0].seq, 1)
