"""画面をまたいで共有する作業の状態

tkinter版は1つのプロセスに全画面が居たので、ロット検索で選んだロットは
そのまま資材選択タブに入っていた。Web版は要求ごとに独立するので、
同じものをプロセス側に置いている。

ここで確かめるのは2つ。

1. **リボンが用をなすか** ── 画面を移っても消えないこと。
   消えるなら「覚えさせないための帯」という役割が果たせない
2. **資材展開が渡せるか** ── 製造板幅・板丈が製品サイズになること
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import lot_service, work_context  # noqa: E402


def make_result(*, lot_no="4102781", width=1200.0, length=2400.0,
                is_ex=False, spec="1P0001") -> lot_service.LotSearchResult:
    return lot_service.LotSearchResult(
        found=True, message="",
        lot=lot_service.LotInfo(lot_no=lot_no, width=width, length=length),
        odr=lot_service.OdrInfo(is_ex=is_ex, packaging_spec=spec),
    )


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = work_context.reset_context()
        self.addCleanup(work_context.reset_context)

    # --- 初期状態 ---
    def test_何も決まっていないことを表示に出す(self) -> None:
        """空白だと、決まっていないのか表示が壊れているのか分からない。"""
        ribbon = self.context.ribbon()
        self.assertEqual(ribbon["lot"], work_context.NO_LOT)
        self.assertEqual(ribbon["pallet"], work_context.UNSET)
        self.assertEqual(ribbon["product"], work_context.UNSET)

    # --- ロット ---
    def test_ロットを覚える(self) -> None:
        self.context.apply_lot(make_result())
        self.assertEqual(self.context.ribbon()["lot"], "4102781")

    def test_見つからなかったロットは覚えない(self) -> None:
        """「無い」を作業中の状態にしない。"""
        self.context.apply_lot(make_result())
        self.context.apply_lot(lot_service.LotSearchResult(found=False))
        self.assertEqual(self.context.lot_no, "4102781")

    def test_EXは常時見せる(self) -> None:
        """見落とすと梱包の仕様が変わり、積み直しになる。"""
        self.context.apply_lot(make_result(is_ex=True))
        self.assertEqual([c["text"] for c in self.context.ribbon()["modes"]], ["EX"])

    def test_EXでなければ出さない(self) -> None:
        """全部出すと、どれが効いているのか分からなくなる。"""
        self.context.apply_lot(make_result(is_ex=False))
        self.assertEqual(self.context.ribbon()["modes"], [])

    def test_同じロットを引き直しても作業は消えない(self) -> None:
        self.context.apply_lot(make_result())
        self.context.expand_materials()
        self.context.apply_lot(make_result())
        self.assertEqual(self.context.product_width, 1200)

    def test_別のロットに移ると前の作業は捨てる(self) -> None:
        """前のロットの製品サイズが残っていると、別のロットの資材を引く。

        VBA `ClearForNewLot` と同じ考え方。
        """
        self.context.apply_lot(make_result(lot_no="4102781"))
        self.context.expand_materials()
        self.context.apply_lot(make_result(lot_no="4102999"))
        self.assertEqual(self.context.product_width, 0)
        self.assertFalse(self.context.expanded)
        self.assertEqual(self.context.ribbon()["product"], work_context.UNSET)

    # --- 資材展開 ---
    def test_資材展開で製品サイズになる(self) -> None:
        """VBA `Page1_OnBtnHBClick`。製造板幅・板丈を製品サイズ欄へ。"""
        self.context.apply_lot(make_result(width=1200.0, length=2400.0))
        self.assertTrue(self.context.expand_materials())
        self.assertEqual((self.context.product_width, self.context.product_length),
                         (1200, 2400))
        self.assertEqual(self.context.ribbon()["product"], "1200×2400")

    def test_ロットが無ければ展開できない(self) -> None:
        self.assertFalse(self.context.can_expand())
        self.assertFalse(self.context.expand_materials())

    def test_寸法が取れていなければ展開できない(self) -> None:
        self.context.apply_lot(make_result(width=0.0, length=0.0))
        self.assertFalse(self.context.can_expand())
        self.assertFalse(self.context.expand_materials())

    def test_小数は丸めて整数にする(self) -> None:
        """製品サイズは入力欄の値なので整数で持つ。"""
        self.context.apply_lot(make_result(width=1199.6, length=2400.4))
        self.context.expand_materials()
        self.assertEqual((self.context.product_width, self.context.product_length),
                         (1200, 2400))

    def test_辞書にできる(self) -> None:
        import json
        self.context.apply_lot(make_result())
        json.dumps(self.context.to_dict(), ensure_ascii=False)


class SingletonTests(unittest.TestCase):
    def test_プロセスに1つ(self) -> None:
        """別の要求からでも同じものが見える(これが無いと渡せない)。"""
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        work_context.get_context().apply_lot(make_result())
        self.assertEqual(work_context.get_context().lot_no, "4102781")


if __name__ == "__main__":
    unittest.main()
