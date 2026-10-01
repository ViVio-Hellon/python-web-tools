/*
  ログ(設定画面の「ログ」の面)。

  エラー記録を新しい順に並べ、1件を押すと中身(どの操作の途中か・直前の動き・
  同じ操作番号の行・トレースバック)を出す。「なぜなぜの下書きをコピー」で
  報告やなぜなぜ分析の紙に貼れる文にする。文はサーバが作る(`trace_log`)。

  **開いたときに読む。** 設定画面は毎日開くので、見ない面のために
  共有フォルダのログを読みに行かない。
*/

import { api } from "../api.js";
import { toast, toastError } from "../toast.js";

const el = {};
let loaded = false;
let current = null;           // いま中身を出している1件

function $(id) { return document.getElementById(id); }

export function start() {
  for (const id of ["errRows", "errEmpty", "errCount", "errQuery", "errFind", "errEveryone",
                    "errEveryoneWrap", "errReload", "errDetailCard", "errDetailTitle", "errFacts",
                    "errRecent", "errOpLines", "errOpId", "errDetail", "errCopy",
                    "logDir", "logDirPassword", "logDirSave", "logDirWhy", "logDirNow",
                    "logDirSource", "logDirWarn"]) {
    el[id] = $(id);
  }
  if (!el.errRows) return;
  el.errFind.addEventListener("click", load);
  el.errReload.addEventListener("click", load);
  el.errEveryone.addEventListener("change", load);
  el.errQuery.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); load(); }
  });
  el.errRows.addEventListener("click", (event) => {
    const tr = event.target.closest("tr[data-ref]");
    if (tr) openError(tr.dataset.ref);
  });
  el.errCopy.addEventListener("click", copyWhyWhy);
  el.logDirSave.addEventListener("click", saveLogDir);
}

/** 面が開かれた。**最初の1回だけ**読む(読み直しはボタンで)。 */
export function opened() {
  if (loaded || !el.errRows) return;
  loaded = true;
  load();
}

async function load() {
  const params = new URLSearchParams({
    q: el.errQuery.value.trim(),
    everyone: el.errEveryone.checked ? "1" : "0",
  });
  try {
    render(await api.get(`/api/trace/errors?${params}`));
  } catch (err) {
    toastError(err);
  }
}

function cell(text, className = "t") {
  const td = document.createElement("td");
  td.className = className;
  td.textContent = text;
  return td;
}

function render(body) {
  const items = body.items || [];
  el.errRows.replaceChildren(...items.map((item) => {
    const tr = document.createElement("tr");
    tr.dataset.ref = item.ref;
    tr.tabIndex = 0;
    tr.className = "clickable";
    tr.append(cell(item.at), cell(item.ref), cell(item.pc), cell(item.screen),
              cell(item.message));
    tr.addEventListener("keydown", (event) => {
      if (event.key === "Enter") openError(item.ref);
    });
    return tr;
  }));
  el.errEmpty.hidden = items.length > 0;
  el.errCount.textContent = items.length ? `${items.length}件` : "";
  renderLogDir(body.log_dir);
}

async function openError(ref) {
  try {
    current = await api.get(`/api/trace/errors/${encodeURIComponent(ref)}`);
  } catch (err) {
    toastError(err);
    return;
  }
  const op = current.operation || {};
  el.errDetailTitle.textContent = `エラー ${current.ref}`;
  const facts = [
    ["いつ", current.at],
    ["どの端末で", `${current.pc || ""}(利用者 ${current.login || "?"}・版 ${current.version || "?"})`],
    ["どの操作で", current.screen || "(画面の操作の外)"],
    ["開いていた画面", op.page || ""],
    ["そのときの入力", op.input || ""],
    ["起きたこと", current.message],
    ["出どころ", current.where],
  ].filter(([, value]) => value);
  el.errFacts.replaceChildren(...facts.flatMap(([key, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    return [dt, dd];
  }));
  el.errRecent.textContent = (current.recent || []).join("\n") || "(記録なし)";
  el.errOpId.textContent = op.id || "なし";
  el.errOpLines.textContent = (current.operation_lines || []).join("\n")
    || "(この操作の行は見つかりません。ログファイルが消えたか、別の場所に書いていた頃のものです)";
  el.errDetail.textContent = current.detail || "(なし)";
  el.errDetailCard.hidden = false;
  // 新しいものが下。いちばん大事な「エラーの直前」を最初から見せる
  el.errRecent.scrollTop = el.errRecent.scrollHeight;
  el.errDetailCard.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function copyWhyWhy() {
  if (!current) return;
  const text = current.why_why || "";
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // クリップボードが使えないブラウザ。選んだ状態にして Ctrl+C を頼む
    const area = document.createElement("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    if (!ok) { toast("コピーできませんでした。下の文を選んでコピーしてください。", "ng"); return; }
  }
  toast(`エラー ${current.ref} のなぜなぜの下書きをコピーしました`, "ok");
}

function renderLogDir(state) {
  if (!state) return;
  el.logDirNow.textContent = state.path;
  el.logDirSource.textContent = state.source_label;
  el.logDirWarn.hidden = !state.warn;
  el.logDirWarn.textContent = state.warn || "";
  el.errEveryoneWrap.hidden = state.source !== "setting";
}

async function saveLogDir() {
  el.logDirWhy.hidden = true;
  try {
    const body = await api.post("/api/trace/log-dir", {
      path: el.logDir.value, password: el.logDirPassword.value,
    });
    el.logDirPassword.value = "";
    renderLogDir(body.log_dir);
    toast(body.message, "ok");
  } catch (err) {
    el.logDirWhy.hidden = false;
    el.logDirWhy.textContent = err.message;
    toastError(err);
  }
}
