/*
  lotplans_view.js — ロット検索「予定ロット」

  長が作業ラインに入れたロットのうち、この端末のライン(アクセス権限の「権限」に書いたライン)のもの。
  まだ確認していないものが知らせ。

    開く … 発注したパレットを決めてボード選定へ移る(移れなければロット情報を開く)。知らせも消える
    確認 … 開かずに知らせだけ消す

  確認済みは畳む(「確認済み N件」を押したときだけ出す)。開け閉めは見張りで出し直しても保つ。

  開いているあいだは見張る(`WATCH_MS` ごと。共有が変わっていれば予定ロットだけ取り込み直す)。
  新しく届いたらトーストでも知らせる。レールの「ロット検索」の印(予N)も合わせる。
*/
import { api } from "./api.js";
import * as nav from "./nav.js";
import * as datasync from "./datasync.js";
import { toast, toastError } from "./toast.js";

export const WATCH_MS = 15_000;

const el = {};
let onOpen = null;
let known = new Set();        // 知らせ済み(トーストを2度出さない)の予定ID
let watch = 0;
let foldOpen = false;         // 確認済みを出しているか(見張りで出し直しても保つ)
let last = null;              // いちばん新しい状態(開け閉めで出し直す)

/** 動かす。`initial` はサーバが埋めた状態、`open(lotNo)` はロット情報を開く関数。 */
export function startPlans(initial, open) {
  for (const id of ["planCard", "planLines", "planUnseen", "planNews", "planWhy",
                    "planList", "planEmpty", "planSeenAll", "planFold", "planSeenList"]) {
    el[id] = document.getElementById(id);
  }
  onOpen = open;
  if (!el.planCard) return;
  foldOpen = false;
  if (el.planFold) el.planFold.addEventListener("click", () => {
    foldOpen = !foldOpen;
    render(last);
  });
  known = new Set((initial?.items || []).filter((i) => !i.seen).map((i) => i.plan_id));
  if (el.planSeenAll) el.planSeenAll.addEventListener("click", () => seenAll());
  render(initial);
  window.clearInterval(watch);
  watch = window.setInterval(() => pull(), WATCH_MS);
  nav.onLeave(() => window.clearInterval(watch));
  // マスタが変わったら(アクセス権限を直した等)、この端末の立場(作業長・ライン)をすぐ出し直す
  datasync.onChange(() => pull());
  // 開いた直後にも一度見に行く(起動直後は共有の分がまだ入っていないことがある)
  pull();
}

/** 共有を見て(変わっていれば取り込み直して)出し直す。新しく届いたものはトーストでも知らせる。 */
export async function pull() {
  let state;
  try {
    state = await api.get("/api/lot/plans?refresh=1");
  } catch (err) {
    return;                    // 見張りの失敗は黙る(次の回でまた見る)
  }
  const fresh = (state.items || []).filter((i) => !i.seen && !known.has(i.plan_id));
  for (const item of fresh) {
    known.add(item.plan_id);
    toast(`予定ロット ${item.lot_no}(${item.line})が届きました`, "info",
          { label: "開く", onClick: () => openPlan(item) });
  }
  render(state);
}

/** 出す。 */
export function render(state) {
  if (!el.planCard || !state) return;
  last = state;
  const items = state.items || [];
  const fresh = items.filter((i) => !i.seen);
  const seen = items.filter((i) => i.seen);
  const unseen = fresh.length;
  // いまの状態はサーバが言葉にする(強いもの優先: Administrator > 作業長 > ライン)
  el.planLines.textContent = state.label || (state.lines && state.lines.length
    ? `この端末のライン: ${state.lines.join(" / ")}` : "この端末のライン: 未登録");
  el.planUnseen.hidden = !unseen;
  el.planUnseen.textContent = `未確認 ${unseen}件`;
  el.planNews.hidden = !unseen;
  el.planNews.textContent = unseen
    ? `新しい予定ロットが ${unseen}件 届いています。「開く」でロット情報を開きます(開かずに消すなら「確認」)`
    : "";
  if (el.planSeenAll) el.planSeenAll.hidden = unseen < 2;
  el.planWhy.hidden = !state.why;
  el.planWhy.textContent = state.why || "";
  el.planEmpty.hidden = unseen > 0 || !!state.why;
  el.planEmpty.textContent = seen.length
    ? "新しい予定ロットはありません"
    : `予定ロットはありません(${state.days || 7}日以内に入ったもの)`;
  el.planList.hidden = !unseen;
  el.planList.replaceChildren(...fresh.map(row));
  // 確認済みは畳む。押したときだけ出す(作業の順番は決まっていないので、消さずに残す。
  // 終わったものは「作業済み」で消す。押し忘れても送ってから日数が過ぎれば出なくなる)
  if (el.planFold && el.planSeenList) {
    const open = foldOpen && seen.length > 0;
    el.planFold.hidden = !seen.length;
    el.planFold.textContent = `確認済み ${seen.length}件 ${open ? "▾ 畳む" : "▸ 出す"}`;
    el.planFold.setAttribute("aria-expanded", open ? "true" : "false");
    el.planSeenList.hidden = !open;
    el.planSeenList.replaceChildren(...(open ? seen.map(row) : []));
  }
  nav.planBadge(unseen);
}

function row(item) {
  const li = document.createElement("li");
  li.className = `plans__row${item.seen ? "" : " is-new"}`;
  li.dataset.plan = item.plan_id;
  const lot = document.createElement("b");
  lot.className = "plans__lot";
  lot.textContent = item.lot_no;
  const line = document.createElement("span");
  line.className = "badge badge--todo";
  line.textContent = item.line;
  const meta = document.createElement("span");
  meta.className = "plans__meta";
  meta.textContent = [item.sent_label, item.terminal, item.summary].filter(Boolean).join(" ・ ");
  const actions = document.createElement("span");
  actions.className = "plans__actions";
  const openBtn = document.createElement("button");
  openBtn.type = "button";
  openBtn.className = item.seen ? "btn" : "btn btn--commit";
  openBtn.textContent = "開く";
  openBtn.addEventListener("click", () => openPlan(item));
  actions.append(openBtn);
  if (item.seen) {
    const done = document.createElement("span");
    done.className = "plans__seen";
    done.textContent = item.opened ? "開いた" : "確認済み";
    actions.append(done);
  } else {
    const seenBtn = document.createElement("button");
    seenBtn.type = "button";
    seenBtn.className = "btn";
    seenBtn.textContent = "確認";
    seenBtn.title = "開かずに知らせを消します";
    seenBtn.addEventListener("click", () => markSeen(item, false));
    actions.append(seenBtn);
  }
  // 終わったら本人が消す。共有へ送るので、同じラインのほかの端末からも消える
  const doneBtn = document.createElement("button");
  doneBtn.type = "button";
  doneBtn.className = "btn";
  doneBtn.textContent = "作業済み";
  doneBtn.title = "作業が終わったら押します。このラインの予定欄から消えます";
  doneBtn.addEventListener("click", () => markDone(item, doneBtn));
  actions.append(doneBtn);
  li.append(lot, line, meta, actions);
  return li;
}

async function markDone(item, button) {
  button.disabled = true;
  try {
    const state = await api.post(`/api/lot/plans/${encodeURIComponent(item.plan_id)}/done`, {});
    render(state);
    if (state.message) toast(state.message, "ok");
  } catch (err) {
    button.disabled = false;
    toastError(err);
  }
}

let opening = false;          // 「開く」の途中(続けて押しても2度開かない)

/** 開く。知らせを先に消してから開く(開くと資材選択へ移ることがあるので、移る前に消す)。 */
export async function openPlan(item) {
  if (opening) return;
  opening = true;
  try {
    if (!item.seen || !item.opened) await markSeen(item, true);
    if (onOpen) await onOpen(item.lot_no);
  } finally {
    opening = false;
  }
}

/** 「開く」の進み具合を出す(`done` / `total`)。`done` が null なら消す。 */
export function progress(done, total, text) {
  const box = document.getElementById("planProgress");
  if (!box) return;
  if (done === null) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const bar = document.getElementById("planProgressBar");
  if (bar) {
    bar.max = total;
    bar.value = done;
  }
  const label = document.getElementById("planProgressText");
  if (label) label.textContent = text;
}

async function markSeen(item, opened) {
  try {
    render(await api.post(`/api/lot/plans/${encodeURIComponent(item.plan_id)}/seen`, { opened }));
  } catch (err) {
    toastError(err);
  }
}

async function seenAll() {
  const items = [...(el.planList?.querySelectorAll(".plans__row.is-new") || [])];
  let state = null;
  for (const node of items) {
    try {
      state = await api.post(`/api/lot/plans/${encodeURIComponent(node.dataset.plan)}/seen`,
                             { opened: false });
    } catch (err) {
      toastError(err);
      break;
    }
  }
  if (state) render(state);
}

