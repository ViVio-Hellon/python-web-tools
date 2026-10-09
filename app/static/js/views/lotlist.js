/**
 * 仕掛一覧 ── 絞り込んで、行を押して選ぶ
 *
 * **判断はサーバが持つ。** どの列で絞れるか・どの演算子が使えるか・
 * いま何件中何件を出しているか・0件の理由は、すべて `lot_query.py` と
 * `presenters/lot_list.py` が決めて返す。ここは受け取った値を並べ、
 * 押されたことをそのまま投げ返すだけ。
 *
 * 条件を画面側で覚えないのが要点。覚えると、サーバが条件を外したことが
 * 画面に伝わらず「条件が出ているのに効いていない」状態を作れる。
 */
import { api } from "../api.js";
import { promptBox } from "../askbox.js";
import { onLeave, pageSignal } from "../nav.js";
import { toast, toastError } from "../toast.js";

const SUGGEST_DEBOUNCE_MS = 160;

const el = {};
let onOpen = null;        // 行を開いたときに呼ぶ(ダブルクリック / Enter)
let lastView = null;     // いま出している一覧(検索欄の Enter で、1件なら開く)
let suggestTimer = null;
let suggestItems = [];
let suggestAt = -1;       // キーボードで選んでいる位置

/* ================================================================
   描画
   ================================================================ */
function headerCell(header) {
  const th = document.createElement("th");
  // 数字は右、文字は左。桁を比べるのは数字だけなので、文字まで
  // 右に寄せると行の頭がそろわず読みにくい
  th.className = header.numeric ? "n" : "t";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "sortbtn";
  button.dataset.column = header.key;
  button.append(header.label);
  if (header.sorted) {
    const mark = document.createElement("span");
    mark.className = "sortmark";
    // ▲▼は文字なので、色を拾えない環境でも並び順が読める
    mark.textContent = header.sorted === "asc" ? "▲" : "▼";
    button.append(mark);
    th.setAttribute("aria-sort",
      header.sorted === "asc" ? "ascending" : "descending");
  }
  th.append(button);
  return th;
}

function bodyRow(row, headers) {
  const tr = document.createElement("tr");
  tr.className = "pickable";
  tr.dataset.lotNo = row.lot_no;
  // `busy.js` がこの目印を見て、開いている間の行を待機の姿にする
  // (ダブルクリックで `openLot` がサーバへ取りに行く)
  tr.dataset.rowAction = "1";
  tr.tabIndex = 0;
  // ダブルクリックが唯一の道にならないようにする。キーボードでも
  // 触っても同じことができる(§5 タッチ対応 / 認識は再生に勝る)
  tr.setAttribute("role", "button");
  tr.title = `${row.lot_no} の詳細を開く`;
  if (row.current) {
    tr.classList.add("is-current");
    tr.setAttribute("aria-current", "true");
  }
  row.values.forEach((value, index) => {
    const td = document.createElement("td");
    td.className = (headers[index] && headers[index].numeric) ? "n" : "t";
    td.textContent = value;
    tr.append(td);
  });
  return tr;
}

function conditionChip(condition, index) {
  const span = document.createElement("span");
  span.className = "fchip";
  span.append(condition.label);
  const x = document.createElement("button");
  x.type = "button";
  x.className = "fchip__x";
  x.dataset.index = String(index);
  x.setAttribute("aria-label", `${condition.label} を外す`);
  x.textContent = "×";
  span.append(x);
  return span;
}

function savedChip(name) {
  const span = document.createElement("span");
  span.className = "fchip fchip--saved";
  const use = document.createElement("button");
  use.type = "button";
  use.className = "fchip__use";
  use.dataset.name = name;
  use.textContent = name;
  const x = document.createElement("button");
  x.type = "button";
  x.className = "fchip__x";
  x.dataset.deleteName = name;
  x.setAttribute("aria-label", `よく使う条件「${name}」を消す`);
  x.textContent = "×";
  span.append(use, x);
  return span;
}

export function render(view) {
  if (!view) return;
  lastView = view;
  el.listCard.hidden = !view.available;
  if (!view.available) return;

  el.lotHead.replaceChildren(...view.headers.map(headerCell));
  el.lotRows.replaceChildren(
    ...view.rows.map((row) => bodyRow(row, view.headers)));

  el.chips.replaceChildren(...view.conditions.map(conditionChip));
  el.chipCount.textContent = `${view.conditions.length}件`;
  el.countNote.textContent = view.count_note;

  el.savedBar.hidden = view.saved.length === 0;
  el.savedChips.replaceChildren(...view.saved.map(savedChip));

  // 入力欄はサーバの値と食い違うときだけ書き換える。無条件に代入すると
  // 打っている途中で消える(資材選択で踏んだのと同じ問題)。
  // **打ち足した文字が送られる前なら書き換えない。** 「A12」の返事が
  // 「3」を打った後に届くと、欄が「A12」に戻り、待っていた検索も
  // 戻った「A12」で送られて「3」が消えていた(現場の声: 文字が差し戻る)
  if (!textPending && el.listText.value !== view.text) el.listText.value = view.text;
  el.pageSize.value = String(view.page_size);

  el.saveFilter.disabled = !view.can_save;
  el.saveWhy.hidden = !view.save_why;
  el.saveWhy.textContent = view.save_why || "";

  el.listEmpty.hidden = !view.empty_why;
  el.listEmpty.textContent = view.empty_why || "";

  // 案内の文言もサーバが持つ。画面ごとに言い回しが割れない
  if (el.searchHint) el.searchHint.textContent = view.search_hint || "";
  if (el.rowHint) el.rowHint.textContent = view.row_hint || "";
}

/* ================================================================
   候補(検索して条件を追加)
   ================================================================ */
function closeSuggest() {
  el.suggest.hidden = true;
  el.suggest.replaceChildren();
  el.condInput.setAttribute("aria-expanded", "false");
  suggestItems = [];
  suggestAt = -1;
}

function markSuggest(at) {
  suggestAt = at;
  [...el.suggest.querySelectorAll("button")].forEach((b, i) => {
    b.classList.toggle("on", i === at);
    if (i === at) b.scrollIntoView({ block: "nearest" });
  });
}

function showSuggest(items) {
  suggestItems = items;
  suggestAt = -1;
  if (!items.length) { closeSuggest(); return; }
  el.suggest.replaceChildren(...items.map((item, index) => {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.index = String(index);
    const mark = document.createElement("span");
    mark.className = "kindmark";
    mark.textContent = item.kind === "saved" ? "保存" : "条件";
    button.append(mark, item.label);
    li.append(button);
    return li;
  }));
  el.suggest.hidden = false;
  el.condInput.setAttribute("aria-expanded", "true");
}

async function loadSuggest() {
  const text = el.condInput.value.trim();
  try {
    const body = await api.get(
      `/api/lot/list/suggest?q=${encodeURIComponent(text)}`);
    showSuggest(body.items || []);
  } catch (err) {
    closeSuggest();
    toastError(err);
  }
}

async function useSuggest(index) {
  const item = suggestItems[index];
  if (!item) return;
  const path = item.kind === "saved"
    ? "/api/lot/list/saved/load" : "/api/lot/list/filter/add";
  const body = item.kind === "saved"
    ? { name: item.name }
    : { column: item.column, op: item.op, value: item.value };
  el.condInput.value = "";
  closeSuggest();
  await send(path, body);
}

/* ================================================================
   送る
   ================================================================ */
// 打った文字がまだ送られていない(間を置いている最中)か
let textPending = false;
// 送った順の番号。**追い越された返事は画面に出さない** ── 打つそばから
// 検索するので、先に送った「A12」の返事が後の「A123」の返事より遅れて
// 届くことがある。そのまま出すと一覧も入力欄も古いほうに戻る
let sendSeq = 0;
// **サーバへは1つずつ順に送る。** 検索の文字はサーバが覚えるので、同時に
// 送るとサーバ側で順番が入れ替わり、覚えた文字が古いほうになることがある
let sendChain = Promise.resolve();

function send(path, body = {}) {
  const seq = ++sendSeq;
  const run = sendChain.then(() => sendNow(path, body, seq));
  sendChain = run;
  return run;
}

async function sendNow(path, body, seq) {
  try {
    const next = await api.post(path, body);
    // 後から送ったぶんがある / 打ち足した文字をこれから送る → この返事は古い。
    // 出すと一覧が古い文字のものになり、1件に絞れたときは打っている途中で開いてしまう
    if (seq !== sendSeq || textPending) return next;
    render(next);
    if (next.message) toast(next.message, "ok");
    return next;
  } catch (err) {
    if (seq !== sendSeq || textPending) return null;
    // 422 は「一覧ぜんぶ + 断りの文言」が返る。表を消さずに理由だけ出す
    if (err.body && err.body.headers) render(err.body);
    toastError(err);
    return null;
  }
}

/* ================================================================
   起動
   ================================================================ */
export function start(options) {
  for (const id of ["listCard", "lotHead", "lotRows", "chips", "chipCount",
                    "countNote", "listText", "pageSize", "condInput",
                    "suggest", "saveFilter", "saveWhy", "clearFilter",
                    "savedBar", "savedChips", "listEmpty",
                    "searchHint", "rowHint"]) {
    el[id] = document.getElementById(id);
  }
  onOpen = options.onOpen;
  lastView = null;
  // 再入場のたびに真っさらから(`nav.js`)
  suggestItems = [];
  suggestAt = -1;
  textPending = false;
  render(options.view);

  // --- 行を開く ---
  // 1回押しただけでは開かない。押しながら一覧を目で追うことがあるので、
  // **開くのは意図した操作のときだけ**にする(ダブルクリック / Enter)。
  // 1回押しは選ぶだけ = キーボードの現在地が移るのと同じ扱い
  const open = (target) => {
    const tr = target.closest("tr.pickable");
    if (tr && onOpen) onOpen(tr.dataset.lotNo);
  };
  el.lotRows.addEventListener("dblclick", (e) => open(e.target));
  el.lotRows.addEventListener("click", (e) => {
    const tr = e.target.closest("tr.pickable");
    if (tr) tr.focus();
  });
  el.lotRows.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(e.target); return; }
    // 上下で行を移る。1行ずつ確かめながら探せるようにする
    const tr = e.target.closest("tr.pickable");
    if (!tr) return;
    const next = e.key === "ArrowDown" ? tr.nextElementSibling
               : e.key === "ArrowUp" ? tr.previousElementSibling : null;
    if (next) { e.preventDefault(); next.focus(); }
  });

  // --- 並べ替え ---
  el.lotHead.addEventListener("click", (e) => {
    const button = e.target.closest("button.sortbtn");
    if (button) send("/api/lot/list/sort", { column: button.dataset.column });
  });

  // --- 条件を外す / よく使う条件 ---
  el.chips.addEventListener("click", (e) => {
    const x = e.target.closest(".fchip__x");
    if (x) send("/api/lot/list/filter/remove", { index: Number(x.dataset.index) });
  });
  el.savedChips.addEventListener("click", (e) => {
    const use = e.target.closest(".fchip__use");
    if (use) { send("/api/lot/list/saved/load", { name: use.dataset.name }); return; }
    const x = e.target.closest(".fchip__x");
    if (x) send("/api/lot/list/saved/delete", { name: x.dataset.deleteName });
  });
  el.clearFilter.addEventListener("click", () => send("/api/lot/list/filter/clear"));

  el.saveFilter.addEventListener("click", async () => {
    const name = await promptBox("この条件に名前を付けてください", { ok: "名前を付けて残す" });
    if (name === null) return;                 // 取り消し
    send("/api/lot/list/saved/save", { name });
  });

  // --- 一覧検索 ---
  let textTimer = null;
  el.listText.addEventListener("input", () => {
    clearTimeout(textTimer);
    textPending = true;
    textTimer = setTimeout(() => {
      textPending = false;
      send("/api/lot/list/search", { text: el.listText.value });
    }, 220);
  });

  // **打っただけでは開かない**(1件に絞れても)。有るかどうかだけ見たいことがある。
  // 開くのは、1件のときに Enter を押したとき(行のダブルクリック / Enter と同じく、意図した操作)
  el.listText.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    let view = lastView;
    if (textPending) {             // 打ち終えてすぐ Enter: いまの文字で絞ってから決める
      clearTimeout(textTimer);
      textPending = false;
      view = (await send("/api/lot/list/search", { text: el.listText.value })) || lastView;
    }
    if (view && view.total === 1 && view.rows.length === 1 && onOpen) {
      onOpen(view.rows[0].lot_no);
    } else if (view && view.total > 1) {
      toast(`${view.total} 件あります。行を選んで開いてください`, "ok");
    }
  });

  el.pageSize.addEventListener("change", () =>
    send("/api/lot/list/page-size", { size: Number(el.pageSize.value) }));

  // --- 条件を足す欄 ---
  el.condInput.addEventListener("input", () => {
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(loadSuggest, SUGGEST_DEBOUNCE_MS);
  });
  el.condInput.addEventListener("focus", loadSuggest);
  el.condInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeSuggest(); return; }
    if (!suggestItems.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      markSuggest((suggestAt + 1) % suggestItems.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      markSuggest((suggestAt - 1 + suggestItems.length) % suggestItems.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      // 選んでいなければ先頭を採る(打って Enter だけで足せる)
      useSuggest(suggestAt >= 0 ? suggestAt : 0);
    }
  });
  el.suggest.addEventListener("click", (e) => {
    const button = e.target.closest("button[data-index]");
    if (button) useSuggest(Number(button.dataset.index));
  });
  // `document` に付けたものは画面を差し替えても残るので、この画面の
  // あいだだけ有効にする(`nav.js`)。残すと、別の画面を押すたびに
  // 消えた候補欄を閉じようとする
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".suggestwrap")) closeSuggest();
  }, { signal: pageSignal() });

  onLeave(() => { clearTimeout(textTimer); clearTimeout(suggestTimer); });
}
