/*
  設定画面。

  判断はサーバが済ませてある。ここは受け取ったものを並べるだけ。

  【見張りは自分で持たない】
  走っている処理は `jobs.js` が1か所で見張っていて、帯にも出ている。
  この画面が別に叩くと**同じ事実を二か所で持つ**ことになり、
  片方だけ古い、が起こる(設計.md §1)。ここは受け取る側に回る。
*/

import { api, tokenUrl } from "../api.js";
import { onLeave } from "../nav.js";
import { toast, toastError } from "../toast.js";
import * as jobs from "../jobs.js";
import * as tabs from "../tabs.js";
import * as trace from "./trace.js";
// `master.js` は静的 import ではなく、**自分と同じ版クエリを付けて**
// 動的に読み込む。
//
// なぜか: 版を付けるのは `url_for('static', ...)` で、それが効くのは
// **テンプレートが名指しするファイルだけ**(このファイル自身)。この
// ファイルの中の `import ... from "./master.js"` は素のURLになり、
// 版が付かない。付かないファイルは `no-cache`(使う前に必ず確かめる)に
// してあるので通常は直るが、共有フォルダ越しの配布やプロキシを挟む
// 環境では確認そのものが働かないことがあり、**「設定画面(入口)は新しい
// のにマスタ管理(中身)だけ古い」**という、過去に共有モジュール
// (`mapedit.js`)で実際に踏んだ壊れ方(`docs/変更履歴.md` 参照)と同じ
// 形になりうる。`import.meta.url` からいま自分が読み込まれたときの
// 版クエリを取り、`master.js` にも同じものを付けて呼べば、設定画面を
// 入れ替えたときに中身も必ず一緒に入れ替わる。
const VERSION_QUERY = new URL(import.meta.url).search;
const master = await import(`./master.js${VERSION_QUERY}`);

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

/**
 * ボード人気度。**どのサイズをよく使っているか。**
 *
 * パレットをどれだけ覆えたか(被覆率)とは別の話 ── そちらは1回の
 * 配置の出来ばえで、資材選択の配置図の下に「使用率」「はみ出し」と
 * して出ている。同じ語を2か所で違う意味に使わない。
 *
 * 分母はボード一覧ぜんぶ。使っていない寸法も0%で並べる ── 一覧に
 * 載っているのに使っていないサイズを見つけるのもこの表の役目で、
 * 隠すと「無い」のか「0回」なのか区別できなくなる。
 */
function renderBoardUsage(usage) {
  const rows = usage.rows || [];
  const unlisted = usage.unlisted || [];

  el.usageRateEmpty.hidden = rows.length > 0;
  // 見出しの文はサーバが作る(全端末の合計か・送れていない分があるか)
  el.usageSummary.textContent = usage.summary || "0 件";

  const cells = (values, right) => values.map((value, index) => {
    const td = document.createElement("td");
    if (right.includes(index)) td.className = "n";
    td.textContent = value;
    return td;
  });

  el.usageRateRows.replaceChildren(...rows.map((r) => {
    const tr = document.createElement("tr");
    // 使っていない行は薄く。**消さずに薄くする** ── 並んでいること
    // 自体が「一覧にはあるが使っていない」という事実だから
    if (!r.used) tr.classList.add("off");
    tr.append(...cells([r.board_type, r.width, r.length, r.sheets, r.times,
                        `${r.share}%`], [1, 2, 3, 4, 5]));
    // 棒。数字だけだと順位の差が読み取れない
    const bar = document.createElement("td");
    bar.className = "t";
    const fill = document.createElement("span");
    fill.className = "popbar";
    fill.style.setProperty("--w", `${r.share}%`);
    fill.setAttribute("aria-hidden", "true");
    bar.appendChild(fill);
    tr.appendChild(bar);
    tr.append(...cells([r.last_used_at || "—"], []));
    return tr;
  }));

  el.usageUnlistedCard.hidden = unlisted.length === 0;
  el.usageUnlistedRows.replaceChildren(...unlisted.map((r) => {
    const tr = document.createElement("tr");
    tr.append(...cells([r.board_type, r.width, r.length, r.sheets,
                        r.last_used_at || "—"], [1, 2, 3]));
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
  // 言葉はサーバが決める。「要確認」で済むところと、することを
  // 書いたほうがよいところがある(`Section.label`)
  level.textContent = section.label || labels[section.level] || section.level;
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
  renderDistribution(state.distribution);
  el.statusGrid.replaceChildren(
    ...state.sections.map((s) => sectionCard(s, LEVEL_LABEL)));
  el.importWhy.hidden = state.can_import;
  el.importWhy.textContent = state.import_reason;
  el.masterDir.value = state.master_dir;
  el.lotDir.value = state.lot_dir;
  el.kanbanDir.value = state.kanban_dir;
  el.thresholdDir.value = state.threshold_dir;
  el.exportDir.value = state.export_dir;
  // 相対で書かれたときだけ、実際に見に行く先を出す(サーバが決める)
  showReal(el.masterDirReal, state.master_dir_real);
  showReal(el.lotDirReal, state.lot_dir_real);
  showReal(el.kanbanDirReal, state.kanban_dir_real);
  showReal(el.thresholdDirReal, state.threshold_dir_real);
  showReal(el.exportDirReal, state.export_dir_real);
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
  renderBoardUsage(state.board_usage || {});
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
  // ログの面も同じ。エラー記録は開いたときに読む
  const logs = el.settingsTabs.querySelector('.tab[data-key="logs"]');
  if (logs) logs.addEventListener("click", () => trace.opened());
  if (tabs.current(el.settingsTabs) === "logs") trace.opened();
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
  for (const id of ["statusGrid", "importWhy", "masterDir", "lotDir", "kanbanDir", "thresholdDir",
                    "exportDir", "exportDirReal", "exportUsage",
                    "masterDirReal", "lotDirReal", "kanbanDirReal",
                    "thresholdDirReal", "autoImport",
                    "position", "specUrl", "specUrlProblem",
                    "filterRows", "filterEmpty", "saveBehavior",
                    "usageRateRows", "usageRateEmpty", "usageSummary",
                    "usageUnlistedCard", "usageUnlistedRows",
                    "admNow", "admNew", "admConfirm", "admSave", "admReset",
                    "admWhy", "adminState",
                    "distState", "distMeta", "distRows", "distPath", "distPassword",
                    "distWhy", "distExport", "distReapply", "distRemove",
                    "masterAuthPass", "masterAuthBtn", "masterAuthWhy", "masterAuthState",
                    "pathAuth", "pathPassword", "pathWhy",
                    "writeBack", "recompute", "savePaths", "refresh",
                    "importDiag", "importDiagSave",
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
  // ログの面を繋いでから、開いている面を見る(先に見ると、ログの面で
  // 開いたときに一覧を読まない)
  trace.start();
  watchMasterTab();

  for (const btn of document.querySelectorAll("[data-import]")) {
    btn.addEventListener("click", () =>
      startJob("/api/settings/import", { target: btn.dataset.import }));
  }
  el.writeBack.addEventListener("click", () =>
    startJob("/api/settings/write-back", {}));
  startBring();
  // 取り込みの記録。ログフォルダは隠しフォルダの中なので、画面から開く
  el.importDiag.addEventListener("click", () =>
    window.open(tokenUrl("/report/import-diag"), "_blank", "noopener"));
  el.importDiagSave.addEventListener("click", () => {
    window.location.href = tokenUrl("/report/import-diag?save=1");
  });
  el.recompute.addEventListener("click", () =>
    startJob("/api/settings/recompute", {}));
  el.refresh.addEventListener("click", refreshStatus);

  // ボード人気度をCSVに。**書いた場所をそのまま出す** ── 書き出しで
  // いちばん困るのは「書けたのに、どこにあるか分からない」
  el.exportUsage.addEventListener("click", async () => {
    try {
      const state = await api.post("/api/settings/board-usage/export",
                                   { dir: el.exportDir.value });
      renderStatus(state);
      if (state.message) toast(state.message, "ok");
    } catch (err) {
      toastError(err);
    }
  });

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
      threshold_dir: el.thresholdDir.value,
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

  // 動作(拠点・起動時の自動取り込み・図面URL)。**押しても何も起きなかった**
  // (押したときの処理がどこにも無かった)。置き場所と同じく、この面の3つだけを送る
  el.saveBehavior.addEventListener("click", async () => {
    const body = {
      auto_import: el.autoImport.checked,
      spec_sheet_url: el.specUrl.value,
    };
    // 拠点は選んだときだけ。「未登録」のまま送ると断られ、ほかの2つも保存されない
    if (el.position.value) body.position = el.position.value;
    try {
      renderStatus(await api.post("/api/settings/save", body));
      toast("動作の設定を保存しました", "ok");
    } catch (err) {
      toastError(err);
    }
  });

  startAdminPassword();
  startDistribution();
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
   配布設定(`packaging_tool/distribution.py`)

   選んだ項目とパスワードを送り、返ってきた状態を写すだけ。
   **パスワードの値は画面に残さない。**
   ================================================================ */
function renderDistribution(dist) {
  if (!dist || !el.distRows) return;
  el.distState.textContent = dist.exists ? "あり" : "なし";
  el.distState.className = `st st--${dist.exists ? "ok" : "warn"}`;
  el.distMeta.textContent = dist.exists
    ? `${dist.created_at} に ${dist.created_on} で作成`
    : "まだありません。下で書き出すと、ツールのフォルダの直下に「配布設定」フォルダができます。";
  el.distRows.replaceChildren(...dist.contents.map((c) => {
    const tr = document.createElement("tr");
    for (const text of [c.label, c.value]) {
      const td = document.createElement("td");
      td.className = "t";
      td.textContent = text;
      tr.appendChild(td);
    }
    return tr;
  }));
  el.distPath.textContent = dist.path;
}

function startDistribution() {
  if (!el.distExport) return;
  const send = async (path, body) => {
    el.distWhy.hidden = true;
    try {
      const state = await api.post(path, { ...body, password: el.distPassword.value });
      el.distPassword.value = "";
      renderStatus(state);
      toast(state.message || "済みました", "ok");
    } catch (err) {
      el.distWhy.hidden = false;
      el.distWhy.textContent = err.message;
      // 下の小さい字だけだと見落とす(「押したのにフォルダが無い」の元)ので、通知にも出す。
      toastError(err);
    }
  };
  const checked = (attr) => [...document.querySelectorAll(`[${attr}]`)]
    .filter((box) => box.checked)
    .map((box) => box.getAttribute(attr));
  el.distExport.addEventListener("click", () => send(
    "/api/settings/distribution/export",
    { items: checked("data-dist-item"), maps: checked("data-dist-map") }));
  el.distReapply.addEventListener("click", () =>
    send("/api/settings/distribution/reapply", {}));
  el.distRemove.addEventListener("click", () =>
    send("/api/settings/distribution/remove", {}));
}


/* ================================================================
   マスタ編集の認証

   以前は資材選択画面にしか入力欄が無かった。
   `/api/settings/admin-auth` はプロセスに1つの状態(`selection_session`)を
   触るだけなので、どの画面から認証しても同じ ── 応答は資材選択の
   状態なので、ここでは `admin.authenticated` だけを見て、設定側の
   表示は取り直す(2つの画面で同じ事実を別々に持たない)。
   ================================================================ */
function startMasterAuth() {
  if (!el.masterAuthBtn) return;

  const authenticate = async () => {
    el.masterAuthWhy.hidden = true;
    try {
      // 設定の口を使う。資材選択の口は、現場モードの権限が無い端末
      // (倉庫だけの端末)には無い
      const res = await api.post("/api/settings/admin-auth",
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
      if (err.code === "denied") {
        el.masterAuthWhy.hidden = false;
        el.masterAuthWhy.textContent = "パスワードが違います。";
      } else {
        toastError(err);
      }
      refreshStatus();
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
// ファイルを選ぶ参照か(「表を持ってくる」)。置き場所の参照はフォルダを選ぶ
let browseFile = false;
// 選んだあと押してもらうボタンの名前(面ごとに違う)
let browseSave = "この設定を保存";

async function browseTo(path) {
  try {
    // ファイルを選ぶ参照(表を持ってくる)では Access のファイルも出す
    const view = await api.get(`/api/fs/list?path=${encodeURIComponent(path || "")}`
                               + (browseFile ? "&access=1" : ""));
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
      // 共有フォルダをネットワーク越しに辿るので、押してから一覧が
      // 返るまで待つことがある。`busy.js` がこの目印を見て待機の
      // 姿にする
      tr.dataset.rowAction = "1";
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
      browseSave = button.dataset.browseSave || "この設定を保存";
      browseFile = button.dataset.browseFile === "1";
      // ファイルを選ぶときはファイルの行を押して決める。「このフォルダにする」は出さない
      browser.pick.hidden = browseFile;
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
    if (file && browseFile && browseTarget) {
      browseTarget.value = file.dataset.file;
      browser.dialog.close();
      browseTarget.dispatchEvent(new Event("change"));
      return;
    }
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
    toast(`フォルダを入れました。「${browseSave}」を押すと効きます。`, "info");
  });
}


/* ================================================================
   Access で作った表を持ってくる(`table_bring`)

   変換したファイルの中を見て、梱包資材マスタに**無い表だけ**を選ばせる。
   もうある表は選べない(触らない)ことを、行ごとに言う。
   ================================================================ */
const bring = {};

function renderBring(plan) {
  // どのファイルに書くのか(ファイルの名前まで)。見ているファイルと違わないか確かめられる
  if (bring.dest) {
    bring.dest.hidden = !plan.dest_note;
    bring.dest.textContent = plan.dest_note || "";
  }
  bring.converted.hidden = !plan.converted;
  bring.converted.textContent = plan.converted || "";
  bring.note.hidden = !plan.message;
  bring.note.textContent = plan.message || "";
  bring.note.className = `status status--${!plan.ok ? "ng" : plan.warn ? "warn" : "ok"}`;
  const rows = (plan.tables || []).map((t) => {
    const tr = document.createElement("tr");
    const pick = document.createElement("td");
    if (t.exists && t.can_refresh) {
      // もうある表。**Access の最新に入れ替える(列が変わっていれば作り直す)**
      // ことだけ選べる。消して入れ直す操作なので、既定では選ばない
      const rebuild = t.action === "rebuild";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = t.name;
      box.dataset.refresh = "1";
      box.dataset.action = t.action;
      box.dataset.loses = (t.import_loses || []).join("・");
      box.checked = false;
      box.addEventListener("change", updateBringRun);
      const label = document.createElement("label");
      label.className = "why";
      label.append(box, rebuild ? " 作り直す" : " 入れ替える");
      pick.appendChild(label);
    } else if (t.exists) {
      pick.textContent = "もうある";
      pick.className = "why";
      tr.title = t.refresh_why || "梱包資材マスタにもう同じ名前の表があります";
    } else {
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = t.name;
      // 文字化けの疑いがある表は、見比べてから自分で選んでもらう
      box.checked = !t.suspect;
      box.addEventListener("change", updateBringRun);
      pick.appendChild(box);
    }
    const name = document.createElement("td");
    name.textContent = t.name;
    const count = document.createElement("td");
    count.className = "num";
    count.textContent = t.rows.toLocaleString();
    if (t.suspect) {
      // 文字化けの疑い。記号と言葉で出す(色だけに頼らない)
      const warn = document.createElement("div");
      warn.className = "why";
      warn.textContent = `⚠ 化けの疑い ${t.suspect}行`;
      count.appendChild(warn);
    }
    const cols = document.createElement("td");
    cols.className = "why";
    cols.textContent = t.columns.join(", ");
    if (t.exists) {
      // 入れ替えたら何行が何行になるか・残すもの・列の違い・できない理由
      const note = document.createElement("div");
      const added = t.added_columns || [];
      const removed = t.removed_columns || [];
      const loses = t.import_loses || [];
      note.textContent = t.can_refresh
        ? `いま ${t.current_rows.toLocaleString()}行 → Access ${t.rows.toLocaleString()}行`
          + (t.keeps ? `。${t.keeps}` : "")
          + (added.length ? `。Access で増えた列を足します: ${added.join(", ")}` : "")
          + (removed.length
            ? `。Access に無い列があるので作り直します(消える列: ${removed.join(", ")})。`
              + "今の表は「表名_作り直す前_日時」の名前で残します" : "")
          + (loses.length
            ? `。⚠ このツールが取り込みで読む列が消えます: ${loses.join(", ")}` : "")
        : `入れ替えられません: ${t.refresh_why}`;
      cols.prepend(note);
    }
    tr.append(pick, name, count, cols);
    // 持ってこられる表を上に、入れ替えられる表を次に、どちらもできない表を下に
    tr.dataset.order = !t.exists ? "0" : t.can_refresh ? "1" : "2";
    return tr;
  }).sort((a, b) => Number(a.dataset.order) - Number(b.dataset.order));
  bring.rows.replaceChildren(...rows);
  bring.list.hidden = rows.length === 0;
  updateBringRun();
}

function chosenTables() {
  return [...bring.rows.querySelectorAll("input[type=checkbox]:checked:not([data-refresh])")]
    .map((box) => box.value);
}

function chosenRefresh() {
  return [...bring.rows.querySelectorAll("input[type=checkbox][data-refresh]:checked")]
    .map((box) => box.value);
}

function updateBringRun() {
  bring.run.disabled = chosenTables().length === 0;
  if (bring.refresh) bring.refresh.disabled = chosenRefresh().length === 0;
}

async function runRefresh() {
  const tables = chosenRefresh();
  if (!tables.length) return;
  const boxes = [...bring.rows.querySelectorAll("input[type=checkbox][data-refresh]:checked")];
  const swap = boxes.filter((b) => b.dataset.action !== "rebuild").map((b) => b.value);
  const rebuild = boxes.filter((b) => b.dataset.action === "rebuild").map((b) => b.value);
  const loses = boxes.filter((b) => b.dataset.loses)
    .map((b) => `${b.value}(${b.dataset.loses})`);
  // **消して入れ直す。** 控えは取るが、押す前に何が起きるかを言う
  if (!window.confirm(
    (swap.length ? `中身を Access の中身に入れ替える表:\n${swap.join("、")}\n\n` : "")
    + (rebuild.length ? `Access の定義で作り直す表(今の表は名前を変えて残します):\n`
                        + `${rebuild.join("、")}\n\n` : "")
    + (loses.length ? `⚠ このツールが取り込みで読む列が消えます: ${loses.join("、")}\n\n` : "")
    + "梱包資材マスタにある今の行は消えます(書く前に控えを取ります)。"
    + "ほかの端末にも次の取り込みで届きます。よろしいですか？")) return;
  try {
    const result = await api.post("/api/settings/table-refresh",
                                  { path: bring.path.value.trim(), tables });
    renderBring(result.plan);
    toast(result.message, "ok");
    if (result.refreshed && result.refreshed.length) master.show(result.refreshed[0].name);
    bring.note.hidden = false;
    bring.note.className = "status status--ok";
    bring.note.textContent = `${result.message}(控え: ${result.backup})`;
  } catch (err) {
    if (err.body && err.body.plan) renderBring(err.body.plan);
    toastError(err);
  }
}

async function lookBring() {
  const path = bring.path.value.trim();
  if (!path) { renderBring({ ok: false, message: "変換したファイルを選んでください。" }); return; }
  try {
    renderBring(await api.get(`/api/settings/table-bring/plan?path=${encodeURIComponent(path)}`));
  } catch (err) {
    toastError(err);
  }
}

async function runBring() {
  const tables = chosenTables();
  if (!tables.length) return;
  try {
    const result = await api.post("/api/settings/table-bring",
                                  { path: bring.path.value.trim(), tables });
    renderBring(result.plan);
    toast(result.message, "ok");
    // **足した表を、そのままマスタ管理で開いておく**(開き直さなくても出る)
    if (result.brought && result.brought.length) master.show(result.brought[0].name);
    bring.note.hidden = false;
    bring.note.className = "status status--ok";
    bring.note.textContent = `${result.message}(控え: ${result.backup})`;
  } catch (err) {
    if (err.body && err.body.plan) renderBring(err.body.plan);
    toastError(err);
  }
}

function startBring() {
  for (const [key, id] of [["dialog", "bringDialog"], ["open", "bringOpen"],
                           ["path", "bringPath"], ["look", "bringLook"],
                           ["note", "bringNote"], ["list", "bringList"],
                           ["rows", "bringRows"], ["run", "bringRun"],
                           ["converted", "bringConverted"], ["drop", "bringDrop"],
                           ["dest", "bringDest"],
                           ["refresh", "bringRefresh"],
                           ["openDrop", "bringOpenDrop"]]) {
    bring[key] = document.getElementById(id);
  }
  if (!bring.dialog) return;
  bring.open.addEventListener("click", () => bring.dialog.showModal());
  // **落とすだけで読む。** ダイアログの中の枠にも、設定画面の段にも落とせる
  // (段に落としたらダイアログを開いてから読む)
  acceptDrop(bring.drop);
  acceptDrop(bring.openDrop, () => {
    if (!bring.dialog.open) bring.dialog.showModal();
  });
  bring.look.addEventListener("click", lookBring);
  bring.path.addEventListener("change", lookBring);
  bring.path.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); lookBring(); }
  });
  bring.run.addEventListener("click", runBring);
  if (bring.refresh) bring.refresh.addEventListener("click", runRefresh);
}

/** ファイルを落とせる場所にする。落ちたら受け取って、すぐ中を読む。 */
function acceptDrop(zone, before) {
  if (!zone) return;
  const over = (on) => zone.classList.toggle("is-over", on);
  zone.addEventListener("dragover", (event) => {
    if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
    event.preventDefault();
    over(true);
  });
  zone.addEventListener("dragleave", () => over(false));
  zone.addEventListener("drop", async (event) => {
    event.preventDefault();
    over(false);
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    if (before) before();
    await uploadBring(file);
  });
}

async function uploadBring(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  bring.note.hidden = false;
  bring.note.className = "status";
  bring.note.textContent = `${file.name} を受け取っています…`;
  try {
    const got = await api.postForm("/api/settings/table-bring/upload", form);
    bring.path.value = got.path;
    await lookBring();
  } catch (err) {
    bring.note.className = "status status--ng";
    bring.note.textContent = err.message;
    toastError(err);
  }
}
