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
  return tr;
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
    box.appendChild(button("確認済みにする", "btn--commit",
                           () => act("/api/warehouse/confirm", row)));
  }
  if (row.can_cancel) {
    box.appendChild(button("取り消し", "btn--danger",
                           () => act("/api/warehouse/cancel", row)));
  }
  // Lotを開くだけなら「操作」ではないので、理由の文は出したままにする
  if (!box.querySelector("button")) {
    const why = document.createElement("span");
    why.className = "why";
    why.style.margin = "0";
    // 「できない」ことを空欄で示さない。なぜ押せないのかを書く。
    // **役割は3つに分かれる**(出すのは現場 / 確認は倉庫 /
    // 取り消しは現場、かつ倉庫の確認前)ので、理由もそれに沿って書く
    if (row.status === "確認済み") {
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

function showDraft() {
  const box = el.drafts;
  if (!box) return;
  box.hidden = !drafts.length;
  if (!drafts.length) { applyExLock(false); return; }

  draftAt = Math.max(0, Math.min(draftAt, drafts.length - 1));
  const order = drafts[draftAt];
  for (const [key, node] of Object.entries(el.fields)) {
    node.value = order[key] === undefined || order[key] === null ? "" : String(order[key]);
  }
  applyExLock(order.is_ex_order);
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
                    "drafts", "draftPrev", "draftNext", "draftPos"]) {
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
      // 先に錠を外す。外さないと、EXの下書きを消したあとも
      // 発注コード欄が押せないまま残る
      applyExLock(false);
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
