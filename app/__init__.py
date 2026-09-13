"""Flask アプリの組み立て

社内「汎用Webアプリ作成 基盤仕様書」に沿った構成。
起動・停止・監視の詳細は docs/設計.md の §2。

【モードと権限】
モードは **現場** と **資材** の2つ(`packaging_tool/modes.py`)。
発注の「確認済みにする」「取り消し」は資材課だけの操作で、二重に守る。

1. **権限が無い端末には登録しない。** `アクセス権限` マスタが
   `mode:material` を与えていなければ、資材専用のエンドポイントは
   **この プロセスに存在しない**(404)。隠すのではなく、無い
2. **権限があっても、いま資材モードで見ていなければ断る。** 登録された
   うえで要求ごとに現在のモードを確かめ、現場モードなら 403

以前は「どのポートで起動したか」だけで分けていた。守っていたのは
**誰か**ではなく**どのショートカットを押したか**で、同じPCの別の人が
資材のポートを開けば資材の操作ができてしまう。1 が身元による分離、
2 が誤操作の防止で、役割が違うので両方置いている。
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from flask import Flask, g, jsonify, request

from packaging_tool import access_control, app_config, db, modes
from packaging_tool.logging_utils import get_logger

# **起動時の権限で登録するかどうかが決まるモード。**
#
# ここに挙げたモードだけが「あとから権限を足しても開き直すまで効かない」。
# 資材モードの操作は要求のたびに権限を見る形に直してあるので
# (`warehouse.material_only` の `before_request`)、**開き直しは要らない**。
# 設定画面の案内もこの表を見る ── 2か所で持つと、直したのに案内だけが
# 残って「面倒ですよ」と言われる(実際に言われた)
GATED_MODES: tuple[str, ...] = (modes.FIELD,)

log = get_logger("app")

APP_DIR = Path(__file__).resolve().parent

# 起動トークンと同一オリジン確認を要求する経路。
#
# **業務データを返す経路はここに入れる。** `/api/*` だけを見ていたころ、
# Phase 6d で足した `/report/label` `/report/cut-request` が素通しになって
# いました(帳票にはLot番号・品名・板厚・寸法・梱包数が丸ごと入ります)。
# 画面側は `?t=` を付けて開いていたので、書いた側は守られているつもりで、
# テストもトークン付きで叩いていたため気づけませんでした。
#
# 画面のHTML(`/selection` 等)を入れないのは、ブラウザのアドレス欄から
# 開く経路だからです。中身は空の器で、業務データは `/api/*` から取ります。
TOKEN_REQUIRED_PREFIXES = ("/api/", "/report/")

# `/api/*` のうち、起動トークンを要求しないもの。
#
# `/api/health` を素通しにするのは、**まだトークンを知らない相手**が
# 正当に問い合わせる場面があるため:
#   - 多重起動の判定(基盤仕様書 2.4)。後から起動したプロセスは、
#     そのポートに居るのが自分と同じアプリかどうかを知る必要がある
#   - 起動待機画面が、アプリの準備が終わったかを確かめる(同 2.3)
# 返すのは識別情報と状態だけで、業務データは一切含めない。
#
# `/api/alive`(心拍)も素通しにします。返すのは「受け取った」だけで
# 業務データを含みません。トークンを要求すると、**トークンが切れた
# 画面が黙って死んだ扱いになり**、開いているのに終了してしまいます。
TOKEN_EXEMPT_PATHS = frozenset({"/api/health", "/api/alive"})


def create_app(mode: str = modes.FIELD, *,
               token: Optional[str] = None,
               port: Optional[int] = None,
               grant: Optional[access_control.Grant] = None) -> Flask:
    """アプリを1つ組み立てる。

    `mode` は起動時のモード。旧名(`warehouse`)も受ける。
    `token` を省略すると起動ごとに新しく作る。テストからは固定値を渡せる。

    `grant` を渡すと権限の引き直しをしない。本番は省略して
    `アクセス権限` マスタから引く ── 渡せるようにしてあるのは、
    権限ごとに何が登録されるかを試験が確かめられるようにするため。

    **権限が無いモードでは起動しない。** 資材モードで起動しようとして
    権限が無ければ、現場モードへ落として理由を残す ── 起動そのものを
    失敗させると、権限を直す画面(設定)にも辿り着けなくなる。
    """
    requested = modes.normalize(mode)
    if requested not in modes.KEYS:
        raise ValueError(
            f"未知のモード: {mode!r} (使えるのは {', '.join(modes.KEYS)})")

    given_grant = grant
    grant = grant if grant is not None else startup_grant()
    mode_note = ""
    if not grant.allows_mode(requested):
        mode_note = (f"{modes.label(requested)}モードの権限が無いため、"
                     f"{modes.label(modes.DEFAULT)}モードで開いています")
        log.warning("%s (%s)", mode_note, grant.identity.label())
        requested = modes.DEFAULT

    app = Flask(__name__,
                template_folder=str(APP_DIR / "templates"),
                static_folder=str(APP_DIR / "static"))

    app.config.update(
        MODE=requested,
        # 起動時に引いた権限。マスタを取り込み直すと変わるので、
        # 画面を出すたびに引き直す(`current_grant()`)。ここに持つのは
        # **エンドポイントを登録するかどうか**を決めた時点の写し
        STARTUP_GRANT=grant,
        # 呼び手が権限を明示した場合だけ入る。入っていれば毎回の
        # 引き直しもこれを使う(試験が権限ごとの振る舞いを固定できる)
        GIVEN_GRANT=given_grant,
        MODE_NOTE=mode_note,
        # 起動ごとの合言葉。同じPC上の別プロセスや、利用者が偶然開いた
        # 外部のWebページから叩かれないようにする
        TOKEN=token or secrets.token_urlsafe(32),
        PORT=port or app_config.port(requested),
        APP_ID=app_config.app_id(),
        VERSION=app_config.version(),
        DISPLAY_NAME=app_config.display_name(),
        STARTED_AT=time.time(),
        # 起動直後は準備中。重い初期化(スキーマ適用・自動取り込み)が
        # 終わってから True にする。起動待機画面はこれを見て切り替える
        READY=False,
        STAGE="アプリを準備中",
        STAGE_KEY="prepare",
        STARTUP_ERROR="",
        # セッションクッキーは使わないが、Flask の secret_key は
        # flash などが暗黙に要求することがあるので入れておく
        SECRET_KEY=secrets.token_hex(16),
    )

    _register_security(app)
    _register_db(app)
    _register_static_version(app)
    _register_routes(app)

    log.info("create_app: mode=%s port=%s 権限=%s (%s)",
             requested, app.config["PORT"], sorted(grant.codes),
             grant.identity.label())
    return app


# ------------------------------------------------------------------
# 権限
# ------------------------------------------------------------------
def startup_grant() -> access_control.Grant:
    """起動時にこの端末の権限を引く。

    **中身は `access_control` にある。** 起動の入口(`start_app`)が
    Flask を読まずに呼べるようにするためで、ここは今までどおりの
    名前で呼べるようにしているだけ(呼び出し側を書き換えない)。
    """
    return access_control.startup_grant()


def current_grant() -> access_control.Grant:
    """いまの権限。**画面を出すたびに引き直す。**

    マスタを取り込み直せばその場で効く。起動時の写しだけを見ていると、
    権限を直したのにアプリを開き直すまで反映されない。

    ただし**登録済みのエンドポイントは増えない**。起動時に権限が無くて
    登録しなかったものは、権限を足しても開き直すまで 404 のまま
    (設定画面がそう案内する)。
    """
    from flask import current_app

    if "grant" not in g:
        given = current_app.config.get("GIVEN_GRANT")
        g.grant = given if given is not None else access_control.resolve(get_db())
    return g.grant


def current_mode() -> str:
    from flask import current_app
    return current_app.config["MODE"]


def set_mode(mode: str) -> bool:
    """モードを切り替える。権限が無ければ False。

    プロセスに1つの状態。このアプリは**1台のPCを1人が使う**前提なので、
    作業状態(`work_context`)と同じ持ち方にそろえている。

    **断る前に、アクセス権限だけ取り込み元から読み直す。**
    マスタ管理から書けばその場で手元へ追いつくが、それ以外の経路
    (Access側の変換を別途やり直す・別の端末が同時に書く等)では
    手元が取り込み元より遅れて残ることがある。「マスタには正しい行が
    入っているのに切り替わらない」という声は、たいていこれが原因
    (`access_control.resync` の説明を参照)。読み直しても通らないなら、
    行が本当に足りていないということなので、そこで素直に断る。
    """
    from flask import current_app
    key = modes.normalize(mode)
    if key not in modes.KEYS:
        return False
    if not current_grant().allows_mode(key):
        if access_control.resync(get_db()):
            g.pop("grant", None)              # 読み直した内容で引き直す
        if not current_grant().allows_mode(key):
            return False
    current_app.config["MODE"] = key
    log.info("モードを切り替えました: %s", key)
    return True


# ------------------------------------------------------------------
# セキュリティ (設計書 §3.6)
# ------------------------------------------------------------------
def _register_security(app: Flask) -> None:
    @app.before_request
    def _check_request():                       # noqa: ANN202 - Flaskのフック
        # --- Host 検証 (DNSリバインディング対策) ---
        # 攻撃者のドメインを 127.0.0.1 に向けられても、Hostヘッダが
        # 一致しないので弾ける
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            log.warning("Host不一致で拒否: %s", request.host)
            return jsonify(_error("bad_host", "このアドレスからは利用できません")), 400

        if not any(request.path.startswith(p) for p in TOKEN_REQUIRED_PREFIXES):
            return None

        # --- 同一オリジンの確認 ---
        # ブラウザが付ける Fetch Metadata。付いていない場合(古い
        # クライアント・curl)は素通しし、トークンで守る
        fetch_site = request.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            log.warning("別オリジンからの要求を拒否: %s %s", fetch_site, request.path)
            return jsonify(_error("cross_origin", "別のページからは利用できません")), 403

        # --- 起動トークン ---
        if request.path in TOKEN_EXEMPT_PATHS:
            return None
        supplied = (request.headers.get("X-Tool-Token")
                    or request.args.get("t", ""))
        # バイト列で比べる。`compare_digest` に str を渡すと非ASCIIで
        # TypeError になり、**500 を返してしまう**(送られた値は誰にでも
        # 決められるので、素直に 401 を返さなければならない)
        if not secrets.compare_digest(supplied.encode("utf-8"),
                                      app.config["TOKEN"].encode("utf-8")):
            log.warning("トークン不一致で拒否: %s", request.path)
            return jsonify(_error(
                "bad_token",
                "この画面は無効になりました。アプリを開き直してください")), 401
        return None

    @app.after_request
    def _headers(response):                     # noqa: ANN202 - Flaskのフック
        # CORS ヘッダは**一切返さない**(返さないことが対策)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        _apply_cache_policy(response)
        return response


# 静的ファイルを控えておいてよい期間(秒)。URLに版が入っているので、
# 入れ替えれば URL が変わり、**必ず取り直される**
STATIC_MAX_AGE = 7 * 24 * 60 * 60


def _apply_cache_policy(response) -> None:
    """何を控えてよくて、何を控えてはいけないか。

    【なぜここを明示するのか】
    **「アプリを入れ替えたのに古いまま」の正体がここでした。**
    以前は `/api/` にだけ `no-store` を付けていて、**画面のHTMLには
    キャッシュの指示が1つもありませんでした**。指示が無いHTMLは、
    ブラウザが自分の判断で控えます(発見的キャッシュ・戻る操作・
    `nav.js` の取得)。版のバッジはそのHTMLの中にあるので、
    入れ替えても古い版が出続けます。

    分け方は1行で言えます:

    - **版がURLに入っているもの(静的ファイル)は、長く控えてよい。**
      入れ替えればURLが変わるので、古いものが出ることはない
    - **それ以外は控えない。** 画面のHTMLも、APIの応答も、
      いま作ったものを毎回渡す

    【版が入るのは入口だけ、という落とし穴】
    版を付けるのは `url_for('static', ...)`(`_register_static_version`)で、
    それが効くのは**テンプレートが名指しするファイルだけ**です。
    その中の

        import * as mapedit from "../mapedit.js";

    は版の付かない素のURLで取りに行きます。ここに `immutable` を
    付けると、ブラウザは**再確認すらしません** ── 入れ替えても
    共有モジュールだけが何日も古いまま残り、

        dragger?.clearSelection is not a function

    のように「入口は新しいのに、その中身が古い」形で壊れます
    (現場で実際に踏みました)。版が入っていないものは
    `no-cache`(=使う前に必ず確かめる)にします。ETag が付いているので
    中身が同じなら 304 が返るだけで、手元のサーバでは事実上ただです。
    """
    if request.path.startswith("/static/"):
        if request.args.get("v"):
            response.headers["Cache-Control"] = (
                f"public, max-age={STATIC_MAX_AGE}, immutable")
        else:
            response.headers["Cache-Control"] = "no-cache"
        return
    response.headers["Cache-Control"] = "no-store"
    # 発見的キャッシュを使う古いブラウザ向け。`no-store` を読まない
    # 実装でも、この2つがあれば控えない
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def _error(code: str, message: str, field: str = "") -> dict:
    """エラー応答の形(設計書 §6.1)。文言はサーバが持つ。"""
    body = {"code": code, "message": message}
    if field:
        body["field"] = field
    return {"error": body}


# ------------------------------------------------------------------
# DB接続 (設計書 §3.5)
# ------------------------------------------------------------------
# **書く要求は1つずつ通す。**
#
# waitress はスレッドプールで動くので、要求は同時に走る。読むだけなら
# WAL があるので困らないが、書くほうが重なると次のことが起きる:
#
#   ・作業状態(`*_session` / `work_context`)はプロセスに1つしかない。
#     2つの要求が同時に書き換えると、片方の結果がもう片方に混ざる
#   ・同じ行を2か所から書くと、後から書いたほうが黙って勝つ
#   ・sqlite3 の書き込みロックに当たると `database is locked` で断られる
#
# マスタを画面から直せるようになって(VER2.1.0)、書く口が増えた。
# **通し方は単純でよい** ── この道具は1台のPCを1人が使う前提なので、
# 書く要求が重なるのは押し間違いか二重送信で、待たせても誰も困らない。
_WRITE_LOCK = threading.RLock()

# 直列化しないもの。**待たせてはいけない**種類の POST。
#   /api/jobs      … 進捗を見に行くだけ(長い処理の最中に呼ばれる)
#   /api/mode      … 画面の切り替え。DBを書かない
# 前方一致で見る
#   /api/alive     … 心拍。20秒ごとに来るので、待たせると自動終了が誤る
_NO_LOCK_PREFIXES = ("/api/jobs", "/api/mode", "/api/log", "/api/alive")


def _is_write(req) -> bool:
    """その要求は「書く」か。**方法だけで決める** ── 経路ごとの表を
    持つと、画面を1つ足すたびに更新が要り、忘れたぶんだけ穴が開く。
    """
    if req.method in ("GET", "HEAD", "OPTIONS"):
        return False
    return not req.path.startswith(_NO_LOCK_PREFIXES)


def _register_db(app: Flask) -> None:
    """リクエストごとに1本開いて、終わったら閉じる。

    waitress はスレッドプールで動くため、`sqlite3` の接続を
    プロセス全体で共有できない(既定で `check_same_thread=True`)。
    WALは設定済みなので読み取りは書き込みとぶつからない。

    加えて、**書く要求は1つずつ通す**(上の `_WRITE_LOCK`)。
    """

    @app.before_request
    def _take_write_lock():                     # noqa: ANN202 - Flaskのフック
        if _is_write(request):
            _WRITE_LOCK.acquire()
            g.holds_write_lock = True

    @app.teardown_request
    def _release_write_lock(_exc):              # noqa: ANN202 - Flaskのフック
        # **接続を閉じるより先に放す。** `teardown_request` は
        # `teardown_appcontext` より前に呼ばれる
        if g.pop("holds_write_lock", False):
            _WRITE_LOCK.release()

    @app.teardown_appcontext
    def _close_db(_exc):                        # noqa: ANN202 - Flaskのフック
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()


def _register_static_version(app: Flask) -> None:
    """CSS/JS の URL に版を付ける。

    **入れ替えたら必ず取り直させる。** 配布はフォルダごとコピーなので、
    ファイル名は版が上がっても変わりません。ブラウザは同じURLの控えを
    持っているので、`Cache-Control` を無視する場面(戻る操作・
    オフライン復帰・企業のプロキシ)では**古い CSS が出続けます**。
    「アプリを入れ替えたのに見た目が古いまま」の正体がこれです。

    版は `config/app.json` ただ1つが出どころなので、**上げ忘れなければ
    必ず変わります**(上げ方は docs/変更履歴.md)。
    """
    stamp = app.config["VERSION"]

    @app.url_defaults
    def _stamp(endpoint, values):               # noqa: ANN202 - Flaskのフック
        if endpoint == "static" and "v" not in values:
            values["v"] = stamp


def get_db():
    """このリクエスト用のDB接続。`app` の外からは呼ばない。"""
    if "db" not in g:
        g.db = db.get_connection()
    return g.db


# ------------------------------------------------------------------
# ルーティング
# ------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    # このモジュールの `log` はロガーなので、画面のモジュールは別名で入れる
    from .routes import (catalog, health, inventory, layout, lot, master,
                         pending, selection, settings, spec_sheet, warehouse)
    from .routes import log as log_routes

    app.register_blueprint(health.bp)
    # 部品カタログ。利用者向けではなく開発を進めるための見本帳
    app.register_blueprint(catalog.bp)
    # 設定画面は**どのモードでも**開ける。取り込みと書き戻し、そして
    # 「なぜこのモードしか選べないのか」を確かめる場所になる
    app.register_blueprint(settings.bp)
    # マスタ管理は設定画面の中にある。**どのモードでも登録する** ──
    # 直せるかどうかはアクセス権限マスタの中身で決まり、その中身は
    # 動いている最中に変わる(この画面から入れられる)。起動時に決めて
    # しまうと食い違うので、要求のたびに見る(`app/routes/master.py`)
    app.register_blueprint(master.bp)
    # 発注の**一覧**は現場も資材も見る(現場は送った分、資材は受けた分)。
    # 出す・取り消すのは現場だけ、確認するのは資材だけなので、それぞれ
    # 別のブループリントに分けて `before_request` で断る
    app.register_blueprint(warehouse.bp)
    # **常に登録する。** いま現場モードかどうかは
    # `warehouse.field_only` の `before_request` が要求のたびに確かめる
    # (`mode:field` は誰でも持つ既定の権限なので、権限では分けない)
    app.register_blueprint(warehouse.field_only)

    grant: access_control.Grant = app.config["STARTUP_GRANT"]

    # 現場の画面は、現場モードを持っていれば登録する。**モードを
    # 切り替えられる**ので、いま資材モードで開いていても URL は要る
    # (レールに出すかどうかは `shell.nav_items` がモードで決める)
    if grant.allows_mode(modes.FIELD):
        app.register_blueprint(log_routes.bp)
        app.register_blueprint(lot.bp)
        app.register_blueprint(selection.bp)
        # 包装仕様書の図面はロット検索の中でしか使わない
        app.register_blueprint(spec_sheet.bp)
        app.register_blueprint(inventory.bp)
        app.register_blueprint(layout.bp)
    else:
        log.info("現場モードの権限が無いため、現場の画面は登録しません")

    if grant.allows_mode(modes.MATERIAL):
        log.info("資材モードの権限あり: 確認のエンドポイントを登録します")
    else:
        log.info("資材モードの権限が無い状態で起動しています(確認の操作は "
                "404になります。マスタ管理で権限を足せば、開き直さずに "
                "その場で使えるようになります)")
    # **常に登録する。** 権限の有無・いま資材モードかどうかは
    # `warehouse.material_only` の `before_request` が要求のたびに
    # 確かめる(`master.py` のマスタ管理と同じ形)。以前は起動時の
    # 権限だけで登録するかどうかを決めていたが、それだと権限を
    # あとから足しても、サーバプロセスを終了して起動し直すまで
    # 反映されなかった
    app.register_blueprint(warehouse.material_only)

    # レールに出ているのに中身がまだ無い画面へ、「準備中」の案内を置く。
    # **業務の画面を全部登録したあと**に呼ぶ(すでに実装済みのものを
    # 上書きしないよう、`shell.READY_SCREENS` を唯一の出どころにしている)
    pending.register(app)

    @app.errorhandler(404)
    def _not_found(_e):                         # noqa: ANN202 - Flaskのフック
        if request.path.startswith("/api/"):
            return jsonify(_error("not_found", "その操作はこのモードでは使えません")), 404
        return jsonify(_error("not_found", "ページが見つかりません")), 404
