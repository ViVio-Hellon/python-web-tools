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

    def test_条件の無い行を教える(self) -> None:
        """書いた人は効いているつもりでいる。"""
        grant_row(self.conn)
        self.assertTrue(any("空の行" in p for p in ac.problems(self.conn)))

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
