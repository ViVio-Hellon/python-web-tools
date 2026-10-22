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
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from . import angle_service, floor_plan, location_service as svc, user_settings
from .logging_utils import get_logger
from .presenters.layout import KIND_BOARD, KIND_LABEL, KINDS

log = get_logger("layout_session")


def _short(name: str) -> str:
    """lblItem1 → 1(図の箱と同じ番号だけ)。"""
    return name.replace("lblItem", "")

# 断りの種類。`selection_session` と同じ語を使う
REFUSE_BAD_INPUT = "bad_input"
REFUSE_NOT_LISTED = "not_listed"
REFUSE_NOT_FOUND = "not_found"
REFUSE_FAILED = "failed"
# 資材選択から何も渡ってきていない。**入力の形の誤りではない**ので 422。
# 押した拍子に図が消えないよう、画面ぜんぶを返す側に載せる(§6.1)
REFUSE_NO_HANDOFF = "no_handoff"
# マスタに書けない(権限・管理者パスワード。VER4.8.85 拠点を追加)
REFUSE_NOT_ALLOWED = "not_allowed"


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
    # いま出ている答えが「1件の検索」か「選んだ資材をまとめて」か。
    # 拠点を替えたら**同じものを**出し直す(VER4.8.62。以前は検索の寸法が
    # 残っていなければ何もせず、前の拠点の合計疲労度が残っていた)
    showing: str = ""                      # "" / "search" / "selection"
    selection_items: list = field(default_factory=list)

    # 押した置き場
    selected: str = ""

    # 配置編集。**保存するまでファイルには書かない**
    editing: bool = False
    dirty: bool = False

    # ------------------------------------------------------------------
    def set_base_point(self, name: str, conn: Any = None) -> LayoutOpResult:
        """拠点の切替。

        拠点は資材選択とも共有する設定(VBAのレジストリ1値に相当)なので、
        ここで保存すれば疲労度の計算もそろう。画面ごとに別の拠点を
        持たせると、同じロットで違う結果が出る。

        **出したままの答えを置き去りにしない。** 距離も疲労度も拠点から
        測るので、切り替えた時点で画面に出ている「最寄り」は前の拠点の
        ものになる。VBAはここで「直前ラベル記憶がリセットされます」と
        訊いていた(`lstPosition_Change`)。こちらは訊くのではなく、
        **同じ条件で探し直す** ── 拠点を替えるのは、そこからの答えが
        見たいからで、訊かれても答えは1つしかない。
        """
        on_plan = name in self.plan.base_point_names
        if not on_plan and name not in svc.base_point_names(conn):
            return LayoutOpResult(False, f"拠点 '{name}' は配置図にもマスタにもありません。",
                                  REFUSE_NOT_LISTED)
        if not user_settings.set_position(name):
            return LayoutOpResult(False, "設定を保存できませんでした。",
                                  REFUSE_FAILED)
        log.info("拠点を切り替えました: %s", name)

        if self.showing == "selection" and self.selection_items:
            if conn is not None:
                # まとめて見ていたなら、まとめて出し直す(合計疲労度も新しい拠点から)
                self.show_selection(conn, self.selection_items)
            else:
                self.result = ""
                self.highlight.clear()
        elif self.showing == "search" and (self.width_text or self.length_text):
            if conn is not None:
                # 探し直した結果(見つからなくても)がそのまま画面に載る。
                # 拠点の切替そのものは成功しているので、断りにはしない
                self.search(conn, self.kind, self.width_text, self.length_text)
            else:
                # 探し直せないなら、前の拠点の答えを**消す**。
                # 残すと、新しい拠点の答えとして読まれる
                self.result = ""
                self.highlight.clear()
        if not on_plan:
            # マスタで足したが、まだ図に置いていない。選べるが、置くまで距離は測れない
            return LayoutOpResult(True, f"拠点を {name} にしました。まだ図に置いていないので距離を測れません"
                                  " ──「配置編集」で図に出して置いてください")
        return LayoutOpResult(True, f"拠点を {name} にしました")

    # --- 検索 -------------------------------------------------------
    def search(self, conn: Any, kind: str, width_text: str,
               length_text: str) -> LayoutOpResult:
        """最寄りの置き場を探す(VBA `frmLayout` の検索)。

        **片側だけでも探せる。ボードとアングルを一度に見る。**

        以前は種別を先に選ばせ、ボードなら幅と丈の両方を必須にしていた。
        手元に片方の寸法しか分からないときに打ちようがなく、種別の選び
        直しも手間だった(現場の指摘:「両方の入力が必須になっていて
        使いにくい」「両方チェックとか両方でいいのでは」)。

        `kind` は**もう使わない**。画面から消したが、古い画面が残って
        いても断らないよう、受け取るだけ受け取って無視する。
        """
        # **打ち間違いと、打たなかったことを混ぜない。**
        # 空欄は「その寸法では絞らない」。数でない字が入っているのは
        # 入力の誤りなので、黙って無視せずに断る(無視すると、打った
        # つもりの寸法で絞られていない結果が出る)
        for label, text in (("幅", width_text), ("丈", length_text)):
            if text.strip() and _to_int(text) is None:
                return LayoutOpResult(False, f"{label}は数字で入力してください。",
                                      REFUSE_BAD_INPUT)
        length = _to_int(length_text)
        width = _to_int(width_text)
        if length is None and width is None:
            return LayoutOpResult(False, "幅か丈のどちらかを入力してください。",
                                  REFUSE_BAD_INPUT)

        self.width_text = width_text
        self.length_text = length_text
        self.showing = "search"
        self.highlight.clear()
        self.result = ""

        # **距離を測る原点が怪しいときは、先にそれを言う。**
        # 拠点が配置図に無いと候補の距離が1件も測れず、結果としては
        # 「置き場が登録されていません」になる ── 読んだ人はマスタの
        # データラベルを直しに行くが、原因は拠点のほう
        base_point = svc.current_base_point()
        if not base_point.known:
            self.result = base_point.note
            return LayoutOpResult(False, self.result, REFUSE_NOT_FOUND)

        hits = svc.find_shelves_by_size(conn, width, length, base_point.name)
        found = [h for h in hits if h.found]

        if not hits:
            # 「そのサイズの在庫が無い」と「置き場が書かれていない」は
            # 別の話。ここは前者
            self.result = _not_in_stock(width, length)
            return LayoutOpResult(False, self.result, REFUSE_NOT_FOUND)

        if not found:
            # 在庫にはあるが、どこに置いてあるかが分からない
            self.result = _no_shelf(hits)
            return LayoutOpResult(False, self.result, REFUSE_NOT_FOUND)

        parts = []
        for hit in found:
            self.highlight.add(hit.nearest.shelf_name)
            score = (svc.score_board_pick(hit.nearest.distance,
                                          width or 0, length or 0)
                     if hit.kind == KIND_BOARD
                     else svc.score_angle_pick(hit.nearest.distance, length or 0))
            parts.append(
                f"{KIND_LABEL[hit.kind]} {' / '.join(hit.matched_sizes)}"
                f" → {hit.nearest.shelf_name}"
                f"(距離{hit.nearest.distance:.0f} 疲労度{score:.1f}"
                f" 候補{hit.nearest.candidate_count}件)")

        self.kind = found[0].kind
        self.selected = found[0].nearest.shelf_name
        missing = [KIND_LABEL[h.kind] for h in hits if not h.found]
        tail = (f"  ※{' / '.join(missing)}は置き場が未登録" if missing else "")
        # **どこから測った距離なのかを添える。** 拠点が違えば答えが全部
        # 変わるのに、既定で当てているだけのときも数字は同じ顔で出る
        if base_point.note:
            tail += f"  ※{base_point.note}"
        self.result = "最寄り: " + "  ".join(parts) + tail
        return LayoutOpResult(True, self.result)

    def show_selection(self, conn: Any, items: list) -> LayoutOpResult:
        """選定した資材の置き場を**まとめて**光らせる(VBA `btnMap_Click`)。

        1件ずつ探すと、5種類のボードを見るのに5回打ち直すことになります。
        旧版はここを1押しで済ませていて、合計疲労度まで出していました
        ── 「どれを取りに行くか」ではなく「**全部でどれだけ歩くか**」が
        分かるのが要点です(取りに行く順を決めるのは人)。

        `items` は資材選択が渡したもの:
            {"kind": "board"/"angle", "width": int, "length": int, "count": int,
             "board_type": str(ボードだけ。選定のボード種別)}
        """
        self.highlight.clear()
        self.result = ""
        self.showing = "selection"
        self.selection_items = [dict(item) for item in items or []]
        if not items:
            return LayoutOpResult(False, "選定した資材がありません。"
                                  "先に資材選択でボードを決めてください。",
                                  REFUSE_NO_HANDOFF)

        # 検索と同じく、原点が怪しいときは先にそれを言う
        base_point = svc.current_base_point()
        if not base_point.known:
            self.result = base_point.note
            return LayoutOpResult(False, self.result, REFUSE_NOT_FOUND)

        base = base_point.name
        total = 0.0
        found = 0
        missing: list[str] = []
        skipped: list[str] = []
        for item in items:
            kind = str(item.get("kind", KIND_BOARD))
            width = _to_int(str(item.get("width", "")))
            length = _to_int(str(item.get("length", "")))
            count = _to_int(str(item.get("count", "1"))) or 1
            if length is None or (kind == KIND_BOARD and width is None):
                continue
            if kind == KIND_BOARD:
                # **選定と同じボード種別の置き場で測る**(VER4.8.62)。種別を見ないと、
                # 同じ寸法の別種別(プロテックボードなど)の棚が近ければそちらを
                # 光らせ、選定の疲労度と違う距離で合計していた
                # 在庫考慮ONで看板が出ている置き場は外す(資材選択が渡す。選定の疲労度と同じ。VER4.8.68)
                nearest = svc.find_nearest_shelf_for_board(
                    conn, width, length, base, str(item.get("board_type") or ""),
                    skip=[str(s) for s in item.get("skip_shelves") or []])
                if nearest.skipped:
                    skipped.append(f"{width}×{length}(棚"
                                   + "・".join(_short(s) for s in nearest.skipped) + ")")
                carry = svc.carry_board_score(width, length, count)
                label = f"{width}×{length}"
            else:
                nearest = svc.find_nearest_shelf_for_angle(conn, length, base)
                carry = svc.carry_angle_score(length, count)
                label = f"アングル {length}"
            if nearest.shelf_name is None:
                # **置き場が登録されていないものは、黙って落とさない。**
                # 光らないぶんは合計にも入らないので、言わないと嘘になる
                missing.append(label)
                continue
            # **歩くのは置き場ごとに1回。** 同じ置き場のもの(アングルの2本対称、
            # 上用と下用が同じサイズ、同じ棚の別サイズ)は1回で取ってくるので、
            # 距離は初めてその置き場に行くときだけ足す(VER4.8.61。以前は1件
            # ごとに足していて二重になっていた)。運ぶぶんは1件ずつ足す
            if nearest.shelf_name not in self.highlight:
                total += angle_service.fat_dist_score(nearest.distance)
            self.highlight.add(nearest.shelf_name)
            total += carry
            found += 1

        if not self.highlight:
            self.result = (
                "選定した資材が、配置図のどこに置いてあるか分かりません。"
                "置き場の登録がまだのようです ── "
                "資材課の人に伝えてください(マスタの「データラベル」欄)。")
            return LayoutOpResult(False, self.result, REFUSE_NOT_FOUND)

        self.kind = KIND_BOARD
        self.selected = ""
        note = f"({len(missing)}件は置き場が未登録: {', '.join(missing)})" if missing else ""
        if skipped:
            note += f"  看板が出ている置き場は外しました: {', '.join(skipped)}"
        # 合計疲労度も拠点からの距離で決まる。どこから測ったかを添える
        if base_point.note:
            note += f"  ※{base_point.note}"
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
    def set_editing(self, on: bool, conn: Any = None) -> LayoutOpResult:
        """編集モードの入り切り。

        通常は動かせない。図はよく押す(中身を見る)ので、常時ドラッグ
        できると**見るつもりの操作で置き場がずれる**。

        **強調表示は`highlight`と`selected`の2つある。両方ここで捨てる。**
        検索と配置編集は別の作業で、どちらも次に検索するかリセットするまで
        持ち越されたままだった。編集に入っても消えないため、現場では
        「別の置き場を掴んで動かして初めて消える」という偶然の回避策
        しかなかった(現場の声:「棚検索の配置編集でつかみ→移動がないと
        強調表示水色枠が消えない」)。編集のON/OFFどちらでも捨てる ──
        編集を終えて検索結果に戻ったときも、前の編集作業中の当たりが
        残っていては紛らわしいため。

        **`selected` を捨てるのを忘れないこと。** 見た目が違う2つで、
        片方だけ消しても現場からは直って見えない:

            `highlight` → `STATE_HIT`。箱が**塗りつぶし**になる
            `selected`  → `is-selected`。箱に**3pxの水色の枠**が付く

        現場が「水色枠」と呼んでいるのは後者。しかも `search()` は
        最寄りの置き場を `selected` にも入れるので、検索すると2つとも
        点く。以前 `highlight` だけを捨てる修正を入れたが、枠のほうが
        残るため現象は何も変わっていなかった(同じ指摘を3度受けた)。
        簡易在庫が `clearSelection()`(Shift複数選択)だけでなく
        `picked`(単発クリック)も一緒に捨てているのと同じ理由。

        **編集を終えたら保存する。** 以前は終えても保存せず、「配置を保存を
        押してください」と出していたが、そのボタンは編集中にしか出ない
        (現場の声:「そんなボタン存在しない。配置編集を終了したら保存して
        いると思ってた。せっかく配置したのに」)。保存に失敗したときだけ
        編集を続ける ── 終えてしまうと、保存し直すボタンが見えなくなる。
        """
        self.highlight.clear()
        self.selected = ""
        if not on and self.editing and self.dirty:
            saved = self.save()
            if not saved.ok:
                return LayoutOpResult(
                    False, saved.message + "編集はそのまま続けています。"
                    "「配置を保存」をもう一度押してください。", saved.reason)
            self.editing = False
            return LayoutOpResult(True, "編集を終わりました。配置図を保存しました")
        self.editing = bool(on)
        if self.editing and conn is not None:
            placed = self._show_unplaced_bases(conn)
            if placed:
                return LayoutOpResult(
                    True, f"配置編集: ON ── マスタで足した拠点 {'・'.join(placed)} を図の左上に出しました。"
                    "動かして置いてください(編集を終えると保存します)")
        return LayoutOpResult(True, "配置編集: " + ("ON" if self.editing else "OFF"))

    def _show_unplaced_bases(self, conn: Any) -> list[str]:
        """マスタ(BoardMaster のデータラベル「拠点〇〇」)にあって図に無い拠点を図に出す。

        **図に出すまで距離は測れない**(座標が無い)。出す場所は左上から縦に並べる ──
        真ん中に重ねて出すと、どれがどれか分からない。保存は編集を終えたとき(ほかの
        編集と同じ)。出しただけで保存しないのは、置き場所を決めるのが人だから
        """
        placed = []
        for index, name in enumerate(svc.unplaced_base_points(conn)):
            self.plan.base_points.append(floor_plan.MapItem(
                name=name, x=10.0, y=10.0 + index * 18.0, w=66.0, h=12.0))
            placed.append(name)
        if placed:
            self.dirty = True
            log.info("マスタの拠点を図に出しました: %s", placed)
        return placed

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
        from . import map_data
        x, y = map_data.clamp_point(x, y, item.w, item.h,
                                    self.plan.width, self.plan.height)
        if not self.plan.move_item(name, x, y):
            return LayoutOpResult(False, f"{name} を動かせませんでした。",
                                  REFUSE_FAILED)
        self.dirty = True
        return LayoutOpResult(True, "")

    def resize(self, name: str, w: float, h: float) -> LayoutOpResult:
        """置き場の大きさを変える(背景の写真に合わせこむため)。

        出荷時の大きさは**どれも同じ四角**です。実際の棚は間口も奥行も
        まちまちなので、写真を下敷きにすると箱だけが浮きます。
        大きさを変えられれば、図を見た瞬間に現場と重なります。
        """
        if not self.editing:
            return LayoutOpResult(False, "先に「配置編集」をONにしてください。",
                                  REFUSE_BAD_INPUT)
        item = self.plan.item(name)
        if item is None:
            return LayoutOpResult(False, f"{name} は配置図にありません。",
                                  REFUSE_NOT_LISTED)
        from . import map_data
        w, h = map_data.clamp_size(w, h, self.plan.width, self.plan.height)
        if not self.plan.resize_item(name, w, h):
            return LayoutOpResult(False, f"{name} の大きさを変えられませんでした。",
                                  REFUSE_FAILED)
        # 大きくした結果、図からはみ出すことがある。動かすときと同じ枠に戻す
        x, y = map_data.clamp_point(item.x, item.y, w, h,
                                    self.plan.width, self.plan.height)
        self.plan.move_item(name, x, y)
        self.dirty = True
        return LayoutOpResult(True, "")

    def arrange(self, names: list[str], op: str) -> LayoutOpResult:
        """選んだ置き場をそろえる/詰める(`map_data.arrange`)。

        **全部そろってから動かす。** 1つでも図に無い名前があれば、
        何も動かさずに断ります ── 途中まで動いた状態で止まると、
        どこまで直ったのかが画面から読めません。

        動かすのは今までの `resize` / `move` と同じ道なので、枠に収める
        決まりも「保存するまでファイルに書かない」も同じです。
        """
        from . import map_data
        if not self.editing:
            return LayoutOpResult(False, "先に「配置編集」をONにしてください。",
                                  REFUSE_BAD_INPUT)
        if op not in map_data.ARRANGE_OPS:
            return LayoutOpResult(False, "その並べ方はありません。", REFUSE_BAD_INPUT)
        names = list(dict.fromkeys(str(n) for n in names))    # 重複を落とす
        if len(names) < 2:
            return LayoutOpResult(
                False, "Shift+クリックで2つ以上選んでから押してください。",
                REFUSE_BAD_INPUT)
        boxes = {}
        for name in names:
            item = self.plan.item(name)
            if item is None:
                return LayoutOpResult(False, f"{name} は配置図にありません。",
                                      REFUSE_NOT_LISTED)
            boxes[name] = (item.x, item.y, item.w, item.h)
        for name, (x, y, w, h) in map_data.arrange(boxes, op).items():
            if (w, h) != boxes[name][2:]:
                done = self.resize(name, w, h)
                if not done.ok:
                    return done
            done = self.move(name, x, y)
            if not done.ok:
                return done
        return LayoutOpResult(
            True, f"{map_data.ARRANGE_LABELS[op]}({len(names)}件)")

    def place_background(self, x: float, y: float,
                         scale: float) -> LayoutOpResult:
        """背景の写真そのものをずらす・拡げ縮めする。

        **箱を動かさずに下敷きを合わせる**ための操作です。写真の画角は
        図の枠と一致しないので、枠いっぱいに引き伸ばすと必ずずれます。
        """
        if not self.editing:
            return LayoutOpResult(False, "先に「配置編集」をONにしてください。",
                                  REFUSE_BAD_INPUT)
        from . import map_data
        self.plan.background = (self.plan.background
                                .placed_at(x, y)
                                .scaled_to(map_data.clamp_scale(scale)))
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

    def add_base(self, name: str, conn: Any) -> LayoutOpResult:
        """拠点を足す(VER4.8.85)。**マスタに足して**、図の中央に置く。

        現場の声:「マスタに拠点を追加して、配置編集で拠点追加できるようにしてください。
        現在の L-1、HVC、LVC はハードコードのデフォルト拠点として残してください」

        マスタ(BoardMaster)に「拠点〇〇」の行(ボード幅・丈 0、ボードタイプ 拠点)を足す
        (`location_service.master_base_points` が読む形。マスタ管理で手で足すのと同じ)。マスタに
        あるので、ほかの端末にも次の取り込みで拠点の選択肢・作業ラインの選択肢に出る。図の位置は
        この端末の配置図(保存は編集を終えたとき。ほかの端末へは配布設定で配る)。
        既定の拠点(L1 / HVC / LVC)と、もうある拠点は足さない。
        """
        if not self.editing:
            return LayoutOpResult(False, "先に「配置編集」をONにしてください。",
                                  REFUSE_BAD_INPUT)
        text = unicodedata.normalize("NFKC", name or "").strip()
        if text.startswith(svc.BASE_PREFIX):
            text = text[len(svc.BASE_PREFIX):].strip()   # 「拠点大板」と打っても「大板」
        if not text:
            return LayoutOpResult(False, "拠点の名前を入力してください(例 大板)。",
                                  REFUSE_BAD_INPUT)
        if "," in text or "、" in text:
            return LayoutOpResult(False, "拠点の名前に「,」は使えません(マスタのデータラベルの区切りです)。",
                                  REFUSE_BAD_INPUT)
        key = svc.base_key(text)
        for default in floor_plan.DEFAULT_BASE_POINTS:
            if svc.base_key(default) == key:
                return LayoutOpResult(False, f"{default} は既定の拠点です(足さなくても使えます)。",
                                      REFUSE_BAD_INPUT)
        for existing in self.plan.base_point_names:
            if svc.base_key(existing) == key:
                return LayoutOpResult(False, f"拠点 {existing} はもう図にあります。",
                                      REFUSE_BAD_INPUT)
        in_master = any(svc.base_key(n) == key for n in svc.master_base_points(conn))
        said = ""
        if not in_master:
            from . import master_admin
            written = master_admin.add_row(conn, "BoardMaster", {
                "ボード幅": "0", "ボード丈": "0", "ボードタイプ": svc.BASE_PREFIX,
                "データラベル": f"{svc.BASE_PREFIX}{text}"})
            if not written.ok:
                reason = (REFUSE_NOT_ALLOWED if written.reason == "not_allowed" else REFUSE_FAILED)
                return LayoutOpResult(False, f"拠点 {text} をマスタに足せませんでした: {written.message}",
                                      reason)
            said = "マスタ(BoardMaster)に足し、"
        self.plan.base_points.append(floor_plan.MapItem(
            name=text, x=self.plan.width / 2, y=self.plan.height / 2, w=66.0, h=12.0))
        self.dirty = True
        log.info("拠点を足しました: %s(マスタ%s)", text, "に足した" if said else "にあった")
        return LayoutOpResult(
            True, f"拠点 {text} を{said}図の中央に置きました。動かして置いてください"
                  "(編集を終えると保存します)。ほかの端末には次の取り込みで選択肢に出ます")

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
        # **差し替えたら位置と倍率は初期に戻す。** 前の写真に合わせた
        # ずらし量を新しい写真へ持ち越すと、開いた瞬間に枠の外へ出ている
        # ことがあり、直し方が分からなくなる
        self.plan.background = map_data.Background(image=image)
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


def has_unsaved() -> bool:
    """保存していない編集があるか。**作業状態を作らずに**答える。

    見張り(`/api/health`)と「終了」が聞く。画面を一度も開いていなければ
    作業状態そのものが無いので「無い」── ここで作ってしまうと、聞いた
    だけで図のファイルを読みに行くことになる。
    """
    return bool(_session is not None and _session.dirty)


# 未保存を知らせるときの呼び名
UNSAVED_LABEL = "棚検索の配置図"


def reset_session() -> None:
    """テスト用。プロセスに1つという前提を壊さずに作り直す。"""
    global _session
    with _lock:
        _session = None


def _size_label(width: Optional[int], length: Optional[int]) -> str:
    """打たれた寸法の言い方。片側だけのときは、そう分かるように書く。"""
    if width is not None and length is not None:
        return f"{width}×{length}"
    if width is not None:
        return f"幅{width}"
    return f"丈{length}"


def _not_in_stock(width: Optional[int], length: Optional[int]) -> str:
    """その寸法の資材が見当たらない。**在庫の話。**"""
    return (f"{_size_label(width, length)} の資材が見当たりません。"
            "寸法を確かめてください(片方だけでも探せます)。")


def _no_shelf(hits: list) -> str:
    """資材はあるが、どこに置いてあるかが分からない。**置き場の話。**

    以前は「このサイズには置き場(データラベル)が登録されていません。
    マスタの「データラベル」列を確認してください。」と出していた。
    作り手の言葉で、読んだ作業者は「あぁ無いのか」で終わってしまう
    (現場の指摘:「ツール制作者よりのコメントすぎる」)。

    **作業者が次にすることを書く。** その場で直せるものではないので、
    誰に言えばよいかまで書く。
    """
    names = " / ".join(sorted({KIND_LABEL[h.kind] for h in hits}))
    sizes = " / ".join(sorted({s for h in hits for s in h.matched_sizes}))
    return (f"{names} {sizes} はありますが、配置図のどこに置いてあるかが"
            "分かりません。置き場の登録がまだのようです ── "
            "資材課の人に伝えてください(マスタの「データラベル」欄)。")
