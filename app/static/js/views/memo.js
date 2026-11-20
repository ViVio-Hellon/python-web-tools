/*
  memo.js — メモ(Excel 風の簡易表)

  保存は**この PC の手元の DB**(`packaging_tool/memo_store.py`)。画面は「変わったもの」だけを貯め、
  手を止めて 0.8 秒たったら1回で送る(貼り付け1万セルでも1回)。Ctrl+S・「保存」はすぐ送る。
  送れなかった分は画面に持ったまま 10 秒ごとに送り直し、念のためこの端末にも控える(本体は DB)。
  閉じる前に未保存があれば止める。次に開いたとき控えが残っていれば「戻しますか」と訊く。

  表は見えている範囲だけを描く(行が多くても重くしない)。入力は隠れた1つの入力欄がいつも受ける
  (日本語の変換もそのまま。打ち始めたら、その欄がセルの上に見えて編集になる)。
*/
import { api, tokenUrl } from "../api.js";
import { confirmBox, promptBox } from "../askbox.js";
import { openWindow } from "../desktop.js";
import * as nav from "../nav.js";
import { toast, toastError } from "../toast.js";

const DEFAULT_W = 80;
const DEFAULT_H = 22;
const SAVE_DELAY_MS = 800;
const RETRY_MS = 10_000;
const BACKUP_KEY = "memo.backup.";
const LAST_KEY = "memo.lastSheet";
const BORDER = "1px solid #000";

const el = {};
let limits = { rows: 20000, cols: 200, text: 32767, name: 31, trash_days: 30 };
let sheets = [];          // [{id, name, ...}]
let sheet = null;         // いま開いているシート {id, name, rows, cols, col_widths, row_heights, rev}
let cells = new Map();    // "r,c" → {v, f}
let colX = [0];           // 列の左端(累積)
let rowY = [0];
let anchor = { r: 0, c: 0 };
let focus = { r: 0, c: 0 };
let editing = null;       // {r, c, byTyping}
let pending = [];         // まだ送っていない操作(起きた順)
let inflight = null;      // 送っている最中の操作
let saveTimer = 0;
let retryTimer = 0;
let lastSavedAt = "";
let dragging = null;      // {kind: "cells"|"col"|"row"|"colsize"|"rowsize", ...}

// ------------------------------------------------------------------ 寸法
const key = (r, c) => `${r},${c}`;
const colW = (c) => Number(sheet.col_widths[c]) || DEFAULT_W;
const rowH = (r) => Number(sheet.row_heights[r]) || DEFAULT_H;

function measure() {
  colX = new Array(sheet.cols + 1);
  colX[0] = 0;
  for (let c = 0; c < sheet.cols; c += 1) colX[c + 1] = colX[c] + colW(c);
  rowY = new Array(sheet.rows + 1);
  rowY[0] = 0;
  for (let r = 0; r < sheet.rows; r += 1) rowY[r + 1] = rowY[r] + rowH(r);
}

function at(edges, pos) {
  // edges[i] <= pos < edges[i+1] となる i
  let lo = 0;
  let hi = edges.length - 2;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (edges[mid] <= pos) lo = mid; else hi = mid - 1;
  }
  return Math.max(0, lo);
}

export function colName(c) {
  let s = "";
  let n = c + 1;
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); }
  return s;
}

// ------------------------------------------------------------------ 描く
function range() {
  return { r0: Math.min(anchor.r, focus.r), c0: Math.min(anchor.c, focus.c),
           r1: Math.max(anchor.r, focus.r), c1: Math.max(anchor.c, focus.c) };
}

function styleCell(div, cell) {
  const f = (cell && cell.f) || {};
  div.style.fontWeight = f.b ? "700" : "";
  div.style.fontStyle = f.i ? "italic" : "";
  div.style.color = f.color || "";
  div.style.background = f.bg || "";
  div.style.textAlign = f.align || "";
  div.style.borderTop = f.bt ? BORDER : "";
  div.style.borderLeft = f.bl ? BORDER : "";
  if (f.br) div.style.borderRight = BORDER; else div.style.borderRight = "";
  if (f.bb) div.style.borderBottom = BORDER; else div.style.borderBottom = "";
  div.classList.toggle("is-wrap", Boolean(f.wrap));
  const v = cell ? cell.v : "";
  div.classList.toggle("is-num", !f.align && v !== "" && /^[-+]?[\d,]*\.?\d+%?$/.test(v));
}

let drawQueued = false;
function draw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; paint(); });
}

function paint() {
  if (!sheet) return;
  const view = el.mView;
  const sx = view.scrollLeft;
  const sy = view.scrollTop;
  const w = view.clientWidth;
  const h = view.clientHeight;
  const c0 = at(colX, sx);
  const c1 = Math.min(sheet.cols - 1, at(colX, sx + w) + 1);
  const r0 = at(rowY, sy);
  const r1 = Math.min(sheet.rows - 1, at(rowY, sy + h) + 1);
  const total = [colX[sheet.cols], rowY[sheet.rows]];
  el.mCells.style.width = `${total[0]}px`;
  el.mCells.style.height = `${total[1]}px`;
  el.mColInner.style.width = `${total[0] + 50}px`;
  el.mColInner.style.height = "24px";
  el.mRowInner.style.height = `${total[1] + 50}px`;
  el.mColInner.style.transform = `translateX(${-sx}px)`;
  el.mRowInner.style.transform = `translateY(${-sy}px)`;
  const sel = range();

  // 列見出し
  const heads = [];
  for (let c = c0; c <= c1; c += 1) {
    const d = document.createElement("div");
    d.className = `memo__hd${c >= sel.c0 && c <= sel.c1 ? " is-on" : ""}`;
    d.style.left = `${colX[c]}px`; d.style.top = "0px"; d.style.width = `${colW(c)}px`; d.style.height = "24px";
    d.textContent = colName(c);
    d.dataset.col = c;
    const g = document.createElement("span");
    g.className = "memo__grip-c"; g.dataset.colsize = c;
    d.append(g);
    heads.push(d);
  }
  el.mColInner.replaceChildren(...heads);
  const rheads = [];
  for (let r = r0; r <= r1; r += 1) {
    const d = document.createElement("div");
    d.className = `memo__hd${r >= sel.r0 && r <= sel.r1 ? " is-on" : ""}`;
    d.style.left = "0px"; d.style.top = `${rowY[r]}px`; d.style.width = "46px"; d.style.height = `${rowH(r)}px`;
    d.textContent = String(r + 1);
    d.dataset.row = r;
    const g = document.createElement("span");
    g.className = "memo__grip-r"; g.dataset.rowsize = r;
    d.append(g);
    rheads.push(d);
  }
  el.mRowInner.replaceChildren(...rheads);

  // セル
  const frag = [];
  for (let r = r0; r <= r1; r += 1) {
    for (let c = c0; c <= c1; c += 1) {
      const d = document.createElement("div");
      d.className = "memo__c";
      d.style.left = `${colX[c]}px`; d.style.top = `${rowY[r]}px`;
      d.style.width = `${colW(c)}px`; d.style.height = `${rowH(r)}px`;
      const cell = cells.get(key(r, c));
      if (cell) { d.textContent = cell.v; styleCell(d, cell); }
      frag.push(d);
    }
  }
  el.mCells.replaceChildren(...frag, el.mSelBox, el.mActive);

  // 選択
  const multi = sel.r0 !== sel.r1 || sel.c0 !== sel.c1;
  el.mSelBox.hidden = !multi;
  if (multi) place(el.mSelBox, sel.r0, sel.c0, sel.r1, sel.c1);
  place(el.mActive, focus.r, focus.c, focus.r, focus.c);
  placeEditor();
  el.mAddr.textContent = multi ? `${colName(sel.c0)}${sel.r0 + 1}:${colName(sel.c1)}${sel.r1 + 1}`
    : `${colName(focus.c)}${focus.r + 1}`;
  const cur = cells.get(key(focus.r, focus.c));
  el.mValue.textContent = cur ? cur.v : "";
}

function place(node, r0, c0, r1, c1) {
  node.style.left = `${colX[c0]}px`;
  node.style.top = `${rowY[r0]}px`;
  node.style.width = `${colX[c1 + 1] - colX[c0] + 1}px`;
  node.style.height = `${rowY[r1 + 1] - rowY[r0] + 1}px`;
}

function placeEditor() {
  const r = editing ? editing.r : focus.r;
  const c = editing ? editing.c : focus.c;
  const left = 46 + colX[c] - el.mView.scrollLeft;
  const top = 24 + rowY[r] - el.mView.scrollTop;
  el.mEditor.style.left = `${Math.max(46, left)}px`;
  el.mEditor.style.top = `${Math.max(24, top)}px`;
  el.mEditor.style.minWidth = `${colW(c) + 1}px`;
  el.mEditor.style.width = editing ? `${Math.max(colW(c) + 1, Math.min(600, el.mEditor.scrollWidth + 4))}px`
    : `${colW(c) + 1}px`;
  el.mEditor.style.height = `${Math.max(rowH(r) + 1, editing ? el.mEditor.scrollHeight : 0)}px`;
}

function reveal(r, c) {
  const v = el.mView;
  if (colX[c] < v.scrollLeft) v.scrollLeft = colX[c];
  else if (colX[c + 1] > v.scrollLeft + v.clientWidth) v.scrollLeft = colX[c + 1] - v.clientWidth;
  if (rowY[r] < v.scrollTop) v.scrollTop = rowY[r];
  else if (rowY[r + 1] > v.scrollTop + v.clientHeight) v.scrollTop = rowY[r + 1] - v.clientHeight;
}

// ------------------------------------------------------------------ 選ぶ・動く
function select(r, c, extend = false) {
  r = Math.max(0, Math.min(sheet.rows - 1, r));
  c = Math.max(0, Math.min(sheet.cols - 1, c));
  focus = { r, c };
  if (!extend) anchor = { r, c };
  reveal(r, c);
  draw();
}

function move(dr, dc, extend = false) {
  select(focus.r + dr, focus.c + dc, extend);
}

function cellAt(event) {
  const rect = el.mView.getBoundingClientRect();
  const x = event.clientX - rect.left + el.mView.scrollLeft;
  const y = event.clientY - rect.top + el.mView.scrollTop;
  return { r: Math.min(sheet.rows - 1, at(rowY, y)), c: Math.min(sheet.cols - 1, at(colX, x)) };
}

// ------------------------------------------------------------------ 変える(すべてここを通す)
function setCells(list) {
  // list: [{r, c, v, f}] — 画面に入れて、保存の列に積む
  if (!list.length) return;
  let grow = false;
  for (const it of list) {
    if (it.r >= sheet.rows) { sheet.rows = Math.min(limits.rows, it.r + 1); grow = true; }
    if (it.c >= sheet.cols) { sheet.cols = Math.min(limits.cols, it.c + 1); grow = true; }
    const k = key(it.r, it.c);
    const f = it.f && Object.keys(it.f).length ? it.f : undefined;
    if ((it.v ?? "") === "" && !f) cells.delete(k); else cells.set(k, { v: it.v ?? "", ...(f ? { f } : {}) });
  }
  if (grow) { measure(); queue({ op: "size", rows: sheet.rows, cols: sheet.cols }); }
  queueCells(list.map((it) => ({ r: it.r, c: it.c, v: it.v ?? "", ...(it.f ? { f: it.f } : {}) })));
  draw();
}

function queueCells(list) {
  const last = pending[pending.length - 1];
  if (last && last.op === "cells") {
    const idx = new Map(last.cells.map((c, i) => [key(c.r, c.c), i]));
    for (const c of list) {
      const i = idx.get(key(c.r, c.c));
      if (i === undefined) { idx.set(key(c.r, c.c), last.cells.length); last.cells.push(c); } else last.cells[i] = c;
    }
  } else {
    pending.push({ op: "cells", cells: list });
  }
  changed();
}

function queue(op) {
  pending.push(op);
  changed();
}

function changed() {
  backup();
  status("未保存の変更があります", "warn");
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => save(), SAVE_DELAY_MS);
}

// ------------------------------------------------------------------ 保存
function status(text, level = "") {
  el.mStatus.textContent = text;
  el.mStatus.dataset.level = level;
}

function backup() {
  try {
    const ops = [...(inflight || []), ...pending];
    if (ops.length) window.localStorage.setItem(BACKUP_KEY + sheet.id, JSON.stringify({ rev: sheet.rev, ops }));
    else window.localStorage.removeItem(BACKUP_KEY + sheet.id);
  } catch { /* 控えられなくても本体は DB に送る */ }
}

export function dirty() {
  return pending.length > 0 || inflight !== null;
}

export async function save(force = false) {
  window.clearTimeout(saveTimer);
  if (inflight) return false;              // 送り終わったら続きを送る
  if (!pending.length) {
    if (force) status(lastSavedAt ? `保存しました ${lastSavedAt}` : "変更はありません", "ok");
    return true;
  }
  const target = sheet;
  inflight = pending;
  pending = [];
  status("保存しています…");
  try {
    const res = await api.post(`/api/memo/sheets/${target.id}/save`, { rev: target.rev, ops: inflight });
    target.rev = res.rev;
    inflight = null;
    lastSavedAt = (res.saved_at || "").slice(11, 16);
    window.clearTimeout(retryTimer);
    if (target === sheet) backup();
    if (pending.length) return save(force);
    status(`保存しました ${lastSavedAt}`, "ok");
    return true;
  } catch (err) {
    // **捨てない。** 送れなかった分を前に戻して、あとで送り直す
    pending = [...inflight, ...pending];
    inflight = null;
    if (target === sheet) backup();
    if (err.code === "conflict") {
      status("別の窓の変更とぶつかりました", "ng");
      await resolveConflict(err);
      return false;
    }
    if (err.status && err.status >= 400 && err.status < 500 && err.code !== "screen_taken") {
      status(`保存できませんでした: ${err.message}`, "ng");
      toastError(err);
      return false;
    }
    status("保存できませんでした。10秒後に送り直します(内容はこの画面とこの端末に残っています)", "ng");
    window.clearTimeout(retryTimer);
    retryTimer = window.setTimeout(() => save(), RETRY_MS);
    return false;
  }
}

async function resolveConflict(err) {
  const keep = await confirmBox(
    `${err.message}\n\nこの窓の内容で上書きしますか?\n「上書きする」… この窓の変更を書きます\n「やめる」… 保存されている内容で読み直します(この窓の未保存の変更は捨てます)`,
    { ok: "上書きする" });
  if (keep) {
    if (!pending.some((op) => op.op !== "cells" && op.op !== "size")) {
      sheet.rev = err.body && err.body.rev ? err.body.rev : sheet.rev;
      save();
      return;
    }
    toast("行・列の足し引きは上書きできません。読み直します。", "warn");
  }
  pending = [];
  backup();
  await openSheet(sheet.id, { keepSelection: true });
}

// 閉じる前: 未保存なら止める。閉じてしまうとき・ほかの画面へ移るときは手元へ控え、送れるだけ送る
function flushNow() {
  if (editing) commitEdit();
  window.clearTimeout(saveTimer);
  window.clearTimeout(retryTimer);
  if (!sheet) return;
  const ops = [...(inflight || []), ...pending];
  if (!ops.length) return;
  backup();
  const id = sheet.id;
  const body = JSON.stringify({ rev: sheet.rev, ops: pending });
  if (!pending.length || body.length > 60000) return;     // 送っている最中の分・大きいものは控えから次に戻す
  try {
    fetch(tokenUrl(`/api/memo/sheets/${id}/save`), {
      method: "POST", body, keepalive: true, headers: { "Content-Type": "application/json" },
    }).then((res) => {
      if (res.ok && !inflight) { try { window.localStorage.removeItem(BACKUP_KEY + id); } catch { /* */ } }
    }).catch(() => {});
  } catch { /* 控えから次に戻す */ }
}

function wireLeave(signal) {
  window.addEventListener("beforeunload", (event) => {
    if (!dirty()) return;
    event.preventDefault();
    event.returnValue = "";
  }, { signal });
  window.addEventListener("pagehide", flushNow, { signal });
  // ほかの画面へ移る(レール・F1〜F7。ページは読み込み直さない)
  nav.onLeave(() => {
    flushNow();
    sheet = null;
    pending = [];
    inflight = null;
  });
}

async function restoreBackup() {
  let saved = null;
  try { saved = JSON.parse(window.localStorage.getItem(BACKUP_KEY + sheet.id) || "null"); } catch { saved = null; }
  if (!saved || !Array.isArray(saved.ops) || !saved.ops.length) return;
  const n = saved.ops.reduce((s, op) => s + (op.op === "cells" ? op.cells.length : 1), 0);
  if (!(await confirmBox(`このシートに保存できなかった変更が残っています(${n}件)。戻して保存しますか?\n「やめる」で捨てます。`,
    { ok: "戻して保存する" }))) {
    try { window.localStorage.removeItem(BACKUP_KEY + sheet.id); } catch { /* */ }
    return;
  }
  for (const op of saved.ops) applyLocal(op);
  pending = saved.ops;
  if (saved.rev !== sheet.rev) {
    // 控えより後に保存されている。行列の足し引きを含むなら重ねられない
    if (pending.some((op) => op.op !== "cells" && op.op !== "size")) {
      toast("控えの行・列の足し引きは、そのあと保存された内容と合わせられないため戻せません。", "warn");
      pending = pending.filter((op) => op.op === "cells" || op.op === "size");
    }
  }
  sheet.rev = saved.rev;
  measure();
  draw();
  save();
}

function applyLocal(op) {
  if (op.op === "cells") {
    for (const c of op.cells) {
      if ((c.v ?? "") === "" && !(c.f && Object.keys(c.f).length)) cells.delete(key(c.r, c.c));
      else cells.set(key(c.r, c.c), { v: c.v ?? "", ...(c.f ? { f: c.f } : {}) });
      sheet.rows = Math.max(sheet.rows, c.r + 1);
      sheet.cols = Math.max(sheet.cols, c.c + 1);
    }
  } else if (op.op === "size") {
    if (op.rows) sheet.rows = op.rows;
    if (op.cols) sheet.cols = op.cols;
    if (op.col_widths) sheet.col_widths = op.col_widths;
    if (op.row_heights) sheet.row_heights = op.row_heights;
  } else {
    shiftLocal(op);
  }
}

// ------------------------------------------------------------------ 編集
function startEdit(byTyping, initial = null) {
  if (editing) return;
  editing = { r: focus.r, c: focus.c, byTyping };
  const cur = cells.get(key(focus.r, focus.c));
  if (initial === null) el.mEditor.value = cur ? cur.v : "";
  el.mEditor.classList.add("is-editing");
  const f = (cur && cur.f) || {};
  el.mEditor.style.fontWeight = f.b ? "700" : "";
  el.mEditor.style.textAlign = f.align || "";
  placeEditor();
  if (!byTyping) {
    const n = el.mEditor.value.length;
    el.mEditor.setSelectionRange(n, n);
  }
}

function commitEdit() {
  if (!editing) return;
  const { r, c } = editing;
  const value = el.mEditor.value.slice(0, limits.text);
  editing = null;
  el.mEditor.classList.remove("is-editing");
  el.mEditor.value = "";
  const cur = cells.get(key(r, c));
  if ((cur ? cur.v : "") !== value) setCells([{ r, c, v: value, f: cur && cur.f }]);
  draw();
}

function cancelEdit() {
  editing = null;
  el.mEditor.classList.remove("is-editing");
  el.mEditor.value = "";
  draw();
}

function clearSelection() {
  const s = range();
  const list = [];
  for (const [k, cell] of cells) {
    const [r, c] = k.split(",").map(Number);
    if (r >= s.r0 && r <= s.r1 && c >= s.c0 && c <= s.c1 && cell.v !== "") list.push({ r, c, v: "", f: cell.f });
  }
  setCells(list);
}

// ------------------------------------------------------------------ 書式
function format(update) {
  const s = range();
  const list = [];
  for (let r = s.r0; r <= s.r1; r += 1) {
    for (let c = s.c0; c <= s.c1; c += 1) {
      const cur = cells.get(key(r, c));
      const f = { ...((cur && cur.f) || {}) };
      update(f, r, c, s);
      for (const k of Object.keys(f)) if (f[k] === undefined || f[k] === false || f[k] === "") delete f[k];
      list.push({ r, c, v: cur ? cur.v : "", f });
    }
  }
  if (list.length > 200000) { toast("一度に書式を付けられるのは20万セルまでです。", "warn"); return; }
  setCells(list);
}

function toggleBold() {
  const cur = cells.get(key(focus.r, focus.c));
  const on = !(cur && cur.f && cur.f.b);
  format((f) => { f.b = on; });
}

function border(kind) {
  format((f, r, c, s) => {
    if (kind === "none") { delete f.bt; delete f.bb; delete f.bl; delete f.br; return; }
    if (kind === "all") { f.bt = f.bb = f.bl = f.br = true; return; }
    if (kind === "bottom") { if (r === s.r1) f.bb = true; return; }
    if (kind === "outer") {
      if (r === s.r0) f.bt = true;
      if (r === s.r1) f.bb = true;
      if (c === s.c0) f.bl = true;
      if (c === s.c1) f.br = true;
    }
  });
}

// ------------------------------------------------------------------ コピー・貼り付け
function tsvField(v) {
  return /[\t\n\r"]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
}

export function toTsv(grid) {
  return grid.map((row) => row.map(tsvField).join("\t")).join("\r\n");
}

/** Excel がクリップボードに置くタブ区切り(引用符で囲んだ改行・タブを含む)を表にする。 */
export function parseTsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  let i = 0;
  const s = text.replace(/\r\n?/g, "\n");
  while (i < s.length) {
    const ch = s[i];
    if (quoted) {
      if (ch === '"') {
        if (s[i + 1] === '"') { field += '"'; i += 2; continue; }
        quoted = false; i += 1; continue;
      }
      field += ch; i += 1; continue;
    }
    if (ch === '"' && field === "") { quoted = true; i += 1; continue; }
    if (ch === "\t") { row.push(field); field = ""; i += 1; continue; }
    if (ch === "\n") { row.push(field); rows.push(row); row = []; field = ""; i += 1; continue; }
    field += ch; i += 1;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function escapeHtml(v) {
  return v.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function copyData() {
  const s = range();
  const grid = [];
  let html = "<table>";
  for (let r = s.r0; r <= s.r1; r += 1) {
    const line = [];
    html += "<tr>";
    for (let c = s.c0; c <= s.c1; c += 1) {
      const cell = cells.get(key(r, c));
      const v = cell ? cell.v : "";
      line.push(v);
      const f = (cell && cell.f) || {};
      const css = [f.b ? "font-weight:700" : "", f.color ? `color:${f.color}` : "", f.bg ? `background:${f.bg}` : "",
        f.align ? `text-align:${f.align}` : "", f.bt ? `border-top:${BORDER}` : "", f.bb ? `border-bottom:${BORDER}` : "",
        f.bl ? `border-left:${BORDER}` : "", f.br ? `border-right:${BORDER}` : ""].filter(Boolean).join(";");
      html += `<td${css ? ` style="${css}"` : ""}>${escapeHtml(v).replace(/\n/g, "<br>")}</td>`;
    }
    html += "</tr>";
    grid.push(line);
  }
  html += "</table>";
  return { text: toTsv(grid), html };
}

function pasteText(text) {
  if (!text) return;
  let grid = parseTsv(text);
  if (grid.length && grid[grid.length - 1].length === 1 && grid[grid.length - 1][0] === "" && grid.length > 1) grid.pop();
  if (!grid.length) return;
  const s = range();
  // 1つの値を範囲に貼ったら、範囲ぜんぶに入れる(Excel と同じ)
  if (grid.length === 1 && grid[0].length === 1 && (s.r0 !== s.r1 || s.c0 !== s.c1)) {
    grid = Array.from({ length: s.r1 - s.r0 + 1 }, () => Array(s.c1 - s.c0 + 1).fill(grid[0][0]));
  }
  const width = Math.max(...grid.map((r) => r.length));
  if (s.r0 + grid.length > limits.rows || s.c0 + width > limits.cols) {
    toast(`貼り付けると表の大きさ(${limits.rows}行 × ${limits.cols}列)を超えます。`, "warn");
    return;
  }
  const list = [];
  grid.forEach((line, i) => {
    for (let j = 0; j < width; j += 1) {
      const r = s.r0 + i;
      const c = s.c0 + j;
      const cur = cells.get(key(r, c));
      const v = (line[j] ?? "").slice(0, limits.text);
      if ((cur ? cur.v : "") !== v) list.push({ r, c, v, f: cur && cur.f });
    }
  });
  setCells(list);
  anchor = { r: s.r0, c: s.c0 };
  focus = { r: s.r0 + grid.length - 1, c: s.c0 + width - 1 };
  draw();
  if (list.length > 1000) toast(`${list.length} セルを貼り付けました。`, "info");
}

// ------------------------------------------------------------------ 行・列
function shiftLocal(op) {
  const rowsAxis = op.op.endsWith("rows");
  const insert = op.op.startsWith("insert");
  const at0 = op.at;
  const n = op.n;
  const next = new Map();
  for (const [k, cell] of cells) {
    let [r, c] = k.split(",").map(Number);
    let p = rowsAxis ? r : c;
    if (insert) { if (p >= at0) p += n; } else if (p >= at0 && p < at0 + n) continue; else if (p >= at0 + n) p -= n;
    if (rowsAxis) r = p; else c = p;
    next.set(key(r, c), cell);
  }
  cells = next;
  const sizes = rowsAxis ? sheet.row_heights : sheet.col_widths;
  const moved = {};
  for (const [k, v] of Object.entries(sizes)) {
    const i = Number(k);
    if (insert) moved[i >= at0 ? i + n : i] = v;
    else if (i < at0) moved[i] = v;
    else if (i >= at0 + n) moved[i - n] = v;
  }
  if (rowsAxis) { sheet.row_heights = moved; sheet.rows = insert ? sheet.rows + n : Math.max(1, sheet.rows - n); }
  else { sheet.col_widths = moved; sheet.cols = insert ? sheet.cols + n : Math.max(1, sheet.cols - n); }
}

async function structural(kind) {
  if (editing) commitEdit();
  const s = range();
  const rowsAxis = kind.endsWith("rows");
  const at0 = rowsAxis ? s.r0 : s.c0;
  const n = rowsAxis ? s.r1 - s.r0 + 1 : s.c1 - s.c0 + 1;
  if (kind.startsWith("insert") && (rowsAxis ? sheet.rows : sheet.cols) + n > (rowsAxis ? limits.rows : limits.cols)) {
    toast("これ以上は足せません。", "warn");
    return;
  }
  if (kind.startsWith("delete")) {
    const total = rowsAxis ? sheet.rows : sheet.cols;
    if (n >= total) { toast("ぜんぶは消せません。", "warn"); return; }
  }
  // 足し引きは別の窓と重ねられないので、先に今の分を送っておく
  await save();
  const op = { op: kind, at: at0, n };
  shiftLocal(op);
  queue(op);
  measure();
  select(focus.r, focus.c);
}

// ------------------------------------------------------------------ 大きさを変える(ドラッグ)
function startResize(kind, index, event) {
  dragging = { kind, index, start: kind === "colsize" ? event.clientX : event.clientY,
               size: kind === "colsize" ? colW(index) : rowH(index) };
  event.preventDefault();
}

function onPointerMove(event) {
  if (!dragging || !sheet) return;
  if (dragging.kind === "colsize" || dragging.kind === "rowsize") {
    const pos = dragging.kind === "colsize" ? event.clientX : event.clientY;
    const size = Math.max(dragging.kind === "colsize" ? 16 : 12, Math.round(dragging.size + pos - dragging.start));
    if (dragging.kind === "colsize") sheet.col_widths = { ...sheet.col_widths, [dragging.index]: size };
    else sheet.row_heights = { ...sheet.row_heights, [dragging.index]: size };
    measure();
    draw();
    return;
  }
  if (dragging.kind === "cells") {
    const p = cellAt(event);
    if (p.r !== focus.r || p.c !== focus.c) { focus = p; reveal(p.r, p.c); draw(); }
  } else if (dragging.kind === "col") {
    const p = cellAt(event);
    focus = { r: sheet.rows - 1, c: p.c };
    draw();
  } else if (dragging.kind === "row") {
    const p = cellAt(event);
    focus = { r: p.r, c: sheet.cols - 1 };
    draw();
  }
}

function onPointerUp() {
  if (!dragging) return;
  if (dragging.kind === "colsize") queue({ op: "size", col_widths: sheet.col_widths });
  if (dragging.kind === "rowsize") queue({ op: "size", row_heights: sheet.row_heights });
  dragging = null;
  el.mEditor.focus({ preventScroll: true });
}

// ------------------------------------------------------------------ キー
function onKey(event) {
  if (!sheet) return;
  const ctrl = event.ctrlKey || event.metaKey;
  if (ctrl && (event.key === "s" || event.key === "S")) {
    event.preventDefault();
    if (editing) commitEdit();
    save(true);
    return;
  }
  if (editing) {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "Enter" && event.altKey) {
      event.preventDefault();
      const t = el.mEditor;
      const p = t.selectionStart;
      t.value = `${t.value.slice(0, p)}\n${t.value.slice(t.selectionEnd)}`;
      t.setSelectionRange(p + 1, p + 1);
      placeEditor();
      return;
    }
    if (event.key === "Enter") { event.preventDefault(); commitEdit(); move(event.shiftKey ? -1 : 1, 0); return; }
    if (event.key === "Tab") { event.preventDefault(); commitEdit(); move(0, event.shiftKey ? -1 : 1); return; }
    if (event.key === "Escape") { event.preventDefault(); cancelEdit(); return; }
    if (editing.byTyping && event.key.startsWith("Arrow")) {
      event.preventDefault();
      commitEdit();
      const d = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[event.key];
      move(d[0], d[1]);
    }
    return;
  }
  const arrows = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
  if (arrows[event.key]) {
    event.preventDefault();
    const [dr, dc] = arrows[event.key];
    if (ctrl) {
      select(dr ? (dr < 0 ? 0 : lastUsed("r")) : focus.r, dc ? (dc < 0 ? 0 : lastUsed("c")) : focus.c, event.shiftKey);
    } else {
      move(dr, dc, event.shiftKey);
    }
    return;
  }
  if (event.key === "Enter") { event.preventDefault(); move(event.shiftKey ? -1 : 1, 0); return; }
  if (event.key === "Tab") { event.preventDefault(); move(0, event.shiftKey ? -1 : 1); return; }
  if (event.key === "Delete" || event.key === "Backspace") { event.preventDefault(); clearSelection(); return; }
  // F2 は Excel と同じ「セルの編集」。ツールの画面切替(F1〜F7)には渡さない
  if (event.key === "F2") { event.preventDefault(); event.stopPropagation(); startEdit(false); return; }
  if (event.key === "Home") { event.preventDefault(); select(ctrl ? 0 : focus.r, 0, event.shiftKey); return; }
  if (event.key === "PageDown" || event.key === "PageUp") {
    event.preventDefault();
    const step = Math.max(1, Math.floor(el.mView.clientHeight / DEFAULT_H) - 1);
    move(event.key === "PageDown" ? step : -step, 0, event.shiftKey);
    return;
  }
  if (ctrl && (event.key === "b" || event.key === "B")) { event.preventDefault(); toggleBold(); return; }
  if (ctrl && (event.key === "a" || event.key === "A")) {
    event.preventDefault();
    anchor = { r: 0, c: 0 }; focus = { r: sheet.rows - 1, c: sheet.cols - 1 }; draw();
  }
}

function lastUsed(axis) {
  let max = 0;
  for (const k of cells.keys()) {
    const [r, c] = k.split(",").map(Number);
    if (axis === "r" && c === focus.c) max = Math.max(max, r);
    if (axis === "c" && r === focus.r) max = Math.max(max, c);
  }
  return max || (axis === "r" ? sheet.rows - 1 : sheet.cols - 1);
}

// ------------------------------------------------------------------ シート
function drawTabs() {
  el.mTabs.replaceChildren(...sheets.map((s) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `memo__tab${sheet && s.id === sheet.id ? " is-on" : ""}`;
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", sheet && s.id === sheet.id ? "true" : "false");
    b.textContent = s.name;
    b.addEventListener("click", () => { if (!sheet || s.id !== sheet.id) openSheet(s.id); });
    b.addEventListener("dblclick", () => renameSheet());
    return b;
  }));
}

async function openSheet(id, { keepSelection = false } = {}) {
  if (editing) commitEdit();
  if (sheet && sheet.id !== id) {
    const ok = await save();
    if (!ok && dirty()) {
      toast("いまのシートを保存できないので、切り替えません。もう一度保存してください。", "ng");
      return;
    }
  }
  let data;
  try {
    data = await api.get(`/api/memo/sheets/${id}`);
  } catch (err) {
    toastError(err);
    return;
  }
  sheet = data;
  cells = new Map(data.cells.map((c) => [key(c.r, c.c), { v: c.v, ...(c.f ? { f: c.f } : {}) }]));
  pending = [];
  inflight = null;
  if (!keepSelection) { anchor = { r: 0, c: 0 }; focus = { r: 0, c: 0 }; el.mView.scrollTop = 0; el.mView.scrollLeft = 0; }
  measure();
  try { window.localStorage.setItem(LAST_KEY, String(id)); } catch { /* */ }
  drawTabs();
  status(`保存されています(${(data.updated_at || "").slice(5, 16)})`, "ok");
  draw();
  await restoreBackup();
  el.mEditor.focus({ preventScroll: true });
}

async function refreshSheets(list) {
  sheets = list;
  drawTabs();
}

async function addSheet() {
  if (!(await save()) && dirty()) return;
  try {
    const res = await api.post("/api/memo/sheets", {});
    await refreshSheets(res.sheets);
    await openSheet(res.sheet.id);
  } catch (err) { toastError(err); }
}

async function renameSheet() {
  const name = await promptBox("シートの名前", { value: sheet.name, ok: "変える" });
  if (name === null || name === undefined || name === sheet.name) return;
  try {
    const res = await api.post(`/api/memo/sheets/${sheet.id}/rename`, { name });
    sheet.name = res.sheet.name;
    await refreshSheets(res.sheets);
  } catch (err) { toastError(err); }
}

async function sheetAction(kind) {
  el.mMenu.hidden = true;
  if (kind === "rename") { renameSheet(); return; }
  if (!(await save()) && dirty()) return;
  try {
    if (kind === "duplicate") {
      const res = await api.post(`/api/memo/sheets/${sheet.id}/duplicate`, {});
      await refreshSheets(res.sheets);
      await openSheet(res.sheet.id);
    } else if (kind === "left" || kind === "right") {
      const i = sheets.findIndex((s) => s.id === sheet.id);
      const res = await api.post(`/api/memo/sheets/${sheet.id}/move`, { to: kind === "left" ? i - 1 : i + 1 });
      await refreshSheets(res.sheets);
    } else if (kind === "delete") {
      if (sheets.length <= 1) { toast("最後の1枚は消せません。", "warn"); return; }
      if (!(await confirmBox(`シート「${sheet.name}」を削除しますか?\n消したシートは${limits.trash_days}日のあいだ「消したシートを戻す」で戻せます。`,
        { ok: "削除する", danger: true }))) return;
      const i = sheets.findIndex((s) => s.id === sheet.id);
      const res = await api.post(`/api/memo/sheets/${sheet.id}/delete`, {});
      try { window.localStorage.removeItem(BACKUP_KEY + sheet.id); } catch { /* */ }
      sheet = null;
      await refreshSheets(res.sheets);
      await openSheet(res.sheets[Math.max(0, Math.min(i, res.sheets.length - 1))].id);
      toast("シートを削除しました。", "ok");
    } else if (kind === "trash") {
      const res = await api.get("/api/memo/sheets");
      if (!res.trash.length) { toast(`消したシートはありません(${limits.trash_days}日を過ぎたものは消えます)。`, "info"); return; }
      const list = res.trash.map((t, i) => `${i + 1}. ${t.name}(${t.deleted_at.slice(5, 16)} に削除)`).join("\n");
      const pick = await promptBox(`戻すシートの番号を入れてください。\n${list}`, { value: "1", ok: "戻す" });
      const n = Number(pick);
      if (!pick || !Number.isInteger(n) || n < 1 || n > res.trash.length) return;
      const done = await api.post(`/api/memo/sheets/${res.trash[n - 1].id}/restore`, {});
      await refreshSheets(done.sheets);
      await openSheet(done.sheet.id);
      toast(`「${done.sheet.name}」を戻しました。`, "ok");
    }
  } catch (err) { toastError(err); }
}

// ------------------------------------------------------------------ 印刷
async function print(kind) {
  el.mPrint.value = "";
  if (!kind) return;
  if (editing) commitEdit();
  if (!(await save()) && dirty()) { toast("保存できていないので、印刷は保存してからにしてください。", "warn"); return; }
  let url = `/memo/print?sheet=${kind === "all" ? "all" : sheet.id}`;
  if (kind === "range") { const s = range(); url += `&range=${s.r0},${s.c0},${s.r1},${s.c1}`; }
  if (!openWindow(tokenUrl(url), "メモの印刷")) toast("別の窓を開けませんでした。ポップアップの許可を確認してください。", "warn");
}

// ------------------------------------------------------------------ 始める
export async function start(initialSheets, givenLimits) {
  Object.assign(limits, givenLimits || {});
  // 同じ画面へ戻ってきたときのために、前の状態を残さない(モジュールは使い回される)
  sheet = null; cells = new Map(); pending = []; inflight = null; editing = null; dragging = null;
  anchor = { r: 0, c: 0 }; focus = { r: 0, c: 0 }; lastSavedAt = "";
  const signal = nav.pageSignal();
  for (const id of ["mView", "mCells", "mColHead", "mColInner", "mRowHead", "mRowInner", "mCorner", "mSelBox",
                    "mActive", "mEditor", "mAddr", "mValue", "mStatus", "mSave", "mTabs", "mAddSheet", "mSheetMenu",
                    "mMenu", "mBold", "mColor", "mColorApply", "mBg", "mBgApply", "mBorder", "mWrap", "mClearFmt",
                    "mCopy", "mPaste", "mRowIns", "mRowDel", "mColIns", "mColDel", "mPrint", "mGrid"]) {
    el[id] = document.getElementById(id);
  }
  sheets = initialSheets || [];
  const editor = el.mEditor;

  el.mView.addEventListener("scroll", () => draw());
  window.addEventListener("resize", () => draw(), { signal });
  el.mView.addEventListener("mousedown", (event) => {
    if (event.button !== 0) return;
    if (editing) commitEdit();
    const p = cellAt(event);
    if (event.shiftKey) { focus = p; } else { anchor = p; focus = p; }
    dragging = { kind: "cells" };
    draw();
    event.preventDefault();
    editor.focus({ preventScroll: true });
  });
  el.mView.addEventListener("dblclick", () => startEdit(false));
  el.mColHead.addEventListener("mousedown", (event) => {
    const grip = event.target.closest("[data-colsize]");
    if (grip) { startResize("colsize", Number(grip.dataset.colsize), event); return; }
    const hd = event.target.closest("[data-col]");
    if (!hd) return;
    if (editing) commitEdit();
    const c = Number(hd.dataset.col);
    if (event.shiftKey) focus = { r: sheet.rows - 1, c }; else { anchor = { r: 0, c }; focus = { r: sheet.rows - 1, c }; }
    dragging = { kind: "col" };
    draw();
    event.preventDefault();
    editor.focus({ preventScroll: true });
  });
  el.mRowHead.addEventListener("mousedown", (event) => {
    const grip = event.target.closest("[data-rowsize]");
    if (grip) { startResize("rowsize", Number(grip.dataset.rowsize), event); return; }
    const hd = event.target.closest("[data-row]");
    if (!hd) return;
    if (editing) commitEdit();
    const r = Number(hd.dataset.row);
    if (event.shiftKey) focus = { r, c: sheet.cols - 1 }; else { anchor = { r, c: 0 }; focus = { r, c: sheet.cols - 1 }; }
    dragging = { kind: "row" };
    draw();
    event.preventDefault();
    editor.focus({ preventScroll: true });
  });
  el.mCorner.addEventListener("click", () => {
    anchor = { r: 0, c: 0 }; focus = { r: sheet.rows - 1, c: sheet.cols - 1 }; draw(); editor.focus();
  });
  window.addEventListener("mousemove", onPointerMove, { signal });
  window.addEventListener("mouseup", onPointerUp, { signal });
  el.mRowHead.addEventListener("wheel", (e) => { el.mView.scrollTop += e.deltaY; }, { passive: true });
  el.mColHead.addEventListener("wheel", (e) => { el.mView.scrollLeft += e.deltaX || e.deltaY; }, { passive: true });

  editor.addEventListener("keydown", onKey);
  // 打ち始めたら編集になる(日本語の変換も、変換を始めた時点で編集になる)
  editor.addEventListener("compositionstart", () => { if (!editing) startEdit(true, ""); });
  editor.addEventListener("input", () => {
    if (!editing) startEdit(true, "");
    placeEditor();
  });
  editor.addEventListener("copy", (event) => {
    if (editing) return;
    event.preventDefault();
    const data = copyData();
    event.clipboardData.setData("text/plain", data.text);
    event.clipboardData.setData("text/html", data.html);
  });
  editor.addEventListener("cut", (event) => {
    if (editing) return;
    event.preventDefault();
    const data = copyData();
    event.clipboardData.setData("text/plain", data.text);
    event.clipboardData.setData("text/html", data.html);
    clearSelection();
  });
  editor.addEventListener("paste", (event) => {
    if (editing) return;
    event.preventDefault();
    pasteText(event.clipboardData.getData("text/plain"));
  });

  // 帯
  const back = () => editor.focus({ preventScroll: true });
  el.mBold.addEventListener("click", () => { toggleBold(); back(); });
  el.mColorApply.addEventListener("click", () => { format((f) => { f.color = el.mColor.value; }); back(); });
  el.mBgApply.addEventListener("click", () => { format((f) => { f.bg = el.mBg.value; }); back(); });
  el.mColor.addEventListener("change", () => { format((f) => { f.color = el.mColor.value; }); back(); });
  el.mBg.addEventListener("change", () => { format((f) => { f.bg = el.mBg.value; }); back(); });
  el.mBorder.addEventListener("change", () => { if (el.mBorder.value) border(el.mBorder.value); el.mBorder.value = ""; back(); });
  document.querySelectorAll("[data-align]").forEach((b) => b.addEventListener("click", () => {
    format((f) => { f.align = f.align === b.dataset.align ? undefined : b.dataset.align; });
    back();
  }));
  el.mWrap.addEventListener("click", () => {
    const cur = cells.get(key(focus.r, focus.c));
    const on = !(cur && cur.f && cur.f.wrap);
    format((f) => { f.wrap = on; });
    back();
  });
  el.mClearFmt.addEventListener("click", () => { format((f) => { for (const k of Object.keys(f)) delete f[k]; }); back(); });
  el.mCopy.addEventListener("click", async () => {
    const data = copyData();
    try {
      if (window.ClipboardItem && navigator.clipboard.write) {
        await navigator.clipboard.write([new ClipboardItem({
          "text/plain": new Blob([data.text], { type: "text/plain" }),
          "text/html": new Blob([data.html], { type: "text/html" }) })]);
      } else {
        await navigator.clipboard.writeText(data.text);
      }
      toast("コピーしました(Excel に貼り付けられます)。", "ok");
    } catch { toast("コピーできませんでした。Ctrl+C を使ってください。", "warn"); }
    back();
  });
  el.mPaste.addEventListener("click", async () => {
    try { pasteText(await navigator.clipboard.readText()); }
    catch { toast("ここからは貼り付けられませんでした。Ctrl+V を使ってください。", "warn"); }
    back();
  });
  el.mRowIns.addEventListener("click", () => { structural("insert_rows"); back(); });
  el.mRowDel.addEventListener("click", () => { structural("delete_rows"); back(); });
  el.mColIns.addEventListener("click", () => { structural("insert_cols"); back(); });
  el.mColDel.addEventListener("click", () => { structural("delete_cols"); back(); });
  el.mSave.addEventListener("click", () => { if (editing) commitEdit(); save(true); back(); });
  el.mPrint.addEventListener("change", () => print(el.mPrint.value));
  el.mAddSheet.addEventListener("click", () => addSheet());
  el.mSheetMenu.addEventListener("click", () => { el.mMenu.hidden = !el.mMenu.hidden; });
  el.mMenu.addEventListener("click", (event) => {
    const b = event.target.closest("[data-sheet]");
    if (b) sheetAction(b.dataset.sheet);
  });
  document.addEventListener("keydown", (event) => {
    // 帯のボタンを押したあとでも Ctrl+S は効かせる
    if ((event.ctrlKey || event.metaKey) && (event.key === "s" || event.key === "S") && event.target !== editor) {
      event.preventDefault();
      if (editing) commitEdit();
      save(true);
    }
  }, { signal });
  wireLeave(signal);

  let first = sheets[0] && sheets[0].id;
  try {
    const last = Number(window.localStorage.getItem(LAST_KEY));
    if (sheets.some((s) => s.id === last)) first = last;
  } catch { /* */ }
  drawTabs();
  if (first) await openSheet(first);
}

// 試験用
export const _test = { get sheet() { return sheet; }, get cells() { return cells; }, get pending() { return pending; },
                       range, select, setCells, save, structural };
