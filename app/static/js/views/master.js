/*
  マスタ管理(設定画面の「マスタ管理」の面)。

  梱包資材マスタは、これまで起動のたびに**黙って読まれるだけ**だった。
  中を見る手立ても、間違いを直す手立ても画面に無かったので、値が
  違っていても現場には「パレットが出ない」としか見えなかった。

  【判断はサーバが済ませてある】
  どの表を直せるか・誰が直せるか・値の形が合っているかは、すべて
  `packaging_tool/master_admin.py` が決める。ここは受け取ったものを
  並べ、押されたら投げ、返ってきたものを描き直すだけ(設計.md §1)。

  【共有フォルダに触るのは開いたときだけ】
  設定画面は毎日何度も開く。開くたびに共有の sqlite3 を数えに行くと、
  設定を1つ変えたいだけの人まで待たされる。だから**この面を開いた
  最初の1回**で読む。
*/

import { api } from "../api.js";
import { toast, toastError } from "../toast.js";

const el = {};
let view = null;          // サーバが返した最後の状態。**画面の唯一の出どころ**
let loaded = false;
let editing = null;       // いま開いている行(足すときは null)

const IDS = ["mTables", "mMark", "mTitle", "mCan", "mCount", "mQuery", "mFind",
             "mAdd", "mCreate", "mReload", "mWhy", "mError", "mHead", "mRows", "mNote",
             "mEdit", "mEditTitle", "mEditKind", "mEditWhy", "mEditError",
             "mFields", "mFoot", "mSave", "mDelete", "mConfirm",
             "mDeleteYes", "mDeleteNo"];

// ------------------------------------------------------------------
export function start(frame) {
  for (const id of IDS) el[id] = document.getElementById(id);
  if (!el.mTables) return;
  loaded = false;
  editing = null;
  // 権限は最初の描画で分かっている(共有に触らずに出せる)
  view = frame;
  showWhy();

  el.mFind.addEventListener("click", () => load(view && view.table));
  el.mQuery.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); load(view && view.table); }
  });
  el.mReload.addEventListener("click", () => load(view && view.table));
  el.mAdd.addEventListener("click", () => openRow(null));
  el.mCreate.addEventListener("click", createTable);

  el.mRows.addEventListener("click", (event) => {
    const tr = event.target.closest("tr[data-key]");
    if (tr) openRow(tr.dataset.key);
  });
  el.mTables.addEventListener("click", (event) => {
    const row = event.target.closest("[data-table]");
    // 表を変えたら絞り込みは外す。前の表の言葉で絞ったままにすると
    // 「0件」だけが出て、なぜ空なのか分からない
    if (row) { el.mQuery.value = ""; load(row.dataset.table); }
  });

  el.mSave.addEventListener("click", saveRow);
  el.mDelete.addEventListener("click", () => askDelete(true));
  el.mDeleteNo.addEventListener("click", () => askDelete(false));
  el.mDeleteYes.addEventListener("click", deleteRow);
}

/** 面が開かれた。**最初の1回だけ**読む。 */
export function opened() {
  if (loaded) return;
  loaded = true;
  load("");
}

/**
 * 取り込みが終わって、**手元の中身が入れ替わった**。
 *
 * 取り込みは総入れ替えなので、一度読んだ一覧はその時点で古い。
 * 開いたまま取り込むと、画面には消えたはずの行が残り、入ったはずの
 * 行が出ない ── 現場の声:「取り込みなおしてもマスタが表示されない。
 * 違うタブに行って戻ると見られるようになる」。
 *
 * まだ一度も開いていない面のために共有フォルダを叩かない(§4.6)ので、
 * **読んであるときだけ**読み直す。編集の途中なら触らない ── 打ちかけの
 * 内容を、本人の操作でもないもので消さない。
 */
export function reload() {
  if (!loaded || editing) return;
  load(view && view.table);
}

// ------------------------------------------------------------------
// 読む
// ------------------------------------------------------------------
async function load(table) {
  const query = el.mQuery.value.trim();
  const params = new URLSearchParams({ table: table || "", q: query });
  try {
    render(await api.get(`/api/master/browse?${params}`));
  } catch (err) {
    toastError(err);
  }
}

function render(next) {
  view = next;
  const page = view.page || {};

  // --- 左: 表の一覧 ---
  const items = (view.tables || []).map((info) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "mrow";
    row.dataset.table = info.table;
    if (info.table === view.table) row.setAttribute("aria-current", "true");

    const mark = document.createElement("i");
    mark.className = `ib ib--sm ib--${info.editable ? "accent" : ""}`;
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = info.mark;

    const name = document.createElement("span");
    name.className = "mrow__name";
    name.textContent = info.label;
    // 押す前に、その先に何があるかを示す(§2.4)
    name.title = info.note || info.why;

    const count = document.createElement("span");
    count.className = "mrow__n";
    count.textContent = info.rows < 0 ? "?" : String(info.rows);

    row.append(mark, name, count);
    return row;
  });
  if (!items.length) {
    const note = document.createElement("p");
    note.className = "why";
    note.textContent = view.source
      ? "取り込み元に表がありません。"
      : "梱包資材マスタが見つかりません。「取り込み元」の面で置き場所を確かめてください。";
    items.push(note);
  }
  el.mTables.replaceChildren(...items);

  // --- 右: 見出し ---
  const info = (view.tables || []).find((t) => t.table === view.table);
  el.mMark.textContent = info ? info.mark : "材";
  el.mTitle.textContent = page.label || view.table || "梱包資材マスタ";
  el.mCount.textContent = page.total
    ? `${page.shown} / ${page.total} 件` : `${page.total || 0} 件`;

  const editable = Boolean(view.can_edit && page.editable);
  // 取り込み元にまだ無い表。**行を足す前に、表そのものを作る**
  const missing = Boolean(page.missing);
  el.mCan.hidden = !view.table;
  el.mCan.className = `st st--${missing ? "warn" : (editable ? "ok" : "warn")}`;
  el.mCan.textContent = missing ? "まだありません" : (editable ? "直せます" : "見るだけ");
  // 表が無いあいだは行を足せない。押せる形にしておくと、押した先で
  // 「入れる値がありません」としか言えず、何が足りないのか分からない
  el.mAdd.disabled = !editable || missing;
  el.mCreate.hidden = !missing;
  el.mCreate.disabled = !view.can_edit;

  showWhy();
  el.mError.hidden = !page.error;
  el.mError.textContent = page.error || "";

  // --- 右: 表 ---
  const columns = page.columns || [];
  const head = document.createElement("tr");
  for (const name of columns) {
    const th = document.createElement("th");
    th.textContent = name;
    head.appendChild(th);
  }
  el.mHead.replaceChildren(head);

  el.mRows.replaceChildren(...(page.rows || []).map((row) => {
    const tr = document.createElement("tr");
    tr.dataset.key = String(row[view.row_key]);
    for (const name of columns) {
      const td = document.createElement("td");
      td.textContent = text(row[name]);
      tr.appendChild(td);
    }
    return tr;
  }));

  // 出しきれなかった分は**黙って落とさない**(文言はサーバが持つ)
  el.mNote.hidden = !page.note;
  el.mNote.textContent = page.note || "";

  if (view.message) toast(view.message, "ok");
}

/** 直せない理由。**押してから断られる**より先に言う。 */
function showWhy() {
  const page = (view && view.page) || {};
  const why = !view || view.can_edit ? (page.why || "") : view.edit_why;
  el.mWhy.hidden = !why;
  el.mWhy.textContent = why || "";
}

function text(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

// ------------------------------------------------------------------
// 1行を開く
// ------------------------------------------------------------------
function openRow(key) {
  const page = (view && view.page) || {};
  const row = key === null ? {}
    : (page.rows || []).find((r) => String(r[view.row_key]) === String(key));
  if (key !== null && !row) return;

  editing = key === null ? null : key;
  const editable = Boolean(view.can_edit && page.editable);
  const columns = view.columns || [];

  el.mEditTitle.textContent = key === null
    ? `${page.label} に1行足す` : `${page.label} の1行`;
  el.mEditKind.className = `st st--${editable ? "ok" : "warn"}`;
  el.mEditKind.textContent = editable ? "直せます" : "見るだけ";
  el.mEditError.hidden = true;
  el.mSave.textContent = key === null ? "取り込み元へ足す" : "取り込み元へ書く";
  // 足すときに「消す」は意味を成さない
  el.mDelete.hidden = key === null;
  el.mFoot.hidden = !editable;
  askDelete(false);

  const why = view.can_edit ? page.why : view.edit_why;
  el.mEditWhy.hidden = !why || editable;
  el.mEditWhy.textContent = why || "";

  // 直せないなら**打ち込める形にしない**。読めない欄を出すと、打てると
  // 思って打ち、押してから断られる。読むだけの表は文字のまま出す
  el.mFields.replaceChildren(
    ...(editable && columns.length ? fields(columns, row) : plain(page, row)));
  el.mEdit.showModal();
  const first = el.mFields.querySelector("input:not([readonly])");
  if (first) first.focus();
}

/** 直せる列を、打ち込める形で並べる。 */
function fields(columns, row) {
  const out = [];
  for (const column of columns) {
    const label = document.createElement("label");
    label.setAttribute("for", `mf-${column.name}`);
    label.className = column.required ? "req" : "";
    label.textContent = column.name;

    const box = document.createElement("div");
    const input = document.createElement("input");
    input.className = "input";
    input.id = `mf-${column.name}`;
    input.dataset.column = column.name;
    input.value = text(row[column.name]);
    input.spellcheck = false;
    input.autocomplete = "off";
    box.appendChild(input);

    // 何を入れる欄なのかを添える。言い方はサーバが持っている
    const hint = [column.kind_label, column.note].filter(Boolean).join(" / ");
    if (hint) {
      const small = document.createElement("small");
      small.className = "mhint";
      small.textContent = hint;
      box.appendChild(small);
    }
    out.push(label, box);
  }
  return out;
}

/** 直す対象ではない表。**読むためだけ**に、そのまま出す。 */
function plain(page, row) {
  const out = [];
  for (const name of page.columns || []) {
    const label = document.createElement("label");
    label.textContent = name;
    const value = document.createElement("span");
    value.className = "mval";
    value.textContent = text(row[name]);
    out.push(label, value);
  }
  return out;
}

function values() {
  const out = {};
  for (const input of el.mFields.querySelectorAll("input[data-column]")) {
    out[input.dataset.column] = input.value;
  }
  return out;
}

function askDelete(on) {
  el.mFoot.hidden = on || !(view && view.can_edit && view.page
                            && view.page.editable);
  el.mConfirm.hidden = !on;
}

// ------------------------------------------------------------------
// 書く
// ------------------------------------------------------------------
async function send(path, body) {
  try {
    render(await api.post(path, {
      table: view.table, q: el.mQuery.value.trim(), ...body,
    }));
    el.mEdit.close();
    return true;
  } catch (err) {
    // 断られても本文には**いまの状態**が入っている。理由を出したうえで
    // 画面も追いつかせる ── 「断られた」と「画面が古い」を同時に起こさない
    if (err.body && err.body.page) render({ ...err.body, message: "" });
    el.mEditError.hidden = false;
    el.mEditError.textContent = err.message;
    toastError(err);
    return false;
  }
}

function saveRow() {
  if (editing === null) return void send("/api/master/row/add",
                                         { values: values() });
  return void send("/api/master/row/save",
                   { key: Number(editing), values: values() });
}

function deleteRow() {
  return void send("/api/master/row/delete", { key: Number(editing) });
}

/** 取り込み元にその表を作る。**帯の中の操作**なので、断りも帯に出す。

    `send` は行の編集用で、断りを編集ダイアログの中へ出す。ここは
    ダイアログを開かずに押すボタンなので、同じ所へ出すと**誰にも
    見えない場所に理由が入る**。 */
async function createTable() {
  const table = view && view.table;
  if (!table) return;
  try {
    render(await api.post("/api/master/table/create",
                          { table, q: el.mQuery.value.trim() }));
  } catch (err) {
    if (err.body && err.body.page) render({ ...err.body, message: "" });
    el.mError.hidden = false;
    el.mError.textContent = err.message;
    toastError(err);
  }
}
