# -*- coding: utf-8 -*-
"""
xlsx_reader.py
標準ライブラリ(zipfile / xml.etree)だけで .xlsx を読む。openpyxl は不要。

openpyxl が入っていれば engine.py はそちらを優先する。これは
「ラインPCに追加ライブラリを入れられない」場合のための代替経路。

対応: 共有文字列 / インライン文字列 / 数値 / 真偽値 / 日付書式の復元
非対応: 数式の再計算(キャッシュ値をそのまま読む)、グラフ、書式一般
"""

import re
import zipfile
import datetime
import xml.etree.ElementTree as ET

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = ("{http://schemas.openxmlformats.org/officeDocument/2006/"
           "relationships}")
_NS_PKG_REL = ("{http://schemas.openxmlformats.org/package/2006/"
               "relationships}")

_EPOCH = datetime.datetime(1899, 12, 30)

# Excel 組み込みの日付/時刻書式 ID
_BUILTIN_DATE_FMT = set(range(14, 23)) | set(range(45, 48))

_CELL_REF = re.compile(r"([A-Z]+)")


def _col_index(ref):
    """'B7' -> 1 (0始まり)。"""
    m = _CELL_REF.match(ref or "")
    if not m:
        return None
    index = 0
    for ch in m.group(1):
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _serial_to_datetime(number, fmt_id):
    try:
        value = _EPOCH + datetime.timedelta(days=float(number))
    except (ValueError, OverflowError):
        return number
    # 時刻専用書式は時刻だけ、日付専用書式(14-17)は日付だけ返す
    if fmt_id in (18, 19, 20, 21, 45, 46, 47):
        return value.time()
    if fmt_id in (14, 15, 16, 17) and value.time() == datetime.time(0, 0):
        return value.date()
    return value


def _shared_strings(zf):
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    strings = []
    with zf.open("xl/sharedStrings.xml") as fp:
        for _, elem in ET.iterparse(fp, events=("end",)):
            if elem.tag == _NS_MAIN + "si":
                # <si> の下の <t> をすべて連結する(書式分割された文字列対策)
                strings.append("".join(
                    t.text or "" for t in elem.iter(_NS_MAIN + "t")))
                elem.clear()
    return strings


def _date_styles(zf):
    """cellXfs のインデックス -> numFmtId のうち、日付書式のものだけ。"""
    if "xl/styles.xml" not in zf.namelist():
        return {}
    with zf.open("xl/styles.xml") as fp:
        root = ET.parse(fp).getroot()

    custom = {}
    for fmt in root.iter(_NS_MAIN + "numFmt"):
        code = fmt.get("formatCode") or ""
        # 引用符の中は書式指定ではないので取り除いてから判定する
        bare = re.sub(r'"[^"]*"', "", code)
        if re.search(r"[ymdhs]", bare, re.IGNORECASE):
            try:
                custom[int(fmt.get("numFmtId"))] = True
            except (TypeError, ValueError):
                pass

    styles = {}
    cell_xfs = root.find(_NS_MAIN + "cellXfs")
    if cell_xfs is None:
        return styles
    for idx, xf in enumerate(cell_xfs.findall(_NS_MAIN + "xf")):
        try:
            fmt_id = int(xf.get("numFmtId") or 0)
        except ValueError:
            continue
        if fmt_id in _BUILTIN_DATE_FMT or fmt_id in custom:
            styles[idx] = fmt_id
    return styles


def _sheet_parts(zf):
    """[(シート名, zip内のパス), ...] を workbook.xml の並び順で返す。"""
    with zf.open("xl/_rels/workbook.xml.rels") as fp:
        rels_root = ET.parse(fp).getroot()
    targets = {}
    for rel in rels_root.findall(_NS_PKG_REL + "Relationship"):
        target = rel.get("Target") or ""
        if target.startswith("/"):
            target = target.lstrip("/")
        elif not target.startswith("xl/"):
            target = "xl/" + target
        targets[rel.get("Id")] = target.replace("xl/worksheets/../", "xl/")

    with zf.open("xl/workbook.xml") as fp:
        wb_root = ET.parse(fp).getroot()

    parts = []
    sheets = wb_root.find(_NS_MAIN + "sheets")
    for sheet in (sheets if sheets is not None else []):
        rid = sheet.get(_NS_REL + "id")
        path = targets.get(rid)
        if path and path in zf.namelist():
            parts.append((sheet.get("name") or "Sheet", path))
    return parts


def _cell_value(cell, strings, date_styles):
    ctype = cell.get("t")

    if ctype == "inlineStr":
        is_elem = cell.find(_NS_MAIN + "is")
        if is_elem is None:
            return None
        return "".join(t.text or "" for t in is_elem.iter(_NS_MAIN + "t"))

    v_elem = cell.find(_NS_MAIN + "v")
    if v_elem is None or v_elem.text is None:
        return None
    raw = v_elem.text

    if ctype == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return raw
    if ctype in ("str", "e"):
        return raw
    if ctype == "b":
        return raw not in ("0", "", "false", "FALSE")

    # 書式なし = 数値
    try:
        style = int(cell.get("s") or 0)
    except ValueError:
        style = 0
    if style in date_styles:
        return _serial_to_datetime(raw, date_styles[style])

    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() and abs(number) < 2 ** 53 \
        else number


def _read_sheet(zf, path, strings, date_styles):
    """(columns, rows) を返す。1行目を見出しとして扱う。"""
    columns = []
    rows = []
    ncol = 0

    with zf.open(path) as fp:
        for _, elem in ET.iterparse(fp, events=("end",)):
            if elem.tag != _NS_MAIN + "row":
                continue

            values = []
            for cell in elem.findall(_NS_MAIN + "c"):
                idx = _col_index(cell.get("r"))
                if idx is None:
                    idx = len(values)
                # 空セルは <c> ごと省略されるので位置を埋め戻す
                while len(values) < idx:
                    values.append(None)
                values.append(_cell_value(cell, strings, date_styles))
            elem.clear()

            if not columns:
                columns = [str(v) if v is not None else "col%d" % (i + 1)
                           for i, v in enumerate(values)]
                ncol = len(columns)
                continue

            if all(v is None for v in values):
                continue
            if len(values) < ncol:
                values += [None] * (ncol - len(values))
            elif len(values) > ncol:
                values = values[:ncol]
            rows.append(tuple(values))

    return columns, rows


def read_xlsx(path):
    """.xlsx を中間形式 [{"name","columns","rows"}, ...] で返す。"""
    tables = []
    with zipfile.ZipFile(path) as zf:
        if "xl/workbook.xml" not in zf.namelist():
            raise ValueError(
                "xlsx として読めません(xl/workbook.xml がありません): %s" % path)
        strings = _shared_strings(zf)
        date_styles = _date_styles(zf)
        for name, part in _sheet_parts(zf):
            columns, rows = _read_sheet(zf, part, strings, date_styles)
            tables.append({"name": name, "columns": columns, "rows": rows})
    return tables
