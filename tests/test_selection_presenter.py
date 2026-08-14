"""資材選択画面の状態機械 (`presenters.selection`) の確認

包装仕様NOで切り替わるモード群(1P0113 / プロテック / 上下共用 / EX)は
**順序依存の状態機械**で、ここが資材選択のいちばん複雑なところ。
画面なしで検証する ── 判断そのものが正しいかを見るので、
画面を立てる必要が無い。

画面に正しく出ているかは `tests/test_web_selection.py` が見る。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import db, material_service, special_packaging as spk  # noqa: E402
from packaging_tool.presenters.selection import (  # noqa: E402
    BANNER_1P0113,
    BANNER_EX,
    BANNER_NONE,
    BANNER_PROTEC,
    TITLE_LOWER,
    TITLE_LOWER_SHARED,
    SelectionPresenter,
)
from packaging_tool.user_log import UserLog  # noqa: E402


# ------------------------------------------------------------------
# ロット検索結果の最小の替え玉
#
# `lot_service.LotSearchResult` をそのまま作ると仕掛台帳が要るので、
# プレゼンタが実際に読む属性だけを持つ器を用意する。
# 読む属性が増えたらここも増やす(= 依存が見える形にしておく)。
# ------------------------------------------------------------------
@dataclass
class FakeLot:
    lot_no: str = "4102781"
    thickness: float = 6.75
    width: float = 1122
    length: float = 2502
    zaishitsu: str = "A5052"
    choshitsu: str = "H112"
    yoto_code: str = "K434"


@dataclass
class FakeOdr:
    packaging_spec: str = ""
    is_ex: bool = False


@dataclass
class FakeResult:
    lot: FakeLot = field(default_factory=FakeLot)
    odr: FakeOdr = field(default_factory=FakeOdr)


def _make_result(*, spec: str = "", is_ex: bool = False, **lot_kwargs) -> FakeResult:
    return FakeResult(lot=FakeLot(**lot_kwargs), odr=FakeOdr(spec, is_ex))


class PresenterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.log = UserLog()
        self.p = SelectionPresenter(self.conn, user_log=self.log)

    def tearDown(self) -> None:
        self.conn.close()

    def logged(self) -> str:
        return self.log.text


# ==================================================================
# 1P0113(裸梱包)モード
# ==================================================================
class Mode1P0113Tests(PresenterTestCase):
    def test_包装仕様NOで入る(self) -> None:
        change = self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113)
        self.assertTrue(change.changed)
        self.assertTrue(change.mode_on)
        self.assertTrue(self.p.mode_1p0113)
        self.assertIn("裸梱包モード有効", self.logged())

    def test_対象外なら入らない(self) -> None:
        change = self.p.apply_1p0113_mode("1P9999")
        self.assertFalse(change.changed)
        self.assertFalse(self.p.mode_1p0113)

    def test_前後の空白は無視する(self) -> None:
        self.assertTrue(
            self.p.apply_1p0113_mode(f"  {spk.HOSOSIYO_1P0113}  ").mode_on)

    def test_抜けると解除ログが出る(self) -> None:
        self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113)
        change = self.p.apply_1p0113_mode("1P9999")
        self.assertTrue(change.changed)
        self.assertFalse(change.mode_on)
        self.assertIn("裸梱包モード解除", self.logged())

    def test_同じ状態が続くとchangedはFalseだが資材は引き直す(self) -> None:
        """VBA踏襲。資材は製品サイズに依存するので毎回引き直す。

        `changed` が False なら画面側はパネルの入れ替えをしない
        (入れ替え不要なのに動かすと、ちらつきと pack 順の崩れが起きる)。
        """
        self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113,
                                 product_width=1122, product_length=2502)
        change = self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113,
                                          product_width=900, product_length=1800)
        self.assertFalse(change.changed)
        self.assertTrue(change.mode_on)
        self.assertTrue(change.materials_reloaded)

    def test_通常モードのままなら資材は引かない(self) -> None:
        change = self.p.apply_1p0113_mode("")
        self.assertFalse(change.materials_reloaded)

    def test_資材は入力欄の値で引く(self) -> None:
        """パレットが無く「セット」を通せないので入力欄を見るのが正しい。"""
        self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113,
                                 product_width=1122, product_length=2502)
        self.assertEqual(self.p.materials_1p0113.product_width, 1122)
        self.assertEqual(self.p.materials_1p0113.product_length, 2502)

    def test_モード外では資材を引かない(self) -> None:
        self.assertIsNone(self.p.load_1p0113_materials(1122, 2502))

    def test_サイズが変わったときだけ乗数を入れ直す(self) -> None:
        """手で変えた乗数を、サイズ据え置きの再読込で潰さない。"""
        self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113,
                                 product_width=1122, product_length=2502)
        self.assertIsNone(self.p.load_1p0113_materials(1122, 2502))   # 据え置き
        self.assertIsNotNone(self.p.load_1p0113_materials(900, 1800))  # 入れ直す

    def test_乗数の初期値は1以上10以下(self) -> None:
        self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113)
        value = self.p.load_1p0113_materials(900, 1800)
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 1)
        self.assertLessEqual(value, 10)

    def test_資材が引けないと強調ログが出る(self) -> None:
        """マスタが空なので必ず引けない。理由が残ること。"""
        self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113,
                                 product_width=1122, product_length=2502)
        self.assertIn("資材が引けません", self.logged())

    def test_ラベルのフル情報を引ける(self) -> None:
        self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113)
        self.assertIsNotNone(self.p.material_1p0113_info("kak"))
        self.assertIsNotNone(self.p.material_1p0113_info("mat"))
        self.assertIsNone(self.p.material_1p0113_info("なにか"))


class Force1P0113Tests(PresenterTestCase):
    """強制トグル(VBA `lblForce1P0113`)。"""

    def test_ONにすると包装仕様NOに関係なく入る(self) -> None:
        change = self.p.toggle_force_1p0113()
        self.assertTrue(self.p.force_1p0113)
        self.assertTrue(change.mode_on)
        self.assertTrue(self.p.mode_1p0113)

    def test_OFFに戻すと現在の包装仕様NOで判定し直す(self) -> None:
        self.p.toggle_force_1p0113()
        change = self.p.toggle_force_1p0113()
        self.assertFalse(self.p.force_1p0113)
        self.assertFalse(change.mode_on)

    def test_包装仕様NOが1P0113ならOFFにしてもモードは続く(self) -> None:
        self.p.apply_lot(_make_result(spec=spk.HOSOSIYO_1P0113))
        self.p.toggle_force_1p0113()       # ON
        self.p.toggle_force_1p0113()       # OFF に戻す
        self.assertTrue(self.p.mode_1p0113, "包装仕様NOが1P0113なのに抜けている")

    def test_新しいロットで自動解除される(self) -> None:
        """VBA `ClearForNewLot`。疲労度優先・在庫考慮は手動トグルなので残す。"""
        self.p.toggle_force_1p0113()
        self.assertTrue(self.p.force_1p0113)
        outcome = self.p.apply_lot(_make_result(spec="1P9999"))
        self.assertTrue(outcome["force_released"])
        self.assertFalse(self.p.force_1p0113)
        self.assertFalse(self.p.mode_1p0113)


# ==================================================================
# プロテックモード
# ==================================================================
class ProtecTests(PresenterTestCase):
    def test_プロテックの包装仕様NOで入る(self) -> None:
        for spec in ("1P1216", "1P1125", "1P1211"):
            with self.subTest(spec=spec):
                p = SelectionPresenter(self.conn, user_log=UserLog())
                p.check_and_set_protec_mode(spec)
                self.assertTrue(p.protec.is_protec, spec)

    def test_1P1216だけ許容が違う(self) -> None:
        self.p.check_and_set_protec_mode("1P1216")
        self.assertTrue(self.p.protec.is_1p1216)
        self.assertIn("-5mm", self.logged())

    def test_他のプロテックは80mm(self) -> None:
        self.p.check_and_set_protec_mode("1P1125")
        self.assertFalse(self.p.protec.is_1p1216)
        self.assertIn("-80mm", self.logged())

    def test_抜けると既定のボード種別に戻る(self) -> None:
        self.p.check_and_set_protec_mode("1P1216")
        protec_type = self.p.protec.board_type
        result = self.p.check_and_set_protec_mode("1P9999")
        self.assertFalse(self.p.protec.is_protec)
        self.assertNotEqual(result.target_board_type, protec_type)

    def test_マスタにある種別なら切り替えを指示する(self) -> None:
        result = self.p.check_and_set_protec_mode(
            "1P1216", available_board_types=["ハードボード", "プロテックボード"],
            current_board_type="ハードボード")
        self.assertTrue(result.should_switch)
        self.assertEqual(result.target_board_type, "プロテックボード")
        self.assertFalse(result.missing_from_master)

    def test_すでにその種別なら切り替えない(self) -> None:
        result = self.p.check_and_set_protec_mode(
            "1P1216", available_board_types=["プロテックボード"],
            current_board_type="プロテックボード")
        self.assertFalse(result.should_switch)

    def test_マスタに無ければ知らせる(self) -> None:
        """移行直後などで種別が無い環境。黙って進むと理由が分からない。"""
        result = self.p.check_and_set_protec_mode(
            "1P1216", available_board_types=["ハードボード"],
            current_board_type="ハードボード")
        self.assertTrue(result.missing_from_master)
        self.assertFalse(result.should_switch)

    def test_選定へ渡すモードに反映される(self) -> None:
        self.p.check_and_set_protec_mode("1P1216")
        flags = self.p.selection_flags()
        self.assertTrue(flags["is_protec_mode"])
        self.assertTrue(flags["is_protec_1p1216"])


# ==================================================================
# 保護材 → アングルの要否 / 上下共用
# ==================================================================
class HosozaiTests(PresenterTestCase):
    def test_アングルならそのまま出す(self) -> None:
        result = self.p.apply_hosozai(material_service.HOSOZAI_ANGLE)
        self.assertTrue(result.show_angle)
        self.assertEqual(result.lower_title, TITLE_LOWER)
        self.assertEqual(result.hosozai_label, "")
        self.assertFalse(self.p.is_shared_board_mode)

    def test_空と一致なしは安全側に倒す(self) -> None:
        """どちらもアングルを出したままにする(VBA `ShowAngleControls`)。"""
        for hosozai in ("", "一致なし"):
            with self.subTest(hosozai=hosozai):
                p = SelectionPresenter(self.conn, user_log=UserLog())
                self.assertTrue(p.apply_hosozai(hosozai).show_angle)
                self.assertFalse(p.is_shared_board_mode)

    def test_アングル以外は上下共用になる(self) -> None:
        result = self.p.apply_hosozai("ザラ板")
        self.assertFalse(result.show_angle)
        self.assertEqual(result.lower_title, TITLE_LOWER_SHARED)
        self.assertEqual(result.hosozai_label, "使用保護材: ザラ板")
        self.assertTrue(self.p.is_shared_board_mode)
        self.assertIn("上下共用", self.logged())

    def test_アングルに戻せば元に戻る(self) -> None:
        self.p.apply_hosozai("ザラ板")
        result = self.p.apply_hosozai(material_service.HOSOZAI_ANGLE)
        self.assertTrue(result.show_angle)
        self.assertEqual(result.lower_title, TITLE_LOWER)
        self.assertFalse(self.p.is_shared_board_mode)

    def test_プロテック中は上下共用が抑止される(self) -> None:
        """VBAの `Not mIsProtecMode And ...` 条件。"""
        self.p.apply_hosozai("ザラ板")
        self.assertTrue(self.p.is_shared_board_mode)
        self.p.check_and_set_protec_mode("1P1216")
        self.assertFalse(self.p.is_shared_board_mode)

    def test_show_angleプロパティは判断と一致する(self) -> None:
        for hosozai in ("", "一致なし", material_service.HOSOZAI_ANGLE, "ザラ板", "ダンボール"):
            with self.subTest(hosozai=hosozai):
                p = SelectionPresenter(self.conn, user_log=UserLog())
                self.assertEqual(p.apply_hosozai(hosozai).show_angle, p.show_angle)

    def test_保護材が引けなくても止まらない(self) -> None:
        """マスタが空でも例外を出さず、安全側("")に倒す。"""
        self.assertEqual(self.p.lookup_hosozai(_make_result()), "")


# ==================================================================
# EXオーダー
# ==================================================================
class ExOrderTests(PresenterTestCase):
    def test_フラグが立つ(self) -> None:
        self.p.set_ex_order(True)
        self.assertTrue(self.p.is_ex_order)
        self.p.set_ex_order(False)
        self.assertFalse(self.p.is_ex_order)

    def test_ロットから伝わる(self) -> None:
        self.p.apply_lot(_make_result(is_ex=True))
        self.assertTrue(self.p.is_ex_order)


# ==================================================================
# モードバー
# ==================================================================
class ModeBannerTests(PresenterTestCase):
    def test_通常モードは何も出さない(self) -> None:
        banner = self.p.mode_banner()
        self.assertEqual(banner.kind, BANNER_NONE)
        self.assertFalse(banner.visible)

    def test_1P0113が最優先(self) -> None:
        """1P0113 → プロテック → EX の順。下位は上位に隠れる(VBA踏襲)。"""
        self.p.set_ex_order(True)
        self.p.check_and_set_protec_mode("1P1216")
        self.p.apply_1p0113_mode(spk.HOSOSIYO_1P0113)
        self.assertEqual(self.p.mode_banner().kind, BANNER_1P0113)

    def test_プロテックはEXより優先(self) -> None:
        self.p.set_ex_order(True)
        self.p.check_and_set_protec_mode("1P1216")
        self.assertEqual(self.p.mode_banner().kind, BANNER_PROTEC)

    def test_EXだけならEX(self) -> None:
        self.p.set_ex_order(True)
        banner = self.p.mode_banner()
        self.assertEqual(banner.kind, BANNER_EX)
        self.assertIn("EXオーダー", banner.text)


# ==================================================================
# ロット確定 — 状態機械の順序
# ==================================================================
class ApplyLotTests(PresenterTestCase):
    def test_ロット情報が入る(self) -> None:
        self.p.apply_lot(_make_result(lot_no="4102781", thickness=6.75))
        self.assertEqual(self.p.lot_no, "4102781")
        self.assertEqual(self.p.manufactured_thickness, 6.75)

    def test_板厚0はNoneにする(self) -> None:
        """`or None` の挙動。5×10業界の強度UP判定で「無い」と扱わせる。"""
        self.p.apply_lot(_make_result(thickness=0))
        self.assertIsNone(self.p.manufactured_thickness)

    def test_1P0113のロットでモードに入る(self) -> None:
        outcome = self.p.apply_lot(_make_result(spec=spk.HOSOSIYO_1P0113))
        self.assertTrue(outcome["mode_change"].mode_on)
        self.assertTrue(self.p.mode_1p0113)

    def test_プロテックのロットでモードに入る(self) -> None:
        self.p.apply_lot(_make_result(spec="1P1216"))
        self.assertTrue(self.p.protec.is_protec)

    def test_保護材の判定順がプロテックより前(self) -> None:
        """VBA `SearchAndDisplay` 末尾の順序。

        `apply_hosozai` → `check_and_set_protec_mode` の順で呼ぶので、
        プロテックのロットでは(保護材が何であれ)上下共用が抑止される。
        逆順にするとここが崩れる。
        """
        self.p.apply_lot(_make_result(spec="1P1216"))
        self.assertTrue(self.p.protec.is_protec)
        self.assertFalse(self.p.is_shared_board_mode)

    def test_ロット表示の文言(self) -> None:
        outcome = self.p.apply_lot(_make_result(lot_no="4102781", thickness=6.75))
        self.assertIn("Lot 4102781", outcome["lot_caption"])
        self.assertIn("6.75", outcome["lot_caption"])
        self.assertNotIn("[EX]", outcome["lot_caption"])

    def test_EXのロットには印が付く(self) -> None:
        outcome = self.p.apply_lot(_make_result(is_ex=True))
        self.assertIn("[EX]", outcome["lot_caption"])

    def test_ロット未確定なら包装仕様NOは空(self) -> None:
        self.assertEqual(self.p.current_packaging_spec(), "")
        self.assertEqual(self.p.lot_caption(), "")

    def test_ロットを切り替えるとモードも切り替わる(self) -> None:
        self.p.apply_lot(_make_result(spec=spk.HOSOSIYO_1P0113))
        self.assertTrue(self.p.mode_1p0113)
        self.p.apply_lot(_make_result(spec="1P1216"))
        self.assertFalse(self.p.mode_1p0113)
        self.assertTrue(self.p.protec.is_protec)


class SelectionFlagsTests(PresenterTestCase):
    """自動選定へ渡すモード群。ここが欠けると選定結果が変わる。"""

    def test_既定は全部OFF(self) -> None:
        self.assertEqual(self.p.selection_flags(),
                         {"is_protec_mode": False, "is_protec_1p1216": False,
                          "last_hosozai": ""})

    def test_保護材が渡る(self) -> None:
        self.p.apply_hosozai("ザラ板")
        self.assertEqual(self.p.selection_flags()["last_hosozai"], "ザラ板")

    def test_auto_select_boardsの引数名と一致する(self) -> None:
        """展開して渡すので、キー名がずれると TypeError になる。"""
        import inspect
        from packaging_tool import board_selection_algorithm as alg
        params = inspect.signature(alg.auto_select_boards).parameters
        for key in self.p.selection_flags():
            self.assertIn(key, params, f"auto_select_boards に {key} が無い")


class SelectionLogDetailTests(unittest.TestCase):
    """選定ログの粒度。

    **「上用2種類」とだけ残っても、あとから追えない。** VBA版は工程ごとに
    細かくユーザーログを出しており、現場はそれを見て「なぜこの寸法に
    なったのか」を確かめていた。Python版はパレット自動選定だけが詳細で、
    それ以外(ボード選定・配置・アングル・確定操作)がほぼ無記録だった
    ── 現場の声(2回)「選定ログがほとんどない。この程度のログは意味がない」。
    """

    def setUp(self) -> None:
        from packaging_tool.selection_session import SelectionSession

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.log = UserLog()
        self.session = SelectionSession(
            presenter=SelectionPresenter(self.conn, user_log=self.log))

    def test_パレットと製品の確定が残る(self) -> None:
        self.session.apply_pallet("1100", "2500")
        self.assertIn("[パレット確定] 1100 x 2500", self.log.text)

    def test_断った操作も残る(self) -> None:
        """**通らなかったことこそ残す。** 押した人には、なぜ次へ進めない
        のかが分からない。"""
        self.session.apply_pallet("あ", "い")
        self.assertIn("[パレット確定] 断りました", self.log.text)

    def test_ボード選定は中止の理由まで残る(self) -> None:
        result = self.session.auto_select_boards()
        self.assertFalse(result.ok)
        text = self.log.text
        self.assertIn("ボード自動選定を開始します", text)
        self.assertIn("中止", text)

    def test_配置は中止の理由まで残る(self) -> None:
        result = self.session.place_boards()
        self.assertFalse(result.ok)
        self.assertIn("ボード配置を開始します", self.log.text)
        self.assertIn("中止", self.log.text)

    def test_アングルは中止の理由まで残る(self) -> None:
        result = self.session.auto_select_angles()
        self.assertFalse(result.ok)
        self.assertIn("アングル自動選定を開始します", self.log.text)
        self.assertIn("中止", self.log.text)


if __name__ == "__main__":
    unittest.main()
