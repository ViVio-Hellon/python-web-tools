/*
  ロットの詳細(モーダル)。**2つの画面で同じものを出す。**

    ロット検索  … 一覧の行を開く。資材展開へ進める
    発注一覧    … 「Lotを開く」。**確かめるだけ**で、資材展開は出さない

  以前はこの中身がロット検索の画面にしか無く、発注一覧からは
  `/lot?lot=…` へ**画面ごと移って**いた。発注を見ていて「どんなロット
  だっけ?」を確かめたいだけなのに、ロット検索まで連れていかれて、
  戻るにはもう一度たどり直す(現場の指摘:「ロット検索タブに戻されると
  非常に手間」)。出すものは同じなので、置き場所をここ1つにして、
  どちらの画面からも**その場で**開けるようにした。

  持ち物(`el`)と「いま開いているロット」はこのモジュールが持つ。
  画面側は `mount()` して `show(view)` を呼ぶだけ。
*/
import { api, tokenUrl } from "./api.js";
import { toast, toastError } from "./toast.js";

// 包装仕様書の図面を取り終わるまでの見に行く間隔(ms)と、あきらめるまでの回数。
// サーバ側の取得は最長15秒で切れるので、そこを少し越えるまで見る
const SPEC_POLL_MS = 700;
const SPEC_POLL_MAX = 24;

let current = null;      // いま開いているロットの詳細(資材展開の可否に使う)
let peekMode = false;    // 見るだけで開いた(発注一覧の「Lotを開く」)。作業中のロットにしない
let onReload = null;     // 選び直して引き直したとき、開いた画面に知らせる
let specNo = "";         // いま出している包装仕様NO
let specRun = 0;         // 見に行っている回。ロットを変えたら古いものは捨てる
// 資材展開のボタンが**無い**画面で、そのとき説明文に出す「なぜ無いのか」
// (サーバから貰う。無いボタンの押せない理由ではない)
let absentWhy = "";

const el = {};

/** いま開いているロット。見つからなかったときは null。 */
export function currentLot() {
  return current;
}

function field(item) {
  const row = document.createElement("div");
  if (item.wide) row.classList.add("wide");

  const dt = document.createElement("dt");
  dt.textContent = item.label;

  const dd = document.createElement("dd");
  if (item.highlight) dd.classList.add("hl");
  if (item.url && item.value !== "---") {
    const link = document.createElement("a");
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = item.value;
    // 包装仕様NOの閲覧システムは固定URLで開くだけで、NOそのものは
    // 渡らない(先方で検索し直す)。**開く前にコピーしておく。**
    //
    // 開ける場所は2か所ある ── 図面カードの「閲覧システム」と、ここ
    // (受注情報の欄)。コピーしていたのは前者だけで、こちらは開く
    // だけだった(現場の指摘:「2か所あるが片方しかコピーできていない」)。
    // **同じ見た目の操作は同じことをしなければならない**ので、
    // どちらから開いても貼れる状態にする
    if (item.key === "packaging_spec") {
      link.title = "押すと包装仕様NOをコピーして閲覧システムを開きます";
      link.addEventListener("click", () => copyText(
        item.value, `包装仕様書NO「${item.value}」をコピーしました`));
    }
    dd.appendChild(link);
  } else {
    dd.textContent = item.value;
  }

  row.append(dt, dd);
  return row;
}

/*
  まとまりごとに区切って並べる。項目の順序はサーバが決めた並びのまま。
  ここでやるのは「連続する同じ group を1組にする」ことだけ。
*/
function groups(items, note) {
  const out = [];
  for (const item of items) {
    const last = out[out.length - 1];
    if (last && last.name === item.group) last.items.push(item);
    else out.push({ name: item.group, items: [item] });
  }
  return out.map((group) => {
    const box = document.createElement("section");
    box.className = "group";
    // 見出しを赤くするのは**まとまり全体**が差し替わっているときだけ。
    // 1項目だけ赤い設備コースまで赤くすると、コース名まで
    // 差し替わったように読める
    if (group.items.every((item) => item.highlight)) box.classList.add("group--hl");
    if (group.name) {
      const name = document.createElement("p");
      name.className = "group__name";
      name.textContent = group.name;
      // BOX最終実績寸法の候補があれば、見出しの横で選ばせる(`boxPicker`)
      if (note && note.group === group.name && note.choices && note.choices.length) {
        name.classList.add("group__name--pick");
        name.appendChild(boxPicker(note.choices));
      }
      box.appendChild(name);
    }
    // 差し替えの説明は、説明している欄のすぐ上に置く(近接)
    if (note && note.group === group.name && note.text) {
      const why = document.createElement("p");
      why.className = "why";
      why.style.margin = "2px 0 4px";
      why.textContent = note.text;
      box.appendChild(why);
    }
    const list = document.createElement("dl");
    list.className = "fields";
    list.append(...group.items.map(field));
    box.appendChild(list);
    return box;
  });
}

/*
  BOX最終実績寸法の候補を選ぶ欄。1つ目の仕掛台帳に BOX最終実績寸法が無いロット
  (BOX実績寸法のときだけ)に、2つ目の仕掛台帳の同じロットの行を BOX設計_設備名 で並べる。
  **どれを使うかはサーバが覚える**(資材展開も同じ寸法を使うため)。選んだら引き直す。
*/
function boxPicker(choices) {
  const select = document.createElement("select");
  select.className = "input box-pick";
  select.id = "boxPick";
  select.setAttribute("aria-label", "2つ目の仕掛台帳の BOX設計_設備名");
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "選んでください";
  select.appendChild(none);
  for (const choice of choices) {
    const option = document.createElement("option");
    option.value = choice.key;
    option.textContent = choice.label;
    option.selected = Boolean(choice.selected);
    select.appendChild(option);
  }
  select.addEventListener("change", () => pickBox(select.value));
  return select;
}

async function pickBox(key) {
  if (!current) return;
  const base = `/api/lot/${encodeURIComponent(current.lot_no)}${peekMode ? "/peek" : ""}`;
  try {
    const body = await api.get(`${base}?box_pick=${encodeURIComponent(key)}`);
    renderDetail(body);
    // 帯(製品サイズの元になる寸法)と一覧は、開いた画面が直す
    if (onReload) onReload(body);
  } catch (err) {
    toastError(err);
  }
}

function badge(item) {
  const span = document.createElement("span");
  span.className = `chip chip--${item.kind}`;
  span.textContent = item.text;
  return span;
}

function hikiRow(row) {
  const tr = document.createElement("tr");
  // **引当行は押せる。** 押すと、その行(引当NOで特定)が指す
  // 受注番号の受注情報に差し替わる(VBA `Page1_OnLstHikiClick`)。
  // 以前はこの経路が無く、受注情報欄は常に先頭1件のままだった
  // (現場の声)。行の特定は**引当NO**で行う(受注番号ではない) ──
  // SIKAHIKI は同じLOTNOの行の中から引当NOで1行を決め、そこから
  // オーダーNOが決まる、という順で辿るデータの流れに合わせている
  tr.dataset.hikiNo = row.hiki_no;
  // `busy.js` がこの目印を見て、押した行を待機の姿にする
  // (`<tr>` は `disabled` を持てないボタンとは別の作法が要る)
  tr.dataset.rowAction = "1";
  tr.tabIndex = 0;
  tr.title = "クリックすると、この行の受注情報を表示します";
  if (row.selected) tr.setAttribute("aria-selected", "true");
  // 引当数量は全量指定だと 0 が入っている。そのまま出すと
  // 「引当が無い」と読めるので、サーバが決めた表示値を使う
  for (const [value, numeric] of [[row.order_no, false], [row.quantity_text, true],
                                  [row.adjust_no, false], [row.hiki_no, true]]) {
    const td = document.createElement("td");
    if (numeric) td.className = "n";
    td.textContent = value || "---";
    tr.appendChild(td);
  }
  return tr;
}

/** 引当行をクリック/Enterで押した。その行の受注情報に差し替える。 */
async function onHikiClick(hikiNo) {
  if (!current || !hikiNo) return;
  try {
    const body = await api.get(
      `/api/lot/${encodeURIComponent(current.lot_no)}/hiki/${encodeURIComponent(hikiNo)}`);
    applyOdrSwitch(body, hikiNo);
  } catch (err) {
    toastError(err);
  }
}

/** 受注情報の断片だけを塗り直す。ロット情報・引当一覧の中身・図面には触らない。 */
function applyOdrSwitch(body, hikiNo) {
  if (!current) return;
  // 手元の`current`も更新する。**このあと別の行を押したときに
  // 前回の状態と混ざらないようにするため**
  current.odr_badges = body.odr_badges;
  current.odr_fields = body.odr_fields;
  current.packaging_spec = body.packaging_spec;
  current.specific_gravity = body.specific_gravity;
  current.is_ex = body.is_ex;
  current.hiki = current.hiki.map((row) => ({
    ...row,
    selected: row.hiki_no === hikiNo,
  }));

  el.odrBadges.replaceChildren(...body.odr_badges.map(badge));
  el.odrFields.replaceChildren(...groups(body.odr_fields));
  el.gravity.textContent = body.specific_gravity;
  for (const tr of el.hikiRows.querySelectorAll("tr")) {
    if (tr.dataset.hikiNo === hikiNo) tr.setAttribute("aria-selected", "true");
    else tr.removeAttribute("aria-selected");
  }
  // 図面は受注番号ではなく包装仕様NOに紐づく。切り替わったなら見に行き直す
  specStart(body.packaging_spec || "");
}

/*
  試験指示票の判定根拠。「要」とだけ出しても、現場は正しいかどうかを
  確かめられない。成立した条件と、そのときの実際の値を並べる。
*/
function checkItem(check) {
  const li = document.createElement("li");
  li.className = check.met ? "met" : "unmet";

  const mark = document.createElement("span");
  mark.className = "mark";
  // 色だけで伝えない。記号でも読める(§3.8)
  mark.textContent = check.met ? "✔" : "—";
  mark.setAttribute("aria-label", check.met ? "該当" : "非該当");

  const label = document.createElement("span");
  label.textContent = check.label;

  const value = document.createElement("span");
  value.className = "val";
  value.textContent = check.value;

  li.append(mark, label, value);
  return li;
}

function showSlipWhy(open) {
  el.slipWhy.hidden = !open;
  el.slip.setAttribute("aria-expanded", String(open));
}

/* ================================================================
   包装仕様書の図面

   ここは「取りに行く」判断をしない。サーバが `state` を返すので、
   それに合わせて出すものを変えるだけ。取得そのものはロットを開いた
   時点で裏で始まっている(`/api/lot/<no>` が起こす)。
   ================================================================ */
function specMessage(text) {
  const p = document.createElement("p");
  p.className = "why";
  p.textContent = text;
  el.specView.replaceChildren(p);
}

function specImage(status) {
  // PDFで返る運用もありうる。`<img>` では出ないので枠で出す
  const node = document.createElement(
    status.content_type === "application/pdf" ? "iframe" : "img");
  // `<img>`/`<iframe>` はヘッダを付けられない。トークンはURLに載せる
  node.src = tokenUrl(status.image_url);
  if (node.tagName === "IMG") {
    node.alt = `包装仕様書 ${status.no} の図面`;
  } else {
    node.title = `包装仕様書 ${status.no}`;
  }
  el.specView.replaceChildren(node);
}

function specApply(status) {
  el.specReload.disabled = status.state === "loading";
  // 出すものが無いときに大きさを変えられても、何も起きない
  specZoomEnabled(status.state === "ready");
  el.specSource.textContent = status.source_url
    ? `取得元: ${status.source_url}` : "";

  if (status.state === "ready") { specImage(status); return; }
  specMessage(status.message || "図面を取得しています…");
  // 未設定は利用者が直せる。直せる場所への行き先を添える
  if (status.state === "unset") {
    const link = document.createElement("a");
    link.className = "btn btn--nav";
    link.href = "/settings";
    link.append("設定画面で開く");
    const glyph = document.createElement("span");
    glyph.className = "gl";
    glyph.textContent = "▸";
    link.appendChild(glyph);
    el.specView.appendChild(link);
  }
}

/** 取り終わるまで見に行く。`run` が変わっていたら別のロットに移っている。 */
async function specPoll(no, run, left) {
  if (run !== specRun) return;
  let status;
  try {
    status = await api.get(`/api/spec-sheet/${encodeURIComponent(no)}`);
  } catch (err) {
    if (run === specRun) specMessage(err.message || "図面を取得できませんでした");
    return;
  }
  if (run !== specRun) return;

  if (status.state !== "loading") { specApply(status); return; }
  if (left <= 0) {
    specMessage("図面の取得に時間がかかっています。「再取得」を押してください");
    el.specReload.disabled = false;
    return;
  }
  specApply(status);
  setTimeout(() => specPoll(no, run, left - 1), SPEC_POLL_MS);
}

function specStart(no) {
  // 同じ仕様NOの別ロットは続けて流れる。出ているものをいったん
  // 「取得しています」に戻すと、出ていた図が消えて瞬く
  if (no && no === specNo && el.specView.querySelector("img, iframe")) return;

  specRun += 1;
  specNo = no || "";
  if (!specNo) { el.specCard.hidden = true; return; }

  el.specCard.hidden = false;
  el.specNo.textContent = specNo;
  specMessage("図面を取得しています…");
  el.specReload.disabled = true;
  specZoomEnabled(false);
  specPoll(specNo, specRun, SPEC_POLL_MAX);
}

function specZoomEnabled(on) {
  for (const button of [el.specFit, el.specFull]) button.disabled = !on;
}

function specZoom(full) {
  el.specView.classList.toggle("specview--full", full);
  el.specView.classList.toggle("specview--fit", !full);
  for (const [button, on] of [[el.specFull, full], [el.specFit, !full]]) {
    button.classList.toggle("btn--on", on);
    button.setAttribute("aria-pressed", String(on));
  }
}

/** 文字をクリップボードへ。**コピーできたかどうかを必ず言う** ──
 * 黙って失敗すると、貼り付け先で「何も入らない」としか分からない。 */
async function copyText(text, okMessage) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast(okMessage, "ok");
  } catch {
    toast("コピーできませんでした。文字を選択してコピーしてください", "error");
  }
}

/** 包装仕様書NOをクリップボードへコピーする。外部の閲覧システムは
 * NOを渡さず固定URLで開くだけなので、開いた先で貼れるようにする。 */
async function copySpecNo() {
  await copyText(specNo, `包装仕様書NO「${specNo}」をコピーしました`);
}

/* ================================================================
   詳細(モーダル)
   ================================================================ */
function renderDetail(view) {
  current = view && view.found ? view : null;

  el.slip.textContent = view.test_slip || "---";
  el.slip.dataset.state = view.found
    ? (view.test_slip_needed ? "needed" : "not") : "";
  el.slip.disabled = !view.found;
  showSlipWhy(false);
  el.slipNote.textContent = view.test_slip_note || "";
  el.slipChecks.replaceChildren(
    ...(view.test_slip_checks || []).map(checkItem));

  // 次の一手は2か所にある。モーダルの中(読み終わった先)と、画面の
  // 見出し(閉じたあとも押せるように)。**押せるかどうかと理由は
  // どちらもサーバの同じ値**を写す
  const canExpand = Boolean(view.found && view.can_expand);
  // ボタンが無いモード(資材)では、押せる/押せないの話をしても意味が
  // 通らない。**無い理由**だけを言う
  const why = el.modalExpand === null && absentWhy
    ? absentWhy
    : view.found
      ? (view.can_expand
          ? "製造板幅・板丈を製品サイズ欄に入れて資材選択へ移ります。"
          : view.expand_reason)
      : "先にロットを選んでください。";
  // **資材モードには資材展開のボタンが無い。** 役割が違う操作なので
  // 隠すのではなく出していない(`can_expand_here`)。無い前提で触る
  for (const button of [el.expand, el.modalExpand]) {
    if (button) button.disabled = !canExpand;
  }
  if (el.expandWhy) el.expandWhy.textContent = why;
  el.modalWhy.textContent = why;

  if (!view.found) {
    specStart("");
    el.modalLot.textContent = "—";
    el.lotBadges.replaceChildren();
    el.lotFields.replaceChildren();
    el.odrFields.replaceChildren();
    el.hikiRows.replaceChildren();
    el.hikiCount.textContent = "";
    toast(view.message || "そのロットは見つかりません", "ng");
    return;
  }

  el.modalLot.textContent = view.lot_no;
  el.lotHeader.textContent = view.lot_title;
  el.odrHeader.textContent = view.odr_title;
  el.lotBadges.replaceChildren(...view.lot_badges.map(badge));
  el.odrBadges.replaceChildren(...view.odr_badges.map(badge));
  el.lotFields.replaceChildren(...groups(
    view.lot_fields,
    { group: view.dimension_group, text: view.dimension_note,
      choices: view.is_box ? (view.box_choices || []) : [] }));
  el.odrFields.replaceChildren(...groups(view.odr_fields));
  el.hikiRows.replaceChildren(...view.hiki.map(hikiRow));
  el.hikiCount.textContent = `${view.hiki.length} 件`;
  el.gravity.textContent = view.specific_gravity;
  // 図面の取得はロットを開いた時点でもう始まっている
  specStart(view.packaging_spec || "");
}

function openModal() {
  if (!el.modal.open) el.modal.showModal();
}

function closeModal() {
  if (el.modal.open) el.modal.close();
}


/** 画面を出るときに呼ぶ。走っている図面の見張りを打ち切る。 */
export function stop() {
  specRun += 1;
}

/**
 * モーダルを使えるようにする。**画面ごとに1回だけ**呼ぶ。
 *
 *   onExpand      資材展開のボタンを押したときの処理。渡さない画面では
 *                 ボタンそのものが無い(テンプレートが出していない)
 *   expandAbsentWhy ボタンが無い画面で説明文に出す一文
 */
export function mount({ onExpand = null, expandAbsentWhy = "", onReloaded = null } = {}) {
  // ダイアログだけ id と持ち名が違う(画面の中では「ロットの詳細」、
  // このモジュールの中では単に「モーダル」)
  el.modal = document.getElementById("lotModal");
  if (!el.modal) return false;          // その画面にはモーダルが無い

  for (const id of ["expand", "expandWhy",
                    "modalLot", "modalClose", "modalExpand", "modalWhy",
                    "lotHeader", "odrHeader", "lotBadges", "odrBadges",
                    "slip", "slipWhy", "slipNote", "slipChecks",
                    "lotFields", "odrFields", "hikiRows", "hikiCount", "gravity",
                    // specMsg は差し替えで消えるので持たない
                    "specCard", "specNo", "specView", "specSource",
                    "specFit", "specFull", "specReload", "specOpen"]) {
    el[id] = document.getElementById(id);
  }

  current = null;
  specNo = "";
  absentWhy = expandAbsentWhy || "";
  onReload = onReloaded;

  el.slip.addEventListener("click", () => showSlipWhy(el.slipWhy.hidden));
  el.modalClose.addEventListener("click", closeModal);
  // 枠の外を押しても閉じる。読むだけのモーダルなので、閉じても何も失わない
  el.modal.addEventListener("click", (event) => {
    if (event.target === el.modal) closeModal();
  });

  specZoom(false);
  el.specFit.addEventListener("click", () => specZoom(false));
  el.specFull.addEventListener("click", () => specZoom(true));

  // 「閲覧システム」はこのツールとは別の外部システムを固定URLで開くだけ
  // で、NOそのものは渡していない(先方で検索し直す必要がある)。開く前に
  // NOをクリップボードへコピーしておけば、開いた先にそのまま貼れる。
  // NOの表示そのものをクリックしても同じことができるようにする
  el.specOpen.addEventListener("click", () => copySpecNo());
  el.specNo.addEventListener("click", () => copySpecNo());
  el.specNo.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      copySpecNo();
    }
  });
  // 引当行を押すと、その行の受注情報に差し替わる(`hikiRow`/`onHikiClick`)
  el.hikiRows.addEventListener("click", (event) => {
    const tr = event.target.closest("tr[data-hiki-no]");
    if (tr) onHikiClick(tr.dataset.hikiNo);
  });
  el.hikiRows.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const tr = event.target.closest("tr[data-hiki-no]");
    if (!tr) return;
    event.preventDefault();
    onHikiClick(tr.dataset.hikiNo);
  });
  el.specReload.addEventListener("click", async () => {
    if (!specNo) return;
    const no = specNo, run = ++specRun;
    specMessage("図面を取得しています…");
    el.specReload.disabled = true;
    specZoomEnabled(false);
    try {
      const status = await api.post(
        `/api/spec-sheet/${encodeURIComponent(no)}/refresh`, {});
      if (run !== specRun) return;
      if (status.state === "loading") specPoll(no, run, SPEC_POLL_MAX);
      else specApply(status);
    } catch (err) {
      toastError(err);
      el.specReload.disabled = false;
    }
  });

  if (onExpand) {
    for (const button of [el.expand, el.modalExpand]) {
      if (button) button.addEventListener("click", onExpand);
    }
  }
  return true;
}

/**
 * 詳細を出して開く。見つからなかったときは開かない。
 * `peek` は見るだけで開いたとき(BOX最終実績寸法を選び直しても作業中のロットにしない)。
 */
export function show(view, { peek = false } = {}) {
  peekMode = Boolean(peek);
  renderDetail(view);
  if (view && view.found) openModal();
}

export { closeModal as close };
