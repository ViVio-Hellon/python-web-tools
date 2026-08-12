"""包装仕様書の図面(`packaging_tool/spec_sheet.py` と `app/routes/spec_sheet.py`)

【ここで守りたいこと】
社内の閲覧システムは試験から叩けない。叩けるのは「取ってきたものを
どう扱うか」の側なので、そこを固定する:

- 包装仕様NOの形の検査。**キャッシュのファイル名を守る役目も兼ねている**
  ので、`../` の類が通らないことを名指しで確かめる
- 図面ではないもの(HTML)が返ったとき、「取れた」ことにしない
- 一度失敗した仕様NOを、ロットを引くたびに叩き直さない
- 未設定は失敗ではない。**利用者が直せる案内**を返す
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import access_control, config, spec_sheet, user_settings  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"
TOKEN = "test-token-abc123"

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
TEMPLATE = "http://example.invalid/spec?no={no}"


class FakeResponse:
    """`urlopen` の戻りの代わり。`with` に入れられる形にそろえる。"""

    def __init__(self, payload: bytes, content_type: str) -> None:
        self._payload = payload
        self.headers = {"Content-Type": content_type}

    def read(self, _limit: int = -1) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class SpecSheetTestCase(unittest.TestCase):
    """設定もキャッシュも、試験のあいだだけの場所に置く。"""

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self._orig_config_path = config.USER_CONFIG_PATH
        config.USER_CONFIG_PATH = root / "user_config.json"
        self.addCleanup(self._restore_config)

        self._cache = root / "cache" / "spec"
        self._patch = mock.patch.object(spec_sheet, "cache_dir",
                                        lambda: self._cache)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        spec_sheet.reset_fetcher()
        self.addCleanup(spec_sheet.reset_fetcher)

    def _restore_config(self) -> None:
        config.USER_CONFIG_PATH = self._orig_config_path

    def set_url(self, template: str) -> None:
        user_settings.save(config.KEY_SPEC_SHEET_URL, template)


# ==================================================================
# 包装仕様NOの形
# ==================================================================
class NoTests(unittest.TestCase):
    def test_実際の形が通る(self) -> None:
        """閲覧システムの入力欄は maxlength="6"。"1P0001" のような形。"""
        for no in ("1P0001", "1P0113", "ABC123", "1"):
            self.assertTrue(spec_sheet.is_valid_no(no), no)

    def test_パスをさかのぼる形は通さない(self) -> None:
        """この検査がキャッシュのファイル名を守っている。

        英数字だけに限れば、`{no}.bin` がキャッシュ用フォルダの外を
        指しようがない。ここを緩めるときは `_paths` も見直すこと。
        """
        for no in ("../etc", "..", "a/b", "a\\b", "a.b", "", " ", "a" * 13):
            self.assertFalse(spec_sheet.is_valid_no(no), repr(no))


# ==================================================================
# 取得先URLのひな形
# ==================================================================
class TemplateTests(unittest.TestCase):
    def test_未設定は不備ではない(self) -> None:
        """設定しない = この機能を使わない。責める文言を出さない。"""
        self.assertEqual(spec_sheet.template_problem(""), "")
        self.assertEqual(spec_sheet.template_problem("   "), "")

    def test_目印が無ければ言う(self) -> None:
        problem = spec_sheet.template_problem("http://example.invalid/spec")
        self.assertIn(spec_sheet.PLACEHOLDER, problem)

    def test_httpで始まらなければ言う(self) -> None:
        self.assertIn("http", spec_sheet.template_problem("example.invalid/{no}"))

    def test_目印が置き換わる(self) -> None:
        self.assertEqual(spec_sheet.build_url(TEMPLATE, "1P0001"),
                         "http://example.invalid/spec?no=1P0001")

    def test_目印は符号化して入れる(self) -> None:
        """`NO_PATTERN` を通れば実際には変化しないが、穴を残さない。"""
        self.assertEqual(spec_sheet.build_url("http://x/{no}", "a b&c"),
                         "http://x/a%20b%26c")


# ==================================================================
# 取ってきたものの扱い
# ==================================================================
class FetchTests(SpecSheetTestCase):
    def test_画像なら手元に置く(self) -> None:
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(PNG, "image/png")):
            status = spec_sheet.fetch("1P0001", TEMPLATE)

        self.assertEqual(status.state, spec_sheet.STATE_READY)
        self.assertEqual(status.content_type, "image/png")
        self.assertEqual(spec_sheet.read_cached("1P0001"), (PNG, "image/png"))

    def test_文字集合つきのContentTypeも通る(self) -> None:
        """`image/png; charset=binary` のように付いてくることがある。"""
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(PNG, "image/png; charset=binary")):
            status = spec_sheet.fetch("1P0001", TEMPLATE)
        self.assertEqual(status.content_type, "image/png")

    def test_PDFも受ける(self) -> None:
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(b"%PDF-1.4", "application/pdf")):
            self.assertTrue(spec_sheet.fetch("1P0001", TEMPLATE).ready)

    def test_HTMLが返ったら取れたことにしない(self) -> None:
        """一番起こりやすい設定の誤り ── 画面のURLを指している。

        200が返るので「取れた」ように見えるが、出すものは図面ではない。
        ここを通してしまうと、画面に壊れた画像だけが出て理由が分からない。
        """
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(b"<html>", "text/html")):
            status = spec_sheet.fetch("1P0001", TEMPLATE)

        self.assertEqual(status.state, spec_sheet.STATE_ERROR)
        self.assertIn("text/html", status.message)
        self.assertIsNone(spec_sheet.read_cached("1P0001"))

    def test_大きすぎるものは受けない(self) -> None:
        big = b"0" * (spec_sheet.MAX_BYTES + 1)
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(big, "image/png")):
            status = spec_sheet.fetch("1P0001", TEMPLATE)
        self.assertEqual(status.state, spec_sheet.STATE_ERROR)
        self.assertIsNone(spec_sheet.read_cached("1P0001"))

    def test_つながらないときは次にすることを書く(self) -> None:
        error = urllib.error.URLError("接続できません")
        with mock.patch.object(spec_sheet.urllib.request, "urlopen", side_effect=error):
            status = spec_sheet.fetch("1P0001", TEMPLATE)
        self.assertEqual(status.state, spec_sheet.STATE_ERROR)
        # どこを叩いて失敗したのかが分からないと、設定の誤りを直せない
        self.assertIn("example.invalid", status.message)

    def test_拒否されたらログインを促す(self) -> None:
        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch.object(spec_sheet.urllib.request, "urlopen", side_effect=error):
            status = spec_sheet.fetch("1P0001", TEMPLATE)
        self.assertIn("ログイン", status.message)

    def test_httpで始まらないひな形は通信しない(self) -> None:
        """設定ファイルを手で書き換えられた場合の受け止め。

        `urlopen` は `file://` も開ける。画面の検査だけに頼らない。
        """
        with mock.patch.object(spec_sheet.urllib.request, "urlopen") as opener:
            status = spec_sheet.fetch("1P0001", "file:///etc/passwd?{no}")
        opener.assert_not_called()
        self.assertEqual(status.state, spec_sheet.STATE_ERROR)


# ==================================================================
# 裏で取りに行く係
# ==================================================================
class FetcherTests(SpecSheetTestCase):
    def test_未設定なら取りに行かない(self) -> None:
        fetcher = spec_sheet.get_fetcher()
        self.assertFalse(fetcher.prefetch("1P0001"))
        status = fetcher.status("1P0001")
        self.assertEqual(status.state, spec_sheet.STATE_UNSET)
        # 「設定してください」だけでは何をすればよいか分からない
        self.assertIn("設定画面", status.message)

    def test_手元にあれば取りに行かない(self) -> None:
        self.set_url(TEMPLATE)
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(PNG, "image/png")) as opener:
            spec_sheet.get_fetcher().prefetch("1P0001")
            self._settle()
            self.assertEqual(opener.call_count, 1)
            # 2回目。同じ仕様NOのロットは続けて流れる
            self.assertFalse(spec_sheet.get_fetcher().prefetch("1P0001"))
            self.assertEqual(opener.call_count, 1)

    def test_一度失敗したら叩き直さない(self) -> None:
        """届かないサーバを、ロットを引くたびに叩くと待ち時間が積み上がる。"""
        self.set_url(TEMPLATE)
        error = urllib.error.URLError("接続できません")
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               side_effect=error) as opener:
            spec_sheet.get_fetcher().prefetch("1P0001")
            self._settle()
            self.assertEqual(opener.call_count, 1)
            self.assertFalse(spec_sheet.get_fetcher().prefetch("1P0001"))
            self.assertEqual(opener.call_count, 1)

        # 状態を聞けば、失敗した理由がそのまま返る
        self.assertEqual(spec_sheet.get_fetcher().status("1P0001").state,
                         spec_sheet.STATE_ERROR)

    def test_再取得は失敗の記憶を消す(self) -> None:
        """設定を直したあと、利用者が自分で取り直せる。"""
        self.set_url(TEMPLATE)
        error = urllib.error.URLError("接続できません")
        with mock.patch.object(spec_sheet.urllib.request, "urlopen", side_effect=error):
            spec_sheet.get_fetcher().prefetch("1P0001")
            self._settle()

        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(PNG, "image/png")) as opener:
            self.assertTrue(spec_sheet.get_fetcher().refresh("1P0001"))
            self._settle()
            self.assertEqual(opener.call_count, 1)
        self.assertTrue(spec_sheet.get_fetcher().status("1P0001").ready)

    def test_古くなったら取り直す(self) -> None:
        self.set_url(TEMPLATE)
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(PNG, "image/png")):
            spec_sheet.get_fetcher().prefetch("1P0001")
            self._settle()

        # 取得時刻を賞味期限より前に巻き戻す
        _, meta_path = spec_sheet._paths("1P0001")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["fetched_at"] -= spec_sheet.CACHE_TTL_SEC + 1
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(PNG, "image/png")) as opener:
            self.assertTrue(spec_sheet.get_fetcher().prefetch("1P0001"))
            self._settle()
            self.assertEqual(opener.call_count, 1)

    def test_出せるときだけ画像のURLを返す(self) -> None:
        self.set_url(TEMPLATE)
        loading = spec_sheet.to_dict(
            spec_sheet.Status(no="1P0001", state=spec_sheet.STATE_LOADING))
        self.assertEqual(loading["image_url"], "")

        ready = spec_sheet.to_dict(
            spec_sheet.Status(no="1P0001", state=spec_sheet.STATE_READY))
        self.assertEqual(ready["image_url"], "/api/spec-sheet/1P0001/image")

    def _settle(self) -> None:
        """裏のスレッドが終わるまで待つ。"""
        import threading
        for thread in threading.enumerate():
            if thread.name.startswith("spec-"):
                thread.join(timeout=5)


# ==================================================================
# API
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class SpecSheetApiTests(SpecSheetTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app import create_app
        app = create_app("field", token=TOKEN, port=8714)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}

    def test_形が違えば400(self) -> None:
        """打ち間違いは通信の失敗ではなく入力の形の誤り。"""
        for no in ("a.b", "1234567890123", "%2E%2E"):
            res = self.client.get(f"/api/spec-sheet/{no}", headers=self.auth())
            self.assertEqual(res.status_code, 400, no)

    def test_区切り文字は経路にも届かない(self) -> None:
        """`/` を混ぜてもキャッシュ用フォルダの外は指せない。

        経路の照合が先に弾く(404)。`is_valid_no` はその内側の二重の備え。
        """
        res = self.client.get("/api/spec-sheet/..%2Fetc", headers=self.auth())
        self.assertEqual(res.status_code, 404)

    def test_未設定でも200で理由を返す(self) -> None:
        """設定していないのは異常ではない。画面は案内を出せばよい。"""
        body = self.client.get("/api/spec-sheet/1P0001",
                               headers=self.auth()).get_json()
        self.assertEqual(body["state"], spec_sheet.STATE_UNSET)
        self.assertEqual(body["image_url"], "")

    def test_取れていれば画像が出る(self) -> None:
        self.set_url(TEMPLATE)
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(PNG, "image/png")):
            self.client.get("/api/spec-sheet/1P0001", headers=self.auth())
            FetcherTests._settle(self)

        res = self.client.get("/api/spec-sheet/1P0001/image", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "image/png")
        self.assertEqual(res.data, PNG)
        # 社内システムから来たものをそのまま返すので、推測させない
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")

    def test_ヘッダ無しでもクエリのトークンで引ける(self) -> None:
        """`<img src>` はヘッダを付けられない。

        ここを塞ぐと画像だけが401になり、**画面には壊れた画像が出る**。
        ブラウザで一度そうなったので、契約として固定しておく
        (画面側は `api.js` の `tokenUrl` でこのクエリを付ける)。
        """
        self.set_url(TEMPLATE)
        with mock.patch.object(spec_sheet.urllib.request, "urlopen",
                               return_value=FakeResponse(PNG, "image/png")):
            self.client.get("/api/spec-sheet/1P0001", headers=self.auth())
            FetcherTests._settle(self)

        res = self.client.get(f"/api/spec-sheet/1P0001/image?t={TOKEN}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, PNG)

        # トークンが無ければ通らない。クエリで受けることが穴になっていない
        self.assertEqual(
            self.client.get("/api/spec-sheet/1P0001/image").status_code, 401)

    def test_画面はトークン付きのURLを使う(self) -> None:
        """上の契約の**画面側**。ここが素のURLに戻ると401で画が出ない。

        JSの実行までは試験していないので、そのままのURLを入れていないか
        だけを見る。落ちたら `api.js` の `tokenUrl` を通すこと。
        """
        source = (_ROOT / "app" / "static" / "js" / "views" / "lot.js").read_text(
            encoding="utf-8")
        self.assertIn("tokenUrl(status.image_url)", source)
        self.assertNotIn("node.src = status.image_url", source)

    def test_まだ取れていなければ404(self) -> None:
        res = self.client.get("/api/spec-sheet/1P0001/image", headers=self.auth())
        self.assertEqual(res.status_code, 404)

    def test_資材だけの権限なら無い(self) -> None:
        """図面はロット検索の中でしか使わない。

        ロット検索が無い(現場の権限が無い)端末には、このURLも存在しない
        (隠すのではなく無い ── Phase 5 の役割分けと同じ考え方)。
        """
        from app import create_app
        app = create_app("material", token=TOKEN, port=8724,
                         grant=access_control.grant_of("mode:material"))
        app.config["TESTING"] = True
        client = app.test_client()
        for path in ("/api/spec-sheet/1P0001", "/api/spec-sheet/1P0001/image"):
            res = client.get(path, headers=self.auth())
            self.assertEqual(res.status_code, 404, path)


# ==================================================================
# ロット検索との接続
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class LotHandoffTests(unittest.TestCase):
    def test_包装仕様NOを単独でも渡す(self) -> None:
        """画面が12項目から探さなくてよいように、そのまま渡す。"""
        import sqlite3

        from packaging_tool import db, lot_service
        from packaging_tool.presenters import lot as lot_presenter
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.apply_schema(conn)
        self.addCleanup(conn.close)

        insert_lot(conn)
        insert_hiki(conn)
        insert_odr(conn, 包装仕様NO="1P0113")

        view = lot_presenter.build(lot_service.search_lot(conn, "1234567"))
        self.assertEqual(view.packaging_spec, "1P0113")
        self.assertEqual(lot_presenter.to_dict(view)["packaging_spec"], "1P0113")


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
