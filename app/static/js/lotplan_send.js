/*
  lotplan_send.js — 倉庫連携「作業予定ライン」(新規発注の下)

  長がロットを作業ラインに入れる。入れたロットは、そのライン(アクセス権限の「権限」に書いたライン)の端末の
  ロット検索に知らせが出る(`packaging_tool/lot_plans.py`)。

  入れるのは**新規発注のロット**(欄が空なら、いま開いているロット)。欄のロットが変わったら
  そのロットの予定を出し直す。発注とは別に送る(発注の無いロットも入れられる)。
*/
import { api } from "./api.js";
import { toast, toastError } from "./toast.js";

const el = {};
let asked = "";
let timer = 0;

/** カードを動かす。`lotInput` は新規発注の LotNo の欄(無ければ、いま開いているロットを使う)。 */
export function startLotPlanCard(lotInput) {
  for (const id of ["lotPlanCard", "lotPlanNow", "lotPlanLine", "lotPlanSend", "lotPlanCancel", "lotPlanWhy",
                    "lotPlanLinesNote"]) {
    el[id] = document.getElementById(id);
  }
  el.lotInput = lotInput || null;
  if (!el.lotPlanCard) return;
  el.lotPlanSend.addEventListener("click", () => send());
  el.lotPlanCancel.addEventListener("click", () => cancel());
  if (el.lotInput) el.lotInput.addEventListener("input", () => reload(400));
}

function lotNo() {
  return (el.lotInput && el.lotInput.value ? el.lotInput.value : "").trim();
}

/** 欄のロットが変わった(下書きを出した・打ち変えた)。そのロットの予定を出し直す。 */
export function reload(delay = 0) {
  if (!el.lotPlanCard) return;
  window.clearTimeout(timer);
  timer = window.setTimeout(async () => {
    const lot = lotNo();
    asked = lot;
    try {
      const state = await api.get(`/api/warehouse/lot-plan?lot_no=${encodeURIComponent(lot)}`);
      if (asked !== lot) return;              // 待つ間に打ち変えた
      render(state);
    } catch (err) {
      // 出せなくても入れられる(入れるときにもう一度確かめる)
    }
  }, delay);
}

/** ラインの選択肢を描き直す。選んでいたものが残っていれば選んだまま。 */
function drawLines(lines, selected) {
  const head = document.createElement("option");
  head.value = "";
  head.textContent = "選んでください";
  el.lotPlanLine.replaceChildren(head, ...lines.map((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    return option;
  }));
  el.lotPlanLine.value = lines.includes(selected) ? selected : "";
}

/** いまの予定を出す。 */
export function render(state) {
  if (!el.lotPlanCard || !state) return;
  const plan = state.plan;
  // 選べるラインは拠点に登録してあるライン名(サーバが決める)。拠点を足した・消したら選択肢も変わる
  if (Array.isArray(state.lines)) drawLines(state.lines, plan ? state.selected_line : el.lotPlanLine.value);
  if (el.lotPlanLinesNote) {
    el.lotPlanLinesNote.hidden = !state.lines_note;
    el.lotPlanLinesNote.textContent = state.lines_note || "";
  }
  const now = el.lotPlanNow;
  now.textContent = "";
  const bold = (text) => { const b = document.createElement("b"); b.textContent = text; return b; };
  if (plan) {
    now.append("ロット ", bold(plan.lot_no), " は ", bold(plan.line),
               ` の予定です(${plan.sent_label} ${plan.terminal})`);
    if (state.unsent) now.append("。まだ共有へ届いていません(届きしだい知らせが出ます)");
    el.lotPlanLine.value = state.selected_line || "";
  } else if (state.done) {
    // ラインの端末で「作業済み」にした。入れ直せば、また予定に入る
    now.append("ロット ", bold(state.done.lot_no), " は ", bold(state.done.line),
               ` で作業済みです(${state.done.sent_label} ${state.done.terminal})`);
  } else if (state.lot_no) {
    now.append("ロット ", bold(state.lot_no), " はまだどのラインの予定にも入っていません");
  } else {
    now.append("ロットがまだありません。ロット検索でロットを開いてから入れてください");
  }
  now.className = plan ? "status status--ok" : "status";
  // 送れるのは作業長の端末だけ(サーバが決める)
  const can = state.can_send !== false;
  el.lotPlanLine.disabled = !can;
  el.lotPlanSend.disabled = !can;
  el.lotPlanCancel.hidden = !plan || !can;
  if (el.lotPlanWhy) {
    el.lotPlanWhy.hidden = can;
    el.lotPlanWhy.textContent = state.send_why || "";
  }
}

async function send() {
  const line = el.lotPlanLine.value;
  if (!line) {
    toast("作業ラインを選んでください", "warn");
    el.lotPlanLine.focus();
    return;
  }
  el.lotPlanSend.disabled = true;
  try {
    const state = await api.post("/api/warehouse/lot-plan", { lot_no: lotNo(), line });
    render(state);
    toast(state.message, "ok");
  } catch (err) {
    toastError(err);
  } finally {
    el.lotPlanSend.disabled = el.lotPlanLine.disabled;
  }
}

async function cancel() {
  el.lotPlanCancel.disabled = true;
  try {
    const state = await api.post("/api/warehouse/lot-plan/cancel", { lot_no: lotNo() });
    render(state);
    toast(state.message, "ok");
  } catch (err) {
    toastError(err);
  } finally {
    el.lotPlanCancel.disabled = false;
  }
}
