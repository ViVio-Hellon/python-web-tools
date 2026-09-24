# -*- coding: utf-8 -*-
"""
writers.py
中間形式 tables を xlsx / sqlite3 に書き出す。
    tables = [{"name": str, "columns": [str, ...], "rows": [(...), ...]}, ...]

xlsx の実体は xlsx_writer.py(標準ライブラリのみ)。openpyxl は使わない。
"""

import os
import sqlite3
import datetime
import decimal

import xlsx_writer


def write_xlsx(tables, out_path, report=None):
    """
    tables を .xlsx に書き出す。

    制御文字の除去や Excel の上限(32,767文字 / シート名31文字 など)への
    丸め込みは xlsx_writer 側で行う。何を直したかは report に入る。
    """
    return xlsx_writer.write_xlsx(tables, out_path, report=report)


def _sqlite_safe(value):
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return str(value)


def _unique(name, used):
    candidate = name
    i = 1
    while candidate in used or candidate == "":
        candidate = "%s_%d" % (name, i)
        i += 1
    used.add(candidate)
    return candidate


def _q(identifier):
    return '"' + str(identifier).replace('"', '""') + '"'


def write_sqlite(tables, out_path, report=None):
    if os.path.exists(out_path):
        os.remove(out_path)

    conn = sqlite3.connect(out_path)
    try:
        cur = conn.cursor()
        used_tables = set()

        for tbl in tables:
            tname = _unique(str(tbl["name"] or "table"), used_tables)
            columns = tbl["columns"]

            if not columns:
                cur.execute("CREATE TABLE %s (dummy)" % _q(tname))
                continue

            used_cols = set()
            safe_cols = [_unique(str(c), used_cols) for c in columns]
            col_defs = ", ".join("%s" % _q(c) for c in safe_cols)
            cur.execute("CREATE TABLE %s (%s)" % (_q(tname), col_defs))

            placeholders = ", ".join(["?"] * len(safe_cols))
            insert_sql = "INSERT INTO %s VALUES (%s)" % (_q(tname),
                                                         placeholders)

            batch = []
            for row in tbl["rows"]:
                vals = [_sqlite_safe(v) for v in row]
                if len(vals) < len(safe_cols):
                    vals += [None] * (len(safe_cols) - len(vals))
                elif len(vals) > len(safe_cols):
                    vals = vals[: len(safe_cols)]
                batch.append(vals)
                if len(batch) >= 1000:
                    cur.executemany(insert_sql, batch)
                    batch = []
            if batch:
                cur.executemany(insert_sql, batch)

        conn.commit()
    finally:
        conn.close()
    return out_path
