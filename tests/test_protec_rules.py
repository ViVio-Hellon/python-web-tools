"""プロテックモード: 強制ボタンと、条件の表(プロテック条件)の試験

現場の声:
「1. プロテックモードの強制起動ボタンの追加(1P0113モードのような強制ON)。ボードがプロテックに代わり、
   ボード配置、切断依頼がプロテックの挙動になる
 2. プロテックモードに条件の追加。1P1125 は SIKALOT のｵｰﾀﾞｰ板厚 3.0〜15.0。1P1255 はｵｰﾀﾞｰ厚 12.0 かつ
   (幅 1075.0〜1081.0・丈 2332.0〜2338.0 または 幅 1072.0〜1078.0・丈 2262.0〜2268.0)。
   プロテックモードのテーブルを足し、マスタ編集で条件行を足すことで発火条件を外から足せるように」

【ここで守りたいこと】
1. 条件は表の行。どれか1行に当たればプロテック。行の中は包装仕様NO かつ ｵｰﾀﾞｰ厚・幅・丈の範囲(空は見ない)
2. 表に行を足せば、その包装仕様NO・寸法でプロテックになる(プログラムを直さずに)。有効=0 の行は見ない
3. 表が無い・読めないときは初期値で前と同じに動く。起動時に空なら初期値を入れ、直した値は書き戻さない
4. 「プロテック 強制」で包装仕様NOに関わらずプロテック(ボード種別・選定・配置・切断依頼が従う)。
   ロットを切り替える・やり直すと解除。押したとき選定済みのボードは外す
5. マスタ管理で直せる表に出て、取り込み元に作ると初期値も入る
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import db, import_specs, master_common, protec_rules  # noqa: E402
from packaging_tool import special_packaging as spk  # noqa: E402
from packaging_tool.presenters.selection import SelectionPresenter  # noqa: E402
from packaging_tool.user_log import UserLog  # noqa: E402

TABLE = protec_rules.TABLE


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def add_rule(conn, spec, *, t=(None, None), w=(None, None), l=(None, None), enabled=1) -> None:
    conn.execute(
        f'INSERT INTO "{TABLE}" (包装仕様NO, オーダー厚_最小, オーダー厚_最大, オーダー幅_最小,'
        " オーダー幅_最大, オーダー丈_最小, オーダー丈_最大, 有効) VALUES (?,?,?,?,?,?,?,?)",
        (spec, *t, *w, *l, enabled))
    conn.commit()


class RuleTests(unittest.TestCase):
    def test_初期値は依頼どおり(self) -> None:
        self.assertEqual([r.describe() for r in protec_rules.DEFAULT_RULES], [
            "1P1216", "1P1211", "1P1125 ｵｰﾀﾞｰ厚 3〜15",
            "1P1255 ｵｰﾀﾞｰ厚 12 / 幅 1075〜1081 / 丈 2332〜2338",
            "1P1255 ｵｰﾀﾞｰ厚 12 / 幅 1072〜1078 / 丈 2262〜2268"])

    def test_包装仕様NOの書き方のゆれは同じに扱う(self) -> None:
        for spec in ("1P1216", " 1p1216 ", "1P 1216"):
            with self.subTest(spec=spec):
                self.assertIsNotNone(protec_rules.find(protec_rules.DEFAULT_RULES, spec))

    def test_空と0の欄は見ない_片側だけも書ける(self) -> None:
        rule = protec_rules._rule_of({"包装仕様NO": "9X", "オーダー厚_最小": 0,
                                      "オーダー厚_最大": "", "オーダー幅_最小": 1000,
                                      "オーダー幅_最大": None})
        self.assertTrue(rule.matches("9X", 0, 1000, 0))
        self.assertTrue(rule.matches("9X", 0, 5000, 0))
        self.assertFalse(rule.matches("9X", 0, 999, 0))
        self.assertEqual(rule.describe(), "9X ｵｰﾀﾞｰ幅 1000以上")

    def test_表に足した行で入る_有効0は見ない(self) -> None:
        conn = make_conn()
        self.assertFalse(spk.protec_state("1P9999", rules=protec_rules.load(conn)).is_protec)
        add_rule(conn, "1P9999", t=(5.0, 8.0))
        add_rule(conn, "1P8888", enabled=0)
        rules = protec_rules.load(conn)
        self.assertTrue(spk.protec_state("1P9999", thickness=6, rules=rules).is_protec)
        self.assertFalse(spk.protec_state("1P9999", thickness=9, rules=rules).is_protec)
        self.assertFalse(spk.protec_state("1P8888", rules=rules).is_protec)

    def test_表を読めなければ初期値(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.assertEqual(protec_rules.load(conn), protec_rules.DEFAULT_RULES)

    def test_空なら初期値を入れ_直した値は書き戻さない(self) -> None:
        conn = make_conn()
        self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0],
                         len(protec_rules.SEED))
        conn.execute(f'DELETE FROM "{TABLE}" WHERE 包装仕様NO = ?', ("1P1211",))
        conn.commit()
        self.assertEqual(protec_rules.seed(conn), 0)
        db.apply_schema(conn)                                    # 次の起動
        self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0],
                         len(protec_rules.SEED) - 1)

    def test_強制なら条件に関わらず入る(self) -> None:
        state = spk.protec_state("1P0001", forced=True)
        self.assertTrue(state.is_protec)
        self.assertTrue(state.forced)
        self.assertEqual(state.board_type, spk.PROTEC_BOARD_TYPE)
        self.assertFalse(state.is_1p1216)
        self.assertTrue(spk.protec_state("1P1216", forced=True).is_1p1216)


class FakeLot:
    def __init__(self, t, w, l):
        self.order_thickness, self.order_width, self.order_length = t, w, l


class FakeOdr:
    def __init__(self, spec):
        self.packaging_spec = spec


class FakeResult:
    def __init__(self, spec, t, w, l):
        self.lot, self.odr = FakeLot(t, w, l), FakeOdr(spec)


class PresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()
        self.log = UserLog()
        self.p = SelectionPresenter(self.conn, user_log=self.log)

    def logged(self) -> str:
        return self.log.text

    def test_ロットのオーダー寸法で決まる(self) -> None:
        self.p.lot_result = FakeResult("1P1255", 12.0, 1076.0, 2335.0)
        self.p.check_and_set_protec_mode("1P1255", order_size=self.p.order_size())
        self.assertTrue(self.p.protec.is_protec)
        self.assertIn("条件 1P1255 ｵｰﾀﾞｰ厚 12", self.logged())

    def test_当たらなければ理由を残す(self) -> None:
        self.p.check_and_set_protec_mode("1P1255", order_size=(12.0, 1076.0, 2300.0))
        self.assertFalse(self.p.protec.is_protec)
        self.assertIn("条件にｵｰﾀﾞｰ寸法が当たりません", self.logged())
        self.assertIn("丈 2300", self.logged())

    def test_強制を押すと入り_戻すと条件で決め直す(self) -> None:
        self.p.lot_result = FakeResult("1P0001", 3.0, 1000.0, 2000.0)
        outcome = self.p.toggle_force_protec(
            available_board_types=["ハードボード", "プロテックボード"],
            current_board_type="ハードボード")
        self.assertTrue(self.p.protec.is_protec)
        self.assertTrue(outcome.should_switch)
        self.assertEqual(outcome.target_board_type, "プロテックボード")
        self.assertTrue(self.p.selection_flags()["is_protec_mode"])
        self.assertEqual(self.p.mode_banner().text, "【プロテックボードオーダー選択中】(強制)")
        self.assertTrue(self.p.is_shared_board_mode)              # 上用は下用と同じ(上下共用の画面)
        outcome = self.p.toggle_force_protec(
            available_board_types=["ハードボード", "プロテックボード"],
            current_board_type="プロテックボード")
        self.assertFalse(self.p.protec.is_protec)
        self.assertEqual(outcome.target_board_type, "ハードボード")


class MasterTests(unittest.TestCase):
    def test_マスタ管理で直せる表_取り込み元に作れる(self) -> None:
        from packaging_tool import master_schema
        self.assertIn(TABLE, master_common.BY_TABLE)
        self.assertIn(TABLE, import_specs.OPTIONAL_TABLES)
        self.assertTrue(master_schema.can_create(TABLE))
        self.assertEqual(import_specs.PROTEC_RULE_TABLE, TABLE)

    def test_列の横に読み方が出る(self) -> None:
        from packaging_tool import master_columns
        conn = make_conn()
        notes = {c.name: c for c in master_columns.columns(conn, TABLE)}
        self.assertTrue(notes["包装仕様NO"].required)
        self.assertFalse(notes["オーダー厚_最小"].required)
        self.assertIn("空欄なら見ない", notes["オーダー厚_最小"].note)


class CreateAndAddTests(unittest.TestCase):
    """取り込み元に作る → 1行足す → 手元に取り込まれて、その条件で入る。"""

    def setUp(self) -> None:
        from packaging_tool import config, selection_session
        self.dir = Path(tempfile.mkdtemp(prefix="protec_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        sqlite3.connect(self.src).close()
        self.conn = make_conn()
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        selection_session.get_session(self.conn).admin = True

    def test_作ると初期値が入り_足した行が効く(self) -> None:
        from packaging_tool import master_admin
        made = master_admin.create_table(self.conn, TABLE, path=self.src)
        self.assertTrue(made.ok, made.message)
        src = sqlite3.connect(self.src)
        self.assertEqual(src.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0],
                         len(protec_rules.SEED))
        src.close()
        added = master_admin.add_row(self.conn, TABLE, {
            "包装仕様NO": "1P7777", "オーダー厚_最小": "4", "オーダー厚_最大": "6",
            "有効": "1", "並び順": "50", "備考": "試験"}, path=self.src)
        self.assertTrue(added.ok, added.message)
        rules = protec_rules.load(self.conn)                    # 書いたら手元も追いつく
        self.assertTrue(spk.protec_state("1P7777", thickness=5, rules=rules).is_protec)
        self.assertFalse(spk.protec_state("1P7777", thickness=7, rules=rules).is_protec)
        self.assertTrue(spk.protec_state("1P1216", rules=rules).is_protec)


if __name__ == "__main__":
    unittest.main()


from tests.test_web_selection import SelectionWebTestCase, insert_board  # noqa: E402


class ForceButtonWebTests(SelectionWebTestCase):
    """画面の「プロテック 強制」(`/api/selection/protec/force`)。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        insert_board(self.conn, width=900, length=1800, board_type="ハードボード")
        insert_board(self.conn, width=900, length=1800, board_type="プロテックボード")
        insert_lot(self.conn)                                       # ｵｰﾀﾞｰ厚 3.2
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100", 包装仕様NO="1P0001")
        self.client.get("/api/lot/1234567", headers=self.auth())

    def press(self) -> dict:
        return self.post("/api/selection/protec/force", {})

    def test_押すとプロテック_もう一度で戻る(self) -> None:
        self.assertFalse(self.get()["protec_forced"])
        session = self.session()
        session.selected.lower.append(object())                     # 選定済みのボードがある
        state = self.press()
        self.assertTrue(state["protec_forced"])
        self.assertIn("(強制)", state["banner"]["text"])
        self.assertEqual(session.board_type, "プロテックボード")
        self.assertTrue(session.presenter.selection_flags()["is_protec_mode"])
        self.assertEqual(session.selected.lower, [])               # 前の前提のボードは外す
        self.assertIn("選定し直してください", state["message"])
        state = self.press()
        self.assertFalse(state["protec_forced"])
        self.assertFalse(session.presenter.protec.is_protec)
        self.assertEqual(session.board_type, "ハードボード")

    def test_ロットを開き直すと解除(self) -> None:
        self.press()
        self.client.get("/api/lot/1234567", headers=self.auth())
        self.assertFalse(self.get()["protec_forced"])
        self.assertFalse(self.session().presenter.protec.is_protec)

    def test_やり直すで解除(self) -> None:
        self.press()
        self.post("/api/selection/clear")
        self.assertFalse(self.get()["protec_forced"])
        self.assertFalse(self.session().presenter.protec.is_protec)

    def test_画面にボタンがある(self) -> None:
        page = self.client.get("/selection", headers=self.auth()).get_data(as_text=True)
        self.assertIn('id="forceProtec"', page)
        self.assertIn("プロテック 強制", page)


class LotRuleWebTests(SelectionWebTestCase):
    """ロットを開くと、SIKALOT のオーダー寸法と条件の表で決まる。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))

    def open_lot(self, spec: str, t: float, w: float, l: float) -> None:
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        for table in ("仕掛ロット", "仕掛引当", "仕掛受注"):
            self.conn.execute(f'DELETE FROM "{table}"')
        insert_lot(self.conn, **{"オーダー板厚": t, "オーダー板幅": w, "オーダー板丈": l})
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100", 包装仕様NO=spec)
        self.client.get("/api/lot/1234567", headers=self.auth())

    def test_1P1255と1P1125(self) -> None:
        cases = [("1P1255", 12.0, 1073.0, 2265.0, True), ("1P1255", 12.0, 1073.0, 2335.0, False),
                 ("1P1125", 3.0, 1000.0, 2000.0, True), ("1P1125", 2.0, 1000.0, 2000.0, False)]
        for spec, t, w, l, want in cases:
            with self.subTest(spec=spec, t=t, w=w, l=l):
                self.open_lot(spec, t, w, l)
                self.assertEqual(self.session().presenter.protec.is_protec, want)
