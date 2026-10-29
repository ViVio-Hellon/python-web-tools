/*
  pallet_fit.js — パレット適合設定(専用の窓)

  手順型: ① 何を直す → ② どのパレット → ③ 業界・記号 → ④ 新しい値 → ⑤ 確かめて反映。
  1つの段に1つの問いだけを出し、左にそれまでの答えを積む。

  **業務の判断はサーバが行う**(packaging_tool/fit_setting.py)。直す表と行・変わるパレット・
  直し方の選択肢・断りの文言は、すべてサーバが返したものを描くだけ。

  マスタが変わったら(ほかの端末・この端末のマスタ管理)、開いたまま読み直す。見本を出して
  いれば作り直す(古い見本のまま反映させない)。
*/

// 確かめの窓は画面の中に出す(ブラウザの confirm はデスクトップ版で出ないまま進むため)
import { confirmBox } from "./askbox.js";

const APP = window.APP || {};
const TOKEN = APP.token || "";
// 心拍で名乗る番号(自動終了の見張りが窓ごとに数える)。「いま使っているタブ」とは別
const PAGE_ID = "fit-" + Math.random().toString(36).slice(2, 10);
const STATE_POLL_MS = 10000;

const S = {
  step: 1,
  kind: "",              // "fit" | "size"
  width: String(APP.startWidth || ""),
  length: String(APP.startLength || ""),
  found: null,           // 探した結果
  groupKey: "",
  fit: { w_min: "", w_max: "", l_min: "", l_max: "" },
  size: { new_width: "", new_length: "", codeChange: false, codes: {}, rows: null,
          stock: "keep", limit: "move", merge: true },
  option: "",
  plan: null,
  check: null,
  checkInput: { width: "", length: "" },
  done: null,            // 反映した結果
  error: "",
  notice: "",
  st: null,              // サーバの状態(直せるか・書く先)
  epoch: APP.dataEpoch,
  history: null,
};

// ------------------------------------------------------------------
// サーバとのやりとり
// ------------------------------------------------------------------
async function call(method, path, body) {
  const res = await fetch(path, {
    method,
    cache: "no-store",
    headers: { "X-Tool-Token": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  if ((res.headers.get("Content-Type") || "").includes("application/json")) data = await res.json();
  if (!res.ok) {
    const info = (data && data.error) || {};
    const err = new Error(info.message || (data && data.message) || `通信に失敗しました (HTTP ${res.status})`);
    err.status = res.status;
    err.code = info.code || "";
    err.body = data;
    throw err;
  }
  return data;
}

let busyCount = 0;
async function busy(work, label = "読んでいます…") {
  busyCount += 1;
  const tip = document.getElementById("busy");
  const timer = setTimeout(() => { tip.textContent = label; tip.hidden = false; }, 250);
  try {
    return await work();
  } finally {
    busyCount -= 1;
    clearTimeout(timer);
    if (!busyCount) tip.hidden = true;
  }
}

// ------------------------------------------------------------------
// 描く
// ------------------------------------------------------------------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const range = (lo, hi) => `${esc(lo)}〜${esc(hi)}`;

function group() {
  const groups = (S.found && S.found.groups) || [];
  return groups.find((g) => g.key === S.groupKey) || null;
}

const STEPS = [
  { n: 1, title: "① 何を直す" },
  { n: 2, title: "② どのパレット" },
  { n: 3, title: "③ 業界・記号" },
  { n: 4, title: "④ 新しい値" },
  { n: 5, title: "⑤ 確かめて反映" },
];

function answer(n) {
  const g = group();
  if (n === 1) return S.kind === "fit" ? "適合範囲" : S.kind === "size" ? "サイズ(幅・丈)" : "";
  if (n === 2) return S.found && S.found.groups.length ? `${S.found.width}×${S.found.length}` : "";
  if (n === 3) return g ? g.label : "";
  if (n === 4 && S.step > 4) {
    if (S.kind === "fit") return `巾 ${range(S.fit.w_min, S.fit.w_max)} / 丈 ${range(S.fit.l_min, S.fit.l_max)}`;
    return `${esc(S.size.new_width)}×${esc(S.size.new_length)}${S.size.codeChange ? "(コードも変わる)" : ""}`;
  }
  return "";
}

function renderSteps() {
  const box = document.getElementById("steps");
  box.innerHTML = STEPS.map((s) => {
    const cls = s.n === S.step ? "now" : s.n < S.step ? "done" : "later";
    return `<button type="button" class="step ${cls}" data-act="goto" data-step="${s.n}"
              ${s.n < S.step ? "" : "disabled"}>
              <span class="n">${s.title}</span><span class="ans">${answer(s.n)}</span></button>`;
  }).join("") + `<p class="hint">済んだ段を押すと、そこからやり直せます。</p>`;
}

function renderStatus() {
  const st = S.st;
  const box = document.getElementById("status");
  const where = document.getElementById("where");
  if (!st) return;
  const chips = [];
  if (st.can_edit) chips.push(`<span class="chip ok">直せます(資材モード・認証済み)</span>`);
  else chips.push(`<span class="chip ng" title="${esc(st.why)}">直せません</span>`);
  if (!st.admin) {
    chips.push(`<form class="auth" data-form="auth"><input type="password" name="password"
                  placeholder="管理者パスワード" autocomplete="current-password" aria-label="管理者パスワード">
                  <button type="submit">認証</button></form>`);
  }
  box.innerHTML = chips.join("");
  if (st.checking && !st.where) {
    where.textContent = "共有の置き場所を確かめています…";
  } else if (st.where) {
    where.innerHTML = `<span>閾値マスタ: <b>${esc(st.where.threshold || "(見つかりません)")}</b></span>
                       <span>梱包資材マスタ: <b>${esc(st.where.master || "(見つかりません)")}</b></span>
                       ${st.can_edit ? "" : `<span>${esc(st.why)}</span>`}`;
  }
}

function errorBox() {
  const parts = [];
  if (S.notice) parts.push(`<div class="box">${esc(S.notice)}</div>`);
  if (S.error) parts.push(`<div class="box err">${esc(S.error)}</div>`);
  return parts.join("");
}

function renderMain() {
  const main = document.getElementById("main");
  const views = { 1: viewKind, 2: viewFind, 3: viewGroup, 4: S.kind === "size" ? viewSize : viewFit, 5: viewPlan };
  main.innerHTML = errorBox() + views[S.step]();
  const focus = main.querySelector("[autofocus]");
  if (focus) focus.focus();
}

function render() {
  renderSteps();
  renderStatus();
  renderMain();
}

// ① 何を直す
function viewKind() {
  return `<h2>① 何を直しますか</h2>
    <p class="lead">直す表と行はこの画面が決めます。反映する前に、変わるパレットを全部見せます。</p>
    <div class="choices">
      <button type="button" class="choice ${S.kind === "fit" ? "on" : ""}" data-act="kind" data-kind="fit">
        <b>適合範囲を直す</b>
        <span>このパレットに載せる製品の幅・丈の範囲(巾適合・丈適合)。
          「この製品にこのパレットが出ない・出てほしくない」とき。</span></button>
      <button type="button" class="choice ${S.kind === "size" ? "on" : ""}" data-act="kind" data-kind="size">
        <b>サイズを直す</b>
        <span>パレットそのものの幅・丈。マスタの寸法が違っていた・新しい寸法に入れ替わるとき。</span></button>
    </div>`;
}

// ② どのパレット
function viewFind() {
  const f = S.found;
  let result = "";
  if (f && !f.groups.length) {
    result = `<div class="box warn">${esc(f.message)}
      ${f.nearby.length ? `<ul>${f.nearby.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}</div>`;
  }
  return `<h2>② どのパレットですか</h2>
    <p class="lead">パレットの幅と丈を入れて「探す」を押してください(台・枚・組をまとめて探します)。</p>
    <form class="row" data-form="find">
      <label>幅 <input class="num" name="width" inputmode="numeric" value="${esc(S.width)}" autofocus></label>
      <span>×</span>
      <label>丈 <input class="num" name="length" inputmode="numeric" value="${esc(S.length)}"></label>
      <button class="btn find" type="submit">探す</button>
    </form>${result}`;
}

// ③ 業界・記号
function viewGroup() {
  const f = S.found;
  const cards = f.groups.map((g) => {
    const rows = g.rows.map((r) => r.kind === "stock"
      ? `<li>在庫の行 位置 ${esc(r.position)}・${esc(r.unit)} ${esc(r.stock)}</li>`
      : `<li>${esc(r.unit)} ${esc(r.code || "(コードなし)")}</li>`).join("");
    return `<button type="button" class="group ${g.key === S.groupKey ? "on" : ""}" data-act="group" data-key="${esc(g.key)}">
      <b>${esc(g.label)}</b><span class="rule">${esc(g.rule_label)}</span>
      <div>巾適合 ${range(g.fit.巾適合min, g.fit.巾適合max)} / 丈適合 ${range(g.fit.丈適合min, g.fit.丈適合max)}
        ・桁 ${esc(g.fit.桁数)}・脚 ${esc(g.fit.脚数)}</div>
      <ul>${rows}</ul></button>`;
  }).join("");
  return `<h2>③ 業界・記号はどれですか</h2>
    <p class="lead">${esc(f.width)}×${esc(f.length)} には次の ${f.groups.length}組 があります。直すものを選んでください。
      札は、いま適合範囲を決めている表です。</p>
    <div class="groups">${cards}</div>`;
}

// ④ 適合
function viewFit() {
  const g = group();
  return `<h2>④ 新しい適合範囲を入れてください</h2>
    <p class="lead">${esc(g.label)}(${esc(S.found.width)}×${esc(S.found.length)})。
      いまは<b>${esc(g.rule_label)}</b>で決まっています。変えない向きは、いまの値のままにしてください。</p>
    <form data-form="fit">
      <div class="fitgrid">
        <label>巾適合</label>
        <div class="row"><input class="num" name="w_min" value="${esc(S.fit.w_min)}" inputmode="numeric" autofocus>
          〜 <input class="num" name="w_max" value="${esc(S.fit.w_max)}" inputmode="numeric">
          <span class="cur">いま ${range(g.fit.巾適合min, g.fit.巾適合max)}</span></div>
        <label>丈適合</label>
        <div class="row"><input class="num" name="l_min" value="${esc(S.fit.l_min)}" inputmode="numeric">
          〜 <input class="num" name="l_max" value="${esc(S.fit.l_max)}" inputmode="numeric">
          <span class="cur">いま ${range(g.fit.丈適合min, g.fit.丈適合max)}</span></div>
      </div>
      <button class="btn primary" type="submit">変わるところを見る</button>
      <span class="muted">(まだ書きません)</span>
    </form>`;
}

// ④ サイズ
function viewSize() {
  const g = group();
  const catalog = g.rows.filter((r) => r.kind === "catalog");
  const stock = g.rows.filter((r) => r.kind === "stock");
  const picked = S.size.rows || new Set(catalog.map((r) => r.code));
  const rowsHtml = catalog.length ? `<div class="rowsgrid">${catalog.map((r) => `
      <label><input type="checkbox" name="row" value="${esc(r.code)}" ${picked.has(r.code) ? "checked" : ""}>
        ${esc(r.unit)} ${esc(r.code)}</label>
      <span>${S.size.codeChange ? `→ 新しいコード <input class="code" name="code:${esc(r.code)}"
          value="${esc(S.size.codes[r.code] || "")}" aria-label="${esc(r.code)} の新しいコード">` : ""}</span>`).join("")}</div>` : "";
  const stockHtml = stock.length ? `
      <h3>倉庫にある前のサイズの在庫(${stock.length}行)</h3>
      <label class="row"><input type="radio" name="stock" value="keep" ${S.size.stock === "keep" ? "checked" : ""}>
        前の寸法のまま残す(ふだんはこちら。在庫がある間は前のサイズも候補に出て、払い出しで0になると消えます)</label>
      <label class="row"><input type="radio" name="stock" value="move" ${S.size.stock === "move" ? "checked" : ""}>
        在庫の行も新しい寸法にする(マスタの寸法が書き間違いだったとき)</label>` : "";
  return `<h2>④ 新しいサイズを入れてください</h2>
    <p class="lead">${esc(g.label)}(いま ${esc(S.found.width)}×${esc(S.found.length)})。</p>
    <form data-form="size">
      <div class="row"><label>新しい 幅 <input class="num" name="new_width" value="${esc(S.size.new_width)}"
          inputmode="numeric" autofocus></label> ×
        <label>丈 <input class="num" name="new_length" value="${esc(S.size.new_length)}" inputmode="numeric"></label></div>
      <h3>コードは変わりますか?</h3>
      <label class="row"><input type="radio" name="codeChange" value="no" ${S.size.codeChange ? "" : "checked"}>
        変わらない(いまの行の寸法を書き換えます)</label>
      <label class="row"><input type="radio" name="codeChange" value="yes" ${S.size.codeChange ? "checked" : ""}>
        変わる(新しいコードの行を足して、前の行を消します。業界・記号・単位は写します)</label>
      <h3>直す行</h3>${rowsHtml || `<p class="muted">コードのある行がありません。</p>`}
      ${stockHtml}
      <h3>前の寸法を決め打っているパレット限定条件</h3>
      <label class="row"><input type="radio" name="limit" value="move" ${S.size.limit === "move" ? "checked" : ""}>
        新しい寸法へ書き換える(前の寸法にほかのパレットが残るときは、書き換えずにそう言います)</label>
      <label class="row"><input type="radio" name="limit" value="keep" ${S.size.limit === "keep" ? "checked" : ""}>
        そのままにする</label>
      <label class="row"><input type="checkbox" name="merge" ${S.size.merge ? "checked" : ""}>
        使われなくなった「この寸法だけの帯」は元の帯につなぎ直す</label>
      <div class="apply"><button class="btn primary" type="submit">変わるところを見る</button>
        <span class="muted">(まだ書きません)</span></div>
    </form>`;
}

function fitCell(before, after, lo, hi) {
  const b = before ? range(before[lo], before[hi]) : "";
  const a = after ? range(after[lo], after[hi]) : "";
  if (b === a) return b;
  return `${b} → <span class="chg">${a}</span>`;
}

function valueCell(before, after, col) {
  const b = before ? before[col] : "";
  const a = after ? after[col] : "";
  return b === a ? esc(b) : `${esc(b)} → <span class="chg">${esc(a)}</span>`;
}

// ⑤ 確かめて反映
function viewPlan() {
  if (S.done) return viewDone();
  const p = S.plan;
  const options = (p.options || []).length > 1 ? `<h3>直し方</h3><div class="opts">${p.options.map((o) => `
      <label class="opt ${o.selected ? "on" : ""} ${o.available ? "" : "off"}">
        <input type="radio" name="option" value="${esc(o.id)}" ${o.selected ? "checked" : ""} ${o.available ? "" : "disabled"}>
        <span><b>${esc(o.label)}</b><small>${esc(o.note)}</small></span></label>`).join("")}</div>`
    // 直し方が1つだけ(固定の表の行を直す)でも、どこまで効くかは必ず言う
    : (p.options || []).length === 1 ? `<div class="box"><b>${esc(p.options[0].label)}</b>: ${esc(p.options[0].note)}</div>` : "";
  const ops = p.ops.length ? `<h3>直す先(自動で決めました)</h3><ul>${p.ops.map((o) =>
      `<li>${esc(o.table)}: ${esc(o.note)}</li>`).join("")}</ul>` : "";
  const warn = p.warnings.length ? `<div class="box warn"><ul>${p.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
  const notes = p.notes.length ? `<div class="box"><ul>${p.notes.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
  const rows = p.affected.map((a) => {
    const size = (a.width_after !== a.width || a.length_after !== a.length)
      ? `${esc(a.width)}×${esc(a.length)} → <span class="chg">${esc(a.width_after)}×${esc(a.length_after)}</span>`
      : `${esc(a.width)}×${esc(a.length)}`;
    const code = a.code_after && a.code_after !== a.code
      ? `${esc(a.code)} → <span class="chg">${esc(a.code_after)}</span>` : esc(a.code || (a.position ? `在庫 ${a.position}` : ""));
    const shared = a.shared_now ? `<span class="tag" title="共有のPalletMasterに書いてある値(Access側の計算)。各端末は計算し直した値を使います">共有の値
        巾${range(a.shared_now.巾適合min, a.shared_now.巾適合max)}・丈${range(a.shared_now.丈適合min, a.shared_now.丈適合max)}</span>` : "";
    return `<tr class="${a.target ? "" : "other"}">
      <td><span class="tag ${a.target ? "me" : ""}">${a.target ? "選んだ" : "ほか"}</span></td>
      <td>${code}</td><td>${esc(a.unit)}</td><td>${size}</td>
      <td>${esc(a.industry)}${a.symbol ? "・" + esc(a.symbol) : ""}</td>
      <td class="num">${fitCell(a.before, a.after, "巾適合min", "巾適合max")}</td>
      <td class="num">${fitCell(a.before, a.after, "丈適合min", "丈適合max")}</td>
      <td class="num">${valueCell(a.before, a.after, "桁数")}</td>
      <td class="num">${valueCell(a.before, a.after, "脚数")}</td>
      <td class="free">${shared}</td></tr>`;
  }).join("");
  const more = p.affected_more ? `<p class="muted">ほか ${p.affected_more}行 は出しきれていません(数には入っています)。</p>` : "";
  const table = p.affected_count ? `<h3>変わるパレット ${p.affected_count}行</h3>
    <div class="scroll"><table><thead><tr><th></th><th>コード</th><th>単位</th><th>幅×丈</th><th>業界・記号</th>
      <th class="num">巾適合</th><th class="num">丈適合</th><th class="num">桁</th><th class="num">脚</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>${more}` : `<p class="muted">変わるパレットはありません。</p>`;
  const st = S.st || {};
  const can = st.can_edit && p.can_apply;
  return `<h2>⑤ 変わるところを確かめて反映</h2>
    <p class="summary">${esc(p.summary)}</p>${options}${ops}${warn}${notes}${table}
    <h3>製品で確かめる</h3>
    <form class="check" data-form="check">
      製品の 幅 <input class="num" name="width" value="${esc(S.checkInput.width)}" inputmode="numeric">
      × 丈 <input class="num" name="length" value="${esc(S.checkInput.length)}" inputmode="numeric">
      <button class="btn small" type="submit">候補の前と後を見る</button>
    </form>${viewCheck()}
    <div class="apply">
      <button class="btn primary" type="button" data-act="apply" ${can ? "" : "disabled"}>
        共有のマスタへ反映する(変わるパレット ${p.affected_count}行)</button>
      <span class="muted">${can ? "反映する前に、共有のファイルを手元へ控えます。記録から元に戻せます。"
        : esc(st.can_edit ? "変えるものがありません。" : (st.why || "直せません"))}</span>
    </div>`;
}

function viewCheck() {
  const c = S.check;
  if (!c) return "";
  const list = (xs) => xs.length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "なし";
  return `<div class="box"><b>製品 ${esc(c.width)}×${esc(c.length)}</b>: 候補 前 ${c.before}件 → 後 ${c.after}件
      <div>出るようになる: ${list(c.added)}</div><div>出なくなる: ${list(c.removed)}</div>
      <small class="muted">${esc(c.note)}</small></div>`;
}

function viewDone() {
  const d = S.done;
  const next = S.kind === "size" && d.ok && !d.undone
    ? `<button class="btn" type="button" data-act="continue-fit">続けて、新しい寸法の適合範囲を直す</button>` : "";
  return `<h2>⑤ ${d.undone ? "元に戻しました" : d.ok ? "反映しました" : "途中で止まりました"}</h2>
    <div class="box ${d.ok ? "ok" : "err"}">${esc(d.message)}</div>
    <div class="apply">${next}<button class="btn" type="button" data-act="restart">最初から</button></div>`;
}

function renderHistory() {
  const box = document.getElementById("history");
  const rows = S.history;
  if (!rows) return;
  if (!rows.length) { box.innerHTML = `<p class="muted">まだ記録はありません。</p>`; return; }
  const can = S.st && S.st.can_edit;
  box.innerHTML = `<div class="scroll"><table><thead><tr><th class="num">番号</th><th>日時</th><th>端末・利用者</th>
      <th>種類</th><th>内容</th><th class="num">行数</th><th></th></tr></thead><tbody>${rows.map((r) => `
      <tr><td class="num">${esc(r.番号)}</td><td>${esc(r.日時)}</td><td>${esc(r.端末)} ${esc(r.利用者)}</td>
        <td>${esc(r.種類)}</td><td class="free">${esc(r.内容)}</td><td class="num">${esc(r.行数)}</td>
        <td>${Number(r.戻した) ? `戻した ${esc(r.戻した日時)}` : r.種類 === "元に戻す" ? ""
          : `<button class="btn small" type="button" data-act="undo" data-no="${esc(r.番号)}" ${can ? "" : "disabled"}>元に戻す</button>`}</td>
      </tr>`).join("")}</tbody></table></div>`;
}

// ------------------------------------------------------------------
// 動き
// ------------------------------------------------------------------
function show(err) {
  S.error = err ? err.message || String(err) : "";
  render();
}

async function refreshState() {
  try {
    const st = await call("GET", "/api/pallet-fit/state");
    const changed = S.epoch !== undefined && st.epoch !== S.epoch;
    S.st = st;
    if (st.where && st.where.history) { S.history = st.where.history; renderHistory(); }
    if (changed) {
      S.epoch = st.epoch;
      await rereadAfterMasterChange();
    } else {
      renderStatus();
      if (S.step === 5) renderMain();          // 直せるかが変わったら、反映の押せる・押せないを描き直す
    }
  } catch (err) {
    // 見張りの失敗は画面を止めない(次の問い合わせでやり直す)
    console.warn("状態を読めませんでした", err);
  }
}

/** マスタが変わった。探した結果を読み直し、見本を出していれば作り直す(古い見本で反映させない)。 */
async function rereadAfterMasterChange() {
  if (!S.found || S.done) { render(); return; }
  try {
    S.found = await call("POST", "/api/pallet-fit/find", { width: S.found.width, length: S.found.length });
    if (!group()) {
      S.step = Math.min(S.step, 3);
      S.notice = "マスタが変わったので読み直しました。選んでいた組が無くなったので、選び直してください。";
      S.plan = null;
    } else if (S.step === 5 && S.plan) {
      S.plan = (await call("POST", "/api/pallet-fit/plan", planRequest())).plan;
      S.check = null;
      S.notice = "マスタが変わったので、見本を作り直しました。もう一度確かめてください。";
    } else {
      S.notice = "マスタが変わったので読み直しました。";
    }
    S.error = "";
  } catch (err) {
    S.plan = null;
    S.step = Math.min(S.step, 4);
    S.notice = "マスタが変わったので見本を捨てました。";
    S.error = err.message;
  }
  render();
}

function planRequest() {
  const base = { width: S.found.width, length: S.found.length, group: S.groupKey };
  if (S.kind === "fit") return { kind: "fit", ...base, ...numbers(S.fit), option: S.option };
  const codes = {};
  if (S.size.codeChange) for (const code of S.size.rows || []) codes[code] = S.size.codes[code] || "";
  return { kind: "size", ...base, new_width: Number(S.size.new_width), new_length: Number(S.size.new_length),
           rows: [...(S.size.rows || [])], codes, stock: S.size.stock, limit: S.size.limit, merge: S.size.merge };
}

function numbers(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) out[k] = Number(v);
  return out;
}

async function doFind(width, length) {
  S.width = width;
  S.length = length;
  S.notice = "";
  try {
    S.found = await busy(() => call("POST", "/api/pallet-fit/find", { width, length }), "探しています…");
    S.error = "";
    S.groupKey = "";
    S.plan = null;
    if (S.found.groups.length) S.step = 3;
  } catch (err) { S.error = err.message; }
  render();
}

function pickGroup(key) {
  S.groupKey = key;
  const g = group();
  S.fit = { w_min: g.fit.巾適合min, w_max: g.fit.巾適合max, l_min: g.fit.丈適合min, l_max: g.fit.丈適合max };
  const catalog = g.rows.filter((r) => r.kind === "catalog" && r.code);
  S.size = { new_width: S.size.new_width || "", new_length: S.size.new_length || "", codeChange: false, codes: {},
             rows: new Set(catalog.map((r) => r.code)), stock: "keep", limit: "move", merge: true };
  S.option = "";
  S.plan = null;
  S.step = 4;
  S.error = "";
  S.notice = "";
  render();
}

async function makePlan() {
  try {
    S.plan = (await busy(() => call("POST", "/api/pallet-fit/plan", planRequest()), "変わるところを計算しています…")).plan;
    S.option = S.plan.option || "";
    S.check = null;
    S.error = "";
    S.notice = "";
    S.step = 5;
  } catch (err) { S.error = err.message; }
  render();
}

async function doApply() {
  try {
    const got = await busy(() => call("POST", "/api/pallet-fit/apply", { plan_id: S.plan.id }), "共有のマスタへ書いています…");
    S.done = { ok: true, message: got.message };
    S.history = got.history;
    S.error = "";
  } catch (err) {
    if (err.body && err.body.history) S.history = err.body.history;
    if (err.code === "changed" || err.code === "expired") {
      S.error = err.message;
    } else if (err.body && err.body.log_no) {
      S.done = { ok: false, message: err.message };     // 片方だけ書けた(記録は残した)
    } else {
      S.error = err.message;
    }
  }
  await refreshState();
  render();
  renderHistory();
}

async function doUndo(no) {
  const ok = await confirmBox(`記録 ${no} を元に戻します。共有のマスタを書き換えます(書く前に控えを取ります)。`,
                              { ok: "元に戻す", danger: true });
  if (!ok) return;
  try {
    const got = await busy(() => call("POST", "/api/pallet-fit/undo", { log_no: Number(no) }), "元に戻しています…");
    S.history = got.history;
    // 反映した直後に戻したなら、⑤ の段も「元に戻しました」にする(「反映しました」を残さない)
    if (S.done) S.done = { ok: true, undone: true, message: got.message };
    else S.notice = got.message;
    S.error = "";
  } catch (err) {
    if (err.body && err.body.history) S.history = err.body.history;
    S.error = err.message;
  }
  renderHistory();
  await refreshState();
  render();
}

async function doAuth(form) {
  const password = form.password.value;
  try {
    await call("POST", "/api/settings/admin-auth", { password });
    S.error = "";
  } catch (err) { S.error = err.message; }
  await refreshState();
  render();
  renderHistory();
}

function readForm(form) {
  if (form.dataset.form === "fit") {
    S.fit = { w_min: form.w_min.value, w_max: form.w_max.value, l_min: form.l_min.value, l_max: form.l_max.value };
  } else if (form.dataset.form === "size") {
    S.size.new_width = form.new_width.value;
    S.size.new_length = form.new_length.value;
    S.size.codeChange = form.querySelector('input[name="codeChange"]:checked').value === "yes";
    S.size.rows = new Set([...form.querySelectorAll('input[name="row"]:checked')].map((i) => i.value));
    for (const input of form.querySelectorAll('input[name^="code:"]')) S.size.codes[input.name.slice(5)] = input.value.trim();
    const stock = form.querySelector('input[name="stock"]:checked');
    if (stock) S.size.stock = stock.value;
    S.size.limit = form.querySelector('input[name="limit"]:checked').value;
    S.size.merge = form.merge.checked;
  }
}

document.addEventListener("submit", async (event) => {
  const form = event.target;
  event.preventDefault();
  const kind = form.dataset.form;
  if (kind === "auth") return doAuth(form);
  if (kind === "find") return doFind(form.width.value.trim(), form.length.value.trim());
  if (kind === "fit" || kind === "size") { readForm(form); return makePlan(); }
  if (kind === "check") {
    S.checkInput = { width: form.width.value, length: form.length.value };
    try {
      S.check = await busy(() => call("POST", "/api/pallet-fit/check",
        { plan_id: S.plan.id, width: Number(form.width.value), length: Number(form.length.value) }));
      S.error = "";
    } catch (err) { S.error = err.message; }
    render();
  }
});

document.addEventListener("change", (event) => {
  const input = event.target;
  // サイズの段: コードが変わるかどうかで、新しいコードの欄を出し入れする
  if (input.name === "codeChange") {
    readForm(input.form);
    render();
  }
  // 直し方を変えたら見本を作り直す
  if (input.name === "option" && S.step === 5) {
    S.option = input.value;
    makePlan();
  }
});

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-act]");
  if (!target || target.disabled) return;
  const act = target.dataset.act;
  if (act === "kind") {
    S.kind = target.dataset.kind;
    S.step = 2;
    S.plan = null;
    S.done = null;
    S.error = "";
    render();
    if (S.width && S.length && !S.found) doFind(S.width, S.length);
  } else if (act === "goto") {
    S.step = Number(target.dataset.step);
    S.done = null;
    S.error = "";
    S.notice = "";
    render();
  } else if (act === "group") {
    pickGroup(target.dataset.key);
  } else if (act === "apply") {
    doApply();
  } else if (act === "undo") {
    doUndo(target.dataset.no);
  } else if (act === "restart") {
    Object.assign(S, { step: 1, kind: "", found: null, groupKey: "", plan: null, done: null, check: null,
                       error: "", notice: "" });
    render();
  } else if (act === "continue-fit") {
    const w = S.size.new_width;
    const l = S.size.new_length;
    const key = S.groupKey;
    Object.assign(S, { kind: "fit", plan: null, done: null, check: null, error: "", notice: "" });
    doFind(w, l).then(() => { if (S.found && S.found.groups.some((g) => g.key === key)) pickGroup(key); });
  }
});

// ------------------------------------------------------------------
// 心拍(この窓だけ開いているときに、自動終了で落とさない)
// ------------------------------------------------------------------
function alive(body) {
  const payload = JSON.stringify({ screen_id: PAGE_ID, ...body });
  if (body.leaving && navigator.sendBeacon) {
    navigator.sendBeacon("/api/alive", new Blob([payload], { type: "application/json" }));
    return;
  }
  fetch("/api/alive", { method: "POST", headers: { "Content-Type": "application/json" }, body: payload })
    .catch(() => {});
}

function start() {
  render();
  refreshState();
  setInterval(refreshState, STATE_POLL_MS);
  alive({ visible: !document.hidden });
  setInterval(() => alive({ visible: !document.hidden }), APP.alivePollMs || 20000);
  document.addEventListener("visibilitychange", () => alive({ visible: !document.hidden }));
  window.addEventListener("pagehide", () => alive({ leaving: true }));
}

start();
