"""選定・配置・描画計画がゴールデンから動いていないか

`scripts/compare_ui.py` を毎回のテストで走らせる。手で叩くのを忘れても
気づけるようにするため。役割の説明はスクリプト側の docstring にある。

ゴールデンは `PalletPatterns` と `BoardMaster` の中身に依存する。
実データを取り込み直して結果が変わった場合は、変わったことを確認したうえで

    python3 scripts/compare_ui.py --update

でゴールデンを作り直す。**差分が出たらまず「変えたつもりが無いのに
変わっていないか」を疑うこと。**
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import compare_ui  # noqa: E402
from packaging_tool import config, db  # noqa: E402


# **基準は本物のDB。** `config.DB_PATH` は試験の書き込み先として
# 一時フォルダへ逃がしてあるので(`tests/_isolation.py`)、ここで
# それを使うと突き合わせる中身が無くなる。読むだけなので本物でよい
REFERENCE_DB = Path(config.BASE_DIR) / "data" / "packaging_tool.db"


def _data_available() -> bool:
    """ゴールデンを再現できるデータがDBにあるか。

    サンプルデータ未投入のクローンでもテスト一式が流せるよう、
    足りなければスキップする(失敗にはしない)。
    """
    if not compare_ui.GOLDEN_PATH.exists():
        return False
    conn = db.get_connection(REFERENCE_DB)
    try:
        from packaging_tool import board_selection_service as svc
        return bool(svc.list_board_types(conn)) and bool(compare_ui.load_cases(conn))
    except Exception:       # noqa: BLE001 - テーブルが無い等はスキップ扱い
        return False
    finally:
        conn.close()


HAS_DATA = _data_available()
_SKIP = "ゴールデンまたは実データが無いためスキップ"


@unittest.skipUnless(HAS_DATA, _SKIP)
class GoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = db.get_connection(REFERENCE_DB)
        cls.current = compare_ui.build(cls.conn)
        cls.golden = json.loads(
            compare_ui.GOLDEN_PATH.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def test_ゴールデンと一致する(self) -> None:
        problems = compare_ui.compare(self.current, self.golden)
        self.assertEqual(
            problems, [],
            "選定・配置・描画計画がゴールデンから変わっています。\n"
            "意図した変更なら `python3 scripts/compare_ui.py --update` で更新してください。\n"
            + "\n".join(f"  - {p}" for p in problems[:20]))

    def test_41通りを対象にしている(self) -> None:
        """README が「41通りで確認済み」と書いている件数。

        データを入れ替えて件数が変わったら、READMEの記述も直すこと。
        """
        self.assertEqual(self.golden["_case_count"], 41)
        self.assertEqual(len(self.golden["cases"]), 41)

    def test_論理キャンバスが設計値と一致する(self) -> None:
        """SVGの viewBox に使う値(設計書 §7.1)。

        変えると全ケースの座標が変わるので、気づかず変わらないよう固定する。
        """
        self.assertEqual(self.golden["_logical_canvas"], [980, 460])

    def test_全ケースで例外が出ない(self) -> None:
        """READMEの「例外なし」という主張を機械で担保する。"""
        self.assertEqual(len(self.current["cases"]), self.golden["_case_count"])

    def test_同カテゴリ内で重ならない(self) -> None:
        """READMEの「同カテゴリ内の重なりなし」という主張の担保。

        座標系はVBA踏襲で X=丈方向 / Y=幅方向。
        `width` がY方向、`length` がX方向の寸法。
        """
        for name, case in self.current["cases"].items():
            for category in ("下用", "上用"):
                boards = [b for b in case["placed"] if b["category"] == category]
                for i in range(len(boards)):
                    for j in range(i + 1, len(boards)):
                        a, b = boards[i], boards[j]
                        overlap_x = (a["x"] < b["x"] + b["length"]
                                     and b["x"] < a["x"] + a["length"])
                        overlap_y = (a["y"] < b["y"] + b["width"]
                                     and b["y"] < a["y"] + a["width"])
                        self.assertFalse(
                            overlap_x and overlap_y,
                            f"{name} の{category}で重なっています: "
                            f"{a} と {b}")


@unittest.skipUnless(HAS_DATA, _SKIP)
@unittest.skipUnless(HAS_DATA, _SKIP)
class DeterminismTests(unittest.TestCase):
    """同じ入力で2回走らせて同じ結果になること。

    ここが揺れると、ゴールデンとの差分が「変更のせい」なのか
    「もともと揺れているせい」なのか判別できなくなる。
    """

    def test_2回走らせても同じ(self) -> None:
        conn = db.get_connection(REFERENCE_DB)
        try:
            first = compare_ui.dump(compare_ui.build(conn))
            second = compare_ui.dump(compare_ui.build(conn))
        finally:
            conn.close()
        self.assertEqual(first, second)


class DiffDetectionTests(unittest.TestCase):
    """比較そのものが機能しているか(空振りしていないこと)。

    比較器が常に「一致」を返すなら、上のテストは何も守っていない。
    """

    def test_値の違いを見つける(self) -> None:
        a = {"cases": {"x": {"selected_lower": [{"count": 2}]}}}
        b = {"cases": {"x": {"selected_lower": [{"count": 3}]}}}
        problems = compare_ui.compare(a, b)
        self.assertTrue(problems)
        self.assertIn("count", problems[0])

    def test_件数の違いを見つける(self) -> None:
        a = {"cases": {"x": {"placed": [1, 2]}}}
        b = {"cases": {"x": {"placed": [1]}}}
        self.assertTrue(any("件数" in p for p in compare_ui.compare(a, b)))

    def test_ケースの増減を見つける(self) -> None:
        self.assertTrue(any("増えています" in p for p in
                            compare_ui.compare({"cases": {"x": {}}}, {"cases": {}})))
        self.assertTrue(any("消えています" in p for p in
                            compare_ui.compare({"cases": {}}, {"cases": {"x": {}}})))

    def test_ボード種別の違いを見つける(self) -> None:
        problems = compare_ui.compare(
            {"_board_type": "A", "cases": {}}, {"_board_type": "B", "cases": {}})
        self.assertTrue(any("_board_type" in p for p in problems))

    def test_一致していれば空(self) -> None:
        same = {"_board_type": "A", "_logical_canvas": [980, 460],
                "cases": {"x": {"placed": [{"x": 1}]}}}
        self.assertEqual(compare_ui.compare(same, json.loads(json.dumps(same))), [])


if __name__ == "__main__":
    unittest.main()
