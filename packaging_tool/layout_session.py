"""棚検索の作業状態 (tkinter版 `LayoutView` のインスタンス変数にあたる)

`selection_session` / `work_context` / `jobs` と同じ考え方で、画面が
持っていた状態をプロセス側に置く。

【なぜ配置図を抱えるのか】
`floor_plan.load()` は毎回ファイルを読む。編集中に読み直すと、まだ
保存していない移動が消える。tkinter版は `self.plan` に読み込んだまま
持っていて「保存」で書き出していたので、同じにする。

**保存するまでファイルに触らない**のが要点。ドラッグするたびに
書き込むと、間違えて動かしたものを戻せない。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from . import floor_plan, location_service as svc, user_settings
from .logging_utils import get_logger
from .presenters.layout import KIND_BOARD, KINDS

log = get_logger("layout_session")

# 断りの種類。`selection_session` と同じ語を使う
REFUSE_BAD_INPUT = "bad_input"
REFUSE_NOT_LISTED = "not_listed"
REFUSE_NOT_FOUND = "not_found"
REFUSE_FAILED = "failed"
# 資材選択から何も渡ってきていない。**入力の形の誤りではない**ので 422。
# 押した拍子に図が消えないよう、画面ぜんぶを返す側に載せる(§6.1)
REFUSE_NO_HANDOFF = "no_handoff"


@dataclass
class LayoutOpResult:
    ok: bool = True
    message: str = ""
    reason: str = ""


@dataclass
class LayoutSession:
    """棚検索で決まっていること。"""

    plan: floor_plan.FloorPlan = field(default_factory=floor_plan.load)

    # 検索
    kind: str = KIND_BOARD
    width_text: str = ""
    length_text: str = ""
    result: str = ""
    highlight: set = field(default_factory=set)

    # 押した置き場
    selected: str = ""

    # 配置編集。**保存するまでファイルには書かない**
    editing: bool = False
    dirty: bool = False

    # ------------------------------------------------------------------
    def set_base_point(self, name: str) -> LayoutOpResult:
        """拠点の切替。

        拠点は資材選択とも共有する設定(VBAのレジストリ1値に相当)なので、
        ここで保存すれば疲労度の計算もそろう。画面ごとに別の拠点を
        持たせると、同じロットで違う結果が出る。
        """
        if name not in self.plan.base_point_names:
            return LayoutOpResult(False, f"拠点 '{name}' は配置図にありません。",
                                  REFUSE_NOT_LISTED)
        if not user_settings.set_position(name):
            return LayoutOpResult(False, "設定を保存できませんでした。",
                                  REFUSE_FAILED)
        log.info("拠点を切り替えました: %s", name)
        return LayoutOpResult(True, f"拠点を {name} にしました")

    # --- 検索 -------------------------------------------------------
    def search(self, conn: Any, kind: str, width_text: str,
               length_text: str) -> LayoutOpResult:
        """最寄りの置き場を探す(VBA `frmLayout` の検索)。"""
        # **断る条件は、状態に触れる前に全部確かめる。**
        # 400 は「入力の形が違う」であって、サーバの状態は動いていない、
        # というのが約束(設計書 §6.1)。動かしてから断ると、画面は前の
        # 検索結果を出したままサーバだけが取り消した状態になり、
        # どちらが本当なのか分からなくなる
        if kind not in KINDS:
            return LayoutOpResult(False, f"知らない種別です: {kind}",
                                  REFUSE_BAD_INPUT)
        length = _to_int(length_text)
        width = _to_int(width_text)
        if length is None or (kind == KIND_BOARD and width is None):
            return LayoutOpResult(False, "サイズを入力してください。",
                                  REFUSE_BAD_INPUT)

        self.kind = kind
        self.width_text = width_text
        self.length_text = length_text
        self.highlight.clear()
        self.result = ""

        base = user_settings.get_position()
        if kind == KIND_BOARD:
            nearest = svc.find_nearest_shelf_for_board(conn, width, length, base)
            score = svc.score_board_pick(nearest.distance, width, length)
        else:
            nearest = svc.find_nearest_shelf_for_angle(conn, length, base)
            score = svc.score_angle_pick(nearest.distance, length)

        if nearest.shelf_name is None:
            # 「見つからない」と「マスタに置き場が書かれていない」は別。
            # 直し方まで書く(データラベル列を見ればよい)
            self.result = ("このサイズには置き場(データラベル)が登録されて"
                           "いません。マスタの「データラベル」列を確認してください。")
            return LayoutOpResult(False, self.result, REFUSE_NOT_FOUND)

        self.highlight.add(nearest.shelf_name)
        self.selected = nearest.shelf_name
        self.result = (f"最寄り: {nearest.shelf_name}  "
                       f"距離={nearest.distance:.0f}  "
                       f"疲労度スコア={score:.1f}  "
                       f"(候補{nearest.candidate_count}件)")
        return LayoutOpResult(True, self.result)

    def show_selection(self, conn: Any, items: list) -> LayoutOpResult:
        """選定した資材の置き場を**まとめて**光らせる(VBA `btnMap_Click`)。

        1件ずつ探すと、5種類のボードを見るのに5回打ち直すことになります。
        旧版はここを1押しで済ませていて、合計疲労度まで出していました
        ── 「どれを取りに行くか」ではなく「**全部でどれだけ歩くか**」が
        分かるのが要点です(取りに行く順を決めるのは人)。

        `items` は資材選択が渡したもの:
            {"kind": "board"/"angle", "width": int, "length": int, "count": int}
        """
        self.highlight.clear()
        self.result = ""
        if not items:
            return LayoutOpResult(False, "選定した資材がありません。"
                                  "先に資材選択でボードを決めてください。",
                                  REFUSE_NO_HANDOFF)

        base = user_settings.get_position()
        total = 0.0
        found = 0
        missing: list[str] = []
        for item in items:
            kind = str(item.get("kind", KIND_BOARD))
            width = _to_int(str(item.get("width", "")))
            length = _to_int(str(item.get("length", "")))
            count = _to_int(str(item.get("count", "1"))) or 1
            if length is None or (kind == KIND_BOARD and width is None):
                continue
            if kind == KIND_BOARD:
                nearest = svc.find_nearest_shelf_for_board(conn, width, length, base)
                score = svc.score_board_pick(nearest.distance, width, length, count)
                label = f"{width}×{length}"
            else:
                nearest = svc.find_nearest_shelf_for_angle(conn, length, base)
                score = svc.score_angle_pick(nearest.distance, length, count)
                label = f"アングル {length}"
            if nearest.shelf_name is None:
                # **置き場が登録されていないものは、黙って落とさない。**
                # 光らないぶんは合計にも入らないので、言わないと嘘になる
                missing.append(label)
                continue
            self.highlight.add(nearest.shelf_name)
            total += score
            found += 1

        if not self.highlight:
            self.result = ("選定した資材の置き場(データラベル)が"
                           "1つも登録されていません。"
                           "マスタの「データラベル」列を確認してください。")
            return LayoutOpResult(False, self.result, REFUSE_NOT_FOUND)

        self.kind = KIND_BOARD
        self.selected = ""
        note = f"({len(missing)}件は置き場が未登録: {', '.join(missing)})" if missing else ""
        self.result = (f"選定した資材 {found}件 / 置き場 {len(self.highlight)}か所  "
                       f"合計疲労度={total:.1f}  {note}").strip()
        return LayoutOpResult(True, self.result)

    def select(self, name: str) -> LayoutOpResult:
        """置き場を押す。中身を出すだけで、図は変えない。"""
        if self.plan.item(name) is None:
            return LayoutOpResult(False, f"{name} は配置図にありません。",
                                  REFUSE_NOT_LISTED)
        self.selected = name
        return LayoutOpResult(True, "")

    # --- 配置編集 ---------------------------------------------------
    def set_editing(self, on: bool) -> LayoutOpResult:
        """編集モードの入り切り。

        通常は動かせない。図はよく押す(中身を見る)ので、常時ドラッグ
        できると**見るつもりの操作で置き場がずれる**。
        """
        self.editing = bool(on)
        if not self.editing and self.dirty:
            return LayoutOpResult(
                True, "編集を終わりました。変更はまだ保存していません。")
        return LayoutOpResult(True, "配置編集: " + ("ON" if self.editing else "OFF"))

    def move(self, name: str, x: float, y: float) -> LayoutOpResult:
        """置き場を動かす。**保存するまでファイルには書かない**。"""
        if not self.editing:
            return LayoutOpResult(False, "先に「配置編集」をONにしてください。",
                                  REFUSE_BAD_INPUT)
        # 図の外へ出すと二度と掴めない。枠の中に留める
        item = self.plan.item(name)
        if item is None:
            return LayoutOpResult(False, f"{name} は配置図にありません。",
                                  REFUSE_NOT_LISTED)
        x = min(max(0.0, x), max(0.0, self.plan.width - item.w))
        y = min(max(0.0, y), max(0.0, self.plan.height - item.h))
        if not self.plan.move_item(name, x, y):
            return LayoutOpResult(False, f"{name} を動かせませんでした。",
                                  REFUSE_FAILED)
        self.dirty = True
        return LayoutOpResult(True, "")

    def add(self, name: str) -> LayoutOpResult:
        """置き場を足す(VBA `do_add_label`)。"""
        if not self.editing:
            return LayoutOpResult(False, "先に「配置編集」をONにしてください。",
                                  REFUSE_BAD_INPUT)
        name = (name or "").strip()
        if not name:
            return LayoutOpResult(False, "置き場の名前を入力してください。",
                                  REFUSE_BAD_INPUT)
        if self.plan.item(name) is not None:
            return LayoutOpResult(False, f"{name} はすでにあります。",
                                  REFUSE_BAD_INPUT)
        # 真ん中に置く。どこに出たか分からないと探すことになる
        self.plan.add_item(name, self.plan.width / 2, self.plan.height / 2)
        self.dirty = True
        self.selected = name
        log.info("置き場を追加しました: %s", name)
        return LayoutOpResult(True, f"{name} を図の中央に置きました。動かしてください")

    def remove(self, name: str) -> LayoutOpResult:
        """置き場を消す(VBA `do_remove_label`)。"""
        if not self.editing:
            return LayoutOpResult(False, "先に「配置編集」をONにしてください。",
                                  REFUSE_BAD_INPUT)
        if not self.plan.remove_item(name):
            return LayoutOpResult(False, f"{name} は配置図にありません。",
                                  REFUSE_NOT_LISTED)
        self.dirty = True
        if self.selected == name:
            self.selected = ""
        self.highlight.discard(name)
        log.info("置き場を削除しました: %s", name)
        return LayoutOpResult(True, f"{name} を消しました")

    def set_background(self, image: str) -> LayoutOpResult:
        """背景画像を差し替える(`data:` URL ごと持つ)。

        tkinter版はファイルのパスを覚えていたが、Web版は**中身を持つ**。
        ブラウザのファイル選択はクライアント側のパスしか返さないので、
        サーバがそのパスを開けるとは限らない(共有フォルダから選ばれる
        こともある)。`tk.PhotoImage` の PNG/GIF 制約も無くなるので
        **JPEG も使える**(§7.2)。
        """
        if not self.editing:
            return LayoutOpResult(False, "先に「配置編集」をONにしてください。",
                                  REFUSE_BAD_INPUT)
        from . import map_data
        self.plan.background = map_data.Background(
            image=image, x=self.plan.background.x,
            y=self.plan.background.y, scale=self.plan.background.scale)
        self.dirty = True
        return LayoutOpResult(True, "背景画像を差し替えました" if image
                              else "背景画像を外しました")

    # --- 保存 -------------------------------------------------------
    def save(self) -> LayoutOpResult:
        """編集した配置図をファイルへ書く(VBA `do_save`)。"""
        if not floor_plan.save(self.plan):
            return LayoutOpResult(False, "配置図を保存できませんでした。",
                                  REFUSE_FAILED)
        self.dirty = False
        log.info("配置図を保存しました")
        return LayoutOpResult(True, "配置図を保存しました")

    def reset(self) -> LayoutOpResult:
        """出荷時の配置に戻す(VBA `do_reset`)。編集した内容は消える。"""
        self.plan = floor_plan.reset_to_default()
        self.dirty = False
        self.highlight.clear()
        self.selected = ""
        log.info("配置図を既定に戻しました")
        return LayoutOpResult(True, "出荷時の配置に戻しました")


def _to_int(text: str) -> Optional[int]:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------
# プロセスに1つ
# ------------------------------------------------------------------
_session: Optional[LayoutSession] = None
_lock = threading.Lock()


def get_session() -> LayoutSession:
    global _session
    with _lock:
        if _session is None:
            _session = LayoutSession()
            log.info("棚検索のセッションを開始しました")
        return _session


def reset_session() -> None:
    """テスト用。プロセスに1つという前提を壊さずに作り直す。"""
    global _session
    with _lock:
        _session = None
