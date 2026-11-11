/*
  設定画面の「管理者以外停止モード」(`packaging_tool/remote_stop.py`)。

  管理者の PC から、ほかの起動中の PC のツールを止める。お願いを出す・各 PC の様子を見る・解除する。
  判断(パスワード・Administrator・出ているか)はサーバが持つ。ここは並べるだけ。
*/

import { api } from "../api.js";
import { confirmBox } from "../askbox.js";
import { onLeave } from "../nav.js";
import { toast, toastError } from "../toast.js";

const POLL_MS = 5000;           // Administrator の端末(各 PC の様子を見る)
const POLL_SLOW_MS = 30000;     // それ以外(お願いが出ているかだけ)
const el = {};
let timer = null;
let admin = false;

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text || "";
  return td;
}

function paint(view) {
  if (!el.rsState) return;
  const active = Boolean(view.request);
  el.rsState.textContent = !view.available ? "使えません" : active ? "お願いを出しています(更新中)" : "出していません";
  el.rsState.className = `st st--${!view.available ? "warn" : active ? "warn" : "ok"}`;

  const why = [];
  if (!view.available) why.push(view.why);
  if (view.unreadable) why.push(view.unreadable);
  if (view.available && !view.administrator) {
    why.push(`この端末(${view.me_pc})のアクセス権限に Administrator が無いので、お願いは出せません。`);
  }
  el.rsWhy.textContent = why.join(" ");
  el.rsWhy.hidden = !why.length;

  el.rsRequest.textContent = active ? view.request_text : "";
  el.rsRequest.hidden = !active;
  el.rsStart.hidden = active;
  el.rsClear.hidden = !active;
  el.rsStart.disabled = !view.available || !view.administrator;
  el.rsClear.disabled = !view.available || !view.administrator;

  el.rsPcs.replaceChildren();
  for (const pc of view.pcs || []) {
    const tr = document.createElement("tr");
    if (pc.stale) tr.dataset.level = "warn";
    // 自分の PC の行は太字で見分けられるように(案内の字も「この PC」と言う)
    if (pc.me) tr.style.fontWeight = "700";
    tr.append(cell(pc.pc), cell(pc.login), cell(pc.version), cell(pc.label), cell(pc.detail), cell(pc.beat));
    el.rsPcs.append(tr);
  }
  el.rsPcsWrap.hidden = !(view.pcs || []).length;
}

function shown() {
  // 欄が見えていて(「実行」の面を開いている)、タブが表にあるときだけ見に行く
  const card = document.getElementById("remoteStopCard");
  return Boolean(card && card.offsetParent !== null && document.visibilityState === "visible");
}

async function refresh() {
  if (shown()) {
    try {
      const view = await api.get("/api/settings/remote-stop");
      admin = Boolean(view.administrator);
      paint(view);
    } catch {
      // 見張りは黙って次を待つ(押したときの断りはトーストで出す)
    }
  }
  timer = setTimeout(refresh, admin ? POLL_MS : POLL_SLOW_MS);
}

async function send(path, body) {
  try {
    const res = await api.post(path, body);
    toast(res.message, "ok");
    if (res.state) paint(res.state);
  } catch (err) {
    toastError(err);
    if (err.body && err.body.state) paint(err.body.state);
  } finally {
    el.rsPassword.value = "";
  }
}

async function start() {
  const reason = el.rsReason.value.trim() || "更新のため";
  if (!(await confirmBox(
    "ほかの PC のツールを止めます(この PC は止めません)。\n\n"
    + "・保存していない図・実行中の処理が無い PC は、まもなく止まります\n"
    + "・残っている PC には帯で知らせ、利用者が保存・終了するのを待ちます\n"
    + "・5分たっても止まらなければ強制で止めます(保存していない図は控えに残し、次の起動で戻します)\n"
    + "・解除するまで、ほかの PC では起動できません\n\n"
    + `理由: ${reason}\nよろしいですか？`,
    { ok: "ほかの PC を止める", danger: true }))) return;
  send("/api/settings/remote-stop/start", { password: el.rsPassword.value, reason });
}

async function clear() {
  if (!(await confirmBox("解除します。ほかの PC もまた起動できるようになります。更新は終わりましたか？",
    { ok: "解除する" }))) return;
  send("/api/settings/remote-stop/clear", { password: el.rsPassword.value });
}

export function attach() {
  for (const id of ["rsState", "rsWhy", "rsRequest", "rsPassword", "rsReason", "rsStart", "rsClear",
                    "rsPcs", "rsPcsWrap"]) {
    el[id] = document.getElementById(id);
  }
  if (!el.rsState) return;
  el.rsStart.addEventListener("click", start);
  el.rsClear.addEventListener("click", clear);
  // 最初の1回は見えていなくても読む(開いたときに「確かめています…」のまま残さない)
  api.get("/api/settings/remote-stop").then((view) => { admin = Boolean(view.administrator); paint(view); })
    .catch(() => {});
  timer = setTimeout(refresh, POLL_MS);
  onLeave(() => { if (timer) clearTimeout(timer); timer = null; });
}
