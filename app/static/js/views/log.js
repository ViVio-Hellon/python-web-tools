/*
  選定ログの画面。

  サーバは連番より後の行だけを返す。連番が跳んでいたら(古い行が
  上限で捨てられていたら)`reset` が立つので、全部描き直す。
  取りこぼしたまま追記を続けると、表示と実体がずれたことに誰も気づけない。
*/

import { api } from "../api.js";
import { onLeave } from "../nav.js";
import { toast, toastError } from "../toast.js";

const KIND_LABEL = { decide: "決定", exclude: "除外", warn: "警告", info: "情報" };

let rows = [];          // 受け取った全行
let lastSeq = 0;
let kind = "all";
let area = "all";       // パレット / ボード / all
let query = "";

const el = {
  list: null, empty: null, count: null, wrap: null, day: null,
};

// いま(プロセスの中)か、日ごと(ファイル)か。**別の見方**
let scope = "live";
let liveRows = [];      // 「いま」の分。日ごとを見ているあいだも捨てない

function matches(row) {
  if (kind !== "all" && row.kind !== kind) return false;
  if (area !== "all" && row.area !== area) return false;
  if (query && !row.text.toLowerCase().includes(query)) return false;
  return true;
}

function render() {
  const visible = rows.filter(matches);
  el.list.replaceChildren(...visible.map(toItem));
  el.empty.hidden = visible.length > 0;
  if (rows.length && !visible.length) {
    el.empty.textContent = "この条件に合う行がありません。";
  }
  el.count.textContent = visible.length === rows.length
    ? `${rows.length} 行`
    : `${visible.length} / ${rows.length} 行`;
}

function toItem(row) {
  const li = document.createElement("li");
  li.dataset.emphasis = String(row.emphasis);
  li.dataset.kind = row.kind;           // 左の色帯。種別はサーバが決める

  // 時刻は小さく静かに。**どの端末の誰か**は大きく出さず、行の説明に持つ
  // (後から「あの日のあれ」を突き合わせるのに要るが、作業中は邪魔)
  const at = document.createElement("span");
  at.className = "at";
  at.textContent = (row.at || "").slice(11) || "--:--:--";
  li.append(at);
  if (row.who) li.title = `${row.at} / ${row.who}`;

  const kindEl = document.createElement("span");
  // 種別は**印**と**左の色帯**の両方で出す。色を知覚できなくても、
  // 「除外」「注意」の字で分かる
  kindEl.className = `kind kind--${row.kind}`;
  kindEl.textContent = KIND_LABEL[row.kind] || row.kind;

  const text = document.createElement("span");
  text.textContent = row.text;          // textContent なので HTML は混ざらない

  li.append(kindEl, text);
  return li;
}

function atBottom() {
  const { scrollTop, scrollHeight, clientHeight } = el.wrap;
  return scrollHeight - scrollTop - clientHeight < 40;
}

async function poll() {
  try {
    const body = await api.get(`/api/log?since=${lastSeq}`);
    if (body.reset) liveRows = [];
    lastSeq = body.last_seq;
    if (!body.rows.length) return;
    const stick = atBottom();        // 下を見ていたときだけ追従する
    // **足す先は「いま」の分だけ。** 日ごとを見ているあいだに足すと、
    // 読んでいる過去の行に今日の行が割り込む
    liveRows = liveRows.concat(body.rows);
    if (scope !== "live") return;
    rows = liveRows;
    render();
    if (stick) el.wrap.scrollTop = el.wrap.scrollHeight;
  } catch (err) {
    if (err.status !== 0) { /* 接続断は health.js が帯で知らせる */ }
  }
}

/** 見る範囲を切り替える。日ごとに移ったら、その日の分を読みに行く。 */
async function setScope(next) {
  scope = next;
  el.day.hidden = scope !== "days";
  if (scope === "live") {
    rows = liveRows;
    render();
    return;
  }
  try {
    const body = await api.get("/api/log/days");
    // **開く前に、その日に何があったかが分かる**(件数と、動かした人)
    el.day.replaceChildren(...body.days.map((d) => {
      const option = document.createElement("option");
      option.value = d.date;
      const who = d.people.length ? ` / ${d.people.join(", ")}` : "";
      option.textContent = `${d.date}  ${d.lines}行${who}`;
      return option;
    }));
    if (!body.days.length) {
      rows = [];
      render();
      el.empty.textContent = "残っている日がありません。選定を行うと、その日の分が残ります。";
      return;
    }
    await openDay(body.days[0].date);
  } catch (err) { toastError(err); }
}

async function openDay(date) {
  if (!date) return;
  try {
    const body = await api.get(`/api/log/day/${encodeURIComponent(date)}`);
    rows = body.rows;
    render();
    el.wrap.scrollTop = 0;              // 過去の分は**上から**読む
    if (!rows.length) el.empty.textContent = "この日の記録はありません。";
  } catch (err) { toastError(err); }
}

export function start(initial, seq) {
  el.list = document.getElementById("list");
  el.empty = document.getElementById("empty");
  el.count = document.getElementById("count");
  el.wrap = document.getElementById("wrap");
  el.day = document.getElementById("day");

  // 再入場のたびに真っさらから。絞り込みの印はサーバが描き直すので、
  // ここも合わせないと、押されていない印で絞られたままになる
  rows = initial || [];
  liveRows = rows;
  lastSeq = seq || 0;
  kind = "all";
  area = "all";
  query = "";
  scope = "live";
  render();
  el.wrap.scrollTop = el.wrap.scrollHeight;

  // 種別の絞り込み(いま / 日ごと のどちらでも効く)
  document.querySelectorAll(".filter[data-kind]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter[data-kind]").forEach(
        (b) => b.classList.remove("is-on"));
      button.classList.add("is-on");
      kind = button.dataset.kind;
      render();
    });
  });

  // 対象(パレット/ボード)の絞り込み。「ボードの選定に疑問を感じたら
  // ここだけ見る」ができるように(現場の声)
  document.querySelectorAll(".filter[data-area]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter[data-area]").forEach(
        (b) => b.classList.remove("is-on"));
      button.classList.add("is-on");
      area = button.dataset.area;
      render();
    });
  });

  // 見る範囲。**いま**はプロセスの中、**日ごと**はファイルから
  document.querySelectorAll(".filter[data-scope]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter[data-scope]").forEach(
        (b) => b.classList.remove("is-on"));
      button.classList.add("is-on");
      setScope(button.dataset.scope);
    });
  });

  el.day.addEventListener("change", () => openDay(el.day.value));

  document.getElementById("q").addEventListener("input", (event) => {
    query = event.target.value.trim().toLowerCase();
    render();
  });

  document.getElementById("clear").addEventListener("click", async () => {
    try {
      const body = await api.post("/api/log/clear");
      // 消えるのは「いま」の分だけ。日ごとのファイルは残る
      liveRows = [];
      if (scope === "live") rows = [];
      lastSeq = body.last_seq;
      render();
      toast(body.message, "ok");
    } catch (err) { toastError(err); }
  });

  // **画面を出たら止める。** 止めないと、別の画面に居るあいだも
  // ログを取りに行き続け、戻ってくるたびに見張りが1本増える
  const timer = setInterval(poll, window.APP.logPollMs || 1000);
  onLeave(() => clearInterval(timer));
}
