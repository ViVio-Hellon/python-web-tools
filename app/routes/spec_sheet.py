"""包装仕様書の図面 (VBAには無かった。閲覧システムへの手作業を畳む)

ロットが見つかった時点で裏で取りに行き、ロット検索の画面にそのまま出す。
判断も取得もサーバ側。画面は状態を見て出し分けるだけ。

【なぜ状態を返すAPIと画像を返すAPIに分けたか】
`<img src>` は失敗しても「なぜ失敗したか」を画面に渡せない。
状態(`GET /api/spec-sheet/<no>`)を先に問い合わせて、出せると分かってから
`<img>` を作る。取れなかった理由は日本語で画面に出る。
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify

from packaging_tool import spec_sheet

bp = Blueprint("spec_sheet", __name__)


@bp.get("/api/spec-sheet/<no>")
def status(no: str):
    """いまの状態を返す。**ついでに裏で取り始める**。

    画面から見ると「聞いたら取りに行っていた」になる。取得の開始を
    別のAPIにすると、画面が2回投げることになるだけで得が無い。
    """
    if not spec_sheet.is_valid_no(no):
        return _bad_no(no)
    fetcher = spec_sheet.get_fetcher()
    fetcher.prefetch(no)
    return jsonify(spec_sheet.to_dict(fetcher.status(no)))


@bp.post("/api/spec-sheet/<no>/refresh")
def refresh(no: str):
    """手元のものを捨てて取り直す。

    一度失敗した仕様NOは覚えていて叩き直さない(届かないサーバを
    ロットのたびに叩かないため)ので、それを解くのもここ。
    """
    if not spec_sheet.is_valid_no(no):
        return _bad_no(no)
    fetcher = spec_sheet.get_fetcher()
    fetcher.refresh(no)
    return jsonify(spec_sheet.to_dict(fetcher.status(no)))


@bp.get("/api/spec-sheet/<no>/image")
def image(no: str):
    """図面そのもの。手元に無ければ404。

    404は失敗ではなく「まだ」。画面は状態APIで出せると分かってから
    ここを呼ぶので、通常の流れでは起こらない。
    """
    if not spec_sheet.is_valid_no(no):
        return _bad_no(no)
    cached = spec_sheet.read_cached(no)
    if cached is None:
        return jsonify({"error": {
            "code": "not_ready",
            "message": "図面がまだ手元にありません"}}), 404

    payload, content_type = cached
    response = Response(payload, mimetype=content_type or "application/octet-stream")
    # 社内システムから来たものをそのまま返すので、ブラウザに
    # 中身を推測させない。SVGが混ざっても `<img>` の中で終わる
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response.headers["Content-Disposition"] = f'inline; filename="{no}"'
    # キャッシュ指定はここで書かない。`app/__init__.py` の `_headers` が
    # `/api/` のすべてに `no-store` を付ける。出どころは手元のディスクなので
    # 毎回取り直しても速く、「再取得」が確実に効くほうが大事
    return response


def _bad_no(no: str):
    """打ち間違いは400。業務上の断りではなく入力の形の誤り。"""
    return jsonify({"error": {
        "code": "bad_no",
        "message": f"包装仕様NOの形が正しくありません: {no}"}}), 400
