"""移行の総点検で見つかった欠陥が、二度と入らないこと

【この試験が別ファイルになっている理由】
どれも Phase 6b〜7 で入れてしまった欠陥で、**全件OKのまま通っていた**
ものです。既にある試験は「作った機能が動くこと」を見ていて、
「tkinter版と同じか」「押せない場面で押せてしまわないか」までは
見ていませんでした。同じ見落としを繰り返さないよう、**どの欠陥に
対する備えなのか**が1か所で読める形にしてあります。

見つけ方は、tkinter版の対応する処理と1つずつ突き合わせる棚卸しです。
`compare_ui.py` は選定と配置しか見ないので、ここは捕まりませんでした。

【共通の筋】
7件のうち5件は**同じ事実を2か所に持ったこと**が原因です
(work_context とセッション / サーバと画面 / 入力欄と確定値)。
設計書 §3 の「判断を2か所に持たない」は、守らないとこうなる、
という実例として残しておきます。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402

_isolation.ensure_isolated()

from packaging_tool import (  # noqa: E402
    layout_session,
    selection_session,
    special_packaging as spk,
)
from packaging_tool.presenters import outputs  # noqa: E402

try:
    import tests.test_web_layout as twl
    import tests.test_web_selection as tws
    from tests.test_web_selection import insert_board, insert_pallet
    HAS_WEB = True
except Exception:                                # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ"

_BASE = tws.SelectionWebTestCase if HAS_WEB else unittest.TestCase
_LAYOUT_BASE = twl.LayoutWebTestCase if HAS_WEB else unittest.TestCase


class LotFixture(_BASE):
    """ロットを1件引いた状態から始める土台。

    **`SendTests` を継承しない**のは、継承すると向こうの試験まで
    このファイルの名前で走ってしまい、落ちたときにどちらの備えが
    壊れたのか読めなくなるため。
    """

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from app.routes import warehouse as wh_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        for module in (lot_routes, wh_routes):
            original = module.get_db
            module.get_db = lambda: self.conn
            self.addCleanup(
                lambda m=module, o=original: setattr(m, "get_db", o))

        insert_pallet(self.conn, width=1100, length=2000, code="P9", unit="台")
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn)
        self.client.get("/api/lot/1234567", headers=self.auth())


# ==================================================================
# 帳票の経路に守りが効いていない
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class ReportTokenTests(LotFixture):
    """`/report/*` は `/api/*` と同じ守りの内側にあること。

    Phase 6d で帳票を `/report/...` に置いたため、`/api/` で始まるかどうか
    だけを見ていた `before_request` を素通りしていました。帳票には
    Lot番号・品名・板厚・寸法・梱包数が丸ごと入ります。

    画面側は `?t=` を付けて開いていたので、書いた側は守られているつもりで、
    試験もトークン付きで叩いていたため**付けても付けなくても同じ**という
    事実が見えませんでした。
    """

    def test_トークンが無ければ帳票は返らない(self) -> None:
        res = self.client.get("/report/label")
        self.assertEqual(res.status_code, 401)
        self.assertNotIn(b"1234567", res.get_data())

    def test_間違ったトークンでも返らない(self) -> None:
        self.assertEqual(self.client.get("/report/label?t=でたらめ").status_code, 401)

    def test_正しいトークンなら返る(self) -> None:
        """守りを足したせいで**使えなくなっていない**ことも見る。"""
        res = self.client.get("/report/label", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertIn("1234567", res.get_data(as_text=True))

    def test_画面のHTMLは素通しのまま(self) -> None:
        """アドレス欄から開く経路。中身は空の器で業務データは持たない。"""
        self.assertEqual(self.client.get("/selection").status_code, 200)


# ==================================================================
# 資材選択の状態
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class SelectionStateTests(LotFixture):

    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1100, length=2000, symbol="A1",
                      code="P-1100", w_min=900, l_min=1700)
        insert_board(self.conn, width=1100, length=2000)
        insert_board(self.conn, width=550, length=1000)

    def session(self):
        return selection_session.get_session(self.conn)

    # --- パレット検索が行を覚えない -------------------------------
    def test_パレット検索は決まった行も覚える(self) -> None:
        """覚えないと、画面上は行が光っているのに倉庫送信が断られる。

        tkinter版 `do_auto_select_pallet` は決定したパレットを一覧の中で
        選択状態にし、以降の倉庫送信・Lot印刷はその行から業界・記号・
        発注コード・単位を採ります。発注コードは**行にしか無い**ので、
        覚えないと Lot印刷はその欄が空欄で刷られます。
        """
        res = self.post("/api/selection/pallet/search",
                        {"product_width": "1000", "product_length": "1800"})
        self.assertTrue(res["pallet_set"])
        self.assertIsNotNone(self.session().pallet_row)
        self.assertTrue(res["outputs"]["can_send"], res["outputs"]["send_why"])
        self.assertEqual(outputs._pallet_info(self.session()),
                         ("一般", "A1", "P-1100", "台"))

    def test_選んでいる行はビューモデルに出る(self) -> None:
        """画面が独自に覚えると、サーバが外したことが伝わらない。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/pallet/pick",
                  {"width": 1100, "length": 2000, "symbol": "A1"})
        rows = self.get("/api/selection/state")["rows"]
        self.assertEqual([r["picked"] for r in rows].count(True), 1)

        # 別の寸法を適用すると、サーバは行を外す。画面にもそう見えること
        self.post("/api/selection/pallet/apply",
                  {"width": "900", "length": "900"})
        rows = self.get("/api/selection/state")["rows"]
        self.assertNotIn(True, [r["picked"] for r in rows])

    # --- 同じ寸法で押し直すと製品サイズの確定が飛ぶ ----------------
    def test_同じパレット寸法で押し直しても製品サイズは残る(self) -> None:
        """パレットが**変わって**いなければ、収まる確認は生きている。

        飛ばしていたころは、パレット検索を押し直すだけでボード選定が
        押せなくなっていました(tkinter版 `do_apply_pallet` は製品サイズに
        一切触れません)。
        """
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        again = self.post("/api/selection/pallet/apply",
                          {"width": "1100", "length": "2000"})
        self.assertTrue(again["product_set"])

    def test_パレット寸法が変われば製品サイズの確定は外れる(self) -> None:
        """こちらは**外れるのが正しい**。収まる確認が無効になるため。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        changed = self.post("/api/selection/pallet/apply",
                            {"width": "900", "length": "900"})
        self.assertFalse(changed["product_set"])

    # --- ロット切替で前の作業が残る --------------------------------
    def test_ロットを切り替えると資材選択の作業を捨てる(self) -> None:
        """残ると、前のロットの寸法・発注コードで新しいロットを送れる。

        `work_context` は自分の分を捨てるのに、セッションが捨てないため、
        リボンは「未設定」・カードは「設定済」という食い違いが同じ画面に
        並んでいました(VBA `ClearForNewLot`)。
        """
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        insert_lot(self.conn, lot_no="7654321", 製造板幅=800.0, 製造板丈=1500.0)
        insert_hiki(self.conn, lot_no="7654321", order_no="ORD2", no="60717002")
        insert_odr(self.conn, order_no="ORD2")

        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        self.post("/api/selection/boards/auto-select")
        self.assertTrue(self.session().palette.is_set)

        self.client.get("/api/lot/7654321", headers=self.auth())
        state = self.get("/api/selection/state")
        self.assertFalse(state["pallet_set"])
        self.assertFalse(state["product_set"])
        self.assertEqual(state["ribbon"]["pallet"], "未設定")
        self.assertEqual(self.session().selected.upper, [])
        self.assertEqual(self.session().selected.lower, [])
        self.assertIsNone(self.session().pallet_row)

    def test_同じロットを引き直しても作業は消えない(self) -> None:
        """消すのは**別のロットに移ったとき**だけ。

        引き直しのたびに消えると、ロット番号を確かめただけで
        やり直しになる。
        """
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.client.get("/api/lot/1234567", headers=self.auth())
        self.assertTrue(self.get("/api/selection/state")["pallet_set"])

    # --- 1行外すと選定結果ごと捨てていた --------------------------
    def test_1行外しても選定結果は残る(self) -> None:
        """捨てると切断依頼が「カットは不要」に変わり、配置の前提も変わる。

        `select_result` は (a) 切断依頼のカット記録と (b) 配置の狭幅
        パレット判定の**唯一の出どころ**です。tkinter版 `_remove_selected`
        も削除では持ち越し、手動追加 `do_add_board` のときだけ捨てます
        ── 追加と削除は意図的に非対称です。
        """
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        self.post("/api/selection/boards/auto-select")
        self.assertIsNotNone(self.session().select_result)

        category = "upper" if self.session().selected.upper else "lower"
        self.post("/api/selection/boards/remove", {"category": category, "index": 0})
        self.assertIsNotNone(self.session().select_result,
                             "1行外しただけで選定結果が消えました")

    def test_手で足したら選定結果は捨てる(self) -> None:
        """こちらは**捨てるのが正しい**。自動選定時の前提が崩れるため。"""
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        self.post("/api/selection/boards/auto-select")
        self.post("/api/selection/boards/add",
                  {"category": "lower", "width": 550, "length": 1000, "count": 1})
        self.assertIsNone(self.session().select_result)

    # --- EXオンリーが自動でONにならない ----------------------------
    def test_EX受注ならEXオンリーが自動でONになる(self) -> None:
        """一覧の中身がtkinter版と変わってしまう。

        VBA は EX受注のとき `chkExOnly` を表示して**自動でONにし**、
        非EXでは非表示+OFF にしていました(tkinter版 `_render_ex_order`)。
        """
        self.conn.execute("UPDATE 仕掛受注 SET EX_輸出区分='EX'")
        insert_pallet(self.conn, width=1200, length=1200, symbol="EX1", code="E-1")
        self.conn.commit()

        self.client.get("/api/lot/1234567", headers=self.auth())
        state = self.get("/api/selection/state")
        self.assertTrue(state["ex_only"])
        self.assertTrue(state["ex_only_enabled"])
        self.assertEqual([r["symbol"] for r in state["rows"]], ["EX1"])

    def test_非EX受注ならEXオンリーは自動でOFFになる(self) -> None:
        """前のロットのONが残ると、一覧が空に見える。"""
        self.session().ex_only = True
        self.client.get("/api/lot/1234567", headers=self.auth())
        self.assertFalse(self.get("/api/selection/state")["ex_only"])


# ==================================================================
# 1P0113(裸梱包)の行き止まり
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class Expand1P0113Tests(LotFixture):
    """資材展開が角材・松板を引き直すこと。

    1P0113 中はパレットカードごと隠れていて「セット」が押せないので、
    展開が引き直さないと、製品サイズを資材へ反映させる操作が画面上に
    1つも無くなります ── 「製品ｻｲｽﾞ未設定」のまま倉庫送信まで
    断られ続ける行き止まりでした(tkinter版 `expand_materials` は
    `if self.mode_1p0113: self.load_1p0113_materials()` を必ず呼ぶ)。
    """

    def setUp(self) -> None:
        super().setUp()
        for name, atsu, haba, code in (
            (spk.KAKUZAI_NAME, spk.KAKUZAI_ATSU, spk.KAKUZAI_HABA, "K001"),
            (spk.MATSUITA_NAME, spk.MATSUITA_ATSU, spk.MATSUITA_HABA, "M001"),
        ):
            self.conn.execute(
                "INSERT INTO 松板角材 (品名, 厚, 幅, 丈min, 丈max, コード, 単位, 備考)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (name, atsu, haba, 1, 9999, code, "本", ""))
        self.conn.execute("UPDATE 仕掛受注 SET 包装仕様NO='1P0113'")
        self.conn.commit()
        self.client.get("/api/lot/1234567", headers=self.auth())

    def test_資材展開で角材と松板が引ける(self) -> None:
        before = self.get("/api/selection/state")["p1"]
        self.assertFalse(before["ready"], "仕立てが違います(最初から引けている)")

        res = self.client.post("/api/lot/expand", json={}, headers=self.auth())
        self.assertEqual(res.status_code, 200)

        after = self.get("/api/selection/state")["p1"]
        self.assertTrue(after["ready"], after["why"])
        self.assertEqual(after["why"], "")
        # 行き止まりが解消したことの本体 ── 展開しただけで送れる
        self.assertTrue(self.get("/api/selection/state")["outputs"]["can_send"],
                        self.get("/api/selection/state")["outputs"]["send_why"])


# ==================================================================
# 棚検索 ── 400 なのに状態が動く
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class LayoutSearchTests(_LAYOUT_BASE):
    """400 は「入力の形が違う」であって、状態は動いていないこと。

    動かしてから断っていたころは、サーバは検索を取り消し、ブラウザは
    前の結果を出したまま、という食い違いが残りました。400 の応答には
    画面ぜんぶを載せないので、画面には直しようがありません。
    """

    def search(self, body, expect=None):
        """検索を1回投げる。**結果は問わない**(置き場が未登録でもよい)。"""
        res = self.client.post("/api/layout/search", json=body,
                               headers=self.auth())
        if expect is not None:
            self.assertEqual(res.status_code, expect, res.get_json())
        return res

    def test_サイズが数字でなければ状態は動かない(self) -> None:
        self.search({"kind": "board", "width": "1000", "length": "1200"})
        s = layout_session.get_session()
        before = (s.kind, s.width_text, s.length_text, s.result,
                  sorted(s.highlight))

        self.search({"kind": "board", "width": "あ", "length": "1200"},
                    expect=400)
        after = (s.kind, s.width_text, s.length_text, s.result,
                 sorted(s.highlight))
        self.assertEqual(before, after)

    def test_知らない種別でも状態は動かない(self) -> None:
        self.search({"kind": "board", "width": "1000", "length": "1200"})
        s = layout_session.get_session()
        before = (s.kind, s.width_text, s.length_text)
        self.search({"kind": "なにか", "length": "1200"}, expect=400)
        self.assertEqual(before, (s.kind, s.width_text, s.length_text))


# ==================================================================
# アングル図が無効化されない
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class AngleInvalidateTests(LotFixture):
    """「アングル配置」を押すまで図を描かない、を**押したあとも**守る。

    `angle_drawn` は描いたときに True になったきり戻らなかったので、
    追加・削除・自動選定のあとも図が作り直され、押していない組み合わせ
    がそのまま図になっていました(tkinter版はキャンバスを描き直さない
    ので、押すまで前の図のままです)。
    """

    def setUp(self) -> None:
        super().setUp()
        for length in (1800, 1200):
            self.conn.execute(
                "INSERT INTO CornerboardMaster (アングル丈) VALUES (?)", (length,))
        self.conn.commit()
        self.post("/api/selection/pallet/apply",
                  {"width": "1100", "length": "2000"})
        self.post("/api/selection/product/apply",
                  {"width": "1000", "length": "1800"})
        self.session = selection_session.get_session(self.conn)

    def draw(self) -> None:
        self.post("/api/selection/angles/draw")
        self.assertTrue(self.session.angle_drawn)

    def test_追加すると図は無効になる(self) -> None:
        self.post("/api/selection/angles/add", {"length": 1800})
        self.draw()
        res = self.post("/api/selection/angles/add", {"length": 1200})
        self.assertFalse(self.session.angle_drawn)
        self.assertIsNone(res["plans"]["angle"])
        self.assertTrue(res["angles"]["can_draw"])

    def test_外すと図は無効になる(self) -> None:
        self.post("/api/selection/angles/add", {"length": 1800})
        self.post("/api/selection/angles/add", {"length": 1200})
        self.draw()
        res = self.post("/api/selection/angles/remove", {"index": 0})
        self.assertFalse(self.session.angle_drawn)
        self.assertIsNone(res["plans"]["angle"])

    def test_自動選定でも図は無効になる(self) -> None:
        self.post("/api/selection/angles/add", {"length": 1800})
        self.draw()
        self.post("/api/selection/angles/auto")
        self.assertFalse(self.session.angle_drawn)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
