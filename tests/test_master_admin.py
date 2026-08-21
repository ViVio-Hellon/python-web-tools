"""梱包資材マスタの管理(確認と修正)のテスト。

【ここで守りたいこと】
1. **書き先は取り込み元ただ1つ。** 手元を直しても次の取り込みで消える
2. **書いたら手元も追いつく。** 直したのに効かない、を作らない
3. **直せる表と直せる人を、断りの種別で言い分ける。** 文言から
   推し量らせない(設計.md §1)
4. **取り込み元に無い列は出さない。** 打ててしまうと保存の瞬間に断られる
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import (access_control, config, db,  # noqa: E402
                            master_admin, modes, selection_session)


def make_source(directory: Path) -> Path:
    """取り込み元の写し。**業務で直す表**をひととおり持たせる。"""
    path = directory / config.MATERIAL_DB_NAME
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE PalletMaster (
            管理番号 INTEGER PRIMARY KEY, 幅 INTEGER, 丈 INTEGER,
            巾適合min INTEGER, 巾適合max INTEGER,
            丈適合min INTEGER, 丈適合max INTEGER,
            業界 TEXT, 記号 TEXT, 位置 TEXT, 在庫数 INTEGER,
            リスト管理 TEXT, 桁数 INTEGER, 脚数 INTEGER,
            コード TEXT, 単位 TEXT, 備考 TEXT, 更新日時 TEXT);
        CREATE TABLE BoardMaster (
            管理番号 INTEGER PRIMARY KEY, ボード幅 INTEGER, ボード丈 INTEGER,
            ボードタイプ TEXT, データラベル TEXT);
        CREATE TABLE PalletPatterns (
            管理番号 INTEGER PRIMARY KEY, パレット幅 INTEGER, パレット丈 INTEGER,
            製品幅 INTEGER, 製品丈 INTEGER, 登録日時 TEXT, 更新日時 TEXT,
            使用回数 INTEGER);
        CREATE TABLE アクセス権限 (
            管理番号 INTEGER PRIMARY KEY, ログインID TEXT, PC名 TEXT,
            権限 TEXT, 有効 INTEGER, 備考 TEXT);
    """)
    conn.execute("INSERT INTO PalletMaster (幅,丈,位置,業界,更新日時)"
                 " VALUES (1100,2000,'A-1','一般','2026-01-01 00:00:00')")
    conn.executemany("INSERT INTO BoardMaster (ボード幅,ボード丈,ボードタイプ)"
                     " VALUES (?,?,?)",
                     [(900, 1800, "ハードボード"), (1200, 2400, "IKボード")])
    conn.execute("INSERT INTO PalletPatterns (パレット幅,パレット丈,製品幅,製品丈,"
                 "登録日時,更新日時,使用回数)"
                 " VALUES (1100,2000,1050,1900,'2026-01-01','2026-01-01',3)")
    conn.commit()
    conn.close()
    return path


class MasterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="master_"))
        self.src = make_source(self.dir)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)

    def rows(self, table: str) -> list[dict]:
        return master_admin.page(self.src, table).rows

    def source_rows(self, table: str) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.src)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(f'SELECT * FROM "{table}"').fetchall()
        finally:
            conn.close()


# ==================================================================
# 見る
# ==================================================================
class BrowseTests(MasterTestCase):
    def test_直せない表も一覧に出す(self) -> None:
        """出さないと「あるはずの表が無い」に見える。確認はどの表でもできる。"""
        found = {t.table: t for t in master_admin.tables(self.src)}
        self.assertTrue(found["PalletMaster"].editable)
        self.assertFalse(found["PalletPatterns"].editable)

    def test_直せない表には理由が付く(self) -> None:
        """**黙って外さない。** なぜここでは直せないのかを言う。"""
        found = {t.table: t for t in master_admin.tables(self.src)}
        self.assertIn("実績から貯まる", found["PalletPatterns"].why)

    def test_行数を出す(self) -> None:
        """押す前に、その先に何があるかが分かるように(§2.4)。"""
        found = {t.table: t.rows for t in master_admin.tables(self.src)}
        self.assertEqual(found["BoardMaster"], 2)

    def test_行は取り込み元から読む(self) -> None:
        """手元の写しを見せると、書けていなくても直ったように見える。"""
        page = master_admin.page(self.src, "PalletMaster")
        self.assertEqual(page.total, 1)
        self.assertEqual(page.rows[0]["位置"], "A-1")
        self.assertIn(master_admin.ROW_KEY, page.rows[0])

    def test_どの列でも絞り込める(self) -> None:
        """どの列に何が入っているかを覚えていなくても引ける。"""
        self.assertEqual(len(master_admin.page(self.src, "BoardMaster",
                                               query="IK").rows), 1)
        self.assertEqual(len(master_admin.page(self.src, "BoardMaster",
                                               query="2400").rows), 1)
        self.assertEqual(len(master_admin.page(self.src, "BoardMaster",
                                               query="無い").rows), 0)

    def test_出しきれない分は数で言う(self) -> None:
        """**黙って切らない。** 切ると「これで全部だ」と読める。"""
        page = master_admin.page(self.src, "BoardMaster", limit=1)
        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.total, 2)
        self.assertIn("ほか 1件", page.note)

    def test_取り込み元が無ければ理由を出す(self) -> None:
        page = master_admin.page(None, "PalletMaster")
        self.assertIn("見つかりません", page.error)


class ColumnTests(MasterTestCase):
    def test_型は手元のスキーマから来る(self) -> None:
        kinds = {c.name: c.kind
                 for c in master_admin.columns(self.conn, "PalletMaster")}
        self.assertEqual(kinds["幅"], "int")
        self.assertEqual(kinds["業界"], "text")

    def test_既定値の無い列だけが必須(self) -> None:
        need = {c.name: c.required
                for c in master_admin.columns(self.conn, "PalletMaster")}
        self.assertTrue(need["幅"])          # NOT NULL・既定なし
        self.assertFalse(need["在庫数"])      # NOT NULL だが既定 0

    def test_日時は空欄で入る(self) -> None:
        """手で打たせる意味が無く、打ち間違いだけが増える。"""
        stamp = {c.name: c.stamp
                 for c in master_admin.columns(self.conn, "PalletMaster")}
        self.assertTrue(stamp["更新日時"])
        self.assertFalse(master_admin.columns(self.conn, "PalletMaster")[0].stamp)

    def test_取り込み元に無い列は出さない(self) -> None:
        """打ててしまうと、保存の瞬間に「そんな列は無い」と断られる。"""
        names = [c.name for c in master_admin.columns(
            self.conn, "PalletMaster", ["幅", "丈", "位置"])]
        self.assertEqual(names, ["幅", "丈", "位置"])

    def test_管理番号は出さない(self) -> None:
        """手元で振り直す番号なので、取り込み元の行を指さない。"""
        names = [c.name for c in master_admin.columns(self.conn, "BoardMaster")]
        self.assertNotIn("管理番号", names)


class ColumnMismatchWhyTests(unittest.TestCase):
    """表はあるのに、列名が想定と違って1つも打ち込めないときの案内。

    現場の声:「1行足す」を押しても入力欄が1つも出てこない。原因は
    取り込み元にその表はあるが、列名がこのツールの想定
    (`import_specs.IMPORT_SPECS`)と一致していないこと。無言で空の
    編集窓が出るだけでは気づけないので、理由を言葉にする。
    """

    def test_1つも一致しなければ理由を返す(self) -> None:
        why = master_admin.column_mismatch_why(
            "アクセス権限", ["id", "LoginID", "PCName", "Permission"])
        self.assertIn("列名が想定と違う", why)
        self.assertIn("ログインID", why)          # 想定の列名を出す
        self.assertIn("LoginID", why)             # 実際の列名も出す

    def test_1つでも一致すれば理由を返さない(self) -> None:
        """一部一致は「列名が違う」ではなく別の状況(その列だけ打ち込める)。"""
        why = master_admin.column_mismatch_why(
            "アクセス権限", ["ログインID", "PC名", "Permission", "有効", "備考"])
        self.assertEqual(why, "")

    def test_想定の列を持たない表では理由を返さない(self) -> None:
        """`IMPORT_SPECS` に無い表(直せない表)は、この理由の対象外。"""
        self.assertEqual(master_admin.column_mismatch_why("知らない表", []), "")


# ==================================================================
# 直す
# ==================================================================
class EditTests(MasterTestCase):
    def key(self, table: str, index: int = 0):
        return self.rows(table)[index][master_admin.ROW_KEY]

    def test_書くのは取り込み元(self) -> None:
        """手元を直しても次の取り込みで消える。元を直す。"""
        result = master_admin.save_row(
            self.conn, "PalletMaster", self.key("PalletMaster"),
            {"位置": "B-2"}, path=self.src)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.source_rows("PalletMaster")[0]["位置"], "B-2")

    def test_書いたら手元も追いつく(self) -> None:
        """**直したのに効かない**が一番たちが悪い。"""
        master_admin.save_row(self.conn, "PalletMaster",
                              self.key("PalletMaster"), {"位置": "B-2"},
                              path=self.src)
        got = self.conn.execute("SELECT 位置 FROM PalletMaster").fetchone()
        self.assertEqual(got["位置"], "B-2")

    def test_パレットは適合範囲も計算し直す(self) -> None:
        """寸法を直したら適合範囲も変わる。取り込みと同じ後始末をする。"""
        master_admin.save_row(self.conn, "PalletMaster",
                              self.key("PalletMaster"), {"幅": "1200"},
                              path=self.src)
        got = self.conn.execute(
            "SELECT 幅, 巾適合min, 巾適合max FROM PalletMaster").fetchone()
        self.assertEqual(got["幅"], 1200)
        # 取り込み元は空だった。計算し直されて、載る製品の幅の幅になる
        self.assertLess(0, got["巾適合min"])
        self.assertLess(got["巾適合min"], got["巾適合max"])
        self.assertLess(got["巾適合max"], got["幅"])

    def test_送っていない列は触らない(self) -> None:
        """一部だけ送られたときに、送っていない項目を消さない。"""
        master_admin.save_row(self.conn, "PalletMaster",
                              self.key("PalletMaster"), {"位置": "B-2"},
                              path=self.src)
        self.assertEqual(self.source_rows("PalletMaster")[0]["業界"], "一般")

    def test_足せる(self) -> None:
        result = master_admin.add_row(
            self.conn, "BoardMaster",
            {"ボード幅": "1500", "ボード丈": "3000", "ボードタイプ": "プロテックボード"},
            path=self.src)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(self.source_rows("BoardMaster")), 3)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM BoardMaster").fetchone()[0], 3)

    def test_消せる(self) -> None:
        result = master_admin.delete_row(
            self.conn, "BoardMaster", self.key("BoardMaster"), path=self.src)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(self.source_rows("BoardMaster")), 1)

    def test_日時は空欄なら今が入る(self) -> None:
        master_admin.save_row(self.conn, "PalletMaster",
                              self.key("PalletMaster"), {"更新日時": ""},
                              path=self.src)
        self.assertNotEqual(self.source_rows("PalletMaster")[0]["更新日時"],
                            "2026-01-01 00:00:00")


class RefuseTests(MasterTestCase):
    """**断りの種別で言い分ける。** 文言から推し量らせない。"""

    def key(self):
        return self.rows("PalletMaster")[0][master_admin.ROW_KEY]

    def test_型が違えば断る(self) -> None:
        result = master_admin.save_row(self.conn, "PalletMaster", self.key(),
                                       {"幅": "ひろい"}, path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_BAD_VALUE)
        self.assertIn("幅", result.message)

    def test_必須を空にすれば断る(self) -> None:
        result = master_admin.add_row(
            self.conn, "BoardMaster",
            {"ボード幅": "", "ボード丈": "1800", "ボードタイプ": "IK"}, path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_BAD_VALUE)

    def test_直す表でなければ断る(self) -> None:
        result = master_admin.save_row(self.conn, "PalletPatterns", 1,
                                       {"使用回数": "9"}, path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_EDITABLE)

    def test_行がもう無ければ断る(self) -> None:
        """一覧を出したあとに誰かが消した。押した人には見えていない事実。"""
        result = master_admin.save_row(self.conn, "PalletMaster", 9999,
                                       {"位置": "X"}, path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NO_ROW)

    def test_取り込み元に届かなければ断る(self) -> None:
        result = master_admin.save_row(
            self.conn, "PalletMaster", 1, {"位置": "X"},
            path=None if _no_master() else self.dir / "無い.sqlite3")
        self.assertIn(result.reason, (master_admin.REFUSE_NO_SOURCE,
                                      master_admin.REFUSE_WRITE_FAILED))

    def test_断ったときは何も書いていない(self) -> None:
        master_admin.save_row(self.conn, "PalletMaster", self.key(),
                              {"幅": "ひろい"}, path=self.src)
        self.assertEqual(self.source_rows("PalletMaster")[0]["幅"], 1100)

    def test_表がまだ無ければ入れる値がありませんとは言わない(self) -> None:
        """現場の声:「1行足す」を押すと『入れる値がありません』と出る。

        原因は、取り込み元に表そのものが無いのに `_ready` が通してしまい、
        `_clean` がどの値も「取り込み元に無い列」として黙って弾いていた
        こと。表が無いことが本当の理由なので、その言葉で断る。
        """
        source_without(self.src, "アクセス権限")
        result = master_admin.add_row(
            self.conn, "アクセス権限",
            {"ログインID": "x", "PC名": "y", "権限": "mode:field",
             "有効": "1", "備考": ""}, path=self.src)
        self.assertFalse(result.ok)
        self.assertNotEqual(result.message, "入れる値がありません。")
        self.assertIn("まだ取り込み元にありません", result.message)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_CREATABLE)

    def test_列名が違う表も入れる値がありませんとは言わない(self) -> None:
        from tests.test_master_admin import source_with_mismatched_columns
        source_with_mismatched_columns(self.src, "アクセス権限")
        result = master_admin.add_row(
            self.conn, "アクセス権限",
            {"ログインID": "x", "PC名": "y", "権限": "mode:field",
             "有効": "1", "備考": ""}, path=self.src)
        self.assertFalse(result.ok)
        self.assertNotEqual(result.message, "入れる値がありません。")
        self.assertIn("列名が想定と違う", result.message)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_CREATABLE)


def _no_master() -> bool:
    from packaging_tool import data_sync
    try:
        return data_sync.find_material_db() is None
    except OSError:                              # pragma: no cover - 共有に届かない
        return True


class PermissionTests(MasterTestCase):
    """**権限で分ける。** いま開いているモードでは分けない。

    mode:material と管理者パスワードは**両方**要る(現場の判断:
    「mode:materialを付与してるからってどのマスタもいじれたら困る」)。
    """

    def setUp(self) -> None:
        super().setUp()
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)

    def grant(self, *codes: str) -> None:
        """アクセス権限マスタを、この端末に効く形で置く。"""
        identity = access_control.current_identity()
        self.conn.execute("DELETE FROM アクセス権限")
        for code in codes:
            self.conn.execute(
                'INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
                " VALUES (?,'',?,1,'')", (identity.login_id, code))
        self.conn.commit()

    def test_資材モードだけでは足りない(self) -> None:
        """パスワードが無ければ、資材モードがあっても直せない。"""
        self.grant(access_control.mode_permission(modes.MATERIAL))
        allowed, why = master_admin.can_edit(self.conn)
        self.assertFalse(allowed)
        self.assertIn("管理者パスワード", why)

    def test_資材モードとパスワードの両方で直せる(self) -> None:
        self.grant(access_control.mode_permission(modes.MATERIAL))
        selection_session.get_session(self.conn).admin = True
        allowed, why = master_admin.can_edit(self.conn, "PalletMaster")
        self.assertTrue(allowed, why)

    def test_現場だけの端末は直せない(self) -> None:
        self.grant(access_control.mode_permission(modes.FIELD))
        allowed, why = master_admin.can_edit(self.conn)
        self.assertFalse(allowed)
        self.assertIn("資材", why)
        # **直し方まで書く。** 何をどこに足せばよいかが分かる
        self.assertIn(access_control.TABLE, why)

    def test_現場だけの端末はパスワードがあっても直せない(self) -> None:
        """アクセス権限マスタ以外は資材モードが要る。パスワードでは代われない。"""
        self.grant(access_control.mode_permission(modes.FIELD))
        selection_session.get_session(self.conn).admin = True
        allowed, why = master_admin.can_edit(self.conn, "PalletMaster")
        self.assertFalse(allowed)
        self.assertIn("資材", why)

    def test_まだ誰も登録されていなければ塞がない(self) -> None:
        """ここを塞ぐと、最初の1行をどこからも入れられなくなる。"""
        self.conn.execute("DELETE FROM アクセス権限")
        self.conn.commit()
        allowed, _why = master_admin.can_edit(self.conn)
        self.assertTrue(allowed)


class AccessTableEscapeHatchTests(MasterTestCase):
    """アクセス権限マスタだけ、常にパスワードも要る。

    書き間違えると誰も直せなくなる表なので(現場の指摘)、
    mode:material を持つ端末であっても、この表だけは管理者パスワードを
    別に入れないと直せない。パスワードさえ分かれば、いまのモードに
    関わらず直せる(mode:material の有無を問わない)。
    """

    def setUp(self) -> None:
        super().setUp()
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)

    def grant(self, *codes: str) -> None:
        identity = access_control.current_identity()
        self.conn.execute("DELETE FROM アクセス権限")
        for code in codes:
            self.conn.execute(
                'INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
                " VALUES (?,'',?,1,'')", (identity.login_id, code))
        self.conn.commit()

    def test_パスワードが無ければ足りない(self) -> None:
        self.grant(access_control.mode_permission(modes.FIELD))
        allowed, why = master_admin.can_edit(self.conn, access_control.TABLE)
        self.assertFalse(allowed)
        self.assertIn("管理者パスワード", why)

    def test_案内は今の入力場所を指す(self) -> None:
        """認証の入力欄は VER2.19.0 で「設定 > パスワード」タブへ移した。
        断り文が古い場所(資材選択画面)を指したままだと、
        言われた場所に何も無くて詰む(現場で実際に踏んだ)。"""
        self.grant(access_control.mode_permission(modes.FIELD))
        _allowed, why = master_admin.can_edit(self.conn, access_control.TABLE)
        self.assertIn("パスワード", why)
        self.assertIn("設定", why)
        self.assertIn("マスタ編集の認証", why)
        self.assertNotIn("資材選択", why)

    def test_資材モードでもパスワードが無ければ直せない(self) -> None:
        """mode:material があっても素通りさせない ── 常に一手間はさむ。"""
        self.grant(access_control.mode_permission(modes.MATERIAL))
        allowed, why = master_admin.can_edit(self.conn, access_control.TABLE)
        self.assertFalse(allowed, why)
        self.assertIn("管理者パスワード", why)

    def test_パスワードだけで直せる_モードは問わない(self) -> None:
        """mode:field すら怪しい状態でも、パスワードさえ分かれば直せる。"""
        self.grant(access_control.mode_permission(modes.FIELD))
        selection_session.get_session(self.conn).admin = True
        allowed, why = master_admin.can_edit(self.conn, access_control.TABLE)
        self.assertTrue(allowed, why)

    def test_資材モードとパスワードの両方でも直せる(self) -> None:
        self.grant(access_control.mode_permission(modes.MATERIAL))
        selection_session.get_session(self.conn).admin = True
        allowed, why = master_admin.can_edit(self.conn, access_control.TABLE)
        self.assertTrue(allowed, why)

    def test_パスワードだけでは他の表は直せない(self) -> None:
        """パスワードで素通りするのはアクセス権限マスタだけ。
        資材課のデータは対象外で、資材モードが要る。"""
        self.grant(access_control.mode_permission(modes.FIELD))
        selection_session.get_session(self.conn).admin = True
        allowed, _why = master_admin.can_edit(self.conn, "PalletMaster")
        self.assertFalse(allowed)

    def test_資材モードだけでは他の表は直せない(self) -> None:
        """他の表は資材モード+パスワードの両方が要る(PermissionTests参照)。"""
        self.grant(access_control.mode_permission(modes.MATERIAL))
        allowed, _why = master_admin.can_edit(self.conn, "PalletMaster")
        self.assertFalse(allowed)

    def test_権限が無ければ書く前に断る(self) -> None:
        self.grant(access_control.mode_permission(modes.FIELD))
        result = master_admin.save_row(
            self.conn, "PalletMaster",
            self.rows("PalletMaster")[0][master_admin.ROW_KEY],
            {"位置": "B-2"}, path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_ALLOWED)
        self.assertEqual(self.source_rows("PalletMaster")[0]["位置"], "A-1")

    def test_権限の判定は表の種別より先(self) -> None:
        """届かないことを先に言うと、権限が無い人に別の原因を読ませる。"""
        self.grant(access_control.mode_permission(modes.FIELD))
        result = master_admin.save_row(self.conn, "PalletPatterns", 1,
                                       {"使用回数": "9"}, path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_ALLOWED)


class GuardTests(unittest.TestCase):
    """全行を書き換える事故を作らない(`source_db` の約束)。"""

    def setUp(self) -> None:
        from packaging_tool import source_db
        self.source_db = source_db
        self.dir = Path(tempfile.mkdtemp(prefix="guard_"))
        self.path = self.dir / "src.sqlite3"
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE 表 (名前 TEXT)")
        conn.executemany("INSERT INTO 表 VALUES (?)", [("あ",), ("い",)])
        conn.commit()
        conn.close()

    def test_条件の無い書き換えは断る(self) -> None:
        with self.source_db.connect(self.path) as src:
            with self.assertRaises(self.source_db.SourceError):
                src.update("表", {"名前": "全部"}, {})

    def test_条件の無い削除は断る(self) -> None:
        with self.source_db.connect(self.path) as src:
            with self.assertRaises(self.source_db.SourceError):
                src.delete("表", {})

    def test_断ったときは1行も動いていない(self) -> None:
        with self.source_db.connect(self.path) as src:
            for call in (lambda: src.update("表", {"名前": "全部"}, {}),
                         lambda: src.delete("表", {})):
                with self.subTest(call=call):
                    with self.assertRaises(self.source_db.SourceError):
                        call()
            self.assertEqual(len(src.query("SELECT * FROM 表")), 2)



# ==================================================================
# 取り込み元に表を作る
#
# アクセス権限は**このツールが後から足した表**で、既存の梱包資材マスタ
# には入っていない。無いあいだはどの端末も現場モードだけになるので、
# 「モードが切り替わらない」という形でしか現れない。作る手立てを
# 画面に置くことと、上流の表まで勝手に作らないことの両方を守る。
# ==================================================================
def source_without(path: Path, table: str) -> None:
    """取り込み元からその表を落とす(まだ足していない状態を作る)。"""
    conn = sqlite3.connect(path)
    conn.execute(f'DROP TABLE "{table}"')
    conn.commit()
    conn.close()


def source_table_names(path: Path) -> set:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


class CreateTableTests(MasterTestCase):
    def setUp(self) -> None:
        super().setUp()
        source_without(self.src, "アクセス権限")

    def test_無い表も一覧に出す(self) -> None:
        """出さないと、モードを決める場所が画面のどこにも見えない。"""
        found = {t.table: t for t in master_admin.tables(self.src)}
        self.assertIn("アクセス権限", found)
        self.assertTrue(found["アクセス権限"].missing)
        self.assertEqual(found["アクセス権限"].rows, 0)

    def test_無い表には理由が付く(self) -> None:
        """**断りではなく、次にできること。**"""
        found = {t.table: t for t in master_admin.tables(self.src)}
        self.assertIn("現場モードだけ", found["アクセス権限"].why)

    def test_開いても故障に見せない(self) -> None:
        view = master_admin.page(self.src, "アクセス権限")
        self.assertTrue(view.missing)
        self.assertEqual(view.error, "")
        self.assertIn("現場モードだけ", view.why)

    def test_作れる(self) -> None:
        result = master_admin.create_table(self.conn, "アクセス権限",
                                           path=self.src)
        self.assertTrue(result.ok, result.message)
        self.assertIn("アクセス権限", source_table_names(self.src))

    def test_作った表は空(self) -> None:
        """最初の1行は普段どおり足す。誰に何を許したかを画面に出すため。"""
        master_admin.create_table(self.conn, "アクセス権限", path=self.src)
        self.assertEqual(self.source_rows("アクセス権限"), [])

    def test_作った表へそのまま足せる(self) -> None:
        """作って終わりにしない。**足せるところまで通ることを確かめる。**"""
        master_admin.create_table(self.conn, "アクセス権限", path=self.src)
        result = master_admin.add_row(
            self.conn, "アクセス権限",
            {"ログインID": "", "PC名": "NLM-PC-042",
             "権限": access_control.mode_permission(modes.MATERIAL),
             "有効": "1", "備考": ""}, path=self.src)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.source_rows("アクセス権限")[0]["PC名"],
                         "NLM-PC-042")

    def test_足した権限はその場で効く(self) -> None:
        """書いたら手元も追いつく。**直したのに効かない**を作らない。"""
        identity = access_control.current_identity()
        master_admin.create_table(self.conn, "アクセス権限", path=self.src)
        master_admin.add_row(
            self.conn, "アクセス権限",
            {"ログインID": "", "PC名": identity.pc_name,
             "権限": access_control.mode_permission(modes.MATERIAL),
             "有効": "1", "備考": ""}, path=self.src)
        grant = access_control.resolve(self.conn)
        self.assertIn(modes.MATERIAL, grant.allowed_modes())

    def test_列は手元のスキーマから来る(self) -> None:
        """書き写すと `schema.sql` を直したとき片方だけ古くなる。"""
        master_admin.create_table(self.conn, "アクセス権限", path=self.src)
        local = [r["name"] for r in self.conn.execute(
            'PRAGMA table_info("アクセス権限")')]
        self.assertEqual(self.source_columns(), local)

    def test_条件を書かない列は空欄で入る(self) -> None:
        """空のログインIDは「どのIDでもよい」という**権限の書き方**。

        手元は空とNULLの2通りを持たないため `NOT NULL DEFAULT ''` だが、
        取り込み元へは空欄をNULLで書く。そのまま写すと、画面は通した
        のに書き込みだけが落ちる。
        """
        master_admin.create_table(self.conn, "アクセス権限", path=self.src)
        conn = sqlite3.connect(self.src)
        try:
            notnull = {r[1]: r[3] for r in conn.execute(
                'PRAGMA table_info("アクセス権限")')}
        finally:
            conn.close()
        self.assertFalse(notnull["ログインID"])   # 空欄で入る
        self.assertFalse(notnull["PC名"])
        self.assertTrue(notnull["権限"])          # 空では意味を成さない

    def source_columns(self) -> list:
        conn = sqlite3.connect(self.src)
        try:
            return [r[1] for r in conn.execute(
                'PRAGMA table_info("アクセス権限")')]
        finally:
            conn.close()

    def test_もうあれば断る(self) -> None:
        """誰かに先を越された。押した人には見えていない事実。"""
        master_admin.create_table(self.conn, "アクセス権限", path=self.src)
        result = master_admin.create_table(self.conn, "アクセス権限",
                                           path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_ALREADY)

    def test_上流の表は作らない(self) -> None:
        """無いのは資材課側の事情。空の表を作ると事情が「0件」に化ける。"""
        source_without(self.src, "PalletMaster")
        result = master_admin.create_table(self.conn, "PalletMaster",
                                           path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_CREATABLE)
        self.assertNotIn("PalletMaster", source_table_names(self.src))

    def test_上流の表は一覧にも出さない(self) -> None:
        source_without(self.src, "PalletMaster")
        found = {t.table for t in master_admin.tables(self.src)}
        self.assertNotIn("PalletMaster", found)

    def test_権限が無ければ作れない(self) -> None:
        """ただし**まだ誰も登録されていないうちは塞がない**(最初の1行)。"""
        self.conn.execute("DELETE FROM アクセス権限")
        self.conn.execute(
            'INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
            " VALUES ('','ほかのPC',?,1,'')",
            (access_control.mode_permission(modes.MATERIAL),))
        self.conn.commit()
        result = master_admin.create_table(self.conn, "アクセス権限",
                                           path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_ALLOWED)


def source_with_mismatched_columns(path: Path, table: str) -> None:
    """その表を、想定と1つも一致しない列名で作り直す(列名違いを再現)。

    sqlite3 ファイルはテキストエディタで直せないため、この状況を
    直す手段はアプリの中にしか無い、という前提を試験でも保つ。
    """
    conn = sqlite3.connect(path)
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(
        f'CREATE TABLE "{table}" '
        '(id INTEGER PRIMARY KEY, LoginID TEXT, PCName TEXT, Permission TEXT)')
    conn.commit()
    conn.close()


class RebuildTableTests(MasterTestCase):
    """表はある。列名が想定と違って1つも打ち込めないとき、作り直す。

    現場の声:「アクセス権限に1行足す」を押しても入力欄が1つも出て
    こない。sqlite3 のファイルはメモ帳では開けず、直す手段が他に無い。
    """

    def setUp(self) -> None:
        super().setUp()
        source_with_mismatched_columns(self.src, "アクセス権限")

    def test_列が1つも一致しなければ作り直せる状態(self) -> None:
        self.assertTrue(
            master_admin.can_rebuild(self.conn, "アクセス権限", self.src))

    def test_列名が一致していれば作り直せる状態ではない(self) -> None:
        """通常のケース(想定どおりの表)では出さない。"""
        source_without(self.src, "アクセス権限")
        master_admin.create_table(self.conn, "アクセス権限", path=self.src)
        self.assertFalse(
            master_admin.can_rebuild(self.conn, "アクセス権限", self.src))

    def test_表が無ければ作り直せる状態ではない(self) -> None:
        """それは `create_table` の仕事。両方が同時に真にならない。"""
        source_without(self.src, "アクセス権限")
        self.assertFalse(
            master_admin.can_rebuild(self.conn, "アクセス権限", self.src))

    def test_作り直せる(self) -> None:
        result = master_admin.rebuild_table(self.conn, "アクセス権限",
                                            path=self.src)
        self.assertTrue(result.ok, result.message)

    def test_作り直すと想定どおりの列名になる(self) -> None:
        master_admin.rebuild_table(self.conn, "アクセス権限", path=self.src)
        names = [r[1] for r in sqlite3.connect(self.src).execute(
            'PRAGMA table_info("アクセス権限")')]
        self.assertEqual(
            set(names),
            {"管理番号", "ログインID", "PC名", "権限", "有効", "備考"})

    def test_元の表は消さず退避する(self) -> None:
        """**中身を失わない。** 黙って消すと取り返しがつかない。"""
        conn = sqlite3.connect(self.src)
        conn.execute("INSERT INTO アクセス権限 (LoginID, Permission) "
                    "VALUES ('old_user', 'old_perm')")
        conn.commit()
        conn.close()

        master_admin.rebuild_table(self.conn, "アクセス権限", path=self.src)

        names = source_table_names(self.src)
        backups = [n for n in names if n.startswith("アクセス権限_旧")]
        self.assertEqual(len(backups), 1)
        conn = sqlite3.connect(self.src)
        try:
            rows = conn.execute(f'SELECT * FROM "{backups[0]}"').fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)   # 退避先に元のデータが残っている

    def test_作り直したらそのまま行を足せる(self) -> None:
        """作って終わりにしない。**足せるところまで通ることを確かめる。**"""
        master_admin.rebuild_table(self.conn, "アクセス権限", path=self.src)
        result = master_admin.add_row(
            self.conn, "アクセス権限",
            {"ログインID": "", "PC名": "NLM-PC-042",
             "権限": access_control.mode_permission(modes.MATERIAL),
             "有効": "1", "備考": ""}, path=self.src)
        self.assertTrue(result.ok, result.message)

    def test_権限が無ければ作り直せない(self) -> None:
        self.conn.execute("DELETE FROM アクセス権限")
        self.conn.execute(
            'INSERT INTO アクセス権限 ("ログインID","PC名","権限","有効","備考")'
            " VALUES ('','ほかのPC',?,1,'')",
            (access_control.mode_permission(modes.MATERIAL),))
        self.conn.commit()
        result = master_admin.rebuild_table(self.conn, "アクセス権限",
                                            path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_ALLOWED)

    def test_上流の表は作り直さない(self) -> None:
        """`PalletMaster` のような上流の表は対象外。"""
        result = master_admin.rebuild_table(self.conn, "PalletMaster",
                                            path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_CREATABLE)

    def test_表がまだ無ければ断る(self) -> None:
        source_without(self.src, "アクセス権限")
        result = master_admin.rebuild_table(self.conn, "アクセス権限",
                                            path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_CREATABLE)
        self.assertIn("取り込み元へ作る", result.message)

    def test_列が一部でも一致していれば断る(self) -> None:
        """一致している列まで巻き込んで消さない。"""
        conn = sqlite3.connect(self.src)
        conn.execute('DROP TABLE "アクセス権限"')
        conn.execute(
            'CREATE TABLE "アクセス権限" '
            '(管理番号 INTEGER PRIMARY KEY, ログインID TEXT, PCName TEXT,'
            ' Permission TEXT)')
        conn.commit()
        conn.close()
        result = master_admin.rebuild_table(self.conn, "アクセス権限",
                                            path=self.src)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_CREATABLE)
        self.assertIn("一部一致", result.message)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
