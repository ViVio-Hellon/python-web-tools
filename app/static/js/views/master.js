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
import * as nav from "../nav.js";

const el = {};
let view = null;          // サーバが返した最後の状態。**画面の唯一の出どころ**
let loaded = false;
let editing = null;       // いま開いている行(足すときは null)

const IDS = ["mTables", "mMark", "mTitle", "mCan", "mCount", "mQuery", "mFind",
             "mAdd", "mCreate", "mRebuild", "mReload", "mWhy", "mError", "mHead",
             "mRows", "mNote",
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

  el.mFind.addEventListener("click", () => loadKeepSort(view && view.table));
  el.mQuery.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); loadKeepSort(view && view.table); }
  });
  el.mReload.addEventListener("click", () => loadKeepSort(view && view.table));
  el.mAdd.addEventListener("click", () => openRow(null));
  el.mCreate.addEventListener("click", createTable);
  el.mRebuild.addEventListener("click", rebuildTable);

  el.mRows.addEventListener("click", (event) => {
    const tr = event.target.closest("tr[data-key]");
    if (tr) openRow(tr.dataset.key);
  });
  el.mTables.addEventListener("click", (event) => {
    const row = event.target.closest("[data-table]");
    // 表を変えたら絞り込みも並び替えも外す。前の表の言葉/列で
    // 絞ったままにすると、「0件」や意味の無い並びだけが出て、
    // なぜそうなっているのか分からない
    if (row) { el.mQuery.value = ""; load(row.dataset.table); }
  });

  // 見出しクリックで並び替え。**サーバが並べ替えた結果を描き直すだけ**
  // (設計.md §1) ── ここでJS側にソートを持たない
  el.mHead.addEventListener("click", (event) => {
    const button = event.target.closest("button.sortbtn");
    if (button) sortBy(button.dataset.column);
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
  loadKeepSort(view && view.table);
}

// ------------------------------------------------------------------
// 読む
// ------------------------------------------------------------------
async function load(table, sort, sortDir) {
  const query = el.mQuery.value.trim();
  const params = new URLSearchParams({
    table: table || "", q: query,
    sort: sort || "", sort_dir: sortDir || "asc",
  });
  try {
    render(await api.get(`/api/master/browse?${params}`));
  } catch (err) {
    toastError(err);
  }
}

/** いま出している表の並び替えを保ったまま読み直す。 */
function loadKeepSort(table) {
  const page = (view && view.page) || {};
  load(table, page.sort, page.sort_dir);
}

/** 見出しを押した。同じ列なら向きを反転、違う列なら昇順から。 */
function sortBy(column) {
  const page = (view && view.page) || {};
  const dir = (page.sort === column && page.sort_dir === "asc") ? "desc" : "asc";
  load(view && view.table, column, dir);
}

/** 見出し1列ぶん。押すと並び替え(`lotlist.js` と同じ作法の `sortbtn`)。 */
function headerCell(name, page) {
  const th = document.createElement("th");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "sortbtn";
  button.dataset.column = name;
  button.append(name);
  if (page.sort === name) {
    const mark = document.createElement("span");
    mark.className = "sortmark";
    // ▲▼は文字なので、色を拾えない環境でも並び順が読める
    mark.textContent = page.sort_dir === "asc" ? "▲" : "▼";
    button.append(mark);
    th.setAttribute("aria-sort", page.sort_dir === "asc" ? "ascending" : "descending");
  }
  th.append(button);
  return th;
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
  // 表はあるが列名が想定と違い、1つも打ち込めない
  const rebuildable = Boolean(page.rebuildable);
  el.mCan.hidden = !view.table;
  el.mCan.className = `st st--${(missing || rebuildable) ? "warn" : (editable ? "ok" : "warn")}`;
  el.mCan.textContent = missing ? "まだありません"
    : rebuildable ? "列名が違います" : (editable ? "直せます" : "見るだけ");
  // 表が無い/列名が違うあいだは行を足せない。押せる形にしておくと、
  // 押した先で「入れる値がありません」としか言えず、何が足りないのか
  // 分からない
  el.mAdd.disabled = !editable || missing || rebuildable;
  el.mCreate.hidden = !missing;
  el.mCreate.disabled = !view.can_edit;
  el.mRebuild.hidden = !rebuildable;
  el.mRebuild.disabled = !view.can_edit;

  showWhy();
  el.mError.hidden = !page.error;
  el.mError.textContent = page.error || "";

  // --- 右: 表 ---
  const columns = page.columns || [];
  const head = document.createElement("tr");
  for (const name of columns) head.appendChild(headerCell(name, page));
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

  // 表がまだ無い/列名が想定と違うあいだは、**新規に行を足すダイアログを
  // 開かせない**。開けてしまうと、`page.columns` が空のまま
  // (`master_admin.page()` は表が無ければ列を読まずに返す)なので、
  // 入力欄が1つも無いのに保存だけは押せてしまい、「入れる値がありません」
  // という的外れな断りになる(現場の声)。行を足す前に、まず表を
  // 作る/作り直すよう、その場で伝える
  if (key === null && (page.missing || page.rebuildable)) {
    toast(page.missing
      ? "先に表を「取り込み元に作る」で作ってください。"
      : "先に「列名を直して作り直す」で表を作り直してください。", "warn");
    return;
  }

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

  // 打ち込める欄が無いなら、直せる表(`editable`)でも理由を隠さない。
  // 「表はある。列名が想定と違うので1つも打ち込めない」という状況は
  // 従来の「直せない表」とは別物で、ここで隠すと空の窓だけが残る
  const noFields = editable && columns.length === 0;
  const why = view.can_edit ? page.why : view.edit_why;
  el.mEditWhy.hidden = !why || (editable && !noFields);
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
/** 権限の表。ここを直したら帯を取り直す(下記 `send`)。 */
const ACCESS_TABLE = "アクセス権限";

async function send(path, body) {
  const table = view.table;
  const page = (view && view.page) || {};
  try {
    render(await api.post(path, {
      table, q: el.mQuery.value.trim(),
      sort: page.sort || "", sort_dir: page.sort_dir || "asc",
      ...body,
    }));
    el.mEdit.close();
    // **足した権限をその場で効かせる。** 帯のモード切替は「いま持って
    // いる権限」で作られるが、作られるのは画面を出すときだけ。この
    // 画面に留まったままだと帯が古いままで、**開き直すまでモードを
    // 切り替えられない**ように見える(現場の声)。
    // 中身(`<main>`)は差し替えないので、開いている面も入力も残る
    if (table === ACCESS_TABLE) nav.refreshShell();
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

/** 列名が想定と違う表を、正しい列名で作り直す。

    sqlite3 のファイルは他に直す手段が無い(テキストエディタでは
    開けない)ことが多いので、ここから完結させる。元の表は消さず
    退避するが、**表名が変わる**という後戻りしにくい操作なので、
    押す前に一度だけ確かめる。 */
async function rebuildTable() {
  const table = view && view.table;
  if (!table) return;
  const ok = window.confirm(
    `${table} を正しい列名で作り直します。\n` +
    "今の表は消さず、別の名前(◯◯_旧_日時)で残ります。\n" +
    "よろしいですか?");
  if (!ok) return;
  try {
    render(await api.post("/api/master/table/rebuild",
                          { table, q: el.mQuery.value.trim() }));
  } catch (err) {
    if (err.body && err.body.page) render({ ...err.body, message: "" });
    el.mError.hidden = false;
    el.mError.textContent = err.message;
    toastError(err);
  }
}
