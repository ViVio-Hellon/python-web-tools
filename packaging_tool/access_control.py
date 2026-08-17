"""アクセス権限 ── この端末で何ができるか

Windows のログインID と PC名 を条件に、その利用者が持つ**権限**を引く。

【なぜ要るのか】
これまで「現場か資材か」は**どのショートカットを押したか**でしか分かれて
いなかった。同じPCの別の人が資材モードのポートを開けば、資材の操作が
そのままできてしまう。守っていたのは「誰か」ではなく「どのポートか」で、
これは権限とは呼べない。

ここでは**誰か**で分ける。ログインIDとPC名は Windows が持っている事実で、
利用者が画面から変えられない。パスワードのように「教え合える」ものでもない。

【拡張性】
権限は `PERMISSIONS` に1行足せば増える。マスタ側は権限コードを持つだけ
なので、**列を足す必要も、この層を書き換える必要もない**。
いまは「使えるモード」だけだが、次に足したいものが来ても同じ形で載る
(例: 実績パターンを保存できる / 発注を取り消せる)。

【マスタの形】
1行 = 1つの許可。

    ログインID  PC名        権限            有効
    ----------  ----------  --------------  ----
    yamada      (空)        mode:material   1     … 山田はどのPCでも資材
    (空)        NLM-PC-042  mode:material   1     … この端末は誰でも資材
    suzuki      NLM-PC-042  mode:material   1     … 鈴木がこの端末のときだけ

空欄は「問わない」。**両方が空の行は誰にも効かない** ── それは全員への
許可で、権限を設ける意味が消える。`Rule.matches` が撥ね、設定画面が
「条件の無い行」として警告に出す(黙って無視すると、書いた人は
効いているつもりのままになる)。

【該当が無いとき】
**現場モードだけ**を持つ。締め出さず、かつ勝手に強い権限も渡さない。

- 全部止める案は採らない。取り込みに失敗しただけで全員が仕事を
  始められなくなる
- 全部許す案も採らない。登録し忘れた端末が資材モードに入れてしまい、
  権限を設けた意味がなくなる

理由は設定画面に出る。「なぜ資材モードが選べないのか」を、
利用者が自分で確かめられるようにしてある。
"""
from __future__ import annotations

import difflib
import getpass
import os
import platform
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .logging_utils import get_logger

log = get_logger("access_control")

TABLE = "アクセス権限"

# 検証用に身元を差し替える。本番では設定しない
ENV_LOGIN = "PACKAGING_TOOL_LOGIN_ID"
ENV_HOST = "PACKAGING_TOOL_PC_NAME"


# ==================================================================
# 権限の一覧 ── ここに足せば増える
# ==================================================================
@dataclass(frozen=True)
class Permission:
    """1つの権限。

    `code` がマスタに書かれる値。**画面の文言はここが唯一の出どころ**で、
    設定画面もモード切替も同じ語を使う(同じ事実を2か所に持たない)。
    """

    code: str
    label: str
    detail: str
    # 何についての権限か。設定画面のまとまりに使う
    category: str = "モード"


# 使えるモード。モードを増やすときは、ここと `modes.py` の両方ではなく
# **`modes.py` だけ**に足す ── この一覧は `modes.py` から作る
PERM_PREFIX_MODE = "mode:"


def mode_permission(mode: str) -> str:
    """モードを使う権限のコード。"""
    return f"{PERM_PREFIX_MODE}{mode}"


def _mode_permissions() -> tuple[Permission, ...]:
    from . import modes
    return tuple(
        Permission(code=mode_permission(m.key),
                   label=f"{m.label}モード",
                   detail=m.detail,
                   category="モード")
        for m in modes.ALL)


def permissions() -> tuple[Permission, ...]:
    """使える権限ぜんぶ。

    いまはモードだけ。別の種類を足すときは、ここに連結する
    (`return _mode_permissions() + _report_permissions()` のように)。
    """
    return _mode_permissions()


def find_permission(code: str) -> Optional[Permission]:
    for item in permissions():
        if item.code == code:
            return item
    return None


def is_known(code: str) -> bool:
    return find_permission(code) is not None


# ==================================================================
# 身元 ── Windows が持っている事実
# ==================================================================
@dataclass(frozen=True)
class Identity:
    """いまこの端末を使っている人と、その端末。"""

    login_id: str
    pc_name: str

    def label(self) -> str:
        return f"{self.login_id or '(不明)'} @ {self.pc_name or '(不明)'}"


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def current_identity() -> Identity:
    """ログインIDとPC名。

    Windows は `USERNAME` / `COMPUTERNAME` を必ず持っている。
    開発機(Linux)でも動くよう、無ければ Python の一般的な手段へ落とす。
    """
    login = _clean(os.environ.get(ENV_LOGIN)) or _clean(os.environ.get("USERNAME"))
    if not login:
        try:
            login = _clean(getpass.getuser())
        except Exception:                     # noqa: BLE001 - 環境依存で落ちうる
            login = ""
    host = _clean(os.environ.get(ENV_HOST)) or _clean(os.environ.get("COMPUTERNAME"))
    if not host:
        host = _clean(platform.node())
    return Identity(login_id=login, pc_name=host)


def _same(a: str, b: str) -> bool:
    """Windows のIDとPC名は**大文字小文字を区別しない**。"""
    return a.casefold() == b.casefold()


# ==================================================================
# マスタの1行
# ==================================================================
@dataclass
class Rule:
    """マスタの1行。空欄は「問わない」。"""

    login_id: str = ""
    pc_name: str = ""
    permission: str = ""
    enabled: bool = True
    note: str = ""

    def has_condition(self) -> bool:
        return bool(self.login_id or self.pc_name)

    def matches(self, identity: Identity) -> bool:
        # 条件が1つも無い行は全員に効いてしまう。権限を設ける意味が
        # 消えるので、書かれていても効かせない
        if not self.enabled or not self.has_condition():
            return False
        if self.login_id and not _same(self.login_id, identity.login_id):
            return False
        if self.pc_name and not _same(self.pc_name, identity.pc_name):
            return False
        return True

    def condition_label(self) -> str:
        """何を条件にした行か(設定画面に出す)。"""
        parts = []
        if self.login_id:
            parts.append(f"ID {self.login_id}")
        if self.pc_name:
            parts.append(f"PC {self.pc_name}")
        return " かつ ".join(parts) if parts else "(条件なし)"

    def to_dict(self) -> dict[str, Any]:
        return {"login_id": self.login_id, "pc_name": self.pc_name,
                "permission": self.permission, "enabled": self.enabled,
                "note": self.note, "condition": self.condition_label()}


# ==================================================================
# 引く
# ==================================================================
@dataclass
class Grant:
    """この端末が持っているもの。

    `codes` だけでなく `matched` と `reason` も返すのは、
    **「なぜ資材モードが選べないのか」を利用者が確かめられる**ようにするため。
    権限は、通ったときより通らなかったときのほうが説明を必要とする。
    """

    identity: Identity
    codes: frozenset[str] = frozenset()
    matched: list[Rule] = field(default_factory=list)
    # マスタが取り込まれているか
    has_master: bool = False
    # 既定へ落ちた理由(落ちていなければ空)
    reason: str = ""

    def has(self, code: str) -> bool:
        return code in self.codes

    def allows_mode(self, mode: str) -> bool:
        return self.has(mode_permission(mode))

    def allowed_modes(self) -> tuple[str, ...]:
        from . import modes
        return tuple(m.key for m in modes.ALL if self.allows_mode(m.key))

    def startup_mode(self) -> str:
        """起動したときに開くモード。

        **どの近道を押したかでは決めない。** 以前は現場用と資材用で
        起動ファイルを分けていたが、資材の端末で現場のほうを押すと
        「発注一覧が出ない」となり、権限の問題として調べることになる。
        開くモードは端末そのものが決められる ── それを決めているのが
        このマスタだから。

        許されているものが1つならそれ。複数あるなら既定(現場)から
        始める ── 帯の切替でいつでも移れるので、**狭いほうから**開く。
        """
        from . import modes
        allowed = self.allowed_modes()
        if modes.DEFAULT in allowed:
            return modes.DEFAULT
        return allowed[0] if allowed else modes.DEFAULT

    def to_dict(self) -> dict[str, Any]:
        return {
            "login_id": self.identity.login_id,
            "pc_name": self.identity.pc_name,
            "label": self.identity.label(),
            "codes": sorted(self.codes),
            "matched": [r.to_dict() for r in self.matched],
            "has_master": self.has_master,
            "reason": self.reason,
            "allowed_modes": list(self.allowed_modes()),
        }


# 該当が無いときに渡すもの。**いちばん狭いモードだけ**
def _fallback_codes() -> frozenset[str]:
    from . import modes
    return frozenset({mode_permission(modes.DEFAULT)})


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    """マスタを読む。テーブルが無ければ空を返す(まだ取り込んでいない)。"""
    try:
        rows = conn.execute(
            f'SELECT "ログインID", "PC名", "権限", "有効", "備考" '
            f'FROM {TABLE} ORDER BY "管理番号"').fetchall()
    except sqlite3.Error as exc:
        log.info("%s を読めません(未取り込み): %s", TABLE, exc)
        return []
    return [Rule(login_id=_clean(r[0]), pc_name=_clean(r[1]),
                 permission=_clean(r[2]), enabled=bool(r[3]),
                 note=_clean(r[4]))
            for r in rows]


def resolve(conn: sqlite3.Connection,
            identity: Optional[Identity] = None) -> Grant:
    """この端末が持っている権限。

    該当行の**和**を取る。「IDで許す行」と「PCで許す行」が両方あれば
    両方効く ── 片方だけを採る規則にすると、どちらが勝つのかを
    覚えていないと結果を説明できない。
    """
    identity = identity or current_identity()
    rules = load_rules(conn)
    if not rules:
        return Grant(identity=identity, codes=_fallback_codes(),
                     has_master=False,
                     reason=f"{TABLE} がまだ取り込まれていません")

    matched = [r for r in rules if r.matches(identity)]
    codes = {r.permission for r in matched if is_known(r.permission)}
    unknown = sorted({r.permission for r in matched
                      if r.permission and not is_known(r.permission)})
    if unknown:
        # 知らない権限コードは黙って捨てない。マスタ側の打ち間違いは
        # 「効かない」としか現れず、原因を探しようがない
        log.warning("%s に未知の権限コードがあります: %s", TABLE, ", ".join(unknown))

    if not codes:
        return Grant(identity=identity, codes=_fallback_codes(),
                     matched=matched, has_master=True,
                     reason=(f"{TABLE} に {identity.label()} の登録が"
                             "ありません"))

    # モードの権限が1つも無ければ**使える画面が1枚も無くなる**。
    # モード以外の権限だけを書いた行(将来足す権限だけを与えた行)で
    # 起きるので、ここで既定のモードを足しておく
    reason = ""
    if not any(c.startswith(PERM_PREFIX_MODE) for c in codes):
        codes |= _fallback_codes()
        reason = (f"{TABLE} の登録にモードの権限が無いため、"
                  "現場モードだけを使えるようにしています")
    return Grant(identity=identity, codes=frozenset(codes),
                 matched=matched, has_master=True, reason=reason)


def problems(conn: sqlite3.Connection) -> list[str]:
    """マスタの中で、書いた人の意図どおりに効かない行。

    設定画面に出す。**黙って無視すると、書いた人は効いているつもりの
    ままになる。** 打ち間違いも、条件を書き忘れた行も、ここに現れる。
    """
    rules = load_rules(conn)
    if not rules:
        return []
    out: list[str] = []
    known = {p.code for p in permissions()}
    unknown = sorted({r.permission for r in rules
                      if r.permission and r.permission not in known})
    if unknown:
        # **打ち間違いは、指摘だけでは直せない。** `mode:materia` と
        # 書かれた行を「知らないコードです」と言われても、どこが違うのかは
        # 目で見比べるしかない(現場の声:1文字足りないことに気づけない)。
        # 近いコードが1つに決まるなら、そのまま書き写せる形で出す
        parts = []
        for code in unknown:
            near = difflib.get_close_matches(code, sorted(known), n=1, cutoff=0.6)
            parts.append(f"{code} → {near[0]} のことですか?" if near else code)
        out.append("知らない権限コードがあります(効きません): "
                   + ", ".join(parts))
    blank = sum(1 for r in rules if not r.has_condition())
    if blank:
        # **何を書けば効くのか**まで出す。空欄のままにした人は、
        # たいてい「全員に効かせたい」つもりでいる
        me = current_identity()
        out.append(f"ログインID も PC名 も空の行が {blank} 件あります"
                   "(全員への許可になるので効かせていません)。"
                   "どちらか一方でも埋めれば効きます ── この端末なら "
                   f"ログインID = {me.login_id or '(空)'} / "
                   f"PC名 = {me.pc_name or '(空)'} です。")
    return out


def grant_of(*codes: str, identity: Optional[Identity] = None) -> Grant:
    """指定した権限だけを持つ `Grant` を作る。

    マスタを用意せずに「この権限を持っていたら何が起きるか」を
    確かめるための入口。試験と、権限を引けなかったときの既定に使う。
    **本番の経路(`resolve`)はここを通らない。**
    """
    unknown = [c for c in codes if not is_known(c)]
    if unknown:
        raise ValueError(f"知らない権限コード: {', '.join(unknown)}")
    return Grant(identity=identity or current_identity(),
                 codes=frozenset(codes), has_master=True)


def explain_grant(grant: Grant) -> str:
    """権限の状態を、設定画面へ移らなくても読める一文にする。

    帯のモード表示は「今このモードしか使えない」ことは見せていたが、
    **なぜか・増やすには何をすればよいか**は設定画面まで押さないと
    出なかった。「変更条件をそこで表示させるのがわかりやすい」という
    現場の声に対応 ── ホバーだけで、設定画面の「この端末の権限」に
    書いてあるのと同じ具体性(誰が・どこで・何を足すか)まで届かせる。

    既定へ落ちたとき(`grant.reason`)だけでなく、**正しく1つだけ
    許可されている**(現場ではよくある、ごく普通の)ときも同じ扱い ──
    「他のモードはどうすれば使えるのか」を知りたいのは、落ちたときも
    落ちていないときも同じだから。
    """
    from . import modes

    allowed = grant.allowed_modes()
    allowed_labels = "/".join(modes.label(m) for m in allowed) or "なし"
    text = f"{grant.identity.label()} が使えるのは{allowed_labels}モードだけです。"
    if grant.reason:
        text = grant.reason + "。" + text

    missing = [m.key for m in modes.ALL if m.key not in allowed]
    if not missing:
        return text

    mode_codes = ", ".join(f"{mode_permission(m)}({modes.label(m)}モード)"
                           for m in missing)
    missing_labels = "/".join(modes.label(m) for m in missing)
    return (text + f"{missing_labels}モードを増やすには、資材モードを持つ人に"
            f"「設定 > マスタ管理 > {TABLE}」で次の行を足してもらってください: "
            f"ログインID = {grant.identity.login_id or '(空でPC名だけでもよい)'} / "
            f"PC名 = {grant.identity.pc_name or '(空でIDだけでもよい)'} / "
            f"権限 = {mode_codes}。")


def summarize(codes: Iterable[str]) -> list[str]:
    """権限コードを画面の言葉にする。文言はサーバが持つ。"""
    out = []
    for code in sorted(codes):
        item = find_permission(code)
        out.append(item.label if item else f"{code}(不明)")
    return out


def resync(conn: sqlite3.Connection, *, path: Optional[Path] = None) -> bool:
    """アクセス権限**だけ**を取り込み元から読み直し、手元をそろえる。

    【なぜここで読み直すのか】
    マスタ管理から書けば `_follow()` がその場で手元を追いつかせる。
    しかし取り込み元は共有フォルダの1ファイルで、書く場所は
    ここ(Webアプリ)だけとは限らない ── 別途Access側の変換を
    やり直す・別の端末が同時に書く、といった経路では、手元がいつの間にか
    取り込み元より遅れて残ることがある。

    **モードを切り替えられなかったときこそ、疑わしいのはここ。**
    「アクセス権限マスタには正しい行が入っている(取り込み元を見れば
    分かる)のに、なぜか切り替わらない」という声は、たいてい手元が
    追いついていないだけ。切替を断る前に一度だけ読み直し、それでも
    駄目なら素直に断る ── 読み直しても通らないなら、行が本当に
    足りていないということ。

    見つからない・開けない・取り込めないときは**黙って諦める**
    (`False` を返すだけ)。ここでの失敗はモード切替そのものを
    止める理由にはしない ── 手元にある分で判定を続ける。
    """
    from . import data_sync, import_specs

    path = path or data_sync.find_material_db()
    if path is None:
        return False
    try:
        result = data_sync.import_tables(
            conn, path, {TABLE: import_specs.IMPORT_SPECS[TABLE]},
            required=import_specs.REQUIRED_KEY_COLUMNS,
            blank_is_missing=import_specs.BLANK_IS_MISSING,
            optional=import_specs.OPTIONAL_TABLES,
            fallbacks=import_specs.NULL_FALLBACKS)
    except sqlite3.Error as exc:
        log.warning("%s の読み直しに失敗しました: %s", TABLE, exc)
        return False
    return TABLE in result.imported


def startup_grant() -> Grant:
    """起動時にこの端末の権限を引く。

    アプリを組み立てる前なので Flask の要求コンテキストが無い。
    自前で接続を開いて閉じる。DBが無い/壊れている段階でも起動は
    続ける必要があるので、失敗しても既定(現場のみ)へ落とす。

    **ここに置くのは、Flask より前に呼ぶため。** 起動の入口が
    `app`(= Flask とアプリ本体ぜんぶ)を読まずに開くモードを決め
    られるので、待機画面がその分だけ早く出る(基盤仕様書 2.2)。
    """
    from . import db, modes

    conn = None
    try:
        conn = db.get_connection()
        return resolve(conn)
    except Exception as exc:                    # noqa: BLE001 - 起動を止めない
        log.warning("権限を引けませんでした(現場モードのみで続行): %s", exc)
        return Grant(identity=current_identity(),
                     codes=frozenset({mode_permission(modes.DEFAULT)}),
                     reason=f"権限を引けませんでした: {exc}")
    finally:
        if conn is not None:
            conn.close()
