"""資材選択画面の状態機械

包装仕様NOで切り替わるモード群(1P0113 裸梱包 / プロテック / 上下共用 / EX)を
持つ。VBA `SearchAndDisplay` 末尾の

    Apply1P0113Mode → GetUpperPartMaterial(→ShowAngleControls) → CheckAndSetProtecMode

という**順序依存の状態機械**をそのまま引き継いでいる。
互いに独立ではない(例: プロテック中は上下共用が抑止される)ので、
`apply_lot()` の中の呼び出し順を変えてはいけない。

【この層の責務】
- 状態を持つ
- 「何を表示すべきか」を**真偽値と文言で**返す
- 選定ログ(`UserLog`)への記録

【この層がやらないこと】
- ウィジェット操作・DOM操作(呼び出し側の仕事)
- 入力欄の値を自分で読むこと(引数で受け取る)

入力欄を自分で読まないのは、VBA `Load1P0113Materials` が
`txtProductWidth.text` を直接読んでいた挙動を保つため。1P0113では
パレットが無く「セット」(パレットに収まるかの検証)を通せないので、
確定済みの製品サイズではなく**入力欄の値**を見るのが正しい。
その値の出どころ(tkinterのEntry / HTMLのinput)は呼び出し側が知っている。
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import (
    lot_service,
    material_service,
    special_packaging as spk,
)
from ..logging_utils import get_logger
from ..user_log import UserLog

log = get_logger("presenters.selection")

# 保護材が「アングル」でも「一致なし」でもないとき、上用は下用と同じ板になる。
# `""`(対象外)と`"一致なし"`(マスタ未登録)はどちらも安全側に倒して
# アングルを出したままにする(VBA `ShowAngleControls` と同じ判断)。
HOSOZAI_NO_MATCH = "一致なし"

# 下用の見出し。上下共用モードでは文言が変わる(VBA踏襲)
TITLE_LOWER = "【下用ボード配置】"
TITLE_LOWER_SHARED = "【上下共用ボード配置】"

# モードバーの種類。色は表示側(CSS / tkinter)が決める。
# 複合(上下共用+EX)は専用の種別を持つ ── 「上下共用」の青と「EX」の赤を
# 単純に混ぜると誰の色でもなくなるので、複合用の見た目をCSS側に用意する
BANNER_NONE = ""
BANNER_1P0113 = "1p0113"
BANNER_PROTEC = "protec"
BANNER_1P1185 = "1p1185"
BANNER_SHARED = "shared"
BANNER_EX = "ex"
BANNER_SHARED_EX = "shared_ex"


@dataclass
class ModeBanner:
    """操作パネル最上部の帯(VBA `DynamicTip_ProtecMark` / `DynamicTip_ExMark`)。

    `kind` は表示側が色を選ぶための種別。`text` が空なら通常モード。

    【複数モードの同時表示について】
    1P0113・プロテック・上下共用は互いに排他(状態機械の設計より)。
    EXだけは独立(EX受注のまま上下共用になることがある)ので、
    「上下共用のときにEXでもある」場合だけ1本のバーに両方を書く
    (現場の要望:「1つのバーに全て並べて出す」)。1P0113・プロテックは
    そもそもボード欄自体を隠すか、EXと同時に成立しないため、単独表示のまま
    でよい。
    """

    kind: str = BANNER_NONE
    text: str = ""

    @property
    def visible(self) -> bool:
        return bool(self.text)


@dataclass
class Mode1P0113Result:
    """`apply_1p0113_mode()` の結果。

    `changed` が True のときだけ、呼び出し側はパネルの入れ替えを行う
    (パレットサイズのセクションと1P0113パネルを**同じ位置で**差し替える)。
    """

    changed: bool = False
    mode_on: bool = False
    materials_reloaded: bool = False
    reset_qty_to: Optional[int] = None


@dataclass
class ProtecResult:
    """`check_and_set_protec_mode()` の結果。"""

    target_board_type: str = ""
    # 呼び出し側がボード種別の一覧を渡してきた場合のみ意味を持つ
    should_switch: bool = False
    missing_from_master: bool = False


@dataclass
class HosozaiResult:
    """`apply_hosozai()` の結果。"""

    hosozai: str = ""
    show_angle: bool = True
    lower_title: str = TITLE_LOWER
    # アングルを隠したときだけ「使用保護材: ○○」を出す(消えた理由を示すため)
    hosozai_label: str = ""


@dataclass
class SelectionPresenter:
    """資材選択画面の状態。

    `conn` 以外はすべて既定値を持つので、テストからは
    `SelectionPresenter(conn)` だけで作れる。
    """

    conn: sqlite3.Connection
    user_log: UserLog = field(default_factory=UserLog)

    # --- ロット検索タブから受け取る情報 (VBA valLot4 / m_isExOrder) ---
    lot_no: str = ""
    lot_result: Optional[Any] = None
    manufactured_thickness: Optional[float] = None
    is_ex_order: bool = False

    # --- 包装仕様NOで切り替わるモード群 ---
    mode_1p0113: bool = False
    force_1p0113: bool = False
    protec: spk.ProtecState = field(default_factory=spk.ProtecState)
    mode_1p1185: spk.Mode1P1185State = field(default_factory=spk.Mode1P1185State)
    last_hosozai: str = ""
    materials_1p0113: spk.Materials1P0113 = field(default_factory=spk.Materials1P0113)

    # ------------------------------------------------------------------
    # 接続はスレッドごと
    # ------------------------------------------------------------------
    # 作業状態(このオブジェクト)はプロセスに1つだが、`sqlite3` の接続は
    # 作ったスレッドでしか使えない。以前は要求のたびに `conn` を差し替えて
    # いたので、**要求が2つ重なると、先の要求が後の要求の接続を使って**
    # 「SQLite objects created in a thread can only be used in that same
    # thread」で 500 になった(ほかのサイトのタブが資材選択の画面を読み込んだ
    # のと、利用者の操作が重なった試験で見つけた。帳票・マスタ管理の権限判定
    # なども同じセッションを引くので、ふだんの操作でも重なりうる)。
    # `conn` への代入と読み出しを、そのスレッドの置き場へ向ける
    def __setattr__(self, name: str, value: Any) -> None:
        if name == "conn":
            self._thread_conns().conn = value
            # そのスレッドで一度も差していないとき(試験・起動時)の予備
            object.__setattr__(self, "_last_conn", value)
            return
        object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Any:
        if name == "conn":
            local = self._thread_conns()
            found = getattr(local, "conn", None)
            return found if found is not None else self.__dict__.get("_last_conn")
        raise AttributeError(name)

    def _thread_conns(self) -> threading.local:
        local = self.__dict__.get("_conns")
        if local is None:
            local = threading.local()
            object.__setattr__(self, "_conns", local)
        return local

    # ------------------------------------------------------------------
    # 包装仕様NO
    # ------------------------------------------------------------------
    def current_packaging_spec(self) -> str:
        """VBA `GetCurrentHososiyoNo` 相当。表示中の包装仕様NO。"""
        if self.lot_result is None:
            return ""
        return self.lot_result.odr.packaging_spec

    # ------------------------------------------------------------------
    # 1. 1P0113(裸梱包)モード
    # ------------------------------------------------------------------
    def apply_1p0113_mode(self, packaging_spec: str, *,
                          product_width: int = 0,
                          product_length: int = 0) -> Mode1P0113Result:
        """VBA `Apply1P0113Mode` の移植。

        強制トグルがONなら包装仕様NOに関わらず1P0113として扱う。
        `product_width` / `product_length` は**入力欄の値**(冒頭の注記を参照)。
        """
        effective = (spk.HOSOSIYO_1P0113 if self.force_1p0113
                     else (packaging_spec or "").strip())
        mode_on = spk.is_1p0113(effective)

        if mode_on == self.mode_1p0113:
            # 状態が変わらない場合でも、資材は製品サイズに依存するので引き直す
            if mode_on:
                update = self.load_1p0113_materials(product_width, product_length)
                return Mode1P0113Result(changed=False, mode_on=True,
                                        materials_reloaded=True,
                                        reset_qty_to=update)
            return Mode1P0113Result(changed=False, mode_on=False)

        self.mode_1p0113 = mode_on
        log.debug("apply_1p0113_mode: 包装仕様NO=%s 強制=%s → mode=%s",
                  packaging_spec, self.force_1p0113, mode_on)

        if mode_on:
            self.user_log.log(
                f"[{spk.HOSOSIYO_1P0113}] 裸梱包モード有効 "
                f"(パレット・ボードは使わず角材+松板で組みます)", emphasis=True)
            update = self.load_1p0113_materials(product_width, product_length)
            return Mode1P0113Result(changed=True, mode_on=True,
                                    materials_reloaded=True, reset_qty_to=update)

        self.user_log.log(f"[{spk.HOSOSIYO_1P0113}] 裸梱包モード解除")
        return Mode1P0113Result(changed=True, mode_on=False)

    def load_1p0113_materials(self, product_width: int,
                              product_length: int) -> Optional[int]:
        """VBA `Load1P0113Materials` の移植(資材の引き直しまで)。

        戻り値は「乗数スピンをこの値で入れ直せ」という指示。`None` なら据え置き。
        製品サイズが変わったときだけ総梱包数で初期化し直すのは、
        利用者が手で変えた値をサイズ据え置きの再読込で潰さないため。
        """
        if not self.mode_1p0113:
            return None

        previous = self.materials_1p0113
        size_changed = (product_width != previous.product_width
                        or product_length != previous.product_length)

        self.materials_1p0113 = spk.load_materials(
            self.conn, product_width, product_length)
        m = self.materials_1p0113

        if m.kakuzai.is_ok and m.matsuita.is_ok:
            self.user_log.log(
                f"[{spk.HOSOSIYO_1P0113}] 角材 {m.kakuzai_length}mm "
                f"{m.base_kakuzai}本 / 松板 {m.a_value}mm {m.base_matsuita}枚")
        else:
            missing = [hit.name for hit in (m.kakuzai, m.matsuita) if not hit.is_ok]
            self.user_log.log(
                f"[{spk.HOSOSIYO_1P0113}] 資材が引けません: "
                f"{'・'.join(missing)} ({m.kakuzai.info})", emphasis=True)

        if not size_changed:
            return None
        # VBA は総梱包数を初期値にする(引当調整NO混在などで
        # 計算不可(-1)や0のときは1)。スピンの上限は10
        packages = (lot_service.calc_total_packages(self.lot_result)
                    if self.lot_result is not None else 0)
        initial = packages if packages > 0 else 1
        return min(initial, 10)

    def toggle_force_1p0113(self, *, product_width: int = 0,
                            product_length: int = 0) -> Mode1P0113Result:
        """VBA `lblForce1P0113_Click` の移植。

        ONにした瞬間に1P0113モードへ入り、OFFに戻したときは
        現在の包装仕様NOで判定し直す。
        """
        self.force_1p0113 = not self.force_1p0113
        spec = (spk.HOSOSIYO_1P0113 if self.force_1p0113
                else self.current_packaging_spec())
        return self.apply_1p0113_mode(
            spec, product_width=product_width, product_length=product_length)

    def material_1p0113_info(self, key: str) -> Optional[str]:
        """VBA `Show1P0113LabelTip` の移植。ラベルに対応するフル情報。

        未知のキーなら `None`(呼び出し側は表示を変えない)。
        """
        hit = (self.materials_1p0113.kakuzai if key == "kak"
               else self.materials_1p0113.matsuita if key == "mat" else None)
        if hit is None:
            log.debug("material_1p0113_info: 不明なkey=%s", key)
            return None
        return hit.full_info

    # ------------------------------------------------------------------
    # 2. プロテックモード
    # ------------------------------------------------------------------
    def check_and_set_protec_mode(
        self, packaging_spec: str, *,
        available_board_types: Optional[list[str]] = None,
        current_board_type: str = "",
    ) -> ProtecResult:
        """VBA `CheckAndSetProtecMode` の移植。

        プロテック対象ならボード種別を「プロテックボード」に、
        そうでなければ既定の「ハードボード」に戻すよう指示する。
        実際に切り替えるのは呼び出し側(ウィジェット/画面の都合があるため)。
        """
        self.protec = spk.protec_state(packaging_spec)
        target = self.protec.board_type

        result = ProtecResult(target_board_type=target)
        if available_board_types is not None:
            if target in available_board_types:
                result.should_switch = current_board_type != target
                if result.should_switch:
                    log.debug("check_and_set_protec_mode: ボード種別 → %s", target)
            else:
                # マスタに種別が無い環境(移行直後など)では切り替えられない。
                # 黙って通常種別のまま進むと理由が分からないので記録する。
                # アプリのログだけでは現場に届かないので**選定ログにも出す**
                # ── プロテックのつもりで別の種別から選ばれてしまう
                result.missing_from_master = True
                log.warning("ボード種別 '%s' がマスタにありません", target)
                self.user_log.log(
                    f"ボード種別 '{target}' がマスタにありません。"
                    f"種別を切り替えずに選定します", emphasis=True)

        if self.protec.is_protec:
            self.user_log.log(
                f"[プロテックモード] 有効 包装仕様NO={packaging_spec}"
                f" (上用は下用と同サイズ・許容"
                f"-{5 if self.protec.is_1p1216 else 80}mm)", emphasis=True)
        return result

    # ------------------------------------------------------------------
    # 2.5. 1P1185(タイト限定)モード
    # ------------------------------------------------------------------
    def check_and_set_1p1185_mode(
        self, packaging_spec: str, customer_name: str,
        manufactured_width: float, manufactured_length: float,
    ) -> None:
        """VBA `CheckAndSet1P1185Mode` の移植。

        発動条件(AND): 包装仕様NO=1P1185、取引先名称に「ﾅﾒｶﾜｱﾙﾐ」を
        含む、製造板幅・製造板丈がそれぞれ1242〜1249。有効になった
        瞬間だけ選定ログに記録する(VBA `changed` 判定と同じ)。
        """
        was_on = self.mode_1p1185.is_1p1185
        self.mode_1p1185 = spk.check_1p1185_mode(
            packaging_spec, customer_name, manufactured_width, manufactured_length)
        if self.mode_1p1185.is_1p1185 and not was_on:
            self.user_log.log(
                f"[1P1185タイト限定モード] 有効 取引先={customer_name}", emphasis=True)

    # ------------------------------------------------------------------
    # 3. 保護材 → アングルの要否 / 上下共用
    # ------------------------------------------------------------------
    def apply_hosozai(self, hosozai: str) -> HosozaiResult:
        """VBA `ShowAngleControls` の移植(状態保存と表示の判断)。

        保護材がアングル以外に確定したときは**上下共用モード**
        (上用=下用と同サイズ)になるので、上用の欄を丸ごと隠し、
        下用の見出しを「【上下共用ボード配置】」に変える。

        【アングル表示と上用キャンバス非表示は独立した判断】
        以前はプロテックのとき、アングルの実際の判定を見ずに
        無条件でアングルも上用キャンバスも非表示にしていた
        (VBA `ShowAngleControls` が1つの引数で両方を同時制御していた
        のと同じ構造的欠陥)。プロテックは「上用キャンバスは常に隠す
        (上下共用として扱う)」が、「アングルを表示するか」は
        `last_hosozai` の実際の判定(`show_angle` プロパティ)に従う
        ── 使用保護材が実際にアングルと判定されているなら、プロテック
        中でもアングルの一覧・配置は出す(現場の声への対応)。
        """
        self.last_hosozai = hosozai or ""
        show_angle = self.show_angle
        hide_upper = self.is_shared_board_mode
        log.debug("apply_hosozai: hosozai=%s protec=%s show_angle=%s hide_upper=%s",
                 hosozai, self.protec.is_protec, show_angle, hide_upper)
        if hide_upper:
            reason = ("プロテック" if self.protec.is_protec
                      else self.last_hosozai)
            self.user_log.log(
                f"[上下共用({reason})] 上用は下用と同サイズになります")
        return HosozaiResult(
            hosozai=self.last_hosozai,
            show_angle=show_angle,
            lower_title=TITLE_LOWER_SHARED if hide_upper else TITLE_LOWER,
            # アングルを出しているあいだはアングルの欄があるので出さない。
            # 消えたときだけ、その場所に何を使うのかを出す。プロテックは
            # アングルの有無に関わらず上用キャンバス自体を隠すので、
            # 「使用保護材」ではなく専用の文言にする
            hosozai_label=("" if not hide_upper
                           else (f"使用保護材: {self.last_hosozai}"
                                 if not self.protec.is_protec
                                 else "プロテックボード(上下共用)")),
        )

    def lookup_hosozai(self, result: Any) -> str:
        """VBA `GetUpperPartMaterial(lblOdr(1), lblLot(2,3,0,4,5,6))` の呼び出し。

        引数の並びは 包装仕様NO / 材質 / 調質 / 用途コード / 板厚 / 板幅 / 板丈。
        BOXコースなら板厚・板幅・板丈はBOX実績に差し替わった値が渡る。
        """
        lot = result.lot
        try:
            return material_service.get_upper_part_material(
                self.conn,
                pack=result.odr.packaging_spec,
                zai=lot.zaishitsu, tyo=lot.choshitsu, you=lot.yoto_code,
                atu=lot.thickness, hab=lot.width, tak=lot.length,
            )
        except Exception as exc:                      # noqa: BLE001 - 補助情報
            # 保護材が引けなくてもボード選定自体は続けられる。
            # 安全側("")に倒すとアングルは出したままになる
            log.warning("保護材選定に失敗しました: %s", exc)
            return ""

    @property
    def is_shared_board_mode(self) -> bool:
        """上用キャンバスを隠し、上下共用として画面に出すか。

        【選定アルゴリズムの分岐とは別物】
        `board_selection_algorithm.SelectUpperBoards` は「上下共用
        (ザラ板等)」と「プロテック」を**別の分岐**として持つ
        (プロテックは許容幅での判定が要るため)。ここはその選定ロジック
        には使わない(`selection_flags` が `is_protec_mode` を個別に渡す)、
        **画面表示だけの判定**。

        プロテックは上用・下用とも同じボードを使う点では上下共用と
        同じなので、画面上は上下共用の一種として扱い、上用キャンバスを
        隠す(製品幅を大きく超えるボードを、超過禁止の枠に配置しようと
        して静かに失敗する不具合があったため)。
        """
        if self.protec.is_protec:
            return True
        return bool(self.last_hosozai
                    and self.last_hosozai not in (HOSOZAI_NO_MATCH,
                                                  material_service.HOSOZAI_ANGLE))

    @property
    def show_angle(self) -> bool:
        """アングル関連の欄を出すか。

        【上用キャンバスの表示可否とは独立】以前はプロテックのとき
        無条件で `False`(非表示)にしていたが、これは誤りだった
        (VBA `ShowAngleControls` の修正と同じ経緯)。上用キャンバスを
        隠すこと(`is_shared_board_mode`)と、アングルを表示するかは
        **別の判断**。プロテックであっても、使用保護材が実際に
        アングルと判定されているなら、アングルの一覧・配置は
        出すべき(現場の声:「プロテックモードでは、使用保護材が実際に
        アングルと判定されていてもアングルリスト・配置が常に非表示に
        なってしまっていた」)。上用キャンバスの非表示は
        `is_shared_board_mode` が別途担う。
        """
        return (not self.last_hosozai
                or self.last_hosozai == HOSOZAI_NO_MATCH
                or self.last_hosozai == material_service.HOSOZAI_ANGLE)

    # ------------------------------------------------------------------
    # 4. EXオーダー
    # ------------------------------------------------------------------
    def set_ex_order(self, is_ex: bool) -> None:
        """VBA `SetExOrder` の移植(状態の保存のみ)。

        VBA はEX受注のとき `chkExOnly` を表示して**自動でONにし**、
        非EXでは非表示+OFFにしたうえで、いずれもパレット一覧を再フィルタする。
        ウィジェットの有効/無効と再フィルタは呼び出し側が行う。
        """
        self.is_ex_order = is_ex

    # ------------------------------------------------------------------
    # 5. ロット確定 — 状態機械の入口
    # ------------------------------------------------------------------
    def apply_lot(self, result: Any, *,
                  product_width: int = 0, product_length: int = 0,
                  available_board_types: Optional[list[str]] = None,
                  current_board_type: str = "") -> dict[str, Any]:
        """ロットが確定したときの一連の処理。

        VBA `SearchAndDisplay` / `Page1_OnLstHikiClick` の末尾で、
        資材選択画面に対して次の順で効いていた処理をまとめて再現する:

          1. `btnAutoSelectPallet_Click` が `valLot4`(表示中の板厚)を読み、
             5×10業界の強度UP判定に使う
          2. `SetExOrder` がEX受注フラグを立て、EXオンリー絞り込みを有効化
          3. `Apply1P0113Mode` が包装仕様NOで裸梱包モードを切り替え
          4. `CheckAndSetProtecMode` がプロテック判定とボード種別切替
          5. `GetUpperPartMaterial` → `ShowAngleControls` でアングルの要否

        **3〜5は互いに独立ではなく、この順で呼ばれる前提の状態機械**
        (プロテックだと上下共用も含めて上用キャンバスを隠す)なので
        順序を守る。

        【4と5の順序について】
        以前は 4(プロテック) と 5(アングル/上下共用) が逆順だった。
        VBA側で「`ReapplyAngleVisibility` が `m_lastHosozai` の早期
        リターンをプロテック判定より先に評価してしまい、プロテックの
        ときに上用キャンバスを隠す処理へ一度も到達しない」という
        不具合が実際に起きたため、VBA側はプロテック判定を早期リターン
        より前に動かす形で直した。Python版はここが同じ順序依存を
        引き継いでいたので、揃えて直す ── `apply_hosozai` が
        `self.protec.is_protec` を見られるよう、プロテック判定を先に
        済ませておく必要がある(`check_and_set_protec_mode` は
        `last_hosozai` に依存しないので、順序を入れ替えても安全)。
        """
        self.lot_no = result.lot.lot_no
        self.lot_result = result
        # BOXコースのときは板厚もBOX実績に差し替わった値を使う(VBA踏襲)
        self.manufactured_thickness = result.lot.thickness or None

        # 前のロットで強制1P0113にしていても新しいロットには持ち越さない
        # (VBA `ClearForNewLot` が m_force1P0113 を解除する)。
        # 疲労度優先・在庫考慮は手動トグルなので、こちらは持ち越す
        force_released = False
        force_change: Optional[Mode1P0113Result] = None
        if self.force_1p0113:
            force_change = self.toggle_force_1p0113(
                product_width=product_width, product_length=product_length)
            force_released = True

        self.set_ex_order(result.odr.is_ex)

        spec = result.odr.packaging_spec
        mode_change = self.apply_1p0113_mode(
            spec, product_width=product_width, product_length=product_length)
        protec = self.check_and_set_protec_mode(
            spec, available_board_types=available_board_types,
            current_board_type=current_board_type)
        self.check_and_set_1p1185_mode(
            spec, result.odr.customer_name, result.lot.width, result.lot.length)
        hosozai = self.apply_hosozai(self.lookup_hosozai(result))

        return {
            "force_released": force_released,
            "force_change": force_change,
            "mode_change": mode_change,
            "hosozai": hosozai,
            "protec": protec,
            "lot_caption": self.lot_caption(),
        }

    def lot_caption(self) -> str:
        """パレット欄の脇に出す現在のロット表示。"""
        if self.lot_result is None:
            return ""
        lot = self.lot_result.lot
        return (f"Lot {lot.lot_no} / 板厚 {lot.thickness:g}"
                + ("  [EX]" if self.lot_result.odr.is_ex else ""))

    # ------------------------------------------------------------------
    # ビューモデル
    # ------------------------------------------------------------------
    def mode_banner(self) -> ModeBanner:
        """モードバーに出す内容。優先順位は 1P0113 → プロテック → 上下共用。

        VBA も同じ優先順で、下位のモードは上位に隠れる。

        【プロテックと上下共用の関係】
        プロテックは画面表示上「上下共用の一種」(`is_shared_board_mode`
        が真になる)だが、プロテック特有の許容幅判定を伴う分、案内する
        情報が上下共用より具体的なので、**プロテックの表示を優先し、
        「上下共用」と重ねて言わない**(「プロテックボードオーダー選択中」
        は既に上用・下用が同じという意味を含む)。1P0113 は
        パレット・ボードを使わないモードなので、これも別枠で最優先。

        EXだけはこれらと独立に成立しうる(EX受注のまま上下共用/プロテックに
        なることがある。ただしEXとプロテックは実業務上同時に成立しない)。
        1P0113とプロテックは、EXと同時に成立しても業務上は問題にならない/
        ボード欄自体が隠れるため単独表示のままとし、**上下共用とEXが
        両方立っているときだけ**、見落とすと直接手戻りにつながるため
        1本のバーにまとめて出す(現場の声:「他のモードはテキストが
        出ていないものもあるのでは」への対応)。
        """
        if self.mode_1p0113:
            return ModeBanner(
                BANNER_1P0113,
                f"【{spk.HOSOSIYO_1P0113} 裸梱包モード】角材+松板で組みます")
        if self.protec.is_protec:
            return ModeBanner(BANNER_PROTEC, "【プロテックボードオーダー選択中】")
        if self.mode_1p1185.is_1p1185:
            return ModeBanner(BANNER_1P1185, "【1P1185モード タイトサイズ限定表示】")
        if self.is_shared_board_mode:
            if self.is_ex_order:
                return ModeBanner(
                    BANNER_SHARED_EX, "【上下共用・EXオーダー選択中】")
            return ModeBanner(BANNER_SHARED, "【上下共用ボードオーダー選択中】")
        if self.is_ex_order:
            return ModeBanner(BANNER_EX, "【EXオーダー選択中】")
        return ModeBanner()

    def selection_flags(self) -> dict[str, Any]:
        """自動選定へ渡すモード群(`auto_select_boards` の引数)。

        呼び出し側がこれを展開して渡せるようにしておくことで、
        「どのモードを選定に渡すか」の知識が画面側に散らばらない。
        """
        return {
            "is_protec_mode": self.protec.is_protec,
            "is_protec_1p1216": self.protec.is_1p1216,
            "last_hosozai": self.last_hosozai,
        }


# ==================================================================
# ビューモデル (Web版の画面が読むもの)
# ==================================================================
# 一覧の列。VBA のツリー列と同じ並び・同じ語。
# `numeric` は右寄せ+等幅にするかどうか(桁をそろえて読ませる)
PALLET_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("幅", "width", True),
    ("丈", "length", True),
    ("巾min", "w_min", True),
    ("巾max", "w_max", True),
    ("丈min", "l_min", True),
    ("丈max", "l_max", True),
    ("業界", "industry", False),
    ("記号", "symbol", False),
    ("脚数", "leg_count", True),
    ("桁数", "keta", True),
)

# 直接検索の許容差(mm)。文言に出すので、サービス側の定数から取る。
# ここに数字を書き写すと、片方だけ直されて説明と挙動がずれる
from ..config import SEARCH_RANGE_TOLERANCE as SEARCH_TOLERANCE  # noqa: E402

# 決まっていないときの表示。空欄にすると、決まっていないのか
# 表示が壊れているのか区別できない(VBA も赤字で「未設定」を出していた)
PALLET_UNSET = "パレット: 未設定"
PRODUCT_UNSET = "製品サイズ: 未設定"

# 操作カラムの段の状態。**判断はサーバが持つ**(設計指針 §1.3)
STEP_TODO = "todo"        # まだ手を付けていない
STEP_CURRENT = "current"  # いま押すべき段
STEP_DONE = "done"        # 決まった。畳んでも確定値は残す

# この画面でまだできないこと。作りかけを黙って出すと、
# 「壊れている」のか「これから作る」のか区別できない
PENDING_PARTS: tuple[tuple[str, str], ...] = ()

# ボード候補の列。tkinter版 `board_tree` と同じ並び・同じ語
BOARD_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("幅", "width", True),
    ("丈", "length", True),
    ("在庫", "stock", False),
)

# 選定済みの列。VBA の `lstSelectedBoards*` は
# 「750 × 1130  ×3  [主]」という1行の文字列だったが、桁がそろわないので
# 枚数と丈を読み違える。**語はそのまま**に、列へ分ける
SELECTED_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("種別", "tag", False),
    ("幅", "width", True),
    ("丈", "length", True),
    ("枚", "count", True),
)

# 在庫が薄い印。色だけで示さない(§設計指針 3.8)ので記号を添える
STOCK_LOW_MARK = "▲薄"

# 配置図の見出し。VBA のラベルそのまま。選定リストの見出し
# (`LIST_TITLE_*`)とは別で、こちらは**図**の見出し
PLAN_TITLE_UPPER = "【上用ボード配置】"
PLAN_TITLE_ANGLE = "【アングル配置】"

# まだ配置していないときの表示。空欄にすると、配置していないのか
# 図が壊れているのか区別できない(tkinter版 `layout_status` と同じ)
STATUS_UNPLACED = "未配置"

# 選定リストの見出し。VBA `fraUpperBoards` / `fraLowerBoards` の caption。
# 上の `TITLE_LOWER` は**配置図**の見出しで、別のもの
LIST_TITLE_UPPER = "【上用ボード】※製品幅以下"
LIST_TITLE_LOWER = "【下用ボード】"
# 上下共用モードでは、この一覧のボードが上にも下にも載る。
# tkinter版は上用の欄を隠すだけで一覧の見出しは「【下用ボード】」のままだった
# ── 上用が消えた理由がここから読めないので、配置図と同じ語に合わせる
LIST_TITLE_SHARED = "【上下共用ボード】"


@dataclass
class PalletRow:
    """一覧の1行。`is_ex` は表示側が色を選ぶための種別。"""

    values: list[str] = field(default_factory=list)
    width: int = 0
    length: int = 0
    symbol: str = ""
    is_ex: bool = False
    # クリックしたときだけ出す情報(VBA `DynamicTip`)。列には無い
    note: str = ""
    # **サーバが覚えている行かどうか。** 発注コードと単位の出どころは
    # `session.pallet_row` ただ1つで、寸法が食い違えば server 側が外す。
    # ここに出さないと画面が「どれを選んだか」を独自に覚えることになり、
    # 外されたことも、自動選定で選ばれたことも画面に伝わらない
    picked: bool = False


@dataclass
class BoardCandidate:
    """候補ボードの1行。"""

    values: list[str] = field(default_factory=list)
    width: int = 0
    length: int = 0
    stock_low: bool = False


@dataclass
class SelectedRow:
    """選定済みの1行。`index` は削除に使う位置(並びがそのまま実体)。"""

    index: int = 0
    values: list[str] = field(default_factory=list)
    width: int = 0
    length: int = 0
    count: int = 0
    tag: str = ""
    # 図の該当ボードと結びつける鍵(`board_key`)
    key: str = ""
    # 「主」と「補填」で印の見た目を変える。補填は主より小さく、
    # 数が多い。同じ強さで並べると主がどれか分からなくなる
    tag_kind: str = ""


@dataclass
class BoardsViewModel:
    """ボード選定の部分(VBA の ①選定リスト ②候補+実行ボタン列)。"""

    board_types: list[str] = field(default_factory=list)
    board_type: str = ""
    candidates: list[BoardCandidate] = field(default_factory=list)
    candidate_note: str = ""

    upper: list[SelectedRow] = field(default_factory=list)
    lower: list[SelectedRow] = field(default_factory=list)
    upper_title: str = LIST_TITLE_UPPER
    lower_title: str = LIST_TITLE_LOWER
    # 上下共用モードでは上用の欄ごと隠す
    # (VBA `ShowAngleControls` の ctrlNames に lstSelectedBoardsUpper が入る)
    show_upper: bool = True
    summary: str = ""

    # 押しっぱなしのモード
    fatigue: bool = False
    stock_aware: bool = False
    # 疲労度は拠点からの距離で決まる。どの拠点で計算したのかを出さないと
    # 「なぜこの並びなのか」が分からない
    base_point: str = ""

    can_select: bool = False
    select_why: str = ""
    can_place: bool = False
    place_why: str = ""

    # 「候補変更」(敷き詰め方式)。押すたびに A→B→C と回る。
    # いまどれが出ているのかを出さないと、押した結果が変わったのか
    # 変わらなかったのかが画面から読めない
    can_change: bool = False
    change_why: str = ""
    change_axis: str = ""


@dataclass
class MaterialRow:
    """1P0113 の資材1件(角材 / 松板)。

    `info` は押したときに出すフル情報(VBA `Show1P0113LabelTip`)。
    ホバーではなく**押して出す** ── タッチ端末にホバーは無い(§0)。
    """

    key: str = ""             # "kak" / "mat"
    name: str = ""
    label: str = ""           # ヒット時は寸法、外れたら理由
    info: str = ""
    count_caption: str = ""
    ok: bool = False


@dataclass
class Mode1P0113ViewModel:
    """裸梱包モード(VBA `Create1P0113UI`)。

    パレットを使わないので、パレット欄と**入れ替える**(VBA も同じ位置に
    出していた)。並びは 松板 → 角材 → コード/単位 → 本数 → 枚数 で、
    寸法の確認を上に、発注する数量を下にまとめる。
    """

    on: bool = False
    forced: bool = False
    materials: list[MaterialRow] = field(default_factory=list)
    tip: str = ""
    qty: int = 1
    qty_min: int = 1
    qty_max: int = 10
    # 資材が引けているか。引けていないと倉庫送信も帳票も通せない
    ready: bool = False
    why: str = ""


@dataclass
class PatternRow:
    """実績パターンの1件(VBA `PatternSelectDialog` の1行)。"""

    id: int = 0
    product: str = ""
    registered_at: str = ""
    usage_count: int = 0
    boards: str = ""
    # 配置方式(通常 / 別案A〜C)。VBA の一覧に足された列
    method: str = ""
    # まだ取り込み元へ届いていない(この端末でしか見えない)
    unsent: bool = False


@dataclass
class BoardUsageRow:
    """選定ボードの使用実績1件(`board_usage.py`)。

    「使用する」を押したときだけ積む値なので、置いてみただけの試しは
    ここに出ない(VBAには無い機能。現場の要望で追加)。数える入口は
    `selection_session.record_usage` 1つだけ。
    """

    width: int = 0
    length: int = 0
    board_type: str = ""
    usage_count: int = 0
    last_used_at: str = ""


@dataclass
class AdminViewModel:
    """管理者エリア(VBA: 右下に隔離。認証しないと保存できない)。

    **パスワードは持たない。** 照合はサーバでしか行わず、画面へ渡すのは
    「認証したか」の1ビットだけ(設計書 §3.6)。
    """

    authenticated: bool = False
    can_save: bool = False
    save_why: str = ""
    # 保存の前に訊く文(VBA の確認 MsgBox)。**文はサーバが持つ**
    save_confirm: dict[str, str] = field(default_factory=dict)
    patterns: list[PatternRow] = field(default_factory=list)
    patterns_note: str = ""
    usage: list[BoardUsageRow] = field(default_factory=list)


@dataclass
class ReportLink:
    """帳票の出し口(VBA `btnPrint_Click` / `btnCutRequest_Click`)。"""

    key: str = ""
    label: str = ""
    url: str = ""
    can: bool = False
    why: str = ""
    # 押す前に確かめること(切断依頼の「丈カットを行いますか」)。
    # **訊くかどうかも、文面も、選択肢もサーバが決める** ── 画面が
    # 組み立て直すと、条件を直した日に文面だけが古いまま残る。
    # 空なら訊かない。中身は `{title, body, choices:[{key,label,note}]}`
    ask: dict = field(default_factory=dict)


@dataclass
class OutputsViewModel:
    """出す操作 ── 帳票と倉庫送信(VBA の右列)。"""

    reports: list[ReportLink] = field(default_factory=list)
    can_send: bool = False
    send_why: str = ""
    # 「使用する」── 配置したボードを使用実績に積む(VBAには無い機能)
    can_use: bool = False
    use_why: str = ""
    use_done: bool = False       # いまの配置をもう積んである


@dataclass
class InfoLine:
    """配置図の下に出す実測(VBA `DisplayInfo`)。

    `kind` は表示側が色を選ぶための種別。**色そのものは渡さない** ──
    tkinter版は `cut_status_color()` が返す RGB をそのまま使っていたが、
    Web版は3つのテーマがあるので色は `tokens.css` が決める。
    """

    label: str = ""
    text: str = ""
    kind: str = "ok"          # "ok" | "cut" | "deficit"


@dataclass
class PlansViewModel:
    """配置図(VBA `RefreshPaletteDisplay`)。

    図の中身は `placement_render` / `angle_render` が作った**計画そのもの**。
    ここは「どの図を出すか」と「下に何と書くか」だけを決める。
    """

    placed: bool = False
    status: str = STATUS_UNPLACED
    # 論理キャンバス。拡大縮小はブラウザ(`viewBox`)に任せる
    view_box: str = ""
    angle_view_box: str = ""

    upper_title: str = PLAN_TITLE_UPPER
    lower_title: str = TITLE_LOWER
    angle_title: str = PLAN_TITLE_ANGLE
    show_upper: bool = True

    upper: Optional[dict[str, Any]] = None
    lower: Optional[dict[str, Any]] = None
    angle: Optional[dict[str, Any]] = None

    usage: str = ""
    lines: list[InfoLine] = field(default_factory=list)


@dataclass
class AnglesViewModel:
    """アングルの部分(VBA `lstAngles` / `lstSelectedAngles` と4つのボタン)。"""

    show: bool = True
    # アングルを隠したときだけ「使用保護材: ○○」を出す(消えた理由を示すため)
    hosozai_label: str = ""
    candidates: list[int] = field(default_factory=list)
    selected: list[int] = field(default_factory=list)
    need_cut: bool = False
    can_auto: bool = False
    auto_why: str = ""
    can_draw: bool = False
    draw_why: str = ""
    # 押せない理由。**同じ文が2つ出ないようにまとめてある** ──
    # 「先に製品サイズを」は自動選定と配置の両方を止めるので、
    # ボタンごとに出すと同じ行が並ぶ
    why: list[str] = field(default_factory=list)


@dataclass
class Step:
    """操作カラムの1段。

    **いま何段目か・何が決まったかの判断はサーバが持つ。** 画面は
    番号と要約を並べ、`state` に応じた見た目を当てるだけ。
    JS側で「パレットが決まったか」を組み立て直すと、条件が2か所に
    分かれて必ずどちらかが古くなる。
    """

    key: str
    number: int
    title: str
    # 畳んだときに残す1行。**確定値を必ず残す**ので、閉じていても
    # 「何が決まっているか」は失われない(設計指針 §1.3)
    summary: str = ""
    state: str = STEP_TODO
    # そのときは使わない段(1P0113 のパレット、共用モードのアングル)。
    # 隠すのではなく、そもそも段として数えない
    hidden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "number": self.number, "title": self.title,
                "summary": self.summary, "state": self.state,
                "hidden": self.hidden}


@dataclass
class SelectionViewModel:
    """資材選択画面(パレット・製品サイズの部分)。"""

    available: bool = True
    unavailable_message: str = ""

    lot_no: str = ""
    lot_caption: str = ""
    banner: ModeBanner = field(default_factory=ModeBanner)

    # 一覧
    rows: list[PalletRow] = field(default_factory=list)
    list_mode: str = ""
    list_note: str = ""

    # 入力欄の値(サーバが決める。画面は言われたものを入れる)
    pallet_width: str = ""
    pallet_length: str = ""
    product_width: str = ""
    product_length: str = ""

    # 確定した内容
    pallet_status: str = PALLET_UNSET
    pallet_set: bool = False
    product_status: str = PRODUCT_UNSET
    product_set: bool = False

    # 押しっぱなしのモード
    show_all: bool = False
    ex_only: bool = False
    ex_only_enabled: bool = False
    ex_only_why: str = ""
    two_stack: bool = False

    # 1P0113ではパレットを使わない。一覧ごと差し替える(VBA と同じ位置)
    mode_1p0113: bool = False

    can_set_product: bool = False
    set_product_why: str = ""
    # 「パレットを選ぶ」── これ1回でパレットと製品の両方が決まる
    can_decide: bool = False
    decide_why: str = ""
    # この製品サイズの出どころ(ロットから来たのか、手で入れたのか)
    product_from: str = ""
    # 決まったものを1行にまとめた文。**製品とパレットは1つの決めごと**
    size_status: str = ""

    boards: BoardsViewModel = field(default_factory=BoardsViewModel)
    angles: AnglesViewModel = field(default_factory=AnglesViewModel)
    plans: PlansViewModel = field(default_factory=PlansViewModel)
    p1: Mode1P0113ViewModel = field(default_factory=Mode1P0113ViewModel)
    admin: AdminViewModel = field(default_factory=AdminViewModel)
    outputs: OutputsViewModel = field(default_factory=OutputsViewModel)

    # 作業の段。順序・番号・要約・いまどこかを**サーバが決める**
    steps: list[Step] = field(default_factory=list)
    # いま強調するボタン。押すべきものが1つに見えるようにする
    # (16個のボタンが同じ重さで並ぶと、どれが次かが読めない)
    primary_action: str = ""
    # 次にすること。1行で言う。押せないなら押せない理由になる
    next_hint: str = ""


def has_data(conn: sqlite3.Connection) -> bool:
    """パレットのマスタが取り込まれているか。

    未取り込みで空の一覧を出すと「該当なし」と読める。取り込みが
    済んでいないことを先に言う(ロット検索の `has_data` と同じ扱い)。
    """
    from .. import db
    rows = db.fetch_all(conn, "SELECT 1 FROM PalletMaster LIMIT 1",
                        caller_name="selection.has_data")
    return bool(rows)


def build_view(session: Any, *, available: bool = True) -> SelectionViewModel:
    """セッションの状態から画面ぜんぶを組み立てる。

    `session` は `selection_session.SelectionSession`。型で書かないのは、
    `selection_session` がこの層を import しているため(循環を避ける)。
    """
    from .. import work_context

    presenter = session.presenter
    context = work_context.get_context()

    view = SelectionViewModel(
        available=available,
        unavailable_message=(
            "" if available else
            "梱包資材マスタが未取り込みです。設定画面から取り込んでください。"),
        lot_no=presenter.lot_no,
        lot_caption=presenter.lot_caption(),
        banner=presenter.mode_banner(),
        list_mode=session.list_mode,
        show_all=session.show_all,
        ex_only=session.ex_only,
        two_stack=session.two_stack,
        mode_1p0113=presenter.mode_1p0113,
    )

    # EXオンリーはEX受注のときだけ効く。効かない状態で押せると、
    # 押したのに何も変わらない ── 押せない理由を添えて止める
    view.ex_only_enabled = presenter.is_ex_order
    view.ex_only_why = ("" if presenter.is_ex_order
                        else "EX受注のロットを引くと使えます")

    # 入力欄。**一覧で選んだ行が最優先**で、無ければ確定値を出す。
    # 行を選ぶのは「この寸法にしたい」という意思表示なので、選んだ直後に
    # 欄が確定値へ戻ると、押した意味が消える(実機で踏んだ)
    if session.pallet_row is not None:
        view.pallet_width = str(session.pallet_row.width)
        view.pallet_length = str(session.pallet_row.length)
    elif session.palette.is_set:
        view.pallet_width = str(session.palette.width)
        view.pallet_length = str(session.palette.length)
    if session.palette.is_set:
        view.pallet_status = (f"パレット: {session.palette.width} × "
                              f"{session.palette.length} (設定済)")
        view.pallet_set = True
    if context.product_width and context.product_length:
        view.product_width = str(context.product_width)
        view.product_length = str(context.product_length)
    if session.product.is_set:
        view.product_status = (f"製品: {session.product.width} × "
                               f"{session.product.length} (設定済)")
        view.product_set = True

    # この寸法がどこから来たのか。**人が打った数字ではない**ことが
    # 分かると、確かめるだけでよいと判断できる(§2.5 なぜを追える)
    if context.lot_no and context.product_width and context.product_length:
        view.product_from = (f"Lot {context.lot_no} の製造板幅×板丈です"
                             "(ロット検索の「資材展開」で入りました)")

    # 「パレット決定」は**一覧で行を選んでいるときだけ**押せる
    # (行を選ぶのは画面側だけの状態なので、ここは前回までに確定した
    # 行があるかどうかの初期表示。押せるかの最終判断はJS側が
    # `selectedRow` を見て決め直す)。
    view.can_decide = session.pallet_row is not None
    view.decide_why = ("" if view.can_decide
                       else "一覧からパレットを選んでください")
    # 製品サイズだけを手で入れ直す道は残す。パレットが決まっていないと
    # 「載るか」を確かめられないので、そのときは押させない
    view.can_set_product = session.palette.is_set
    view.set_product_why = ("" if session.palette.is_set
                            else "先にパレットを決めてください"
                                 "(「パレットを選ぶ」で一度に決まります)")

    rows = list_rows(session)
    view.rows = [_pallet_row(row, session.pallet_row) for row in rows]
    view.list_note = _list_note(session, len(rows))

    view.boards = build_boards(session)
    view.angles = build_angles(session)
    view.plans = build_plans(session)
    view.p1 = build_1p0113(session)
    view.admin = build_admin(session)
    view.outputs = build_outputs(session)
    _apply_steps(view)
    return view


# ------------------------------------------------------------------
# 作業の段
# ------------------------------------------------------------------
def _apply_steps(view: SelectionViewModel) -> None:
    """いま何段目かを決め、段ごとの要約を付ける。

    **どのボタンが「次の一手」かをサーバが1つ選ぶ。** 16個のボタンが
    同じ重さで並ぶと、押すべきものを毎回探すことになる(ヒックの法則)。
    決まった段は確定値を1行にして畳めるようにする ── 畳んでも
    「何が決まっているか」は失われない(設計指針 §1.3)。
    """
    pallet_done = view.mode_1p0113 or view.pallet_set
    boards_done = bool(view.boards.upper or view.boards.lower)
    placed = bool(view.plans.placed)

    # **製品サイズとパレットは1つの決めごと。**
    #
    # 決まっていく順は「ロットを選ぶ → 製品サイズが入る → それが載る
    # パレットが決まる」で、パレットは製品のために選ぶもの。以前は
    # パレット → 製品 の2段に分けていたので、まだ分からないものを先に
    # 決めさせ、あとから「載るかどうか」を確かめる形になっていた。
    # しかも段を分けると、いまの段だけが開く仕組み(§1.3)のせいで
    # **候補の一覧が必要なときに畳まれている**という噛み合わなさが出る。
    size_done = view.product_set and pallet_done
    steps = [
        Step(key="size",
             number=1,
             title="裸梱包(1P0113)" if view.mode_1p0113 else "パレットを決める",
             summary=(view.p1.tip if view.mode_1p0113
                      else _size_summary(view)),
             state=STEP_DONE if (view.mode_1p0113 or size_done) else STEP_TODO),
        Step(key="boards", number=3, title="ボード",
             summary=(view.boards.summary if boards_done
                      else view.boards.candidate_note),
             state=STEP_DONE if placed else STEP_TODO,
             # 1P0113 はパレットもボードも使わない
             hidden=view.mode_1p0113),
        Step(key="angles", number=4, title="アングル",
             summary=view.angles.hosozai_label,
             state=STEP_DONE if view.plans.angle else STEP_TODO,
             hidden=not view.angles.show),
        Step(key="outputs", number=5, title="出す",
             summary="帳票と倉庫送信",
             state=STEP_TODO),
    ]

    # いま押すべき段 = 終わっていない最初の段。全部終わっていれば「出す」
    visible = [s for s in steps if not s.hidden]
    current = next((s for s in visible if s.state != STEP_DONE), None)
    if current is not None:
        current.state = STEP_CURRENT
    # 番号は**見える段だけ**で振り直す。1P0113 で3が飛ぶと、
    # 抜けた段を探すことになる
    for index, step in enumerate(visible, start=1):
        step.number = index

    view.steps = steps
    view.size_status = _size_summary(view)
    view.primary_action, view.next_hint = _next_action(view, current)


def _size_summary(view: SelectionViewModel) -> str:
    """畳んだときに残す1行。**決まった2つを両方載せる。**

    片方だけだと「製品は決まったがパレットは?」を開いて確かめることに
    なり、畳んだ意味が無くなる(設計指針 §1.3)。
    """
    parts = []
    if view.product_set:
        parts.append(f"製品 {view.product_status.split(':', 1)[-1].strip()}")
    if view.pallet_set:
        parts.append(f"パレット {view.pallet_status.split(':', 1)[-1].strip()}")
    return " / ".join(parts) if parts else view.product_status


def _next_action(view: SelectionViewModel,
                 current: Optional[Step]) -> tuple[str, str]:
    """次の一手と、その一言。押せないなら押せない理由を返す。"""
    if current is None:
        return "", ""
    if current.key == "size":
        if view.mode_1p0113:
            return "p1Qty", "数量を確かめてください。角材の本数と松板の枚数に掛かります。"
        if view.can_decide:
            # 一覧で選んだ行を「パレット決定」で確定させるところまで
            return "decide", ("一覧で選んだパレットで確定するので、"
                              "「パレット決定」を押します。"
                              "別のものにしたいときは一覧の別の行を押します。")
        if view.product_width and view.product_length:
            return "palletRows", "一覧からパレットを選んでください。"
        return "prodWidth", "製品の幅・丈を入れてください(ロットを選ぶと入ります)。"
    if current.key == "boards":
        if view.boards.can_place:
            return "place", "選んだボードを「ボード配置」で図にします。"
        if view.boards.can_select:
            return "autoSelect", "「ボード選定」でボードを自動で選びます。"
        return "", view.boards.select_why
    if current.key == "angles":
        if view.angles.can_draw:
            return "drawAngle", "「アングル配置」で図にします。"
        if view.angles.can_auto:
            return "autoAngle", "「アングル自動」で選びます。"
        return "", ""
    if view.outputs.can_send:
        return "sendWarehouse", "帳票を出すか、倉庫へ送ります。"
    return "", ""


# ------------------------------------------------------------------
# 管理者と実績パターン
# ------------------------------------------------------------------
def build_admin(session: Any) -> AdminViewModel:
    """管理者エリア。**パスワードは1文字も渡さない**(設計書 §3.6)。"""
    view = AdminViewModel(authenticated=session.admin)

    # 保存は「認証してある」「配置してある」「配置のあとで変えていない」とき
    # (VBA `btnSavePattern_Click`。判断は `session.save_refusal` の1か所)
    view.save_why = session.save_refusal()
    view.can_save = not view.save_why
    view.save_confirm = session.save_confirm()

    from .. import board_usage
    view.usage = [
        BoardUsageRow(width=u.width, length=u.length, board_type=u.board_type,
                     usage_count=u.usage_count, last_used_at=u.last_used_at)
        for u in board_usage.list_usage(session.presenter.conn)]

    if not session.palette.is_set:
        view.patterns_note = "パレットサイズを適用すると実績を探せます"
        return view

    patterns = session.patterns()
    view.patterns = [
        PatternRow(id=p.id,
                   product=(f"{p.product_width}×{p.product_length}"
                            if p.product_width and p.product_length else "—"),
                   registered_at=p.registered_at, usage_count=p.usage_count,
                   boards=p.board_summary, method=p.method, unsent=p.unsent)
        for p in patterns]
    view.patterns_note = (
        f"{session.palette.width}×{session.palette.length} の実績 "
        f"{len(patterns)} 件" if patterns
        else f"{session.palette.width}×{session.palette.length} の実績はありません")
    return view


# ------------------------------------------------------------------
# 出す ── 帳票と倉庫送信
# ------------------------------------------------------------------
def build_outputs(session: Any) -> OutputsViewModel:
    """帳票と倉庫送信。押せるかどうかと理由はサーバが決める。"""
    from . import outputs as out

    view = OutputsViewModel()
    for key, label, refusal in (
        (out.REPORT_LABEL, "Lot印刷", out.label_refusal(session)),
        (out.REPORT_CUT, "切断依頼", out.cut_request_refusal(session)),
        (out.REPORT_PLAN, "配置図印刷", out.plan_refusal(session)),
    ):
        view.reports.append(ReportLink(
            key=key, label=label, url=f"/report/{key}",
            can=refusal is None, why="" if refusal is None else refusal.message,
            ask=out.len_cut_question(session)
            if key == out.REPORT_CUT and refusal is None else {}))

    send = out.send_refusal(session)
    view.can_send = send is None
    view.send_why = "" if send is None else send.message

    # 「使用する」。**押したあとも押せるままにはしない** ── 同じ配置を
    # 二度積むと実績が実態より多くなる。押せない理由は必ず出す
    why = session.usage_refusal()
    view.use_done = session.usage_done
    view.can_use = not why and not view.use_done
    view.use_why = why or ("この配置はもう記録してあります。"
                           if view.use_done else "")
    return view


# ------------------------------------------------------------------
# 1P0113 裸梱包
# ------------------------------------------------------------------
def build_1p0113(session: Any) -> Mode1P0113ViewModel:
    """裸梱包モードのパネル(VBA `Create1P0113UI`)。"""
    from .. import special_packaging as spk

    presenter = session.presenter
    view = Mode1P0113ViewModel(
        on=presenter.mode_1p0113,
        forced=presenter.force_1p0113,
        qty=session.qty_1p0113,
        qty_min=ss_qty_min(), qty_max=ss_qty_max(),
    )
    if not view.on:
        return view

    m = presenter.materials_1p0113
    qty = session.qty_1p0113
    view.materials = [
        MaterialRow(key="mat", name=spk.MATSUITA_NAME, label=m.matsuita_label,
                    info=m.matsuita.full_info, ok=m.matsuita.is_ok,
                    count_caption=spk.matsuita_count_caption(m.matsuita_count(qty))),
        MaterialRow(key="kak", name=spk.KAKUZAI_NAME, label=m.kakuzai_label,
                    info=m.kakuzai.full_info, ok=m.kakuzai.is_ok,
                    count_caption=spk.kakuzai_count_caption(m.kakuzai_count(qty))),
    ]
    # 通常はコード/単位を出す(VBA `DynamicTip_PalUnit`)。押すと
    # その資材のフル情報に差し替わるのは画面側の仕事
    view.tip = (f"コード: {spk.parse_info(m.kakuzai.full_info, 'CD') or '---'}"
                f"   単位: {spk.parse_info(m.kakuzai.full_info, '単位') or '---'}")

    view.ready = m.kakuzai.is_ok and m.matsuita.is_ok
    if not view.ready:
        missing = [hit.name for hit in (m.kakuzai, m.matsuita) if not hit.is_ok]
        view.why = (f"{'・'.join(missing)} をマスタから引けていません。"
                    f"製品サイズを入れてください。")
    return view


def ss_qty_min() -> int:
    from .. import selection_session as ss
    return ss.QTY_MIN


def ss_qty_max() -> int:
    from .. import selection_session as ss
    return ss.QTY_MAX


# ------------------------------------------------------------------
# ボード
# ------------------------------------------------------------------
def build_boards(session: Any) -> BoardsViewModel:
    """ボード選定の部分。**押せるかどうかもここで決める**。

    画面が条件を組み立て直すと、サーバが断る条件と画面が押させる条件が
    別々に育つ。倉庫連携で `can_confirm` をサーバから渡しているのと
    同じ理由(§設計書 Phase 5)。
    """
    presenter = session.presenter
    shared = presenter.is_shared_board_mode

    view = BoardsViewModel(
        board_types=session.board_types(),
        board_type=session.board_type,
        # 上下共用モードでは上用=下用と同サイズになるので、見出しを変える
        # (VBA `ShowAngleControls`)。上用の欄を隠すだけだと、
        # 上用を選び忘れたのか同じでよいのかが読めない
        lower_title=LIST_TITLE_SHARED if shared else LIST_TITLE_LOWER,
        show_upper=not shared,
        fatigue=session.fatigue,
        stock_aware=session.stock_aware,
        base_point=_base_point_label(),
    )

    candidates = session.candidates()
    view.candidates = [_board_candidate(row) for row in candidates]
    view.candidate_note = _candidate_note(session, len(candidates))

    from .. import placement_render as render
    view.upper = [_selected_row(i, b, render.CATEGORY_UPPER)
                  for i, b in enumerate(session.selected.upper)]
    view.lower = [_selected_row(i, b, render.CATEGORY_LOWER)
                  for i, b in enumerate(session.selected.lower)]
    view.summary = _selected_summary(session)

    # 「選定」はパレットと製品サイズの両方が要る(tkinter版 `_require_sizes`)。
    # 押してから断るより、押せない理由が先に見えているほうがよい
    missing = _missing_sizes(session)
    if missing:
        view.select_why = missing
    elif not candidates:
        view.select_why = "候補ボードがありません。ボード種別を確認してください。"
    view.can_select = not view.select_why

    # 「配置」は選定済みが1つ以上あって初めて意味を持つ
    if missing:
        view.place_why = missing
    elif not (session.selected.upper or session.selected.lower):
        view.place_why = "先にボードを選定してください。"
    view.can_place = not view.place_why

    # 「候補変更」は敷き詰め方式の補助。要るのは寸法と候補だけで、
    # 先に「ボード選定」を押しておく必要はない ── 現行の選定を
    # 通さずに別の解を出すのがこのボタンの役目
    if missing:
        view.change_why = missing
    elif not candidates:
        view.change_why = "候補ボードがありません。ボード種別を確認してください。"
    elif presenter.protec.is_protec:
        # プロテックのルール(製品幅基準・マイナス許容)を別案は持たない。
        # 押させておいて断るより、先に理由を出す
        from ..selection_tiling import PROTEC_TILING_REFUSAL
        view.change_why = PROTEC_TILING_REFUSAL
    view.can_change = not view.change_why
    state = getattr(session, "tiling", None)
    if state is not None and state.axis >= 0:
        from .. import tiling_algorithm as tiling
        view.change_axis = tiling.AXIS_NAMES[state.axis]
    return view


def _missing_sizes(session: Any) -> str:
    """足りないサイズを名指しで返す。両方そろっていれば空。"""
    if not session.palette.is_set:
        return "先にパレットサイズを適用してください。"
    if not session.product.is_set:
        return "先に製品サイズをセットしてください。"
    return ""


def _base_point_label() -> str:
    from .. import user_settings
    return user_settings.get_position_label()


def _board_candidate(row: Any) -> BoardCandidate:
    return BoardCandidate(
        values=[str(row.width), str(row.length),
                STOCK_LOW_MARK if row.stock_low else ""],
        width=row.width, length=row.length, stock_low=row.stock_low)


def _candidate_note(session: Any, count: int) -> str:
    """候補一覧が何を出しているのかを一言で。"""
    if not session.board_type:
        return "ボードマスタが未取り込みです"
    note = f"{session.board_type} {count} 件"
    return note + "(在庫考慮)" if session.stock_aware else note


def board_key(category: str, width: int, length: int) -> str:
    """図と一覧を結びつける鍵(§設計指針 共通運命の要因)。

    **上用/下用を分けたうえで、向きを無視した寸法の組**にする。

    向きを無視するのは、配置が1枚ごとに向きを選ぶため
    (`get_best_orientation`)。同じボードでも図の中では幅と丈が
    入れ替わっていることがある。並び順や添字を鍵にしないのは、
    補填ボードの並べ替え(`sort_fill_boards`)で対応が崩れるため。

    上用/下用を分けるのは、同じ寸法のボードが両方に出ることがあるから。
    分けないと**下用の行を押したのに上用の図が光る**。

    同じ寸法を2行に分けて足していると両方光る。それは同じボードなので、
    まとめて光るほうが実態に合っている。
    """
    return f"{category}:{min(width, length)}x{max(width, length)}"


def _selected_row(index: int, board: Any, category: str) -> SelectedRow:
    """選定済みの1行。タグの語は VBA のまま('主' / '幅補填' / '丈補填' …)。"""
    tag = board.tag or ""
    return SelectedRow(
        index=index,
        values=[tag, str(board.width), str(board.length), str(board.count)],
        width=board.width, length=board.length, count=board.count, tag=tag,
        key=board_key(category, board.width, board.length),
        # 「主」だけを強く出す。補填は主の隙間を埋めるもので、
        # 同じ強さで並べるとどれが本体か分からなくなる
        tag_kind=("main" if tag == "主" else "fill" if tag else ""))


def _selected_summary(session: Any) -> str:
    """選定の結果を1行で。種類と枚数は別の数で、取り違えると発注がずれる。

    【VBAからの追加】配置図はボードを置くまで何も描かれないため、
    選定した直後は寸法がどこにも見えず「何を選んだのか分からない」
    という声があった。この見出しは畳んだ段でも読めるので、種類・枚数
    だけでなく具体的な寸法もここに出す。
    """
    def describe(boards: list) -> str:
        if not boards:
            return "なし"
        count_text = f"{len(boards)}種 {sum(b.count for b in boards)}枚"
        sizes = ", ".join(f"{b.width}×{b.length}×{b.count}" for b in boards)
        return f"{count_text}({sizes})"

    if not (session.selected.upper or session.selected.lower):
        return "まだ選定していません"
    return (f"上用 {describe(session.selected.upper)} / "
            f"下用 {describe(session.selected.lower)}")


# ------------------------------------------------------------------
# 配置図
# ------------------------------------------------------------------
def build_plans(session: Any) -> PlansViewModel:
    """配置図(VBA `RefreshPaletteDisplay` / `DisplayInfo`)。

    図そのものは `placement_render` / `angle_render` が作る計画で、
    tkinter版が Canvas に描くのとまったく同じもの。ここは
    **どの図を出すか**と**下に何と書くか**だけを決める。
    """
    from .. import placement_render as render
    from . import render_json

    presenter = session.presenter
    shared = presenter.is_shared_board_mode

    view = PlansViewModel(
        view_box=render_json.view_box(),
        angle_view_box=render_json.view_box(render_json.LOGICAL_ANGLE_CANVAS),
        # 図の見出しは上下共用で変わる(VBA `ShowAngleControls`)。
        # 一覧の見出しと同じ語をここでも使う
        lower_title=TITLE_LOWER_SHARED if shared else TITLE_LOWER,
        show_upper=not shared,
    )
    view.angle = _angle_plan(session)

    placement = session.placement
    if placement is None:
        return view

    # 自動選定の結果、または実績から戻した結果(`session.narrow_flags`)
    narrow_lower, narrow_upper = session.narrow_flags()

    placed = placement.placed
    view.placed = True
    view.lower = _board_plan(
        placed, render.CATEGORY_LOWER,
        session.palette.width, session.palette.length, narrow=narrow_lower)
    if view.show_upper:
        view.upper = _board_plan(
            placed, render.CATEGORY_UPPER,
            session.product.width, session.product.length, narrow=narrow_upper)

    n_lower = sum(1 for p in placed if p.board_category == render.CATEGORY_LOWER)
    n_upper = sum(1 for p in placed if p.board_category == render.CATEGORY_UPPER)
    view.status = (f"配置完了: 上用{n_upper}枚 / 下用{n_lower}枚 "
                   f"(計{len(placed)}枚)")
    view.usage, view.lines = _info_lines(session, placed, shared)
    return view


def _board_plan(placed: list, category: str, base_w: int, base_l: int, *,
                narrow: bool) -> dict[str, Any]:
    """1カテゴリ分の図。計画に**鍵と読み上げ文**を添えて返す。

    `plan.boards` は `placed` を category で絞った順そのままなので、
    同じ順で並べれば1対1に対応する(`placement_render.build_render_plan`)。
    鍵と読み上げ文をここで足すのは、`render_json` を通る計画そのものを
    ゴールデン(41通り)が1バイト単位で見張っているため ──
    図の見せ方の都合でそちらを動かさない。
    """
    from . import render_json

    plan = render_json.with_tokens(
        render_json.build_render_plan_dict(
            placed, category, base_w, base_l, narrow=narrow), category)

    source = [b for b in placed if b.board_category == category]
    for rect, board in zip(plan["boards"], source):
        rect["key"] = board_key(
            category, board.original_width, board.original_length)
        # 図の中の文字は小さく、細いボードには入らない。
        # 寸法は必ず読めるようにしておく(SVG の <title>)
        rect["title"] = f"幅{board.width} × 丈{board.length}"
    return plan


def _info_lines(session: Any, placed: list,
                shared: bool) -> tuple[str, list[InfoLine]]:
    """図の下に出す実測(VBA `DisplayInfo_Normal` / `_Kyoyo`)。

    上用の図が無い(上下共用)ときは1行にまとめる ── VBA も
    `DisplayInfo_Kyoyo` で同じことをしていて、上用の欄だけ残ると
    「上用の実測が出ていない」と読める。
    """
    from .. import placement_render as render

    palette, product = session.palette, session.product
    lower_cut = render.cut_summary_text(
        placed, render.CATEGORY_LOWER, palette.width, palette.length)
    lower_deficit = render.coverage_deficit_text(
        placed, render.CATEGORY_LOWER, product.width, product.length)
    usage_lower = render.usage_ratio_by_category(
        placed, render.CATEGORY_LOWER, palette.width, palette.length)
    overhang_lower = render.overhang_ratio(
        placed, render.CATEGORY_LOWER, palette.width, palette.length)

    lower = InfoLine(
        label="上下共用" if shared else "下用",
        text=f"{lower_cut}  ／  {lower_deficit}",
        kind=_status_kind(lower_cut, lower_deficit))

    if shared:
        usage = f"使用率: {usage_lower:.1f}%　はみ出し: {overhang_lower:.1f}%"
        return usage, [lower]

    upper_cut = render.cut_summary_text(
        placed, render.CATEGORY_UPPER, product.width, product.length)
    upper_deficit = render.coverage_deficit_text(
        placed, render.CATEGORY_UPPER, product.width, product.length)
    usage_upper = render.usage_ratio_by_category(
        placed, render.CATEGORY_UPPER, palette.width, palette.length)
    upper = InfoLine(
        label="上用", text=f"{upper_cut}  ／  {upper_deficit}",
        kind=_status_kind(upper_cut, upper_deficit))

    usage = (f"下用: {usage_lower:.1f}%　上用: {usage_upper:.1f}%　"
             f"下はみ出し: {overhang_lower:.1f}%")
    # 図の並び(上用が上・下用が下)と同じ順にする。読む順と図の順が
    # 食い違うと、どちらの実測なのかを毎回確かめることになる
    return usage, [upper, lower]


def _status_kind(cut_text: str, deficit_text: str) -> str:
    """VBA `cut_status_color` の**判断だけ**を取り出す。

    色はテーマが3つあるので表示側が決める。判断を写し取らずに
    `cut_status_color()` を呼んで種別へ翻訳するのは、
    「どういうときに警告か」を2か所に書かないため。
    """
    from .. import placement_render as render
    background, _fg = render.cut_status_color(cut_text, deficit_text)
    if background == render.COLOR_INFO_DEFICIT_BG:
        return "deficit"
    if background == render.COLOR_INFO_CUT_BG:
        return "cut"
    return "ok"


def _angle_plan(session: Any) -> Optional[dict[str, Any]]:
    """アングル配置図。「アングル配置」を押すまでは描かない。"""
    from . import render_json

    if not (session.angle_drawn and session.selected_angles
            and session.product.is_set):
        return None
    leg_count = 0
    pallet_len = 0
    if session.palette.is_set:
        from .. import angle_service
        pallet_len = session.palette.length
        leg_count = angle_service.get_leg_count(
            session.presenter.conn, session.palette.width, session.palette.length)
    return render_json.with_tokens(render_json.build_angle_plan_dict(
        session.selected_angles, session.product.length, pallet_len, leg_count,
        need_cut=session.angle_need_cut))


# ------------------------------------------------------------------
# アングル
# ------------------------------------------------------------------
def build_angles(session: Any) -> AnglesViewModel:
    """アングルの部分。保護材が実際にアングルと判定されていなければ

    丸ごと出さない(`presenter.show_angle`)。

    【プロテックでも実際の判定に従う】以前はプロテックのとき、実際の
    保護材判定を見ずに常に非表示にし、"プロテックボード(上下共用)"
    という文言で隠していた。しかし「上用キャンバスを隠す」
    (`is_shared_board_mode`)ことと「アングルを表示するか」
    (`show_angle`)は独立した判断で、プロテックでも使用保護材が実際に
    アングルなら表示すべきだった(現場の声への対応。VBA
    `ShowAngleControls` の `angleVisibleOverride` 分離と同じ経緯)。
    ここでアングルが隠れているとすれば、それは上用キャンバスの都合
    ではなく、**保護材が実際にアングル以外と判定された**からなので、
    その理由をそのまま示す。
    """
    presenter = session.presenter
    show = presenter.show_angle
    hosozai_label = ("" if show or not presenter.last_hosozai
                     else f"使用保護材: {presenter.last_hosozai}")

    view = AnglesViewModel(
        show=show,
        # アングルを出しているあいだはアングルの欄があるので出さない。
        # 消えたときだけ、その場所に何を使うのかを出す
        hosozai_label=hosozai_label,
        selected=list(session.selected_angles),
        need_cut=session.angle_need_cut,
    )
    if not show:
        # 出さない欄の中身は引かない。マスタを読む分だけ遅くなる
        return view

    view.candidates = session.angle_candidates()
    if not session.product.is_set:
        view.auto_why = "先に製品サイズをセットしてください。"
    elif not view.candidates:
        view.auto_why = "アングルデータがありません。"
    view.can_auto = not view.auto_why

    # 「アングル配置」は選んだものを図にする操作。選ぶ前に押せると、
    # 何も起きないのか壊れているのか区別できない
    if not session.product.is_set:
        view.draw_why = "先に製品サイズをセットしてください。"
    elif not session.selected_angles:
        view.draw_why = "先にアングルを選んでください。"
    view.can_draw = not view.draw_why

    # 出す順は押す順(自動 → 配置)。dict でまとめると重複が落ちる
    view.why = list(dict.fromkeys(w for w in (view.auto_why, view.draw_why) if w))
    return view


def list_rows(session: Any) -> list[Any]:
    """いま一覧に出すべき行を、**そのつど引き直す**。

    セッションが覚えているのは「何で絞ったか」だけ。行を抱えておくと、
    EXの絞り込みを触ったときに古い行が残る。引き直せば、検索で絞った
    文脈を保ったまま EX のフィルタだけを掛け替えられる
    (tkinter版 `_on_pallet_filter_toggled` が直したのと同じ問題)。
    """
    from .. import board_selection_service as svc
    from .. import selection_session as ss
    from .. import work_context

    conn = session.presenter.conn
    flags = session.flags()
    context = work_context.get_context()

    # **絞り込みの経緯を残す。** サイズを打った時点で自動的に走る検索
    # なので、押した覚えのないまま候補が減る ── なぜその行が消えたのかは
    # 記録にしか残らない(現場の声)。2山積もここに効く
    if (session.list_mode == ss.LIST_PRODUCT
            and context.product_width and context.product_length):
        return svc.list_pallets_for_product(
            conn, product_width=context.product_width,
            product_length=context.product_length,
            two_stack=session.two_stack,
            # **単位の絞り込みに要る。** 渡していなかったので、製品
            # サイズを入れたあとの一覧だけ単位で絞られていなかった
            last_hosozai=session.presenter.last_hosozai,
            manufactured_thickness=session.presenter.manufactured_thickness,
            user_log=session.presenter.user_log,
            **flags)

    if session.list_mode == ss.LIST_PRODUCT_LIVE:
        return svc.list_pallets_by_product_dims(
            conn, product_width_text=session.live_product_width,
            product_length_text=session.live_product_length,
            last_hosozai=session.presenter.last_hosozai,
            two_stack=session.two_stack,
            manufactured_thickness=session.presenter.manufactured_thickness,
            user_log=session.presenter.user_log,
            **flags)

    if session.list_mode == ss.LIST_DIRECT:
        # 直接検索はEXオンリーを見ない(VBA 仕様どおり)
        return svc.search_pallet_direct(
            conn, pallet_width_text=session.direct_width,
            pallet_length_text=session.direct_length,
            show_all=session.show_all,
            last_hosozai=session.presenter.last_hosozai,
            is_1p1185_mode=session.presenter.mode_1p1185.is_1p1185)

    return svc.list_pallet_sizes(
        conn, last_hosozai=session.presenter.last_hosozai, **flags)


def _match_note(row: Any) -> str:
    """その行が**どう当たったのか**。単位・コードと並べて出す。

    製品サイズで絞ったときだけ意味がある(全件一覧では当たり方が無い)。
    現場の声:「クリックしたときに、コードと単位とは別に、回転が
    あったのか・厳密だったのか・+だったのかの表示も要る」── 同じ
    候補でも、製品を回す前提の行と、許容差でようやく入った行は、
    現物を前にしたときの扱いが違う。
    """
    if not getattr(row, "exact", False) and not getattr(row, "tolerance", 0):
        return ""                      # 製品サイズで絞っていない(全件一覧)
    parts = ["回転あり" if getattr(row, "rotated", False) else "回転なし"]
    if getattr(row, "exact", False):
        parts.append("厳密")
    else:
        parts.append(f"+{getattr(row, 'tolerance', 0)}mm")
    # 2山積は積み方まで出す。**幅2山と丈2山では現物の積み方が違う**
    if getattr(row, "two_stack", ""):
        parts.append(row.two_stack)
    return "   当たり方: " + " / ".join(parts)


def _pallet_row(row: Any, picked: Any = None) -> PalletRow:
    is_ex = "EX" in (row.symbol or "").upper()
    return PalletRow(
        values=[str(getattr(row, key)) for _label, key, _numeric in PALLET_COLUMNS],
        width=row.width, length=row.length, symbol=row.symbol, is_ex=is_ex,
        note=(f"単位: {row.unit or '---'}   コード: {row.code or '---'}"
              + _match_note(row)),
        picked=(picked is not None and picked.id == row.id),
    )


def _list_note(session: Any, count: int) -> str:
    """一覧が何を出しているのかを一言で。

    同じ見た目の表が「全件」だったり「検索結果」だったりすると、
    件数が減った理由が分からない。
    """
    from .. import selection_session as ss
    if session.list_mode == ss.LIST_PRODUCT:
        return f"製品サイズに収まる候補 {count} 件"
    if session.list_mode == ss.LIST_PRODUCT_LIVE:
        has_width = bool(session.live_product_width.strip())
        has_length = bool(session.live_product_length.strip())
        if has_width and has_length:
            return f"製品サイズに収まる候補 {count} 件(入力中)"
        if has_width:
            return f"製品 幅に近いパレット {count} 件(丈も入れると絞り込みます)"
        if has_length:
            return f"製品 丈に近いパレット {count} 件(幅も入れると絞り込みます)"
        return f"{count} 件"
    if session.list_mode == ss.LIST_DIRECT:
        return (f"パレット寸法 {session.direct_width or '—'}×"
                f"{session.direct_length or '—'} の前後 {SEARCH_TOLERANCE}mm: "
                f"{count} 件")
    if session.show_all:
        return f"全 {count} 件(EXまで表示)"
    return f"{count} 件"


def to_dict(view: SelectionViewModel) -> dict[str, Any]:
    """JSONにできる形。"""
    return {
        "available": view.available,
        "unavailable_message": view.unavailable_message,
        "lot_no": view.lot_no,
        "lot_caption": view.lot_caption,
        "banner": {"kind": view.banner.kind, "text": view.banner.text,
                   "visible": view.banner.visible},
        "rows": [{"values": r.values, "width": r.width, "length": r.length,
                  "symbol": r.symbol, "is_ex": r.is_ex, "note": r.note,
                  "picked": r.picked}
                 for r in view.rows],
        "list_mode": view.list_mode,
        "list_note": view.list_note,
        "pallet_width": view.pallet_width,
        "pallet_length": view.pallet_length,
        "product_width": view.product_width,
        "product_length": view.product_length,
        "pallet_status": view.pallet_status,
        "pallet_set": view.pallet_set,
        "product_status": view.product_status,
        "product_set": view.product_set,
        "show_all": view.show_all,
        "ex_only": view.ex_only,
        "ex_only_enabled": view.ex_only_enabled,
        "ex_only_why": view.ex_only_why,
        "two_stack": view.two_stack,
        "mode_1p0113": view.mode_1p0113,
        "can_set_product": view.can_set_product,
        "set_product_why": view.set_product_why,
        "can_decide": view.can_decide,
        "decide_why": view.decide_why,
        "product_from": view.product_from,
        "size_status": view.size_status,
        "boards": boards_to_dict(view.boards),
        "angles": angles_to_dict(view.angles),
        "plans": plans_to_dict(view.plans),
        "p1": p1_to_dict(view.p1),
        "admin": admin_to_dict(view.admin),
        "outputs": outputs_to_dict(view.outputs),
        "steps": [s.to_dict() for s in view.steps],
        "primary_action": view.primary_action,
        "next_hint": view.next_hint,
    }


def admin_to_dict(view: AdminViewModel) -> dict[str, Any]:
    return {
        "authenticated": view.authenticated,
        "can_save": view.can_save,
        "save_why": view.save_why,
        "save_confirm": view.save_confirm,
        "patterns": [{"id": p.id, "product": p.product,
                      "registered_at": p.registered_at,
                      "usage_count": p.usage_count, "boards": p.boards,
                      "method": p.method, "unsent": p.unsent}
                     for p in view.patterns],
        "patterns_note": view.patterns_note,
        "usage": [{"width": u.width, "length": u.length, "board_type": u.board_type,
                   "usage_count": u.usage_count, "last_used_at": u.last_used_at}
                  for u in view.usage],
    }


def outputs_to_dict(view: OutputsViewModel) -> dict[str, Any]:
    return {
        "reports": [{"key": r.key, "label": r.label, "url": r.url,
                     "can": r.can, "why": r.why, "ask": r.ask}
                    for r in view.reports],
        "can_send": view.can_send,
        "send_why": view.send_why,
        "can_use": view.can_use,
        "use_why": view.use_why,
        "use_done": view.use_done,
    }


def p1_to_dict(view: Mode1P0113ViewModel) -> dict[str, Any]:
    return {
        "on": view.on,
        "forced": view.forced,
        "materials": [{"key": m.key, "name": m.name, "label": m.label,
                       "info": m.info, "count_caption": m.count_caption,
                       "ok": m.ok} for m in view.materials],
        "tip": view.tip,
        "qty": view.qty,
        "qty_min": view.qty_min,
        "qty_max": view.qty_max,
        "ready": view.ready,
        "why": view.why,
    }


def plans_to_dict(view: PlansViewModel) -> dict[str, Any]:
    return {
        "placed": view.placed,
        "status": view.status,
        "view_box": view.view_box,
        "angle_view_box": view.angle_view_box,
        "upper_title": view.upper_title,
        "lower_title": view.lower_title,
        "angle_title": view.angle_title,
        "show_upper": view.show_upper,
        "upper": view.upper,
        "lower": view.lower,
        "angle": view.angle,
        "usage": view.usage,
        "lines": [{"label": line.label, "text": line.text, "kind": line.kind}
                  for line in view.lines],
    }


def boards_to_dict(view: BoardsViewModel) -> dict[str, Any]:
    return {
        "board_types": view.board_types,
        "board_type": view.board_type,
        "candidates": [{"values": c.values, "width": c.width,
                        "length": c.length, "stock_low": c.stock_low}
                       for c in view.candidates],
        "candidate_note": view.candidate_note,
        "upper": [_selected_to_dict(r) for r in view.upper],
        "lower": [_selected_to_dict(r) for r in view.lower],
        "upper_title": view.upper_title,
        "lower_title": view.lower_title,
        "show_upper": view.show_upper,
        "summary": view.summary,
        "fatigue": view.fatigue,
        "stock_aware": view.stock_aware,
        "base_point": view.base_point,
        "can_select": view.can_select,
        "select_why": view.select_why,
        "can_place": view.can_place,
        "place_why": view.place_why,
        "can_change": view.can_change,
        "change_why": view.change_why,
        "change_axis": view.change_axis,
    }


def _selected_to_dict(row: SelectedRow) -> dict[str, Any]:
    return {"index": row.index, "values": row.values, "width": row.width,
            "length": row.length, "count": row.count, "key": row.key,
            "tag": row.tag, "tag_kind": row.tag_kind}


def angles_to_dict(view: AnglesViewModel) -> dict[str, Any]:
    return {
        "show": view.show,
        "hosozai_label": view.hosozai_label,
        "candidates": view.candidates,
        "selected": view.selected,
        "need_cut": view.need_cut,
        "can_auto": view.can_auto,
        "auto_why": view.auto_why,
        "can_draw": view.can_draw,
        "draw_why": view.draw_why,
        "why": view.why,
    }
