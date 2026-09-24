"""棚検索(Web版) — 配置図・最寄り検索・配置編集

`packaging_tool/layout_session.py` と `app/routes/layout.py` を見る。

【ここで守りたいこと】
- **保存するまでファイルに触らない**。ドラッグするたびに書き込むと、
  間違えて動かしたものを戻せない
- 通常は動かせない。図はよく押す(中身を見る)ので、常時ドラッグ
  できると見るつもりの操作で置き場がずれる
- 拠点は資材選択と**共有する設定**。画面ごとに別の拠点を持たせると、
  同じロットで違う結果が出る
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import access_control, floor_plan  # noqa: E402
from packaging_tool import layout_session, map_data  # noqa: E402
from packaging_tool.presenters import layout as presenter  # noqa: E402
from tests import _web  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"
TOKEN = _web.TOKEN          # 土台と同じものを使う


def insert_board(conn, *, width, length, board_type="ハードボード",
                 label="lblItem1") -> None:
    conn.execute(
        "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
        " VALUES (?,?,?,?)", (width, length, board_type, label))
    conn.commit()


@unittest.skipUnless(HAS_WEB, _SKIP)
class LayoutWebTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import layout as routes

        layout_session.reset_session()
        self.addCleanup(layout_session.reset_session)

        # 配置図の保存先を一時フォルダへ逃がす。**作業ツリーを汚さない**
        # (テストを流しただけで data/floor_plan.json ができると、
        #  次の実行が前回の編集を読む)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._user_path = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"
        self.addCleanup(
            lambda: setattr(floor_plan, "USER_PATH", self._user_path))

        self.conn = _web.bind_db(self, routes)
        self.client = _web.make_client(port=8715, ready=False)

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}

    def get(self, path="/api/layout/state") -> dict:
        res = self.client.get(path, headers=self.auth())
        self.assertEqual(res.status_code, 200, path)
        return res.get_json()

    def post(self, path, body=None, expect=200) -> dict:
        res = self.client.post(path, json=body or {}, headers=self.auth())
        self.assertEqual(res.status_code, expect, f"{path}: {res.get_json()}")
        return res.get_json()

    def session(self):
        return layout_session.get_session()

    def first_shelf(self) -> str:
        return self.get()["shelves"][0]["name"]


class PageTests(LayoutWebTestCase):
    def test_開ける(self) -> None:
        self.assertEqual(self.client.get("/layout").status_code, 200)

    def test_資材だけの権限なら無い(self) -> None:
        """倉庫スタッフは資材を取りに行かない。隠すのではなく存在しない。"""
        from app import create_app
        app = create_app("material", token=TOKEN, port=8725,
                         grant=access_control.grant_of("mode:material"))
        app.config["TESTING"] = True
        client = app.test_client()
        self.assertEqual(client.get("/layout").status_code, 404)
        self.assertEqual(
            client.get("/api/layout/state", headers=self.auth()).status_code, 404)

    def test_複数選択の件数を出す場所がある(self) -> None:
        """Shift+クリックの見た目(点線の色)だけでは選べたか分からない、
        という現場の声への対応。文字でも件数を出す(JS `mapedit.js` の
        `onSelectionChange` がここへ書き込む)。"""
        html = self.client.get("/layout").get_data(as_text=True)
        self.assertIn('id="multiNote"', html)
        self.assertIn("Shift", html)

    def test_マスタが無ければ理由を出す(self) -> None:
        state = self.get()
        self.assertFalse(state["available"])
        self.assertIn("設定画面", state["unavailable_message"])

        insert_board(self.conn, width=750, length=1130)
        self.assertTrue(self.get()["available"])

    def test_図がそのままSVGに載る(self) -> None:
        state = self.get()
        self.assertTrue(state["shelves"])
        self.assertTrue(state["bases"])
        self.assertRegex(state["view_box"], r"^0 0 \d")


class SearchTests(LayoutWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        name = floor_plan.load().item_names[0]
        insert_board(self.conn, width=750, length=1130, label=name)
        self.label = name

    def test_最寄りが当たる(self) -> None:
        state = self.post("/api/layout/search",
                          {"width": 750, "length": 1130})
        self.assertTrue(state["found"])
        self.assertIn(self.label, state["result"])
        self.assertIn("疲労度", state["result"])
        hit = [s for s in state["shelves"] if s["state"] == presenter.STATE_HIT]
        self.assertEqual([s["name"] for s in hit], [self.label])

    def test_サイズが無ければ400(self) -> None:
        self.post("/api/layout/search", {"width": "", "length": ""}, expect=400)

    def test_打ち間違いは無視せず断る(self) -> None:
        """**空欄と打ち間違いを混ぜない。**

        空欄は「その寸法では絞らない」。数でない字は入力の誤りなので、
        黙って無視すると、打ったつもりの寸法で絞られていない結果が出る。
        """
        body = self.post("/api/layout/search", {"width": "あ", "length": "1130"},
                         expect=400)
        self.assertIn("幅は数字で", body["error"]["message"])

    def test_その寸法の資材が無ければ422(self) -> None:
        """「資材が無い」と「置き場が書かれていない」は別の話。"""
        body = self.post("/api/layout/search",
                         {"width": 9999, "length": 9999}, expect=422)
        self.assertIn("見当たりません", body["message"])
        # 断られても画面ぜんぶが入っている
        self.assertIn("shelves", body)

    def test_置き場が登録されていなければ422(self) -> None:
        """**作業者の言葉で言う。**

        以前は「マスタの『データラベル』列を確認してください」と出して
        いた。作り手の言葉で、読んだ作業者は「あぁ無いのか」で終わる
        (現場の指摘:「ツール制作者よりのコメントすぎる」)。
        次にすること ── 誰に言えばよいか ── まで書く。
        """
        insert_board(self.conn, width=321, length=654, label="")
        body = self.post("/api/layout/search",
                         {"width": 321, "length": 654}, expect=422)
        self.assertIn("資材課の人に伝えてください", body["message"])
        self.assertIn("321×654", body["message"])

    def test_種別を選ばせない(self) -> None:
        """**両方を一度に見る。** 古い画面が `kind` を送っても断らない。"""
        state = self.post("/api/layout/search",
                          {"kind": "なにか", "width": 750, "length": 1130})
        self.assertTrue(state["found"])

    def test_片側だけでも探せる(self) -> None:
        """手元に片方の寸法しか分からないことがある。"""
        for body in ({"width": 750}, {"length": 1130}):
            with self.subTest(body=body):
                state = self.post("/api/layout/search", body)
                self.assertTrue(state["found"], state["result"])
                self.assertIn(self.label, state["result"])

    def test_向きを入れ替えても当たる(self) -> None:
        state = self.post("/api/layout/search", {"width": 1130, "length": 750})
        self.assertTrue(state["found"])

    def test_アングルは打った数のどれかに当たれば出る(self) -> None:
        """**ボードと不揃いにしない。**

        ボードは向きを入れ替えて当てるのに、アングルだけ「幅の欄に
        打ったから当たらない」では、1130と打ったのに1130のアングルが
        出てこないことになる。
        """
        other = floor_plan.load().item_names[1]
        self.conn.execute(
            "INSERT INTO CornerboardMaster (アングル丈, データラベル)"
            " VALUES (?, ?)", (1130, other))
        self.conn.commit()
        state = self.post("/api/layout/search", {"width": 1130, "length": 750})
        self.assertIn("アングル", state["result"])

    def test_アングルも一緒に当たる(self) -> None:
        """種別を選び直さずに、ボードとアングルの両方を見る。"""
        other = floor_plan.load().item_names[1]
        self.conn.execute(
            "INSERT INTO CornerboardMaster (アングル丈, データラベル)"
            " VALUES (?, ?)", (1130, other))
        self.conn.commit()
        state = self.post("/api/layout/search", {"length": 1130})
        self.assertIn("ボード", state["result"])
        self.assertIn("アングル", state["result"])
        names = {s["name"] for s in state["shelves"]
                 if s["state"] == presenter.STATE_HIT}
        self.assertEqual(names, {self.label, other})

    def test_押すと中身が出る(self) -> None:
        """VBA版に無い機能。図から何があるか読めるようにする。"""
        state = self.post("/api/layout/select", {"name": self.label})
        self.assertEqual(state["selected"], self.label)
        self.assertEqual([m["size"] for m in state["materials"]], ["750×1130"])

    def test_図に無い置き場は押せない(self) -> None:
        self.post("/api/layout/select", {"name": "無い置き場"}, expect=400)

    def test_資材のある置き場と空の置き場を分ける(self) -> None:
        """空まで同じ濃さだと、どこに物があるのか図から読めない。"""
        states = {s["name"]: s["state"] for s in self.get()["shelves"]}
        self.assertEqual(states[self.label], presenter.STATE_IDLE)
        self.assertIn(presenter.STATE_EMPTY, states.values())

    def test_図に無いラベルを知らせる(self) -> None:
        """検索に当たらないので、黙っていると原因が分からない。"""
        insert_board(self.conn, width=100, length=100, label="lblItem999")
        self.assertIn("lblItem999", self.get()["unplaced"])


class ShelfContentsTests(LayoutWebTestCase):
    """置き場の中身(VBA `OpenSizePopup` → `frmSizePopup`)。"""

    def test_ボードとアングルを両方出す(self) -> None:
        """**原文はアングルのある置き場でボードを隠していた**
        (`ShowAngleSection` の `mAngleOnly`)。両方置いてある場所では
        ボードが見えず、取りに行ってから気づくことになる。
        """
        name = floor_plan.load().item_names[0]
        insert_board(self.conn, width=750, length=1130, label=name)
        self.conn.execute(
            "INSERT INTO CornerboardMaster (アングル丈, データラベル)"
            " VALUES (?, ?)", (1500, name))
        self.conn.commit()

        state = self.post("/api/layout/select", {"name": name})
        kinds = {row["category"] for row in state["materials"]}
        self.assertEqual(kinds, {"ボード", "アングル"})

    def test_中身が無いときは作業者の言葉で言う(self) -> None:
        """「マスタの『データラベル』列を確認してください」は作り手の
        言葉で、読んだ作業者は次に何をすればよいか分からない
        (現場の指摘:「ツール制作者よりのコメントすぎる」)。
        検索側は直してあったが、こちらが残っていた。
        """
        insert_board(self.conn, width=750, length=1130)
        name = floor_plan.load().item_names[2]
        state = self.post("/api/layout/select", {"name": name})
        self.assertEqual(state["materials"], [])
        self.assertIn("資材課の人に伝えてください", state["materials_note"])
        self.assertNotIn("列を確認してください", state["materials_note"])


class BasePointNoteTests(LayoutWebTestCase):
    """**どこから測った距離なのかを言う。**

    距離も疲労度も拠点を原点に測るので、拠点が違えば答えが全部変わる。
    VBAは拠点が選ばれていないとき「拠点未選択 → クリック位置で計算」と
    その場に書いていた(`FindNearestSameSize` / `CalcDistance`)。
    """

    def setUp(self) -> None:
        super().setUp()
        name = floor_plan.load().item_names[0]
        insert_board(self.conn, width=750, length=1130, label=name)

    def search(self, **kw):
        return self.post("/api/layout/search",
                         {"width": 750, "length": 1130}, **kw)

    def test_未登録のまま探したら既定で測っていると言う(self) -> None:
        from packaging_tool import user_settings
        with mock.patch.object(user_settings, "is_position_set",
                               return_value=False):
            state = self.search()
        self.assertTrue(state["found"])
        self.assertIn("拠点が未登録", state["result"])
        self.assertIn(user_settings.get_position(), state["result"])

    def test_選んであれば何も言わない(self) -> None:
        from packaging_tool import user_settings
        with mock.patch.object(user_settings, "is_position_set",
                               return_value=True):
            state = self.search()
        self.assertNotIn("拠点が未登録", state["result"])

    def test_図に無い拠点なら置き場のせいにしない(self) -> None:
        """**原因の取り違えを防ぐ。**

        拠点が配置図に無いと候補の距離が1件も測れず、結果としては
        「置き場が登録されていません」になる。読んだ人はマスタの
        データラベルを直しに行くが、直すべきは拠点のほう。
        """
        from packaging_tool import user_settings
        with mock.patch.object(user_settings, "get_position",
                               return_value="もう無い拠点"):
            body = self.search(expect=422)
        self.assertIn("配置図にありません", body["message"])
        self.assertNotIn("資材課の人に伝えてください", body["message"])


class BasePointTests(LayoutWebTestCase):
    def test_拠点は共有の設定(self) -> None:
        """画面ごとに別の拠点を持たせると、同じロットで違う結果が出る。"""
        from packaging_tool import user_settings
        target = [n for n in self.get()["base_points"]
                  if n != user_settings.get_position()][0]

        state = self.post("/api/layout/base-point", {"name": target})
        self.assertEqual(state["base_point"], target)
        self.assertEqual(user_settings.get_position(), target)

    def test_図に無い拠点は選べない(self) -> None:
        self.post("/api/layout/base-point", {"name": "どこか"}, expect=400)

    def test_拠点を替えたら探し直す(self) -> None:
        """**前の拠点の答えを置き去りにしない。**

        距離も疲労度も拠点から測るので、切り替えた時点で画面に出ている
        「最寄り」は前の拠点のもの。VBAはここで「直前ラベル記憶が
        リセットされます」と訊いていた(`lstPosition_Change`)。
        こちらは訊かずに同じ条件で探し直す ── 拠点を替えるのは、
        そこからの答えが見たいからで、訊かれても答えは1つしかない。
        """
        from packaging_tool import user_settings
        name = floor_plan.load().item_names[0]
        insert_board(self.conn, width=750, length=1130, label=name)
        first = self.post("/api/layout/search", {"width": 750, "length": 1130})
        self.assertTrue(first["found"])

        target = [n for n in self.get()["base_points"]
                  if n != user_settings.get_position()][0]
        state = self.post("/api/layout/base-point", {"name": target})

        # 答えは残っている(消して空白にするのではなく、出し直す)
        self.assertIn("最寄り", state["result"])
        self.assertTrue(
            [s for s in state["shelves"] if s["state"] == presenter.STATE_HIT])
        # **新しい拠点から測り直した距離になっている。** 前の拠点の
        # 数字が残っていれば、ここで食い違う
        from packaging_tool import location_service
        after = location_service.find_nearest_shelf_for_board(
            self.conn, 750, 1130, target)
        self.assertIn(f"距離{after.distance:.0f}", state["result"])

    def test_探していなければ何も出さない(self) -> None:
        from packaging_tool import user_settings
        target = [n for n in self.get()["base_points"]
                  if n != user_settings.get_position()][0]
        state = self.post("/api/layout/base-point", {"name": target})
        self.assertEqual(state["result"], "")


class EditTests(LayoutWebTestCase):
    def test_編集をONにするまで動かせない(self) -> None:
        """図はよく押す。常時ドラッグできると、見るつもりの操作でずれる。"""
        name = self.first_shelf()
        body = self.post("/api/layout/move", {"name": name, "x": 10, "y": 10},
                         expect=400)
        self.assertIn("配置編集", body["error"]["message"])

    def marks(self, state) -> tuple[list, str]:
        """図に出ている強調表示を**2つとも**返す。

        **片方だけ見ない。** 見た目の違う2つがあり、片方を消しても
        現場からは何も変わって見えない:

            塗りつぶし → `state == STATE_HIT`(`session.highlight` 由来)
            3pxの水色枠 → `selected`(`session.selected` 由来。
                          画面では `.shelf.is-selected` が描く)

        現場が「強調表示水色枠」と呼んでいるのは後者。以前ここで
        塗りつぶしだけを確かめていたため、枠が残ったままなのに試験は
        通り、同じ指摘を3度受けることになった。
        """
        fill = [s["name"] for s in state["shelves"]
                if s["state"] == presenter.STATE_HIT]
        return fill, state["selected"]

    def test_編集ONで検索の強調表示が塗りも枠も消える(self) -> None:
        """現場の声:「棚検索の配置編集でつかみ→移動がないと強調表示
        水色枠が消えない」。検索と配置編集は別の作業なので、編集に入った
        時点で前の検索結果の強調表示は捨てる。動かして初めて消える、
        という偶然の回避策に頼らせない。

        **検索は塗りと枠の両方を点ける**(`search` は最寄りの置き場を
        `selected` にも入れる)ので、両方消えるまでが直ったということ。
        """
        name = self.first_shelf()
        insert_board(self.conn, width=750, length=1130, label=name)
        self.post("/api/layout/search", {"kind": "board", "width": 750, "length": 1130})
        fill, frame = self.marks(self.get())
        self.assertEqual(fill, [name])
        self.assertEqual(frame, name, "検索は枠(selected)も点けるはず")

        fill, frame = self.marks(self.post("/api/layout/edit", {"on": True}))
        self.assertEqual(fill, [])
        self.assertEqual(frame, "", "水色枠(selected)が残っている")

    def test_編集OFFでも検索の強調表示が塗りも枠も消える(self) -> None:
        name = self.first_shelf()
        insert_board(self.conn, width=750, length=1130, label=name)
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/search", {"kind": "board", "width": 750, "length": 1130})

        fill, frame = self.marks(self.post("/api/layout/edit", {"on": False}))
        self.assertEqual(fill, [])
        self.assertEqual(frame, "", "水色枠(selected)が残っている")

    def test_置き場を押した枠も編集に入れば消える(self) -> None:
        """検索を通さず、置き場を押して中身を見ただけでも枠は点く。

        簡易在庫が `clearSelection()`(Shift複数選択)だけでなく
        `picked`(単発クリック)も捨てているのと同じ ── 片方だけでは
        「編集にしても強調表示が残る」になる。
        """
        name = self.first_shelf()
        self.post("/api/layout/select", {"name": name})
        self.assertEqual(self.get()["selected"], name)

        self.assertEqual(self.post("/api/layout/edit", {"on": True})["selected"], "")

        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/move", {"name": name, "x": 10, "y": 10})

    def test_動かしても保存するまでファイルに書かない(self) -> None:
        """間違えて動かしても、保存しなければ元に戻せる。"""
        name = self.first_shelf()
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/move", {"name": name, "x": 40, "y": 60})
        self.assertTrue(state["dirty"])
        self.assertFalse(floor_plan.USER_PATH.exists())

        state = self.post("/api/layout/save")
        self.assertFalse(state["dirty"])
        self.assertTrue(floor_plan.USER_PATH.exists())

    def test_図の外へは出せない(self) -> None:
        """外へ出すと二度と掴めない。"""
        name = self.first_shelf()
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/move",
                          {"name": name, "x": -500, "y": 99999})
        moved = next(s for s in state["shelves"] if s["name"] == name)
        self.assertGreaterEqual(moved["x"], 0)
        self.assertLessEqual(moved["y"], state["height"])

    def test_足して消せる(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/add", {"name": "lblItem99"})
        self.assertIn("lblItem99", [s["name"] for s in state["shelves"]])
        # 真ん中に置く。どこに出たか分からないと探すことになる
        added = next(s for s in state["shelves"] if s["name"] == "lblItem99")
        self.assertAlmostEqual(added["x"], state["width"] / 2, delta=1)

        state = self.post("/api/layout/remove", {"name": "lblItem99"})
        self.assertNotIn("lblItem99", [s["name"] for s in state["shelves"]])

    def test_同じ名前は足せない(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/add", {"name": self.first_shelf()}, expect=400)

    def test_名前が空なら足せない(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/add", {"name": "   "}, expect=400)

    def test_出荷時に戻せる(self) -> None:
        name = self.first_shelf()
        before = next(s for s in self.get()["shelves"] if s["name"] == name)
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/move", {"name": name, "x": 5, "y": 5})
        self.post("/api/layout/save")

        state = self.post("/api/layout/reset")
        after = next(s for s in state["shelves"] if s["name"] == name)
        self.assertEqual((after["x"], after["y"]), (before["x"], before["y"]))
        self.assertFalse(floor_plan.USER_PATH.exists())


class BackgroundTests(LayoutWebTestCase):
    # 1x1 の透明PNG
    PNG = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

    def test_編集中でないと差し替えられない(self) -> None:
        self.post("/api/layout/background", {"image": self.PNG}, expect=400)

    def test_JPEGも使える(self) -> None:
        """`tk.PhotoImage` の PNG/GIF 制約から解放される(§7.2)。"""
        from app.routes import layout as routes
        self.assertIn("data:image/jpeg;base64,", routes.ALLOWED_IMAGE_PREFIXES)

    def test_画像でないものは断る(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/background",
                  {"image": "data:text/html;base64,PHNjcmlwdD4="}, expect=400)

    def test_大きすぎる画像は断る(self) -> None:
        from app.routes import layout as routes
        self.post("/api/layout/edit", {"on": True})
        huge = "data:image/png;base64," + "A" * routes.MAX_BACKGROUND_BYTES
        body = self.post("/api/layout/background", {"image": huge}, expect=400)
        self.assertIn("大きすぎ", body["error"]["message"])

    def test_差し替えると図から読める(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/background", {"image": self.PNG})
        self.assertEqual(state["background"], "/api/layout/map/background")

        res = self.client.get("/api/layout/map/background", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "image/png")

    def test_出荷時の図には背景写真がある(self) -> None:
        res = self.client.get("/api/layout/map/background", headers=self.auth())
        self.assertEqual(res.status_code, 200)

    def test_外したら404(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/background", {"image": ""})
        res = self.client.get("/api/layout/map/background", headers=self.auth())
        self.assertEqual(res.status_code, 404)

    def test_外せる(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/background", {"image": self.PNG})
        state = self.post("/api/layout/background", {"image": ""})
        self.assertEqual(state["background"], "")


# ==================================================================
# サーバ側のフォルダ参照 (§7.2)
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class FsBrowseTests(unittest.TestCase):
    """`GET /api/fs/list`。**名前しか返さない**ことを守る。"""

    def setUp(self) -> None:
        from app import create_app
        app = create_app("field", token=TOKEN, port=8716)
        app.config["TESTING"] = True
        self.client = app.test_client()

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "sub").mkdir()
        (self.root / "master.sqlite3").write_text("dummy", encoding="utf-8")
        (self.root / "secret.txt").write_text("見せない", encoding="utf-8")

    def browse(self, path: str) -> dict:
        res = self.client.get(f"/api/fs/list?path={path}",
                              headers={"X-Tool-Token": TOKEN})
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def test_フォルダと取り込み元ファイルだけ出す(self) -> None:
        """ファイルの中身を読む口はここに作らない。"""
        view = self.browse(str(self.root))
        self.assertEqual([d["name"] for d in view["dirs"]], ["sub"])
        self.assertEqual([f["name"] for f in view["files"]], ["master.sqlite3"])
        # 探していないファイルは名前も出さない
        self.assertNotIn("secret.txt", str(view))

    def test_見つかったことを言う(self) -> None:
        self.assertIn("取り込み元ファイル", self.browse(str(self.root))["message"])

    def test_上のフォルダへ行ける(self) -> None:
        """出発点を探せないと、置き場所を指定できない。"""
        view = self.browse(str(self.root / "sub"))
        self.assertEqual(view["parent"], str(self.root))

    def test_相対パスはアプリのフォルダから見る(self) -> None:
        """**どこから見た相対かは1か所で決まっている**ので、断らない。

        起点を「いまの作業フォルダ」にすると、どこから起動したかで
        同じ設定が違う場所を指す。アプリのフォルダなら、フォルダごと
        コピーして配っても写しの中の同じ場所を指し続ける。
        """
        from packaging_tool import config

        view = self.browse("packaging_tool")
        self.assertEqual(view["path"], str(config.BASE_DIR / "packaging_tool"))
        self.assertTrue(view["exists"])
        # どこから見た相対かを画面に出せる
        self.assertEqual(view["relative_to"], str(config.BASE_DIR))

    def test_ファイルを指したら入れ物を開く(self) -> None:
        """目当てのファイルが見えているのに「フォルダを選べ」は一手多い。"""
        view = self.browse(str(self.root / "master.sqlite3"))
        self.assertEqual(view["path"], str(self.root))
        self.assertEqual(view["picked"], "master.sqlite3")

    def test_無いフォルダは理由を出す(self) -> None:
        view = self.browse(str(self.root / "nope"))
        self.assertFalse(view["exists"])
        self.assertIn("見えません", view["message"])

    def test_空なら出発点を出す(self) -> None:
        """どこから始めればよいか分からない、を作らない。"""
        view = self.browse("")
        self.assertTrue(view["roots"])

    def test_件数に上限がある(self) -> None:
        """共有フォルダには数千の項目があることがある。"""
        from packaging_tool.presenters import fs_browse
        many = self.root / "many"
        many.mkdir()
        for i in range(fs_browse.MAX_ENTRIES + 5):
            (many / f"d{i:04d}").mkdir()
        view = self.browse(str(many))
        self.assertTrue(view["truncated"])
        self.assertEqual(len(view["dirs"]), fs_browse.MAX_ENTRIES)

    def test_トークンが要る(self) -> None:
        self.assertEqual(self.client.get("/api/fs/list?path=/").status_code, 401)


class MapDataTests(unittest.TestCase):
    """ファイルの読み書き。`floor_plan` と `pallet_map` が共有する部分。"""

    def test_壊れたJSONでも落ちない(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{ こわれている", encoding="utf-8")
            self.assertIsNone(map_data.read_json(path))



class ResizeTests(LayoutWebTestCase):
    """置き場の大きさを変える。**背景の写真に合わせこむため。**

    出荷時の大きさはどれも同じ四角だが、実際の棚は間口も奥行もまちまち。
    写真を下敷きにすると箱だけが浮くので、大きさを変えられないと
    「図を見た瞬間に現場と重なる」という値打ちが出ない。
    """

    def size_of(self, state, name):
        shelf = next(s for s in state["shelves"] if s["name"] == name)
        return (shelf["w"], shelf["h"])

    def test_編集をONにするまで変えられない(self) -> None:
        name = self.first_shelf()
        body = self.post("/api/layout/resize", {"name": name, "w": 40, "h": 30},
                         expect=400)
        self.assertIn("配置編集", body["error"]["message"])

    def test_大きさを変えられる(self) -> None:
        name = self.first_shelf()
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/resize", {"name": name, "w": 40, "h": 30})
        self.assertEqual(self.size_of(state, name), (40, 30))
        self.assertTrue(state["dirty"])

    def test_左上は動かない(self) -> None:
        """掴んだ角だけが動く。どちらが動くのか分からない操作にしない。"""
        name = self.first_shelf()
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/move", {"name": name, "x": 30, "y": 40})
        state = self.post("/api/layout/resize", {"name": name, "w": 25, "h": 25})
        shelf = next(s for s in state["shelves"] if s["name"] == name)
        self.assertEqual((shelf["x"], shelf["y"]), (30, 40))

    def test_掴めない大きさにはできない(self) -> None:
        """小さくしすぎると掴めなくなり、編集モードから戻せない。"""
        name = self.first_shelf()
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/resize", {"name": name, "w": 0, "h": -5})
        w, h = self.size_of(state, name)
        self.assertGreaterEqual(w, 1)
        self.assertGreaterEqual(h, 1)

    def test_図より大きくはできない(self) -> None:
        """図より大きい箱は、動かしても端が見えず位置を合わせられない。"""
        name = self.first_shelf()
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/resize",
                          {"name": name, "w": 99999, "h": 99999})
        w, h = self.size_of(state, name)
        self.assertLessEqual(w, state["width"])
        self.assertLessEqual(h, state["height"])

    def test_大きくしてもはみ出さない(self) -> None:
        """右下へ寄せた箱を広げると枠から出る。動かすときと同じ枠に戻す。"""
        name = self.first_shelf()
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/move",
                          {"name": name, "x": 99999, "y": 99999})
        state = self.post("/api/layout/resize", {"name": name, "w": 80, "h": 60})
        shelf = next(s for s in state["shelves"] if s["name"] == name)
        self.assertLessEqual(shelf["x"] + shelf["w"], state["width"])
        self.assertLessEqual(shelf["y"] + shelf["h"], state["height"])

    def test_大きさが違えば断る(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/resize",
                  {"name": self.first_shelf(), "w": "ひろい", "h": 10},
                  expect=400)

    def test_図に無い置き場は変えられない(self) -> None:
        # `move` と同じ断り方(`_STATUS_BY_REASON` の not_listed)
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/resize", {"name": "無い置き場", "w": 20, "h": 20},
                  expect=400)


class BackgroundPlaceTests(LayoutWebTestCase):
    """背景の写真をずらす・拡げ縮めする。**箱は動かさない。**

    写真の画角は図の枠と一致しないので、枠いっぱいに引き伸ばすと必ず
    ずれる。箱を全部動かして合わせるより、下敷きの側を合わせるほうが
    早く、やり直しても箱の位置は壊れない。
    """

    def test_編集をONにするまで動かせない(self) -> None:
        self.post("/api/layout/background/place",
                  {"x": 10, "y": 10, "scale": 1}, expect=400)

    def test_ずらして拡げられる(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/background/place",
                          {"x": 12, "y": -8, "scale": 1.25})
        self.assertEqual(state["background_x"], 12)
        self.assertEqual(state["background_y"], -8)
        self.assertEqual(state["background_scale"], 1.25)
        self.assertTrue(state["dirty"])

    def test_箱は動かない(self) -> None:
        name = self.first_shelf()
        self.post("/api/layout/edit", {"on": True})
        before = next(s for s in self.get()["shelves"] if s["name"] == name)
        state = self.post("/api/layout/background/place",
                          {"x": 50, "y": 50, "scale": 2})
        after = next(s for s in state["shelves"] if s["name"] == name)
        self.assertEqual((after["x"], after["y"]), (before["x"], before["y"]))

    def test_消える倍率にはできない(self) -> None:
        """0倍は消えたのと同じで、戻し方が分からなくなる。"""
        self.post("/api/layout/edit", {"on": True})
        state = self.post("/api/layout/background/place",
                          {"x": 0, "y": 0, "scale": 0})
        self.assertGreater(state["background_scale"], 0)

    def test_値が無ければ断る(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/background/place", {"x": 1, "y": 2}, expect=400)

    def test_差し替えたら合わせこみは初期に戻る(self) -> None:
        """前の写真のずらし量を持ち越すと、枠の外に出ていることがある。"""
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/background/place",
                  {"x": 40, "y": 40, "scale": 3})
        state = self.post("/api/layout/background", {
            "image": "data:image/png;base64,"
                     "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="})
        self.assertEqual(state["background_x"], 0)
        self.assertEqual(state["background_y"], 0)
        self.assertEqual(state["background_scale"], 1)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()


class ArrangeTests(LayoutWebTestCase):
    """棚検索の「そろえる・隙間をなくす」。計算は `map_data.arrange`。"""

    def shelves(self, state=None) -> dict:
        state = state or self.get()
        return {s["name"]: s for s in state["shelves"]}

    def names(self, n=3) -> list[str]:
        return list(self.shelves())[:n]

    def test_編集をONにするまで並べられない(self) -> None:
        body = self.post("/api/layout/arrange",
                         {"names": self.names(), "op": "top"}, expect=400)
        self.assertIn("配置編集", body["error"]["message"])

    def test_上をそろえる(self) -> None:
        names = self.names()
        self.post("/api/layout/edit", {"on": True})
        before = self.shelves()
        top = min(before[n]["y"] for n in names)
        after = self.shelves(self.post("/api/layout/arrange",
                                       {"names": names, "op": "top"}))
        for n in names:
            with self.subTest(name=n):
                self.assertAlmostEqual(after[n]["y"], top)

    def test_横に詰めると隙間が無くなる(self) -> None:
        names = self.names()
        self.post("/api/layout/edit", {"on": True})
        after = self.shelves(self.post("/api/layout/arrange",
                                       {"names": names, "op": "pack_row"}))
        order = sorted(names, key=lambda n: after[n]["x"])
        for left, right in zip(order, order[1:]):
            with self.subTest(pair=(left, right)):
                self.assertAlmostEqual(after[right]["x"],
                                       after[left]["x"] + after[left]["w"])

    def test_何をしたか言う(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        body = self.post("/api/layout/arrange",
                         {"names": self.names(), "op": "top"})
        self.assertIn("上をそろえました", body["message"])
        self.assertIn("3件", body["message"])

    def test_保存するまでファイルに書かない(self) -> None:
        """**動かす・大きさを変えると同じ約束。** 並べ直しただけで
        ファイルが書き換わると、試しに押したのが残ってしまう。"""
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/arrange", {"names": self.names(), "op": "top"})
        self.assertTrue(self.session().dirty)
        self.assertFalse(floor_plan.USER_PATH.exists())

    def test_1つだけでは並べない(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        body = self.post("/api/layout/arrange",
                         {"names": self.names(1), "op": "top"}, expect=400)
        self.assertIn("2つ以上", body["error"]["message"])

    def test_知らない名前が混ざったら1つも動かさない(self) -> None:
        """**途中まで動いた状態で止めない。** どこまで直ったのかが
        画面から読めなくなる。"""
        names = self.names()
        self.post("/api/layout/edit", {"on": True})
        before = self.shelves()
        self.post("/api/layout/arrange",
                  {"names": names + ["無い棚"], "op": "left"}, expect=400)
        after = self.shelves()
        for n in names:
            with self.subTest(name=n):
                self.assertEqual((after[n]["x"], after[n]["y"]),
                                 (before[n]["x"], before[n]["y"]))

    def test_名前は文字の一覧でだけ受ける(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        for bad in ("lblItem1", 3, None, [1, 2], {"a": 1}):
            with self.subTest(names=bad):
                self.post("/api/layout/arrange", {"names": bad, "op": "top"},
                          expect=400)

    def test_知らない並べ方は断る(self) -> None:
        self.post("/api/layout/edit", {"on": True})
        self.post("/api/layout/arrange", {"names": self.names(), "op": "center"},
                  expect=400)

    def test_図からはみ出さない(self) -> None:
        """枠に収めるのは**動かすときと同じ道**。右に詰めても外へ出ない。"""
        state = self.get()
        self.post("/api/layout/edit", {"on": True})
        names = [s["name"] for s in state["shelves"]]      # 全部を1列に
        after = self.post("/api/layout/arrange", {"names": names, "op": "pack_row"})
        width = after["width"]              # 無ければここで落ちる(素通りさせない)
        for s in after["shelves"]:
            with self.subTest(name=s["name"]):
                self.assertGreaterEqual(s["x"], 0)
                self.assertLessEqual(s["x"] + s["w"], width + 1e-6)
