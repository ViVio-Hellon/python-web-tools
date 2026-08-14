"""設定画面のテスト

`packaging_tool/presenters/settings.py` と `app/routes/settings.py` を見る。

【ここで守りたいこと】
取り込みがうまくいかない原因はほぼ決まっている ── ファイルが無い /
テーブル名が違う / 列名が違う。状態を1つの文字列にして出すと、
どこが問題なのかが読み取れない。
1項目ずつ良し悪しを付けて返すこと、そして**直し方まで書く**ことを固定する。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import config, jobs  # noqa: E402
from packaging_tool.presenters import settings as presenter  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"
TOKEN = "test-token-abc123"


class Result:
    def __init__(self, ok: bool = True, summary: str = "3件") -> None:
        self.ok = ok
        self._summary = summary

    def summary(self) -> str:
        return self._summary


class PresenterTestCase(unittest.TestCase):
    """設定を一時フォルダへ逃がして、本物の共有フォルダを見に行かせない。"""

    def setUp(self) -> None:
        from packaging_tool import user_settings
        self.dir = Path(tempfile.mkdtemp(prefix="data_test_"))
        self._saved = {
            key: user_settings.get(key)
            for key in (config.KEY_MASTER_DB_DIR, config.KEY_LOT_DB_DIR,
                        config.KEY_KANBAN_DB_DIR, config.KEY_AUTO_IMPORT)
        }
        user_settings.save(config.KEY_MASTER_DB_DIR, str(self.dir))
        user_settings.save(config.KEY_LOT_DB_DIR, str(self.dir))
        user_settings.save(config.KEY_KANBAN_DB_DIR, str(self.dir))
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        from packaging_tool import user_settings
        for key, value in self._saved.items():
            user_settings.save(key, value if value is not None else "")


# ==================================================================
# いまの状態
# ==================================================================
class StatusTests(PresenterTestCase):
    def test_何も無ければ足りないと出る(self) -> None:
        view = presenter.build()
        self.assertEqual(view.level, presenter.NG)
        self.assertFalse(view.can_import)

    def test_押す前に理由を出す(self) -> None:
        """押しても何も起きないボタンを黙って置かない。"""
        view = presenter.build()
        self.assertIn("見つかりません", view.import_reason)

    def test_探した場所まで書く(self) -> None:
        """「見つかりません」で終わらせない。どこを見たかが分かれば直せる。"""
        view = presenter.build()
        details = [c.detail for s in view.sections for c in s.checks]
        self.assertTrue(any(str(self.dir) in d for d in details),
                        "探した場所が出ていません")

    def test_項目ごとに良し悪しが付く(self) -> None:
        """全部同じ黒い等幅文字にしない。どこが問題かを色と記号で示す。"""
        view = presenter.build()
        levels = {c.level for s in view.sections for c in s.checks}
        self.assertIn(presenter.NG, levels)
        self.assertIn(presenter.INFO, levels)

    def test_まとまりの重さは一番重いものになる(self) -> None:
        section = presenter.Section("試験", [
            presenter.Check("a", "", presenter.OK),
            presenter.Check("b", "", presenter.NG),
        ])
        self.assertEqual(section.level, presenter.NG)

    def test_仕掛台帳は3ファイルぶん出る(self) -> None:
        view = presenter.build()
        lot = next(s for s in view.sections if s.title == "仕掛台帳")
        labels = [c.label for c in lot.checks]
        for filename in config.LOT_DB_FILES.values():
            self.assertIn(filename, labels)

    def test_1つでも見つかれば取り込みは試せる(self) -> None:
        """3ファイルのうち1つでも読めるなら、そこまでは取り込む。"""
        (self.dir / config.LOT_DB_FILES["仕掛ロット"]).write_bytes(b"")
        view = presenter.build()
        self.assertTrue(view.can_import)

    def test_取り込み元が無ければ先に言う(self) -> None:
        """ファイルが1つも見つからないなら、押させる前に言う。

        「置き場所は設定できています」だけ出して押させると、
        押した先で初めて失敗する。
        """
        view = presenter.build()
        self.assertFalse(view.can_import)
        self.assertIn("見つかりません", view.import_reason)

    def test_中身がsqlite3でなければ足りない扱い(self) -> None:
        """拡張子が合っていても中身が別物ということはある。

        名前を変えただけの別形式を置かれると、ファイルは在るのに
        1件も取り込めない ── その状態を「問題なし」と出さない。
        """
        (self.dir / config.MATERIAL_DB_NAME).write_text("sqlite3 ではない",
                                                        encoding="utf-8")
        master = next(s for s in presenter.build().sections
                      if s.title == "梱包資材マスタ")
        body = next(c for c in master.checks if c.label == "中身")
        self.assertEqual(body.level, presenter.NG)

    def test_この端末の事実を出す(self) -> None:
        """「どのDBを見ているのか」を聞かれるたびに調べるのは無駄。"""
        terminal = next(s for s in presenter.build().sections
                        if s.title == "この端末")
        labels = [c.label for c in terminal.checks]
        self.assertIn("手元のデータベース", labels)
        self.assertIn("読み取り方式", labels)

    def test_色だけで良し悪しを伝えない(self) -> None:
        """工場は照明も見え方もまちまち(§3.8)。文言を必ず添える。"""
        for level in (presenter.OK, presenter.WARN, presenter.NG, presenter.INFO):
            with self.subTest(level=level):
                self.assertIn(level, presenter.LEVEL_LABEL)
                self.assertTrue(presenter.LEVEL_LABEL[level])

    def test_取り込めない理由を出す(self) -> None:
        """押せないボタンには理由を添える。押した先で初めて失敗させない。"""
        view = presenter.build()
        if not view.can_import:
            self.assertTrue(view.import_reason)

    def test_辞書にできる(self) -> None:
        import json
        json.dumps(presenter.to_dict(presenter.build()), ensure_ascii=False)

    def test_文言はサーバが持つ(self) -> None:
        """画面側で「問題なし / 要確認」を書き分けない(設計書 §6.1)。"""
        self.assertEqual(presenter.to_dict(presenter.build())["level_label"],
                         presenter.LEVEL_LABEL)


class KanbanSectionTests(PresenterTestCase):
    """看板(在庫薄警告)マスタは梱包資材マスタとは別ファイル。

    以前は Form状態管理・看板_* の8テーブルも「梱包資材マスタ」の面が
    数えていたので、梱包資材マスタには本来入っていないこの8つが毎回
    「不足」に見えていた。別の面(「看板マスタ」)で数えるようにし、
    梱包資材マスタの面からは除外する。
    """

    def _make_kanban_file(self, tables) -> None:
        import sqlite3
        conn = sqlite3.connect(self.dir / config.KANBAN_DB_NAME)
        for table in tables:
            conn.execute(f'CREATE TABLE "{table}" (dummy TEXT)')
        conn.commit()
        conn.close()

    def test_看板マスタの面がある(self) -> None:
        view = presenter.build()
        self.assertTrue(any(s.title == "看板マスタ" for s in view.sections))

    def test_看板マスタが無くてもNGにしない(self) -> None:
        """機能自体は任意ではないが、分けたばかりで置き場所が未設定の
        端末が多いうちは NG(直すべき不足)に見せない。"""
        section = next(s for s in presenter.build().sections
                      if s.title == "看板マスタ")
        self.assertNotEqual(section.level, presenter.NG)

    def test_梱包資材マスタの面は看板テーブルを数えない(self) -> None:
        """8テーブルぶんの「不足」が、梱包資材マスタ側には出ない。"""
        (self.dir / config.MATERIAL_DB_NAME).write_text("", encoding="utf-8")
        import sqlite3
        conn = sqlite3.connect(self.dir / config.MATERIAL_DB_NAME)
        conn.execute("CREATE TABLE BoardMaster (ボード幅 INTEGER)")
        conn.commit()
        conn.close()

        from packaging_tool import import_specs
        master = next(s for s in presenter.build().sections
                      if s.title == "梱包資材マスタ")
        table_check = next(c for c in master.checks if c.label == "テーブル")
        for table in import_specs.KANBAN_TABLES:
            self.assertNotIn(table, table_check.detail,
                             f"{table} が梱包資材マスタの不足に出ています")

    def test_看板マスタのファイルが見つかる(self) -> None:
        from packaging_tool import import_specs
        self._make_kanban_file(import_specs.KANBAN_TABLES)
        section = next(s for s in presenter.build().sections
                      if s.title == "看板マスタ")
        file_check = next(c for c in section.checks if c.label == "ファイル")
        self.assertEqual(file_check.level, presenter.OK)
        table_check = next(c for c in section.checks if c.label == "テーブル")
        self.assertEqual(table_check.level, presenter.OK)

    def test_看板マスタの中で足りないテーブルを言う(self) -> None:
        from packaging_tool import import_specs
        self._make_kanban_file(["Form状態管理"])  # 7つ足りない状態にする
        section = next(s for s in presenter.build().sections
                      if s.title == "看板マスタ")
        table_check = next(c for c in section.checks if c.label == "テーブル")
        self.assertEqual(table_check.level, presenter.WARN)
        self.assertIn("看板_AIM", table_check.detail)


class SettingsTests(PresenterTestCase):
    def test_保存すると次から使われる(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="data_test2_"))
        presenter.save(master_dir=str(other), lot_dir=str(other), auto_import=False,
                       password=config.ADMIN_PASSWORD)
        view = presenter.build()
        self.assertEqual(view.master_dir, str(other))
        self.assertFalse(view.auto_import)

    def test_空にすると既定に戻る(self) -> None:
        presenter.save(master_dir="", lot_dir="", auto_import=True,
                       password=config.ADMIN_PASSWORD)
        self.assertEqual(presenter.build().master_dir, str(config.DEFAULT_MASTER_DB_DIR))

    def test_前後の空白は落とす(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="data_test3_"))
        presenter.save(master_dir=f"  {other}  ", lot_dir="", auto_import=True,
                       password=config.ADMIN_PASSWORD)
        self.assertEqual(presenter.build().master_dir, str(other))


class FixRouteTests(PresenterTestCase):
    """**問題を見せた面から、直せる面へ繋ぐ。**

    「見つかりません」と書いてある面には直す手立てが無い。行き先を
    出さないと、利用者はタブを探し回ることになる。
    """

    def section(self, title: str):
        return next(s for s in presenter.build().sections if s.title == title)

    def test_見つからないなら置き場所へ繋ぐ(self) -> None:
        found = self.section("梱包資材マスタ")
        self.assertEqual(found.action, presenter.FIX_SOURCE)
        self.assertEqual(found.action[1], "source")

    def test_仕掛台帳も繋ぐ(self) -> None:
        self.assertEqual(self.section("仕掛台帳").action, presenter.FIX_SOURCE)

    def test_行き先は実在する面(self) -> None:
        """消えた面へ繋ぐと、押しても何も起きない。"""
        for section in presenter.build().sections:
            if section.action[0]:
                with self.subTest(section=section.title):
                    self.assertIn(section.action[1], presenter.TAB_KEYS)

    def test_問題が無ければ出さない(self) -> None:
        """いつも出ていると、押す意味のある印にならない。"""
        self.assertEqual(self.section("この端末").action, ("", ""))

    def test_画面にも渡る(self) -> None:
        state = presenter.to_dict(presenter.build())
        found = next(s for s in state["sections"] if s["title"] == "梱包資材マスタ")
        self.assertEqual(found["action"],
                         {"label": presenter.FIX_SOURCE[0], "tab": "source"})
        end = next(s for s in state["sections"] if s["title"] == "この端末")
        self.assertIsNone(end["action"])


class AccessGuidanceTests(PresenterTestCase):
    """権限が既定へ落ちたとき、直し方が具体的に分かること(現場の声)。

    「アクセス権限に行を足してください」とだけ言われても、どこで
    (マスタ管理タブ)・誰が(資材モードを持つ人)・何を(自分の
    ログインID/PC名と、使いたいモードの権限コード)足せばよいかが
    分からないと、結局誰にも直せない。
    """

    def setUp(self) -> None:
        super().setUp()
        from packaging_tool import access_control, db
        self.access_control = access_control
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)

    def section(self):
        return next(s for s in presenter.build(self.conn).sections
                    if s.title == "この端末の権限")

    def test_マスタ管理タブへの案内が付く(self) -> None:
        section = self.section()
        self.assertEqual(section.action, presenter.FIX_ACCESS)
        self.assertEqual(section.action[1], "master")

    def test_パスワードの置き場所を常に案内する(self) -> None:
        """「いつもどこだっけって探しちゃう」という現場の声への対応。
        マスタを直すには資材モードに加えて管理者パスワードも要る
        (VER2.17.0)。入力欄はこの設定画面自体に移した(VER2.19.0)ので、
        置き場所をここに常に出す(値そのものは出さない)。"""
        check = next(c for c in self.section().checks
                    if c.label == "マスタを直すパスワードの入力欄")
        self.assertIn("マスタ編集の認証", check.value)

    def test_身元と権限コードの候補が具体的に入る(self) -> None:
        identity = self.access_control.current_identity()
        detail = next(c for c in self.section().checks
                      if c.label == "権限の出どころ").detail
        if identity.login_id:
            self.assertIn(identity.login_id, detail)
        self.assertIn(self.access_control.TABLE, detail)
        self.assertIn("mode:", detail)

    def test_資材選択の認証がここにも映る(self) -> None:
        """認証はプロセスに1つの状態(VER2.19.0)。どちらの画面で
        通しても、もう一方にそのまま出る。"""
        from packaging_tool import config, selection_session

        self.assertFalse(presenter.build(self.conn).admin_authenticated)
        session = selection_session.get_session(self.conn)
        try:
            session.authenticate(config.ADMIN_PASSWORD)
            self.assertTrue(presenter.build(self.conn).admin_authenticated)
        finally:
            selection_session._session = None

    def test_全モードを持っていれば案内を出さない(self) -> None:
        """他に取れていないモードが無ければ、案内することが無い。"""
        from packaging_tool import modes
        identity = self.access_control.current_identity()
        for mode in modes.ALL:
            self.conn.execute(
                'INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
                " VALUES (?,'',?,1,'')",
                (identity.login_id, self.access_control.mode_permission(mode.key)))
        self.conn.commit()
        self.assertEqual(self.section().action, ("", ""))

    def test_一部のモードしか無ければ他の増やし方を案内する(self) -> None:
        """正しく1つだけ許可されている(現場ではよくある)ときも、
        「他のモードはどう増やすか」を案内する ── 既定へ落ちた
        (grant.reason がある)ときだけの話ではない。"""
        from packaging_tool import modes
        identity = self.access_control.current_identity()
        self.conn.execute(
            'INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
            " VALUES (?,'',?,1,'')",
            (identity.login_id, self.access_control.mode_permission(modes.MATERIAL)))
        self.conn.commit()
        section = self.section()
        self.assertEqual(section.action, presenter.FIX_ACCESS)
        detail = next(c for c in section.checks if c.label == "他のモードを使うには").detail
        self.assertIn(self.access_control.mode_permission(modes.FIELD), detail)


class RestartNeededTests(PresenterTestCase):
    """**足した権限は、開き直すまで全部は効かない。**

    使えるモードは要求のたびに引き直すので切り替えは通る。ところが
    使える画面(登録するURL)は起動時の権限で決まっている。
    マスタ管理から権限を足せるようになって、ここを踏みやすくなった。
    """

    def setUp(self) -> None:
        super().setUp()
        from packaging_tool import access_control, db, modes
        self.access_control = access_control
        self.modes = modes
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)
        identity = access_control.current_identity()
        self.conn.execute(
            'INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
            " VALUES (?,'',?,1,'')",
            (identity.login_id, access_control.mode_permission(modes.MATERIAL)))
        self.conn.commit()

    def checks(self, startup_modes):
        section = next(s for s in presenter.build(self.conn, startup_modes).sections
                       if s.title == "この端末の権限")
        return {c.label: c for c in section.checks}

    def test_起動後に増えた権限は開き直しが要ると言う(self) -> None:
        found = self.checks((self.modes.FIELD,))
        self.assertIn("開き直しが要ります", found)
        self.assertEqual(found["開き直しが要ります"].level, presenter.WARN)
        self.assertIn("開き直す", found["開き直しが要ります"].detail)

    def test_起動時から持っていれば言わない(self) -> None:
        self.assertNotIn("開き直しが要ります", self.checks((self.modes.MATERIAL,)))

    def test_渡されなければ黙っている(self) -> None:
        """起動時の権限が分からない場面(試験・DBを開けない等)。"""
        self.assertNotIn("開き直しが要ります", self.checks(None))


class PathStyleTests(PresenterTestCase):
    """道の書き方は**絶対と相対の2通り**を受ける。"""

    def test_相対はアプリのフォルダから見る(self) -> None:
        """起点が「いまの作業フォルダ」だと、起動の仕方で行き先が変わる。"""
        presenter.save(master_dir="data/src", password=config.ADMIN_PASSWORD)
        self.assertEqual(config.master_db_dir(), config.BASE_DIR / "data" / "src")

    def test_絶対はそのまま(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="abs_"))
        presenter.save(master_dir=str(other), password=config.ADMIN_PASSWORD)
        self.assertEqual(config.master_db_dir(), other)

    def test_打った形のまま画面に戻す(self) -> None:
        """絶対に直して返すと、相対で書いたはずの字が消える。"""
        presenter.save(master_dir="data/src", password=config.ADMIN_PASSWORD)
        view = presenter.build()
        self.assertEqual(view.master_dir, "data/src")
        # **実際に見に行く先**も併せて出す
        self.assertEqual(view.master_dir_real,
                         str(config.BASE_DIR / "data" / "src"))

    def test_絶対のときは行き先を二重に出さない(self) -> None:
        """同じものが2行あると、違うものに見える。"""
        other = Path(tempfile.mkdtemp(prefix="abs2_"))
        presenter.save(master_dir=str(other), password=config.ADMIN_PASSWORD)
        state = presenter.to_dict(presenter.build())
        self.assertEqual(state["master_dir"], str(other))
        self.assertEqual(state["master_dir_real"], "")

    def test_仕掛台帳も同じ書き方(self) -> None:
        presenter.save(lot_dir="../共有/台帳", password=config.ADMIN_PASSWORD)
        self.assertEqual(config.lot_db_dir(),
                         config.BASE_DIR / ".." / "共有" / "台帳")

    def test_引用符を付けて貼っても通る(self) -> None:
        """エクスプローラの「パスのコピー」は `"` で囲んで返す。"""
        other = Path(tempfile.mkdtemp(prefix="quoted_"))
        presenter.save(master_dir=f'"{other}"', password=config.ADMIN_PASSWORD)
        self.assertEqual(config.master_db_dir(), other)


# ==================================================================
# 画面とAPI
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class DataWebTestCase(PresenterTestCase):
    role = "field"

    def setUp(self) -> None:
        super().setUp()
        from app import create_app
        # プロセス共通の記録を、テストごとにまっさらな一時ファイルへ逃がす
        self.registry = jobs.reset_registry(
            jobs.JobRegistry(store_path=self.dir / "jobs.json"))
        self.addCleanup(jobs.reset_registry, jobs.JobRegistry(
            store_path=self.dir / "unused.json"))

        app = create_app(self.role, token=TOKEN, port=8713)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}

    def wait_idle(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.registry.is_busy():
                return
            time.sleep(0.01)
        self.fail("処理が終わりませんでした")


class DataPageTests(DataWebTestCase):
    def test_開ける(self) -> None:
        res = self.client.get("/settings")
        self.assertEqual(res.status_code, 200)
        self.assertIn("取り込みと反映", res.get_data(as_text=True))

    def test_状態がJSの前に描かれる(self) -> None:
        """設計書 §3.2。JSが動く前でも読める。"""
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("仕掛台帳", html)
        self.assertIn(str(self.dir), html)

    def test_うまくいかないときだけ触るものを下に置く(self) -> None:
        """取り込み → 状態 → 取り込み元 → 動作 → よく使う条件 → 最近の結果。

        毎回押すものを先に、うまくいかないときだけ触るものを後ろに置く
        (ヒックの法則)。設定を1画面に集めた分、並びで迷わせない。

        面(タブ)に分けたので、**並びはタブの並び**が決める。中身の
        並びも同じにしておく ── 読み上げとタブ移動はDOMの順で回るため。
        """
        html = self.client.get("/settings").get_data(as_text=True)
        # `<head>` の中のスタイル(注釈に同じ語が出る)を拾わないよう、
        # 本体だけを切り出してから見る
        body = html[html.index('<div class="pane'):]

        bar = body[body.index('class="tabs__bar"'):body.index('class="tabs__panels"')]
        labels = ["取り込みと反映", "いまの状態", "取り込み元",
                  "動作", "よく使う条件", "最近の結果"]
        seen = [bar.index(f'class="tab__label">{name}<') for name in labels]
        self.assertEqual(seen, sorted(seen), "タブの並びが変わっています")

        # 見出しだけを見る(注釈にも同じ語が出るため)
        order = ["<h3>取り込みと反映</h3>", "<h3>取り込み元</h3>",
                 "<h3>動作</h3>", "<h3>よく使う条件</h3>", "<h3>最近の結果</h3>"]
        positions = [body.index(label) for label in order]
        self.assertEqual(positions, sorted(positions))

    def test_取り込めないならボタンを押させない(self) -> None:
        import re
        html = self.client.get("/settings").get_data(as_text=True)
        tag = re.search(r'<button[^>]*data-import="all"[^>]*>', html)
        self.assertIsNotNone(tag)
        self.assertIn("disabled", tag.group(0))

    def test_tabパラメータで面を名指しできる(self) -> None:
        """帯のモード表示(`/settings?tab=status`)が正しい面を開く。

        JSを待たずに最初の描画から `data-default="1"` が「いまの状態」の
        タブに付くこと ── JSが動く前に見えるHTMLだけでも合っている必要がある。
        """
        html = self.client.get("/settings?tab=status").get_data(as_text=True)
        bar = html[html.index('class="tabs__bar"'):html.index('class="tabs__panels"')]
        status_tag = bar[bar.index('data-key="status"'):bar.index('data-key="status"') + 120]
        self.assertIn('data-default="1"', status_tag)

    def test_知らないtabは既定へ落ちる(self) -> None:
        html = self.client.get("/settings?tab=nonsense").get_data(as_text=True)
        bar = html[html.index('class="tabs__bar"'):html.index('class="tabs__panels"')]
        run_tag = bar[bar.index('data-key="run"'):bar.index('data-key="run"') + 120]
        self.assertIn('data-default="1"', run_tag)

    def test_倉庫モードにもある(self) -> None:
        """倉庫スタッフも書き戻しをする。ここは両方に置く。"""
        from app import create_app
        app = create_app("warehouse", token=TOKEN, port=8723)
        app.config["TESTING"] = True
        self.assertEqual(app.test_client().get("/settings").status_code, 200)


class LaneTests(unittest.TestCase):
    """段のレーン。**全部並べない。失敗は必ず出す。**"""

    @staticmethod
    def steps(*specs) -> list[jobs.Step]:
        """(名前, 終わったか, 成功か) から段を作る。"""
        out = []
        for label, finished, ok in specs:
            out.append(jobs.Step(label=label, started_at=1.0,
                                 finished_at=2.0 if finished else 0.0, ok=ok))
        return out

    def test_段が1つなら並べない(self) -> None:
        """横棒1本で足りる。カード1枚を出しても情報は増えない。"""
        self.assertEqual(presenter.lane_steps(self.steps(("A", True, True))), [])

    def test_上限までしか並べない(self) -> None:
        many = self.steps(*[(f"T{i}", True, True) for i in range(19)])
        self.assertEqual(len(presenter.lane_steps(many)), presenter.LANE_LIMIT)

    def test_落とした分は数えて言う(self) -> None:
        many = self.steps(*[(f"T{i}", True, True) for i in range(19)])
        self.assertEqual(presenter.lane_note(many), "ほか 13 件は完了しました")

    def test_失敗した段は必ず出す(self) -> None:
        """先頭で転んだものが、新しい段に押し出されてはいけない。"""
        many = self.steps(("転んだ", True, False),
                          *[(f"T{i}", True, True) for i in range(18)])
        labels = [d["label"] for d in presenter.lane_steps(many)]
        self.assertIn("転んだ", labels)

    def test_いま走っている段も必ず出す(self) -> None:
        many = self.steps(*[(f"T{i}", True, True) for i in range(18)],
                          ("いま", False, True))
        shown = presenter.lane_steps(many)
        self.assertEqual(shown[-1]["label"], "いま")
        self.assertEqual(shown[-1]["state"], "run")

    def test_通し番号は元の並びのまま(self) -> None:
        """「19段のうちの17段目」と読めること。詰め直さない。"""
        many = self.steps(*[(f"T{i}", True, True) for i in range(19)])
        shown = presenter.lane_steps(many)
        self.assertEqual([d["no"] for d in shown], [14, 15, 16, 17, 18, 19])
        self.assertEqual({d["total"] for d in shown}, {19})

    def test_落としたほうに失敗があれば数を言う(self) -> None:
        many = self.steps(*[(f"転んだ{i}", True, False) for i in range(8)],
                          *[(f"T{i}", True, True) for i in range(4)])
        self.assertIn("2 件が失敗", presenter.lane_note(many))

    def test_見出しから進行形を落とす(self) -> None:
        """レーンには終わった段も並ぶ。「読み込み中」のままだと嘘になる。"""
        self.assertEqual(presenter.step_title("BoardMaster を読み込み中..."),
                         "BoardMaster")
        self.assertEqual(presenter.step_title("まとめて取り込みを準備しています"),
                         "まとめて取り込み")

    def test_落とすと何も残らない文はそのまま(self) -> None:
        self.assertEqual(presenter.step_title("準備しています"), "準備しています")


class JobApiTests(DataWebTestCase):
    def test_トークンが要る(self) -> None:
        for path in ("/api/jobs", "/api/settings/state"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_走っていなくても直近の結果を返す(self) -> None:
        """開き直したときに「さっきの取り込みはどうなったのか」が要る。"""
        self.registry.start("import", "取り込み", lambda _p: Result())
        self.wait_idle()
        body = self.client.get("/api/jobs", headers=self.auth()).get_json()
        self.assertEqual(body["running"], [])
        self.assertFalse(body["busy"])
        self.assertEqual(body["recent"][0]["label"], "取り込み")
        self.assertEqual(body["recent"][0]["state_label"], "完了")

    def test_取り込みを始められる(self) -> None:
        res = self.client.post("/api/settings/import", json={"target": "master"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.get_json()["job"]["label"], "マスタだけ")
        self.wait_idle()

    def test_知らない対象は断る(self) -> None:
        res = self.client.post("/api/settings/import", json={"target": "なにか"},
                               headers=self.auth())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], "bad_target")

    def test_二重に走らせない(self) -> None:
        """同じDBを2か所から書くと壊れる。"""
        release = threading.Event()
        self.registry.start("import", "まとめて取り込み", lambda _p: release.wait(3))
        try:
            res = self.client.post("/api/settings/import", json={"target": "all"},
                                   headers=self.auth())
            self.assertEqual(res.status_code, 409)
            body = res.get_json()
            self.assertEqual(body["error"]["code"], "busy")
            # 何が走っているかまで返す。「あとで」だけでは待ち時間が読めない
            self.assertEqual(body["job"]["label"], "まとめて取り込み")
        finally:
            release.set()
        self.wait_idle()

    def test_実行中は止めない(self) -> None:
        """取り込みの途中で落とすとDBが中途半端に残る(基盤仕様書 2.8)。"""
        release = threading.Event()
        self.registry.start("import", "まとめて取り込み", lambda _p: release.wait(3))
        try:
            res = self.client.post("/api/shutdown", json={},
                                   headers=self.auth())
            self.assertEqual(res.status_code, 409)
            body = res.get_json()
            self.assertFalse(body["stopped"])
            self.assertEqual(body["running"], ["まとめて取り込み"])
        finally:
            release.set()
        self.wait_idle()

    def test_失敗も結果として返る(self) -> None:
        def work(_p):
            raise RuntimeError("共有フォルダに届きません")

        self.registry.start("import", "取り込み", work)
        self.wait_idle()
        recent = self.client.get("/api/jobs", headers=self.auth()).get_json()["recent"]
        self.assertEqual(recent[0]["state_label"], "失敗")
        self.assertIn("共有フォルダ", recent[0]["error"])


class SettingsApiTests(DataWebTestCase):
    def test_保存すると新しい状態が返る(self) -> None:
        """「保存しました」だけだと、直ったのかどうかが分からない。"""
        other = Path(tempfile.mkdtemp(prefix="data_test4_"))
        (other / config.LOT_DB_FILES["仕掛ロット"]).write_bytes(b"")
        body = self.client.post("/api/settings/save", headers=self.auth(), json={
            "master_dir": str(other), "lot_dir": str(other), "auto_import": False,
            "password": config.ADMIN_PASSWORD,
        }).get_json()
        self.assertEqual(body["master_dir"], str(other))
        self.assertFalse(body["auto_import"])
        self.assertTrue(body["can_import"])

    def test_状態だけ取り直せる(self) -> None:
        body = self.client.get("/api/settings/state", headers=self.auth()).get_json()
        self.assertIn("sections", body)
        self.assertIn("level_label", body)



# ==================================================================
# 置き場所は管理者パスワードで守る
#
# 置き場所を変えると、このツールが読み書きする相手そのものが変わる。
# 取り込みは総入れ替えなので、間違った先を指したまま取り込むと手元の
# 中身が入れ替わり、書き戻しもそちらへ行く。押し間違いが**別のファイルを
# 書き換える**ところまで届く設定は、いまここだけ。
# ==================================================================
class PathPasswordTests(PresenterTestCase):
    def other(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="guard_"))

    def test_パスワード無しでは変えられない(self) -> None:
        before = presenter.build().master_dir
        result = presenter.save(master_dir=str(self.other()))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, presenter.REFUSE_NEED_PASSWORD)
        self.assertEqual(presenter.build().master_dir, before)

    def test_違うパスワードでは変えられない(self) -> None:
        before = presenter.build().master_dir
        result = presenter.save(master_dir=str(self.other()),
                                password="ちがうもの")
        self.assertEqual(result.reason, presenter.REFUSE_NEED_PASSWORD)
        self.assertEqual(presenter.build().master_dir, before)

    def test_合っていれば変えられる(self) -> None:
        other = self.other()
        result = presenter.save(master_dir=str(other),
                                password=config.ADMIN_PASSWORD)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(presenter.build().master_dir, str(other))

    def test_断ったときは1つも書いていない(self) -> None:
        """関門は**書く前**。一部だけ変わった状態を残さない。"""
        other = self.other()
        before = presenter.build()
        presenter.save(master_dir=str(other), lot_dir=str(other),
                       auto_import=not before.auto_import)
        view = presenter.build()
        self.assertEqual(view.master_dir, before.master_dir)
        self.assertEqual(view.lot_dir, before.lot_dir)
        # 同じ本文に入っていた「守らない設定」も巻き添えで書かない
        self.assertEqual(view.auto_import, before.auto_import)

    def test_変えない保存では聞かない(self) -> None:
        """設定画面は置き場所を毎回まとめて送る。

        変わらない値まで聞くと「何をしても聞かれる」になり、
        パスワードそのものが形骸化する。
        """
        view = presenter.build()
        result = presenter.save(master_dir=view.master_dir,
                                lot_dir=view.lot_dir)
        self.assertTrue(result.ok, result.message)

    def test_守らない設定は聞かない(self) -> None:
        """拠点・自動取り込み・仕様書URLは、間違えても見え方が変わるだけ。"""
        result = presenter.save(auto_import=False)
        self.assertTrue(result.ok, result.message)

    def test_看板マスタの置き場所もパスワードで守る(self) -> None:
        """置き場所を**変えるとき**だけ管理者パスワードが要る(master_dirと同じ)。"""
        before = presenter.build().kanban_dir
        result = presenter.save(kanban_dir=str(self.other()))
        self.assertEqual(result.reason, presenter.REFUSE_NEED_PASSWORD)
        self.assertEqual(presenter.build().kanban_dir, before)

    def test_看板マスタの置き場所を変えられる(self) -> None:
        other = self.other()
        result = presenter.save(kanban_dir=str(other),
                                password=config.ADMIN_PASSWORD)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(presenter.build().kanban_dir, str(other))

    def test_既定に戻すのも変更(self) -> None:
        """空にすると既定へ戻る。**戻すのも行き先が変わること**。"""
        other = self.other()
        presenter.save(master_dir=str(other), password=config.ADMIN_PASSWORD)
        result = presenter.save(master_dir="")
        self.assertEqual(result.reason, presenter.REFUSE_NEED_PASSWORD)
        self.assertEqual(presenter.build().master_dir, str(other))

    def test_変えたパスワードで通る(self) -> None:
        """この端末で変えてあれば、そちらで通る(既定では通らない)。"""
        from packaging_tool import admin_password
        changed = admin_password.change(config.ADMIN_PASSWORD,
                                        "あたらしい値", "あたらしい値")
        self.assertTrue(changed.ok, changed.message)
        self.addCleanup(admin_password.reset, "あたらしい値")

        other = self.other()
        self.assertEqual(
            presenter.save(master_dir=str(other),
                           password=config.ADMIN_PASSWORD).reason,
            presenter.REFUSE_NEED_PASSWORD)
        self.assertTrue(presenter.save(master_dir=str(other),
                                       password="あたらしい値").ok)


class PathPasswordApiTests(DataWebTestCase):
    """断りの種別を HTTP に写す(設計.md §1)。403 = 許されていない。"""

    def save(self, **body):
        return self.client.post("/api/settings/save", headers=self.auth(),
                                json=body)

    def test_パスワード無しは403(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="guardapi_"))
        res = self.save(master_dir=str(other))
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"],
                         presenter.REFUSE_NEED_PASSWORD)

    def test_合っていれば通る(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="guardapi2_"))
        res = self.save(master_dir=str(other),
                        password=config.ADMIN_PASSWORD)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["master_dir"], str(other))

    def test_入力の形の誤りは400のまま(self) -> None:
        """**種別で言い分ける。** 403と400を1つにまとめない。"""
        res = self.save(position="そんな拠点は無い")
        self.assertEqual(res.status_code, 400)

    def test_返す本文にパスワードは入らない(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="guardapi3_"))
        body = self.save(master_dir=str(other),
                         password=config.ADMIN_PASSWORD).get_json()
        self.assertNotIn("password", body)
        self.assertNotIn(config.ADMIN_PASSWORD,
                         str(body))

    def test_看板マスタの置き場所もパスワード無しでは403(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="guardapi4_"))
        res = self.save(kanban_dir=str(other))
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"],
                         presenter.REFUSE_NEED_PASSWORD)

    def test_看板マスタの置き場所は合っていれば通る(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="guardapi5_"))
        res = self.save(kanban_dir=str(other), password=config.ADMIN_PASSWORD)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["kanban_dir"], str(other))


if __name__ == "__main__":
    unittest.main()
