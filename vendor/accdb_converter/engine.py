# -*- coding: utf-8 -*-
"""
engine.py
Access(.accdb/.mdb) の全テーブルを読み取り、共通中間形式へ変換する。

中間形式:
    tables = [
        {"name": "テーブル名", "columns": ["col1", ...], "rows": [(v1, ...), ...]},
        ...
    ]

読み取りエンジンは 2 系統を自動選択（どちらか一方が使えれば動作）:
  1) pyodbc + Microsoft Access Driver (ACE)   … 最も忠実。導入済みなら優先。
  2) access_parser (純Python)                 … オフライン用フォールバック。
"""

import os


def available_engines():
    """このPCで使えるエンジンを調べて返す（診断/起動時表示用）。"""
    engines = []
    try:
        import pyodbc  # noqa: F401
        # ドライバ自体の有無も確認
        try:
            drivers = [d for d in pyodbc.drivers()
                       if "Access Driver" in d]
        except Exception:
            drivers = []
        engines.append(("pyodbc", bool(drivers), drivers))
    except Exception:
        engines.append(("pyodbc", False, []))
    try:
        import access_parser  # noqa: F401
        engines.append(("access_parser", True, []))
    except Exception:
        engines.append(("access_parser", False, []))
    return engines


# ---------------------------------------------------------------------------
# エンジン 1: pyodbc + Microsoft Access Driver (ACE)
# ---------------------------------------------------------------------------
def _read_with_pyodbc(path):
    import pyodbc

    conn_str = (
        r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};"
        r"DBQ=%s;" % path
    )
    conn = pyodbc.connect(conn_str, autocommit=True)
    try:
        cur = conn.cursor()
        table_names = []
        for row in cur.tables(tableType="TABLE"):
            name = row.table_name
            if name and not name.startswith("MSys"):
                table_names.append(name)

        tables = []
        for t in table_names:
            cur.execute("SELECT * FROM [%s]" % t)
            columns = [d[0] for d in cur.description]
            rows = [tuple(r) for r in cur.fetchall()]
            tables.append({"name": t, "columns": columns, "rows": rows})
        return tables
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# エンジン 2: access_parser (純Python)
# ---------------------------------------------------------------------------
def _read_with_access_parser(path):
    import jet_text_fix; jet_text_fix.apply()  
    from access_parser import AccessParser

    db = AccessParser(path)
    tables = []
    for name in list(db.catalog.keys()):
        try:
            parsed = db.parse_table(name)
        except Exception:
            parsed = {}

        if not parsed:
            tables.append({"name": name, "columns": [], "rows": []})
            continue

        columns = list(parsed.keys())
        n = max((len(v) for v in parsed.values() if isinstance(v, list)), default=0)
        rows = []
        for i in range(n):
            row = []
            for c in columns:
                col = parsed[c]
                if isinstance(col, list):
                    row.append(col[i] if i < len(col) else None)
                else:
                    row.append(col if i == 0 else None)
            rows.append(tuple(row))
        tables.append({"name": name, "columns": columns, "rows": rows})
    return tables


# ---------------------------------------------------------------------------
# 公開関数
# ---------------------------------------------------------------------------
def read_access(path, prefer="auto"):
    """
    Access ファイルを読み取り (tables, engine_name) を返す。
    prefer: "auto" / "pyodbc" / "access_parser"
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    errors = []

    if prefer in ("auto", "pyodbc"):
        try:
            return _read_with_pyodbc(path), "pyodbc (Microsoft Access Driver)"
        except Exception as e:
            errors.append("pyodbc: %s" % e)
            if prefer == "pyodbc":
                raise

    if prefer in ("auto", "access_parser"):
        try:
            return _read_with_access_parser(path), "access_parser (pure python)"
        except Exception as e:
            errors.append("access_parser: %s" % e)

    # ここに来たら両方失敗
    hint = (
        "どちらの読み取りエンジンも使用できませんでした。\n"
        "対処:\n"
        " (A) pyodbc 経路: Microsoft Access Database Engine (ACE) を導入すると\n"
        "     pyodbc で最も忠実に読めます。\n"
        " (B) access_parser 経路: access_parser / construct / tabulate を\n"
        "     オフラインwheelで導入するとドライバ不要で読めます。\n"
        "詳細:\n" + "\n".join(errors)
    )
    raise RuntimeError(hint)


# ---------------------------------------------------------------------------
# 追加リーダー: xlsx / sqlite （相互変換のための入力対応）
# ---------------------------------------------------------------------------
def read_xlsx(path):
    """
    .xlsx を読み取り中間形式へ。1シート = 1テーブル、1行目 = 列名。

    openpyxl があればそちらを使い、無ければ標準ライブラリだけの
    xlsx_reader へ切り替える(ラインPCに追加ライブラリを入れられない場合)。
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        import xlsx_reader
        read_xlsx.engine_used = "標準ライブラリ"
        return xlsx_reader.read_xlsx(path)

    read_xlsx.engine_used = "openpyxl"

    wb = load_workbook(path, read_only=True, data_only=True)
    tables = []
    for ws in wb.worksheets:
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = next(rows_iter)
        except StopIteration:
            tables.append({"name": ws.title, "columns": [], "rows": []})
            continue

        # 末尾の空セル列を切り詰め、列名を確定
        columns = []
        for i, h in enumerate(header):
            columns.append(str(h) if h is not None else "col%d" % (i + 1))
        ncol = len(columns)

        data_rows = []
        for r in rows_iter:
            # 完全な空行はスキップ
            if r is None:
                continue
            if all(v is None for v in r):
                continue
            r = list(r)
            if len(r) < ncol:
                r += [None] * (ncol - len(r))
            elif len(r) > ncol:
                r = r[:ncol]
            data_rows.append(tuple(r))

        tables.append({"name": ws.title, "columns": columns, "rows": data_rows})
    wb.close()
    return tables


def _decode_sqlite_text(raw):
    """
    SQLite の TEXT を可能な限り文字列にする。

    既定の sqlite3 は UTF-8 以外のバイト列に当たると
    「Could not decode to UTF-8 column ...」で読み取り全体を止めてしまう。
    Access 由来のデータには cp932 のまま入った値が混ざることがあるため、
    UTF-8 -> cp932 の順に試し、最後は文字を置き換えてでも読み進める。
    """
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def read_sqlite(path):
    """
    .sqlite3 / .db を読み取り中間形式へ。sqlite_master の全テーブルを対象。
    """
    import sqlite3

    with open(path, "rb") as fp:
        if fp.read(16) != b"SQLite format 3\x00":
            raise ValueError(
                "SQLite3 ファイルではありません（先頭の識別子が違います）。"
                "別の形式のファイルを .sqlite3 という名前で保存していないか"
                "確認してください。")

    con = sqlite3.connect(path)
    con.text_factory = _decode_sqlite_text
    try:
        cur = con.cursor()
        names = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables = []
        for n in names:
            qn = '"' + n.replace('"', '""') + '"'
            columns = [c[1] for c in cur.execute("PRAGMA table_info(%s)" % qn)]
            rows = [tuple(r) for r in cur.execute("SELECT * FROM %s" % qn)]
            tables.append({"name": n, "columns": columns, "rows": rows})
        return tables
    finally:
        con.close()


# 対応拡張子
ACCESS_EXT = {".accdb", ".mdb"}
XLSX_EXT = {".xlsx", ".xlsm"}
SQLITE_EXT = {".sqlite3", ".sqlite", ".db"}
SUPPORTED_EXT = ACCESS_EXT | XLSX_EXT | SQLITE_EXT


def source_kind(path):
    """拡張子から入力種別を返す: 'access' / 'xlsx' / 'sqlite' / None"""
    ext = os.path.splitext(path)[1].lower()
    if ext in ACCESS_EXT:
        return "access"
    if ext in XLSX_EXT:
        return "xlsx"
    if ext in SQLITE_EXT:
        return "sqlite"
    return None


def read_source(path, prefer="auto"):
    """
    入力ファイルを種別自動判定で読み取り (tables, source_desc) を返す。
    - .accdb/.mdb : Accessエンジン
    - .xlsx/.xlsm : openpyxl
    - .sqlite3/.db: sqlite3
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    kind = source_kind(path)
    if kind == "access":
        tables, eng = read_access(path, prefer=prefer)
        return tables, "Access (%s)" % eng
    if kind == "xlsx":
        tables = read_xlsx(path)
        return tables, "xlsx (%s)" % getattr(read_xlsx, "engine_used", "?")
    if kind == "sqlite":
        return read_sqlite(path), "sqlite3"
    raise ValueError(
        "未対応の拡張子です: %s（対応: .accdb/.mdb/.xlsx/.sqlite3/.db）"
        % os.path.splitext(path)[1])
