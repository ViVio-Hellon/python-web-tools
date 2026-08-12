"""簡易在庫の保管位置マップの編集状態 (`layout_session` と同じ考え方)

【なぜ要るのか】
保管位置の図(`pallet_map.json`)は、置き場が増えたり動いたりしても
**画面から直す手立てがありませんでした**。棚検索の配置図には配置編集が
あるのに、こちらだけ JSON を手で書き換えるしかない状態です。

位置が増えたのに図を直せないと、その棚のパレットは図から押せません
(`PalletMap.unknown` が名前を返すので、増えたことには気づけます)。
気づけるのに直せない、という行き止まりをここで開けます。

【`layout_session` と同じ約束にそろえる】
覚え直しを増やさないため、操作も断り方もそろえてあります。

    保存するまでファイルに触らない   間違えて動かしたものを戻せる
    編集モードの入り切りが要る       見るつもりの操作で位置がずれない
    断りの種類は文言から推し量らない REFUSE_* を返す

【棚検索と別のセッションにする理由】
図が別のファイル(`floor_plan.json` / `pallet_map.json`)なので、
編集中かどうかも別々に持ちます。片方を編集中にもう片方が編集モードに
なると、見るつもりの画面で位置が動きます。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from . import map_data, pallet_map
from .logging_utils import get_logger

log = get_logger("pallet_map_session")

# 断りの種類。**`layout_session` と同じ語**を使う(同じ話に別の名前を付けない)
REFUSE_BAD_INPUT = "bad_input"
REFUSE_NOT_LISTED = "not_listed"
REFUSE_FAILED = "failed"


@dataclass
class MapOpResult:
    ok: bool = True
    message: str = ""
    reason: str = ""


@dataclass
class PalletMapSession:
    """保管位置マップで決まっていること。"""

    plan: pallet_map.PalletMap = field(default_factory=pallet_map.load)

    # 配置編集。**保存するまでファイルには書かない**
    editing: bool = False
    dirty: bool = False

    # --- 編集モード -------------------------------------------------
    def set_editing(self, on: bool) -> MapOpResult:
        """編集モードの入り切り。

        通常は動かせません。図はよく押す(その位置の在庫を見る)ので、
        常時ドラッグできると**見るつもりの操作で位置がずれます**。
        """
        self.editing = bool(on)
        if not self.editing and self.dirty:
            return MapOpResult(
                True, "編集を終わりました。変更はまだ保存していません。")
        return MapOpResult(True, "配置編集: " + ("ON" if self.editing else "OFF"))

    def _require_editing(self) -> Optional[MapOpResult]:
        if self.editing:
            return None
        return MapOpResult(False, "先に「配置編集」をONにしてください。",
                           REFUSE_BAD_INPUT)

    # --- 箱 ---------------------------------------------------------
    def move(self, name: str, x: float, y: float) -> MapOpResult:
        """保管位置を動かす。座標は**図の論理座標**。"""
        refused = self._require_editing()
        if refused:
            return refused
        item = self.plan.position(name)
        if item is None:
            return MapOpResult(False, f"{name} は保管位置マップにありません。",
                               REFUSE_NOT_LISTED)
        x, y = map_data.clamp_point(x, y, item.w, item.h,
                                    self.plan.width, self.plan.height)
        if not self.plan.move_position(name, x, y):
            return MapOpResult(False, f"{name} を動かせませんでした。",
                               REFUSE_FAILED)
        self.dirty = True
        return MapOpResult(True, "")

    def resize(self, name: str, w: float, h: float) -> MapOpResult:
        """保管位置の大きさを変える(背景の写真に合わせこむため)。"""
        refused = self._require_editing()
        if refused:
            return refused
        item = self.plan.position(name)
        if item is None:
            return MapOpResult(False, f"{name} は保管位置マップにありません。",
                               REFUSE_NOT_LISTED)
        w, h = map_data.clamp_size(w, h, self.plan.width, self.plan.height)
        if not self.plan.resize_position(name, w, h):
            return MapOpResult(False, f"{name} の大きさを変えられませんでした。",
                               REFUSE_FAILED)
        # 大きくした結果はみ出すことがある。動かすときと同じ枠に戻す
        x, y = map_data.clamp_point(item.x, item.y, w, h,
                                    self.plan.width, self.plan.height)
        self.plan.move_position(name, x, y)
        self.dirty = True
        return MapOpResult(True, "")

    def add(self, name: str) -> MapOpResult:
        """保管位置を足す。"""
        refused = self._require_editing()
        if refused:
            return refused
        name = (name or "").strip()
        if not name:
            return MapOpResult(False, "保管位置の名前を入力してください。",
                               REFUSE_BAD_INPUT)
        if self.plan.position(name) is not None:
            return MapOpResult(False, f"{name} はすでにあります。",
                               REFUSE_BAD_INPUT)
        # 真ん中に置く。どこに出たか分からないと探すことになる
        self.plan.add_position(name, self.plan.width / 2, self.plan.height / 2)
        self.dirty = True
        log.info("保管位置を追加しました: %s", name)
        return MapOpResult(True, f"{name} を図の中央に置きました。動かしてください")

    def remove(self, name: str) -> MapOpResult:
        """保管位置を消す。"""
        refused = self._require_editing()
        if refused:
            return refused
        if not self.plan.remove_position(name):
            return MapOpResult(False, f"{name} は保管位置マップにありません。",
                               REFUSE_NOT_LISTED)
        self.dirty = True
        log.info("保管位置を削除しました: %s", name)
        return MapOpResult(True, f"{name} を消しました")

    # --- 背景 -------------------------------------------------------
    def set_background(self, image: str) -> MapOpResult:
        """背景画像を差し替える(`data:` URL ごと持つ)。

        棚検索と同じで**中身を持ちます**。ブラウザのファイル選択は
        クライアント側のパスしか返さないので、サーバがそのパスを開ける
        とは限りません(共有フォルダから選ばれることもあります)。
        """
        refused = self._require_editing()
        if refused:
            return refused
        # 差し替えたら位置と倍率は初期に戻す(前の写真のずらし量を
        # 持ち越すと、開いた瞬間に枠の外に出ていることがある)
        self.plan.background = map_data.Background(image=image)
        self.dirty = True
        return MapOpResult(True, "背景画像を差し替えました" if image
                           else "背景画像を外しました")

    def place_background(self, x: float, y: float,
                         scale: float) -> MapOpResult:
        """背景の写真そのものをずらす・拡げ縮めする。"""
        refused = self._require_editing()
        if refused:
            return refused
        self.plan.background = (self.plan.background
                                .placed_at(x, y)
                                .scaled_to(map_data.clamp_scale(scale)))
        self.dirty = True
        return MapOpResult(True, "")

    # --- 保存 -------------------------------------------------------
    def save(self) -> MapOpResult:
        if not pallet_map.save(self.plan):
            return MapOpResult(False, "保管位置マップを保存できませんでした。",
                               REFUSE_FAILED)
        self.dirty = False
        log.info("保管位置マップを保存しました")
        return MapOpResult(True, "保管位置マップを保存しました")

    def reset(self) -> MapOpResult:
        """出荷時の配置に戻す。編集した内容は消える。"""
        self.plan = pallet_map.reset_to_default()
        self.dirty = False
        log.info("保管位置マップを既定に戻しました")
        return MapOpResult(True, "出荷時の配置に戻しました")


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_session: Optional[PalletMapSession] = None
_lock = threading.Lock()


def get_session() -> PalletMapSession:
    global _session
    with _lock:
        if _session is None:
            _session = PalletMapSession()
            log.info("保管位置マップのセッションを開始しました")
        return _session


def reset_session() -> None:
    """テスト用。プロセスに1つという前提を壊さずに作り直す。"""
    global _session
    with _lock:
        _session = None
