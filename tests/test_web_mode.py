"""モードの切替と、権限による登録の切り分け

【何を守っているか】
守りは2段で、**目的が違うので両方要る**。

1. 権限が無い端末には、そのモードの画面もAPIも**登録しない**(404)。
   守るのは「誰か」── 以前は「どのショートカットを押したか」でしか
   分かれておらず、同じPCの別の人が資材のポートを開けば通っていた
2. 権限があっても、いま別のモードで見ていれば**断る**(403)。
   こちらは誤操作の防止

**404 は「無い」、403 は「今はできない」。** 混ぜると、権限を足せば
直るのか、モードを切り替えれば直るのかが利用者に分からない。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402

_isolation.ensure_isolated()

from packaging_tool import access_control as ac  # noqa: E402
from packaging_tool import modes  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ"
TOKEN = "test-token"

BOTH = ("mode:field", "mode:material")
FIELD_ONLY = ("mode:field",)
MATERIAL_ONLY = ("mode:material",)


@unittest.skipUnless(HAS_WEB, _SKIP)
class ModeTestCase(unittest.TestCase):
    def make(self, mode: str, *codes: str):
        from app import create_app
        app = create_app(mode, token=TOKEN, port=8791,
                         grant=ac.grant_of(*codes))
        app.config["TESTING"] = True
        app.config["READY"] = True
        return app

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}


class StartupTests(ModeTestCase):
    def test_権限があれば指定したモードで開く(self) -> None:
        self.assertEqual(self.make("material", *BOTH).config["MODE"],
                         modes.MATERIAL)

    def test_権限が無ければ現場へ落として理由を残す(self) -> None:
        """**起動そのものは失敗させない。**

        失敗させると、権限を直す画面(設定)にも辿り着けなくなる。
        """
        app = self.make("material", *FIELD_ONLY)
        self.assertEqual(app.config["MODE"], modes.FIELD)
        self.assertIn("権限", app.config["MODE_NOTE"])

    def test_旧名でも起動できる(self) -> None:
        """現場に配ってあるショートカットは `warehouse` で書かれている。"""
        self.assertEqual(self.make("warehouse", *BOTH).config["MODE"],
                         modes.MATERIAL)

    def test_知らないモードは例外(self) -> None:
        with self.assertRaises(ValueError):
            self.make("けんさ", *BOTH)


class RegistrationTests(ModeTestCase):
    def rules(self, app) -> set:
        return {r.rule for r in app.url_map.iter_rules()}

    def test_現場の権限が無ければ現場の画面は無い(self) -> None:
        rules = self.rules(self.make("material", *MATERIAL_ONLY))
        for path in ("/selection", "/layout", "/log"):
            with self.subTest(path=path):
                self.assertNotIn(path, rules)

    def test_資材のレールにある画面は資材だけの端末にもある(self) -> None:
        """**レールに出ている画面は必ず開ける。** 以前は資材だけの端末で
        「ロット検索」を押すと404だった(現場の画面と一緒に、現場モードの
        権限でだけ登録していたため)。"""
        from app import shell
        rules = self.rules(self.make("material", *MATERIAL_ONLY))
        for item in shell.nav_items(modes.MATERIAL):
            with self.subTest(path=item.url):
                self.assertIn(item.url, rules)

    def test_資材だけの端末でロット検索が開ける(self) -> None:
        app = self.make("material", *MATERIAL_ONLY)
        res = app.test_client().get("/lot", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        # 資材展開は出さない(資材モードで資材選択へ行く道は無い)
        self.assertNotIn('id="expand"', res.get_data(as_text=True))

    def test_資材の権限が無ければ確認の操作は404(self) -> None:
        """以前はURLごと登録しなかった(起動時の権限で決めていた)。

        いまは常に登録し、`material_only.before_request` が要求のたびに
        権限を確かめる ── 権限を持たない端末には見える結果は変わらない
        (404)。ただし**マスタ管理で権限を足せば、開き直さずにその場の
        プロセスでも使えるようになる**(`ResyncOnSwitchTests` 参照)。
        """
        app = self.make("field", *FIELD_ONLY)
        res = app.test_client().post(
            "/api/warehouse/confirm", json={}, headers=self.auth())
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.get_json()["error"]["code"], "not_found")

    def test_両方持っていれば両方ある(self) -> None:
        """モードを切り替えられるので、いま資材モードで見ていても
        現場のURLは要る(レールに出すかどうかはモードが決める)。"""
        rules = self.rules(self.make("material", *BOTH))
        self.assertIn("/lot", rules)
        self.assertIn("/api/warehouse/confirm", rules)

    def test_設定はどのモードでも開ける(self) -> None:
        """「なぜこのモードしか選べないのか」を確かめる場所。"""
        for codes in (FIELD_ONLY, MATERIAL_ONLY, BOTH):
            with self.subTest(codes=codes):
                self.assertIn("/settings", self.rules(self.make("field", *codes)))


class SwitchTests(ModeTestCase):
    def test_権限のあるモードへ切り替えられる(self) -> None:
        app = self.make("field", *BOTH)
        body = app.test_client().post("/api/mode", json={"mode": "material"},
                                      headers=self.auth()).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(app.config["MODE"], modes.MATERIAL)

    def test_行き先も返す(self) -> None:
        """切り替えると出す画面が変わる。いまの画面が切替先に
        無いことがある(資材モードに資材選択は無い)。"""
        app = self.make("field", *BOTH)
        body = app.test_client().post("/api/mode", json={"mode": "material"},
                                      headers=self.auth()).get_json()
        self.assertEqual(body["next"], "/warehouse")

    def test_権限の無いモードへは切り替えられない(self) -> None:
        """画面には出さないが、要求を直接投げられても通らないようにする
        (設計書 §1 の6番)。"""
        app = self.make("field", *FIELD_ONLY)
        res = app.test_client().post("/api/mode", json={"mode": "material"},
                                     headers=self.auth())
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "not_allowed")
        self.assertEqual(app.config["MODE"], modes.FIELD)

    def test_知らないモードは400(self) -> None:
        """入力の形が違う。**サーバの状態は動いていない。**"""
        app = self.make("field", *BOTH)
        res = app.test_client().post("/api/mode", json={"mode": "けんさ"},
                                     headers=self.auth())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(app.config["MODE"], modes.FIELD)

    def test_同じモードなら何もしない(self) -> None:
        app = self.make("field", *BOTH)
        body = app.test_client().post("/api/mode", json={"mode": "field"},
                                      headers=self.auth()).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["next"], "")

    def test_旧名でも切り替えられる(self) -> None:
        app = self.make("field", *BOTH)
        app.test_client().post("/api/mode", json={"mode": "warehouse"},
                               headers=self.auth())
        self.assertEqual(app.config["MODE"], modes.MATERIAL)

    def test_トークンが要る(self) -> None:
        app = self.make("field", *BOTH)
        self.assertEqual(
            app.test_client().post("/api/mode", json={"mode": "material"}).status_code,
            401)


class RibbonTests(ModeTestCase):
    def html(self, app) -> str:
        return app.test_client().get("/settings").get_data(as_text=True)

    def test_持っているモードが2つなら切替を出す(self) -> None:
        html = self.html(self.make("field", *BOTH))
        self.assertIn('class="modeswitch"', html)
        self.assertIn('data-mode="material"', html)

    def test_1つしか無ければ切替を出さない(self) -> None:
        """押せない選択肢を並べても、できないことが増えたようにしか
        見えない。"""
        html = self.html(self.make("field", *FIELD_ONLY))
        self.assertNotIn('class="modeswitch"', html)
        self.assertIn("現場モード", html)

    def test_いまのモードが分かる(self) -> None:
        """どれが今かの判断もサーバが持つ(画面は写すだけ)。"""
        import re
        html = self.html(self.make("material", *BOTH))
        pressed = {
            m.group(1) for m in re.finditer(
                r'data-mode="(\w+)"[^>]*?aria-pressed="true"', html, re.S)}
        self.assertEqual(pressed, {"material"})

    def test_タブのアイコンがモードで変わる(self) -> None:
        field = self.html(self.make("field", *BOTH))
        material = self.html(self.make("material", *BOTH))
        self.assertNotEqual(
            field[field.index('rel="icon"'):field.index('rel="icon"') + 400],
            material[material.index('rel="icon"'):material.index('rel="icon"') + 400])


class RoleChipLinkTests(ModeTestCase):
    """モードを1つしか持たない端末では、理由をホバーだけに頼らない。

    以前は `title` 属性(ホバーでしか読めない)だけだったため、
    「モード選択できないけど何をしたらできるの?」と聞かれていた
    (現場の声)。理由があるときは押せば設定画面の説明へ飛べるようにする。
    """

    def html(self, app) -> str:
        return app.test_client().get("/settings").get_data(as_text=True)

    def test_理由があるときはリンクになる(self) -> None:
        app = self.make("field", *FIELD_ONLY)
        app.config["MODE_NOTE"] = "アクセス権限 に登録がありません"
        html = self.html(app)
        self.assertIn('<a class="role-chip', html)
        self.assertIn('href="/settings?tab=status"', html)

    def test_理由が無くてもリンクになる(self) -> None:
        """1つしか使えるモードが無いのは、既定へ落ちたときだけではない。

        正しく1つだけ許可されている(現場ではよくある)ときも、
        「他のモードはどう増やすか」を知りたいのは同じなので、
        理由(mode_note)が空でも常にリンクにする。
        """
        html = self.html(self.make("field", *FIELD_ONLY))
        self.assertIn('<a class="role-chip', html)
        self.assertNotIn('<span class="role-chip', html)


class SettingsSectionTests(ModeTestCase):
    def test_設定画面に権限の節が出る(self) -> None:
        """**なぜこのモードしか選べないのか**を利用者が自分で確かめられる。"""
        html = self.html_of(self.make("field", *FIELD_ONLY))
        self.assertIn("この端末の権限", html)
        self.assertIn("使えるモード", html)

    def html_of(self, app) -> str:
        return app.test_client().get("/settings").get_data(as_text=True)


@unittest.skipUnless(HAS_WEB, _SKIP)
class ResyncOnSwitchTests(unittest.TestCase):
    """切替を断る前に、アクセス権限だけ取り込み元から読み直す(実DB経路)。

    上の `SwitchTests` は `grant=` で権限を固定して試験しており、
    そこでは実際のDB読み直しは働かない(試験の狙いどおり)。ここは
    **固定せず**、本物の `startup_grant()` / `current_grant()` の経路を
    通す ── 現場の声「マスタには正しい行が入っているのに切り替わらない」
    を再現し、直っていることを確かめる。
    """

    def setUp(self) -> None:
        import tempfile

        from packaging_tool import access_control as ac
        from packaging_tool import config, data_sync, db

        self.dir = Path(tempfile.mkdtemp(prefix="resync_web_"))
        self.local_db = self.dir / "local.db"
        self.src = self.dir / "梱包資材マスタ.sqlite3"

        # 手元: スキーマだけ当てて、アクセス権限は空のまま
        # (「取り込んだつもりが手元へ追いついていない」状態を再現)
        with db.connect(self.local_db) as conn:
            db.apply_schema(conn)

        # 取り込み元: 正しい2行が**最初から**入っている
        src = sqlite3.connect(self.src)
        src.execute(
            'CREATE TABLE "アクセス権限" '
            '("ログインID" TEXT, "PC名" TEXT, "権限" TEXT,'
            ' "有効" INTEGER, "備考" TEXT)')
        identity = ac.current_identity()
        for permission in ("mode:field", "mode:material"):
            src.execute(
                'INSERT INTO "アクセス権限" VALUES (?,?,?,1,"")',
                (identity.login_id, identity.pc_name, permission))
        src.commit()
        src.close()

        self._saved_db_path = config.DB_PATH
        config.DB_PATH = self.local_db
        self._saved_find = data_sync.find_material_db
        data_sync.find_material_db = lambda directory=None: self.src
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        from packaging_tool import config, data_sync
        config.DB_PATH = self._saved_db_path
        data_sync.find_material_db = self._saved_find

    def make(self):
        from app import create_app
        app = create_app("field", token=TOKEN, port=8792)   # grant を渡さない
        app.config["TESTING"] = True
        app.config["READY"] = True
        return app

    def test_手元が遅れていても読み直して切り替わる(self) -> None:
        app = self.make()
        # 起動時点では、この端末の手元はまだアクセス権限が空
        self.assertEqual(app.config["MODE"], modes.FIELD)

        res = app.test_client().post(
            "/api/mode", json={"mode": "material"},
            headers={"X-Tool-Token": TOKEN})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertTrue(res.get_json()["ok"])
        self.assertEqual(app.config["MODE"], modes.MATERIAL)

    def test_取り込み元にも本当に無ければ403のまま(self) -> None:
        """読み直しは同期のずれを直すだけで、無い権限は作らない。"""
        src = sqlite3.connect(self.src)
        src.execute('DELETE FROM "アクセス権限" WHERE 権限 = "mode:material"')
        src.commit()
        src.close()

        app = self.make()
        res = app.test_client().post(
            "/api/mode", json={"mode": "material"},
            headers={"X-Tool-Token": TOKEN})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(app.config["MODE"], modes.FIELD)


class MaterialOnlyWithoutRestartTests(unittest.TestCase):
    """起動後に資材モードの権限を足したら、**開き直さずに**確認・取消が

    使えるようになるか。

    現場の声:「マスタ管理でアクセス権限に mode:material を足して
    モードを切り替えたのに、確認ボタンを押すと失敗する」。原因は、
    確認・取消のエンドポイント(`warehouse.material_only`)が**起動時の
    権限だけ**で登録するかどうかを決めていたこと。モード切替のボタンは
    ページを読み込み直すだけで、Pythonのサーバプロセス自体は再起動しない
    ため、起動時に権限が無ければ登録されず、あとから権限を足しても
    404のままだった。

    ここでは「起動時は権限が無い」→「取り込み元にだけ mode:material の
    行を足す」→「サーバプロセスは同じまま、モードを切り替えて確認操作を
    呼ぶ」という、まさにその手順を再現する。
    """

    def setUp(self) -> None:
        import tempfile

        from packaging_tool import access_control as ac
        from packaging_tool import config, data_sync, db

        self.dir = Path(tempfile.mkdtemp(prefix="norestart_web_"))
        self.local_db = self.dir / "local.db"
        self.src = self.dir / "梱包資材マスタ.sqlite3"

        with db.connect(self.local_db) as conn:
            db.apply_schema(conn)

        # 取り込み元: 起動時点では現場モードの権限しか無い
        src = sqlite3.connect(self.src)
        src.execute(
            'CREATE TABLE "アクセス権限" '
            '("ログインID" TEXT, "PC名" TEXT, "権限" TEXT,'
            ' "有効" INTEGER, "備考" TEXT)')
        self.identity = ac.current_identity()
        src.execute(
            'INSERT INTO "アクセス権限" VALUES (?,?,?,1,"")',
            (self.identity.login_id, self.identity.pc_name, "mode:field"))
        src.commit()
        src.close()

        self._saved_db_path = config.DB_PATH
        config.DB_PATH = self.local_db
        self._saved_find = data_sync.find_material_db
        data_sync.find_material_db = lambda directory=None: self.src
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        from packaging_tool import config, data_sync
        config.DB_PATH = self._saved_db_path
        data_sync.find_material_db = self._saved_find

    def test_起動後に権限を足しても開き直さず確認できる(self) -> None:
        from app import create_app

        # 起動: この時点ではまだ mode:material は無い
        app = create_app("field", token=TOKEN, port=8793)  # grantを渡さない
        app.config["TESTING"] = True
        app.config["READY"] = True
        client = app.test_client()
        headers = {"X-Tool-Token": TOKEN}

        # 起動直後は確認できない(まだ権限が無いので404)
        res0 = client.post("/api/warehouse/confirm", json={}, headers=headers)
        self.assertEqual(res0.status_code, 404)

        # 取り込み元にだけ mode:material を足す(マスタ管理からの追加を
        # 模す ── `master_admin.add_row` は取り込み元へ直接書く)
        src = sqlite3.connect(self.src)
        src.execute(
            'INSERT INTO "アクセス権限" VALUES (?,?,?,1,"")',
            (self.identity.login_id, self.identity.pc_name, "mode:material"))
        src.commit()
        src.close()

        # モードを切り替える(プロセスは同じまま。ページの読み込み直し
        # に相当する操作はここでは行わない)
        res1 = client.post("/api/mode", json={"mode": "material"},
                           headers=headers)
        self.assertEqual(res1.status_code, 200, res1.get_data(as_text=True))
        self.assertEqual(app.config["MODE"], modes.MATERIAL)

        # 確認操作が、**サーバプロセスを再起動せずに**使えるようになって
        # いること。存在しない発注番号なので業務としては断られる
        # (422/400等)が、**「そのURLが無い」(404)にはならない**
        res2 = client.post("/api/warehouse/confirm",
                           json={"order_no": "存在しない"}, headers=headers)
        self.assertNotEqual(res2.status_code, 404,
                            res2.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_WEB, _SKIP)
class SwitchAppearsWithoutRestartTests(unittest.TestCase):
    """**閉じて開き直さなくても切り替えられること。**

    現場から繰り返し届いていた:「一回閉じないと現場資材モード
    切り替えれません」。切替ボタンは手元のDBの権限で出す/出さないを
    決めていたので、資材課が取り込み元へ行を足しても、動いている
    プロセスには増えなかった。直す仕組み(`resync`)はその出ていない
    ボタンの向こうにあった。
    """

    def setUp(self) -> None:
        import sqlite3
        import tempfile
        from pathlib import Path

        from packaging_tool import access_control, config, db, user_settings

        self.ac = access_control
        self.dir = Path(tempfile.mkdtemp(prefix="switch_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        self.ident = access_control.current_identity()

        self.saved = user_settings.get(config.KEY_MASTER_DB_DIR)
        user_settings.save(config.KEY_MASTER_DB_DIR, str(self.dir))
        self.addCleanup(user_settings.save, config.KEY_MASTER_DB_DIR,
                        self.saved or "")
        access_control.reset_screen_resync()
        self.addCleanup(access_control.reset_screen_resync)

        self.conn = db.get_connection()
        db.apply_schema(self.conn)
        self.conn.execute("DELETE FROM アクセス権限")   # 手元はまだ追いつかない
        self.conn.commit()
        self.addCleanup(self._clear_local)
        self._sqlite3 = sqlite3

    def _clear_local(self) -> None:
        self.conn.execute("DELETE FROM アクセス権限")
        self.conn.commit()

    def write_source(self, *permissions: str) -> None:
        """取り込み元(共有フォルダ)に資材課が書いた状態を作る。"""
        self.src.unlink(missing_ok=True)
        src = self._sqlite3.connect(self.src)
        src.execute('CREATE TABLE "アクセス権限" ("ログインID" TEXT,'
                    ' "PC名" TEXT, "権限" TEXT, "有効" INTEGER, "備考" TEXT)')
        for permission in permissions:
            src.execute('INSERT INTO "アクセス権限" VALUES (?,?,?,1,"")',
                        (self.ident.login_id, self.ident.pc_name, permission))
        src.commit()
        src.close()

    def open_app(self):
        from app import create_app
        app = create_app("field", token=TOKEN, port=8792)
        app.config["TESTING"] = True
        app.config["READY"] = True
        return app.test_client()

    def test_開き直さずに切替が出る(self) -> None:
        self.write_source("mode:field", "mode:material")
        client = self.open_app()                 # 起動時、手元は空のまま
        html = client.get("/lot", headers={"X-Tool-Token": TOKEN}
                          ).get_data(as_text=True)
        self.assertIn("modeswitch", html,
                      "取り込み元に権限があるのに切替が出ていません")

    def test_そのまま切り替えられる(self) -> None:
        self.write_source("mode:field", "mode:material")
        client = self.open_app()
        client.get("/lot", headers={"X-Tool-Token": TOKEN})
        res = client.post("/api/mode", json={"mode": "material"},
                          headers={"X-Tool-Token": TOKEN})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["mode"], modes.MATERIAL)

    def test_権限が本当に無ければ出さない(self) -> None:
        """**出しすぎない。** 権限を設けた意味が消える。"""
        self.write_source("mode:field")
        client = self.open_app()
        html = client.get("/lot", headers={"X-Tool-Token": TOKEN}
                          ).get_data(as_text=True)
        self.assertNotIn("modeswitch", html)


@unittest.skipUnless(HAS_WEB, _SKIP)
class RibbonFollowsModesTests(unittest.TestCase):
    """**切り替えられるようになったら、触らなくても帯が変わる。**

    帯のモード切替は画面を出すときにしか作られなかった。権限があとから
    増えても(起動時の取り込みが終わった・資材課が行を足した)、画面を
    移るまで「現場?」のまま ── 現場の声:「触ることで 現場・資材 に
    なった」。見張り(`/api/health`)が使えるモードを返し、帯の
    `data-modes` と食い違ったら帯だけ描き直す。
    """

    def setUp(self) -> None:
        from app import create_app
        from packaging_tool import screen_lock
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        self.app = create_app("field", token=TOKEN, port=8793,
                              grant=ac.grant_of(*BOTH))
        self.app.config["TESTING"] = True
        self.app.config["READY"] = True
        self.client = self.app.test_client()

    def health(self, **headers):
        return self.client.get("/api/health", headers=headers).get_json()

    def test_画面からの問い合わせには使えるモードを返す(self) -> None:
        from packaging_tool import screen_lock
        body = self.health(**{screen_lock.HEADER: "abc"})
        self.assertEqual(sorted(body["modes"]), sorted([modes.FIELD, modes.MATERIAL]))

    def test_画面以外には返さない(self) -> None:
        """起動の判定や待機画面は権限を要らない。**DBを開かせない。**"""
        self.assertIsNone(self.health()["modes"])

    def test_準備中は返さない(self) -> None:
        """「分からない」を「変わった」と取り違えさせない。"""
        from packaging_tool import screen_lock
        self.app.config["READY"] = False
        self.assertIsNone(self.health(**{screen_lock.HEADER: "abc"})["modes"])

    def test_帯が描いたときのモードを持っている(self) -> None:
        """見張りが比べる相手。**サーバの答えと同じ作り方**で書く。"""
        import re
        html = self.client.get("/warehouse", headers=self.auth()).get_data(as_text=True)
        found = re.search(r'class="ribbon__end" data-modes="([^"]*)"', html)
        self.assertIsNotNone(found, "帯に data-modes がありません")
        self.assertEqual(sorted(found.group(1).split(",")),
                         sorted([modes.FIELD, modes.MATERIAL]))

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}


class RibbonRedrawWiringTests(unittest.TestCase):
    """画面の側が繋がっているか。**外すと黙って元に戻る**ところ。"""

    def js(self, name: str) -> str:
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        return (root / "app" / "static" / "js" / name).read_text(encoding="utf-8")

    def test_見張りが食い違いを見て描き直す(self) -> None:
        src = self.js("health.js")
        self.assertIn("body.modes", src)
        self.assertIn("refreshShell", src)

    def test_描き直しはこのタブの番号を付ける(self) -> None:
        """**付けないと、描き直したタブ自身が締め出される。**

        番号無しで取りに行くと、サーバは「新しいタブが開いた」と読んで
        新しい番号を振る。VER2.77.0 でタブの取り合いを止めたとき、画面の
        移動(`go`)には付けたが描き直し(`refreshShell`)に付け忘れ、
        マスタ管理でアクセス権限を保存した直後に「このタブは操作できません」
        が出ていた。**自動で描き直すようにしたので、放っておけば15秒ごと
        に締め出される**ところだった。
        """
        src = self.js("nav.js")
        body = src[src.index("export async function refreshShell"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("screen.HEADER", body)

    def test_タブに戻ったらすぐ確かめる(self) -> None:
        src = self.js("health.js")
        start = src.index('addEventListener("visibilitychange"')
        self.assertIn("beat()", src[start:start + 400])
