"""実績(スナップショット)の保存庫 ── VBA `modPatternStore` の移植"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import config, db  # noqa: E402
from packaging_tool import pattern_store as ps  # noqa: E402
from packaging_tool.board_selection_types import ProtecCutResult  # noqa: E402

H, SELT, PLACET, CUTT = (config.TBL_PT_HEADER, config.TBL_PT_SELECT,
                         config.TBL_PT_PLACE, config.TBL_PT_CUT)


def memory() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def header(**over) -> dict:
    base = {"パレット幅": 1150, "パレット丈": 2650, "製品幅": 1122, "製品丈": 2502,
            "製品回転": 0, "ボード種別": "ハードボード", "配置方式": "通常",
            "狭幅下": 0, "狭幅上": 0, "保護材": "", "アングル": "1550,1550",
            "プロテック確定値": "", "LotNo": "1234567", "拠点": "A"}
    base.update(over)
    return base


SEL = [{"区分": "下用", "行順": 1, "幅": 1150, "丈": 2500, "枚数": 1, "タグ": "主"},
       {"区分": "下用", "行順": 2, "幅": 100, "丈": 2000, "枚数": 1, "タグ": "丈補填"},
       {"区分": "上用", "行順": 1, "幅": 1122, "丈": 2500, "枚数": 1, "タグ": ""}]
PLACE = [{"区分": "下用", "順番": 1, "板ID": 1, "インスタンスID": "下用_1",
          "座標X": 0, "座標Y": 0, "幅": 1150, "丈": 2500, "元幅": 1150,
          "元丈": 2500, "補填": 0},
         {"区分": "下用", "順番": 2, "板ID": 2, "インスタンスID": "下用_2",
          "座標X": 2500, "座標Y": 0, "幅": 1150, "丈": 100, "元幅": 100,
          "元丈": 2000, "補填": 1}]
CUT = [{"種別": "幅カット", "キー": "100x2000", "値": 100},
       {"種別": "丈カット長", "キー": "1150x2500", "値": 2400}]


def count(conn, table) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


class SaveLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = memory()

    def test_保存したものがそのまま戻る(self) -> None:
        pid = ps.save_pattern_snapshot(self.conn, header(), SEL, PLACE, CUT)
        snap = ps.load_pattern_snapshot(self.conn, pid)
        self.assertEqual(snap.header["配置方式"], "通常")
        self.assertEqual(snap.header["保存形式"], config.PT_FORMAT_VER)
        # VBA と同じく 行順 で並べる(区分ごとの順が保たれていればよい)
        by = lambda cat: [(r["幅"], r["タグ"]) for r in snap.select_rows  # noqa: E731
                          if r["区分"] == cat]
        self.assertEqual(by("下用"), [(1150, "主"), (100, "丈補填")])
        self.assertEqual(by("上用"), [(1122, None)])
        self.assertEqual([(r["座標X"], r["座標Y"], r["補填"]) for r in snap.place_rows],
                         [(0, 0, 0), (2500, 0, 1)])
        self.assertEqual({(r["種別"], r["キー"], r["値"]) for r in snap.cut_rows},
                         {("幅カット", "100x2000", 100), ("丈カット長", "1150x2500", 2400)})

    def test_空文字はNULLで書く(self) -> None:
        """VBA `DbValue`。テキスト列の空文字の扱いに依存しない。"""
        pid = ps.save_pattern_snapshot(self.conn, header(保護材=""), SEL, [], [])
        row = self.conn.execute(f'SELECT 保護材 FROM "{H}" WHERE 実績ID=?',
                                (pid,)).fetchone()
        self.assertIsNone(row[0])

    def test_読むと使用回数が増える_共有へ足す分も覚える(self) -> None:
        pid = ps.save_pattern_snapshot(self.conn, header(), SEL, PLACE, CUT)
        ps.load_pattern_snapshot(self.conn, pid)
        ps.load_pattern_snapshot(self.conn, pid)
        row = self.conn.execute(f'SELECT 使用回数, 使用回数未反映 FROM "{H}"'
                                ' WHERE 実績ID=?', (pid,)).fetchone()
        self.assertEqual(tuple(row), (2, 2))

    def test_無い実績は断る(self) -> None:
        with self.assertRaisesRegex(ps.PatternStoreError, "見つかりません"):
            ps.load_pattern_snapshot(self.conn, 999)

    def test_保存形式が違えば読まない(self) -> None:
        pid = ps.save_pattern_snapshot(self.conn, header(), SEL, PLACE, CUT)
        self.conn.execute(f'UPDATE "{H}" SET 保存形式=99 WHERE 実績ID=?', (pid,))
        with self.assertRaisesRegex(ps.PatternStoreError, "保存形式"):
            ps.load_pattern_snapshot(self.conn, pid)

    def test_知らない列は断る_何も書かない(self) -> None:
        with self.assertRaises(ps.PatternStoreError):
            ps.save_pattern_snapshot(self.conn, header(), [{"区分": "下用", "謎": 1}], [], [])
        self.assertEqual(count(self.conn, H), 0)

    def test_途中で失敗したら全部戻す(self) -> None:
        """ヘッダだけ残る・明細が半分だけ、を作らない(1トランザクション)。"""
        real = ps._insert
        calls = {"n": 0}

        def flaky(conn, table, values):
            calls["n"] += 1
            if calls["n"] == 4:                  # ヘッダ+明細2行のあと
                raise sqlite3.OperationalError("disk I/O error")
            return real(conn, table, values)

        with mock.patch.object(ps, "_insert", side_effect=flaky):
            with self.assertRaises(ps.PatternStoreError):
                ps.save_pattern_snapshot(self.conn, header(), SEL, PLACE, CUT)
        for table in (H, SELT, PLACET, CUTT):
            self.assertEqual(count(self.conn, table), 0, table)

    def test_送信IDは保存ごとに違う(self) -> None:
        a = ps.save_pattern_snapshot(self.conn, header(), SEL, [], [])
        b = ps.save_pattern_snapshot(self.conn, header(), SEL, [], [])
        ids = {r[0] for r in self.conn.execute(
            f'SELECT 送信ID FROM "{H}" WHERE 実績ID IN (?,?)', (a, b))}
        self.assertEqual(len(ids), 2)


class ListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = memory()

    def test_新しい順_パレットで絞る_ボード構成に方式と印(self) -> None:
        ps.save_pattern_snapshot(self.conn, header(), SEL, PLACE, CUT)
        ps.save_pattern_snapshot(self.conn, header(配置方式="別案B"), SEL, [], [])
        ps.save_pattern_snapshot(self.conn, header(パレット幅=1000), SEL, [], [])
        rows = ps.get_pattern_list(self.conn, 1150, 2650)
        self.assertEqual([r.method for r in rows], ["別案B", "通常"])
        self.assertEqual(rows[1].board_summary,
                         "[通常] 下:1150x2500(1) 100x2000(1)[丈] / 上:1122x2500(1)")
        self.assertEqual(len(ps.get_pattern_list(self.conn)), 3)

    def test_保存形式が違うものは出さない(self) -> None:
        pid = ps.save_pattern_snapshot(self.conn, header(), SEL, [], [])
        self.conn.execute(f'UPDATE "{H}" SET 保存形式=0 WHERE 実績ID=?', (pid,))
        self.assertEqual(ps.get_pattern_list(self.conn), [])

    def test_未送信が分かる(self) -> None:
        ps.save_pattern_snapshot(self.conn, header(), SEL, [], [])
        self.assertTrue(ps.get_pattern_list(self.conn)[0].unsent)


class DeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = memory()

    def test_ヘッダと明細を全部消す_共有へ伝える分を覚える(self) -> None:
        pid = ps.save_pattern_snapshot(self.conn, header(), SEL, PLACE, CUT)
        other = ps.save_pattern_snapshot(self.conn, header(), SEL, PLACE, CUT)
        self.assertTrue(ps.delete_pattern_by_id(self.conn, pid))
        for table in (SELT, PLACET, CUTT):
            ids = {r[0] for r in self.conn.execute(f'SELECT 実績ID FROM "{table}"')}
            self.assertEqual(ids, {other}, table)
        self.assertEqual(count(self.conn, ps.TBL_PT_DELETED), 1)

    def test_無ければFalse(self) -> None:
        self.assertFalse(ps.delete_pattern_by_id(self.conn, 42))


class ProtecTextTests(unittest.TestCase):
    def test_往復で同じ値(self) -> None:
        r = ProtecCutResult(valid=True, orig_width=1000, orig_length=2000,
                            is_rotated=True, eff_width_before_cut=1010,
                            cut_eff_width=990, eff_length=1000, need_cut=True,
                            count=3, need_length_cut=True, length_cut_eff=500,
                            len_normal_cnt=2, len_cut_cnt=1, len_cut_optional=True,
                            len_opt_normal_cnt=1, len_opt_cut_cnt=1,
                            len_opt_cut_eff=700)
        self.assertEqual(ps.protec_from_text(ps.protec_to_text(r)), r)

    def test_VBAと同じ順の16個_17個目はPythonだけ(self) -> None:
        r = ProtecCutResult(valid=True, orig_width=1000, orig_length=2000,
                            cut_eff_width=990, eff_length=1000, count=3,
                            eff_width_before_cut=1010)
        values = ps.protec_to_text(r).split(",")
        self.assertEqual(values[:8], ["1", "1000", "2000", "0", "990", "1000", "0", "3"])
        self.assertEqual(len(values), 17)

    def test_VBAが書いた16個も読める(self) -> None:
        r = ps.protec_from_text("1,1000,2000,1,990,1000,1,3,0,3,0,0,0,0,0,0")
        self.assertTrue(r.valid and r.is_rotated and r.need_cut)
        self.assertEqual((r.cut_eff_width, r.count), (990, 3))
        self.assertEqual(r.eff_width_before_cut, 990)

    def test_無効なら空_空や短い文字は無効(self) -> None:
        self.assertEqual(ps.protec_to_text(ProtecCutResult()), "")
        self.assertEqual(ps.protec_to_text(None), "")
        self.assertFalse(ps.protec_from_text("").valid)
        self.assertFalse(ps.protec_from_text("1,2,3").valid)

    def test_PtLong(self) -> None:
        self.assertEqual([ps.pt_long(v) for v in (None, "", "12", 3.0, "x")],
                         [0, 0, 12, 3, 0])


if __name__ == "__main__":
    unittest.main()
