"""ロット検索画面の表示内容

UIツールキットに依存しない。tkinter からも Flask からも同じものを呼ぶ。

【この層の責務】
`lot_service.search_lot()` が返す結果を、**そのまま並べれば画面になる形**に
組み替える。項目の並び・見出し・強調するかどうかは表示の仕様であって、
tkinter と Web で違ってはいけない。

【並び順は仕様】
VBA `CreatePage1` のフレーム順(検索条件 → ロット情報 → 引当情報 → 受注情報)と
`capLot` / `lblOdr` の項目順をそのまま引き継ぐ。データの流れる順に並んでおり、
入れ替えは仕様変更にあたる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import lot_service

# ロット情報の表示項目(VBA `capLot` のキャプションと `lblLot` の対応)
#
# 【まとまりを付ける理由】
# VBAは12項目を縦一列に並べていた。並び自体は意味のある順序なので変えないが、
# 12個を等間隔に置くと「板厚・板幅・板丈」と「オーダー板厚・板幅・板丈」が
# 見た目で区別できない。実際に間違えると資材が1サイズずれる。
# 順序はそのままに、**近接**でまとまりだけ作る(一度に把握できるのは4±1)。
# 見出し語はVBAのキャプションのまま使う。詳細は
# docs/UIUX設計指針.md の情報アーキテクチャの節。
LOT_FIELD_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("品目", (
        ("用途コード", "yoto_code"),
        ("用途名", "yoto_name"),
        ("製造材質", "zaishitsu"),
        ("製造調質", "choshitsu"),
    )),
    # このまとまりの見出しだけは、BOXコースのとき「BOX実績寸法」に変わる
    ("製造寸法", (
        ("板厚", "thickness"),
        ("板幅", "width"),
        ("板丈", "length"),
    )),
    ("オーダー寸法", (
        ("オーダー板厚", "order_thickness"),
        ("オーダー板幅", "order_width"),
        ("オーダー板丈", "order_length"),
    )),
    ("設備コース", (
        ("設計_設備コース", "design_course"),
        ("実績_設備コース", "actual_course"),
    )),
)

# 平らに並べたもの。**順序はVBA `capLot` のまま**で、上のまとまりは
# 区切りを入れているだけ(順序を変える余地を残さないよう、ここで導出する)
LOT_FIELDS: tuple[tuple[str, str], ...] = tuple(
    item for _, items in LOT_FIELD_GROUPS for item in items)

# 寸法のまとまり。BOXコースのときは見出しが「BOX実績寸法」になる
DIMENSION_GROUP = "製造寸法"

# 受注情報の表示項目(VBA `lblOdr(0..7)`)
ODR_FIELDS: tuple[tuple[str, str], ...] = (
    ("納入先名称", "delivery_name"),
    ("包装仕様NO", "packaging_spec"),
    ("取引先名称", "customer_name"),
    ("送り先名称", "ship_to_name"),
    ("納送用コメント", "delivery_comment"),
    ("工場用コメント", "factory_comment"),
    ("VC_表", "vc_front"),
    ("VC_裏", "vc_back"),
)

# 値が長くなりがちな項目(例 "HOT PSW ST2 ANF ST2 PSW KEN")。
# 2列に収めず1行を丸ごと使う
COURSE_FIELDS = ("design_course", "actual_course")

# 小数で表示する項目(VBA の Format 指定に対応)
THICKNESS_FIELDS = ("thickness", "order_thickness")
DIMENSION_FIELDS = ("width", "length", "order_width", "order_length")

# BOXコースのとき赤くする項目。
# 板厚・板幅・板丈が**BOX実績の値に差し替わっている**ことを示す
# (VBA `lblLot(10).ForeColor = RGB(200,0,0)` と、寸法3つの強調)
BOX_HIGHLIGHT_FIELDS = ("thickness", "width", "length", "design_course")

# 社内URL(VBA `URL_LOT_DISPLAY` / `URL_HOSO_SHIYOSHO`)。
# **包装仕様NOだけ**が包装仕様書の閲覧画面を持つ。ほかの欄は開く先が
# 無いのでリンクにしない(開きそうに見えて紛らわしい)。
URL_LOT_DISPLAY = "http://nlmfangyweb1a/lotdsp/"
URL_HOSO_SHIYOSHO = ("http://nlmfangysysv:9084/NgyPkgWeb/"
                     "#!/home?mode=hosoShiyoshoEtsuran")


@dataclass
class Field:
    """画面に出す1項目。"""

    label: str
    key: str
    value: str
    highlight: bool = False      # BOX実績に差し替わっている等、目を引かせる
    wide: bool = False           # 1行を丸ごと使う
    url: str = ""                # 押すと開く先(無ければ空)
    group: str = ""              # このまとまりの見出し(連続する同じ値が1組)


@dataclass
class Badge:
    """見出しの横に出す短い標識。

    VBA はフレームの caption に `---【BOX実績寸法】---前工程実績数: 248枚` の
    ように全部つないでいた。区切りの `---` は文字数を稼ぐためのもので、
    意味は持たない。語はそのままに、標識として切り出す。
    """

    text: str
    kind: str = "info"           # "info" | "alert"


@dataclass
class LotViewModel:
    """ロット検索画面ぜんぶ。"""

    found: bool = False
    message: str = ""
    lot_no: str = ""
    # 見出しは題と標識に分ける。VBA は caption 1本に詰め込んでいた
    lot_title: str = "ロット情報 (SIKALOT)"
    odr_title: str = "受注情報 (SIKAODR)"
    lot_badges: list[Badge] = field(default_factory=list)
    odr_badges: list[Badge] = field(default_factory=list)
    # 寸法が差し替わっている理由。黙って値だけ変えない。
    # `dimension_group` は、その説明をどのまとまりの下に置くか
    dimension_note: str = ""
    dimension_group: str = ""
    # BOX最終実績寸法の候補(仕掛台帳の2つ目の置き場所から)。BOX実績寸法のロットで、
    # 1つ目の BOX最終実績寸法が空か HOT のときだけ並ぶ。見出しの横の選ぶ欄に出す
    box_choices: list[dict[str, Any]] = field(default_factory=list)
    box_pick: str = ""
    is_box: bool = False
    is_ex: bool = False
    # 試験指示票。「要 / 不要」であって、検索の成否ではない
    test_slip: str = ""
    test_slip_needed: bool = False
    # 要否の**判定根拠**。「要」とだけ出しても、現場は正しいか確かめられない
    test_slip_checks: list[dict[str, Any]] = field(default_factory=list)
    test_slip_note: str = ""
    lot_fields: list[Field] = field(default_factory=list)
    odr_fields: list[Field] = field(default_factory=list)
    hiki: list[dict[str, Any]] = field(default_factory=list)
    # 包装仕様NOだけは欄の中だけでなく単独でも渡す。画面がこれを使って
    # 図面(`spec_sheet`)を出しに行くので、12項目から探させない
    packaging_spec: str = ""
    specific_gravity: str = ""
    final_process_count: int = 0
    # 資材選択へ渡せるか(製造板幅・板丈が取れているか)
    can_expand: bool = False
    expand_reason: str = ""


def has_data(conn) -> bool:
    """仕掛台帳が取り込まれているか。"""
    return lot_service.has_lot_data(conn)


def normalize(lot_no: str) -> str:
    """入力を半角・大文字にそろえる(VBA `TXT_Change` 相当)。

    全角や小文字で打たれても検索が通るようにするためのもの。
    """
    from .. import text_input
    return text_input.to_narrow_alnum(lot_no or "").strip()


def is_complete(lot_no: str) -> bool:
    """7桁そろったか。そろった時点で自動検索する(VBA踏襲)。"""
    return len(normalize(lot_no)) == lot_service.LOT_NO_LENGTH


def search(conn, lot_no: str) -> LotViewModel:
    """検索して、そのまま並べれば画面になる形にして返す。"""
    result = lot_service.search_lot(conn, normalize(lot_no))
    return build(result)


def build(result: lot_service.LotSearchResult) -> LotViewModel:
    if not result.found:
        return LotViewModel(found=False, message=result.message,
                            test_slip=lot_service.TEST_SLIP_UNKNOWN)

    lot, odr = result.lot, result.odr
    return LotViewModel(
        found=True,
        message=result.message,
        lot_no=lot.lot_no,
        lot_title="ロット情報 (SIKALOT)",
        odr_title="受注情報 (SIKAODR)",
        lot_badges=_lot_badges(lot),
        odr_badges=_odr_badges(lot, odr),
        dimension_note=_dimension_note(lot),
        dimension_group=f"{lot.dimension_source}寸法",
        box_choices=[{"key": c.key, "label": c.label, "equipment": c.equipment,
                      "selected": c.key == lot.box_pick} for c in lot.box_choices],
        box_pick=lot.box_pick,
        is_box=lot.is_box,
        is_ex=odr.is_ex,
        test_slip=lot.test_slip_text,
        test_slip_needed=lot.needs_test_slip,
        test_slip_checks=[{"label": label, "met": met, "value": value}
                          for label, met, value in lot.test_slip_checks()],
        test_slip_note=_test_slip_note(lot),
        lot_fields=[_lot_field(lot, group, label, key)
                    for group, items in LOT_FIELD_GROUPS
                    for label, key in items],
        odr_fields=[_odr_field(odr, label, key) for label, key in ODR_FIELDS],
        hiki=[_hiki_row(row, selected=(row.order_no == odr.order_no))
              for row in result.hiki],
        packaging_spec=odr.packaging_spec,
        specific_gravity=f"{odr.specific_gravity:g}",
        final_process_count=lot.final_process_count,
        can_expand=bool(lot.width and lot.length),
        expand_reason=("" if lot.width and lot.length
                       else "製造板幅・板丈が取得できていません"),
    )


def _test_slip_note(lot: lot_service.LotInfo) -> str:
    """要否の結論を一言で。**何をすればよいか**まで書く。

    VBA `AdvanceCheck` は「表面処理が免除対象でなく、かつ
    (板厚が薄い または 用途コードが該当)」で要と判定する。
    条件が3つあって片方はAND、片方はORなので、結論だけ見ても
    どちらの理由で要になったのかが分からない。
    """
    if lot.needs_test_slip:
        return "先行データ(試験指示票)を用意してください。"
    surface = (lot.quality_surface or "").strip()
    if surface in lot_service.ADVANCE_CHECK_EXEMPT_SURFACES:
        return f"品質グレード_表面処理が {surface} のため不要です。"
    return "板厚・用途コードのどちらも該当しないため不要です。"


def _lot_badges(lot: lot_service.LotInfo) -> list[Badge]:
    """VBA `fraLotInfo.caption` に詰め込まれていた情報。"""
    badges = []
    if lot.is_box:
        badges.append(Badge("BOX実績寸法", "alert"))
    badges.append(Badge(f"最終実績数 {lot.final_process_text or lot.final_process_count}枚"))
    return badges


def _odr_badges(lot: lot_service.LotInfo, odr: lot_service.OdrInfo) -> list[Badge]:
    """VBA `fraOdrInfo.caption` の `EX` と設備コース。

    EX(輸出)は梱包の仕様が変わる。見落とすと積み直しになるので目を引かせる。
    """
    badges = []
    if odr.is_ex:
        badges.append(Badge("EX", "alert"))
    if lot.box_course:
        badges.append(Badge(lot.box_course))
    return badges


def _dimension_note(lot: lot_service.LotInfo) -> str:
    """寸法欄が何の値なのかを一言で。差し替わっているときだけ出す。

    値だけ黙って変えると、製造寸法だと思って読まれる。
    """
    if not lot.is_box:
        return ""
    note = (f"設計_設備コースが {lot.box_course} のため、"
            "製造寸法ではなくBOX実績寸法を表示しています")
    # 1つ目の値が使えない理由。HOT は値が入っていても使わない(使うに値しない、と現場)ので、
    # 「無い」とは書かずに、なぜ使わないかを書く
    if lot.box_final_hot:
        why = (f"1つ目の仕掛台帳の BOX最終実績_設備名が {lot.box_final_problem} のため、"
               "その寸法は使いません")
    else:
        why = "1つ目の仕掛台帳に BOX最終実績寸法がありません"
    picked = lot.picked_choice
    if picked is not None:
        # **どの値を使っているか**を黙らない。1つ目の値ではないことを書く
        return (f"{note}。{why}。2つ目の"
                f" {picked.equipment or '(設備名なし)'} の{picked.source}寸法を使っています")
    if lot.box_choices:
        return (f"{note}。{why}。"
                "横の欄で、2つ目の仕掛台帳の BOX設計_設備名 を選ぶと、その寸法を使います")
    if lot.box_final_missing:
        from .. import config
        where = ("2つ目の仕掛台帳にもありません" if config.lot_db_dir2() is not None
                 else "2つ目の仕掛台帳の置き場所を設定すると、そちらから選べます")
        return f"{note}。{why}({where})"
    return note


def _lot_field(lot: lot_service.LotInfo, group: str, label: str, key: str) -> Field:
    value = getattr(lot, key)
    if key in THICKNESS_FIELDS:
        text = lot_service.format_thickness(value)
    elif key in DIMENSION_FIELDS:
        text = lot_service.format_dimension(value)
    else:
        text = str(value)
    return Field(
        label=label, key=key, value=text or "---",
        # BOXコースのときだけ強調する。板厚・板幅・板丈が
        # 製造値ではなくBOX実績に差し替わっていることを示すため
        highlight=lot.is_box and key in BOX_HIGHLIGHT_FIELDS,
        wide=key in COURSE_FIELDS,
        # 寸法のまとまりだけ、見出しでどちらの値かを示す
        group=(f"{lot.dimension_source}寸法" if group == DIMENSION_GROUP else group),
    )


def _odr_field(odr: lot_service.OdrInfo, label: str, key: str) -> Field:
    return Field(
        label=label, key=key, value=str(getattr(odr, key) or "---"),
        url=URL_HOSO_SHIYOSHO if key == "packaging_spec" else "",
    )


def build_odr_switch(odr: lot_service.OdrInfo, lot: lot_service.LotInfo) -> dict[str, Any]:
    """引当行をクリックしたときに差し替える、受注情報だけの断片。

    `build()`(画面ぜんぶ)ではなく、ここだけを返す ── ロット情報・
    引当一覧・図面は変わらないので、そこまで作り直すと重いだけでなく、
    製品サイズの入力中だったものが再描画で消える(現場の声と同種の
    「触っていないのに変わった」を起こす)。

    `order_no` は `odr.order_no`(実際に解決された受注番号)をそのまま
    返す。呼び出し元がURLで受け取るのは引当NOだが、画面が知りたいのは
    「結果としてどの受注番号が出ているか」なので、ここは受注番号で返す。
    """
    return {
        "order_no": odr.order_no,
        "odr_badges": [_badge_dict(b) for b in _odr_badges(lot, odr)],
        "odr_fields": [_field_dict(_odr_field(odr, label, key))
                       for label, key in ODR_FIELDS],
        "packaging_spec": odr.packaging_spec,
        "specific_gravity": f"{odr.specific_gravity:g}",
        "is_ex": odr.is_ex,
    }


def _hiki_row(row: lot_service.HikiRow, *, selected: bool = False) -> dict[str, Any]:
    """引当情報の1行。**引当番号の昇順**は `lot_service` が済ませている。

    引当数量の表示(`quantity_text`)は `lot_service` が決める。
    tkinter版も同じものを読むので、ここで作り直さない。

    `selected` は、この行が指す受注番号が**いま受注情報欄に出ている
    ものか**。クリックで切り替えられることを示す(押せる行だと
    分からないと、そもそも押そうとされない)。
    """
    return {
        "order_no": row.order_no,
        "quantity": f"{row.quantity:g}",
        "quantity_text": row.quantity_text,
        "adjust_no": row.adjust_no,
        "hiki_no": row.hiki_no,
        "display_value": row.display_value,
        "is_all": row.is_all,
        "selected": selected,
    }


def to_dict(view: LotViewModel) -> dict[str, Any]:
    """JSONにできる形。Web版のAPIが返す。"""
    return {
        "found": view.found,
        "message": view.message,
        "lot_no": view.lot_no,
        "lot_title": view.lot_title,
        "odr_title": view.odr_title,
        "lot_badges": [_badge_dict(b) for b in view.lot_badges],
        "odr_badges": [_badge_dict(b) for b in view.odr_badges],
        "dimension_note": view.dimension_note,
        "dimension_group": view.dimension_group,
        "box_choices": view.box_choices,
        "box_pick": view.box_pick,
        "is_box": view.is_box,
        "is_ex": view.is_ex,
        "test_slip": view.test_slip,
        "test_slip_needed": view.test_slip_needed,
        "test_slip_checks": view.test_slip_checks,
        "test_slip_note": view.test_slip_note,
        "lot_fields": [_field_dict(f) for f in view.lot_fields],
        "odr_fields": [_field_dict(f) for f in view.odr_fields],
        "hiki": view.hiki,
        "packaging_spec": view.packaging_spec,
        "specific_gravity": view.specific_gravity,
        "final_process_count": view.final_process_count,
        "can_expand": view.can_expand,
        "expand_reason": view.expand_reason,
    }


def _field_dict(item: Field) -> dict[str, Any]:
    return {"label": item.label, "key": item.key, "value": item.value,
            "highlight": item.highlight, "wide": item.wide, "url": item.url,
            "group": item.group}


def _badge_dict(item: Badge) -> dict[str, Any]:
    return {"text": item.text, "kind": item.kind}
