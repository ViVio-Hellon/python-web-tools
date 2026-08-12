"""画面とAPIの試験で毎回使う土台

【なぜまとめるか】
`test_web_*.py` は6つあり、どれも同じ3つを組んでいた。

    1. 手元のDBを `:memory:` で作ってスキーマを当てる
    2. その接続を掴ませるため `routes.get_db` を差し替える
    3. `create_app()` して `test_client()` を取る

2つ目が厄介で、**元に戻し忘れると次の試験が閉じた接続を掴む**。
症状は「別のファイルの試験だけが落ちる」なので、原因に辿り着くまでが遠い。
戻す約束をここ1か所に閉じ込めて、呼ぶ側が忘れられないようにする。

**判断は何もしません。** 組み立ての手順を短く書けるようにするだけです。
"""
from __future__ import annotations

import sqlite3
import unittest
from typing import Any, Optional

from packaging_tool import db

# 試験用の起動トークン。**本物と同じ経路を通す**ために必ず付ける
TOKEN = "test-token-abc123"


def memory_db() -> sqlite3.Connection:
    """スキーマを当てた手元のDB。1件の試験のあいだだけ生きる。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def bind_db(case: unittest.TestCase, routes: Any,
            conn: Optional[sqlite3.Connection] = None) -> sqlite3.Connection:
    """`routes.get_db` にこの試験の接続を掴ませる。**戻すのはここが約束する。**

    `case.addCleanup` に閉じるところまで積むので、呼ぶ側は受け取った接続を
    使うだけでよい。
    """
    conn = conn if conn is not None else memory_db()
    case.addCleanup(conn.close)
    original = routes.get_db
    routes.get_db = lambda: conn
    case.addCleanup(setattr, routes, "get_db", original)
    return conn


def make_client(mode: str = "field", *, port: int = 8713, ready: bool = True,
                grant: Any = None):
    """トークン付きで叩ける `test_client`。

    `grant` を渡せる形にしてあるのは、**開発機の `アクセス権限` マスタの
    中身で試験の結果が変わってはいけない**ため。
    """
    from app import create_app

    app = create_app(mode, token=TOKEN, port=port, grant=grant)
    app.config["TESTING"] = True
    if ready:
        app.config["READY"] = True
    return app.test_client()


def auth() -> dict[str, str]:
    """トークンを載せるヘッダ。"""
    return {"X-Tool-Token": TOKEN}
