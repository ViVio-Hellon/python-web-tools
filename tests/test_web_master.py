"""マスタ管理の API のテスト (`app/routes/master.py`)。

【ここで守りたいこと】
1. **断りの種別が HTTP に正しく写る**(設計.md §1)。
   400=入力の形 / 403=許されていない / 409=先を越された / 422=業務の断り
2. **断ったときも画面ぜんぶを返す。**「断られた」と「画面が古いまま」を
   同時に起こさない
3. **見るのはどのモードでも通る。** 中身を確かめられることと、
   書き換えられることは別の話
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import (access_control, config, master_admin,  # noqa: E402
                            modes, user_settings)

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"


@unittest.skipUnless(HAS_WEB, _SKIP)
class MasterApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests import _web
        from tests.test_master_admin import make_source
        from app.routes import master as master_routes

        self.dir = Path(tempfile.mkdtemp(prefix="webmaster_"))
        self.src = make_source(self.dir)
        # **本物の共有フォルダを見に行かせない**
        self._saved = user_settings.get(config.KEY_MASTER_DB_DIR)
        user_settings.save(config.KEY_MASTER_DB_DIR, str(self.dir))
        self.addCleanup(user_settings.save, config.KEY_MASTER_DB_DIR,
                        self._saved or "")

        self.conn = _web.bind_db(self, master_routes)
        self.client = _web.make_client("field", port=8713)
        self.auth = _web.auth()
        # マスタを書くには必ず管理者パスワードが要る。書く試験は通した状態から
        from packaging_tool import selection_session
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        selection_session.get_session(self.conn).admin = True

    # -- 道具 -------------------------------------------------------
    def browse(self, **params):
        res = self.client.get("/api/master/browse", query_string=params,
                              headers=self.auth)
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def post(self, path: str, body: dict):
        return self.client.post(f"/api/master/{path}", json=body,
                                headers=self.auth)

    def key(self, table: str = "PalletMaster"):
        return self.browse(table=table)["page"]["rows"][0][master_admin.ROW_KEY]

    def only_field(self) -> None:
        """この端末を現場モードだけにする。"""
        from packaging_tool import selection_session
        # 権限を見る試験。パスワードは通していない状態から(通すなら試験が自分で)
        selection_session.get_session(self.conn).admin = False
        identity = access_control.current_identity()
        self.conn.execute(
            'INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
            " VALUES (?,'',?,1,'')",
            (identity.login_id, access_control.mode_permission(modes.FIELD)))
        self.conn.commit()

    def only_material(self) -> None:
        """この端末を資材モードだけにする(管理者パスワードは別)。"""
        from packaging_tool import selection_session
        # 権限を見る試験。パスワードは通していない状態から(通すなら試験が自分で)
        selection_session.get_session(self.conn).admin = False
        identity = access_control.current_identity()
        self.conn.execute(
            'INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
            " VALUES (?,'',?,1,'')",
            (identity.login_id, access_control.mode_permission(modes.MATERIAL)))
        self.conn.commit()

    # -- 見る -------------------------------------------------------
    def test_表と中身が返る(self) -> None:
        state = self.browse()
        names = [t["table"] for t in state["tables"]]
        self.assertIn("PalletMaster", names)
        self.assertEqual(state["table"], "PalletMaster")
        self.assertEqual(state["page"]["total"], 1)

    def test_表を指定して引ける(self) -> None:
        state = self.browse(table="BoardMaster")
        self.assertEqual(state["table"], "BoardMaster")
        self.assertEqual(state["page"]["total"], 2)

    def test_絞り込める(self) -> None:
        state = self.browse(table="BoardMaster", q="IK")
        self.assertEqual(state["page"]["shown"], 1)

    def test_知らない表を指定したら先頭に戻す(self) -> None:
        """空の面を出して「自分で選べ」とするより、いちばん触る表を開く。"""
        self.assertEqual(self.browse(table="無い表")["table"], "PalletMaster")

    def test_見るのは現場モードでも通る(self) -> None:
        self.only_field()
        state = self.browse()
        self.assertFalse(state["can_edit"])
        self.assertEqual(state["page"]["total"], 1)
        # 押す前に理由が読める
        self.assertIn("資材", state["edit_why"])

    def test_資材モードだけではパスワードが無いと直せない(self) -> None:
        """「mode:materialを付与してるからってどのマスタもいじれたら困る」
        という現場の判断で、資材モードに加えて管理者パスワードも要る。"""
        self.only_material()
        state = self.browse()
        self.assertFalse(state["can_edit"])
        self.assertIn("管理者パスワード", state["edit_why"])

    def test_資材モードとパスワードの両方で直せる(self) -> None:
        from packaging_tool import selection_session
        self.addCleanup(selection_session.reset_session)
        self.only_material()
        selection_session.get_session(self.conn).admin = True
        state = self.browse()
        self.assertTrue(state["can_edit"], state["edit_why"])

    def test_アクセス権限はパスワード無しでは直せない(self) -> None:
        self.only_field()
        state = self.browse(table=access_control.TABLE)
        self.assertFalse(state["can_edit"])
        self.assertIn("管理者パスワード", state["edit_why"])

    def test_アクセス権限はパスワードだけで直せる_モードは問わない(self) -> None:
        """書き間違えて全員から mode:material を消しても、直す手立てが残る。
        「パスワードが分かれば直せる」を最優先にするので、モードは問わない。"""
        from packaging_tool import selection_session
        self.addCleanup(selection_session.reset_session)
        self.only_field()
        selection_session.get_session(self.conn).admin = True
        state = self.browse(table=access_control.TABLE)
        self.assertTrue(state["can_edit"], state["edit_why"])

    def test_パスワードだけでは他の表は直せない(self) -> None:
        from packaging_tool import selection_session
        self.addCleanup(selection_session.reset_session)
        self.only_field()
        selection_session.get_session(self.conn).admin = True
        state = self.browse(table="PalletMaster")
        self.assertFalse(state["can_edit"])

    # -- 直す -------------------------------------------------------
    def test_直すと画面ぜんぶが返る(self) -> None:
        res = self.post("row/save", {"table": "PalletMaster",
                                     "key": self.key(), "values": {"位置": "B-2"}})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["page"]["rows"][0]["位置"], "B-2")
        self.assertIn("直しました", body["message"])

    def test_足せる(self) -> None:
        res = self.post("row/add", {
            "table": "BoardMaster",
            "values": {"ボード幅": "1500", "ボード丈": "3000",
                       "ボードタイプ": "プロテックボード"}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["page"]["total"], 3)

    def test_消せる(self) -> None:
        res = self.post("row/delete", {"table": "BoardMaster",
                                       "key": self.key("BoardMaster")})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["page"]["total"], 1)

    def test_絞り込みは書いたあとも残る(self) -> None:
        """直すたびに一覧が全件に戻ると、次の行を探し直すことになる。"""
        res = self.post("row/save", {"table": "BoardMaster", "q": "IK",
                                     "key": self.key("BoardMaster") + 1,
                                     "values": {"データラベル": "x"}})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["query"], "IK")
        self.assertEqual(body["page"]["shown"], 1)

    # -- 断り -------------------------------------------------------
    def test_入力の形が違えば400(self) -> None:
        res = self.post("row/save", {"table": "PalletMaster",
                                     "key": self.key(), "values": {"幅": "ひろい"}})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_BAD_VALUE)

    def test_許されていなければ403(self) -> None:
        self.only_field()
        res = self.post("row/save", {"table": "PalletMaster",
                                     "key": self.key(), "values": {"位置": "B"}})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_NOT_ALLOWED)

    def test_先を越されていれば409(self) -> None:
        res = self.post("row/save", {"table": "PalletMaster", "key": 9999,
                                     "values": {"位置": "B"}})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_NO_ROW)

    def test_業務として断るなら422(self) -> None:
        res = self.post("row/save", {"table": "PalletPatterns", "key": 1,
                                     "values": {"使用回数": "9"}})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_NOT_EDITABLE)

    def test_断ったときも画面ぜんぶが入っている(self) -> None:
        """「断られた」と「画面が古いまま」を同時に起こさない。"""
        res = self.post("row/save", {"table": "PalletMaster",
                                     "key": self.key(), "values": {"幅": "ひろい"}})
        body = res.get_json()
        self.assertIn("page", body)
        self.assertEqual(body["page"]["rows"][0]["幅"], 1100)
        self.assertEqual(body["message"], "")

    def test_断ったときは何も動いていない(self) -> None:
        self.post("row/save", {"table": "PalletMaster",
                               "key": self.key(), "values": {"幅": "ひろい"}})
        conn = sqlite3.connect(self.src)
        try:
            self.assertEqual(
                conn.execute("SELECT 幅 FROM PalletMaster").fetchone()[0], 1100)
        finally:
            conn.close()


@unittest.skipUnless(HAS_WEB, _SKIP)
class ColumnMismatchTests(unittest.TestCase):
    """アクセス権限の表はあるが、列名が想定と違うときの画面。

    現場の声:「1行足す」を押しても入力欄が1つも出てこない。
    """

    def setUp(self) -> None:
        from tests import _web
        from app.routes import master as master_routes

        self.dir = Path(tempfile.mkdtemp(prefix="webmaster_mismatch_"))
        self.src = self.dir / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(self.src)
        conn.executescript("""
            CREATE TABLE PalletMaster (
                管理番号 INTEGER PRIMARY KEY, 幅 INTEGER, 丈 INTEGER);
            -- 列名がこのツールの想定(ログインID/PC名/権限/有効/備考)と
            -- 1つも一致しない ── 資材課側で別の付け方をした状況を再現
            CREATE TABLE アクセス権限 (
                id INTEGER PRIMARY KEY, LoginID TEXT, PCName TEXT,
                Permission TEXT);
        """)
        conn.execute("INSERT INTO PalletMaster (幅,丈) VALUES (1100,2000)")
        conn.commit()
        conn.close()

        self._saved = user_settings.get(config.KEY_MASTER_DB_DIR)
        user_settings.save(config.KEY_MASTER_DB_DIR, str(self.dir))
        self.addCleanup(user_settings.save, config.KEY_MASTER_DB_DIR,
                        self._saved or "")

        self.conn = _web.bind_db(self, master_routes)
        self.client = _web.make_client("field", port=8714)
        self.auth = _web.auth()

    def browse(self, **params):
        res = self.client.get("/api/master/browse", query_string=params,
                              headers=self.auth)
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def test_打ち込める欄が無いことを画面が言葉にする(self) -> None:
        state = self.browse(table="アクセス権限")
        # 表そのものは「まだありません」ではない(取り込み元に実在する)
        self.assertFalse(state["page"]["missing"])
        # 直せる表として案内はされるが、入力欄は1つも無い
        self.assertEqual(state["columns"], [])
        # なぜ打ち込めないのかが、ここで言葉になっている
        self.assertIn("列名が想定と違う", state["page"]["why"])
        self.assertIn("ログインID", state["page"]["why"])

    def test_列名が一致していれば理由は出ない(self) -> None:
        state = self.browse(table="PalletMaster")
        self.assertEqual(state["page"]["why"], "")


@unittest.skipUnless(HAS_WEB, _SKIP)
class SettingsPageTests(unittest.TestCase):
    """設定画面の面として載っていること。"""

    def setUp(self) -> None:
        from tests import _web
        from app.routes import master as master_routes
        from app.routes import settings as settings_routes

        conn = _web.bind_db(self, settings_routes)
        _web.bind_db(self, master_routes, conn)
        self.client = _web.make_client("field", port=8713)
        self.auth = _web.auth()

    def test_面がある(self) -> None:
        html = self.client.get("/settings", headers=self.auth).get_data(as_text=True)
        self.assertIn('id="tab-master"', html)
        self.assertIn("マスタ管理", html)

    def test_最初の描画では共有フォルダに触らない(self) -> None:
        """設定画面は毎日開く。19テーブルを数え終わるのを待たせない。

        面を開くまで読まないので、最初の描画に行は入っていない。
        """
        from packaging_tool import source_db

        called = []
        original = source_db.table_counts
        source_db.table_counts = lambda path: called.append(path) or {}
        self.addCleanup(setattr, source_db, "table_counts", original)
        self.client.get("/settings", headers=self.auth)
        self.assertEqual(called, [])

    def test_資材モードでも開ける(self) -> None:
        from tests import _web

        grant = access_control.grant_of(
            access_control.mode_permission(modes.MATERIAL))
        client = _web.make_client("material", port=8723, grant=grant)
        html = client.get("/settings", headers=self.auth).get_data(as_text=True)
        self.assertIn('id="tab-master"', html)



@unittest.skipUnless(HAS_WEB, _SKIP)
class CreateTableApiTests(MasterApiTests):
    """取り込み元にまだ無い表を、画面から作る。

    アクセス権限が無いあいだは**どの端末も現場モードだけ**になる。
    「モードが切り替わらない」としか見えないので、作る手立てが画面に
    要る(`master_admin.creatable_tables`)。
    """

    def setUp(self) -> None:
        super().setUp()
        from tests.test_master_admin import source_without
        source_without(self.src, "アクセス権限")

    def test_無い表も一覧に出る(self) -> None:
        found = {t["table"]: t for t in self.browse()["tables"]}
        self.assertTrue(found["アクセス権限"]["missing"])

    def test_開いても故障扱いにしない(self) -> None:
        page = self.browse(table="アクセス権限")["page"]
        self.assertTrue(page["missing"])
        self.assertEqual(page["error"], "")
        self.assertIn("現場モードだけ", page["why"])

    def test_作れる(self) -> None:
        res = self.post("table/create", {"table": "アクセス権限"})
        self.assertEqual(res.status_code, 200)
        page = res.get_json()["page"]
        # 作ったあとは**そのまま行を足せる状態**で返る
        self.assertFalse(page["missing"])
        self.assertTrue(page["editable"])
        self.assertEqual(page["total"], 0)

    def test_もうあれば409(self) -> None:
        """先を越された。`row/save` の「その行はもう無い」と同じ種類。"""
        self.post("table/create", {"table": "アクセス権限"})
        res = self.post("table/create", {"table": "アクセス権限"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_ALREADY)

    def test_上流の表は422(self) -> None:
        res = self.post("table/create", {"table": "PalletMaster"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_NOT_CREATABLE)

    def test_権限が無ければ403(self) -> None:
        self.only_field()
        res = self.post("table/create", {"table": "アクセス権限"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_NOT_ALLOWED)

    def test_断ったときも画面ぜんぶを返す(self) -> None:
        self.only_field()
        body = self.post("table/create", {"table": "アクセス権限"}).get_json()
        self.assertIn("tables", body)
        self.assertIn("page", body)

    def test_表が無いまま行を足そうとすると422で表を作れと言う(self) -> None:
        """現場の声:「1行足す」を押すと『入れる値がありません』と出る。

        画面(`master.js`)は表が無いあいだ `openRow(null)` 自体を止めるが、
        直接APIを叩かれた場合のために、サーバ側(`_ready`)でも確かめる。
        断りの文言は「値が無い」ではなく「表がまだ無い」でなければならない
        ── 本当の原因と違う理由を返すと、直しようがない。
        """
        res = self.post("row/add", {"table": "アクセス権限",
                                    "values": {"ログインID": "x", "PC名": "y",
                                              "権限": "mode:field",
                                              "有効": "1", "備考": ""}})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], master_admin.REFUSE_NOT_CREATABLE)
        self.assertNotEqual(body["error"]["message"], "入れる値がありません。")
        self.assertIn("まだ取り込み元にありません", body["error"]["message"])


@unittest.skipUnless(HAS_WEB, _SKIP)
class RebuildTableApiTests(MasterApiTests):
    """表はあるが列名が想定と違い、1つも打ち込めない表を作り直す。

    `CreateTableApiTests` の対になる話。あちらは「表が無い」(よく
    ある)、こちらは「表はあるが列名が違う」(稀だが、起きると
    sqlite3 のファイルを直接直す手段が無いので詰む)。
    """

    def setUp(self) -> None:
        super().setUp()
        from tests.test_master_admin import source_with_mismatched_columns
        source_with_mismatched_columns(self.src, "アクセス権限")

    def test_列名が違う表は一覧でも案内される(self) -> None:
        page = self.browse(table="アクセス権限")["page"]
        self.assertFalse(page["missing"])       # 「まだ無い」ではない
        self.assertTrue(page["rebuildable"])
        self.assertIn("列名が想定と違う", page["why"])

    def test_打ち込める欄が無い(self) -> None:
        state = self.browse(table="アクセス権限")
        self.assertEqual(state["columns"], [])

    def test_列名が違うまま行を足そうとすると422で作り直せと言う(self) -> None:
        res = self.post("row/add", {"table": "アクセス権限",
                                    "values": {"ログインID": "x", "PC名": "y",
                                              "権限": "mode:field",
                                              "有効": "1", "備考": ""}})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], master_admin.REFUSE_NOT_CREATABLE)
        self.assertNotEqual(body["error"]["message"], "入れる値がありません。")
        self.assertIn("列名が想定と違う", body["error"]["message"])

    def test_作り直せる(self) -> None:
        res = self.post("table/rebuild", {"table": "アクセス権限"})
        self.assertEqual(res.status_code, 200)
        page = res.get_json()["page"]
        # 作り直したあとは**そのまま行を足せる状態**で返る
        self.assertFalse(page["rebuildable"])
        self.assertTrue(page["editable"])
        self.assertEqual(page["total"], 0)
        self.assertEqual(len(res.get_json()["columns"]), 5)

    def test_元の表は消さず退避する(self) -> None:
        """**中身を失わない。**"""
        conn = sqlite3.connect(self.src)
        conn.execute("INSERT INTO アクセス権限 (LoginID, Permission) "
                    "VALUES ('old_user', 'old_perm')")
        conn.commit()
        conn.close()

        self.post("table/rebuild", {"table": "アクセス権限"})

        conn = sqlite3.connect(self.src)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        backups = [n for n in names if n.startswith("アクセス権限_旧")]
        self.assertEqual(len(backups), 1)

    def test_列名が一致していれば対象外(self) -> None:
        """通常のケース(想定どおりの表)ではボタンを出さない。"""
        from tests.test_master_admin import source_without
        source_without(self.src, "アクセス権限")
        self.post("table/create", {"table": "アクセス権限"})
        page = self.browse(table="アクセス権限")["page"]
        self.assertFalse(page["rebuildable"])

    def test_表がまだ無ければ422(self) -> None:
        from tests.test_master_admin import source_without
        source_without(self.src, "アクセス権限")
        res = self.post("table/rebuild", {"table": "アクセス権限"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_NOT_CREATABLE)

    def test_上流の表は422(self) -> None:
        res = self.post("table/rebuild", {"table": "PalletMaster"})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_NOT_CREATABLE)

    def test_権限が無ければ403(self) -> None:
        self.only_field()
        res = self.post("table/rebuild", {"table": "アクセス権限"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"],
                         master_admin.REFUSE_NOT_ALLOWED)

    def test_断ったときも画面ぜんぶを返す(self) -> None:
        self.only_field()
        body = self.post("table/rebuild", {"table": "アクセス権限"}).get_json()
        self.assertIn("tables", body)
        self.assertIn("page", body)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
