"""選定ログ画面と、全画面共通の外枠

Web版で最初に作る画面。書き込みが無いぶんリスクが低いので、
ここで通信・差分取得・外枠の型を固める。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import access_control, modes  # noqa: E402

# 権限は明示して渡す。開発機のマスタの中身で結果が変わってはいけない
_ALL = access_control.grant_of("mode:field", "mode:material")
from packaging_tool.user_log import UserLog, get_user_log  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"
TOKEN = "test-token-abc123"  # 実物と同じくASCII


@unittest.skipUnless(HAS_WEB, _SKIP)
class WebTestCase(unittest.TestCase):
    mode = "field"

    def setUp(self) -> None:
        from app import create_app
        app = create_app(self.mode, token=TOKEN, port=8713, grant=_ALL)
        app.config["TESTING"] = True
        self.app = app
        self.client = app.test_client()
        # 共有の選定ログを毎回まっさらにする(テスト間で漏れないように)
        self.log = get_user_log()
        self.log.clear()
        # `clear()` は連番を戻さない(戻すと取得側が取りこぼす)。
        # テストは絶対値ではなく、この時点からの相対で見る
        self.base_seq = self.log.last_seq

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}

    def html(self, path: str) -> str:
        res = self.client.get(path)
        self.assertEqual(res.status_code, 200, f"{path} が開けません")
        return res.get_data(as_text=True)


# ==================================================================
# 外枠
# ==================================================================
class ShellTests(WebTestCase):
    def test_ステータスリボンに4項目出る(self) -> None:
        """作業記憶に置けるまとまりは4±1。覚えさせないための常時表示。"""
        html = self.html("/log")
        for label in ("Lot", "パレット", "製品", "拠点"):
            self.assertIn(label, html)

    def test_レールが作業順に並ぶ(self) -> None:
        """並びは tkinter 版のタブ順、さらに遡ると VBA `mpMain.Pages`。

        番号は装飾ではなく、順序が実在するから振っている。
        入れ替えは仕様変更にあたる。
        """
        html = self.html("/log")
        # `<title>` など nav の外にある同じ語を拾わないよう、
        # レールの中だけを切り出してから見る
        start = html.index('<nav class="rail"')
        rail = html[start:html.index("</nav>", start)]
        order = ["ロット検索", "資材選択", "簡易在庫", "倉庫連携",
                 "棚検索", "選定ログ", "設定"]
        positions = [rail.index(label) for label in order]
        self.assertEqual(positions, sorted(positions),
                         "レールの並びが作業順になっていません")

    def test_現在の画面が示される(self) -> None:
        self.assertIn('aria-current="page"', self.html("/log"))

    def test_接続断の帯がある(self) -> None:
        """基盤仕様書 2.9。黙って古い表示を出し続けない。"""
        html = self.html("/log")
        self.assertIn("バックエンドに接続できません", html)
        self.assertIn("再接続", html)

    def test_終了ボタンがある(self) -> None:
        self.assertIn("終了", self.html("/log"))

    def test_トークンが画面に渡る(self) -> None:
        self.assertIn(TOKEN, self.html("/log"))

    def test_タブのアイコンが出る(self) -> None:
        """現場と倉庫を同時に開くので、タブで見分けられるようにする。

        タブの表題は幅が足りずに切れる。色と1文字なら切れない。
        """
        self.assertIn('rel="icon"', self.html("/log"))

    def test_現場の権限が無ければ選定ログは存在しない(self) -> None:
        """資材課は資材の選定をしないので、この画面自体が無い。

        隠すのではなく登録しない。資材専用APIを権限の無い端末に
        置かないのと同じ考え方。
        """
        from app import create_app
        from packaging_tool import access_control
        app = create_app("material", token=TOKEN, port=8723,
                         grant=access_control.grant_of("mode:material"))
        app.config["TESTING"] = True
        client = app.test_client()
        self.assertEqual(client.get("/log").status_code, 404)
        self.assertEqual(
            client.get("/api/log", headers={"X-Tool-Token": TOKEN}).status_code, 404)


@unittest.skipUnless(HAS_WEB, _SKIP)
class PendingScreenTests(unittest.TestCase):
    """レールに出ている以上、押したら何か出る。

    まだ無いことと壊れていることは違う。404 を返すと、押した人には
    「壊れている」としか見えない。
    """

    def _client(self, mode: str = "field"):
        from app import create_app
        app = create_app(mode, token=TOKEN, port=8713, grant=_ALL)
        app.config["TESTING"] = True
        app.config["READY"] = True
        return app.test_client()

    def test_レールの行き先がすべて開く(self) -> None:
        from app.shell import nav_items
        for mode in modes.KEYS:
            client = self._client(mode)
            for item in nav_items(mode):
                with self.subTest(mode=mode, screen=item.label):
                    self.assertEqual(client.get(item.url).status_code, 200,
                                     f"{item.url} が開けません")

    def _with_pending(self):
        """**準備中の画面を1つ仕立てる**。

        Phase 7 で7画面すべてが揃い、実在する準備中の画面が無くなった。
        だからといってこの仕組みを試さなくなると、次に画面を足したときに
        「押した先が404」が復活する ── 仕組みそのものは残すので、
        レールに**架空の画面**を1つ足して試す。

        実在する画面を借りないのは、業務のBlueprintが先に登録されて
        準備中の経路を上書きするため(`pending.register` の意図どおり)。
        """
        from app import shell
        from app.routes import pending as pending_routes

        key, url = "__demo", "/__demo"
        nav = shell.FIELD_NAV + ((key, "見本", url, "説明の見本"),)
        screens = dict(shell.PENDING_SCREENS)
        screens[key] = ("Phase X", "この画面がすることの説明。", "棚検索タブ")

        originals = {"FIELD_NAV": shell.FIELD_NAV,
                     "PENDING_SCREENS": shell.PENDING_SCREENS}
        shell.FIELD_NAV = nav
        shell.PENDING_SCREENS = screens
        # `pending` は import 時に束縛した名前を見るので、そちらも差し替える
        original_pending = pending_routes.PENDING_SCREENS
        pending_routes.PENDING_SCREENS = screens
        self.addCleanup(lambda: setattr(pending_routes, "PENDING_SCREENS",
                                        original_pending))
        for name, value in originals.items():
            self.addCleanup(lambda n=name, v=value: setattr(shell, n, v))
        return key

    def _pending(self):
        from app.shell import pending_items
        pending = pending_items(modes.FIELD)
        self.assertTrue(pending, "準備中の画面が1つも無い")
        return pending

    def test_準備中だと分かる(self) -> None:
        """作り込んだ画面と同じ見た目にすると、動くと思って待ってしまう。

        画面ができるたびに直さなくてよいよう、**準備中のものを総なめ**する
        (ここに個別の画面名を書いていたため、資材選択が出来た日に落ちた)。
        """
        self._with_pending()
        client = self._client()
        for item in self._pending():
            with self.subTest(screen=item.label):
                html = client.get(item.url).get_data(as_text=True)
                self.assertIn("準備中", html)
                self.assertIn("この画面はまだありません", html)

    def test_いつ作るかを書く(self) -> None:
        self._with_pending()
        from app.shell import PENDING_SCREENS
        client = self._client()
        for item in self._pending():
            phase, _, _ = PENDING_SCREENS[item.key]
            with self.subTest(screen=item.label):
                self.assertIn(phase, client.get(item.url).get_data(as_text=True))

    def test_それまで何を使うかを書く(self) -> None:
        """「まだありません」だけでは、その人の作業が止まる。

        画面ができるたびに直さなくてよいよう、**準備中のものを総なめ**する。
        """
        self._with_pending()
        from app.shell import PENDING_SCREENS
        client = self._client()
        for item in self._pending():
            _, _, meanwhile = PENDING_SCREENS[item.key]
            with self.subTest(screen=item.label):
                html = client.get(item.url).get_data(as_text=True)
                self.assertIn(meanwhile, html)

    def test_レールでも行く前に分かる(self) -> None:
        """情報の匂い。開いてから知るのでは遅い。"""
        self._with_pending()
        from app.shell import nav_items
        pending = [i for i in nav_items(modes.FIELD) if not i.ready]
        self.assertTrue(pending)
        for item in pending:
            with self.subTest(screen=item.label):
                self.assertEqual(item.badge, "準備中")

    def test_できている画面には準備中を出さない(self) -> None:
        from app.shell import nav_items, READY_SCREENS
        for item in nav_items(modes.FIELD):
            if item.key in READY_SCREENS:
                with self.subTest(screen=item.label):
                    self.assertTrue(item.ready)
                    self.assertNotEqual(item.badge, "準備中")

    def test_起動直後は準備中の画面に着地しない(self) -> None:
        """立ち上げた先が案内では、アプリが動いたのか分からない。"""
        from app.shell import READY_SCREENS, home_url, nav_items
        from app import create_app
        for mode in modes.KEYS:
            app = create_app(mode, token=TOKEN, port=8713, grant=_ALL)
            with app.app_context():
                url = home_url(mode)
            keys = {i.url: i.key for i in nav_items(mode)}
            with self.subTest(mode=mode):
                self.assertIn(keys.get(url), READY_SCREENS)

    def test_説明のある画面と準備中の画面が一致する(self) -> None:
        """説明を書き忘れると、行き先だけあって中身が空になる。"""
        from app.shell import PENDING_SCREENS, pending_items
        for mode in modes.KEYS:
            for item in pending_items(mode):
                with self.subTest(mode=mode, screen=item.label):
                    self.assertIn(item.key, PENDING_SCREENS)


class NavTests(unittest.TestCase):
    """レールの組み立て(画面なしで確認できる)。"""

    def test_現場は7画面(self) -> None:
        from app.shell import nav_items
        self.assertEqual(len(nav_items(modes.FIELD)), 7)

    def test_資材は絞られる(self) -> None:
        """資材は現場より狭い。**発注を出す前の段(簡易在庫・棚検索・
        選定ログ)は持たない。**

        ロット検索は持つ ── 届いた発注のLotを確かめるため(現場の指摘:
        「倉庫モードの時に送られてきたデータに添付しているLOT情報を
        現場モードのように展開できる必要があります」)。

        **資材選択は持たない。** 資材展開はここには出さないので
        (現場の指摘:「倉庫モードは資材展開して資材選択へが出ていては
        ダメです」)、辿り着く道の無い画面になる。並べると押した人には
        壊れて見える。
        """
        from app.shell import nav_items
        keys = [i.key for i in nav_items(modes.MATERIAL)]
        self.assertLess(len(keys), 7)
        self.assertEqual(keys, ["warehouse", "lot", "settings"])
        for absent in ("selection", "inventory", "layout", "log"):
            self.assertNotIn(absent, keys)

    def test_モードごとにタブのアイコンが違う(self) -> None:
        from app.shell import favicon
        field = favicon(modes.FIELD)
        material = favicon(modes.MATERIAL)
        self.assertNotEqual(field, material)
        self.assertIn("現", field)
        self.assertIn("資", material)

    def test_アイコンはURLに入れられる(self) -> None:
        """`data:` URL に埋めるので、そのままでは使えない文字を含む。"""
        from urllib.parse import quote
        from app.shell import favicon
        svg = favicon(modes.FIELD)
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("#", quote(svg, safe="/"))

    def test_バッジを付けられる(self) -> None:
        """行く前に「その先に何があるか」を示す(情報の匂い)。"""
        from app.shell import nav_items
        items = nav_items(modes.FIELD, {"log": ("12", "todo")})
        log_item = next(i for i in items if i.key == "log")
        self.assertEqual(log_item.badge, "12")


# ==================================================================
# 選定ログ
# ==================================================================
class LogPageTests(WebTestCase):
    def test_開ける(self) -> None:
        self.assertIn("選定ログ", self.html("/log"))

    def test_初回は全行が埋め込まれる(self) -> None:
        """最初の描画をJSに任せない。開いた瞬間に読める。"""
        self.log.log("PASS1 スカシ T2 を採用")
        self.assertIn("PASS1 スカシ T2 を採用", self.html("/log"))

    def test_件数がレールのバッジに出る(self) -> None:
        for i in range(3):
            self.log.log(f"行{i}")
        self.assertIn(">3<", self.html("/log"))

    def test_空でも案内が出る(self) -> None:
        self.assertIn("まだログがありません", self.html("/log"))


class LogApiTests(WebTestCase):
    def test_トークンが要る(self) -> None:
        self.assertEqual(self.client.get("/api/log").status_code, 401)

    def test_全行取れる(self) -> None:
        for text in ("あ", "い", "う"):
            self.log.log(text)
        body = self.client.get(f"/api/log?since={self.base_seq}",
                               headers=self.auth()).get_json()
        self.assertEqual([r["text"] for r in body["rows"]], ["あ", "い", "う"])
        self.assertEqual(body["last_seq"], self.base_seq + 3)

    def test_差分だけ取れる(self) -> None:
        for text in ("あ", "い", "う"):
            self.log.log(text)
        body = self.client.get(f"/api/log?since={self.base_seq + 2}",
                               headers=self.auth()).get_json()
        self.assertEqual([r["text"] for r in body["rows"]], ["う"])

    def test_新しい行が無ければ空(self) -> None:
        self.log.log("あ")
        body = self.client.get(f"/api/log?since={self.base_seq + 1}",
                               headers=self.auth()).get_json()
        self.assertEqual(body["rows"], [])

    def test_sinceが壊れていても落ちない(self) -> None:
        self.log.log("あ")
        body = self.client.get("/api/log?since=なにか", headers=self.auth()).get_json()
        self.assertGreaterEqual(len(body["rows"]), 1)

    def test_取りこぼしたらresetが立つ(self) -> None:
        """古い行が上限で捨てられたら、画面は全部描き直す。

        取りこぼしたまま追記を続けると、表示と実体がずれたことに
        誰も気づけない。
        """
        from app.routes import log as log_routes
        small = UserLog(max_entries=2)
        for text in ("あ", "い", "う", "え"):
            small.log(text)
        original = log_routes.get_user_log
        log_routes.get_user_log = lambda: small
        try:
            body = self.client.get("/api/log?since=1", headers=self.auth()).get_json()
        finally:
            log_routes.get_user_log = original
        self.assertTrue(body["reset"], "連番が跳んでいるのに reset が立っていません")

    def test_消去できる(self) -> None:
        self.log.log("あ")
        body = self.client.post("/api/log/clear", headers=self.auth()).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.log), 0)

    def test_消去しても連番は戻らない(self) -> None:
        """戻すと、消去前の番号を持つ画面が取りこぼす。"""
        self.log.log("あ")
        before = self.log.last_seq
        self.client.post("/api/log/clear", headers=self.auth())
        self.log.log("い")
        body = self.client.get(f"/api/log?since={before}",
                               headers=self.auth()).get_json()
        self.assertEqual([r["text"] for r in body["rows"]], ["い"])


class LogClassifyTests(unittest.TestCase):
    """行の種別。**文言は変えず**、絞り込みのために分類するだけ。"""

    def _kind(self, text: str, emphasis: bool = False) -> str:
        from packaging_tool.user_log import LogEntry
        from app.routes.log import classify
        return classify(LogEntry(text=text, emphasis=emphasis, seq=1))

    def test_除外(self) -> None:
        for text in ("×除外: 1150×2700 範囲外(L:2560〜2690)",
                     "除外: 桁数偶数(8)",
                     "1280×3150 属性不一致(業界=一般)"):
            with self.subTest(text=text):
                self.assertEqual(self._kind(text), "exclude")

    def test_決定(self) -> None:
        for text in ("PASS1 スカシ T2 を採用", "下用(PASS2): 320x985 2枚"):
            with self.subTest(text=text):
                self.assertEqual(self._kind(text), "decide")

    def test_警告(self) -> None:
        for text in ("資材マスタ未取り込み", "資材が引けません", "疲労度マップ取得失敗"):
            with self.subTest(text=text):
                self.assertEqual(self._kind(text), "warn")

    def test_分からなければ情報に倒す(self) -> None:
        """勝手に警告扱いしない。現場が「異常だ」と誤解する。"""
        self.assertEqual(self._kind("=== パレット検索 開始 ==="), "info")

    def test_文言は変えない(self) -> None:
        """分類しても本文はそのまま。現場が覚えている言葉を保つ。"""
        from packaging_tool.user_log import LogEntry
        from app.routes.log import _serialize
        text = "×除外: 1150×2700 範囲外(L:2560〜2690)"
        self.assertEqual(_serialize(LogEntry(text=text, seq=1))["text"], text)


if __name__ == "__main__":
    unittest.main()
