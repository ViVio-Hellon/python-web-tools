"""包装仕様書の図面を裏で取ってきて、ロット検索の画面に出す

【いま起きていること】
包装仕様NOは社内の閲覧システム(`presenters.lot.URL_HOSO_SHIYOSHO`)への
リンクになっている。押すと別タブが開き、そこで**もう一度**包装仕様NOを
打ち直して「検索」を押すと、ようやく図面が出る。
目の前にNOが出ているのに、それをまた打つ ── 転記の手間と打ち間違いが、
そのまま作業の中に残っている。

ここは、ロットが見つかった時点で**裏で図面を取りに行き**、ロット検索の
画面にそのまま出す。押す・打つ・戻る、が消える。

【取得先URLを設定にしてある理由】
閲覧システムは Angular で、画面のURL(`#!/home?mode=hosoShiyoshoEtsuran`)
には仕様NOが入らない。図面そのものは別のエンドポイントが返していて、
その形はこのリポジトリからは分からない。
**推測でURLを埋め込むと、間違った先を叩き続けて原因が見えなくなる**。
なのでURLは設定画面の項目にした。設定文字列の `{no}` が
包装仕様NOに置き換わる。

    http://nlmfangysysv:9084/NgyPkgWeb/rest/pkgSpec/image?pkgSpecNo={no}

実際の形は、閲覧システムで「検索」を押したときにブラウザが投げている
リクエスト(開発者ツールのネットワークタブ)がそのまま答えになる。

【プロキシについて】
ここは `launch_guard.local_request` と違い、**プロキシ設定をそのまま使う**。
あちらは自分自身(127.0.0.1)への通信なので必ず直結させる必要があった。
こちらは社内の別サーバで、ブラウザが届いている経路と同じ扱いにするのが
正しい(社内ホストは通常 no_proxy 側で直結になる)。

【キャッシュ】
同じ仕様NOのロットは連続して流れる。`%LOCALAPPDATA%\\PackagingTool\\cache\\
spec\\` に置いて、2回目以降は即座に出す(基盤仕様書 2.7 の cache 領域)。
消えても取り直せるものしか置かない。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import app_config, config, user_settings
from .logging_utils import get_logger

log = get_logger("spec_sheet")

# 設定文字列の中で包装仕様NOに置き換わる目印
PLACEHOLDER = "{no}"

# 包装仕様NOとして通す形。閲覧システムの入力欄が maxlength="6" なので
# 6桁が実際の形("1P0001" など)だが、桁が増えても困らないよう少し広く取る。
#
# **この検査はキャッシュのファイル名を守る役目も兼ねている**。
# 英数字だけに限れば `..` や `/` がパスに混ざりようがない
NO_PATTERN = re.compile(r"[0-9A-Za-z]{1,12}")

# 取りに行く時間の上限(秒)。裏で走るので画面は止まらないが、
# 応答しないサーバをいつまでも待つと「読み込み中」が消えなくなる
TIMEOUT_SEC = 15.0

# 受け取る大きさの上限。図面1枚が数十MBになることは無く、
# それを超えるものが返ってきたら設定先が違っている
MAX_BYTES = 20 * 1024 * 1024

# キャッシュを取り直す間隔(秒)。仕様書は頻繁には変わらないが、
# 変わったときに古いままだと困るので1日で見直す
CACHE_TTL_SEC = 24 * 60 * 60

# 「取れなかった」を覚えておく長さ(秒)。
#
# **覚えておく理由** … 届かないサーバを、ロットを引くたびに叩き続けない
# **忘れる理由**     … 期限が無いと、閲覧システムが一瞬落ちただけで、
#                      そのNOはツールを閉じるまでずっと図面が出ない。
#                      「再取得」を押せば直るが、現場からは「出ない」と
#                      しか見えない(開いたままの端末ほど長く尾を引く)
#
# 3分は「連打では叩きに行かないが、席を立って戻ってくれば直っている」長さ
FAILED_TTL_SEC = 3 * 60

# 図として出せる形。HTMLが返ってきたら「取れた」ではなく設定の誤り
IMAGE_TYPES = ("image/", "application/pdf")

STATE_UNSET = "unset"      # 取得先URLが未設定
STATE_LOADING = "loading"  # 取りに行っている最中
STATE_READY = "ready"      # 出せる
STATE_ERROR = "error"      # 取れなかった(理由を添える)

# URLが未設定のときに画面へ出す案内。「設定してください」だけでは
# **何を**設定すればよいか分からないので、調べ方まで書く
UNSET_MESSAGE = (
    "包装仕様書の取得先URLが未設定です。"
    "設定画面で設定すると、ここに図面が出ます")


@dataclass
class Status:
    """1件の状態。画面はこれをそのまま読む。"""

    no: str = ""
    state: str = STATE_UNSET
    message: str = ""
    content_type: str = ""
    size: int = 0
    fetched_at: float = 0.0
    source_url: str = ""       # どこから取ったか(設定の誤りを追える)

    @property
    def ready(self) -> bool:
        return self.state == STATE_READY


# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
def url_template() -> str:
    """取得先URLのひな形。未設定なら空。"""
    value = user_settings.get(config.KEY_SPEC_SHEET_URL)
    return value.strip() if isinstance(value, str) else ""


def template_problem(template: str) -> str:
    """ひな形の不備を日本語で返す。問題が無ければ空。

    保存する前にここで弾く。保存できてしまってから
    「なぜか図面が出ない」を追うより、その場で言うほうが早い。
    """
    text = (template or "").strip()
    if not text:
        return ""          # 未設定は不備ではない(機能を使わないだけ)
    if not text.lower().startswith(("http://", "https://")):
        return "http:// または https:// で始めてください"
    if PLACEHOLDER not in text:
        return f"包装仕様NOの位置に {PLACEHOLDER} を入れてください"
    return ""


def build_url(template: str, no: str) -> str:
    """ひな形と仕様NOから、実際に叩くURLを作る。

    `{no}` はURLの一部になるので、そのまま入れずに符号化する。
    `NO_PATTERN` を通った英数字だけなので実際には変化しないが、
    検査を緩めたときにここが穴にならないようにしておく。
    """
    return template.replace(PLACEHOLDER, urllib.parse.quote(no, safe=""))


def is_valid_no(no: str) -> bool:
    return bool(NO_PATTERN.fullmatch(no or ""))


# ------------------------------------------------------------------
# キャッシュ
# ------------------------------------------------------------------
def cache_dir() -> Path:
    return app_config.local_dir("cache") / "spec"


def _paths(no: str) -> tuple[Path, Path]:
    """(中身, 付帯情報)。`no` は検査済みである前提。"""
    base = cache_dir()
    return base / f"{no}.bin", base / f"{no}.json"


def _read_meta(no: str) -> Optional[dict]:
    body, meta = _paths(no)
    if not (body.exists() and meta.exists()):
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(no: str, payload: bytes, content_type: str, source_url: str) -> None:
    body, meta = _paths(no)
    body.parent.mkdir(parents=True, exist_ok=True)
    # 書いている途中のものを読ませない。別名に書いてから差し替える
    tmp = body.with_suffix(".part")
    tmp.write_bytes(payload)
    tmp.replace(body)
    meta.write_text(json.dumps({
        "content_type": content_type,
        "size": len(payload),
        "fetched_at": time.time(),
        "source_url": source_url,
    }, ensure_ascii=False), encoding="utf-8")


def read_cached(no: str) -> Optional[tuple[bytes, str]]:
    """(中身, Content-Type)。無ければ None。"""
    meta = _read_meta(no)
    if meta is None:
        return None
    body, _ = _paths(no)
    try:
        return body.read_bytes(), str(meta.get("content_type") or "")
    except OSError:
        return None


def clear(no: str) -> None:
    for path in _paths(no):
        try:
            path.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------
# 取得
# ------------------------------------------------------------------
def _describe(exc: Exception, url: str) -> str:
    """失敗の理由を、現場が次に何をすればよいか分かる言葉にする。"""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return (f"閲覧システムに拒否されました (HTTP {exc.code})。"
                    "ブラウザで一度ログインしてから、もう一度試してください")
        if exc.code == 404:
            return (f"その包装仕様NOの図面が見つかりません (HTTP {exc.code})。"
                    "取得先URLの形が違う可能性もあります")
        return f"閲覧システムがエラーを返しました (HTTP {exc.code})"
    if isinstance(exc, urllib.error.URLError):
        return f"閲覧システムに接続できません ({exc.reason})。{url}"
    return f"取得に失敗しました: {exc}"


def fetch(no: str, template: str) -> Status:
    """1件取りに行く。**呼んだスレッドを止める**(裏方から呼ぶ)。"""
    url = build_url(template, no)
    # 設定ファイルを手で書き換えられた場合の受け止め。`urlopen` は
    # `file://` も開けてしまうので、ここでも通信先の形を確かめる
    if not url.lower().startswith(("http://", "https://")):
        return Status(no=no, state=STATE_ERROR, source_url=url,
                      message="取得先URLは http:// または https:// で始めてください")
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as res:
            content_type = (res.headers.get("Content-Type") or "").split(";")[0].strip()
            payload = res.read(MAX_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - 通信は何が来るか分からない
        log.warning("包装仕様書を取得できませんでした: %s (%s)", no, exc)
        return Status(no=no, state=STATE_ERROR, message=_describe(exc, url),
                      source_url=url)

    if len(payload) > MAX_BYTES:
        return Status(no=no, state=STATE_ERROR, source_url=url,
                      message=(f"図面が大きすぎます({MAX_BYTES // 1024 // 1024}MB超)。"
                               "取得先URLが図面以外を指している可能性があります"))

    if not content_type.startswith(IMAGE_TYPES):
        # HTMLが返るのは「閲覧システムの画面そのもの」を指しているとき。
        # ここが一番起こりやすい設定の誤りなので、名指しで言う
        return Status(no=no, state=STATE_ERROR, source_url=url,
                      message=(f"図面ではなく {content_type or '不明な形式'} が"
                               "返りました。取得先URLは画面ではなく"
                               "**画像を返すアドレス**を指定してください"))

    _write(no, payload, content_type, url)
    log.info("包装仕様書を取得しました: %s (%s, %d bytes)", no, content_type, len(payload))
    return Status(no=no, state=STATE_READY, content_type=content_type,
                  size=len(payload), fetched_at=time.time(), source_url=url)


class Fetcher:
    """裏で取りに行く係。プロセスに1つ。

    `jobs.JobRegistry` と分けてあるのは、あちらが「一度に1つだけ」を
    守る取り込み処理用だから。図面の取得は短く、ロットを続けて引くたびに
    走るので、同じ枠で数えると取り込みが始められなくなる。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()
        # NO → (いつ失敗したか, そのときの状態)
        self._failed: dict[str, tuple[float, Status]] = {}

    # -- 問い合わせ --------------------------------------------------
    def status(self, no: str) -> Status:
        """いまの状態。画面はこれを見て出し分ける。"""
        template = url_template()
        if not template:
            return Status(no=no, state=STATE_UNSET, message=UNSET_MESSAGE)

        meta = _read_meta(no)
        if meta is not None:
            return Status(no=no, state=STATE_READY,
                          content_type=str(meta.get("content_type") or ""),
                          size=int(meta.get("size") or 0),
                          fetched_at=float(meta.get("fetched_at") or 0.0),
                          source_url=str(meta.get("source_url") or ""))

        with self._lock:
            if no in self._running:
                return Status(no=no, state=STATE_LOADING,
                              message="図面を取得しています")
            failed = self._failed.get(no)
        # 期限が切れていても**理由は出したまま**にする。次に開いた時点で
        # 取り直しは始まるので、状態は `prefetch` の側で変わる
        if failed is not None:
            return failed[1]
        return Status(no=no, state=STATE_LOADING, message="図面を取得しています")

    # -- 指示 --------------------------------------------------------
    def prefetch(self, no: str) -> bool:
        """裏で取り始める。始めたら True。

        すでに手元にあるとき・走っているとき・一度失敗しているときは
        始めない。**失敗を覚えておく**のは、届かないサーバを
        ロットを引くたびに叩き続けないため(「再取得」で消える)。
        """
        template = url_template()
        if not (template and is_valid_no(no)):
            return False

        meta = _read_meta(no)
        if meta is not None and not _is_stale(meta):
            return False

        with self._lock:
            if no in self._running:
                return False
            failed = self._failed.get(no)
            if failed is not None and time.time() - failed[0] < FAILED_TTL_SEC:
                return False
            self._failed.pop(no, None)            # 期限切れ。もう一度試す
            self._running.add(no)

        threading.Thread(target=self._run, args=(no, template),
                         name=f"spec-{no}", daemon=True).start()
        return True

    def refresh(self, no: str) -> bool:
        """手元のものを捨てて取り直す(「再取得」ボタン)。"""
        clear(no)
        with self._lock:
            self._failed.pop(no, None)
        return self.prefetch(no)

    def _run(self, no: str, template: str) -> None:
        try:
            result = fetch(no, template)
        finally:
            with self._lock:
                self._running.discard(no)
        if result.state == STATE_ERROR:
            with self._lock:
                self._failed[no] = (time.time(), result)

    # -- 試験用 ------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self._running.clear()
            self._failed.clear()


def _is_stale(meta: dict) -> bool:
    return time.time() - float(meta.get("fetched_at") or 0.0) > CACHE_TTL_SEC


_fetcher: Optional[Fetcher] = None
_fetcher_lock = threading.Lock()


def get_fetcher() -> Fetcher:
    global _fetcher
    with _fetcher_lock:
        if _fetcher is None:
            _fetcher = Fetcher()
        return _fetcher


def reset_fetcher() -> None:
    """試験用。プロセスに1つという前提を壊さずに作り直す。"""
    global _fetcher
    with _fetcher_lock:
        _fetcher = None


def to_dict(status: Status) -> dict:
    """JSONにできる形。"""
    return {
        "no": status.no,
        "state": status.state,
        "message": status.message,
        "content_type": status.content_type,
        "size": status.size,
        "fetched_at": status.fetched_at,
        "source_url": status.source_url,
        # 画面はこれを <img src> に入れるだけでよい
        "image_url": f"/api/spec-sheet/{status.no}/image" if status.ready else "",
    }
