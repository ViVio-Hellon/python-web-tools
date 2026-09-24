# -*- coding: utf-8 -*-
"""
xlsx_writer.py
標準ライブラリ(zipfile / xml)だけで .xlsx を書き出す。openpyxl は不要。

なぜ自前で書くのか
------------------
  * ラインPCには追加ライブラリを入れられないため、openpyxl に依存しない。
  * openpyxl はシート全体をメモリ上のセルオブジェクトとして保持するため、
    大きなテーブルでメモリを食い潰す。ここでは行を逐次 ZIP へ流し込む。
  * openpyxl は Excel が扱えない文字(制御文字)が1つでもあると
    IllegalCharacterError で変換全体を中断する。ここでは取り除いて続行し、
    何件直したかを report に残す。

Excel の制約(本モジュールで守るもの)
------------------------------------
  行 1,048,576 / 列 16,384 / 1セル 32,767文字 / シート名31文字
  シート名に []:*?/\\ は使えず、先頭と末尾の ' も不可、空も不可
  XML に載せられない制御文字(TAB/LF/CR 以外の C0)は書けない
"""

import re
import zipfile
import datetime
import decimal

MAX_ROWS = 1048576
MAX_COLS = 16384
MAX_CELL_CHARS = 32767
MAX_SHEET_NAME = 31

# XML / Excel に載せられない文字。TAB(09) LF(0A) CR(0D) は残す。
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

# シート名に使えない文字
_BAD_SHEET = re.compile(r"[\[\]\:\*\?\/\\]")

# Excel が内部で予約している名前
_RESERVED_SHEET = {"history"}

# Excel のシリアル値の基準日(1900年のうるう年バグ込みで 1899-12-30)
_EPOCH = datetime.datetime(1899, 12, 30)


# ---------------------------------------------------------------------------
# 文字列 / セル値の安全化
# ---------------------------------------------------------------------------
def _escape(text):
    """XML のテキストノードとして安全な文字列にする。"""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _escape_attr(text):
    return _escape(text).replace('"', "&quot;")


def clean_text(text, report=None):
    """制御文字を除去し、32,767文字に収める。"""
    cleaned = _ILLEGAL.sub("", text)
    if report is not None and cleaned != text:
        report["removed_control_chars"] = report.get(
            "removed_control_chars", 0) + 1
    if len(cleaned) > MAX_CELL_CHARS:
        cleaned = cleaned[:MAX_CELL_CHARS]
        if report is not None:
            report["truncated_cells"] = report.get("truncated_cells", 0) + 1
    return cleaned


def _excel_serial(value):
    """datetime/date/time を Excel のシリアル値へ。"""
    if isinstance(value, datetime.datetime):
        delta = value - _EPOCH
        return delta.days + delta.seconds / 86400.0 + \
            delta.microseconds / 86400000000.0
    if isinstance(value, datetime.date):
        return (value - _EPOCH.date()).days
    # datetime.time は日付を持たないので時刻の割合だけ
    return (value.hour * 3600 + value.minute * 60 + value.second +
            value.microsecond / 1000000.0) / 86400.0


# セル書式 (styles.xml の cellXfs の並び順と一致させること)
_STYLE_GENERAL = 0
_STYLE_DATE = 1
_STYLE_DATETIME = 2
_STYLE_TIME = 3


def _cell_xml(ref, value, report):
    """1セル分の <c> を返す。"""
    if value is None or value == "":
        return ""

    # bool は int のサブクラスなので先に判定する
    if isinstance(value, bool):
        return '<c r="%s" t="b"><v>%d</v></c>' % (ref, 1 if value else 0)

    if isinstance(value, int):
        return '<c r="%s"><v>%d</v></c>' % (ref, value)

    if isinstance(value, decimal.Decimal):
        value = float(value)

    if isinstance(value, float):
        # NaN / Inf は Excel が読めないので文字列として残す
        if value != value or value in (float("inf"), float("-inf")):
            return '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (
                ref, _escape(repr(value)))
        return '<c r="%s"><v>%.17g</v></c>' % (ref, value)

    if isinstance(value, datetime.datetime):
        return '<c r="%s" s="%d"><v>%.11f</v></c>' % (
            ref, _STYLE_DATETIME, _excel_serial(value))
    if isinstance(value, datetime.date):
        return '<c r="%s" s="%d"><v>%d</v></c>' % (
            ref, _STYLE_DATE, _excel_serial(value))
    if isinstance(value, datetime.time):
        return '<c r="%s" s="%d"><v>%.11f</v></c>' % (
            ref, _STYLE_TIME, _excel_serial(value))

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        value = raw.hex() if len(raw) <= 64 else "[BLOB %d bytes]" % len(raw)
    elif not isinstance(value, str):
        value = str(value)

    text = clean_text(value, report)
    if not text:
        return ""
    return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t>' \
           '</is></c>' % (ref, _escape(text))


# ---------------------------------------------------------------------------
# 列記号 / シート名
# ---------------------------------------------------------------------------
def column_letter(index):
    """1 -> A, 27 -> AA。"""
    if index < 1:
        raise ValueError("列番号は1以上です: %r" % index)
    letters = ""
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def safe_sheet_name(raw, used):
    """Excel が受け付けるシート名にして、重複しないようにする。"""
    name = _BAD_SHEET.sub("_", str(raw or "Sheet"))
    name = _ILLEGAL.sub("", name)
    # 先頭と末尾のアポストロフィは Excel が開けなくなる
    name = name.strip("'").strip()
    if not name:
        name = "Sheet"
    if name.lower() in _RESERVED_SHEET:
        name += "_"
    name = name[:MAX_SHEET_NAME]

    candidate = name
    i = 1
    lowered = set(u.lower() for u in used)     # Excel はシート名の大小を区別しない
    while candidate.lower() in lowered:
        suffix = "_%d" % i
        candidate = name[:MAX_SHEET_NAME - len(suffix)] + suffix
        i += 1
    used.add(candidate)
    return candidate


# ---------------------------------------------------------------------------
# 固定パーツ
# ---------------------------------------------------------------------------
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_RELS_ROOT = _XML_HEAD + (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rId1" Type="http://schemas.openxml'
    'formats.org/officeDocument/2006/relationships/officeDocument"'
    ' Target="xl/workbook.xml"/></Relationships>')

# numFmtId 14=日付 / 22=日付時刻 / 21=時刻 はExcel組み込みなので定義不要
_STYLES = _XML_HEAD + (
    '<styleSheet xmlns="%s">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/>'
    '</border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0"'
    ' borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0"'
    ' applyNumberFormat="1"/>'
    '<xf numFmtId="22" fontId="0" fillId="0" borderId="0" xfId="0"'
    ' applyNumberFormat="1"/>'
    '<xf numFmtId="21" fontId="0" fillId="0" borderId="0" xfId="0"'
    ' applyNumberFormat="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/>'
    '</cellStyles></styleSheet>') % _NS_MAIN


def _content_types(sheet_count):
    parts = [_XML_HEAD,
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
             'content-types">',
             '<Default Extension="rels" ContentType="application/vnd.openxml'
             'formats-package.relationships+xml"/>',
             '<Default Extension="xml" ContentType="application/xml"/>',
             '<Override PartName="/xl/workbook.xml" ContentType="application/'
             'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
             '<Override PartName="/xl/styles.xml" ContentType="application/'
             'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(1, sheet_count + 1):
        parts.append('<Override PartName="/xl/worksheets/sheet%d.xml"'
                     ' ContentType="application/vnd.openxmlformats-office'
                     'document.spreadsheetml.worksheet+xml"/>' % i)
    parts.append("</Types>")
    return "".join(parts)


def _workbook_xml(sheet_names):
    parts = [_XML_HEAD,
             '<workbook xmlns="%s" xmlns:r="%s"><sheets>' % (_NS_MAIN, _NS_REL)]
    for i, name in enumerate(sheet_names, start=1):
        parts.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
                     % (_escape_attr(name), i, i))
    parts.append("</sheets></workbook>")
    return "".join(parts)


def _workbook_rels(sheet_count):
    parts = [_XML_HEAD,
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
             '2006/relationships">']
    for i in range(1, sheet_count + 1):
        parts.append('<Relationship Id="rId%d" Type="http://schemas.openxml'
                     'formats.org/officeDocument/2006/relationships/worksheet"'
                     ' Target="worksheets/sheet%d.xml"/>' % (i, i))
    parts.append('<Relationship Id="rId%d" Type="http://schemas.openxml'
                 'formats.org/officeDocument/2006/relationships/styles"'
                 ' Target="styles.xml"/>' % (sheet_count + 1))
    parts.append("</Relationships>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------
def _col_widths_xml(columns):
    if not columns:
        return ""
    parts = ["<cols>"]
    for idx, name in enumerate(columns, start=1):
        width = min(max(10, len(str(name)) + 2), 40)
        parts.append('<col min="%d" max="%d" width="%d" customWidth="1"/>'
                     % (idx, idx, width))
    parts.append("</cols>")
    return "".join(parts)


def _write_sheet(zf, arcname, table, report):
    """1テーブルを1シートとして ZIP へ逐次書き出す。"""
    columns = list(table.get("columns") or [])
    rows = table.get("rows") or []

    if len(columns) > MAX_COLS:
        raise ValueError(
            "テーブル『%s』の列数 %d が Excel の上限 %d を超えています。"
            "出力形式に SQLite3 を選んでください。"
            % (table.get("name"), len(columns), MAX_COLS))

    header_rows = 1 if columns else 0
    if len(rows) + header_rows > MAX_ROWS:
        raise ValueError(
            "テーブル『%s』の行数 %d が Excel の上限 %d 行を超えています。"
            "出力形式に SQLite3 を選んでください。"
            % (table.get("name"), len(rows), MAX_ROWS - 1))

    with zf.open(arcname, "w") as fp:
        def emit(text):
            fp.write(text.encode("utf-8"))

        # 列数は見出しに合わせる。見出しが無いテーブルは最長行に合わせる。
        ncol = len(columns) or max((len(r) for r in rows), default=0)
        ncol = min(ncol, MAX_COLS)
        nrow = len(rows) + header_rows

        emit(_XML_HEAD)
        emit('<worksheet xmlns="%s">' % _NS_MAIN)
        # <dimension> が無いと行数・列数を見ないツールがある(openpyxl の
        # read_only では max_row が None になる)。先に範囲を書いておく。
        if ncol and nrow:
            emit('<dimension ref="A1:%s%d"/>' % (column_letter(ncol), nrow))
        emit(_col_widths_xml(columns))
        emit("<sheetData>")

        row_no = 0
        if columns:
            row_no = 1
            cells = "".join(
                _cell_xml("%s1" % column_letter(i), str(c), report)
                for i, c in enumerate(columns, start=1))
            emit('<row r="1">%s</row>' % cells)

        buffer = []
        for row in rows:
            row_no += 1
            cells = []
            for i in range(ncol):
                value = row[i] if i < len(row) else None
                cell = _cell_xml("%s%d" % (column_letter(i + 1), row_no),
                                 value, report)
                if cell:
                    cells.append(cell)
            buffer.append('<row r="%d">%s</row>' % (row_no, "".join(cells)))
            if len(buffer) >= 500:
                emit("".join(buffer))
                buffer = []
        if buffer:
            emit("".join(buffer))

        emit("</sheetData></worksheet>")


def write_xlsx(tables, out_path, report=None):
    """
    中間形式 tables を .xlsx として out_path に書き出す。

    tables: [{"name": str, "columns": [str, ...], "rows": [(...), ...]}, ...]
    report: 渡すと {"removed_control_chars": n, "truncated_cells": n,
                    "renamed_sheets": [(元, 後), ...]} を書き込む
    """
    if report is None:
        report = {}
    report.setdefault("removed_control_chars", 0)
    report.setdefault("truncated_cells", 0)
    report.setdefault("renamed_sheets", [])

    tables = list(tables or [])
    if not tables:
        tables = [{"name": "empty", "columns": [], "rows": []}]

    used = set()
    sheet_names = []
    for table in tables:
        raw = table.get("name")
        name = safe_sheet_name(raw, used)
        if str(raw or "") != name:
            report["renamed_sheets"].append((str(raw or ""), name))
        sheet_names.append(name)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types(len(tables)))
        zf.writestr("_rels/.rels", _RELS_ROOT)
        zf.writestr("xl/workbook.xml", _workbook_xml(sheet_names))
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(tables)))
        zf.writestr("xl/styles.xml", _STYLES)
        for i, table in enumerate(tables, start=1):
            _write_sheet(zf, "xl/worksheets/sheet%d.xml" % i, table, report)

    return out_path
