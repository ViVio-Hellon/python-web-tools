"""アクセス権限のテスト

【ここで守りたいこと】
権限は、**通ったときより通らなかったときのほうが説明を必要とする**。
「資材モードが選べない」とだけ分かっても、原因がマスタ未取込なのか
登録漏れなのかIDの綴り違いなのかで、次にすることが全部違う。

だからこの試験は「持っている/持っていない」だけでなく、
**理由が出るか**と**書いた人の意図どおりに効かない行を教えるか**を見る。
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
from packaging_tool import db, modes  # noqa: E402

YAMADA = ac.Identity(login_id="yamada", pc_name="NLM-PC-042")


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def grant_row(conn, login="", pc="", permission="mode:material",
              enabled=1, note="") -> None:
    conn.execute(
        'INSERT INTO アクセス権限 (ログインID, "PC名", 権限, 有効, 備考)'
        " VALUES (?,?,?,?,?)", (login, pc, permission, enabled, note))
    conn.commit()


# ==================================================================
# 権限の一覧
# ==================================================================
class PermissionCatalogTests(unittest.TestCase):
    def test_モードの数だけ権限がある(self) -> None:
        """モードを足したら権限も増える。**一覧を2か所に持たない。**"""
        codes = {p.code for p in ac.permissions()}
        self.assertEqual(codes, {ac.mode_permission(m) for m in modes.KEYS})

    def test_文言が付いている(self) -> None:
        """画面に出す言葉はここが唯一の出どころ。"""
        for item in ac.permissions():
            with self.subTest(code=item.code):
                self.assertTrue(item.label)
                self.assertTrue(item.detail)

    def test_知らないコードは知らないと言う(self) -> None:
        self.assertFalse(ac.is_known("mode:なにか"))
        self.assertTrue(ac.is_known(ac.mode_permission(modes.FIELD)))


# ==================================================================
# 該当が無いとき
# ==================================================================
class FallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_未取り込みなら現場だけ(self) -> None:
        """締め出さず、勝手に強い権限も渡さない。

        全部止めると取り込みの失敗で全員が仕事を始められなくなる。
        全部許すと登録し忘れた端末が資材モードに入れてしまう。
        """
        self.conn.execute("DROP TABLE アクセス権限")
        grant = ac.resolve(self.conn, YAMADA)
        self.assertEqual(grant.allowed_modes(), (modes.FIELD,))
        self.assertFalse(grant.has_master)
        self.assertIn("取り込まれていません", grant.reason)

    def test_登録が無ければ現場だけ_理由も出る(self) -> None:
        grant_row(self.conn, login="suzuki")
        grant = ac.resolve(self.conn, YAMADA)
        self.assertEqual(grant.allowed_modes(), (modes.FIELD,))
        self.assertIn("yamada", grant.reason)

    def test_通ったときは理由を出さない(self) -> None:
        """落ちていないのに理由が出ると、何か問題があるように読める。"""
        grant_row(self.conn, login="yamada", permission="mode:field")
        self.assertEqual(ac.resolve(self.conn, YAMADA).reason, "")

    def test_モード以外の権限だけでも現場は使える(self) -> None:
        """**使える画面が1枚も無い状態を作らない。**

        モード以外の権限を足したあと、その権限だけを書いた行を作ると
        起きる。いまは実在しないので、権限の一覧に1つ足して再現する。
        """
        from unittest import mock

        extra = ac.Permission(code="report:print", label="帳票を出せる",
                              detail="試験用", category="帳票")
        with mock.patch.object(ac, "permissions",
                               lambda: ac._mode_permissions() + (extra,)):
            grant_row(self.conn, login="yamada", permission="report:print")
            grant = ac.resolve(self.conn, YAMADA)
        self.assertEqual(grant.allowed_modes(), (modes.FIELD,))
        self.assertIn("report:print", grant.codes)
        self.assertIn("モードの権限が無い", grant.reason)


# ==================================================================
# 突き合わせ
# ==================================================================
class MatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_IDで許す(self) -> None:
        grant_row(self.conn, login="yamada")
        self.assertTrue(ac.resolve(self.conn, YAMADA).allows_mode(modes.MATERIAL))

    def test_PC名で許す(self) -> None:
        grant_row(self.conn, pc="NLM-PC-042")
        self.assertTrue(ac.resolve(self.conn, YAMADA).allows_mode(modes.MATERIAL))

    def test_両方書いてあれば両方一致で許す(self) -> None:
        grant_row(self.conn, login="yamada", pc="NLM-PC-999")
        self.assertFalse(ac.resolve(self.conn, YAMADA).allows_mode(modes.MATERIAL))

    def test_大文字小文字は区別しない(self) -> None:
        """WindowsのIDとPC名は区別しない。ここで区別すると、
        マスタの書き方だけで効いたり効かなかったりする。"""
        grant_row(self.conn, login="YAMADA", pc="nlm-pc-042")
        self.assertTrue(ac.resolve(self.conn, YAMADA).allows_mode(modes.MATERIAL))

    def test_前後の空白は無視する(self) -> None:
        grant_row(self.conn, login="  yamada  ")
        self.assertTrue(ac.resolve(self.conn, YAMADA).allows_mode(modes.MATERIAL))

    def test_無効の行は効かない(self) -> None:
        grant_row(self.conn, login="yamada", enabled=0)
        self.assertFalse(ac.resolve(self.conn, YAMADA).allows_mode(modes.MATERIAL))

    def test_条件の無い行は効かない(self) -> None:
        """それは全員への許可で、権限を設ける意味が消える。"""
        grant_row(self.conn)          # ID も PC名 も空
        self.assertFalse(ac.resolve(self.conn, YAMADA).allows_mode(modes.MATERIAL))

    def test_該当行の和を取る(self) -> None:
        """どちらが勝つかを覚えていないと結果を説明できない、を避ける。"""
        grant_row(self.conn, login="yamada", permission="mode:field")
        grant_row(self.conn, pc="NLM-PC-042", permission="mode:material")
        grant = ac.resolve(self.conn, YAMADA)
        self.assertEqual(set(grant.allowed_modes()), {modes.FIELD, modes.MATERIAL})

    def test_効いた行を返す(self) -> None:
        """なぜこの権限なのかを、設定画面で確かめられるようにする。"""
        grant_row(self.conn, login="yamada", note="資材課")
        matched = ac.resolve(self.conn, YAMADA).matched
        self.assertEqual(len(matched), 1)
        self.assertIn("yamada", matched[0].condition_label())


# ==================================================================
# マスタの問題を黙って無視しない
# ==================================================================
class ProblemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_問題が無ければ何も言わない(self) -> None:
        grant_row(self.conn, login="yamada")
        self.assertEqual(ac.problems(self.conn), [])

    def test_知らない権限コードを教える(self) -> None:
        """打ち間違いは「効かない」としか現れず、原因を探しようがない。"""
        grant_row(self.conn, login="yamada", permission="mode:materal")
        self.assertTrue(any("mode:materal" in p for p in ac.problems(self.conn)))

    def test_打ち間違いは近いコードを示す(self) -> None:
        """**指摘だけでは直せない。** 「知らないコードです」と言われても、
        1文字足りないことは目で見比べないと気づけない(現場の声:
        `mode:materia` と書いた行が効かず、原因に辿り着けなかった)。"""
        grant_row(self.conn, login="yamada", permission="mode:materia")
        said = " ".join(ac.problems(self.conn))
        self.assertIn("mode:materia", said)
        self.assertIn("mode:material", said)      # 書き写せる形で出す

    def test_条件の無い行を教える(self) -> None:
        """書いた人は効いているつもりでいる。"""
        grant_row(self.conn)
        self.assertTrue(any("空の行" in p for p in ac.problems(self.conn)))

    def test_条件の無い行には何を書けばよいかまで出す(self) -> None:
        """空欄のままにした人は「全員に効かせたい」つもりでいる。
        効かせるために何を書けばよいかが無ければ、直しようがない。"""
        grant_row(self.conn)
        said = " ".join(ac.problems(self.conn))
        me = ac.current_identity()
        self.assertIn("どちらか一方でも埋めれば効きます", said)
        if me.login_id:
            self.assertIn(me.login_id, said)

    def test_未取り込みなら問題は出さない(self) -> None:
        """まだ作っていないだけ。それは「問題」ではない。"""
        self.conn.execute("DROP TABLE アクセス権限")
        self.assertEqual(ac.problems(self.conn), [])


# ==================================================================
# 身元
# ==================================================================
class IdentityTests(unittest.TestCase):
    def test_環境変数で差し替えられる(self) -> None:
        """検証のための逃げ道。本番では設定しない。"""
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {ac.ENV_LOGIN: "tanaka",
                                          ac.ENV_HOST: "PC-1"}):
            identity = ac.current_identity()
        self.assertEqual((identity.login_id, identity.pc_name),
                         ("tanaka", "PC-1"))

    def test_表示は誰とどの端末が分かる(self) -> None:
        self.assertIn("yamada", YAMADA.label())
        self.assertIn("NLM-PC-042", YAMADA.label())


class ExplainGrantTests(unittest.TestCase):
    """帯のホバーだけで読める一文(設定画面へ移らなくてよい)。"""

    def setUp(self) -> None:
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_誰が使えるかが出る(self) -> None:
        grant_row(self.conn, pc="NLM-PC-042", permission="mode:material")
        grant = ac.resolve(self.conn, identity=YAMADA)
        text = ac.explain_grant(grant)
        self.assertIn(YAMADA.login_id, text)
        self.assertIn(YAMADA.pc_name, text)

    def test_正しく1つだけ許可でも増やし方が出る(self) -> None:
        """既定へ落ちていなくても(`reason` が空でも)具体的に答える。"""
        grant_row(self.conn, pc="NLM-PC-042", permission="mode:material")
        grant = ac.resolve(self.conn, identity=YAMADA)
        self.assertEqual(grant.reason, "")
        text = ac.explain_grant(grant)
        self.assertIn("マスタ管理", text)
        self.assertIn(ac.mode_permission(modes.FIELD), text)

    def test_PC名だけの行でも誰でもそのPCで使える(self) -> None:
        """空欄は「問わない」。ログインIDが空ならPCの誰にでも効く。"""
        grant_row(self.conn, pc="NLM-PC-042", permission="mode:material")
        other = ac.Identity(login_id="suzuki", pc_name="NLM-PC-042")
        grant = ac.resolve(self.conn, identity=other)
        self.assertTrue(grant.allows_mode(modes.MATERIAL))

    def test_既定へ落ちたときは理由も含む(self) -> None:
        grant = ac.resolve(self.conn, identity=YAMADA)
        text = ac.explain_grant(grant)
        self.assertIn("取り込まれていません", text)
        self.assertIn("マスタ管理", text)


class ResyncTests(unittest.TestCase):
    """`resync()` ── 手元が取り込み元より遅れて残っているときの立て直し。

    マスタ管理から書けば `_follow()` がその場で追いつかせるが、
    それ以外の経路(Access側の変換をやり直す・別端末が同時に書く等)では
    手元だけが古いまま残ることがある。「マスタには正しい行が入っている
    のに切り替わらない」という現場の声はたいていこれが原因だった。
    """

    def setUp(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp(prefix="resync_"))
        self.src = self.dir / "梱包資材マスタ.sqlite3"
        src = sqlite3.connect(self.src)
        src.execute(
            'CREATE TABLE "アクセス権限" '
            '("ログインID" TEXT, "PC名" TEXT, "権限" TEXT,'
            ' "有効" INTEGER, "備考" TEXT)')
        src.commit()
        src.close()
        self.conn = make_db()          # 手元は空(未取り込み)
        self.addCleanup(self.conn.close)

    def add_source_row(self, permission: str) -> None:
        src = sqlite3.connect(self.src)
        src.execute(
            'INSERT INTO "アクセス権限" VALUES (?,?,?,1,"")',
            (YAMADA.login_id, YAMADA.pc_name, permission))
        src.commit()
        src.close()

    def test_取り込み元にはあるが手元が空なら読み直して見つかる(self) -> None:
        self.add_source_row("mode:field")
        self.add_source_row("mode:material")

        before = ac.resolve(self.conn, YAMADA)
        self.assertFalse(before.has_master)
        self.assertEqual(before.allowed_modes(), (modes.FIELD,))

        self.assertTrue(ac.resync(self.conn, path=self.src))

        after = ac.resolve(self.conn, YAMADA)
        self.assertTrue(after.has_master)
        self.assertIn(modes.MATERIAL, after.allowed_modes())
        self.assertIn(modes.FIELD, after.allowed_modes())

    def test_取り込み元にも本当に無ければ読み直しても増えない(self) -> None:
        """読み直しは**同期のずれを直す**だけで、無い権限を作らない。"""
        self.add_source_row("mode:field")   # material の行は入れない

        self.assertTrue(ac.resync(self.conn, path=self.src))
        after = ac.resolve(self.conn, YAMADA)
        self.assertNotIn(modes.MATERIAL, after.allowed_modes())

    def test_取り込み元が見つからなければ黙って諦める(self) -> None:
        """ここでの失敗はモード切替そのものを止める理由にしない。"""
        missing = self.dir / "無い.sqlite3"
        self.assertFalse(ac.resync(self.conn, path=missing))


class ScreenResyncTests(unittest.TestCase):
    """**権限を足したのに切り替えられない、の袋小路。**

    現場の言い方:「一回閉じないと現場資材モード切り替えれません」。

        資材課が取り込み元に `mode:material` の行を足す
        動いているアプリは手元のDBしか見ない  → 権限は増えない
        権限が1つしか無いので、帯にモード切替そのものが出ない
        `set_mode` は断る前に `resync` を呼ぶ ── **が、そこへ行くには
            その出ていないボタンを押すしかない**

    直す仕組みが、それが開けるはずの扉の向こうにあった。閉じて開き直すと
    `auto_import` が手元を追いつかせるので直る ── 現場が見つけた唯一の
    逃げ道がそれだった。

    足りないと分かったその場で一度だけ見に行くようにした。ただし
    **共有フォルダを毎回は叩かない**(遅いので)。
    """

    def setUp(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp(prefix="screensync_"))
        self.src = self.dir / "梱包資材マスタ.sqlite3"
        self.write_source()
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        ac.reset_screen_resync()
        self.addCleanup(ac.reset_screen_resync)

        from packaging_tool import config, user_settings
        self.saved = user_settings.get(config.KEY_MASTER_DB_DIR)
        user_settings.save(config.KEY_MASTER_DB_DIR, str(self.dir))
        self.addCleanup(user_settings.save, config.KEY_MASTER_DB_DIR,
                        self.saved or "")

    def write_source(self, *permissions: str) -> None:
        self.src.unlink(missing_ok=True)
        src = sqlite3.connect(self.src)
        src.execute(
            'CREATE TABLE "アクセス権限" '
            '("ログインID" TEXT, "PC名" TEXT, "権限" TEXT,'
            ' "有効" INTEGER, "備考" TEXT)')
        for permission in permissions:
            src.execute('INSERT INTO "アクセス権限" VALUES (?,?,?,1,"")',
                        (YAMADA.login_id, YAMADA.pc_name, permission))
        src.commit()
        src.close()

    def modes_for(self, identity):
        """`resolve_for_screen` は端末の身元で引くので、そこだけ差し替える。"""
        from unittest import mock
        with mock.patch.object(ac, "current_identity", return_value=identity):
            return ac.resolve_for_screen(self.conn).allowed_modes()

    def test_取り込み元にあれば閉じずに増える(self) -> None:
        """これが本題。**開き直さずに**切替が出るようになる。"""
        self.write_source("mode:field", "mode:material")
        self.assertIn(modes.MATERIAL, self.modes_for(YAMADA))

    def test_手元にも取り込み元にも無ければ増やさない(self) -> None:
        """見に行くだけで、無い権限は作らない。"""
        self.write_source("mode:field")
        self.assertNotIn(modes.MATERIAL, self.modes_for(YAMADA))

    def test_足りていれば見に行かない(self) -> None:
        """**共有フォルダは遅い。** 用が無いのに毎回叩かない。"""
        from unittest import mock

        self.write_source("mode:field", "mode:material")
        self.modes_for(YAMADA)                    # 1回目でそろう
        with mock.patch.object(ac, "resync") as spy:
            for _ in range(10):
                self.modes_for(YAMADA)
        spy.assert_not_called()

    def test_取り込み元が変わらなければ何度も読み直さない(self) -> None:
        self.write_source("mode:field")           # 足りないまま
        from unittest import mock

        self.modes_for(YAMADA)                    # 1回目は見に行く
        with mock.patch.object(ac, "resync", return_value=False) as spy:
            for _ in range(10):
                self.modes_for(YAMADA)
            self.assertEqual(spy.call_count, 0,
                             "取り込み元が変わっていないのに読み直しています")

    def test_取り込み元が変われば読み直す(self) -> None:
        """資材課が書いた直後に効く。**そこが効かないと元の木阿弥。**"""
        self.write_source("mode:field")
        self.assertNotIn(modes.MATERIAL, self.modes_for(YAMADA))

        self.write_source("mode:field", "mode:material")   # 書き換わった
        self.assertIn(modes.MATERIAL, self.modes_for(YAMADA))

    def test_取り込み元に届かなくても断らない(self) -> None:
        """共有に届かない端末でも、手元にある分で仕事は続く。"""
        self.src.unlink()
        self.assertEqual(self.modes_for(YAMADA), (modes.FIELD,))


class GrantOfTests(unittest.TestCase):
    def test_知らないコードは受け付けない(self) -> None:
        """試験が実在しない権限を仮定していると、直したつもりの
        振る舞いが本番で効かない。"""
        with self.assertRaises(ValueError):
            ac.grant_of("mode:なにか")

    def test_指定した権限だけを持つ(self) -> None:
        grant = ac.grant_of("mode:material")
        self.assertTrue(grant.allows_mode(modes.MATERIAL))
        self.assertFalse(grant.allows_mode(modes.FIELD))


if __name__ == "__main__":
    unittest.main()
