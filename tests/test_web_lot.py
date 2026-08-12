"""ロット検索画面(Web版)のテスト

`packaging_tool/presenters/lot.py` と `app/routes/lot.py` を見る。

【ここで守りたいこと】
判断はぜんぶプレゼンタ側にある。JSは受け取った値を並べるだけなので、
「BOX実績に差し替わっているから赤くする」「7桁そろったか」といった
仕様はこのテストが通るかどうかで決まる。tkinter版とWeb版で表示が
食い違わないよう、並び順も項目名もここで固定する。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import db, lot_service  # noqa: E402
from packaging_tool.presenters import lot as presenter  # noqa: E402

# 仕掛台帳の作り方は `test_lot_service` にある。同じ形を2度書くと、
# 片方だけ直されて「どちらが正しい列構成か」が分からなくなる
from tests.test_lot_service import insert_hiki, insert_lot, insert_odr  # noqa: E402
from tests import _web  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ (pip install -r requirements.txt)"
TOKEN = _web.TOKEN          # 土台と同じものを使う


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


# ==================================================================
# 入力のそろえ方(VBA `TXT_Change`)
# ==================================================================
class NormalizeTests(unittest.TestCase):
    def test_全角は半角になる(self) -> None:
        """テンキーやIMEの状態によっては全角で入ってくる。

        現場に「半角で打ち直してください」と言わせないための処理。
        """
        self.assertEqual(presenter.normalize("４１０２７８１"), "4102781")

    def test_小文字は大文字になる(self) -> None:
        self.assertEqual(presenter.normalize("a1b2c3d"), "A1B2C3D")

    def test_前後の空白は落とす(self) -> None:
        self.assertEqual(presenter.normalize("  4102781  "), "4102781")

    def test_空でも落ちない(self) -> None:
        self.assertEqual(presenter.normalize(""), "")
        self.assertEqual(presenter.normalize(None), "")

    def test_7桁そろったら検索する(self) -> None:
        """VBA `Page1_OnTxtLotNoChanged`。ボタンを押させない。"""
        self.assertTrue(presenter.is_complete("4102781"))

    def test_足りない間は検索しない(self) -> None:
        for text in ("", "4", "410278", "41027811"):
            with self.subTest(text=text):
                self.assertFalse(presenter.is_complete(text))

    def test_全角7桁も7桁と数える(self) -> None:
        """そろえてから数える。見た目が7桁なら検索が走る。"""
        self.assertTrue(presenter.is_complete("４１０２７８１"))

    def test_桁数はlot_serviceに合わせる(self) -> None:
        """VBA `txtLotNo.maxLength = 7`。2か所に数字を書かない。"""
        self.assertEqual(len("4102781"), lot_service.LOT_NO_LENGTH)


# ==================================================================
# ビューモデルの組み立て
# ==================================================================
class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def view(self, lot_no: str = "1234567"):
        return presenter.search(self.conn, lot_no)

    def field(self, key: str) -> presenter.Field:
        return next(f for f in self.view().lot_fields if f.key == key)

    # --- 見つからないとき ---
    def test_未発見でも例外にしない(self) -> None:
        """「無い」は通信の失敗ではなく正しい検索結果。"""
        view = self.view("9999999")
        self.assertFalse(view.found)
        self.assertIn("未発見", view.message)

    def test_未発見なら試験指示票は要否不明(self) -> None:
        """要でも不要でもない。空欄にすると「不要」と読まれる。"""
        self.assertEqual(self.view("9999999").test_slip,
                         lot_service.TEST_SLIP_UNKNOWN)

    def test_未発見なら資材展開できない(self) -> None:
        self.assertFalse(self.view("9999999").can_expand)

    # --- 試験指示票 ---
    def test_要否の根拠を出す(self) -> None:
        """「要」とだけ出しても、現場は正しいかどうかを確かめられない。

        この判定は過去に**誤った前提で実装されていた**ことがあり
        (試験NOで判定していたが、VBAの実クエリに試験NO列は無かった)、
        根拠が見えていれば早く気づけたはずのもの。
        """
        insert_lot(self.conn, 品質グレード_表面処理="A", 製造板厚=2.0,
                   用途コード="T100")
        view = self.view()
        self.assertEqual(view.test_slip, lot_service.TEST_SLIP_NEEDED)
        self.assertEqual(len(view.test_slip_checks), 3)
        self.assertTrue(all(c["met"] for c in view.test_slip_checks))

    def test_根拠に実際の値も出す(self) -> None:
        """条件だけでは、そのロットがどうだったのかが分からない。"""
        insert_lot(self.conn, 品質グレード_表面処理="A", 製造板厚=2.0,
                   用途コード="T100")
        values = [c["value"] for c in self.view().test_slip_checks]
        self.assertIn("2mm", values)
        self.assertIn("A", values)
        self.assertIn("T100", values)

    def test_成立していない条件も出す(self) -> None:
        """成立したものだけ出すと、見ていない条件があるように読める。"""
        insert_lot(self.conn, 品質グレード_表面処理="A", 製造板厚=10.0,
                   用途コード="K100")
        view = self.view()
        self.assertEqual(view.test_slip, lot_service.TEST_SLIP_NOT_NEEDED)
        self.assertEqual([c["met"] for c in view.test_slip_checks],
                         [False, True, False])

    def test_不要なら理由を書く(self) -> None:
        """表面処理で免除されたのか、どちらも該当しなかったのかは違う。"""
        insert_lot(self.conn, 品質グレード_表面処理="S", 製造板厚=2.0,
                   用途コード="T100")
        note = self.view().test_slip_note
        self.assertIn("S", note)
        self.assertIn("不要", note)

    def test_要なら何をするかを書く(self) -> None:
        insert_lot(self.conn, 品質グレード_表面処理="A", 製造板厚=2.0)
        self.assertIn("先行データ", self.view().test_slip_note)

    def test_判定と根拠が食い違わない(self) -> None:
        """判定式と根拠を別々に書くと、片方だけ直されて食い違う。

        `needs_test_slip` は 表面処理 AND (板厚 OR 用途コード)。
        根拠の3つからこの式を組み立て直して、結論と一致するか見る。
        """
        cases = [
            ("A", 2.0, "T100"), ("S", 2.0, "T100"), ("T", 10.0, "K100"),
            ("A", 10.0, "K100"), ("A", 10.0, "D2X"), ("", 2.0, "K100"),
        ]
        for surface, thickness, yoto in cases:
            with self.subTest(surface=surface, thickness=thickness, yoto=yoto):
                self.conn.execute("DELETE FROM 仕掛ロット")
                insert_lot(self.conn, 品質グレード_表面処理=surface,
                           製造板厚=thickness, 用途コード=yoto,
                           設計_設備コース="AAA")
                view = self.view()
                thin, exempt_ok, yoto_ok = [c["met"] for c in view.test_slip_checks]
                self.assertEqual(view.test_slip_needed,
                                 exempt_ok and (thin or yoto_ok))

    def test_未発見なら根拠も出さない(self) -> None:
        """判定していないものに根拠は無い。"""
        self.assertEqual(self.view("9999999").test_slip_checks, [])

    # --- 項目の並び ---
    def test_ロット情報の並びはVBAどおり(self) -> None:
        """`capLot` の順。データの流れる順なので入れ替えは仕様変更。"""
        insert_lot(self.conn)
        keys = [f.key for f in self.view().lot_fields]
        self.assertEqual(keys, [key for _, key in presenter.LOT_FIELDS])

    def test_受注情報の並びはVBAどおり(self) -> None:
        """`lblOdr(0..7)` の順。"""
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn)
        keys = [f.key for f in self.view().odr_fields]
        self.assertEqual(keys, [key for _, key in presenter.ODR_FIELDS])

    def test_見出しは現場の言葉のまま(self) -> None:
        """「用途コード」を「品目区分」等に言い換えない。"""
        insert_lot(self.conn)
        labels = [f.label for f in self.view().lot_fields]
        self.assertIn("設計_設備コース", labels)
        self.assertIn("オーダー板厚", labels)

    # --- 書式 ---
    def test_板厚は小数3桁(self) -> None:
        insert_lot(self.conn, 製造板厚=6.75)
        value = self.field("thickness").value
        self.assertEqual(value, "6.750")

    def test_板幅板丈は小数1桁(self) -> None:
        insert_lot(self.conn, 製造板幅=1000.0)
        self.assertEqual(self.field("width").value, "1000.0")

    def test_空欄は三点で埋める(self) -> None:
        """空白のままだと「取得できていない」のか「空が正しい」のか分からない。"""
        insert_lot(self.conn, 用途名="")
        self.assertEqual(self.field("yoto_name").value, "---")

    # --- BOX実績の差し替え ---
    def test_BOXなら差し替えた寸法を赤くする(self) -> None:
        """値だけ変えて黙っていると、製造寸法と読み違える。"""
        insert_lot(self.conn, 設計_設備コース="X-GCT-1")
        view = self.view()
        self.assertTrue(view.is_box)
        highlighted = {f.key for f in view.lot_fields if f.highlight}
        self.assertEqual(highlighted, set(presenter.BOX_HIGHLIGHT_FIELDS))

    def test_BOXでもオーダー寸法は赤くしない(self) -> None:
        """差し替わっていないので、強調すると嘘になる。"""
        insert_lot(self.conn, 設計_設備コース="GFS")
        for key in ("order_thickness", "order_width", "order_length"):
            with self.subTest(key=key):
                self.assertFalse(self.field(key).highlight)

    def test_BOXでなければどこも赤くしない(self) -> None:
        insert_lot(self.conn, 設計_設備コース="AAA")
        view = self.view()
        self.assertFalse(view.is_box)
        self.assertEqual([f.key for f in view.lot_fields if f.highlight], [])

    def test_見出しにBOX実績寸法と前工程実績数が出る(self) -> None:
        """VBA は caption 1本に `---` でつないでいた。語はそのまま、
        区切り記号だけ標識に置き換える。"""
        insert_lot(self.conn, 設計_設備コース="GSS", BOX実績_枚本数=7)
        view = self.view()
        self.assertEqual(view.lot_title, "ロット情報 (SIKALOTNOW)")
        texts = [b.text for b in view.lot_badges]
        self.assertIn("BOX実績寸法", texts)
        self.assertIn("前工程実績数 7枚", texts)
        self.assertEqual(view.prev_process_count, 7)

    def test_BOXでなければ寸法の標識は出さない(self) -> None:
        insert_lot(self.conn, 設計_設備コース="AAA")
        self.assertNotIn("BOX実績寸法",
                         [b.text for b in self.view().lot_badges])

    def test_気づかないと手戻りになるものだけ目立たせる(self) -> None:
        """全部を強調すると、強調が効かなくなる。"""
        insert_lot(self.conn, 設計_設備コース="GCT")
        insert_hiki(self.conn)
        insert_odr(self.conn, EX_輸出区分="1")
        view = self.view()
        alerts = {b.text for b in view.lot_badges + view.odr_badges
                  if b.kind == "alert"}
        self.assertEqual(alerts, {"BOX実績寸法", "EX"})

    def test_差し替えの理由を書く(self) -> None:
        """値だけ黙って変えない。どのコースで差し替わったかまで言う。"""
        insert_lot(self.conn, 設計_設備コース="X-GCT-1")
        note = self.view().dimension_note
        self.assertIn("GCT", note)
        self.assertIn("BOX実績寸法", note)

    def test_差し替えていなければ理由も無い(self) -> None:
        insert_lot(self.conn, 設計_設備コース="AAA")
        self.assertEqual(self.view().dimension_note, "")

    # --- まとまり ---
    def test_寸法は製造とオーダーで分ける(self) -> None:
        """12項目を等間隔に並べると「板厚」と「オーダー板厚」を取り違える。"""
        insert_lot(self.conn, 設計_設備コース="AAA")
        by_key = {f.key: f.group for f in self.view().lot_fields}
        self.assertEqual(by_key["thickness"], "製造寸法")
        self.assertEqual(by_key["order_thickness"], "オーダー寸法")

    def test_BOXならまとまりの見出しもBOX実績寸法(self) -> None:
        insert_lot(self.conn, 設計_設備コース="GFS")
        by_key = {f.key: f.group for f in self.view().lot_fields}
        self.assertEqual(by_key["thickness"], "BOX実績寸法")
        # オーダー側は差し替わらないので変わらない
        self.assertEqual(by_key["order_thickness"], "オーダー寸法")

    def test_まとまりは連続する(self) -> None:
        """同じまとまりが飛び飛びになると、区切りが引けない。"""
        insert_lot(self.conn)
        seen, previous = [], None
        for f in self.view().lot_fields:
            if f.group != previous:
                self.assertNotIn(f.group, seen,
                                 f"まとまり {f.group} が分断されています")
                seen.append(f.group)
                previous = f.group

    def test_設備コースのまとまりごと赤くはしない(self) -> None:
        """赤いのは「設計_設備コース」1項目だけ。

        画面はまとまり全体が強調されているときだけ見出しを赤くする。
        コース名まで差し替わったように読ませないため、ここで
        「全部は赤くない」ことを確かめておく。
        """
        insert_lot(self.conn, 設計_設備コース="GCT")
        course = [f for f in self.view().lot_fields if f.group == "設備コース"]
        self.assertTrue(any(f.highlight for f in course))
        self.assertFalse(all(f.highlight for f in course))

    def test_寸法のまとまりは全部赤くなる(self) -> None:
        insert_lot(self.conn, 設計_設備コース="GCT")
        dims = [f for f in self.view().lot_fields if f.group == "BOX実績寸法"]
        self.assertEqual(len(dims), 3)
        self.assertTrue(all(f.highlight for f in dims))

    def test_説明を出す先のまとまりを示す(self) -> None:
        insert_lot(self.conn, 設計_設備コース="GCT")
        view = self.view()
        self.assertEqual(view.dimension_group, "BOX実績寸法")
        self.assertIn(view.dimension_group,
                      {f.group for f in view.lot_fields})

    def test_まとまりは一度に把握できる大きさ(self) -> None:
        """作業記憶に置けるのは4±1。区切っても大きすぎては意味がない。"""
        for name, items in presenter.LOT_FIELD_GROUPS:
            with self.subTest(group=name):
                self.assertLessEqual(len(items), 5)

    def test_平らな並びはVBAのまま(self) -> None:
        """まとまりを付けても順序は動かさない。"""
        self.assertEqual(
            [key for _, key in presenter.LOT_FIELDS],
            ["yoto_code", "yoto_name", "zaishitsu", "choshitsu",
             "thickness", "width", "length",
             "order_thickness", "order_width", "order_length",
             "design_course", "actual_course"])

    # --- 設備コースは1行を丸ごと使う ---
    def test_設備コースは折り返さず1行使う(self) -> None:
        """"HOT PSW ST2 ANF ..." のように長い。2列に押し込むと読めない。"""
        insert_lot(self.conn)
        for key in presenter.COURSE_FIELDS:
            with self.subTest(key=key):
                self.assertTrue(self.field(key).wide)

    def test_短い項目は2列に収める(self) -> None:
        insert_lot(self.conn)
        self.assertFalse(self.field("yoto_code").wide)

    # --- 受注情報 ---
    def test_EXなら見出しに出す(self) -> None:
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn, EX_輸出区分="1")
        view = self.view()
        self.assertTrue(view.is_ex)
        self.assertIn("EX", [b.text for b in view.odr_badges])

    def test_EXでなければ見出しに出さない(self) -> None:
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn, EX_輸出区分="")
        view = self.view()
        self.assertFalse(view.is_ex)
        self.assertNotIn("EX", [b.text for b in view.odr_badges])

    def test_BOXコース名も見出しに出す(self) -> None:
        insert_lot(self.conn, 設計_設備コース="X-GSS-2")
        insert_hiki(self.conn)
        insert_odr(self.conn)
        self.assertIn("GSS", [b.text for b in self.view().odr_badges])

    def test_包装仕様NOだけがリンクになる(self) -> None:
        """開く先があるのは1つだけ。ほかを青くすると押せそうに見える。"""
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn)
        view = self.view()
        linked = {f.key for f in view.odr_fields if f.url}
        self.assertEqual(linked, {"packaging_spec"})

    def test_包装仕様書のURLはVBAと同じ(self) -> None:
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn)
        spec = next(f for f in self.view().odr_fields if f.key == "packaging_spec")
        self.assertEqual(spec.url, presenter.URL_HOSO_SHIYOSHO)

    def test_比重を出す(self) -> None:
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn, 材質_比重=2.7)
        self.assertEqual(self.view().specific_gravity, "2.7")

    # --- 引当情報 ---
    def test_引当は引当番号の昇順(self) -> None:
        """並べ替えは `lot_service` が済ませてある。ここで崩さない。"""
        insert_lot(self.conn)
        for hiki_no in ("60717003", "60717001", "60717002"):
            insert_hiki(self.conn, no=hiki_no, order_no=f"O{hiki_no[-1]}")
        insert_odr(self.conn, order_no="O1")
        numbers = [row["hiki_no"] for row in self.view().hiki]
        self.assertEqual(numbers, ["60717001", "60717002", "60717003"])

    def test_全量の行は数量欄に全量と出す(self) -> None:
        """数量欄には 0 が入っている。そのまま出すと「引当が無い」と読める。"""
        insert_lot(self.conn)
        insert_hiki(self.conn, qty=0.0, adj="")
        row = self.view().hiki[0]
        self.assertEqual(row["quantity"], "0")       # 生の値は 0 のまま
        self.assertEqual(row["quantity_text"], "全量")
        self.assertTrue(row["is_all"])

    def test_数量がある行はそのまま数字(self) -> None:
        insert_lot(self.conn)
        insert_hiki(self.conn, qty=120.0)
        self.assertEqual(self.view().hiki[0]["quantity_text"], "120")

    def test_引当調整NOはそのまま出す(self) -> None:
        insert_lot(self.conn)
        insert_hiki(self.conn, adj="A9")
        self.assertEqual(self.view().hiki[0]["adjust_no"], "A9")

    # --- 資材展開の可否 ---
    def test_寸法が取れていれば資材展開できる(self) -> None:
        insert_lot(self.conn, 製造板幅=1000.0, 製造板丈=2000.0)
        view = self.view()
        self.assertTrue(view.can_expand)
        self.assertEqual(view.expand_reason, "")

    def test_寸法が無ければ理由を添えて止める(self) -> None:
        """押せないボタンを黙って置かない(なぜ押せないかを言う)。"""
        insert_lot(self.conn, 設計_設備コース="AAA", 製造板幅=0.0, 製造板丈=0.0)
        view = self.view()
        self.assertFalse(view.can_expand)
        self.assertIn("製造板幅", view.expand_reason)

    # --- JSON ---
    def test_辞書にしてもJSONにできる(self) -> None:
        import json
        insert_lot(self.conn, 設計_設備コース="GCT")
        insert_hiki(self.conn)
        insert_odr(self.conn)
        json.dumps(presenter.to_dict(self.view()), ensure_ascii=False)

    def test_辞書の鍵はビューモデルと同じだけある(self) -> None:
        """片方だけ増やすと、画面に出ない項目が静かに生まれる。"""
        import dataclasses
        insert_lot(self.conn)
        data = presenter.to_dict(self.view())
        expected = {f.name for f in dataclasses.fields(presenter.LotViewModel)}
        self.assertEqual(set(data), expected)


# ==================================================================
# 画面とAPI
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class LotWebTestCase(unittest.TestCase):
    mode = "field"

    def setUp(self) -> None:
        from app.routes import lot as lot_routes
        from packaging_tool import selection_session, work_context

        # 作業中のロットはプロセスに1つ。試験の間で漏れないようにする
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)

        # 実DBを触らせない。ルートは `from .. import get_db` で
        # 自分の名前空間に取り込んでいるので、ここを差し替えれば足りる
        self.conn = _web.bind_db(self, lot_routes, make_db())
        self.client = _web.make_client(self.mode, ready=False)

    def auth(self) -> dict:
        return _web.auth()

    def get(self, path: str) -> dict:
        res = self.client.get(path, headers=self.auth())
        self.assertEqual(res.status_code, 200, f"{path} が引けません")
        return res.get_json()


class LotPageTests(LotWebTestCase):
    def test_開ける(self) -> None:
        res = self.client.get("/lot")
        self.assertEqual(res.status_code, 200)
        self.assertIn("ロット検索", res.get_data(as_text=True))

    def test_未取り込みなら理由と行き先を出す(self) -> None:
        """空の画面だけ見せると「壊れている」と思われる。

        「引けません」で終わらせず、直せる場所まで連れて行く。
        """
        html = self.client.get("/lot").get_data(as_text=True)
        self.assertIn("仕掛台帳が未取り込みです", html)
        # レールにも `/data` はあるので、案内の中のリンクを見る
        self.assertIn("設定画面へ", html)

    def test_取り込み済みなら案内を出さない(self) -> None:
        insert_lot(self.conn)
        html = self.client.get("/lot").get_data(as_text=True)
        self.assertNotIn("仕掛台帳が未取り込みです", html)

    def test_詳細のブロックが作業順に並ぶ(self) -> None:
        """VBA のフレーム順。ロット情報 → 試験指示票 → 引当情報 → 受注情報。

        画面の作りは変えたが(縦積み → モーダル)、**読む順序は仕様**
        なので変えない。
        """
        html = self.client.get("/lot").get_data(as_text=True)
        # 見出しの位置で見る。`<style>` の中のコメントを拾わないように
        body = html[html.index("<dialog"):]
        order = ["ロット情報", "試験指示票", "引当情報", "受注情報"]
        positions = [body.index(label) for label in order]
        self.assertEqual(positions, sorted(positions),
                         "ブロックの並びがVBAのフレーム順と違います")

    def test_入口は検索欄1つだけ(self) -> None:
        """**番号を打つ専用の欄は置かない。**

        入口が2つあると、番号を知っているときにどちらへ打つのが正しいかを
        利用者が判断しなければならない。7桁を打てば必ず1件になるので、
        検索欄がそのまま速い道になる。
        """
        html = self.client.get("/lot").get_data(as_text=True)
        self.assertNotIn('id="lotNo"', html)
        self.assertIn('id="listText"', html)

    def test_1件で自動確定することを画面で伝える(self) -> None:
        """検索ボタンが無いので、押し忘れたのかと思われないようにする。"""
        from packaging_tool.presenters import lot_list
        insert_lot(self.conn)
        self.assertIn(lot_list.SEARCH_HINT,
                      self.client.get("/lot").get_data(as_text=True))

    def test_詳細の開き方を画面で伝える(self) -> None:
        """ダブルクリックは見ただけでは分からない。覚えている前提にしない。"""
        from packaging_tool.presenters import lot_list
        insert_lot(self.conn)
        self.assertIn(lot_list.ROW_HINT,
                      self.client.get("/lot").get_data(as_text=True))

    def test_取り込んだ列を全部出す(self) -> None:
        """一覧に出ていない列は、1件ずつ詳細を開かないと分からない。

        取り込みが持っている事実は一覧に出しておいて、**目で探せる**
        ようにする。出す列を増やせば絞り込みと並べ替えにも同時に効く。
        """
        from packaging_tool import import_specs, lot_query
        taken = {local for local, _src, _conv
                 in import_specs.LOT_IMPORT_SPECS["仕掛ロット"]}
        shown = {c.source for c in lot_query.COLUMNS}
        self.assertEqual(taken - shown, set(),
                         "取り込んでいるのに一覧に出ていない列があります")

    def test_列は1つも畳まない(self) -> None:
        """全列とも並べ替えに使う。**畳んだ値では見出しを押せない。**"""
        from packaging_tool import lot_browse_session, lot_query
        from packaging_tool.presenters import lot_list
        insert_lot(self.conn)
        view = lot_list.build(lot_browse_session.LotBrowseSession(), self.conn)
        body = view.to_dict()
        self.assertEqual([h["key"] for h in body["headers"]],
                         [c.key for c in lot_query.COLUMNS])
        # 値の数と見出しの数がずれると、列がまるごと1つずれて読める
        for row in body["rows"]:
            self.assertEqual(len(row["values"]), len(body["headers"]))

    def test_資材展開は最初押せない(self) -> None:
        """ロットが決まる前は押しても意味が無い(強制選択機能)。

        押せてしまうと、空のまま資材選択へ移って原因を探すことになる。
        """
        import re
        html = self.client.get("/lot").get_data(as_text=True)
        tag = re.search(r"<button[^>]*id=\"expand\"[^>]*>", html)
        self.assertIsNotNone(tag, "資材展開ボタンが見つかりません")
        self.assertIn("disabled", tag.group(0))

    def test_押せない理由を先に書いておく(self) -> None:
        """JSが動く前から読める。無効なボタンの理由は常に添える。"""
        self.assertIn("先にロットを選んでください",
                      self.client.get("/lot").get_data(as_text=True))


class LotApiTests(LotWebTestCase):
    def test_トークンが要る(self) -> None:
        self.assertEqual(self.client.get("/api/lot/1234567").status_code, 401)

    def test_引ける(self) -> None:
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn)
        body = self.get("/api/lot/1234567")
        self.assertTrue(body["found"])
        self.assertEqual(body["lot_no"], "1234567")
        self.assertEqual(len(body["lot_fields"]), len(presenter.LOT_FIELDS))

    def test_未発見も200で返す(self) -> None:
        """404にすると通信エラーの表示になり、現場が原因を誤解する。"""
        res = self.client.get("/api/lot/9999999", headers=self.auth())
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["found"])

    def test_全角で引いても通る(self) -> None:
        insert_lot(self.conn)
        self.assertTrue(self.get("/api/lot/１２３４５６７")["found"])

    def test_検索するとリボンに載る(self) -> None:
        """画面を移っても見えるように、**サーバが**覚える。"""
        insert_lot(self.conn)
        body = self.get("/api/lot/1234567")
        self.assertEqual(body["ribbon"]["lot"], "1234567")

    def test_別の画面へ移ってもLotが残る(self) -> None:
        """リボンは「覚えさせないための帯」。移るたびに消えるなら意味がない。"""
        insert_lot(self.conn)
        self.get("/api/lot/1234567")
        html = self.client.get("/log").get_data(as_text=True)
        self.assertIn("1234567", html)

    def test_BOXの強調がAPIにも乗る(self) -> None:
        insert_lot(self.conn, 設計_設備コース="GCT")
        body = self.get("/api/lot/1234567")
        highlighted = {f["key"] for f in body["lot_fields"] if f["highlight"]}
        self.assertEqual(highlighted, set(presenter.BOX_HIGHLIGHT_FIELDS))


class ExpandApiTests(LotWebTestCase):
    """資材展開(VBA `Page1_OnBtnHBClick`)。

    押しても何も起きないボタンにしない。製造板幅・板丈を製品サイズとして
    渡し、渡した先へ移る。
    """

    def test_検索前は展開できない(self) -> None:
        res = self.client.post("/api/lot/expand", json={}, headers=self.auth())
        self.assertEqual(res.status_code, 422)
        self.assertIn("先にロットを検索", res.get_json()["error"]["message"])

    def test_寸法が取れていなければ理由を返す(self) -> None:
        insert_lot(self.conn, 設計_設備コース="AAA", 製造板幅=0.0, 製造板丈=0.0)
        self.get("/api/lot/1234567")
        res = self.client.post("/api/lot/expand", json={}, headers=self.auth())
        self.assertEqual(res.status_code, 422)
        self.assertIn("製造板幅", res.get_json()["error"]["message"])

    def test_製品サイズとして渡る(self) -> None:
        insert_lot(self.conn, 設計_設備コース="AAA", 製造板幅=1200.0, 製造板丈=2400.0)
        self.get("/api/lot/1234567")
        body = self.client.post("/api/lot/expand", json={},
                                headers=self.auth()).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["ribbon"]["product"], "1200×2400")

    def test_渡した先へ移る(self) -> None:
        """VBA も `mp.value = 1` でページを切り替えていた。"""
        insert_lot(self.conn, 設計_設備コース="AAA")
        self.get("/api/lot/1234567")
        body = self.client.post("/api/lot/expand", json={},
                                headers=self.auth()).get_json()
        self.assertEqual(body["next"], "/selection")
        self.assertEqual(self.client.get(body["next"]).status_code, 200)

    def test_渡った先で受け取れている(self) -> None:
        """渡した値が資材選択の**製品サイズ欄に入っている**こと。

        「セット」までは押さない(パレットに収まるかの検証は利用者が
        自分で行う)ので、確定値ではなく入力欄に入るのが正しい。
        """
        insert_lot(self.conn, 設計_設備コース="AAA", 製造板幅=1200.0, 製造板丈=2400.0)
        self.get("/api/lot/1234567")
        self.client.post("/api/lot/expand", json={}, headers=self.auth())

        state = self.get("/api/selection/state")
        self.assertEqual(state["product_width"], "1200")
        self.assertEqual(state["product_length"], "2400")
        # 確定はしていない。押していない「セット」を押したことにしない
        self.assertFalse(state["product_set"])

        # 画面にも出ている(渡っているのに見えない、を防ぐ)
        html = self.client.get("/selection").get_data(as_text=True)
        self.assertIn('value="1200"', html)
        self.assertIn('value="2400"', html)

    def test_展開していなければ何も出さない(self) -> None:
        state = self.get("/api/selection/state")
        self.assertEqual(state["product_width"], "")
        self.assertEqual(state["product_length"], "")


@unittest.skipUnless(HAS_WEB, _SKIP)
class AccessTests(unittest.TestCase):
    """現場の権限が無ければロット検索は無い。隠すのではなく登録しない。"""

    def setUp(self) -> None:
        from app import create_app
        from packaging_tool import access_control
        app = create_app("material", token=TOKEN, port=8723,
                         grant=access_control.grant_of("mode:material"))
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_画面が無い(self) -> None:
        self.assertEqual(self.client.get("/lot").status_code, 404)

    def test_APIも無い(self) -> None:
        for path in ("/api/lot/1234567", "/api/lot?no=1234567"):
            with self.subTest(path=path):
                res = self.client.get(path, headers={"X-Tool-Token": TOKEN})
                self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
