/*
  ロット検索の画面。

  流れは 探す → 絞る → 見つける → 開く → 次へ の一方向。
  「開く」は前面のモーダルで、閉じれば元の一覧に戻る。

  判断はサーバが済ませてある。ここは受け取ったビューモデルを並べるだけ。
  「BOX実績に差し替わっているから赤くする」といった判断は
  `presenters/lot.py` が `highlight` として返す。
*/

import { api, tokenUrl } from "../api.js";
import * as nav from "../nav.js";
import { toast, toastError } from "../toast.js";
// `lotlist.js` は動的に読み込む。理由は `settings.js` が `master.js` を
// 動的に読み込んでいるのと同じ(このファイル自身と版クエリを合わせ、
// 入れ替えたときに中身も必ず一緒に入れ替わるようにするため)
const VERSION_QUERY = new URL(import.meta.url).search;
const lotlist = await import(`./lotlist.js${VERSION_QUERY}`);

// 包装仕様書の図面を取り終わるまでの見に行く間隔(ms)と、あきらめるまでの回数。
// サーバ側の取得は最長15秒で切れるので、そこを少し越えるまで見る
const SPEC_POLL_MS = 700;
const SPEC_POLL_MAX = 24;

let current = null;      // いま開いているロットの詳細(資材展開の可否に使う)
let specNo = "";         // いま出している包装仕様NO
let specRun = 0;         // 見に行っている回。ロットを変えたら古いものは捨てる

const el = {};

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

/** 包装仕様書NOをクリップボードへコピーする。外部の閲覧システムは
 * NOを渡さず固定URLで開くだけなので、開いた先で貼れるようにする。 */
async function copySpecNo() {
  if (!specNo) return;
  try {
    await navigator.clipboard.writeText(specNo);
    toast(`包装仕様書NO「${specNo}」をコピーしました`, "ok");
  } catch {
    toast("コピーできませんでした。NOを選択してコピーしてください", "error");
  }
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
  const why = view.found
    ? (view.can_expand
        ? "製造板幅・板丈を製品サイズ欄に入れて資材選択へ移ります。"
        : view.expand_reason)
    : "先にロットを選んでください。";
  for (const button of [el.expand, el.modalExpand]) button.disabled = !canExpand;
  el.expandWhy.textContent = why;
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
    { group: view.dimension_group, text: view.dimension_note }));
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

/** ステータスリボンを書き換える。値はサーバが決める。 */
function applyRibbon(ribbon) {
  if (!ribbon) return;
  document.getElementById("rb-lot").textContent = ribbon.lot;
  document.getElementById("rb-product").textContent = ribbon.product;
  document.getElementById("rb-pallet").textContent = ribbon.pallet;
  document.getElementById("rb-modes").replaceChildren(
    ...ribbon.modes.map((chip) => {
      const span = document.createElement("span");
      span.className = `chip chip--${chip.kind}`;
      span.textContent = chip.text;
      return span;
    }));
}

/** 行を開く(ダブルクリック / Enter)。開くことが「選ぶ」ことでもある。 */
async function openLot(lotNo) {
  try {
    const body = await api.get(`/api/lot/${encodeURIComponent(lotNo)}`);
    renderDetail(body);
    applyRibbon(body.ribbon);
    // どの行が作業中かはサーバが決める(応答に一覧ごと入っている)
    lotlist.render(body.list);
    if (body.found) openModal();
  } catch (err) {
    toastError(err);
  }
}

/*
  絞り込みで1件になったとき。サーバが確定まで済ませて `detail` を
  付けて返すので、ここは開くだけ。**番号を打つ専用の欄の代わり**で、
  7桁を打てば必ず1件になる。
*/
function onListChanged(view) {
  if (!view || !view.detail) return;
  renderDetail(view.detail);
  applyRibbon(view.ribbon);
  if (view.detail.found) openModal();
}

export function start(options) {
  // ダイアログだけ id と持ち名が違う(画面の中では「ロットの詳細」、
  // このモジュールの中では単に「モーダル」)
  el.modal = document.getElementById("lotModal");
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
  // (「NOをクリックしてもコピーされない」という現場の声への対応)。
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

  const expand = async () => {
    if (!current) return;
    try {
      const body = await api.post("/api/lot/expand", {});
      applyRibbon(body.ribbon);
      toast(body.message, "ok");
      // 渡した先へそのまま移る(VBA も `mp.value = 1` でページを切り替えていた)。
      // 差し替えで移るので、いま出したトーストは消えない
      nav.go(body.next);
    } catch (err) {
      toastError(err);
    }
  };
  for (const button of [el.expand, el.modalExpand]) {
    button.addEventListener("click", expand);
  }

  current = null;       // 再入場のたびに真っさらから(`nav.js`)
  specNo = "";

  lotlist.start({ view: options.list, onOpen: openLot,
                  onChanged: onListChanged });

  // 画面を出たら図面の見張りを打ち切る。`specRun` を進めるだけでよい ──
  // 走っている `specPoll` は自分の回が古いことに気づいて静かに終わる
  nav.onLeave(() => { specRun += 1; });
}
