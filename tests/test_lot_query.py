"""仕掛一覧の絞り込み ── 条件・SQL・並べ替え・候補

【何を守っているか】
- **一覧に無い列や演算子では絞れない**(画面で弾くだけでなくサーバでも)
- **1ロット1行**。1件検索が先頭レコードしか見ないので、一覧も同じ1行だけ
  出す ── 押しても同じ内容が出る行が並ぶと、押し分けたつもりの人が
  「押した行と違うものが出た」と受け取る
- 候補は**実際にデータにある値**から作る。0件になる条件を勧めない
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import db  # noqa: E402
from packaging_tool import lot_query as q  # noqa: E402


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def insert(conn, lot_no, *, yoto_code="K100", yoto_name="一般",
           zaishitsu="A5052", choshitsu="H32", thickness=3.0,
           width=1000.0, length=2000.0) -> None:
    conn.execute(
        "INSERT INTO 仕掛ロット (ロット番号, 用途コード, 用途名, 製造材質,"
        " 製造調質, 製造板厚, 製造板幅, 製造板丈) VALUES (?,?,?,?,?,?,?,?)",
        (lot_no, yoto_code, yoto_name, zaishitsu, choshitsu,
         thickness, width, length))
    conn.commit()


class ConditionTests(unittest.TestCase):
    """条件は**作れないものを作らせない**。"""

    def test_一覧に無い列は指せない(self) -> None:
        cond, refusal = q.make_condition("秘密の列", "=", "x")
        self.assertIsNone(cond)
        self.assertEqual(refusal.reason, q.REFUSE_NOT_LISTED)

    def test_その列で使えない演算子は指せない(self) -> None:
        """文字の列に「≥」は無い。使える演算子まで文言に出す。"""
        cond, refusal = q.make_condition("zaishitsu", q.OP_GE, "A")
        self.assertIsNone(cond)
        self.assertEqual(refusal.reason, q.REFUSE_NOT_LISTED)
        self.assertIn(q.OP_CONTAINS, refusal.message)

    def test_数値の列に数字でないものは入れられない(self) -> None:
        cond, refusal = q.make_condition("thickness", "=", "あつい")
        self.assertIsNone(cond)
        self.assertEqual(refusal.reason, q.REFUSE_BAD_INPUT)

    def test_空の値は断る(self) -> None:
        _, refusal = q.make_condition("zaishitsu", "=", "   ")
        self.assertEqual(refusal.reason, q.REFUSE_BAD_INPUT)

    def test_通れば読める文言になる(self) -> None:
        cond, refusal = q.make_condition("thickness", q.OP_GE, " 3 ")
        self.assertIsNone(refusal)
        self.assertEqual(cond.label, "製造板厚 ≥ 3")


class FetchTests(unittest.TestCase):

    def setUp(self) -> None:
        self.conn = make_conn()
        self.addCleanup(self.conn.close)
        insert(self.conn, "1111111", zaishitsu="A5052", thickness=3.0)
        insert(self.conn, "2222222", zaishitsu="A1050", thickness=0.8)
        insert(self.conn, "3333333", zaishitsu="A5052", thickness=8.0,
               yoto_name="ﾃﾞﾝﾁﾌﾀ")

    def lots(self, conditions, **kw):
        return [r["ロット番号"] for r in q.fetch(self.conn, conditions, **kw)]

    def test_条件なら全件(self) -> None:
        self.assertEqual(self.lots([]), ["1111111", "2222222", "3333333"])

    def test_文字で絞る(self) -> None:
        cond, _ = q.make_condition("zaishitsu", "=", "A5052")
        self.assertEqual(self.lots([cond]), ["1111111", "3333333"])

    def test_含むで絞る(self) -> None:
        cond, _ = q.make_condition("yoto_name", q.OP_CONTAINS, "ﾌﾞﾀ")
        self.assertEqual(self.lots([cond]), [])
        cond, _ = q.make_condition("yoto_name", q.OP_CONTAINS, "ﾃﾞﾝﾁ")
        self.assertEqual(self.lots([cond]), ["3333333"])

    def test_数値の帯で絞る(self) -> None:
        cond, _ = q.make_condition("thickness", q.OP_GE, "3")
        self.assertEqual(self.lots([cond]), ["1111111", "3333333"])

    def test_条件は重ねるとANDになる(self) -> None:
        a, _ = q.make_condition("zaishitsu", "=", "A5052")
        b, _ = q.make_condition("thickness", q.OP_GE, "5")
        self.assertEqual(self.lots([a, b]), ["3333333"])

    def test_一覧検索は出ている列を横断する(self) -> None:
        """どの列に入っているか分からないから打つので、列は指定させない。"""
        self.assertEqual(self.lots([], text="A1050"), ["2222222"])
        self.assertEqual(self.lots([], text="ﾃﾞﾝﾁ"), ["3333333"])
        # 数値の列も文字として引っかかる
        self.assertEqual(self.lots([], text="0.8"), ["2222222"])

    def test_並べ替え(self) -> None:
        self.assertEqual(self.lots([], sort="thickness"),
                         ["2222222", "1111111", "3333333"])
        self.assertEqual(self.lots([], sort="thickness", descending=True),
                         ["3333333", "1111111", "2222222"])

    def test_同値でも並びが動かない(self) -> None:
        """安定しないと、開き直すたびに行が入れ替わって見失う。"""
        insert(self.conn, "4444444", zaishitsu="A5052", thickness=3.0)
        first = self.lots([], sort="zaishitsu")
        for _ in range(3):
            self.assertEqual(self.lots([], sort="zaishitsu"), first)

    def test_表示件数で切る(self) -> None:
        self.assertEqual(len(self.lots([], limit=2)), 2)

    def test_件数は切る前の数(self) -> None:
        """「200件」が全部なのか切られたのかを言えるようにするため。"""
        self.assertEqual(q.count(self.conn, []), 3)

    def test_知らない列で並べ替えても落ちない(self) -> None:
        """既定へ倒す。要求は直接投げられるので、落ちるのは筋が悪い。"""
        self.assertEqual(self.lots([], sort="でたらめ"),
                         ["1111111", "2222222", "3333333"])


class RepresentativeRowTests(unittest.TestCase):
    """同じロット番号が複数行あっても、一覧には1行しか出さない。

    `lot_service._load_lot_info` が `ORDER BY 管理番号 LIMIT 1` で
    先頭だけを見るので、一覧もその1行に揃える。揃えないと、押しても
    同じ内容が出る行が並ぶ。
    """

    def setUp(self) -> None:
        self.conn = make_conn()
        self.addCleanup(self.conn.close)
        # BOX工程ごとに行が分かれている状態(実データで実際に起きる)
        insert(self.conn, "1234567", thickness=3.0)
        insert(self.conn, "1234567", thickness=9.9)
        insert(self.conn, "7654321", thickness=1.0)

    def test_1ロット1行(self) -> None:
        rows = q.fetch(self.conn, [])
        self.assertEqual([r["ロット番号"] for r in rows], ["1234567", "7654321"])

    def test_件数も1ロット1件(self) -> None:
        self.assertEqual(q.count(self.conn, []), 2)

    def test_出るのは先頭レコード(self) -> None:
        """1件検索が返すものと同じ行であること。"""
        from packaging_tool import lot_service
        rows = q.fetch(self.conn, [])
        listed = next(r for r in rows if r["ロット番号"] == "1234567")
        picked = lot_service.search_lot(self.conn, "1234567")
        self.assertEqual(listed["製造板厚"], picked.lot.thickness)

    def test_2件目の値では絞れない(self) -> None:
        """代表行に無い値で絞れると、一覧に出ない行を指せてしまう。"""
        cond, _ = q.make_condition("thickness", "=", "9.9")
        self.assertEqual(q.fetch(self.conn, [cond]), [])


class SuggestTests(unittest.TestCase):

    def setUp(self) -> None:
        self.conn = make_conn()
        self.addCleanup(self.conn.close)
        insert(self.conn, "1111111", zaishitsu="A5052", thickness=3.0)
        insert(self.conn, "2222222", zaishitsu="A1050", thickness=0.8)

    def labels(self, text, saved=None):
        return [s.label for s in q.suggest(self.conn, text, saved)]

    def test_データにある値から作る(self) -> None:
        self.assertIn("製造材質 = A5052", self.labels("A50"))

    def test_無い値は勧めない(self) -> None:
        """0件になる条件を勧めても意味がない。"""
        self.assertEqual([l for l in self.labels("ZZZZ") if "材質" in l], [])

    def test_数値を打つと数値の列が先に出る(self) -> None:
        """「3」と打つ人が探しているのはたいてい板厚で、
        「3を含むロット番号」ではない。"""
        first = self.labels("3")[0]
        self.assertTrue(first.startswith("製造板厚"), first)

    def test_数値は帯も出す(self) -> None:
        labels = self.labels("3")
        self.assertIn("製造板厚 ≥ 3", labels)
        self.assertIn("製造板厚 ≤ 3", labels)

    def test_保存フィルタも候補に出る(self) -> None:
        saved = {"薄板": [{"column": "thickness", "op": "≤", "value": "1"}]}
        self.assertIn("薄板", self.labels("薄", saved))

    def test_空なら保存フィルタだけ出す(self) -> None:
        saved = {"薄板": []}
        self.assertEqual(self.labels("", saved), ["薄板"])

    def test_出しすぎない(self) -> None:
        for i in range(40):
            insert(self.conn, f"9{i:06d}", zaishitsu=f"X{i:03d}")
        self.assertLessEqual(len(self.labels("X")), q.SUGGEST_TOTAL)


class HikiFlagTests(unittest.TestCase):
    """引当有無(SIKAHIKINOW に同じロット番号があるか)の 1/0 フラグ。

    仕掛ロットには実体が無い**計算列**なので、表示だけでなく
    絞り込み・並べ替え・横断検索まで実列と同じに効くことを確かめる。
    """

    def setUp(self) -> None:
        self.conn = make_conn()
        for lot_no in ("1111111", "2222222", "3333333"):
            insert(self.conn, lot_no)
        # 引当があるのは1番と3番だけ。1番は2件あるが、行が増えてはいけない
        for lot_no, hiki_no in (("1111111", "60000001"), ("1111111", "60000002"),
                                ("3333333", "60000003")):
            self.conn.execute(
                "INSERT INTO 仕掛引当 (ロット番号, 引当番号) VALUES (?,?)",
                (lot_no, hiki_no))
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _flags(self, rows) -> dict[str, int]:
        return {str(r["ロット番号"]): r["引当有無"] for r in rows}

    def test_有は1_無は0(self) -> None:
        self.assertEqual(self._flags(q.fetch(self.conn, [])),
                         {"1111111": 1, "2222222": 0, "3333333": 1})

    def test_引当が複数あっても行は増えない(self) -> None:
        """1番は引当2件。`EXISTS` なので件数ではなく有無だけを見る。"""
        rows = q.fetch(self.conn, [])
        self.assertEqual(len(rows), 3)
        self.assertEqual(self._flags(rows)["1111111"], 1)

    def test_有無で絞れる(self) -> None:
        has = q.Condition("hiki", q.OP_EQ, "1")
        self.assertEqual([str(r["ロット番号"]) for r in q.fetch(self.conn, [has])],
                         ["1111111", "3333333"])
        none = q.Condition("hiki", q.OP_EQ, "0")
        self.assertEqual([str(r["ロット番号"]) for r in q.fetch(self.conn, [none])],
                         ["2222222"])

    def test_絞った件数も合う(self) -> None:
        """`count` は `SELECT *` を通らないので、別経路として確かめる。"""
        self.assertEqual(q.count(self.conn, [q.Condition("hiki", q.OP_EQ, "1")]), 2)
        self.assertEqual(q.count(self.conn, [q.Condition("hiki", q.OP_EQ, "0")]), 1)

    def test_並べ替えに使える(self) -> None:
        rows = q.fetch(self.conn, [], sort="hiki", descending=True)
        self.assertEqual([r["引当有無"] for r in rows], [1, 1, 0])
        rows = q.fetch(self.conn, [], sort="hiki")
        self.assertEqual([r["引当有無"] for r in rows], [0, 1, 1])

    def test_一覧の表示は1と0(self) -> None:
        col = q.BY_KEY["hiki"]
        self.assertEqual(q.format_value(col, 1), "1")
        self.assertEqual(q.format_value(col, 0), "0")

    def test_候補は0と1のときだけ出す(self) -> None:
        """取りうる値が 0/1 しか無い列に「= 3」を勧めても0件にしかならない。"""
        labels = [s.label for s in q.suggest(self.conn, "1")]
        self.assertIn("引当有無 = 1", labels)
        # 帯(≥ / ≤)は2値には意味が無いので出さない
        self.assertNotIn("引当有無 ≥ 1", labels)
        self.assertEqual(
            [s for s in q.suggest(self.conn, "3") if s.column == "hiki"], [])

    def test_数値の列より前に出す(self) -> None:
        """候補は12件で打ち切られる。埋もれると一度も出てこない。"""
        self.assertEqual(q.suggest(self.conn, "1")[0].label, "引当有無 = 1")


class FormatTests(unittest.TestCase):

    def test_小数の末尾は落とす(self) -> None:
        col = q.BY_KEY["thickness"]
        self.assertEqual(q.format_value(col, 3.0), "3")
        self.assertEqual(q.format_value(col, 0.85), "0.85")

    def test_空は空のまま(self) -> None:
        self.assertEqual(q.format_value(q.BY_KEY["yoto_name"], None), "")


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
