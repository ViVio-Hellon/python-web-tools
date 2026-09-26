/*
  倉庫連携。

  現場は出す、倉庫は確認する。**どちらができるかはサーバが決める**
  (`can_confirm` / `can_cancel`)。画面がボタンの出し方を自分で決めると、
  現場の画面に確認ボタンが出る余地が生まれる。

  確認のエンドポイントは資材モードの権限がある端末にしか無いので、
  仮にここでボタンを描いても 404 になる ── 守りは1枚ではない。
*/

import { api } from "../api.js";
import * as nav from "../nav.js";
import { toast, toastError } from "../toast.js";
import * as toggles from "../toggles.js";
// ロットの詳細はロット検索と同じものを出す(`../lotdetail.js`)。
// 動的に読むのは版クエリを合わせるため(`views/lot.js` と同じ理由)
const VERSION_QUERY = new URL(import.meta.url).search;
const lotdetail = await import(`../lotdetail.js${VERSION_QUERY}`);

const el = {};
let columns = [];
let isMaterial = false;
// 1行を開いたときの見せ方。**サーバが決める**(`ORDER_DETAIL`)
let detailFields = [];
let copyColumn = "発注コード";
// 押す前に見せる項目と言葉。**サーバが決める**(`ORDER_ASK*`)
let askFields = [];
let askTexts = {};

function setStatus(node, message, kind) {
  if (!message) { node.hidden = true; return; }
  node.hidden = false;
  node.textContent = message;
  node.className = `status status--${kind}`;
}

// ------------------------------------------------------------------
// 一覧
// ------------------------------------------------------------------
function render(view) {
  columns = view.columns;
  if (view.detail_fields) detailFields = view.detail_fields;
  if (view.copy_column) copyColumn = view.copy_column;
  if (view.ask_fields) askFields = view.ask_fields;
  if (view.ask) askTexts = view.ask;
  el.rows.replaceChildren(...view.rows.map(rowElement));
  el.listNote.textContent = view.message || "";
  el.listNote.hidden = !view.message;
  el.found.textContent = view.found ? `${view.found} 件` : "";
  el.pending.hidden = !view.pending;
  el.pending.textContent = `未確認 ${view.pending}`;
  // 数字だけでは何の数か読めない(現場の声:「未確認 4とはなんだ」)。
  // 見えている一覧のうち、倉庫がまだ受けていない件数
  el.pending.title = isMaterial
    ? `倉庫がまだ確認していない発注が ${view.pending} 件あります`
    : `送った発注のうち、倉庫がまだ確認していないものが ${view.pending} 件`
      + "(この間は取り消せます)";
}

/**
 * 1つの升。**主を太く、従を小さく**の2行に組む。
 *
 * どれを主に、どれを従に置くかは**サーバの指図**(`ORDER_VIEW`)で、
 * ここは言われたとおりに `row.values` から拾って並べるだけ。
 * 事実の置き場所は `values` ただ1つ。
 */
function cell(row, column) {
  const td = document.createElement("td");
  if (column.numeric) td.className = "n";

  if (column.kind === "status") {
    const chip = document.createElement("span");
    // 種別名はサーバが返す(`presenters/warehouse.py`)。
    // 共通の状態ピルへ写すだけで、良し悪しの判断はしない
    chip.className = `st st--${row.status_kind}`;
    chip.textContent = row.status;
    td.appendChild(chip);
    return td;
  }

  const head = document.createElement("span");
  head.className = "pri";
  const value = row.values[column.primary];
  head.textContent = (value === "" || value === null) ? "---" : value;
  // 省略されたときに全文を出す。畳んだせいで読めなくなるのを防ぐ
  head.title = head.textContent;
  td.appendChild(head);

  const sub = (column.sub || [])
    .map((label) => row.values[label])
    .filter((v) => v !== "" && v !== null && v !== undefined)
    .join(column.sep || " ");
  if (sub) {
    const tail = document.createElement("span");
    tail.className = "sec";
    tail.textContent = sub;
    tail.title = sub;
    td.appendChild(tail);
  }
  return td;
}

function rowElement(row) {
  const tr = document.createElement("tr");
  for (const column of columns) tr.appendChild(cell(row, column));
  tr.appendChild(actionsCell(row));
  // **行を押したら開く。** 一覧は詰めて並べるので字が小さく、
  // 「どの発注だったか」を確かめるには開く場所が要る。
  // ボタンを押したときは開かない(操作と閲覧を混ぜない)
  tr.tabIndex = 0;
  tr.addEventListener("click", (event) => {
    if (event.target.closest("button, a")) return;
    openOrder(row);
  });
  tr.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openOrder(row);
  });
  return tr;
}

/**
 * 1行を大きく出す(VBA `frmWarehouseOrder.lstOrders_Click`)。
 *
 * VBAは同じフォームの中に13個のラベルを並べて埋めていた。こちらは
 * 一覧が14列の表で、同じ場所に置くと表が読めなくなるので開いて出す。
 *
 * **発注コードは押したときだけ控える**(VBA `evtHatchu_Click`)。
 * あちらは青字・下線・枠付きのラベルに「クリックでコピー」と出して
 * いた。開いただけで控えると、利用者が別に写していたものを黙って
 * 消すことになる。
 */
function openOrder(row) {
  el.orderTitle.textContent = row.values[copyColumn] || `管理番号 ${row.mgr_no}`;
  el.orderStatus.textContent = row.status;
  el.orderStatus.className = `st st--${row.status_kind}`;
  el.orderFields.replaceChildren(
    ...detailFields.map((f) => detailRow(row, f)));
  // 前の行で出した「コピーしました」を持ち越さない(VBA も選択のたびに
  // `lblCopyMsg` を空にしていた)
  el.orderCopied.textContent = "";
  el.orderModal.showModal();
}

function detailRow(row, field) {
  const box = document.createElement("div");
  if (field.big) box.className = "big";
  const dt = document.createElement("dt");
  dt.textContent = field.label;
  const dd = document.createElement("dd");
  const value = row.values[field.key];
  const text = (value === "" || value === null || value === undefined)
    ? "---" : String(value);
  dd.textContent = text;

  if (field.key === copyColumn && text !== "---") {
    // **押せることが見て分かる形にする**(VBA は青字・下線・枠付き)
    dd.className = "copyable";
    dd.tabIndex = 0;
    dd.role = "button";
    dd.title = "クリックでコピー";
    const copy = () => copyCode(text);
    dd.addEventListener("click", copy);
    dd.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      copy();
    });
  }
  box.append(dt, dd);
  return box;
}

/** 発注コードを控える。**控えられたかどうかは必ず言う。** */
async function copyCode(text) {
  try {
    await navigator.clipboard.writeText(text);
    el.orderCopied.textContent = "コピーしました";
    el.orderCopied.className = "why ok";
  } catch {
    // 権限やブラウザの都合で控えられないことがある。**黙らない**
    el.orderCopied.textContent =
      "コピーできませんでした。値を選んで写してください。";
    el.orderCopied.className = "why";
  }
}

function actionsCell(row) {
  const td = document.createElement("td");
  const box = document.createElement("div");
  box.className = "rowacts";

  // **どのLotの発注かを辿れるようにする。**
  //
  // 現場は ロット情報 → 資材選択 → 送信 の順に進む。受け取る側から
  // その逆(発注に付いているLotを開いて同じように展開する)ができないと、
  // 何のための発注なのかを確かめる手立てが、番号を目で読んで別の端末で
  // 引き直すしかない(現場の指摘:「倉庫モードの時に送られてきた
  // データに添付しているLOT情報を現場モードのように展開できる必要が
  // あります」)。7桁を打ち直させない ── 打ち間違えても開けてしまう
  if (row.lot_no) {
    // **画面は移らない。** ここで出して、閉じれば一覧のまま。
    // 以前はロット検索へ飛ばしていたが、確かめるだけなのに戻るには
    // たどり直しで、現場から「非常に手間」と言われた
    box.appendChild(button("Lotを開く", "btn--find",
                           () => peekLot(row.lot_no)));
  }

  // できることはサーバが返す。ここで条件を組み立て直さない
  if (row.can_confirm) {
    box.appendChild(button("確認済みにする", "btn--commit", async () => {
      if (await ask("confirm", row)) act("/api/warehouse/confirm", row);
    }));
  }
  if (row.can_cancel) {
    box.appendChild(button("取り消し", "btn--danger", async () => {
      if (await ask("cancel", row)) act("/api/warehouse/cancel", row);
    }));
  }
  // Lotを開くだけなら「操作」ではないので、理由の文は出したままにする。
  // **操作のボタン(確認・取り消し)だけを見る。** ボタンなら何でも数えて
  // いたので、Lotの入った発注では理由が一度も出ていなかった
  if (!row.can_confirm && !row.can_cancel) {
    const why = document.createElement("span");
    why.className = "why";
    why.style.margin = "0";
    // 「できない」ことを空欄で示さない。なぜ押せないのかを書く。
    // **言葉はサーバが決める**(送った端末で絞るなど、条件はサーバが持つ)
    if (row.why) {
      why.textContent = row.why;
    } else if (row.status === "確認済み") {
      why.textContent = isMaterial ? "確認済みです"
        : "倉庫が確認済みのため取り消せません";
    } else if (row.status === "取消済") {
      why.textContent = "取り消し済みです";
    } else {
      // 未確認なのにボタンが無い場合。資材モードは確認できるはずなので
      // ここへは来ないが、来たときに空欄にはしない
      why.textContent = "この画面からできる操作はありません";
    }
    box.appendChild(why);
  }

  td.appendChild(box);
  return td;
}

function button(label, kind, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `btn ${kind}`;
  btn.textContent = label;
  btn.addEventListener("click", onClick);
  return btn;
}

// ------------------------------------------------------------------
// 通信
// ------------------------------------------------------------------
function params() {
  const period = toggles.value(el.periodGroup, "period") || "all";
  const params = new URLSearchParams({ q: el.q.value.trim(), period });
  if (el.cancelled && el.cancelled.checked) params.set("cancelled", "1");
  return params;
}

function query() {
  return `/api/warehouse/orders?${params()}`;
}

async function load() {
  try {
    render(await api.get(query()));
  } catch (err) {
    toastError(err);
  }
}

// **取り込み元まで見に行く更新。** 手元のDBを引き直すだけでは、ツールを
// 開きっぱなしにした端末に新しい発注が出てこない ── 現場が昼に出した
// ぶんは、閉じて開き直すまで一覧に現れなかった。
//
//     押されたとき … 必ず見に行く。変わっていなければそう言う
//     見張りのとき … 取り込み元の姿だけ見て、変わっていたら取り込む
//
async function pull({ quiet = false } = {}) {
  const search = params();
  if (quiet) search.set("only_if_changed", "1");
  try {
    const body = await api.post(`/api/warehouse/refresh?${search}`, {});
    // 見張りのときは、変わっていないのに画面を作り直さない ── 選んで
    // いる行や途中の操作を、何も起きていないのに取り上げることになる
    if (!quiet || body.refresh.updated) render(body);
    if (!quiet) toast(body.refresh.message, body.refresh.looked ? "ok" : "ng");
    else if (body.refresh.updated) toast(body.refresh.message, "ok");
  } catch (err) {
    // 見張りは黙って諦める。共有へ届かない端末で、一定の間隔で
    // 赤い帯が出続けるのは邪魔でしかない(次の回でまた試す)
    if (!quiet) toastError(err);
  }
}

/** そのLotの詳細をこの場で出す。**作業中のロットは変えない。** */
async function peekLot(lotNo) {
  try {
    const body = await api.get(
      `/api/lot/${encodeURIComponent(lotNo)}/peek`);
    lotdetail.show(body);
    if (!body.found) toast(body.message || "そのロットは見つかりません", "ng");
  } catch (err) {
    toastError(err);
  }
}

/**
 * 押す前に訊く(VBA `btnConfirm_Click` / `btnDelete_Click`)。
 *
 * **何を動かすのかを見せてから訊く。** 確認も取り消しも取り返しが
 * つかず、一覧は14列を詰めて並べるので、押す行を1行ずれて選んでも
 * 気づけない。VBAはどちらの操作でも 品名・発注コード・登録日時 を
 * 出して Yes/No を訊いていた。
 */
function ask(kind, row) {
  const text = askTexts[kind] || {};
  return new Promise((resolve) => {
    el.askTitle.textContent = text.title || "よろしいですか？";
    el.askWhy.textContent = text.why || "";
    el.askYes.textContent = text.ok || "する";
    el.askFields.replaceChildren(...askFields.map((label) => {
      const box = document.createElement("div");
      box.className = "big";
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      const value = row.values[label];
      dd.textContent = (value === "" || value === null || value === undefined)
        ? "---" : value;
      box.append(dt, dd);
      return box;
    }));

    const done = (yes) => {
      el.askYes.removeEventListener("click", onYes);
      el.askNo.removeEventListener("click", onNo);
      el.askModal.removeEventListener("close", onNo);
      el.askModal.close();
      resolve(yes);
    };
    const onYes = () => done(true);
    const onNo = () => done(false);
    el.askYes.addEventListener("click", onYes);
    el.askNo.addEventListener("click", onNo);
    // Esc や枠の外で閉じたときも「やめる」
    el.askModal.addEventListener("close", onNo);
    el.askModal.showModal();
  });
}

async function act(path, row) {
  try {
    const body = await api.post(path, { mgr_no: row.mgr_no });
    toast(body.message, "ok");
  } catch (err) {
    // 409 は「他の端末が先に動かした」。古い一覧のまま押させない
    toastError(err);
  }
  await load();
}

async function send() {
  const values = {};
  for (const [key, node] of Object.entries(el.fields)) values[key] = node.value.trim();
  // EX受注かどうかは**打った内容から推し量らない**。組み立てた側
  // (資材選択)が立てた旗をそのまま運ぶ(`presenters/outputs.build_orders`)
  values.is_ex_order = draftIsEx;
  // 下書きかどうかも運ぶ。サーバはこれを見て**LotNoが作業中のロットと
  // 同じか**を確かめる ── 画面で打てなくするだけにしない(守りは1枚では
  // ない)。手入力は別のロットの分を起こすことがあるので掛からない
  values.from_draft = drafts.length > 0;

  try {
    const body = await api.post("/api/warehouse/send", values);
    setStatus(el.sendStatus, body.message, "ok");
    toast(body.message, "ok");
    // 送った内容は消す。同じものを二度送らせないため
    for (const node of Object.values(el.fields)) node.value = "";
    dropDraft();
    await load();
  } catch (err) {
    setStatus(el.sendStatus, err.message, "ng");
    if (err.field && el.fields[err.field]) el.fields[err.field].focus();
  }
}

/* ================================================================
   資材選択から届いた下書き

   **まだ登録していない。** 1件ずつ欄に入れて、内容を確かめてから
   「倉庫へ送信」を押してもらう(1P0113 は角材と松板の2行になる)。
   発注は取り消しに人手が要るので、送る前に画面で見せる。
   ================================================================ */
let drafts = [];
let draftAt = 0;
/** いま欄に入っているのがEX受注の行か(送信時に運ぶ)。 */
let draftIsEx = false;
/** EX受注で空のまま送る欄。サーバが持っている(`ex_blank_keys`)。 */
let exBlankKeys = [];

/**
 * EX受注の欄を**押せなくする**(VBA `BuildRowUI` の `Enabled=False` 相当)。
 *
 * 空で送るのが正しいので、打てるままにしておくと「入れ忘れ」に見える。
 * 押せない形にして、理由を添える ── 押せないことだけ示して黙ると、
 * 壊れているのか決まりなのかが分からない(§2.3)。
 */
function applyExLock(on) {
  draftIsEx = Boolean(on);
  for (const key of exBlankKeys) {
    const node = el.fields[key];
    if (!node) continue;
    // 見た目は `.input:disabled`(沈んだ地・not-allowed)が持っている
    node.disabled = draftIsEx;
    if (draftIsEx) node.value = "";
    // **押せない欄に「必須」の印を残さない。** 空で送るのが正しいので、
    // * が付いたままだと入れろと言っていることになる
    const mark = document.querySelector(`label[data-for="${key}"] .req`);
    if (mark) mark.hidden = draftIsEx;
  }
  if (el.exNote) el.exNote.hidden = !draftIsEx;
}

/**
 * 下書きを出しているあいだ、**発注数以外を打ち直せなくする**
 * (VBA `BuildRowUI`。原文は発注数だけが入力欄で、ほかはラベルだった)。
 *
 * **送るのは道具が組み立てたもの、人が触るのは発注数だけ。** 資材選択が
 * ロットから引いた値を打ち直せると、LotNo を書き替えて別のロットの発注に
 * してしまえる ── そうなると倉庫が受け取った現物と帳簿が合わなくなり、
 * どちらが正しいのかを後から決められない。
 *
 * `disabled` ではなく `readOnly` にするのは、**読めて選べるまま**に
 * するため。原文もラベルで出していた(発注コードを控えることがある)。
 *
 * 打ち直したいときは「入力を消す」で下書きから抜け、手入力に切り替わる。
 */
function applyDraftLock(on) {
  for (const [key, node] of Object.entries(el.fields)) {
    node.readOnly = on && key !== "hatchu_suu";
  }
  if (el.draftNote) el.draftNote.hidden = !on;
}

function showDraft() {
  const box = el.drafts;
  if (!box) return;
  box.hidden = !drafts.length;
  if (!drafts.length) { applyExLock(false); applyDraftLock(false); return; }

  draftAt = Math.max(0, Math.min(draftAt, drafts.length - 1));
  const order = drafts[draftAt];
  for (const [key, node] of Object.entries(el.fields)) {
    node.value = order[key] === undefined || order[key] === null ? "" : String(order[key]);
  }
  applyExLock(order.is_ex_order);
  applyDraftLock(true);
  el.draftPos.textContent = `${draftAt + 1} / ${drafts.length}`;
  el.draftPrev.disabled = draftAt === 0;
  el.draftNext.disabled = draftAt === drafts.length - 1;
}

/** 送れたぶんは下書きから外す。同じものを二度送らせない。 */
function dropDraft() {
  if (!drafts.length) return;
  drafts.splice(draftAt, 1);
  showDraft();
}

// ------------------------------------------------------------------
export function start(state, material, lotPeekWhy) {
  isMaterial = material;
  drafts = [];          // 再入場のたびに真っさらから(`nav.js`)
  draftAt = 0;
  exBlankKeys = state.ex_blank_keys || [];
  draftIsEx = false;
  for (const id of ["rows", "listNote", "found", "pending", "q", "refresh",
                    "send", "clearForm", "sendStatus", "cancelled", "exNote",
                    "drafts", "draftPrev", "draftNext", "draftPos", "draftNote"]) {
    el[id] = document.getElementById(id);
  }
  // 発注フォームは現場モードにしか無い
  el.fields = {};
  for (const node of document.querySelectorAll('[id^="f-"]')) {
    el.fields[node.id.slice(2)] = node;
  }

  // **絞り込みの道具は取りに行く前に揃える。** `query()` が期間の
  // セグメントを見るので、`load()` より後に拾うと1回目が投げる
  el.periodGroup = document.querySelector('.seg[aria-label="期間"]');

  render(state);
  load();

  el.refresh.addEventListener("click", () => pull());
  startWatch();
  // ロットの詳細を使えるようにする。**資材展開は渡さない** ── ここは
  // 確かめる場所なので、ボタンはテンプレートにも出していない
  for (const id of ["orderModal", "orderTitle", "orderStatus",
                    "orderFields", "orderClose", "orderCopied",
                    "askModal", "askTitle", "askFields", "askWhy",
                    "askYes", "askNo"]) {
    el[id] = document.getElementById(id);
  }
  el.orderClose.addEventListener("click", () => el.orderModal.close());
  el.orderModal.addEventListener("click", (event) => {
    if (event.target === el.orderModal) el.orderModal.close();
  });

  lotdetail.mount({ expandAbsentWhy: lotPeekWhy });
  nav.onLeave(lotdetail.stop);
  // **打つそばから絞る。** Enterを押すまで何も起きない作りだったので、
  // 現場からは「絞り込みが効かない」に見えていた ── 打った本人には
  // 押し忘れたのか効かないのか区別できない。連打で毎回サーバへ
  // 聞きにいかないよう少しだけ間を置く(資材選択の一覧と同じ)
  let typing = 0;
  el.q.addEventListener("input", () => {
    window.clearTimeout(typing);
    typing = window.setTimeout(load, 250);
  });
  el.q.addEventListener("keydown", (event) => {
    // 待たずにいま出す
    if (event.key === "Enter") {
      event.preventDefault();
      window.clearTimeout(typing);
      load();
    }
  });
  toggles.attach(el.periodGroup, "period", load);
  if (el.cancelled) el.cancelled.addEventListener("change", load);

  if (el.send) el.send.addEventListener("click", send);
  if (el.clearForm) {
    el.clearForm.addEventListener("click", () => {
      // **下書きから抜ける。** 消したのに下書きの錠が残っていると、
      // 空欄なのに打てない状態になる。ここが手入力への切り替え口でも
      // ある(道具が組み立てた値を直したいときは、一度消して打ち直す)
      drafts = [];
      draftAt = 0;
      // 先に錠を外す。外さないと、EXの下書きを消したあとも
      // 発注コード欄が押せないまま残る
      applyExLock(false);
      applyDraftLock(false);
      showDraft();
      for (const node of Object.values(el.fields)) node.value = "";
      setStatus(el.sendStatus, "", "ok");
    });
  }

  // 資材選択から届いた下書き。サーバがテンプレートに埋めてある
  if (el.drafts) {
    try {
      drafts = JSON.parse(el.drafts.dataset.orders || "[]");
    } catch {
      drafts = [];
    }
    el.draftPrev.addEventListener("click", () => { draftAt -= 1; showDraft(); });
    el.draftNext.addEventListener("click", () => { draftAt += 1; showDraft(); });
    showDraft();
  }
}

// ------------------------------------------------------------------
// 見張り
// ------------------------------------------------------------------
// 一定の間隔で取り込み元の姿だけを見に行き、変わっていたら取り込む。
// **見に行くだけなら共有への往復は軽い**(ファイルの大きさと更新時刻を
// 見るだけで、開きも読みもしない)。
//
// 間隔は「気づくのが遅れて困る長さ」で決める。発注は出してすぐ動く
// ものではないので、分の単位で足りる。短くしても共有を叩く回数が
// 増えるだけで、現場の仕事は速くならない。
const WATCH_MS = 60_000;

let watch = 0;

function startWatch() {
  window.clearInterval(watch);
  watch = window.setInterval(() => pull({ quiet: true }), WATCH_MS);
  // 画面を出たら止める。**出たあとも見に行き続けない** ── 見えていない
  // 画面のために共有を叩くのは、誰の役にも立たない
  nav.onLeave(() => window.clearInterval(watch));
}
