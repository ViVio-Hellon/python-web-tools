"""起動確認・生存監視・停止 (基盤仕様書 2.3 / 2.8 / 2.9)

- `GET  /`            … 準備中なら起動待機画面、終わっていればアプリ本体へ
- `GET  /api/health`  … 起動確認と生存監視。**トークン不要**
- `POST /api/shutdown`… 安全な停止。トークン必須
- `POST /api/mode`    … モードの切替。権限のあるモードだけ
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from flask import Blueprint, current_app, jsonify, redirect, request

from packaging_tool import app_config, modes, screen_lock
from packaging_tool.logging_utils import get_logger

from .. import shell

log = get_logger("app.routes.health")

bp = Blueprint("health", __name__)

# 停止要求を受けてから実際に落とすまでの猶予(秒)。
# 応答を返しきる前にサーバを止めると、利用者側は「押したのに
# 何も起きなかった」ように見えるため、少し待ってから落とす。
SHUTDOWN_DELAY_SEC = 0.4

# `POST /api/shutdown` が呼ばれたときに実行する後始末。
# `server.py` が起動時に差し込む(ここが直接 waitress を知らないようにする)。
_shutdown_hook = None


def set_shutdown_hook(func) -> None:
    """サーバの止め方を登録する。"""
    global _shutdown_hook
    _shutdown_hook = func


@bp.get("/")
def index():
    """準備が終わるまでは起動待機画面を出す。

    基盤仕様書 2.3 は「ブラウザーだけが先に開き、接続エラーや未完成の
    画面が表示されることを防ぐ」ことを求めている。サーバ自身が待機画面を
    出すことで、**起動直後でも必ず何かが表示される**状態を作る。
    """
    if not current_app.config["READY"]:
        # **骨格は `boot_screen` に1つだけ。** 起動サーバ(Flask が
        # 読み込まれる前に出すほう)と同じものを出す ── 2枚持つと、
        # 直したほうと直していないほうが場面によって出る
        from packaging_tool import boot_screen

        return boot_screen.render(
            display_name=current_app.config["DISPLAY_NAME"],
            # 起動中も版が読める。帯がまだ出ていない場面なので、
            # ここに出さないと「どれが入っているか」を確かめる先が無くなる
            version_label=app_config.version_label(),
            token=current_app.config["TOKEN"],
            app_id=current_app.config["APP_ID"],
            poll_ms=app_config.job_poll_ms(),
            # 待たずに入る道。取り込みは背景で続き、帯に出る
            home_url=shell.home_url(current_app.config["MODE"]),
        )
    # 準備ができたら業務画面へ送る。
    # ここで止まる画面を出すと、`Start.vbs` から起動した利用者が
    # **アプリに入れない**(レールが無いので行き先が分からない)。
    # 実際にそうなっていた
    return redirect(shell.home_url(current_app.config["MODE"]), code=302)


@bp.get("/favicon.ico")
def favicon():
    """タブのアイコン。**中身なし(204)で返す。**

    画面(base.html)はモードの色のアイコンを埋め込んでいるが、帳票・
    起動待機・カタログのような単独のページは持たない。そのときブラウザは
    `/favicon.ico` を取りに来て、以前は 404 が返り、帳票を開くたびに
    エラーが1行記録されていた(通しの試験で見つけた)。
    """
    return "", 204


@bp.get("/api/health")
def health():
    """起動確認と生存監視。

    **`app_id` を返すのが要点**。同じポートを別のアプリが使っていても、
    HTTPが返るだけでは「自分と同じアプリが起動している」とは言えない
    (基盤仕様書 2.3)。多重起動の判定はこの値の一致で行う。

    業務データは含めないので、トークン無しで答えてよい。
    """
    config = current_app.config
    return jsonify({
        "app_id": config["APP_ID"],
        "display_name": config["DISPLAY_NAME"],
        "version": config["VERSION"],
        # **どのフォルダの、どの版が動いているか。**
        # 「入れ替えたのに古いまま」を調べるとき、これが無いと
        # 端末に行って確かめることになる(古いプロセスが残っている
        # のか、別のフォルダを起動しているのかが区別できない)
        "app_root": str(app_config.APP_ROOT),
        "mode": config["MODE"],
        "port": config["PORT"],
        "pid": os.getpid(),
        "ready": bool(config["READY"]),
        "stage": config["STAGE"],
        # どの段に居るか。**待機画面が段を組み立て直さない**ための鍵
        # (文言だけ渡すと、画面側が文字列から段を推し量ることになる)
        "stage_key": config.get("STAGE_KEY", "prepare"),
        "startup_error": config["STARTUP_ERROR"],
        "uptime_sec": round(time.time() - config["STARTED_AT"], 1),
        # **そのタブがまだ操作できるか。**
        #
        # 譲ったことを古いタブへ伝える口がここです(`screen_lock`)。
        # 断りは押したときにも返りますが、それだけだと**押すまで
        # 分かりません** ── 古いタブは古いLotを出したまま待っていて、
        # 押した1回目が「効かない操作」になります。心拍は15秒ごとに
        # 来るので、押す前に覆いが出せます。
        #
        # ここ自体は素通し(`SCREEN_EXEMPT_PATHS`)。断ってしまうと
        # この知らせが届きません。
        "screen_ok": screen_lock.is_active(
            request.headers.get(screen_lock.HEADER, "")),
        # **その画面がいま使えるモード。** 帯のモード切替は画面を出すとき
        # にしか作られないので、権限があとから増えても、画面を移るまで
        # 「現場?」のままだった(現場の声:「触ることで 現場・資材 に
        # なった」)。見張りがこれと帯の `data-modes` を比べ、食い違ったら
        # 帯だけ描き直す。画面からの問い合わせにだけ答える ── 起動の
        # 判定や待機画面は権限を要らないので、DBを開かせない
        "modes": _screen_modes(),
        # **保存していない図。** 画面がこれを覚えておき、タブを閉じる
        # ときに「保存していない変更があります」を出す。閉じると8秒で
        # 自動終了し、そこで**未保存の配置編集は消える**ので、黙って
        # 閉じさせない(現場の声:「配置編集保存されてないよ」)。
        # どの画面にいても分かるよう、棚検索・簡易在庫の両方を返す
        "unsaved": _screen_unsaved(),
        # **管理者認証の世代。** 画面が開いたときの世代と違えば、認証に
        # 関わるところ(マスタ管理の「直せる/直せない」など)を描き直す
        # (`auth_state` の説明。現場の声:「認証系はすぐにどこにでも反映」)
        "auth": _screen_auth(),
        # いま走っているものの一言。**起動待機画面はこれを読む** ──
        # 取り込みが終わるまで待たせるので、何をどこまでやっているかを
        # 出さないと「止まっている」と受け取られる
        "job": _running_note(),
    })


def _screen_modes() -> Optional[list[str]]:
    """画面からの問い合わせなら、いま使えるモード。それ以外は `None`。

    **権限の引き方は画面を出すときと同じ**(`current_grant` =
    `access_control.resolve_for_screen`)。足りなければ取り込み元を
    見に行くので、資材課が行を足せば、画面に触らなくてもここで拾える。
    取り込み元を叩く回数は向こうが抑えている(更新時刻が変わったとき
    と、30秒おき)。

    準備が終わっていない・DBを開けないときは答えない ── 見張りは
    「分からない」を「変わった」と取り違えないよう、`None` なら何もしない。
    """
    if not request.headers.get(screen_lock.HEADER):
        return None
    if not current_app.config.get("READY"):
        return None
    try:
        from .. import current_grant
        return list(current_grant().allowed_modes())
    except Exception:                       # noqa: BLE001 - 見張りは止めない
        log.debug("モードを引けませんでした(次の問い合わせで試します)",
                  exc_info=True)
        return None


def unsaved_edits() -> dict[str, str]:
    """保存していない図 {鍵: 呼び名}。**作業状態を作らずに**調べる。

    見張りと「終了」の2か所が聞くので、ここ1か所で持つ。
    """
    from packaging_tool import layout_session, pallet_map_session

    out = {}
    if layout_session.has_unsaved():
        out["layout"] = layout_session.UNSAVED_LABEL
    if pallet_map_session.has_unsaved():
        out["inventory"] = pallet_map_session.UNSAVED_LABEL
    return out


def _screen_auth() -> Optional[dict]:
    """画面からの問い合わせなら、管理者認証の状態と世代。"""
    if not request.headers.get(screen_lock.HEADER):
        return None
    from packaging_tool import auth_state
    return auth_state.snapshot()


def _screen_unsaved() -> Optional[dict[str, str]]:
    """画面からの問い合わせにだけ答える(`_screen_modes` と同じ理由)。"""
    if not request.headers.get(screen_lock.HEADER):
        return None
    return unsaved_edits()


def _running_note() -> Optional[dict]:
    """走っている処理の、待機画面に出すぶんだけ。"""
    from packaging_tool import jobs

    running = jobs.get_registry().running()
    if running is None:
        return None
    return {"label": running.label, "pct": running.pct,
            "message": running.message}


@bp.post("/api/mode")
def switch_mode():
    """モードを切り替える。

    **権限のあるモードだけ。** 画面は権限のあるものしか出さないが、
    要求を直接投げられても通らないよう、ここでも確かめる(設計書 §1 の6番、
    「一覧に無いものは選べない」)。

    切り替わると出す画面が変わるので、行き先(`next`)も返す。いまの画面が
    切替先に無いことがある ── 資材モードに「資材選択」は無い。
    """
    from .. import set_mode

    body = request.get_json(silent=True) or {}
    requested = modes.normalize(str(body.get("mode", "")))
    if requested not in modes.KEYS:
        return jsonify({"error": {"code": "bad_mode",
                                  "message": "そのモードはありません。"}}), 400
    if requested == current_app.config["MODE"]:
        return jsonify({"ok": True, "mode": requested,
                        "next": "", "message": ""})
    if not set_mode(requested):
        return jsonify({"error": {
            "code": "not_allowed",
            "message": (f"{modes.label(requested)}モードの権限がありません。"
                        "設定画面で登録状況を確認してください。")}}), 403
    return jsonify({
        "ok": True,
        "mode": requested,
        "next": shell.home_url(requested),
        "message": f"{modes.label(requested)}モードに切り替えました",
    })


@bp.get("/api/jobs")
def jobs():
    """長時間処理の状態(監視レベル2)。

    取り込み・書き戻しの進捗はここで取る。ブラウザを閉じて開き直しても
    ここを読めば状態が戻せる。走っているものが無くても直近の結果を返す
    ので、「さっきの取り込みはどうなったのか」が分かる。
    """
    from packaging_tool.presenters import settings as settings_presenter
    return jsonify(settings_presenter.jobs_dict())


@bp.post("/api/alive")
def alive():
    """画面が生きていることの心拍(基盤仕様書 2.8「自動終了」)。

    **トークンは要らない。** ここで返すのは「受け取った」だけで、
    業務データは1つも含みません。心拍にトークンを要求すると、
    トークンが切れた画面が黙って死んだ扱いになります。

    `leaving=true` はタブを閉じた合図(`sendBeacon`)。猶予のあとで
    終わりますが、そのあいだに心拍が戻れば取り消されます。
    `visible=false` は裏に回った合図。ブラウザは裏のタブのタイマーを間引く
    (止める)ので、そう言った画面は心拍が途切れても落としません。
    `resumed=true` は表に戻った / スリープから戻った合図(`gap_ms` は止まっていた長さ)。

    **閉じたタブは「いま使っている画面」も手放します。** 番号は本文で
    受け取ります ── `sendBeacon` にはヘッダを付けられないからです。
    手放すことで、残ったタブが読み込み直さずに操作へ戻れます。
    """
    from packaging_tool import idle_exit

    body = request.get_json(silent=True) or {}
    if body.get("leaving"):
        screen_lock.release(str(body.get("screen_id") or ""))

    watch = idle_exit.get()
    if watch is None:
        return jsonify({"ok": True, "watching": False})

    # **画面ごとに見る。** 2つ開いているうちの1つを閉じても、もう1つは生きている
    screen = str(body.get("screen_id") or "")
    if body.get("leaving"):
        watch.leaving(screen)
    elif body.get("resumed"):
        # 表に戻った / スリープから戻った(裏のまま気づいたなら裏のまま)
        watch.resumed(screen, gap_sec=_number(body.get("gap_ms")) / 1000,
                      visible=body.get("visible") is not False)
    elif body.get("visible") is False:
        # 裏に回った。ブラウザが心拍を間引いても落とさない
        watch.hidden(screen)
    else:
        watch.beat(screen, visible=True if body.get("visible") else None)
    return jsonify({"ok": True, "watching": True})


def _number(value) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


@bp.post("/api/shutdown")
def shutdown():
    """安全な停止(基盤仕様書 2.8)。

    実行中の長時間処理があるときは、**止めずに理由を返す**。
    利用者に「中断して終了 / 完了を待つ / やめる」を選ばせるため。
    `force=true` を付けて呼び直すと中断して落とす。
    """
    force = bool((request.get_json(silent=True) or {}).get("force"))
    # **保存していない図があれば、捨てる前に聞く。** 以前は何も言わずに
    # 終わり、配置編集が消えていた。勝手に保存はしない ── 「保存を押す
    # まで書かない。間違えても開き直せば元に戻る」という約束があるので
    unsaved = unsaved_edits()
    running = _running_jobs()
    if unsaved and not force:
        # 実行中の処理もあれば**同じ1問で**聞く。「はい」は force で
        # 呼び直すので、別々に聞くと2つ目を聞かずに中断してしまう
        names = "・".join(unsaved.values())
        message = f"保存していない変更があります({names})。"
        if running:
            message += "実行中の処理もあります。"
        return jsonify({
            "stopped": False,
            "reason": "unsaved",
            "unsaved": unsaved,
            "running": running,
            "message": message + (
                "保存せず、処理も中断して終了しますか?" if running
                else "保存せずに終了しますか?"),
        }), 409
    if running and not force:
        return jsonify({
            "stopped": False,
            "reason": "busy",
            "running": running,
            "message": "実行中の処理があります。中断して終了しますか?",
        }), 409

    if _shutdown_hook is None:
        # サーバ抜きで組み立てた場合(テストなど)
        return jsonify({"stopped": False, "reason": "no_hook",
                        "message": "このプロセスは停止操作に対応していません"}), 501

    log.info("停止要求を受け付けました (force=%s)", force)
    threading.Timer(SHUTDOWN_DELAY_SEC, _shutdown_hook).start()
    return jsonify({"stopped": True, "message": "終了します"})


def _running_jobs() -> list[str]:
    """いま走っている長時間処理の名前。

    取り込みの途中で落とすと、DBが中途半端な状態で残る。
    止める前に必ずここを見る(基盤仕様書 2.8)。
    """
    from packaging_tool import jobs as job_registry
    return job_registry.get_registry().busy_labels()
