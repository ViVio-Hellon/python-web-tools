"""設定画面のテスト

`packaging_tool/presenters/settings.py` と `app/routes/settings.py` を見る。

【ここで守りたいこと】
取り込みがうまくいかない原因はほぼ決まっている ── ファイルが無い /
テーブル名が違う / 列名が違う。状態を1つの文字列にして出すと、
どこが問題なのかが読み取れない。
1項目ずつ良し悪しを付けて返すこと、そして**直し方まで書く**ことを固定する。
"""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _web  # noqa: E402
from packaging_tool import config, db, jobs, modes  # noqa: E402
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
                        config.KEY_KANBAN_DB_DIR, config.KEY_AUTO_IMPORT,
                        config.KEY_EXPORT_DIR)
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


class OpeningWayTests(PresenterTestCase):
    """「開き方」の judgement。**できているのにできていないと出さない。**

    現場の声:「取り込めているはずなのに要確認になる/いや・・・取り込めて
    ないの?/でもマスタで見れるよ?」。27テーブル 26,115件を取り込んだ
    直後に、梱包資材マスタ・看板マスタ・パレット閾値マスタの3つが
    そろって「要確認」でした。

    原因は判断の仕方です。VER2.23.1 の時点では読むときの1手目が URI で、
    手元への写しは**開けなかったときの最後の手**だったので、
    `opened_by != WAY_URI` で「回り道した」と言えました。VER2.76.1 で
    `.pending_` を止めるために順番を入れ替え(読むときは最初から写す)、
    こちらを直し忘れたので、**ふつうの経路が毎回「要確認」**になりました。

    できているのにできていないと出すのがいちばん高くつきます ──
    本当の問題が同じ顔で並ぶので、次からは誰も読まなくなります。
    """

    def _make_master(self) -> None:
        conn = sqlite3.connect(self.dir / config.MATERIAL_DB_NAME)
        conn.execute("CREATE TABLE BoardMaster (ボード幅 INTEGER)")
        conn.commit()
        conn.close()

    def _opening(self, title: str = "梱包資材マスタ"):
        section = next(s for s in presenter.build().sections
                       if s.title == title)
        return next((c for c in section.checks if c.label == "開き方"), None)

    def test_ふつうに読めていれば要確認にしない(self) -> None:
        self._make_master()
        check = self._opening()
        self.assertIsNotNone(check, "開き方が出ていません")
        self.assertEqual(check.level, presenter.OK)

    def test_空の括弧を出さない(self) -> None:
        """「ふだんの開き方は通りませんでした()。」が現場に出ていた。

        試していない手の失敗理由を取りに行っていたので、括弧の中が
        空でした。**理由が書けないなら、そもそも断る理由が無い。**
        """
        self._make_master()
        self.assertNotIn("()", self._opening().detail)

    def test_なぜ写すのかが読める(self) -> None:
        """遅くなる経路を通っているので、理由は出す(良し悪しとは別)。"""
        self._make_master()
        self.assertIn(".pending_", self._opening().detail)

    def test_本当に折れた手があれば要確認にする(self) -> None:
        """**黙らせたのではない。** 手当てが要るときは今までどおり言う。"""
        from unittest import mock

        from packaging_tool import source_db
        self._make_master()
        with mock.patch.object(source_db, "_try_copy",
                               side_effect=OSError("写せません")):
            check = self._opening()
        self.assertEqual(check.level, presenter.WARN)
        self.assertIn("写せません", check.detail)

    def test_3つの面すべてで同じ判断(self) -> None:
        """梱包資材マスタだけ直して、看板と閾値を忘れない。"""
        from packaging_tool import import_specs
        self._make_master()
        for name, tables in (
                (config.KANBAN_DB_NAME, import_specs.KANBAN_TABLES),
                (config.THRESHOLD_DB_NAME, import_specs.THRESHOLD_TABLES)):
            conn = sqlite3.connect(self.dir / name)
            for table in tables:
                conn.execute(f'CREATE TABLE "{table}" (dummy TEXT)')
            conn.commit()
            conn.close()
        from packaging_tool import user_settings
        user_settings.save(config.KEY_THRESHOLD_DB_DIR, str(self.dir))
        self.addCleanup(user_settings.save, config.KEY_THRESHOLD_DB_DIR, "")

        for title in ("梱包資材マスタ", "看板マスタ", "パレット閾値マスタ"):
            with self.subTest(title=title):
                check = self._opening(title)
                self.assertIsNotNone(check, f"{title} に開き方が出ていません")
                self.assertEqual(check.level, presenter.OK)


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


class MojibakeTests(PresenterTestCase):
    """**文字化けは版を上げただけでは消えない。**

    取り込み元をUTF-8として決め打ちで読んでいたころ(〜VER2.19.0)に
    入った `�` は、置き換わった時点で元のバイトが失われている。
    読み方を直しても手元の行はそのままなので、取り込み直すまで画面には
    文字化けが出続ける ── 現場の声「文字化け治ってないよ」。
    こちらから見つけて、入れ直しへ案内する。
    """

    def setUp(self) -> None:
        super().setUp()
        from packaging_tool import db
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)

    def test_手元に残った文字化けを数える(self) -> None:
        from packaging_tool import data_sync
        self.conn.execute(
            "INSERT INTO 仕掛ロット (ロット番号,用途名) VALUES (?,?)",
            ("4102781", "JISN�y�y"))
        self.conn.execute(
            "INSERT INTO 仕掛ロット (ロット番号,用途名) VALUES (?,?)",
            ("4102782", "シャーシ"))
        self.conn.commit()
        self.assertEqual(data_sync.mojibake_rows(self.conn), {"仕掛ロット": 1})

    def test_型を書いていない表の文字化けも見つける(self) -> None:
        """手元のDBは型付きだが、走査の条件は取り込み元と同じにしておく。
        「TEXT型の列だけ」に絞ると、型の無い表を素通りする。"""
        from packaging_tool import data_sync
        self.conn.execute('CREATE TABLE "仕掛ロット2" ("ロット番号", "用途名")')
        self.conn.execute('INSERT INTO "仕掛ロット2" VALUES (?,?)',
                          ("4102781", "JISN�y"))
        self.conn.commit()
        columns = [r[1] for r in
                   self.conn.execute('PRAGMA table_info("仕掛ロット2")')]
        self.assertEqual(columns, ["ロット番号", "用途名"])
        found = self.conn.execute(
            'SELECT COUNT(*) FROM "仕掛ロット2" WHERE "用途名" LIKE ?',
            (f"%{data_sync.REPLACEMENT}%",)).fetchone()[0]
        self.assertEqual(found, 1)

    def test_きれいなら黙っている(self) -> None:
        """出続ける警告は読まれなくなる。"""
        from packaging_tool import data_sync
        self.conn.execute(
            "INSERT INTO 仕掛ロット (ロット番号,用途名) VALUES (?,?)",
            ("4102782", "シャーシ①"))
        self.conn.commit()
        self.assertEqual(data_sync.mojibake_rows(self.conn), {})

    def test_判定した文字の入れ方を出す(self) -> None:
        """**判定を隠さない。**

        文字化けの問い合わせが来たとき、これが出ていれば
        「判定を誤った」のか「元のファイルが壊れている」のかを
        その場で切り分けられる。出ていないと調べ直しを一からやることになる。
        """
        from packaging_tool import source_db

        for encoding, shown in ((source_db.ENCODING_CP932, "CP932"),
                                (source_db.ENCODING_UTF8, "UTF-8")):
            with self.subTest(encoding=encoding):
                section = presenter.Section("試し")
                presenter._encoding_check(
                    section, source_db.Probe(encoding=encoding))
                check = next(c for c in section.checks
                             if c.label == "文字の入れ方")
                self.assertIn(shown, check.value)
                self.assertIn("文字化け", check.detail)

    def test_開けなかったファイルには入れ方を出さない(self) -> None:
        """判定できていないのに何か書くと、それ自体が誤情報になる。"""
        from packaging_tool import source_db

        section = presenter.Section("試し")
        presenter._encoding_check(section, source_db.Probe())
        self.assertEqual(section.checks, [])

    def test_取り込み直しへ案内する(self) -> None:
        """見つけただけでは直らない。**入れ直す場所へ繋ぐ。**"""
        self.conn.execute(
            "INSERT INTO 仕掛ロット (ロット番号,用途名) VALUES (?,?)",
            ("4102781", "JISN�y"))
        self.conn.commit()
        section = next(s for s in presenter.build(self.conn).sections
                       if s.title == "梱包資材マスタ")
        check = next(c for c in section.checks if c.label == "文字化けした行")
        self.assertEqual(check.level, presenter.WARN)
        self.assertIn("仕掛ロット", check.detail)
        self.assertIn("取り込み", check.detail)


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
    def test_保存場所の面でこのPCだけのものと共有のものを分けて出す(self) -> None:
        """現場の声:「ローカルに保存してそのPCで引き継ぐものは、設定にそういう
        ファイルがあると明記して。複数PCで共有するものとは違う」。
        場所はいまの設定から引いた実際のパスで出す。"""
        from packaging_tool import config, floor_plan, pallet_map
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('id="panel-storage"', html)
        local = html[html.index('id="storageLocal"'):html.index('id="storageShared"')]
        shared = html[html.index('id="storageShared"'):]
        for path in (config.USER_CONFIG_PATH, config.DB_PATH,
                     floor_plan.USER_PATH, pallet_map.USER_PATH):
            self.assertIn(str(path), local)
        self.assertIn(config.MATERIAL_DB_NAME, shared)
        self.assertIn(str(config.lot_db_dir()), shared)
        self.assertNotIn(str(config.USER_CONFIG_PATH), shared)

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



class BoardUsageCsvTests(DataWebTestCase):
    """ボード人気度のCSV書き出し。"""

    role = "material"

    def setUp(self) -> None:
        super().setUp()
        from app.routes import settings as routes
        self.conn = _web.bind_db(self, routes)
        self.out = self.dir / "out"
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅,ボード丈,ボードタイプ) "
            "VALUES (900,1800,'ハードボード')")
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅,ボード丈,ボードタイプ) "
            "VALUES (1200,2400,'ハードボード')")
        self.conn.execute(
            "INSERT INTO ボード使用実績 "
            "(ボード幅,ボード丈,ボードタイプ,枚数,使用日時) "
            "VALUES (900,1800,'ハードボード',12,'2026-09-10 09:30:00')")
        self.conn.commit()

    def export(self, directory=None, expect=200) -> dict:
        res = self.client.post("/api/settings/board-usage/export",
                               json={"dir": str(directory or self.out)},
                               headers=self.auth())
        self.assertEqual(res.status_code, expect, res.get_json())
        return res.get_json()

    def written(self) -> Path:
        files = sorted(self.out.glob("*.csv"))
        self.assertEqual(len(files), 1, f"書き出したファイルが1つではない: {files}")
        return files[0]

    def test_書いた場所を必ず返す(self) -> None:
        """**「書けました」だけでは足りない。** どこにあるか分からない。"""
        body = self.export()
        self.assertIn(str(self.out), body["message"])
        self.assertIn(".csv", body["message"])

    def test_無いフォルダは作る(self) -> None:
        """既定の書き出し先は初回に存在しない。作らないと1回目だけ落ちる。"""
        self.assertFalse(self.out.exists())
        self.export()
        self.assertTrue(self.written().exists())

    def test_使っていない寸法も入る(self) -> None:
        """画面と同じ中身にする。CSVだけ減っていると突き合わせられない。"""
        self.export()
        text = self.written().read_text(encoding="utf-8-sig")
        self.assertIn("900,1800,12", text)
        self.assertIn("1200,2400,0", text)       # 0回の行も出す

    def test_一覧に無い寸法も同じ表に入る(self) -> None:
        """別ファイルにすると、片方だけ配られて「無い」と読まれる。"""
        self.conn.execute(
            "INSERT INTO ボード使用実績 "
            "(ボード幅,ボード丈,ボードタイプ,枚数,使用日時) "
            "VALUES (930,1800,'ハードボード',1,'2026-09-09 14:00:00')")
        self.conn.commit()
        self.export()
        text = self.written().read_text(encoding="utf-8-sig")
        self.assertIn("一覧に無い,ハードボード,930,1800", text)

    def test_ExcelがそのままUTF8と読める(self) -> None:
        """BOMを付ける。付けないとExcelで日本語が化ける。"""
        self.export()
        self.assertEqual(self.written().read_bytes()[:3], b"\xef\xbb\xbf")

    def test_書き出し先は覚える(self) -> None:
        """毎回打ち直すものではない。次からは空で押せば同じ場所へ。"""
        from packaging_tool import user_settings
        self.export()
        self.assertEqual(user_settings.get(config.KEY_EXPORT_DIR), str(self.out))

    def test_前に出したものを上書きしない(self) -> None:
        """上書きすると、増えたのか減ったのかを見比べられなくなる。"""
        self.export()
        time.sleep(1.05)                          # ファイル名は秒まで
        self.export()
        self.assertEqual(len(list(self.out.glob("*.csv"))), 2)

    def test_書けないときはどこへ書こうとしたかを言う(self) -> None:
        """「書けません」だけでは、道が違うのか権限が無いのか分からない。"""
        wall = self.dir / "壁"
        wall.write_text("", encoding="utf-8")     # 同名のファイルがあると作れない
        body = self.export(wall, expect=400)
        self.assertIn(str(wall), body["error"]["message"])



if __name__ == "__main__":
    unittest.main()


class StaleBadgeTests(unittest.TestCase):
    """**消えた印が消えること。**

    タブの印は「渡されたぶんを書く」だけで、渡されなくなったものを
    消していなかった。取り込み元が見つかるようになっても
    「できません」が出たままで、画面を丸ごと読み込み直すまで消えない
    (現場の指摘:「今の状態をリロードしたら できません が消えた /
    リロードしないとだめなの?」)。

    直っても直ったと言わない画面は、次から誰も信じない。
    """

    def test_付ける側と消す側が同じ場所にある(self) -> None:
        """`setBadges` が、書く前に全部消していること。"""
        js = (Path(__file__).resolve().parent.parent
              / "app/static/js/tabs.js").read_text("utf-8")
        body = js.split("export function setBadges")[1]
        clear, write = body.find('badge.hidden = true'), body.find("Object.entries")
        self.assertGreater(clear, 0, "消す処理がありません")
        self.assertGreater(write, 0)
        self.assertLess(clear, write, "消すのが後では、消えた印が残ります")


class SectionHeadlineTests(unittest.TestCase):
    """**見出しは、次にすることを言う。**

    段階の名前(要確認)だけでは何をすればよいか分からない ── 現場の
    指摘:「この端末の権限の 要確認 もちょっと意味が分からない」。
    することが決まっているまとまりは、それを見出しに出す。
    """

    def test_ふだんは段階の名前のまま(self) -> None:
        section = presenter.Section("試験", checks=[
            presenter.Check("何か", "変です", presenter.WARN)])
        self.assertEqual(section.label(), "要確認")

    def test_差し替えたらそれを出す(self) -> None:
        section = presenter.Section("試験", headline="開き直してください",
                                    checks=[presenter.Check(
                                        "何か", "変です", presenter.WARN)])
        self.assertEqual(section.label(), "開き直してください")

    def test_権限が増えたら開き直してくださいと出る(self) -> None:
        """起動後に権限が増えた端末。**確かめるのではなく起動し直す。**

        起動時に1つも権限が無かったことにすると、いま使えるモードは
        すべて「あとから増えたぶん」になる。
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        self.addCleanup(conn.close)
        section = presenter._access_section(conn, startup_modes=[])
        self.assertTrue(any(c.label == "開き直しが要ります"
                            for c in section.checks), section.checks)
        self.assertEqual(section.label(), "開き直してください")

    def test_資材があとから増えても開き直しを求めない(self) -> None:
        """**画面を起動時の権限で決めているモードだけが、開き直しを要る。**

        資材モードの操作は要求のたびに権限を見る形に直してあるので、
        あとから足してもその場で使える。それなのに「開き直してください」と
        出していた(現場の指摘:「なぜ開きなおしが要るんですか 面倒ですよ」)。
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        self.addCleanup(conn.close)
        # 起動時は現場だけ持っていた = 資材があとから増えた端末
        section = presenter._access_section(conn, startup_modes=("field",))
        self.assertFalse(any(c.label == "開き直しが要ります"
                             for c in section.checks), section.checks)

    def test_現場があとから増えたら開き直しを求める(self) -> None:
        """こちらは本当に開き直さないと画面が出ない(URLを登録していない)。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        self.addCleanup(conn.close)
        section = presenter._access_section(conn, startup_modes=("material",))
        rows = [c for c in section.checks if c.label == "開き直しが要ります"]
        self.assertEqual([c.value for c in rows], ["現場"])

    def test_ほかのツールの権限は参考として出し警告にしない(self) -> None:
        """アクセス権限の表はほかのツールも使う。本ツールが読まない値は
        読み飛ばしていると書くだけで、「マスタの問題」にはしない。"""
        from packaging_tool import access_control
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        self.addCleanup(conn.close)
        me = access_control.current_identity()
        for perm in ("mode:material", "mode:field", "kensa:admin", "出荷担当"):
            conn.execute('INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
                         " VALUES (?, '', ?, 1, '')", (me.login_id or "someone", perm))
        conn.commit()
        section = presenter._access_section(conn, startup_modes=("field", "material"))
        self.assertFalse([c for c in section.checks if c.label == "マスタの問題"],
                         section.checks)
        foreign = [c for c in section.checks if c.label == "ほかのツールの権限"]
        self.assertEqual(len(foreign), 1)
        self.assertEqual(foreign[0].level, presenter.INFO)
        self.assertIn("kensa:admin", foreign[0].detail)
        self.assertIn("出荷担当", foreign[0].detail)

    def test_開き直しが要るモードの表は1か所(self) -> None:
        """案内と実装が別々に持つと、直したのに案内だけ残る。"""
        from app import GATED_MODES
        self.assertEqual(GATED_MODES, (modes.FIELD,))

    def test_開き直しが要らなければ差し替えない(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        self.addCleanup(conn.close)
        section = presenter._access_section(conn, startup_modes=None)
        self.assertFalse(any(c.label == "開き直しが要ります"
                             for c in section.checks))
        self.assertNotEqual(section.label(), "開き直してください")


class BrowserDialogPlacementTests(unittest.TestCase):
    """**フォルダ参照はどのタブからでも開けること。**

    `<dialog>` をタブの中に置くと、別のタブを開いているあいだ
    `display:none` の中に居る。その状態で `showModal()` すると、
    ダイアログは見えないのに画面だけがモーダルで固まる
    (現場の指摘:「ボード人気 CSV 書き出し先を参照すると固まる」)。
    """

    def setUp(self) -> None:
        self.html = (Path(__file__).resolve().parent.parent
                     / "app/templates/settings.html").read_text("utf-8")

    def test_ダイアログはタブの中に無い(self) -> None:
        lines = self.html.splitlines()
        panel = ""
        for line in lines:
            found = re.search(r'id="(panel-[a-z]+)"', line)
            if found:
                panel = found.group(1)
            if "</section>" in line and panel:
                panel = ""
            if 'id="browser"' in line:
                self.assertEqual(
                    panel, "",
                    f"フォルダ参照が {panel} の中にあります。"
                    "別のタブから開くと画面が固まります")

    def test_参照を持つ欄は複数のタブにある(self) -> None:
        """1つのタブに閉じているなら、この試験自体が要らなくなる。"""
        tabs = set()
        panel = ""
        for line in self.html.splitlines():
            found = re.search(r'id="(panel-[a-z]+)"', line)
            if found:
                panel = found.group(1)
            if "data-browse=" in line:
                tabs.add(panel)
        self.assertGreater(len(tabs), 1, tabs)


class NoServerErrorTests(unittest.TestCase):
    """**断り方を用意していない道を残さない。**

    入力の形が違えば 400、業務として通せなければ 422 を返す、と決めて
    あります(設計書 §6.1)。ところが断りへ辿り着く**前に**落ちる道が
    あり、そこは 500 になります。画面には

        通信に失敗しました (HTTP 500)

    としか出ません ── 通信は成功しているのに通信の失敗として案内される
    ので、現場は直しようがありません。

    見つかったのは4つで、どれも「`float()` は通るが `int()` で落ちる」
    「`in` に一覧を渡す」といった、**型の想定違い**でした。
    """

    def setUp(self) -> None:
        from packaging_tool import access_control as ac
        from app import create_app
        app = create_app("field", token=TOKEN, port=8794,
                         grant=ac.grant_of("mode:field", "mode:material"))
        app.config["TESTING"] = True
        app.config["READY"] = True
        self.client = app.test_client()

    def post(self, path: str, body: dict):
        return self.client.post(path, json=body, headers={"X-Tool-Token": TOKEN})

    def test_無限大の寸法は断る(self) -> None:
        """`float("1e400")` は `inf`。`_is_numeric` は通していた。"""
        for path in ("/api/selection/pallet/apply",
                     "/api/selection/product/apply"):
            for value in ("1e400", "inf", "nan", "-inf"):
                with self.subTest(path=path, value=value):
                    res = self.post(path, {"width": value, "length": value})
                    self.assertLess(res.status_code, 500)

    def test_取り込む対象に一覧が来ても断る(self) -> None:
        """`[] in TARGET_KEYS` は `TypeError: unhashable type`。"""
        for value in ([], {}, ["all"]):
            with self.subTest(value=value):
                res = self.post("/api/settings/import", {"target": value})
                self.assertEqual(res.status_code, 400)

    def test_道として使えない書き出し先は断る(self) -> None:
        """`\\x00` を含む道は `OSError` ではなく `ValueError` で落ちる。"""
        res = self.post("/api/settings/board-usage/export", {"dir": "\x00"})
        self.assertLess(res.status_code, 500)


class NumericGuardTests(unittest.TestCase):
    """`_is_numeric` は「**寸法として使える数値か**」を答える。

    呼ぶ側はこれが True なら `int(float(text))` してよい、という約束で
    書かれている。`float()` が `inf` / `nan` を受けるので、そこを
    通り抜けて `OverflowError` で 500 になっていた。
    """

    def test_無限大と非数は数値として扱わない(self) -> None:
        from packaging_tool.pallet_common import _is_numeric
        for text in ("inf", "-inf", "nan", "NaN", "1e400", "infinity"):
            with self.subTest(text=text):
                self.assertFalse(_is_numeric(text))

    def test_ふつうの寸法は今までどおり(self) -> None:
        from packaging_tool.pallet_common import _is_numeric
        for text in ("1200", "1122.0", "0", "-5", "1e3"):
            with self.subTest(text=text):
                self.assertTrue(_is_numeric(text))

    def test_通した値は必ず整数にできる(self) -> None:
        """**約束そのもの。** これが破れると呼ぶ側が全部落ちる。"""
        from packaging_tool.pallet_common import _is_numeric
        for text in ("1200", "1e308", "-0.0", "  50  "):
            with self.subTest(text=text):
                if _is_numeric(text):
                    int(float(text))          # 落ちなければよい


class ExportDirTypeTests(unittest.TestCase):
    """**書き出し先に文字以外が来たら、フォルダを作らずに断る。**

    以前は何でも `str()` していたので、一覧 `[1, 2]` や `None` が
    そのまま**フォルダ名になって作られて**いた。相対の道はアプリの
    フォルダ基準なので、アプリの隣に `[1, 2]` `None` `True` `1e400` …
    が並び、それが配布物にまで入った(現場の指摘:「変な名前のフォルダ
    とかあるけど」)。
    """

    def setUp(self) -> None:
        from packaging_tool import access_control as ac
        from app import create_app
        app = create_app("field", token=TOKEN, port=8795,
                         grant=ac.grant_of("mode:field"))
        app.config["TESTING"] = True
        app.config["READY"] = True
        self.client = app.test_client()

    def test_文字でなければ断ってフォルダを作らない(self) -> None:
        import shutil
        for value in ([1, 2], [], {}, True, 1.5, 0):
            with self.subTest(value=value):
                before = set(p.name for p in config.BASE_DIR.iterdir())
                res = self.client.post("/api/settings/board-usage/export",
                                       json={"dir": value},
                                       headers={"X-Tool-Token": TOKEN})
                after = set(p.name for p in config.BASE_DIR.iterdir())
                made = after - before
                # **落ちても散らかさない。** 直しが壊れたとき、この試験
                # 自身がアプリの隣にフォルダを作ってしまう ── 今回の事故と
                # 同じ形で、`git add -A` すれば配布物に入る
                for name in made:
                    shutil.rmtree(config.BASE_DIR / name, ignore_errors=True)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(made, set(), f"フォルダができていました: {made}")


class RepoRootTests(unittest.TestCase):
    """**配るフォルダに、配るつもりのないものを入れない。**

    `git add -A` で、試験が作ったフォルダ(`[1, 2]` `あ` `1e400` …)と
    網羅率の作業ファイル(`.coverage`)が紛れ込み、現場のフォルダに
    入った。直下に何を置くかはめったに変わらないので、一覧で持つ。
    **ここに足すときは、それが配るものかを確かめること。**
    """

    ALLOWED = {
        ".gitattributes", ".gitignore", "README.md", "requirements.txt",
        "Start.vbs", "start.bat", "stop.bat",
        "start_app.py", "server.py", "boot_server.py", "launch_guard.py",
        "process_manager.py",
        "app", "config", "docs", "packaging_tool", "scripts", "tests",
        # 同梱した変換ツールの読み取り部品(「表を持ってくる」で Access を読む)
        "vendor",
        # デスクトップ版(Tauri)の入口。外枠の exe がこれを子として起動する
        "bridge.py",
        # デスクトップ版の外枠の**ソース**と、それを作る GitHub Actions。
        # 配るのは作った exe だけ(`make_dist.DEV_ONLY`)
        "src-tauri", ".github",
    }

    def test_直下には配るものだけ(self) -> None:
        import shutil
        import subprocess
        if shutil.which("git") is None:
            self.skipTest("git がありません")
        root = Path(__file__).resolve().parent.parent
        try:
            out = subprocess.run(
                ["git", "-c", "core.quotepath=false", "ls-files"],
                cwd=root, capture_output=True, text=True, check=True,
                # git は UTF-8 で返す(Windows の既定の文字コードでは日本語の名前を読めない)
                encoding="utf-8").stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git の作業フォルダではありません")
        tracked = {line.split("/")[0] for line in out.splitlines() if line}
        self.assertEqual(tracked - self.ALLOWED, set(),
                         "配るつもりのないものが追跡されています")


class ImportDiagReportTests(DataWebTestCase):
    """取り込みの記録を画面から開く・保存する(`/report/import-diag`)。

    ログフォルダは Windows では隠しフォルダ(`%LOCALAPPDATA%`)の中にあり、
    現場からは探せなかった(「ログフォルダなんてないけど」)。
    """

    def test_記録が無ければ案内を返す(self) -> None:
        res = self.client.get("/report/import-diag", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertIn("まだ取り込みの記録がありません", res.get_data(as_text=True))

    def test_記録を開ける_保存もできる(self) -> None:
        from packaging_tool import import_diag
        with import_diag.run("試しの取り込み"):
            import_diag.write("  見送り: SIKALOT.sqlite3 ── 更新時刻が進んでいない")
        res = self.client.get("/report/import-diag", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.mimetype.startswith("text/plain"))
        text = res.get_data(as_text=True)
        self.assertIn("■ 試しの取り込み", text)
        self.assertIn("更新時刻が進んでいない", text)
        self.assertNotIn("Content-Disposition", res.headers)

        saved = self.client.get("/report/import-diag?save=1", headers=self.auth())
        self.assertIn("attachment", saved.headers["Content-Disposition"])

    def test_トークンが無ければ見せない(self) -> None:
        """記録にはファイルの置き場所やロット番号が入る。"""
        res = self.client.get("/report/import-diag")
        self.assertIn(res.status_code, (401, 403))

    def test_設定画面にボタンがある(self) -> None:
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('id="importDiag"', html)
        self.assertIn('id="importDiagSave"', html)


class TableBringTests(DataWebTestCase):
    """Access で作った表を持ってくる(`table_bring`)。

    表を足すたびに sqlite3 を丸ごと差し替えると、ツールが書いた行と
    送信IDの索引が消える。**無い表だけ**を写し、今ある表には触らない。
    """

    def setUp(self) -> None:
        super().setUp()
        import sqlite3
        from unittest import mock
        from packaging_tool import master_admin, selection_session
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        self.session = selection_session.get_session(sqlite3.connect(":memory:"))
        patcher = mock.patch.object(master_admin, "can_edit",
                                    return_value=(True, ""))
        patcher.start()
        self.addCleanup(patcher.stop)
        local = mock.patch.dict("os.environ",
                                {"PACKAGING_TOOL_LOCAL_DIR": str(self.dir / "local")})
        local.start()
        self.addCleanup(local.stop)

        # いまの梱包資材マスタ: ツールが書いた行が入っている
        self.master = self.dir / config.MATERIAL_DB_NAME
        conn = sqlite3.connect(self.master)
        conn.execute("CREATE TABLE BoardMaster (管理番号 INTEGER, ボード幅 INTEGER)")
        conn.execute("INSERT INTO BoardMaster VALUES (1, 1100)")
        conn.execute('CREATE TABLE "資材パレット注文管理" (管理番号 INTEGER, 送信ID TEXT)')
        conn.execute('INSERT INTO "資材パレット注文管理" VALUES (1, "abc")')
        conn.commit()
        conn.close()
        # Access から変換したファイル: 同じ表(中身は古い)+ 新しい表
        self.converted = self.dir / "access" / "変換.sqlite3"
        self.converted.parent.mkdir()
        conn = sqlite3.connect(self.converted)
        conn.execute("CREATE TABLE BoardMaster (管理番号 INTEGER, ボード幅 INTEGER)")
        conn.execute('CREATE TABLE "資材パレット注文管理" (管理番号 INTEGER, 送信ID TEXT)')
        conn.execute('CREATE TABLE "新しい表" (ID INTEGER PRIMARY KEY, 名前 TEXT NOT NULL)')
        conn.execute('CREATE INDEX "IX_新しい表" ON "新しい表"(名前)')
        conn.executemany('INSERT INTO "新しい表"(名前) VALUES (?)', [("あ",), ("い",)])
        conn.commit()
        conn.close()

    def plan(self) -> dict:
        from urllib.parse import quote
        res = self.client.get(f"/api/settings/table-bring/plan?path={quote(str(self.converted))}",
                              headers=self.auth())
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def bring(self, tables, expect=200) -> dict:
        res = self.client.post("/api/settings/table-bring", headers=self.auth(),
                               json={"path": str(self.converted), "tables": tables})
        self.assertEqual(res.status_code, expect, res.get_json())
        return res.get_json()

    def master_rows(self, sql: str) -> list:
        import sqlite3
        conn = sqlite3.connect(self.master)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_中を見ると無い表だけ選べる(self) -> None:
        plan = self.plan()
        self.assertTrue(plan["ok"])
        by_name = {t["name"]: t for t in plan["tables"]}
        self.assertFalse(by_name["新しい表"]["exists"])
        self.assertEqual(by_name["新しい表"]["rows"], 2)
        self.assertTrue(by_name["BoardMaster"]["exists"])
        self.assertIn("無い表が 1 個", plan["message"])

    def test_無い表だけ写して今ある表には触らない(self) -> None:
        self.session.admin = True
        body = self.bring(["新しい表"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["brought"], [{"name": "新しい表", "rows": 2}])
        # 定義(主キー・NOT NULL)と索引も元のとおり
        sqls = [r[0] for r in self.master_rows(
            "SELECT sql FROM sqlite_master WHERE tbl_name = '新しい表'")]
        self.assertIn("PRIMARY KEY", sqls[0])
        self.assertTrue(any("IX_新しい表" in s for s in sqls))
        self.assertEqual(self.master_rows('SELECT 名前 FROM "新しい表" ORDER BY ID'),
                         [("あ",), ("い",)])
        # ツールが書いた行はそのまま
        self.assertEqual(self.master_rows('SELECT 送信ID FROM "資材パレット注文管理"'),
                         [("abc",)])
        # 書く前の控えがある
        self.assertTrue(Path(body["backup"]).is_file())
        # 持ってきたあとは「もうある」になる
        self.assertTrue({t["name"]: t for t in body["plan"]["tables"]}["新しい表"]["exists"])

    def test_もうある表は持ってこない(self) -> None:
        self.session.admin = True
        body = self.bring(["BoardMaster"], expect=409)
        self.assertIn("もうある表は持ってきません", body["error"]["message"])
        self.assertEqual(self.master_rows("SELECT * FROM BoardMaster"), [(1, 1100)])

    def test_足した表はマスタ管理でそのまま直せる(self) -> None:
        """現場の声:「マスタ管理には追加されていないので結局何もできない」。"""
        self.session.admin = True
        self.bring(["新しい表"])
        # 一覧に「足」で出て、直せる
        browse = self.client.get("/api/master/browse?table=" + "新しい表",
                                 headers=self.auth()).get_json()
        info = {t["table"]: t for t in browse["tables"]}
        self.assertTrue(info["新しい表"]["editable"])
        self.assertEqual(info["新しい表"]["mark"], "足")
        # 記録の表そのものは出さない
        self.assertNotIn("ツールで足した表", info)
        self.assertTrue(browse["page"]["editable"])
        self.assertEqual([c["name"] for c in browse["columns"]], ["ID", "名前"])
        # 1行足す・直す・消す
        res = self.client.post("/api/master/row/add", headers=self.auth(),
                               json={"table": "新しい表", "values": {"名前": "う"}})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.master_rows('SELECT 名前 FROM "新しい表" ORDER BY rowid'),
                         [("あ",), ("い",), ("う",)])
        rowid = self.master_rows('SELECT rowid FROM "新しい表" WHERE 名前 = \'あ\'')[0][0]
        res = self.client.post("/api/master/row/save", headers=self.auth(),
                               json={"table": "新しい表", "key": rowid,
                                     "values": {"名前": "え"}})
        self.assertEqual(res.status_code, 200, res.get_json())
        res = self.client.post("/api/master/row/delete", headers=self.auth(),
                               json={"table": "新しい表", "key": rowid})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.master_rows('SELECT 名前 FROM "新しい表" ORDER BY rowid'),
                         [("い",), ("う",)])

    # --- 表を消す -------------------------------------------------------
    def drop(self, table, confirm, expect=200) -> dict:
        res = self.client.post("/api/master/table/drop", headers=self.auth(),
                               json={"table": table, "confirm": confirm})
        self.assertEqual(res.status_code, expect, res.get_json())
        return res.get_json()

    def test_持ってきた表は消せる(self) -> None:
        """現場の声:「無駄に入れてしまったものを消せないと困ってしまう」。"""
        self.session.admin = True
        self.bring(["新しい表"])
        page = self.client.get("/api/master/browse?table=新しい表",
                               headers=self.auth()).get_json()["page"]
        self.assertTrue(page["droppable"])
        self.assertIn("持ってくる", page["drop_note"])

        body = self.drop("新しい表", "新しい表")
        self.assertIn("消しました", body["message"])
        self.assertEqual(self.master_rows(
            "SELECT name FROM sqlite_master WHERE name = '新しい表'"), [])
        # 足した表の記録からも消える。一覧にも出ない
        self.assertEqual(self.master_rows(
            'SELECT 表 FROM "ツールで足した表"'), [])
        self.assertNotIn("新しい表", [t["table"] for t in body["tables"]])
        # 消す前の控え
        backups = list((self.dir / "local" / "backup").glob("*.sqlite3"))
        self.assertTrue(backups)
        # ツールが書いた行には触らない
        self.assertEqual(self.master_rows('SELECT 送信ID FROM "資材パレット注文管理"'),
                         [("abc",)])

    def test_表の名前が違えば消さない(self) -> None:
        self.session.admin = True
        self.bring(["新しい表"])
        body = self.drop("新しい表", "新しい", expect=400)
        self.assertIn("そのまま入れてください", body["error"]["message"])
        self.assertTrue(self.master_rows(
            "SELECT name FROM sqlite_master WHERE name = '新しい表'"))

    def test_管理者認証が無ければ消さない(self) -> None:
        self.session.admin = True
        self.bring(["新しい表"])
        self.session.admin = False
        self.drop("新しい表", "新しい表", expect=403)
        self.assertTrue(self.master_rows(
            "SELECT name FROM sqlite_master WHERE name = '新しい表'"))

    def test_このツールが使う表は消せない(self) -> None:
        self.session.admin = True
        page = self.client.get("/api/master/browse?table=BoardMaster",
                               headers=self.auth()).get_json()["page"]
        self.assertFalse(page["droppable"])
        body = self.drop("BoardMaster", "BoardMaster", expect=422)
        self.assertIn("このツールが使っている表", body["error"]["message"])
        self.assertEqual(self.master_rows("SELECT * FROM BoardMaster"), [(1, 1100)])

    def test_よその表も消せるが一言添える(self) -> None:
        """前の版で持ってきて記録の無い表(現場の「Test」)も消せるように。"""
        import sqlite3
        conn = sqlite3.connect(self.master)
        conn.execute('CREATE TABLE "Test" (a TEXT)')
        conn.commit()
        conn.close()
        self.session.admin = True
        page = self.client.get("/api/master/browse?table=Test",
                               headers=self.auth()).get_json()["page"]
        self.assertTrue(page["droppable"])
        self.assertIn("VBA", page["drop_note"])
        self.drop("Test", "Test")
        self.assertEqual(self.master_rows(
            "SELECT name FROM sqlite_master WHERE name = 'Test'"), [])

    def test_足していない知らない表は見るだけ(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.master)
        conn.execute('CREATE TABLE "よその表" (a TEXT)')
        conn.commit()
        conn.close()
        res = self.client.post("/api/master/row/add", headers=self.auth(),
                               json={"table": "よその表", "values": {"a": "x"}})
        self.assertEqual(res.status_code, 422)

    def test_管理者認証が無ければ書かない(self) -> None:
        self.session.admin = False
        body = self.bring(["新しい表"], expect=403)
        self.assertIn("管理者認証", body["error"]["message"])
        self.assertEqual(self.master_rows(
            "SELECT name FROM sqlite_master WHERE name = '新しい表'"), [])

    def test_いまの梱包資材マスタそのものは選べない(self) -> None:
        from urllib.parse import quote
        res = self.client.get(f"/api/settings/table-bring/plan?path={quote(str(self.master))}",
                              headers=self.auth())
        self.assertFalse(res.get_json()["ok"])
        self.assertIn("いま使っている梱包資材マスタそのもの", res.get_json()["message"])

    def test_画面に段がある(self) -> None:
        html = self.client.get("/settings").get_data(as_text=True)
        for key in ('id="bringOpen"', 'id="bringPath"', 'data-browse-file="1"', 'id="bringLook"',
                    'id="bringRun"'):
            self.assertIn(key, html)


# 本物の Access の頭(`table_bring.sniff` が Access と見分ける最小の形)
ACCESS_HEAD = b"\x00\x01\x00\x00Standard ACE DB\x00" + b"\x00" * 200

_FAKE_ENGINE = '''
import os
def read_source(src, prefer="auto"):
    # 本物の変換ツールと同じ言い方(どちらで読めなくても同じ前置きで、方式ごとの行が続く)
    if "部品無し" in os.path.basename(src):
        raise RuntimeError("どちらの読み取りエンジンも使用できませんでした。\\n詳細:\\n"
                           "pyodbc: No module named 'pyodbc'\\n"
                           "access_parser: No module named 'access_parser'")
    if "読めない" in os.path.basename(src):
        with open(os.path.join(os.path.dirname(__file__), "calls.txt"), "a") as fh:
            fh.write("1")
        raise RuntimeError("どちらの読み取りエンジンも使用できませんでした。\\n詳細:\\n"
                           "pyodbc: No module named 'pyodbc'\\n"
                           "access_parser: Failed to parse DB file header. "
                           "Check it is a valid access database")
    # 呼ばれた回数を数える(同じ Access を2回変換しないことを確かめる)
    with open(os.path.join(os.path.dirname(__file__), "calls.txt"), "a") as fh:
        fh.write("1")
    desc = ("Access (access_parser (pure python))" if "予備" in os.path.basename(src)
            else "Access (fake)")
    return ([{"name": "新しい表", "columns": ["ID", "名前"], "rows": [(1, "あ"), (2, "い")]},
             {"name": "BoardMaster", "columns": ["ボード幅"], "rows": [(9,)]},
             {"name": "MSysObjects", "columns": ["Id"], "rows": [(1,)]},
             # 実物で起きた読み違え(幅 1310 → 131、丈 1800 → 化けた字)
             {"name": "化けた表", "columns": ["幅", "丈"],
              "rows": [(1310, "1800"), (131, "Ｐㇾ\ufffd")]}],
            desc)
'''
_FAKE_WRITERS = '''
import sqlite3
def write_sqlite(tables, out_path, report=None):
    conn = sqlite3.connect(out_path)
    for t in tables:
        cols = ", ".join('"%s"' % c for c in t["columns"])
        conn.execute('CREATE TABLE "%s" (%s)' % (t["name"], cols))
        marks = ", ".join("?" for _ in t["columns"])
        conn.executemany('INSERT INTO "%s" VALUES (%s)' % (t["name"], marks), t["rows"])
    conn.commit()
    conn.close()
    return out_path
'''


class TableRefreshTests(TableBringTests):
    """もうある表の中身を、Access の最新に入れ替える(`table_bring.refresh`)。

    以前は Access をまるごと変換して差し替えるしかなく、このツールが共有に
    足した表・列・行が消えていた。**選んだ表の中身だけ**を入れ替える。
    """

    def setUp(self) -> None:
        super().setUp()
        import sqlite3
        # いまの梱包資材マスタ: 型録の行 + このツールが足した在庫の行
        conn = sqlite3.connect(self.master)
        conn.execute("CREATE TABLE PalletMaster (管理番号, 幅, 丈, 位置, 在庫数, 備考)")
        conn.executemany("INSERT INTO PalletMaster VALUES (?,?,?,?,?,?)", [
            (1, 1000, 1000, "", "", "古い型録"),
            (2, 1100, 2000, "", "", "古い型録"),
            (3, 1000, 1000, "A1", 5, "ツールが受け入れた在庫")])
        conn.execute("CREATE TABLE 松板角材 (管理番号 INTEGER, 名前 TEXT NOT NULL)")
        conn.execute("INSERT INTO 松板角材 VALUES (1, '残るはず')")
        conn.commit()
        conn.close()
        # Access 側: BoardMaster と PalletMaster が新しくなっている
        conn = sqlite3.connect(self.converted)
        conn.executemany("INSERT INTO BoardMaster VALUES (?, ?)", [(1, 1200), (2, 1300)])
        conn.execute("ALTER TABLE BoardMaster ADD COLUMN Accessだけの列 TEXT")
        conn.execute("CREATE TABLE PalletMaster (管理番号, 幅, 丈, 位置, 在庫数, 備考)")
        conn.executemany("INSERT INTO PalletMaster VALUES (?,?,?,?,?,?)", [
            (1, 1000, 1000, "", "", "新しい型録"),
            (2, 1100, 2000, "", "", "新しい型録"),
            (3, 1200, 2500, "", "", "足された型録"),
            (4, 1000, 1000, "A1", 1, "Access の古い在庫")])
        conn.execute("CREATE TABLE 松板角材 (管理番号 INTEGER, 名前 TEXT)")
        conn.execute("INSERT INTO 松板角材 VALUES (1, NULL)")        # 入れると断られる行
        conn.commit()
        conn.close()

    def refresh(self, tables, expect=200) -> dict:
        res = self.client.post("/api/settings/table-refresh", headers=self.auth(),
                               json={"path": str(self.converted), "tables": tables})
        self.assertEqual(res.status_code, expect, res.get_json())
        return res.get_json()

    def test_中を見るともうある表の入れ替えを選べる(self) -> None:
        by_name = {t["name"]: t for t in self.plan()["tables"]}
        board = by_name["BoardMaster"]
        self.assertTrue(board["can_refresh"])
        self.assertEqual((board["current_rows"], board["rows"]), (1, 2))
        self.assertEqual(board["action"], "refresh")
        self.assertEqual(board["added_columns"], ["Accessだけの列"])
        self.assertEqual(board["removed_columns"], [])
        orders = by_name["資材パレット注文管理"]
        self.assertFalse(orders["can_refresh"])
        self.assertIn("このツールが書き込む表", orders["refresh_why"])
        self.assertIn("在庫の行", by_name["PalletMaster"]["keeps"])

    def test_選んだ表の中身だけを入れ替える(self) -> None:
        self.session.admin = True
        body = self.refresh(["BoardMaster"])
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["refreshed"], [{"name": "BoardMaster", "before": 1, "after": 2}])
        self.assertEqual(self.master_rows("SELECT 管理番号, ボード幅 FROM BoardMaster"),
                         [(1, 1200), (2, 1300)])
        # Access で足した列は、型も Access のまま足す
        self.assertIn(("Accessだけの列", "TEXT"),
                      [(r[1], r[2]) for r in self.master_rows("PRAGMA table_info(BoardMaster)")])
        self.assertIn("列 Accessだけの列 を足しました", body["message"])
        # ほかの表(ツールが書いた発注)には触らない
        self.assertEqual(self.master_rows('SELECT * FROM "資材パレット注文管理"'), [(1, "abc")])
        self.assertTrue(Path(body["backup"]).exists())

    def test_パレットはツールが足した在庫の行を残す(self) -> None:
        self.session.admin = True
        body = self.refresh(["PalletMaster"])
        self.assertIn("在庫の行 1行 は残しました", body["message"])
        rows = self.master_rows("SELECT 幅, 丈, 位置, 在庫数, 備考 FROM PalletMaster ORDER BY 幅, 位置")
        self.assertEqual(rows, [
            (1000, 1000, "", "", "新しい型録"),
            (1000, 1000, "A1", 5, "ツールが受け入れた在庫"),       # Access の古い在庫では上書きしない
            (1100, 2000, "", "", "新しい型録"),
            (1200, 2500, "", "", "足された型録")])
        numbers = [r[0] for r in self.master_rows("SELECT 管理番号 FROM PalletMaster")]
        self.assertEqual(len(numbers), len(set(numbers)), numbers)   # 番号が重ならない

    def test_ツールが書き込む表は入れ替えない(self) -> None:
        self.session.admin = True
        body = self.refresh(["資材パレット注文管理"], expect=400)
        self.assertIn("このツールが書き込む表", body["message"])
        self.assertEqual(self.master_rows('SELECT * FROM "資材パレット注文管理"'), [(1, "abc")])

    def test_入れられない行があれば表は元のまま(self) -> None:
        """消す+入れるを1回で確定する。途中で断られたら消した分も戻る。"""
        self.session.admin = True
        body = self.refresh(["松板角材"], expect=422)
        self.assertFalse(body["ok"])
        self.assertEqual(self.master_rows("SELECT * FROM 松板角材"), [(1, "残るはず")])

    def test_文字化けの疑いがある表は入れ替えない(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.converted)
        conn.execute("INSERT INTO BoardMaster (管理番号, ボード幅) VALUES (9, '13\ufffd')")
        conn.commit()
        conn.close()
        board = {t["name"]: t for t in self.plan()["tables"]}["BoardMaster"]
        self.assertFalse(board["can_refresh"])
        self.assertIn("文字化け", board["refresh_why"])

    def test_管理者認証が要る(self) -> None:
        self.refresh(["BoardMaster"], expect=403)
        self.assertEqual(self.master_rows("SELECT ボード幅 FROM BoardMaster"), [(1100,)])

    # -- マスタ管理で足した列(`master_schema.add_column`) --------------------
    def add_column(self, table, name, kind="text", initial=""):
        from unittest import mock
        from packaging_tool import master_admin, master_schema
        # 権限は別の試験で見る(ここでは足せる状態から)
        import sqlite3
        from packaging_tool import db
        local = sqlite3.connect(":memory:")       # 足したあとの取り込み直しの先
        local.row_factory = sqlite3.Row
        db.apply_schema(local)
        self.addCleanup(local.close)
        with mock.patch.object(master_schema, "can_edit", return_value=(True, "")):
            result = master_admin.add_column(local, table, name, kind, initial,
                                             path=self.master)
        self.assertTrue(result.ok, result.message)

    def refresh_ok(self, tables, expect=200) -> dict:
        res = self.client.post("/api/settings/table-refresh", headers=self.auth(),
                               json={"path": str(self.converted), "tables": tables,
                                     "empty_ok": True})
        self.assertEqual(res.status_code, expect, res.get_json())
        return res.get_json()

    def test_足した列は消える列に数えず作り直さない(self) -> None:
        """数えると、列を1つ足しただけの表が作り直され、足した列ごと消える。"""
        self.add_column("BoardMaster", "棚メモ", initial="奥")
        board = {t["name"]: t for t in self.plan()["tables"]}["BoardMaster"]
        self.assertEqual(board["action"], "refresh")
        self.assertEqual(board["removed_columns"], [])
        self.assertEqual(board["tool_columns"], ["棚メモ"])
        self.assertEqual(board["emptied"], [{"name": "棚メモ", "rows": 1}])

    def test_足した列に値があれば確かめるまで入れ替えない(self) -> None:
        self.session.admin = True
        self.add_column("BoardMaster", "棚メモ", initial="奥")
        body = self.refresh(["BoardMaster"], expect=409)
        self.assertEqual(body["error"]["code"], "need_confirm")
        self.assertIn("棚メモ 1行", body["message"])
        self.assertEqual(self.master_rows("SELECT ボード幅, 棚メモ FROM BoardMaster"),
                         [(1100, "奥")])

    def test_確かめたら入れ替えて列は残し値は空にする(self) -> None:
        self.session.admin = True
        self.add_column("BoardMaster", "棚メモ", initial="奥")
        body = self.refresh_ok(["BoardMaster"])
        self.assertTrue(body["ok"], body)
        self.assertIn("足した列 棚メモ の値は空にしました", body["message"])
        self.assertEqual(self.master_rows("SELECT ボード幅, 棚メモ FROM BoardMaster"),
                         [(1200, None), (1300, None)])
        board = {t["name"]: t for t in body["plan"]["tables"]}["BoardMaster"]
        self.assertEqual(board["emptied"], [])       # もう空。次は確かめない

    def test_足した列に値が無ければ確かめない(self) -> None:
        self.session.admin = True
        self.add_column("BoardMaster", "棚メモ")
        body = self.refresh(["BoardMaster"])
        self.assertTrue(body["ok"], body)
        self.assertIn("棚メモ", [r[1] for r in self.master_rows("PRAGMA table_info(BoardMaster)")])

    def test_作り直す表でも足した列は足し直す(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.master)
        conn.execute("ALTER TABLE BoardMaster ADD COLUMN Accessで消した列 TEXT")
        conn.commit()
        conn.close()
        self.session.admin = True
        self.add_column("BoardMaster", "年次", "int", "2")
        board = {t["name"]: t for t in self.plan()["tables"]}["BoardMaster"]
        self.assertEqual(board["action"], "rebuild")
        self.assertEqual(board["removed_columns"], ["Accessで消した列"])
        body = self.refresh_ok(["BoardMaster"])
        self.assertTrue(body["ok"], body)
        self.assertIn("足した列 年次 は作り直した表にも足しました", body["message"])
        info = {r[1]: r[2] for r in self.master_rows("PRAGMA table_info(BoardMaster)")}
        self.assertEqual(info.get("年次"), "INTEGER")
        self.assertNotIn("Accessで消した列", info)

    def test_パレットの在庫の行は足した列の値も残す(self) -> None:
        """在庫の行は入れ替えでも残す(`KEEPS`)ので、その行の値は空にならない。"""
        self.session.admin = True
        self.add_column("PalletMaster", "棚メモ", initial="奥")
        pallet = {t["name"]: t for t in self.plan()["tables"]}["PalletMaster"]
        self.assertEqual(pallet["emptied"], [{"name": "棚メモ", "rows": 2}])  # 型録の2行だけ
        self.refresh_ok(["PalletMaster"])
        self.assertEqual(self.master_rows(
            "SELECT 位置, 棚メモ FROM PalletMaster WHERE 位置 = 'A1'"), [("A1", "奥")])

    def test_足した列の記録は持ってくる表に出さない(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.converted)
        conn.execute('CREATE TABLE "ツールで足した列" (表 TEXT, 列 TEXT)')
        conn.commit()
        conn.close()
        names = [t["name"] for t in self.plan()["tables"]]
        self.assertNotIn("ツールで足した列", names)

    def rename_in_access(self, table: str, create: str, rows: list[tuple]) -> None:
        """Access 側で列を消した・名前を変えた表にする。"""
        import sqlite3
        conn = sqlite3.connect(self.converted)
        conn.execute(f'DROP TABLE "{table}"')
        conn.execute(create)
        marks = ",".join("?" * len(rows[0]))
        conn.executemany(f'INSERT INTO "{table}" VALUES ({marks})', rows)
        conn.commit()
        conn.close()

    def test_列の名前が変わった表は作り直して前の表を残す(self) -> None:
        self.rename_in_access(
            "BoardMaster",
            "CREATE TABLE BoardMaster (管理番号 INTEGER, 幅 INTEGER, 備考 TEXT)",
            [(1, 1200, "新"), (2, 1300, "新")])
        import sqlite3
        conn = sqlite3.connect(self.converted)
        conn.execute('CREATE INDEX "IX_Board幅" ON BoardMaster(幅)')
        conn.commit()
        conn.close()
        # 梱包資材マスタ側にも同じ名前の索引(前の表)があっても作り直せる
        conn = sqlite3.connect(self.master)
        conn.execute('CREATE INDEX "IX_Board幅" ON BoardMaster(ボード幅)')
        conn.commit()
        conn.close()

        board = {t["name"]: t for t in self.plan()["tables"]}["BoardMaster"]
        self.assertTrue(board["can_refresh"])
        self.assertEqual(board["action"], "rebuild")
        self.assertEqual(board["removed_columns"], ["ボード幅"])
        self.assertEqual(board["added_columns"], ["幅", "備考"])
        self.assertEqual(board["import_loses"], ["ボード幅"])     # 取り込みで読む列

        self.session.admin = True
        body = self.refresh(["BoardMaster"])
        self.assertTrue(body["ok"], body)
        self.assertEqual(len(body["kept_old"]), 1)
        old = body["kept_old"][0]["old"]
        self.assertTrue(old.startswith("BoardMaster_作り直す前_"), old)
        self.assertIn(old, body["message"])
        self.assertEqual([r[1] for r in self.master_rows("PRAGMA table_info(BoardMaster)")],
                         ["管理番号", "幅", "備考"])
        self.assertEqual(self.master_rows("SELECT * FROM BoardMaster"),
                         [(1, 1200, "新"), (2, 1300, "新")])
        # 前の表は中身ごと残る
        self.assertEqual(self.master_rows(f'SELECT 管理番号, ボード幅 FROM "{old}"'), [(1, 1100)])
        # 索引は作り直した表に付く
        self.assertEqual(self.master_rows(
            "SELECT tbl_name FROM sqlite_master WHERE name = 'IX_Board幅'"), [("BoardMaster",)])
        self.assertEqual(self.master_rows('SELECT * FROM "資材パレット注文管理"'), [(1, "abc")])

    def test_パレットは作り直しても在庫の行を移す(self) -> None:
        self.rename_in_access(
            "PalletMaster",
            "CREATE TABLE PalletMaster (管理番号 INTEGER PRIMARY KEY, 幅, 丈, 位置, 在庫数, 摘要)",
            [(1, 1000, 1000, "", "", "新しい型録"),
             (3, 1200, 2500, "", "", "足された型録"),
             (4, 1000, 1000, "A1", 1, "Access の古い在庫")])
        self.session.admin = True
        body = self.refresh(["PalletMaster"])
        self.assertTrue(body["ok"], body)
        self.assertIn("在庫の行 1行 は作り直した表へ移しました", body["message"])
        rows = self.master_rows("SELECT 幅, 丈, 位置, 在庫数, 摘要 FROM PalletMaster"
                                " ORDER BY 幅, 位置")
        self.assertEqual(rows, [
            (1000, 1000, "", "", "新しい型録"),
            (1000, 1000, "A1", 5, None),             # ツールの在庫(備考は Access で消えた列)
            (1200, 2500, "", "", "足された型録")])
        numbers = [r[0] for r in self.master_rows("SELECT 管理番号 FROM PalletMaster")]
        self.assertEqual(len(numbers), len(set(numbers)), numbers)

    def test_作り直すとAccessに無い索引は付かないと言う(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.master)
        conn.execute("CREATE INDEX IX_共有だけ ON PalletMaster(幅)")
        conn.commit()
        conn.close()
        self.rename_in_access(
            "PalletMaster", "CREATE TABLE PalletMaster (管理番号, 幅, 丈, 位置, 在庫数)",
            [(1, 1000, 1000, "", "")])
        self.session.admin = True
        body = self.refresh(["PalletMaster"])
        self.assertIn("Access に無い索引 IX_共有だけ は付けていません", body["message"])
        self.assertEqual(self.master_rows(
            "SELECT name FROM sqlite_master WHERE name = 'IX_共有だけ'"), [])

    def test_作り直しで入れられない行があれば表は元のまま(self) -> None:
        self.rename_in_access(
            "松板角材", "CREATE TABLE 松板角材 (管理番号 INTEGER, 品名 TEXT NOT NULL)",
            [(1, "a")])
        import sqlite3
        conn = sqlite3.connect(self.converted)
        conn.execute("DROP TABLE 松板角材")
        conn.execute("CREATE TABLE 松板角材 (管理番号 INTEGER, 品名 TEXT CHECK (品名 <> 'x'))")
        conn.execute("INSERT INTO 松板角材 VALUES (1, 'a')")
        conn.commit()
        conn.close()
        from unittest import mock
        from packaging_tool import source_db
        real = source_db.read_table
        with mock.patch.object(source_db, "read_table",
                               lambda p, t: real(p, t) + [{"管理番号": 2, "品名": "x"}]):
            self.session.admin = True
            body = self.refresh(["松板角材"], expect=422)
        self.assertFalse(body["ok"])
        self.assertEqual(self.master_rows("SELECT * FROM 松板角材"), [(1, "残るはず")])
        self.assertEqual(self.master_rows(
            "SELECT name FROM sqlite_master WHERE name LIKE '%作り直す前%'"), [])


class TableBringAccessTests(TableBringTests):
    """Access(.accdb)のまま選べる。いつもの変換ツール(accdb_converter)を中で呼ぶ。"""

    def setUp(self) -> None:
        super().setUp()
        from packaging_tool import table_bring
        table_bring._converted_cache.clear()
        self.addCleanup(table_bring._converted_cache.clear)
        self.conv = self.dir / "accdb_converter"
        self.conv.mkdir()
        (self.conv / "engine.py").write_text(_FAKE_ENGINE, encoding="utf-8")
        (self.conv / "writers.py").write_text(_FAKE_WRITERS, encoding="utf-8")
        from packaging_tool import user_settings
        saved = user_settings.get(config.KEY_CONVERTER_DIR, "")
        self.addCleanup(user_settings.save, config.KEY_CONVERTER_DIR, saved or "")
        user_settings.save(config.KEY_CONVERTER_DIR, str(self.conv))
        self.converted = self.dir / "access" / "資材.accdb"
        self.converted.write_bytes(ACCESS_HEAD)   # 中身は偽の変換ツールが読む

    def calls(self) -> int:
        path = self.conv / "calls.txt"
        return len(path.read_text()) if path.exists() else 0

    def test_中を見ると無い表だけ選べる(self) -> None:
        plan = self.plan()
        self.assertTrue(plan["ok"], plan["message"])
        self.assertIn("Access を変換して読みました", plan["converted"])
        self.assertIn("Access (fake)", plan["converted"])
        by_name = {t["name"]: t for t in plan["tables"]}
        self.assertFalse(by_name["新しい表"]["exists"])
        self.assertTrue(by_name["BoardMaster"]["exists"])
        # Access の内部の表は出さない
        self.assertNotIn("MSysObjects", by_name)
        self.assertIn("内部の表 1 個は出していません", plan["message"])

    def test_無い表だけ写して今ある表には触らない(self) -> None:
        self.session.admin = True
        self.plan()
        body = self.bring(["新しい表"])
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.master_rows('SELECT 名前 FROM "新しい表" ORDER BY ID'),
                         [("あ",), ("い",)])
        self.assertEqual(self.master_rows("SELECT * FROM BoardMaster"), [(1, 1100)])
        # 中を見る → 持ってくる で、変換は1回だけ
        self.assertEqual(self.calls(), 1)

    def test_もうある表は持ってこない(self) -> None:
        self.session.admin = True
        body = self.bring(["BoardMaster"], expect=409)
        self.assertIn("もうある表は持ってきません", body["error"]["message"])

    def test_内部の表は持ってこない(self) -> None:
        self.session.admin = True
        body = self.bring(["MSysObjects"], expect=400)
        self.assertIn("内部の表は持ってきません", body["error"]["message"])

    def test_文字化けの疑いがある行を数える(self) -> None:
        """access_parser で読むと、まれに1行まるごと読み違える(実物の
        梱包資材マスタ.accdb で PalletMaster 4,133行中1行)。読み違えた欄には
        置き換え文字が混ざるので、それを数えて持ってくる前に知らせる。"""
        plan = self.plan()
        by_name = {t["name"]: t for t in plan["tables"]}
        self.assertEqual(by_name["化けた表"]["suspect"], 1)
        self.assertEqual(by_name["新しい表"]["suspect"], 0)
        self.assertIn("文字化けの疑いがある行があります: 化けた表 1行", plan["message"])
        self.assertTrue(plan["warn"])
        self.assertNotIn("予備の読み方", plan["converted"])

    def test_予備の読み方で読んだら言う(self) -> None:
        from packaging_tool import table_bring
        spare = self.dir / "access" / "予備.accdb"
        spare.write_bytes(ACCESS_HEAD)
        plan = table_bring.plan(str(spare))
        self.assertTrue(plan.ok)
        self.assertIn("予備の読み方(access_parser)で読みました", plan.converted)

    def test_変換の部品は同梱のものを使う(self) -> None:
        """場所を指定させない(現場の声:「使いにくい」)。設定が無ければ同梱のもの。"""
        from packaging_tool import table_bring, user_settings
        user_settings.save(config.KEY_CONVERTER_DIR, "")
        self.assertEqual(table_bring.converter_dir(), table_bring.BUNDLED_CONVERTER)
        for name in ("engine.py", "writers.py", "jet_text_fix.py", "xlsx_writer.py"):
            self.assertTrue((table_bring.BUNDLED_CONVERTER / name).is_file(), name)
        # 設定が違っていても同梱のものへ戻る
        user_settings.save(config.KEY_CONVERTER_DIR, str(self.dir / "無い"))
        self.assertEqual(table_bring.converter_dir(), table_bring.BUNDLED_CONVERTER)

    def test_部品が欠けていたらそう言う(self) -> None:
        from unittest import mock
        from packaging_tool import table_bring, user_settings
        user_settings.save(config.KEY_CONVERTER_DIR, "")
        with mock.patch.object(table_bring, "BUNDLED_CONVERTER", self.dir / "無い"):
            plan = table_bring.plan(str(self.converted))
        self.assertFalse(plan.ok)
        self.assertIn("Access を読む部品(vendor", plan.message)

    def test_場所が違えば覚えない(self) -> None:
        from urllib.parse import quote
        res = self.client.get(
            f"/api/settings/table-bring/plan?path={quote(str(self.converted))}"
            f"&converter={quote(str(self.dir))}", headers=self.auth())
        self.assertIn("変換ツールのフォルダではないようです", res.get_json()["message"])
        from packaging_tool import table_bring
        self.assertEqual(table_bring.converter_dir(), self.conv)

    def test_読み取り部品が無ければそう言う(self) -> None:
        from packaging_tool import table_bring
        broken = self.dir / "access" / "部品無し.accdb"
        broken.write_bytes(ACCESS_HEAD)
        plan = table_bring.plan(str(broken))
        self.assertFalse(plan.ok)
        self.assertIn("Access を読む部品が見つかりません", plan.message)
        self.assertIn("_libs", plan.message)

    def test_部品はあって読めないときは部品が無いと言わない(self) -> None:
        """現場で出た形: 同梱の access_parser は動いたが「Failed to parse DB file header」。
        それを「部品が見つかりません」と言い、Python を3つ試して3回とも同じ失敗をしていた。"""
        from unittest import mock
        from packaging_tool import table_bring
        bad = self.dir / "access" / "読めない.accdb"
        bad.write_bytes(ACCESS_HEAD)
        with mock.patch.object(table_bring, "_pythons",
                               return_value=[[sys.executable], [sys.executable, "-B"]]):
            plan = table_bring.plan(str(bad))
        self.assertFalse(plan.ok)
        self.assertNotIn("部品が見つかりません", plan.message)
        self.assertIn("読めませんでした", plan.message)
        self.assertIn("Failed to parse DB file header", plan.message)
        self.assertEqual(self.calls(), 1)            # ほかの Python では試し直さない

    def test_名前がAccessでも中身で見分ける(self) -> None:
        from packaging_tool import table_bring
        folder = self.dir / "access"
        cases = {
            "空.accdb": (b"", "は空です"),
            "零.accdb": (b"\x00" * 4096, "頭が 0 ばかり"),
            "表計算.accdb": (b"PK\x03\x04" + b"x" * 100, "zip の形"),
            "古い表計算.mdb": (b"\xd0\xcf\x11\xe0" + b"x" * 100, "Office の別の形"),
            "別物.accdb": (b"hello world, not access", "Access のファイルではないようです"),
        }
        for name, (data, said) in cases.items():
            with self.subTest(name=name):
                (folder / name).write_bytes(data)
                plan = table_bring.plan(str(folder / name))
                self.assertFalse(plan.ok)
                self.assertIn(said, plan.message)
                self.assertNotIn("部品", plan.message)
        self.assertEqual(self.calls(), 0)            # 変換ツールは呼ばない

    def test_名前がAccessで中身がsqlite3ならそのまま読む(self) -> None:
        from packaging_tool import table_bring
        inside = self.dir / "access" / "中身は.accdb"
        conn = sqlite3.connect(inside)
        conn.execute('CREATE TABLE "新しい表" (ID INTEGER, 名前 TEXT)')
        conn.commit()
        conn.close()
        plan = table_bring.plan(str(inside))
        self.assertTrue(plan.ok, plan.message)
        self.assertIn("中身は sqlite3", plan.converted)
        self.assertEqual(self.calls(), 0)

    def test_参照でAccessのファイルが見える(self) -> None:
        from urllib.parse import quote
        folder = quote(str(self.converted.parent))
        plain = self.client.get(f"/api/fs/list?path={folder}", headers=self.auth()).get_json()
        access = self.client.get(f"/api/fs/list?path={folder}&access=1",
                                 headers=self.auth()).get_json()
        self.assertNotIn("資材.accdb", [f["name"] for f in plain["files"]])
        self.assertIn("資材.accdb", [f["name"] for f in access["files"]])

    def test_いまの梱包資材マスタそのものは選べない(self) -> None:
        super().test_いまの梱包資材マスタそのものは選べない()

    def test_画面に段がある(self) -> None:
        html = self.client.get("/settings").get_data(as_text=True)
        # 変換ツールの場所は訊かない。落とす場所がある
        self.assertNotIn('id="bringConverter"', html)
        self.assertIn('id="bringDrop"', html)
        self.assertIn('id="bringOpenDrop"', html)

    def test_落としたファイルを受け取る(self) -> None:
        import io
        res = self.client.post(
            "/api/settings/table-bring/upload", headers=self.auth(),
            data={"file": (io.BytesIO(self.converted.read_bytes()), "資材.accdb")},
            content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200, res.get_json())
        saved = Path(res.get_json()["path"])
        self.assertEqual(saved.name, "資材.accdb")
        self.assertEqual(saved.read_bytes(), self.converted.read_bytes())
        # 受け取ったものをそのまま読める
        from urllib.parse import quote
        plan = self.client.get(f"/api/settings/table-bring/plan?path={quote(str(saved))}",
                               headers=self.auth()).get_json()
        self.assertTrue(plan["ok"], plan["message"])

    def test_知らない種類のファイルは受け取らない(self) -> None:
        import io
        res = self.client.post(
            "/api/settings/table-bring/upload", headers=self.auth(),
            data={"file": (io.BytesIO(b"x"), "../../悪い.exe")},
            content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)
        self.assertIn("落としてください", res.get_json()["error"]["message"])


class BehaviorSaveTests(DataWebTestCase):
    """「動作の設定を保存」。**押しても何も起きなかった**(押したときの処理が無かった)。

    配った先の端末は拠点を1台ずつ決める(配布設定に入れない項目)ので、ここが
    動かないと配ったあとに困る。
    """

    def setUp(self) -> None:
        super().setUp()
        from packaging_tool import user_settings
        saved = {k: user_settings.get(k) for k in
                 (user_settings.KEY_POSITION, config.KEY_SPEC_SHEET_URL)}
        self.addCleanup(lambda: [user_settings.save(k, v if v is not None else "")
                                 for k, v in saved.items()])

    def test_ボタンに保存の処理がある(self) -> None:
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "views" / "settings.js").read_text(encoding="utf-8")
        head = js[js.index("el.saveBehavior.addEventListener"):]
        body = head[:head.index("});")]
        self.assertIn("/api/settings/save", body)
        for key in ("auto_import", "spec_sheet_url", "position"):
            self.assertIn(key, body)

    def test_拠点が未登録なら未登録と出す(self) -> None:
        from packaging_tool import user_settings
        user_settings.save(user_settings.KEY_POSITION, "")
        state = self.client.get("/api/settings/state", headers=self.auth()).get_json()
        self.assertEqual(state["position"], "")
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('<option value="" selected>未登録</option>', html)

    def test_拠点を選ばずに保存してもほかは保存される(self) -> None:
        res = self.client.post("/api/settings/save", headers=self.auth(),
                               json={"auto_import": True,
                                     "spec_sheet_url": "http://x/{no}"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["spec_sheet_url"], "http://x/{no}")
