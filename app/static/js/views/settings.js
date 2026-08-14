/*
  設定画面。

  判断はサーバが済ませてある。ここは受け取ったものを並べるだけ。

  【見張りは自分で持たない】
  走っている処理は `jobs.js` が1か所で見張っていて、帯にも出ている。
  この画面が別に叩くと**同じ事実を二か所で持つ**ことになり、
  片方だけ古い、が起こる(設計.md §1)。ここは受け取る側に回る。
*/

import { api } from "../api.js";
import { onLeave } from "../nav.js";
import { toast, toastError } from "../toast.js";
import * as jobs from "../jobs.js";
import * as tabs from "../tabs.js";
import * as master from "./master.js";

const el = {};
let unwatch = null;
let lastJobId = "";

function setDisabled(disabled) {
  for (const btn of document.querySelectorAll(
    "[data-import], #writeBack, #recompute, #savePaths, #saveBehavior")) {
    // 元から押せないもの(書き戻せない端末など)は押せないまま
    if (btn.dataset.lockedOff === "1") continue;
    btn.disabled = disabled;
  }
}

/** いま押せないボタンを覚えておく。走り終わったときに復活させないため。 */
function rememberLocked() {
  for (const btn of document.querySelectorAll(
    "[data-import], #writeBack, #recompute, #savePaths, #saveBehavior")) {
    if (btn.disabled) btn.dataset.lockedOff = "1";
  }
}

/** よく使う条件の一覧。足すのはロット検索、消すのはここ。 */
function renderFilters(items) {
  el.filterEmpty.hidden = items.length > 0;
  el.filterRows.replaceChildren(...items.map((item) => {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.className = "t";
    name.textContent = item.name;
    const conds = document.createElement("td");
    conds.className = "t";
    // 中身まで見せる。名前だけだと「何で絞るものだったか」が分からない
    conds.textContent = item.conditions.join("  かつ  ");
    const act = document.createElement("td");
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn--danger";
    del.dataset.delete = item.name;
    del.textContent = "× 消す";
    act.append(del);
    tr.append(name, conds, act);
    return tr;
  }));
}

// ------------------------------------------------------------------
// 状態
// ------------------------------------------------------------------
function checkRow(check) {
  const row = document.createElement("div");
  row.className = `check check--${check.level}`;
  const dt = document.createElement("dt");
  dt.textContent = check.label;
  const dd = document.createElement("dd");
  const value = document.createElement("span");
  value.className = "v";
  value.textContent = check.value;
  dd.appendChild(value);
  if (check.detail) {
    const why = document.createElement("span");
    why.className = "why";
    why.textContent = check.detail;
    dd.appendChild(why);
  }
  row.append(dt, dd);
  return row;
}

function sectionCard(section, labels) {
  const card = document.createElement("section");
  card.className = "card";

  const head = document.createElement("header");
  // 見出しの印。**中身の一番重い状態を見出しが背負う**(サーバが決める)
  const badge = document.createElement("i");
  badge.className = `ib ib--sm ib--${section.badge_tone}`;
  badge.setAttribute("aria-hidden", "true");
  badge.textContent = section.mark;
  const title = document.createElement("h3");
  title.textContent = section.title;
  const level = document.createElement("span");
  level.className = `lv lv--${section.level}`;
  level.textContent = labels[section.level] || section.level;
  head.append(badge, title, level);

  const pad = document.createElement("div");
  pad.className = "pad";
  const list = document.createElement("dl");
  list.className = "checks";
  list.append(...section.checks.map(checkRow));
  pad.appendChild(list);

  // 直しに行く先。**問題を見せた面には直す手立てが無い**ので、そこから繋ぐ
  // (文言も行き先もサーバが決める)
  if (section.action) {
    const row = document.createElement("div");
    row.className = "btn-row";
    const go = document.createElement("button");
    go.type = "button";
    go.className = "btn btn--find";
    go.dataset.goto = section.action.tab;
    const glyph = document.createElement("span");
    glyph.className = "gl";
    glyph.textContent = "▸";
    go.append(glyph, document.createTextNode(section.action.label));
    row.appendChild(go);
    pad.appendChild(row);
  }

  card.append(head, pad);
  return card;
}

function renderStatus(state) {
  el.statusGrid.replaceChildren(
    ...state.sections.map((s) => sectionCard(s, LEVEL_LABEL)));
  el.importWhy.hidden = state.can_import;
  el.importWhy.textContent = state.import_reason;
  el.masterDir.value = state.master_dir;
  el.lotDir.value = state.lot_dir;
  el.kanbanDir.value = state.kanban_dir;
  // 相対で書かれたときだけ、実際に見に行く先を出す(サーバが決める)
  showReal(el.masterDirReal, state.master_dir_real);
  showReal(el.lotDirReal, state.lot_dir_real);
  showReal(el.kanbanDirReal, state.kanban_dir_real);
  el.autoImport.checked = state.auto_import;
  el.position.value = state.position;
  el.specUrl.value = state.spec_sheet_url;
  // 不備があるときだけ言う。無いときにも出すと読まれなくなる
  el.specUrlProblem.hidden = !state.spec_sheet_problem;
  el.specUrlProblem.textContent = state.spec_sheet_problem || "";
  // 管理者パスワード。**値は来ない。** 変えてあるかどうかだけ
  if (el.adminState) {
    el.adminState.className = `st st--${state.admin_custom ? "ok" : "warn"}`;
    el.adminState.textContent = state.admin_custom ? "変更済み" : "既定のまま";
  }
  // マスタ編集の認証。プロセスに1つの状態なので、他画面で通しても
  // ここに反映される
  if (el.masterAuthState) {
    el.masterAuthState.className = `st st--${state.admin_authenticated ? "ok" : "warn"}`;
    el.masterAuthState.textContent = state.admin_authenticated ? "認証済み" : "未認証";
  }
  for (const btn of document.querySelectorAll("[data-import]")) {
    btn.disabled = !state.can_import;
    btn.dataset.lockedOff = state.can_import ? "" : "1";
  }
  renderFilters(state.lot_filters || []);
  // **開いていない面の問題を隠さない。** 面で分けたせいで
  // 「足りません」に気づけなくなるなら、スクロールのほうがまだまし
  if (el.settingsTabs) tabs.setBadges(el.settingsTabs, state.tab_badges);
}

/** 相対で書かれた道の行き先。同じ道なら出さない(判断はサーバ)。 */
function showReal(node, text) {
  if (!node) return;
  node.hidden = !text;
  node.textContent = text ? `→ ${text}` : "";
}

// サーバから渡ってくる文言。JSでは決めない
let LEVEL_LABEL = {};

// ------------------------------------------------------------------
// 長時間処理
// ------------------------------------------------------------------
/** 進捗の欄だけを描く。「最近の結果」には触らない。 */
function showJob(job) {
  if (!job) { el.job.hidden = true; return; }
  el.job.hidden = false;
  el.job.className = `job job--${job.state}`;
  el.jobLabel.textContent = job.label;
  el.jobState.textContent = job.state_label;
  el.jobPct.textContent = job.running ? `${job.pct}%`
    : `${job.elapsed_sec.toFixed(1)}秒`;
  el.jobBar.style.width = `${job.pct}%`;
  el.jobMessage.textContent = job.message || job.error || "";
  const summary = job.summary || "";
  el.jobSummary.hidden = !summary;
  el.jobSummary.textContent = summary;
  showLanes(job);
}

/**
 * 段ごとのレーン。**横棒1本では「どの段まで終わったか」が読めない。**
 *
 * 段はサーバが返したものをそのまま並べる ── 何段あるかも、いま何段目かも
 * こちらでは決めない(`packaging_tool/jobs.py` の `Step`)。
 */
function showLanes(job) {
  const steps = job.steps || [];
  el.jobLanes.hidden = steps.length === 0;
  if (!steps.length) { el.jobLanes.replaceChildren(); return; }

  const cards = steps.map((step) => {
    const box = document.createElement("div");
    box.className = `lane lane--${step.state}`;

    const head = document.createElement("div");
    head.className = "lane__h";
    const no = document.createElement("b");
    // 通し番号は**元の並びのまま**。並べきれない段があっても
    // 「19段のうちの17段目」だと分かる
    no.textContent = `${step.no} / ${step.total}`;
    const pill = document.createElement("span");
    pill.className = `st st--${step.state === "done" ? "ok" : step.state}`;
    pill.textContent = step.state_label;
    head.append(no, pill);

    const name = document.createElement("strong");
    name.className = "lane__name";
    name.textContent = step.label;
    name.title = step.label;

    const note = document.createElement("small");
    note.className = "lane__note";
    note.textContent = `${step.elapsed_sec.toFixed(1)}秒`;

    box.append(head, name, note);
    return box;
  });

  // 並べきれなかった段。**黙って落とさない**(文言はサーバが持つ)
  if (job.steps_note) {
    const rest = document.createElement("p");
    rest.className = "lane__rest";
    rest.textContent = job.steps_note;
    cards.push(rest);
  }
  el.jobLanes.replaceChildren(...cards);
}

function renderJob(state) {
  showJob(state.running[0] || state.recent[0] || null);
  el.recentRows.replaceChildren(...state.recent.map(recentRow));
  el.recentEmpty.hidden = state.recent.length > 0;
  setDisabled(state.busy);
  return state.busy;
}

function recentRow(job) {
  const tr = document.createElement("tr");
  for (const [text, cls] of [
    [job.label, ""],
    [job.state_label, `st st--${job.state_kind}`],
    [`${job.elapsed_sec.toFixed(1)}秒`, "n"],
    // 失敗の理由と、成功のまとめは同じ欄に出す。行数が増えても
    // 「何が起きたか」を1行目で読み切れるようにする
    [(job.error || job.summary || "").split("\n")[0], ""],
  ]) {
    const td = document.createElement("td");
    if (cls) td.className = cls;
    td.textContent = text || "---";
    tr.appendChild(td);
  }
  return tr;
}

/** `jobs.js` が新しい状態を掴むたびに呼ばれる。 */
function onJobs(state) {
  renderJob(state);
  const job = state.running[0] || state.recent[0];
  if (state.busy || !job || job.id === lastJobId) return;
  // 走り終わった瞬間に1回だけ知らせて、状態も取り直す
  lastJobId = job.id;
  if (job.ok) toast(`${job.label}が終わりました`, "ok");
  else toast(`${job.label}: ${job.error || "うまくいきませんでした"}`, "ng");
  refreshStatus();
  // 取り込みは総入れ替え。開いたままのマスタ一覧は、この時点で古い
  // (「取り込みなおしてもマスタが表示されない」への対応)
  if (job.ok) master.reload();
}

async function refreshStatus() {
  try {
    renderStatus(await api.get("/api/settings/state"));
  } catch (err) {
    toastError(err);
  }
}

/**
 * マスタ管理の面が開かれたら読みに行く。
 *
 * **開いていない面のために共有フォルダを叩かない。** 設定画面は
 * 毎日何度も開くので、19テーブルを数えるのを毎回待たされては困る
 * (§4.6)。前に見ていた面が復元されたときも同じ扱いにする。
 */
function watchMasterTab() {
  if (!el.settingsTabs) return;
  const tab = el.settingsTabs.querySelector('.tab[data-key="master"]');
  if (tab) tab.addEventListener("click", () => master.opened());
  if (tabs.current(el.settingsTabs) === "master") master.opened();
}

async function startJob(path, body) {
  try {
    const res = await api.post(path, body);
    lastJobId = "";                 // 新しい処理の結果を知らせるため
    // 見張りの1回目を待たずに反応を返す。「最近の結果」は消さない
    // (消すと、押した瞬間だけ履歴が空になって見える)
    showJob(res.job);
    setDisabled(true);
    jobs.refresh();
  } catch (err) {
    // 409(すでに走っている)は異常ではない。押した人に理由を返すだけ
    toastError(err);
    jobs.refresh();
  }
}

// ------------------------------------------------------------------
export function start(state, jobState, masterFrame) {
  for (const id of ["statusGrid", "importWhy", "masterDir", "lotDir", "kanbanDir",
                    "masterDirReal", "lotDirReal", "kanbanDirReal", "autoImport",
                    "position", "specUrl", "specUrlProblem",
                    "filterRows", "filterEmpty", "saveBehavior",
                    "admNow", "admNew", "admConfirm", "admSave", "admReset",
                    "admWhy", "adminState",
                    "masterAuthPass", "masterAuthBtn", "masterAuthWhy", "masterAuthState",
                    "pathAuth", "pathPassword", "pathWhy",
                    "writeBack", "recompute", "savePaths", "refresh",
                    "job", "jobLabel", "jobState", "jobPct", "jobBar",
                    "jobMessage", "jobSummary", "jobLanes", "recentRows", "recentEmpty",
                    "settingsTabs"]) {
    el[id] = document.getElementById(id);
  }
  LEVEL_LABEL = state.level_label || {};
  lastJobId = "";       // 再入場のたびに真っさらから(`nav.js`)
  master.start(masterFrame || {});
  tabs.attachAll();
  rememberLocked();
  watchMasterTab();

  for (const btn of document.querySelectorAll("[data-import]")) {
    btn.addEventListener("click", () =>
      startJob("/api/settings/import", { target: btn.dataset.import }));
  }
  el.writeBack.addEventListener("click", () =>
    startJob("/api/settings/write-back", {}));
  el.recompute.addEventListener("click", () =>
    startJob("/api/settings/recompute", {}));
  el.refresh.addEventListener("click", refreshStatus);

  // 「置き場所を直す」── 状態の面から、直せる面へ移る。
  // 一度だけ張って、描き直された中身にも効くように委譲で拾う
  el.statusGrid.addEventListener("click", (event) => {
    const go = event.target.closest("[data-goto]");
    if (go && el.settingsTabs) tabs.select(el.settingsTabs, go.dataset.goto);
  });

  // 置き場所と動作は**別々に保存する**。片方を直したいだけのときに
  // もう片方まで送ると、他のタブで変えた値を上書きしてしまう
  // 置き場所を**変えるとき**だけ管理者パスワードが要る。要るかどうかを
  // 画面で判断しない ── 「変わったか」はサーバが持っている値との
  // 比べ合わせで、画面が持っているのは打ち込み中の文字だけ
  el.savePaths.addEventListener("click", async () => {
    el.pathWhy.hidden = true;
    const body = {
      master_dir: el.masterDir.value,
      lot_dir: el.lotDir.value,
      kanban_dir: el.kanbanDir.value,
    };
    // 欄が出ているあいだだけ送る。**打っていないのに送らない**
    if (!el.pathAuth.hidden) body.password = el.pathPassword.value;
    try {
      // 保存しただけで終わらせず、その設定で何が見つかるかまで出す
      renderStatus(await api.post("/api/settings/save", body));
      // 打った値は残さない。肩越しに見られる時間を短くする
      el.pathPassword.value = "";
      el.pathAuth.hidden = true;
      toast("置き場所を保存しました。次の取り込みから使われます", "ok");
    } catch (err) {
      if (err.code === "need_password") {
        // 押す前から欄を出さないので、断られてから開く。理由はサーバが持つ
        el.pathAuth.hidden = false;
        el.pathWhy.hidden = false;
        el.pathWhy.textContent = err.message;
        el.pathPassword.focus();
        return;
      }
      toastError(err);
    }
  });

  startAdminPassword();
  startMasterAuth();

  startBrowser();

  // 開いた時点で走っているものがあれば、その進捗から続ける。
  // ブラウザを閉じても処理は続いているので、開き直しても迷子にならない
  if (jobState.recent[0]) lastJobId = jobState.recent[0].id;
  // テンプレートに載っている状態を、見張りの最初の1つとして渡す
  // (帯もこれで描かれる。同じものを二か所で持たない)
  jobs.seed(jobState);

  // 見張りは `jobs.js` が持っている。ここは受け取る口を開けるだけ。
  // **画面を出たら閉じる** ── 消えた要素へ書こうとしないため
  // (処理そのものはプロセス側で続く。基盤仕様書 2.9)
  unwatch = jobs.subscribe(onJobs);
  jobs.refresh();
  onLeave(() => { if (unwatch) unwatch(); unwatch = null; });
}


/* ================================================================
   管理者パスワード

   **値は画面に持たない。** 打った3つを送って、返ってきた状態を写すだけ。
   合っているかどうかの判断はサーバにしかない(`admin_password.py`)。
   ================================================================ */
function startAdminPassword() {
  if (!el.admSave) return;

  const send = async (body) => {
    el.admWhy.hidden = true;
    try {
      const state = await api.post("/api/settings/admin-password", body);
      renderStatus(state);
      // 打った値は残さない。肩越しに見られる時間を短くする
      for (const box of [el.admNow, el.admNew, el.admConfirm]) box.value = "";
      toast(state.message || "変えました", "ok");
    } catch (err) {
      // 断りの理由はサーバが持っている。押した場所のそばに出す
      el.admWhy.hidden = false;
      el.admWhy.textContent = err.message;
    }
  };

  el.admSave.addEventListener("click", () => send({
    current: el.admNow.value, new: el.admNew.value,
    confirm: el.admConfirm.value,
  }));
  el.admReset.addEventListener("click", () => send({
    current: el.admNow.value, reset: true,
  }));
}


/* ================================================================
   マスタ編集の認証

   以前は資材選択画面にしか入力欄が無かった。
   `/api/selection/auth` はプロセスに1つの状態(`selection_session`)を
   触るだけなので、どの画面から認証しても同じ ── 応答は資材選択の
   状態なので、ここでは `admin.authenticated` だけを見て、設定側の
   表示は取り直す(2つの画面で同じ事実を別々に持たない)。
   ================================================================ */
function startMasterAuth() {
  if (!el.masterAuthBtn) return;

  const authenticate = async () => {
    el.masterAuthWhy.hidden = true;
    try {
      const res = await api.post("/api/selection/auth",
        { password: el.masterAuthPass.value });
      el.masterAuthPass.value = "";
      if (res.admin && res.admin.authenticated) {
        toast("認証しました", "ok");
      } else {
        el.masterAuthWhy.hidden = false;
        el.masterAuthWhy.textContent = "パスワードが違います。";
      }
      refreshStatus();
    } catch (err) {
      toastError(err);
    }
  };

  el.masterAuthBtn.addEventListener("click", authenticate);
  el.masterAuthPass.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); authenticate(); }
  });
}


/* ================================================================
   フォルダ参照(§7.2)

   ブラウザのファイル選択ダイアログは**クライアント側**のパスしか
   返さないので、サーバ(=このPC)から見たフォルダは選べない。
   代わりに `GET /api/fs/list` が出したものを並べる。

   出るのは**フォルダ名**と、そこにある **Access ファイルの名前**だけ。
   ファイルの中身を読む口はここに無い。
   ================================================================ */
const browser = {};
let browseTarget = null;

async function browseTo(path) {
  try {
    const view = await api.get(`/api/fs/list?path=${encodeURIComponent(path || "")}`);
    browser.path.value = view.path;
    browser.note.hidden = !view.message;
    browser.note.textContent = view.message;
    browser.note.className = `status status--${view.readable ? "ok" : "ng"}`;
    browser.up.disabled = !view.parent;
    browser.upPath = view.parent;
    browser.pick.disabled = !view.readable;

    const rows = [];
    // 読めなかったときは**出発点(ドライブなど)へ戻す**。空の一覧だけ
    // 出すと、そこから先へ行く手立てが無い ── 置き場所が違っていたときに
    // 探しに行くための機能なのに、行き止まりになる
    const source = view.readable ? [...view.dirs, ...view.files] : view.roots;
    for (const entry of source) {
      const tr = document.createElement("tr");
      if (entry.is_dir) tr.dataset.dir = entry.path;
      // **ファイルも押せる。** 目当てのファイルが見えているのに
      // 「フォルダを選べ」と言われるのは、余計な一手になる
      else tr.dataset.file = entry.path;
      if (entry.name === view.picked) tr.setAttribute("aria-current", "true");
      const td = document.createElement("td");
      const glyph = document.createElement("span");
      glyph.className = "gl";
      // 記号でも種類が分かるようにする。色だけに頼らない
      glyph.textContent = entry.is_dir ? "▸" : "▪";
      td.append(glyph, document.createTextNode(entry.name));
      tr.appendChild(td);
      rows.push(tr);
    }
    browser.rows.replaceChildren(...rows);
  } catch (err) {
    toastError(err);
  }
}

function startBrowser() {
  for (const [key, id] of [["dialog", "browser"], ["path", "browsePath"],
                           ["rows", "browseRows"], ["note", "browseNote"],
                           ["go", "browseGo"], ["up", "browseUp"],
                           ["pick", "browsePick"], ["for", "browseFor"]]) {
    browser[key] = document.getElementById(id);
  }
  if (!browser.dialog) return;

  for (const button of document.querySelectorAll("[data-browse]")) {
    button.addEventListener("click", () => {
      browseTarget = document.getElementById(button.dataset.browse);
      const label = document.querySelector(`label[for="${button.dataset.browse}"]`);
      browser.for.textContent = label ? label.textContent : "";
      browser.dialog.showModal();
      browseTo(browseTarget.value);
    });
  }

  browser.rows.addEventListener("click", (event) => {
    const dir = event.target.closest("tr[data-dir]");
    if (dir) { browseTo(dir.dataset.dir); return; }
    // ファイルを押したら、その入れ物を開いて印を付ける
    // (置き場所として持つのはフォルダ。ファイル名は `source_db` が探す)
    const file = event.target.closest("tr[data-file]");
    if (file) browseTo(file.dataset.file);
  });
  browser.go.addEventListener("click", () => browseTo(browser.path.value));
  browser.path.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); browseTo(browser.path.value); }
  });
  browser.up.addEventListener("click", () => browseTo(browser.upPath));
  browser.pick.addEventListener("click", () => {
    // 選んだだけでは保存しない。「この設定を保存」を押すまで効かない
    if (browseTarget) browseTarget.value = browser.path.value;
    browser.dialog.close();
    toast("フォルダを入れました。「この設定を保存」を押すと効きます。", "info");
  });
}
