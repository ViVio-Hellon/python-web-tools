"""倉庫連携(Web版)のテスト

`packaging_tool/presenters/warehouse.py` と `app/routes/warehouse.py` を見る。

【この画面でいちばん大事なこと】
**現場から「確認済みにする」ができてはいけない。**

資材が実際に受け取ったかどうかに関わらず現場の都合で確認済みにできると、
確認という工程そのものが意味を失う。VBA版は現場用と資材用が別フォーム
だった。Web版は同じ保証を2段で作る。

1. `mode:material` の権限が無い端末には**登録しない**(404)
2. 権限があっても、いま現場モードで見ていれば**断る**(403)

隠すのではなく、存在しない。だからこの試験は「ボタンが無い」ではなく
**状態コード**を確かめる。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from unittest import mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import access_control, db, modes  # noqa: E402
from packaging_tool import warehouse_service as svc  # noqa: E402
from packaging_tool.presenters import warehouse as presenter  # noqa: E402
from tests import _web  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"
TOKEN = _web.TOKEN          # 土台と同じものを使う

ORDER = {
    "lot_no": "4102781", "hinmei": "一般　P1　1100x1300",
    "hatchu_code": "PL-001", "tani": "台", "zaisitu": "A5052",
    "choshitu": "H32", "atu": "3.0", "haba": "1100", "take": "1300",
    "yoto_code": "K100", "nounyusaki": "A社", "hatchu_suu": "4",
}


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


# ==================================================================
# 表示の組み立て
# ==================================================================
class PresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def add(self, **kw) -> int:
        values = {**ORDER, **kw}
        return svc.create_order(self.conn, **values).mgr_no

    def test_列の並びはVBAのまま(self) -> None:
        self.assertEqual([label for label, _, _ in presenter.ORDER_COLUMNS][:5],
                         ["管理番号", "登録日時", "LotNo", "品名", "発注コード"])

    # ---- 主従に畳んだ見せ方 ------------------------------------------
    def test_畳んでも事実は1つも減らない(self) -> None:
        """15列を8列に畳んでいるが、**出ない値があってはならない**。"""
        shown: list[str] = []
        for column in presenter.ORDER_VIEW:
            shown.append(column.primary)
            shown.extend(column.sub)
        self.assertEqual(sorted(shown),
                         sorted(label for label, _, _ in presenter.ORDER_COLUMNS))

    def test_同じ値を二か所に出さない(self) -> None:
        """同じ列を主にも従にも置くと、片方だけ古い、が起こりうる。"""
        shown: list[str] = []
        for column in presenter.ORDER_VIEW:
            shown.append(column.primary)
            shown.extend(column.sub)
        self.assertEqual(len(shown), len(set(shown)))

    def test_関係の無いものを畳まない(self) -> None:
        """現場の声:「発注コード / 用途は違和感。用途は製品の用途で

        あってコードとは何ら関係がない」。上下に組むと「このコードの
        用途」と読めてしまう。畳んでよいのは**同じものの言い換えか
        単位**だけ。
        """
        pairs = {c.primary: c.sub for c in presenter.ORDER_VIEW}
        self.assertEqual(pairs["発注コード"], (), "コードに用途を添えない")
        self.assertEqual(pairs["用途コード"], (), "用途は単独の列")
        # 同じものの言い換え・単位は畳んだままでよい(現場の確認済み)
        self.assertEqual(pairs["LotNo"], ("品名",))
        self.assertEqual(pairs["発注数"], ("単位",))
        self.assertEqual(pairs["材質"], ("調質",))
        self.assertEqual(pairs["厚"], ("幅", "丈"))

    def test_幅の合計は操作の列を残す(self) -> None:
        """ボタンは畳めない。**先に確保してから**残りを読み物に配る。"""
        total = sum(int(c.width.rstrip("%")) for c in presenter.ORDER_VIEW)
        action = int(presenter.ORDER_ACTION_WIDTH.rstrip("%"))
        self.assertEqual(total + action, 100)

    def test_状態の列だけが別扱い(self) -> None:
        """ピルを描くのは1列だけ。画面は `kind` を見て決める。"""
        kinds = [c.kind for c in presenter.ORDER_VIEW]
        self.assertEqual(kinds.count("status"), 1)
        status = next(c for c in presenter.ORDER_VIEW if c.kind == "status")
        self.assertEqual(status.primary, "状態")

    def test_見せ方はJSONにも出る(self) -> None:
        """画面は指図どおりに `values` から拾う。事実の置き場所は1つ。"""
        self.add()
        body = presenter.to_dict(presenter.build(self.conn, mode=modes.FIELD))
        first = body["columns"][0]
        self.assertEqual(first["primary"], "管理番号")
        self.assertEqual(first["sub"], ["登録日時"])
        # 事実は rows[].values にしかない
        self.assertIn("管理番号", body["rows"][0]["values"])
        self.assertIn("登録日時", body["rows"][0]["values"])

    def test_倉庫だけが確認できる(self) -> None:
        """判断はサーバが持つ。画面が条件を組み立て直さない。"""
        self.add()
        field = presenter.build(self.conn, mode=modes.FIELD)
        warehouse = presenter.build(self.conn, mode=modes.MATERIAL)
        self.assertFalse(field.rows[0].can_confirm)
        self.assertTrue(warehouse.rows[0].can_confirm)

    def test_取り消せるのは現場だけ(self) -> None:
        """**出した側が引っ込める操作。** 資材は受けて確認する側で、
        消す立場ではない。

        VBAでも取り消しは現場の送信確認ダイアログ
        (`frmSendConfirm.btnDelete_Click`)にあり、資材側の
        `frmWarehouseOrder` にあったのは確認だけだった。移植のときに
        確認とまとめて資材専用にしてしまい、**逆**になっていた
        (現場の声:「送った発注を取り消せない」)。
        """
        self.add()
        self.assertTrue(presenter.build(self.conn, mode=modes.FIELD).rows[0].can_cancel)
        self.assertFalse(presenter.build(self.conn, mode=modes.MATERIAL).rows[0].can_cancel)

    def test_確認は倉庫だけのまま(self) -> None:
        """**取り消しを開けても、確認は開けない。**

        現場から確認済みにできると、倉庫が実際に受け取ったかに関わらず
        現場の都合で確認済みにでき、確認という工程が意味を失う。
        """
        self.add()
        self.assertFalse(presenter.build(self.conn, mode=modes.FIELD).rows[0].can_confirm)
        self.assertTrue(presenter.build(self.conn, mode=modes.MATERIAL).rows[0].can_confirm)

    def test_倉庫が確認したら現場からも取り消せない(self) -> None:
        """現場に開けたのは「まだ誰も受けていない依頼の取り下げ」まで。

        受け取ったことを確認したものを消すと、現物と帳簿が合わなくなる。
        """
        mgr_no = self.add()
        svc.confirm_order(self.conn, mgr_no)
        self.assertFalse(presenter.build(self.conn, mode=modes.FIELD).rows[0].can_cancel)

    def test_確認済みは確認も取消もできない(self) -> None:
        mgr_no = self.add()
        svc.confirm_order(self.conn, mgr_no)
        row = presenter.build(self.conn, mode=modes.MATERIAL).rows[0]
        self.assertFalse(row.can_confirm)
        self.assertFalse(row.can_cancel)

    def test_未確認の件数を数える(self) -> None:
        """倉庫のレールに出す。行く前に「やることがある」と分かる。"""
        self.add()
        self.add()
        svc.confirm_order(self.conn, 1)
        self.assertEqual(presenter.build(self.conn, mode=modes.MATERIAL).pending, 1)

    def test_倉庫には取消済を既定で出さない(self) -> None:
        """倉庫が処理する対象ではない。"""
        mgr_no = self.add()
        svc.cancel_order(self.conn, mgr_no)
        self.assertEqual(presenter.build(self.conn, mode=modes.MATERIAL).found, 0)

    def test_現場には取消済も出す(self) -> None:
        """自分が送った履歴なので、取り消したことも見えたほうがよい。"""
        mgr_no = self.add()
        svc.cancel_order(self.conn, mgr_no)
        view = presenter.build(self.conn, mode=modes.FIELD)
        self.assertEqual(view.found, 1)
        self.assertEqual(view.rows[0].status, svc.STATUS_CANCELLED)

    def test_状態は色だけで伝えない(self) -> None:
        """文言も持つ(§3.8)。"""
        for status in (svc.STATUS_PENDING, svc.STATUS_CONFIRMED,
                       svc.STATUS_CANCELLED):
            with self.subTest(status=status):
                self.assertIn(status, presenter.STATUS_KIND)

    def test_絞り込みで引ける(self) -> None:
        self.add(lot_no="4102781")
        self.add(lot_no="9999999")
        view = presenter.build(self.conn, mode=modes.FIELD, keyword="4102781")
        self.assertEqual(view.found, 1)

    def test_知らない期間指定は全部に倒す(self) -> None:
        """勝手に絞ると、在るものを無いと誤解させる。"""
        self.add()
        self.assertEqual(
            presenter.build(self.conn, mode=modes.FIELD, date_filter="なにか").found, 1)

    def test_辞書にできる(self) -> None:
        import json
        self.add()
        json.dumps(presenter.to_dict(presenter.build(self.conn, mode=modes.FIELD)),
                   ensure_ascii=False)


class ValidateTests(unittest.TestCase):
    def test_必須が欠けたら欄を名指しで返す(self) -> None:
        """どこを直せばよいかが分からないと、打ち直しになる。"""
        for key in presenter.REQUIRED_KEYS:
            with self.subTest(key=key):
                body = {**ORDER, key: ""}
                _, problem = presenter.validate(body)
                self.assertIsNotNone(problem)
                self.assertEqual(problem[0], key)

    def test_数値でなければ弾く(self) -> None:
        _, problem = presenter.validate({**ORDER, "haba": "ひろい"})
        self.assertEqual(problem[0], "haba")

    def test_数値の空欄は0にする(self) -> None:
        """VBA も空欄を許して 0 として扱っていた。"""
        values, problem = presenter.validate({**ORDER, "atu": ""})
        self.assertIsNone(problem)
        self.assertEqual(values["atu"], "0")

    def test_前後の空白は落とす(self) -> None:
        values, _ = presenter.validate({**ORDER, "lot_no": "  4102781  "})
        self.assertEqual(values["lot_no"], "4102781")


class ExOrderTests(unittest.TestCase):
    """EX受注は発注コード・単位・発注数を**空のまま**通す。

    EXの実データは別の職場から届き、そちらが優先される。こちらから
    中身のある値を送ると突き合わせで混乱するので、意図的に空で送る。
    """

    def ex(self, **over) -> dict:
        blank = {key: "" for key in presenter.EX_BLANK_KEYS}
        return {**ORDER, **blank, "hinmei": "EX", "is_ex_order": True, **over}

    def test_空でも通す(self) -> None:
        values, problem = presenter.validate(self.ex())
        self.assertIsNone(problem)
        for key in presenter.EX_BLANK_KEYS:
            self.assertEqual(values[key], "", key)

    def test_発注数を0で埋めない(self) -> None:
        """0 を入れると「0個の発注」を送ったことになる。空は空のまま。"""
        values, _ = presenter.validate(self.ex())
        self.assertEqual(values["hatchu_suu"], "")

    def test_旗が無ければこれまでどおり弾く(self) -> None:
        """**品名の文字から推し量らない。** 品名が EX でも旗が無ければ通常扱い。"""
        _, problem = presenter.validate(
            {**ORDER, "hatchu_code": "", "hinmei": "EX"})
        self.assertEqual(problem[0], "hatchu_code")

    def test_ロットと品名は空にできない(self) -> None:
        """どのロットのEXかが分からないと、届いた側で突き合わせられない。"""
        for key in ("lot_no", "hinmei"):
            with self.subTest(key=key):
                _, problem = presenter.validate(self.ex(**{key: ""}))
                self.assertEqual(problem[0], key)

    def test_登録まで通る(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        self.addCleanup(conn.close)

        values, problem = presenter.validate(self.ex())
        self.assertIsNone(problem)
        result = svc.create_order(conn, **values)
        self.assertTrue(result.ok, result.message)

        row = conn.execute(
            "SELECT 品名, 発注コード, 単位, 発注数 FROM 資材パレット注文管理"
        ).fetchone()
        self.assertEqual(row["品名"], "EX")
        self.assertEqual(row["発注コード"], "")
        self.assertEqual(row["単位"], "")
        self.assertIsNone(row["発注数"])      # 0 ではなく空

    def test_通常の発注は厳しいまま(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        self.addCleanup(conn.close)
        result = svc.create_order(conn, **{**ORDER, "hatchu_code": ""})
        self.assertFalse(result.ok)


# ==================================================================
# 画面とAPI
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class WarehouseWebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import warehouse as routes

        self.conn = _web.bind_db(self, routes, make_db())

        # 権限は明示して渡す。**マスタの中身で試験の結果が変わっては
        # いけない**ので、「何を持っているとどうなるか」を固定する
        both = access_control.grant_of("mode:field", "mode:material")
        field_only = access_control.grant_of("mode:field")

        self.clients = {}
        for mode, port, grant in (
                # 権限が無い端末。確認のエンドポイント自体が無い
                ("field", 8713, field_only),
                # 両方持っている端末を資材モードで開いたもの
                ("material", 8723, both),
                # 両方持っているが、いまは現場モードで見ている
                ("field-allowed", 8733, both)):
            self.clients[mode] = _web.make_client(
                modes.FIELD if mode.startswith("field") else mode,
                port=port, grant=grant)

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}

    def send(self, mode: str = "field", **kw):
        return self.clients[mode].post("/api/warehouse/send",
                                       json={**ORDER, **kw}, headers=self.auth())


class ExOrderPageTests(WarehouseWebTestCase):
    """資材選択から届いたEXの下書きが、画面まで旗を保ったまま渡ること。"""

    def test_下書きに旗と説明が乗る(self) -> None:
        from packaging_tool import work_context

        work_context.get_context().set_pending_orders([{
            "lot_no": "4102781", "hinmei": "EX", "hatchu_code": "",
            "tani": "", "hatchu_suu": "", "zaisitu": "A5052",
            "choshitu": "H112", "atu": "6.75", "haba": "1122",
            "take": "2502", "yoto_code": "K434", "nounyusaki": "倉庫A",
            "is_ex_order": True,
        }])
        self.addCleanup(work_context.get_context().take_pending_orders)

        html = self.clients["field"].get(
            "/warehouse", headers=self.auth()).get_data(as_text=True)
        # 旗が下書きに乗っている(画面がこれを見て欄に錠を掛ける)
        self.assertIn("is_ex_order", html)
        # 押せない理由を書いておく欄
        self.assertIn('id="exNote"', html)
        self.assertIn("EXの実データは別の職場から届き", html)

    def test_空にしてよい欄はサーバが決める(self) -> None:
        """画面側で持つと、どちらかが古くなる。"""
        body = self.clients["field"].get(
            "/api/warehouse/orders", headers=self.auth()).get_json()
        self.assertEqual(body["ex_blank_keys"], list(presenter.EX_BLANK_KEYS))

    def test_EXの発注をAPIから登録できる(self) -> None:
        res = self.send(hinmei="EX", hatchu_code="", tani="",
                        hatchu_suu="", is_ex_order=True)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))


class LotBackTrackTests(WarehouseWebTestCase):
    """**発注からLotへ辿れる。** 現場の逆順。

    現場は ロット情報 → 資材選択 → 送信 の順に進む。受け取る側からは
    その逆ができないと、何のための発注なのかを確かめる手立てが、番号を
    目で読んで別の端末で引き直すしかない(現場の指摘:「倉庫モードの
    時に送られてきたデータに添付しているLOT情報を現場モードのように
    展開できる必要があります」)。
    """

    def test_行にLotを開く行き先が付く(self) -> None:
        self.send()
        for mode in ("field", "material"):
            with self.subTest(mode=mode):
                body = self.clients[mode].get("/api/warehouse/orders",
                                              headers=self.auth()).get_json()
                row = body["rows"][0]
                self.assertEqual(row["lot_no"], ORDER["lot_no"])
                # **番号を打ち直させない。** 7桁の打ち間違いは、そのまま
                # 別のロットを開いてしまい、開けるので気づけない
                self.assertEqual(row["lot_url"], f"/lot?lot={ORDER['lot_no']}")

    def test_Lotが入っていない発注は辿り先を出さない(self) -> None:
        """空のリンクを出すと、押しても何も起きない画面になる。"""
        row = presenter._row({"LotNo": "", "状態": "未確認"}, is_material=True)
        self.assertEqual(row.lot_url, "")

    def test_資材でもロット検索と資材選択を開ける(self) -> None:
        """辿り先が開けなければ、辿れるようにした意味が無い。"""
        for path in ("/lot", "/selection"):
            with self.subTest(path=path):
                res = self.clients["material"].get(path, headers=self.auth())
                self.assertEqual(res.status_code, 200)

    def test_資材から発注は出せないまま(self) -> None:
        """**広げたのは辿る道だけ。** 出すのは現場だけという決まりは
        そのまま(`warehouse.field_only`)。
        """
        res = self.clients["material"].post(
            "/api/warehouse/send", json={"orders": []}, headers=self.auth())
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "wrong_mode")


class RoleSeparationTests(WarehouseWebTestCase):
    """この画面の要。**現場から確認済みにできない**。"""

    def test_現場に確認のURLは存在しない(self) -> None:
        """隠すのではなく、無い。押せないボタンではなく 404。"""
        mgr_no = self.send().get_json()["mgr_no"]
        res = self.clients["field"].post(
            "/api/warehouse/confirm", json={"mgr_no": mgr_no}, headers=self.auth())
        self.assertEqual(res.status_code, 404)

    def test_資材では確認できる(self) -> None:
        mgr_no = self.send().get_json()["mgr_no"]
        res = self.clients["material"].post(
            "/api/warehouse/confirm", json={"mgr_no": mgr_no}, headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["ok"])

    def test_現場の画面に確認ボタンを出さない(self) -> None:
        """URLが無いだけでなく、画面にも出さない。守りは1枚ではない。"""
        self.send()
        body = self.clients["field"].get("/api/warehouse/orders",
                                         headers=self.auth()).get_json()
        self.assertFalse(any(row["can_confirm"] for row in body["rows"]))

    def test_確認と取消のURLは常に登録されている(self) -> None:
        """以前は現場モードの端末ではURLごと登録しなかったが、いまは

        **常に登録**し、`material_only.before_request` が権限を要求のたびに
        確かめる(`app/routes/warehouse.py` の説明を参照)。これにより、
        マスタ管理でアクセス権限を足したあと、サーバプロセスを終了して
        起動し直さなくても、その場でモードを切り替えて使えるようになる。
        現場モードの端末で実際に呼べないこと(404)は他のテストが確かめる。
        """
        field = {r.rule for r in self.clients["field"].application.url_map.iter_rules()}
        material = {r.rule for r in
                    self.clients["material"].application.url_map.iter_rules()}
        for path in ("/api/warehouse/confirm", "/api/warehouse/cancel"):
            with self.subTest(path=path):
                self.assertIn(path, field)
                self.assertIn(path, material)

    def test_現場からも取り消せる(self) -> None:
        """**確認とは扱いが違う。** 出した本人が引っ込められる操作なので、
        現場モードのサーバにも登録する(VBA `frmSendConfirm` の取り消し)。
        """
        mgr_no = self.send().get_json()["mgr_no"]
        res = self.clients["field"].post(
            "/api/warehouse/cancel", json={"mgr_no": mgr_no}, headers=self.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(svc.list_orders(self.conn, include_cancelled=True)[0]["状態"],
                         svc.STATUS_CANCELLED)

    def test_現場から確認済みのものは取り消せない(self) -> None:
        """止めるのは状態。モードではない。"""
        mgr_no = self.send().get_json()["mgr_no"]
        svc.confirm_order(self.conn, mgr_no)
        res = self.clients["field"].post(
            "/api/warehouse/cancel", json={"mgr_no": mgr_no}, headers=self.auth())
        self.assertEqual(res.status_code, 409, res.get_json())

    def test_発注を出せるのは現場だけ(self) -> None:
        """**出すのは現場の仕事。** 資材は受けて確認する側なので出さない。

        資材課の人でも、現場モードに切り替えれば出せる(断るのは
        「いまどちらのモードで見ているか」だけ)。
        """
        self.assertTrue(self.send("field").get_json()["ok"])
        res = self.send("material")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "wrong_mode")

    def test_取り消せるのは現場だけ(self) -> None:
        """**取り消すのは出した側の操作。** 資材は確認する側で、
        受けたものを消す立場ではない。
        """
        mgr_no = self.send().get_json()["mgr_no"]
        res = self.clients["material"].post(
            "/api/warehouse/cancel", json={"mgr_no": mgr_no}, headers=self.auth())
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "wrong_mode")

        res = self.clients["field"].post(
            "/api/warehouse/cancel", json={"mgr_no": mgr_no}, headers=self.auth())
        self.assertEqual(res.status_code, 200, res.get_json())

    def test_権限があっても現場モードでは断る(self) -> None:
        """守りは2段。**登録の可否(誰か)と、いまのモード(誤操作)**は
        目的が違うので両方要る。

        資材課の人が現場モードで作業している最中に、手が滑って
        確認済みにできてしまうのを防ぐ。

        **かかるのは確認だけ。** 取り消しは現場の操作なので、現場モードで
        見ていても断らない(断ると、送った本人が引っ込められなくなる)。
        """
        mgr_no = self.send().get_json()["mgr_no"]
        res = self.clients["field-allowed"].post(
            "/api/warehouse/confirm", json={"mgr_no": mgr_no}, headers=self.auth())
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "wrong_mode")

        res = self.clients["field-allowed"].post(
            "/api/warehouse/cancel", json={"mgr_no": mgr_no}, headers=self.auth())
        self.assertEqual(res.status_code, 200, res.get_json())

    def test_権限が無い端末とある端末で404と403を使い分ける(self) -> None:
        """**404 は「無い」、403 は「今はできない」。** 混ぜると、
        権限を足せば直るのか、モードを切り替えれば直るのかが分からない。
        """
        mgr_no = self.send().get_json()["mgr_no"]
        body = {"mgr_no": mgr_no}
        self.assertEqual(self.clients["field"].post(
            "/api/warehouse/confirm", json=body, headers=self.auth()).status_code, 404)
        self.assertEqual(self.clients["field-allowed"].post(
            "/api/warehouse/confirm", json=body, headers=self.auth()).status_code, 403)


class PageTests(WarehouseWebTestCase):
    def test_現場は発注フォームがある(self) -> None:
        html = self.clients["field"].get("/warehouse").get_data(as_text=True)
        self.assertIn("新規発注", html)
        self.assertIn("倉庫へ送信", html)

    def test_資材に発注フォームは無い(self) -> None:
        """資材は受け取って確認するだけ。出す側の道具を置かない。"""
        html = self.clients["material"].get("/warehouse").get_data(as_text=True)
        self.assertNotIn("新規発注", html)
        self.assertNotIn("倉庫へ送信", html)

    def test_見出しがモードで変わる(self) -> None:
        self.assertIn("倉庫連携",
                      self.clients["field"].get("/warehouse").get_data(as_text=True))
        self.assertIn("発注一覧",
                      self.clients["material"].get("/warehouse").get_data(as_text=True))

    def test_必須の印を出す(self) -> None:
        html = self.clients["field"].get("/warehouse").get_data(as_text=True)
        self.assertIn('aria-label="必須"', html)

    def test_長い値の欄は横に広げる(self) -> None:
        """現場の声:「品名が見切れて見えない」。

        品名(「5×10 1550x3100」)と納入先(「ﾊｸﾄﾞｳ(ｶ)ｶﾅｶﾞﾜｼﾞｷﾞｮｳ」)は
        他の欄の倍近く長い。入力欄は打った内容を**確かめる**ための
        ものなので、後ろが切れて読めないのは入っていないのと同じ。
        """
        html = self.clients["field"].get("/warehouse").get_data(as_text=True)
        for key in ("hinmei", "nounyusaki"):
            with self.subTest(key=key):
                cell = html[html.rindex('<div class="f', 0, html.index(f'id="f-{key}"')):]
                self.assertTrue(cell.startswith('<div class="f f--wide"'), cell[:60])
        # 短い欄は広げない。全部広げたら1行1項目になって縦に伸びる
        cell = html[html.rindex('<div class="f', 0, html.index('id="f-tani"')):]
        self.assertTrue(cell.startswith('<div class="f"'), cell[:60])

    def test_広げる欄も見出しと組で置く(self) -> None:
        """見出しと入力が別のマスだと、広げた欄だけ行をまたいで離れる。"""
        html = self.clients["field"].get("/warehouse").get_data(as_text=True)
        cell = html[html.index('<div class="f f--wide"'):]
        cell = cell[:cell.index("</div>")]
        self.assertIn('data-for="hinmei"', cell)
        self.assertIn('id="f-hinmei"', cell)

    def test_絞り込みはクエリで効く(self) -> None:
        """現場の声:「送った発注の文字列絞り込みが機能していない」。

        サーバ側は効いていた ── 画面が**Enterを押すまで送っていなかった**
        のが原因(`warehouse.js` で打つそばから送るようにした)。
        送りさえすれば効くことを、ここで固定しておく。
        """
        self.send(lot_no="H3416J0", hinmei="5×10 1550x3100")
        self.send(lot_no="H9999Z9", hinmei="4×8 1250x2500")
        client = self.clients["field"]

        def hit(q: str) -> int:
            body = client.get(f"/api/warehouse/orders?q={q}",
                              headers=self.auth()).get_json()
            return len(body["rows"])

        self.assertEqual(hit(""), 2)
        self.assertEqual(hit("H3416"), 1, "LotNoで引ける")
        self.assertEqual(hit("5×10"), 1, "品名で引ける")
        self.assertEqual(hit("ない"), 0)

    def test_未確認の件数がレールに出る(self) -> None:
        """行く前に「やることがある」と分かる(情報の匂い)。"""
        self.send()
        html = self.clients["material"].get("/warehouse").get_data(as_text=True)
        start = html.index('<nav class="rail"')
        self.assertIn(">1<", html[start:html.index("</nav>", start)])


class SendApiTests(WarehouseWebTestCase):
    def test_トークンが要る(self) -> None:
        self.assertEqual(
            self.clients["field"].post("/api/warehouse/send", json=ORDER).status_code,
            401)

    def test_送れる(self) -> None:
        body = self.send().get_json()
        self.assertTrue(body["ok"])
        self.assertIsNotNone(body["mgr_no"])

    def test_登録したら取り込み元へも送りにいく(self) -> None:
        """設定画面が「登録のたびに自動でも送られる」と書いている分。

        tkinter版を撤去したとき呼び手が消え、**文言だけが残っていた**。
        繋がっていないと発注は手元に溜まったままになり、倉庫は
        誰かが「取り込み元へ反映」を押すまで気づけない。
        """
        with mock.patch("packaging_tool.data_sync.write_back_in_background") as sent:
            self.assertTrue(self.send().get_json()["ok"])
        sent.assert_called_once_with()

    def test_断られたときは送りにいかない(self) -> None:
        """手元に無いものを取り込み元へ送りにいっても意味がない。"""
        with mock.patch("packaging_tool.data_sync.write_back_in_background") as sent:
            self.assertEqual(self.send(hatchu_suu="0").status_code, 422)
        sent.assert_not_called()

    def test_必須漏れは400で欄を返す(self) -> None:
        res = self.send(hinmei="")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["field"], "hinmei")

    def test_数値でなければ400(self) -> None:
        self.assertEqual(self.send(haba="ひろい").status_code, 400)

    def test_発注数0は422(self) -> None:
        """打ち間違いではなく、業務として通らない値。"""
        res = self.send(hatchu_suu="0")
        self.assertEqual(res.status_code, 422)


class ActionApiTests(WarehouseWebTestCase):
    def test_二重確認は409(self) -> None:
        """他の端末が先に確認していることがある。取り直せば正しく見える。"""
        mgr_no = self.send().get_json()["mgr_no"]
        client = self.clients["material"]
        client.post("/api/warehouse/confirm", json={"mgr_no": mgr_no},
                    headers=self.auth())
        res = client.post("/api/warehouse/confirm", json={"mgr_no": mgr_no},
                          headers=self.auth())
        self.assertEqual(res.status_code, 409)

    def test_確認済みは現場からも取り消せない(self) -> None:
        """受け取ったことを確認したものを消すと、現物と帳簿が合わなくなる。

        取り消せるのは現場だけだが、**倉庫が確認したあとはその現場でも
        取り消せない**(止めるのは状態)。
        """
        mgr_no = self.send().get_json()["mgr_no"]
        self.clients["material"].post(
            "/api/warehouse/confirm", json={"mgr_no": mgr_no}, headers=self.auth())
        res = self.clients["field"].post(
            "/api/warehouse/cancel", json={"mgr_no": mgr_no}, headers=self.auth())
        self.assertEqual(res.status_code, 409)

    def test_対象が無ければ409(self) -> None:
        res = self.clients["material"].post(
            "/api/warehouse/confirm", json={"mgr_no": 9999}, headers=self.auth())
        self.assertEqual(res.status_code, 409)

    def test_管理番号が数値でなければ400(self) -> None:
        res = self.clients["material"].post(
            "/api/warehouse/confirm", json={"mgr_no": "あ"}, headers=self.auth())
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
