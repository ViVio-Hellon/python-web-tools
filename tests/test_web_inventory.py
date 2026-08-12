"""簡易在庫画面(Web版)のテスト

`packaging_tool/presenters/inventory.py` と `app/routes/inventory.py` を見る。

【ここで守りたいこと】
この画面から**在庫が動く**。読むだけの画面と違い、間違えると現物と
帳簿がずれる。そこで次の3つを固定する。

1. 図が使いものになるか(座標・状態・図に無い位置の扱い)
2. 失敗の種類が区別されるか(打ち間違い400 / 業務上の拒否422 / 競合409)
3. 競合したときに**黙って上書きしない**か
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from unittest import mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import access_control, config, db, pallet_service  # noqa: E402
from packaging_tool.presenters import inventory as presenter  # noqa: E402
from tests import _web  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"
TOKEN = _web.TOKEN          # 土台と同じものを使う


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def add_stock(conn, *, width=1100, length=1300, position="A1", qty=5,
              symbol="P1", industry="全面", listed=""):
    conn.execute(
        "INSERT INTO PalletMaster (幅, 丈, 巾適合min, 巾適合max, 丈適合min, 丈適合max,"
        " 業界, 記号, 位置, 在庫数, リスト管理, 桁数, 脚数, コード, 単位, 備考, 更新日時)"
        " VALUES (?, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?, 0, 0, '', '台', '', ?)",
        (width, length, industry, symbol, position, qty, listed,
         db.now_db_string()))
    conn.commit()


# ==================================================================
# 図
# ==================================================================
class MapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_図の座標をそのまま出す(self) -> None:
        """VBA は配置図の写真の上に、実際の並びどおりにボタンを置いていた。

        並びが崩れると「図を見た瞬間に分かる」というこの画面の値打ちが消える。
        """
        from packaging_tool import pallet_map
        plan = pallet_map.load()
        view = presenter.build_map(self.conn)
        self.assertEqual(len(view.positions), len(plan.positions))
        for shown, source in zip(view.positions, plan.positions):
            with self.subTest(name=source.name):
                self.assertEqual((shown.name, shown.x, shown.y, shown.w, shown.h),
                                 (source.name, source.x, source.y,
                                  source.w, source.h))

    def test_viewBoxは図の大きさ(self) -> None:
        view = presenter.build_map(self.conn)
        self.assertEqual(view.view_box, f"0 0 {view.width:g} {view.height:g}")

    def test_在庫のある棚と空の棚を分ける(self) -> None:
        """空まで同じ濃さで描くと、どこに物があるのか図から読めない。"""
        add_stock(self.conn, position="A1")
        states = {p.name: p.state for p in presenter.build_map(self.conn).positions}
        self.assertEqual(states["A1"], presenter.STATE_IDLE)
        self.assertEqual(states["A2"], presenter.STATE_EMPTY)

    def test_検索で当たった棚は青(self) -> None:
        """色の意味はVBA `clsMapBtn` のまま。現場の記憶をそのまま使う。"""
        add_stock(self.conn, position="A1")
        view = presenter.build_map(self.conn, hit={"A1"})
        self.assertEqual(_state(view, "A1"), presenter.STATE_HIT)

    def test_いま見ている棚が優先される(self) -> None:
        """検索で当たっていても、開いている棚は「開いている」と出す。"""
        add_stock(self.conn, position="A1")
        view = presenter.build_map(self.conn, hit={"A1"}, active="A1")
        self.assertEqual(_state(view, "A1"), presenter.STATE_ACTIVE)

    def test_棚ごとの件数を出す(self) -> None:
        add_stock(self.conn, position="A1", width=1100)
        add_stock(self.conn, position="A1", width=1200)
        counts = {p.name: p.count for p in presenter.build_map(self.conn).positions}
        self.assertEqual(counts["A1"], 2)

    def test_前後の空白と大小文字を無視して数える(self) -> None:
        """一覧は `TRIM(...) COLLATE NOCASE` で引く。図が別の数え方をすると、
        押した先の件数と図の見た目が食い違う。"""
        add_stock(self.conn, position=" a1 ")
        counts = {p.name: p.count for p in presenter.build_map(self.conn).positions}
        self.assertEqual(counts["A1"], 1)

    def test_図に無い位置は名指しで知らせる(self) -> None:
        """図から押せない棚があることに、誰も気づけないと困る。"""
        add_stock(self.conn, position="ZZ9")
        self.assertIn("ZZ9", presenter.build_map(self.conn).missing)

    def test_図にある位置は知らせない(self) -> None:
        add_stock(self.conn, position="A1")
        self.assertEqual(presenter.build_map(self.conn).missing, [])


def _state(view, name: str) -> str:
    return next(p.state for p in view.positions if p.name == name)


# ==================================================================
# 一覧
# ==================================================================
class ListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_列の並びはVBAのまま(self) -> None:
        """`hdrStk0`〜`hdrStk6`。現場が見慣れた順序を変えない。"""
        self.assertEqual([label for label, _, _ in presenter.STOCK_COLUMNS],
                         ["幅", "丈", "業界", "記号", "位置", "在庫数", "更新日時"])

    def test_完全一致で引く(self) -> None:
        add_stock(self.conn, width=1100, length=1300)
        add_stock(self.conn, width=1120, length=1300, position="A2")
        view = presenter.search(self.conn, 1100, 1300, "exact")
        self.assertEqual(view.found, 1)

    def test_範囲で引くと近いものも拾う(self) -> None:
        add_stock(self.conn, width=1100, length=1300)
        add_stock(self.conn, width=1120, length=1300, position="A2")
        view = presenter.search(self.conn, 1100, 1300, "range")
        self.assertEqual(view.found, 2)

    def test_知らない引き方は完全一致に倒す(self) -> None:
        """勝手に広く拾うと、無いものを在ると誤解させる。"""
        add_stock(self.conn, width=1120, length=1300)
        self.assertEqual(presenter.search(self.conn, 1100, 1300, "なにか").found, 0)

    def test_見つからないときは次の手を示す(self) -> None:
        view = presenter.search(self.conn, 1100, 1300, "exact")
        self.assertIn(f"±{config.SEARCH_RANGE_TOLERANCE}mm", view.message)

    def test_位置で引ける(self) -> None:
        add_stock(self.conn, position="A1")
        add_stock(self.conn, position="A2", width=1200)
        self.assertEqual(presenter.at_position(self.conn, "A1").found, 1)

    def test_行に払出用の識別子が付く(self) -> None:
        """幅・丈・位置の3つで1行が決まる(VBAと同じ)。"""
        add_stock(self.conn, width=1100, length=1300, position="A1")
        row = presenter.at_position(self.conn, "A1").rows[0]
        self.assertEqual(row["key"],
                         {"width": 1100, "length": 1300, "position": "A1"})

    def test_辞書にできる(self) -> None:
        import json
        add_stock(self.conn)
        json.dumps(presenter.to_dict(presenter.search(self.conn, 1100, 1300)),
                   ensure_ascii=False)


# ==================================================================
# 画面とAPI
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class InventoryWebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import inventory as routes
        self.conn = _web.bind_db(self, routes)
        self.client = _web.make_client()

    def auth(self) -> dict:
        return _web.auth()

    def post(self, path: str, body: dict):
        return self.client.post(path, json=body, headers=self.auth())


class InventoryPageTests(InventoryWebTestCase):
    def test_開ける(self) -> None:
        res = self.client.get("/inventory")
        self.assertEqual(res.status_code, 200)
        self.assertIn("簡易在庫", res.get_data(as_text=True))

    def test_図がJSの前に用意される(self) -> None:
        html = self.client.get("/inventory").get_data(as_text=True)
        self.assertIn('id="map"', html)
        self.assertIn("viewBox", html)

    def test_入力の候補を出す(self) -> None:
        """打つ手間を減らす。VBAのプリセットボタンに対応する。"""
        html = self.client.get("/inventory").get_data(as_text=True)
        for value in ("C1", "スカシ", "組"):
            with self.subTest(value=value):
                self.assertIn(f'value="{value}"', html)

    def test_凡例を出す(self) -> None:
        """色だけで意味を伝えない(§3.8)。"""
        html = self.client.get("/inventory").get_data(as_text=True)
        for label in ("検索で一致", "表示中", "在庫あり", "空"):
            with self.subTest(label=label):
                self.assertIn(label, html)

    def test_払出は一覧から選ばせる(self) -> None:
        """手で打たせると、在庫に無い組み合わせを打ててしまう。"""
        html = self.client.get("/inventory").get_data(as_text=True)
        self.assertIn("上の一覧から払い出す行を選んでください", html)

    def test_資材だけの権限なら無い(self) -> None:
        """資材課は在庫を動かさない。現場の権限が無ければ、隠すのではなく無い。"""
        from app import create_app
        app = create_app("material", token=TOKEN, port=8723,
                         grant=access_control.grant_of("mode:material"))
        app.config["TESTING"] = True
        self.assertEqual(app.test_client().get("/inventory").status_code, 404)


class SearchApiTests(InventoryWebTestCase):
    def test_トークンが要る(self) -> None:
        self.assertEqual(self.client.get("/api/inventory/search?w=1&l=1").status_code, 401)

    def test_引ける(self) -> None:
        add_stock(self.conn)
        body = self.client.get("/api/inventory/search?w=1100&l=1300",
                               headers=self.auth()).get_json()
        self.assertEqual(body["found"], 1)
        hit = [p["name"] for p in body["map"]["positions"] if p["state"] == "hit"]
        self.assertEqual(hit, ["A1"])

    def test_数字でなければ400(self) -> None:
        """打ち間違いは通信の失敗ではない。"""
        res = self.client.get("/api/inventory/search?w=あ&l=1300", headers=self.auth())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], "bad_size")

    def test_0以下は400(self) -> None:
        res = self.client.get("/api/inventory/search?w=0&l=1300", headers=self.auth())
        self.assertEqual(res.status_code, 400)

    def test_位置で引ける(self) -> None:
        add_stock(self.conn, position="A1")
        body = self.client.get("/api/inventory/position/A1",
                               headers=self.auth()).get_json()
        self.assertEqual(body["found"], 1)
        self.assertEqual(_position_state(body, "A1"), presenter.STATE_ACTIVE)

    def test_背景が無ければ404(self) -> None:
        res = self.client.get("/api/inventory/map/background", headers=self.auth())
        self.assertEqual(res.status_code, 404)


def _position_state(body: dict, name: str) -> str:
    return next(p["state"] for p in body["map"]["positions"] if p["name"] == name)


class ReceiveApiTests(InventoryWebTestCase):
    def test_受け入れられる(self) -> None:
        body = self.post("/api/inventory/receive", {
            "width": 1100, "length": 1300, "qty": 5, "position": "A1",
            "symbol": "P1", "industry": "全面", "unit": "台"}).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["new_stock"], 5)

    def test_登録したら取り込み元へも送りにいく(self) -> None:
        """入出庫履歴も「登録のたびに自動でも送られる」対象。"""
        with mock.patch("packaging_tool.data_sync.write_back_in_background") as sent:
            self.post("/api/inventory/receive", {
                "width": 1100, "length": 1300, "qty": 5, "position": "A1"})
        sent.assert_called_once_with()

    def test_断られたときは送りにいかない(self) -> None:
        with mock.patch("packaging_tool.data_sync.write_back_in_background") as sent:
            res = self.post("/api/inventory/issue", {
                "width": 1100, "length": 1300, "qty": 9999, "position": "A1"})
            self.assertGreaterEqual(res.status_code, 400)
        sent.assert_not_called()

    def test_同じ幅丈位置なら足す(self) -> None:
        add_stock(self.conn, qty=5)
        body = self.post("/api/inventory/receive", {
            "width": 1100, "length": 1300, "qty": 3, "position": "A1"}).get_json()
        self.assertEqual(body["new_stock"], 8)

    def test_台数が数字でなければ400(self) -> None:
        res = self.post("/api/inventory/receive", {
            "width": 1100, "length": 1300, "qty": "たくさん", "position": "A1"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["field"], "qty")

    def test_位置が空なら422(self) -> None:
        """業務上の拒否。打ち間違い(400)とは区別する。"""
        res = self.post("/api/inventory/receive", {
            "width": 1100, "length": 1300, "qty": 5, "position": ""})
        self.assertEqual(res.status_code, 422)
        self.assertIn("位置", res.get_json()["error"]["message"])

    def test_0台は422(self) -> None:
        res = self.post("/api/inventory/receive", {
            "width": 1100, "length": 1300, "qty": 0, "position": "A1"})
        self.assertEqual(res.status_code, 422)

    def test_履歴が残る(self) -> None:
        """現物と帳簿がずれたとき、経緯をたどれるようにする。"""
        self.post("/api/inventory/receive", {
            "width": 1100, "length": 1300, "qty": 5, "position": "A1"})
        rows = self.conn.execute(
            "SELECT 区分, 数量, 在庫数_更新後 FROM パレット入出庫履歴").fetchall()
        self.assertEqual([(r["区分"], r["数量"], r["在庫数_更新後"]) for r in rows],
                         [("受入", 5, 5)])


class IssueApiTests(InventoryWebTestCase):
    def test_払い出せる(self) -> None:
        add_stock(self.conn, qty=5)
        body = self.post("/api/inventory/issue", {
            "width": 1100, "length": 1300, "position": "A1", "qty": 2}).get_json()
        self.assertEqual(body["new_stock"], 3)

    def test_在庫を超えたら422(self) -> None:
        """現物より多く払い出せてしまうと、帳簿が現物とずれる。"""
        add_stock(self.conn, qty=5)
        res = self.post("/api/inventory/issue", {
            "width": 1100, "length": 1300, "position": "A1", "qty": 6})
        self.assertEqual(res.status_code, 422)
        self.assertIn("在庫数を超える", res.get_json()["error"]["message"])

    def test_無い行は422(self) -> None:
        res = self.post("/api/inventory/issue", {
            "width": 9999, "length": 9999, "position": "ZZ", "qty": 1})
        self.assertEqual(res.status_code, 422)

    def test_0になったら行ごと消える(self) -> None:
        """リスト管理が「要」でなければ、VBA と同じく行を残さない。"""
        add_stock(self.conn, qty=2)
        self.post("/api/inventory/issue", {
            "width": 1100, "length": 1300, "position": "A1", "qty": 2})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM PalletMaster").fetchone()[0], 0)

    def test_リスト管理が要なら0でも残る(self) -> None:
        add_stock(self.conn, qty=2, listed="要")
        self.post("/api/inventory/issue", {
            "width": 1100, "length": 1300, "position": "A1", "qty": 2})
        row = self.conn.execute("SELECT 在庫数 FROM PalletMaster").fetchone()
        self.assertEqual(row["在庫数"], 0)


class ConflictTests(InventoryWebTestCase):
    """他の端末が先に動かしたとき。

    1台のPCで複数ラインを切り替えて使う前提だが、DBは共有フォルダに
    置ける。**黙って上書きしない**ことが要点。
    """

    def _force_conflict(self, name: str) -> None:
        """`pallet_service` が競合を返した状況を作る。

        本物の競合は「読んだ直後に他所が更新する」という時間の隙で起きるので、
        1プロセスの中では作れない。競合そのものは
        `tests/test_pallet_service.py` が見ているので、ここでは
        **競合をHTTPでどう返すか**だけを確かめる。
        """
        from app.routes import inventory as routes
        original = getattr(routes.pallet_service, name)

        def conflicting(*_args, **_kwargs):
            return pallet_service.TransactionResult(
                ok=False, conflict=True,
                message="他の端末がこの在庫を同時に更新しました。"
                        "画面を更新してからやり直してください。")

        setattr(routes.pallet_service, name, conflicting)
        self.addCleanup(setattr, routes.pallet_service, name, original)

    def test_払出の競合は409(self) -> None:
        add_stock(self.conn, qty=5)
        self._force_conflict("issue")
        res = self.post("/api/inventory/issue", {
            "width": 1100, "length": 1300, "position": "A1", "qty": 1})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "conflict")

    def test_受入の競合も409(self) -> None:
        add_stock(self.conn, qty=5)
        self._force_conflict("receive")
        res = self.post("/api/inventory/receive", {
            "width": 1100, "length": 1300, "position": "A1", "qty": 1})
        self.assertEqual(res.status_code, 409)

    def test_競合の文言は取り直しを促す(self) -> None:
        """古い数字のまま操作を続けさせない。"""
        add_stock(self.conn, qty=5)
        self._force_conflict("issue")
        res = self.post("/api/inventory/issue", {
            "width": 1100, "length": 1300, "position": "A1", "qty": 1})
        self.assertIn("やり直して", res.get_json()["error"]["message"])

    def test_競合と業務上の拒否を取り違えない(self) -> None:
        """409は「取り直せば通るかもしれない」、422は「このままでは通らない」。

        同じ扱いにすると、画面はどちらを案内すべきか決められない。
        """
        add_stock(self.conn, qty=1)
        rejected = self.post("/api/inventory/issue", {
            "width": 1100, "length": 1300, "position": "A1", "qty": 99})
        self.assertEqual(rejected.status_code, 422)
        self.assertFalse(rejected.get_json()["conflict"])


if __name__ == "__main__":
    unittest.main()
