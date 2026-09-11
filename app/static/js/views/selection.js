/*
  資材選択の画面(パレット・製品サイズ・ボード選定・アングル)。

  どの操作もサーバが**画面ぜんぶ**を返すので、ここは受け取ったものを
  そのまま描き直すだけ。押した欄だけを個別に触ると、触り忘れた欄が
  古い値のまま残る(tkinter版で実際に何度か起きた)。

  **業務の判断はここに1つも無い。** 押せるかどうか・どの欄を出すか・
  見出しに何を書くかは、すべてサーバが決めたものを写している
  (設計書 §3.3)。JSで組み立て直すと、サーバが断る条件と画面が
  押させる条件が別々に育つ。
*/

import { api, tokenUrl } from "../api.js";
import * as nav from "../nav.js";
import { pageSignal } from "../nav.js";
import { angleSvg, boardSvg, fitToContent, legendItems } from "../svgplan.js";
import { toast, toastError } from "../toast.js";
import * as tabs from "../tabs.js";

const el = {};
let state = null;

/**
 * いま一覧で選んでいる行(まだサーバへは伝えていない)。
 *
 * **行を押しただけでは何も確定しない。** 以前は行を押した時点で
 * サーバへ「決める」を送っていたため、一覧を見比べているだけで
 * 確定の通知が何度もたまってしまっていた(現場の声)。ここでは
 * 選んだことだけを覚えておき、「パレット決定」を押した瞬間に初めて
 * サーバへ伝える。
 */
let pickedPalletRow = null;   // { width, length, symbol, key }

/**
 * 帳票を出す前に訊くこと(帳票の名前 → `{title, body, choices}`)。
 *
 * **訊くかどうかも、文面も、選択肢もサーバが決める**
 * (`outputs.len_cut_question`)。画面で組み立て直すと、条件を直した日に
 * 文面だけが古いまま残る。
 */
const asks = {};

/**
 * 選択肢を出して、選ばれた `key` を返す(選ばずに閉じたら何もしない)。
 *
 * **`window.open` は選んだその場で呼ぶ。** ここで待ってから開くと
 * 「人が押した流れ」から外れて、ブラウザに別窓を止められる。
 */
function showAsk(ask, onPick) {
  el.askTitle.textContent = ask.title || "";
  why(el.askBody, ask.body || "");
  el.askChoices.replaceChildren(...(ask.choices || []).map((choice, index) => {
    const button = document.createElement("button");
    button.type = "button";
    // 先頭が既定の答え。**やめるだけは見た目を分ける**
    button.className = "btn " + (choice.key === "cancel" ? "btn--danger"
                                 : index === 0 ? "btn--run" : "");
    button.textContent = choice.label;
    if (choice.note) {
      const note = document.createElement("span");
      note.className = "note";
      note.textContent = choice.note;
      button.appendChild(note);
    }
    button.addEventListener("click", () => {
      el.askDialog.close();
      onPick(choice.key);
    });
    return button;
  }));
  el.askDialog.showModal();
}

function openReport(url) {
  const win = window.open(url, "_blank");
  if (!win) toast("別の窓を開けませんでした。ポップアップの許可を確認してください。", "warn");
}

/**
 * 押しっぱなしのモードのON/OFF。
 *
 * `variant` を渡すとONの色を変えられる。疲労度優先だけ別の色にするのは、
 * それが**操作の種類ではなく、選定の考え方そのもの**だから
 * (§設計指針の色相の意味)。在庫考慮と同じ緑にすると、2つ並んだときに
 * どちらが効いているのか色から読めなくなる。
 */
function setToggle(button, on, variant = "btn--on") {
  button.classList.toggle(variant, on);
  button.setAttribute("aria-pressed", String(on));
}

function setStatus(node, text, ok) {
  node.textContent = text;
  node.className = `status status--${ok ? "ok" : "ng"}`;
}

function why(node, text) {
  node.hidden = !text;
  node.textContent = text || "";
}

/**
 * 入力欄を更新する。**サーバ側の値が変わったときだけ**書き換える。
 *
 * 打った値かどうかの判断は画面が持つしかない ── 「まだ確定していない
 * 打鍵」はブラウザにしか存在せず、サーバは確定値しか知らない。
 * サーバが前回と同じ値を返しているあいだは、利用者が打った内容が勝つ。
 */
/**
 * 「パレット決定」を押せるかどうかを、**いま一覧で選んでいる行があるか**
 * で決める。
 *
 * 以前は製品 幅・丈が入っているかどうかで押せた。それだと「検索して
 * いないのに押せる」「押しても何を決めたのか分からない」という
 * あいまいさがあった(現場の声)。一覧の行を選んで初めて「これに
 * 決める」という意思が画面上にも表れるので、それを条件にする。
 */
function refreshDecide() {
  if (!el.decide) return;
  el.decide.disabled = !pickedPalletRow;
}

/** 一覧の行の見た目を、いま選んでいる行(`pickedPalletRow`)に合わせ直す。
 *
 * 描き直し(`render`)のたびに `<tr>` は全部作り直されるので、
 * ハイライトも毎回付け直す必要がある。
 */
function applySelection() {
  let found = false;
  for (const tr of el.palletRows.querySelectorAll("tr")) {
    const on = !!pickedPalletRow && tr.dataset.key === pickedPalletRow.key;
    tr.classList.toggle("on", on);
    if (on) found = true;
  }
  // 打ち直しで一覧が変わり、選んでいた行が消えたら選択も外す
  if (pickedPalletRow && !found) {
    pickedPalletRow = null;
    el.rowNote.textContent = "";
  }
  refreshDecide();
}

function setInput(node, value) {
  const text = value ?? "";
  if (node.dataset.fromServer === text) return false;   // サーバ側は変わっていない
  node.dataset.fromServer = text;
  node.value = text;
  return true;
}

// 製品 幅・丈を打つたびに、**確定させずに**候補を一覧へ出す
// (`list_pallets_by_product_dims`)。連打で毎回サーバへ聞きに行かない
// よう間を置く(デバウンス)。`render()` からも呼ぶ ── ロット検索の
// 「資材展開」のように、幅・丈がサーバから**同時に**入るときは
// `input` イベントが発火せず、打鍵を待つ仕組みだけでは一覧が
// 古いままになっていた(現場の声)
let liveListTimer = 0;
function scheduleLiveList() {
  window.clearTimeout(liveListTimer);
  liveListTimer = window.setTimeout(() => {
    send("/api/selection/pallet/list", {
      product_width: el.prodWidth.value,
      product_length: el.prodLength.value,
    });
  }, 300);
}

function row(item) {
  const tr = document.createElement("tr");
  if (item.is_ex) tr.classList.add("ex");
  // 幅・丈は行を選んだときに入力欄へ写す。どの行かはこれで分かる。
  // 記号まで持つのは、同じ寸法でも記号違いで発注コードが変わるため
  tr.dataset.width = item.width;
  tr.dataset.length = item.length;
  tr.dataset.symbol = item.symbol;
  tr.dataset.note = item.note;
  tr.dataset.key = `${item.width}x${item.length}:${item.symbol}`;
  // マウスを乗せたときだけ、そのパレットの単位・コードを見せる
  // (VBA `DynamicTip` に相当)。押さなくても分かるようにする
  tr.title = item.note;
  // **決めるのは「パレット決定」を押した瞬間。** 押すまではサーバへ
  // 何も伝えない(押しただけで確定通知がたまる、という声への対応)。
  // ここでの `on` はまだ最終確定のサーバ側 `picked` の初期反映で、
  // 選び直した分は `applySelection()` が上書きする
  if (item.picked) tr.classList.add("on");

  item.values.forEach((value, index) => {
    const td = document.createElement("td");
    // 列の並び・右寄せはサーバが決めた `PALLET_COLUMNS` に従う
    if (NUMERIC[index]) td.className = "n";
    td.textContent = value;
    tr.appendChild(td);
  });
  return tr;
}

// 右寄せにする列。サーバが返す並びと1対1で対応する
let NUMERIC = [];

function render(next) {
  state = next;

  el.lotCaption.textContent = next.lot_caption;
  el.banner.hidden = !next.banner.visible;
  el.banner.textContent = next.banner.text;
  el.banner.dataset.kind = next.banner.kind;

  // どの行が最終確定しているかはサーバが `picked` で返す。まだ
  // クリックで選んでいる途中(`pickedPalletRow`)なら、そちらを優先する
  el.palletRows.replaceChildren(...next.rows.map(row));
  if (!pickedPalletRow) {
    const pickedItem = next.rows.find((r) => r.picked);
    if (pickedItem) {
      pickedPalletRow = {
        width: String(pickedItem.width), length: String(pickedItem.length),
        symbol: pickedItem.symbol || "",
        key: `${pickedItem.width}x${pickedItem.length}:${pickedItem.symbol}`,
      };
    }
  }
  applySelection();
  const pickedRow = picked(el.palletRows);
  if (pickedRow) pickedRow.scrollIntoView({ block: "nearest" });
  el.listNote.textContent = next.list_note;

  // 入力欄は**サーバが変えたときだけ**書き換える。
  // どの操作でも画面ぜんぶが返ってくる設計なので、無条件に代入すると
  // 「まだセットしていない打鍵」が関係ないボタン1つで消える
  // (tkinter版は入力欄に触れないので消えなかった)
  setInput(el.palWidth, next.pallet_width);
  setInput(el.palLength, next.pallet_length);
  const widthChanged = setInput(el.prodWidth, next.product_width);
  const lengthChanged = setInput(el.prodLength, next.product_length);
  // サーバ側の製品サイズが変わった(資材展開・自動選定の確定など)。
  // 打った本人がいなくても一覧を追随させる
  if (widthChanged || lengthChanged) scheduleLiveList();

  // 決まったものは1行。**製品とパレットは1つの決めごと**なので、
  // 2行に分けると読む場所が2つになる
  setStatus(el.sizeStatus, next.size_status,
            next.product_set && next.pallet_set);
  // パレットが決まって初めて渡せる。決まる前は出さない(旧版 `btnUFMAP`)
  el.showOnStock.hidden = !next.pallet_set;

  setToggle(el.showAll, next.show_all);
  setToggle(el.exOnly, next.ex_only);
  setToggle(el.twoStack, next.two_stack);
  el.exOnly.disabled = !next.ex_only_enabled;
  why(el.exOnlyWhy, next.ex_only_why);

  // **主役は「パレットを選ぶ」1つ。** 押せない理由はサーバが持つ
  why(el.decideWhy, next.decide_why);
  why(el.productFrom, next.product_from);
  refreshDecide();

  renderSteps(next);
  renderResultTabs(next);
  render1P0113(next.p1);
  renderBoards(next.boards);
  renderAngles(next.angles);
  renderPlans(next.plans);
  renderOutputs(next.outputs);
  renderAdmin(next.admin);
  // 描き直したあとも、光らせていたボードは光らせたままにする
  applyLink();

  // ステータスリボン。値を持っているのはサーバなので写すだけ
  if (next.ribbon) applyRibbon(next.ribbon);
}

/* ================================================================
   作業の段

   **いま何段目か・何が決まったか・次に押すものは、すべてサーバが決める。**
   ここは受け取った値を属性と文字にするだけで、条件を組み立て直さない
   ── 組み立て直すと、判断が2か所に分かれて必ずどちらかが古くなる。
   ================================================================ */
function renderSteps(next) {
  for (const item of next.steps || []) {
    for (const card of document.querySelectorAll(
        `[data-step="${item.key}"]`)) {
      card.dataset.state = item.state;
      const no = card.querySelector(".stepno");
      if (no) no.textContent = item.number;
    }
  }
  // 畳んだときに残す確定値。**閉じていても何が決まっているかは失わない**
  const summary = Object.fromEntries(
    (next.steps || []).map((s) => [s.key, s.summary]));
  for (const [id, key] of [["sizeSummary", "size"],
                           ["boardSummary", "boards"]]) {
    const node = document.getElementById(id);
    if (node) node.textContent = summary[key] || "";
  }

  why(el.nextHint, next.next_hint);
  // 段が進んだら開き直しは解除する。**いまの段は常に開く**ので、
  // 前の段が開いたままだと操作カラムがまた入りきらなくなる
  for (const card of document.querySelectorAll(".step.is-open")) {
    if (card.dataset.state === "current") card.classList.remove("is-open");
  }

  // 主動作はいつも1つ。前に付けた強調は必ず外す
  for (const button of document.querySelectorAll(".btn.is-primary")) {
    button.classList.remove("is-primary");
  }
  const primary = next.primary_action && document.getElementById(next.primary_action);
  if (primary) primary.classList.add("is-primary");
}

/**
 * 結果の面(配置図 / 選定一覧)の見出し。
 *
 * **開く前に中身が分かるようにする**(情報の匂い、§2.4)。
 * 枚数と配置の有無はサーバが決めた値をそのまま写す。
 */
function renderResultTabs(next) {
  if (!el.resultTabs) return;
  const boards = next.boards || {};
  const picked = (boards.upper || []).length + (boards.lower || []).length;
  tabs.setBadges(el.resultTabs, {
    plan: next.plans && next.plans.placed
      ? { text: "配置済", level: "ok" } : { text: "" },
    picked: picked ? { text: String(picked) } : { text: "" },
  });
}

/* ================================================================
   1P0113 裸梱包
   ================================================================ */
function render1P0113(p1) {
  if (!p1) return;

  // パレットを使わないモードなので、パレット欄と入れ替える
  el.p1Card.hidden = !p1.on;
  el.sizeCard.hidden = p1.on;
  setToggle(el.force1p, p1.forced, "btn--on");
  if (!p1.on) return;

  el.p1Materials.replaceChildren(...p1.materials.map((m) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "p1mat";
    button.dataset.info = m.info;
    button.dataset.ok = String(m.ok);

    const name = document.createElement("b");
    name.textContent = m.name;
    const size = document.createElement("span");
    size.className = "p1size";
    size.textContent = m.label;
    const count = document.createElement("span");
    count.className = "p1count";
    count.textContent = m.count_caption;
    button.append(name, size, count);
    return button;
  }));

  el.p1Tip.textContent = p1.tip;
  el.p1Tip.className = `status status--${p1.ready ? "ok" : "ng"}`;
  el.p1Qty.value = p1.qty;
  el.p1Qty.min = p1.qty_min;
  el.p1Qty.max = p1.qty_max;
  why(el.p1Why, p1.why);
}

/* ================================================================
   出す — 帳票と倉庫送信
   ================================================================ */
function renderOutputs(outputs) {
  if (!outputs) return;

  const reasons = [];
  for (const report of outputs.reports) {
    const button = document.getElementById(`report-${report.key}`);
    if (!button) continue;
    button.disabled = !report.can;
    // 押す前に確かめること。**訊くかどうかも文面も選択肢もサーバが
    // 決める**(切断依頼の「丈カットを行いますか」。空なら訊かない)
    asks[report.key] = report.ask && report.ask.choices ? report.ask : null;
    if (report.why) reasons.push(`${report.label}: ${report.why}`);
  }
  el.sendWarehouse.disabled = !outputs.can_send;
  if (outputs.send_why) reasons.push(`倉庫送信: ${outputs.send_why}`);

  // 「使用する」。押したあとは**押せないまま札を替える** ── 消すと
  // 「押したのか、そもそも無いのか」が分からない。理由はボタンの
  // 真横に出す(押せない理由を離すと、どのボタンの話か読めない)
  el.useBoards.disabled = !outputs.can_use;
  el.useLabel.textContent = outputs.use_done ? "使用済み" : "使用する";
  el.useWhy.textContent = outputs.use_why || "";

  // 押せない理由をボタンの真下にまとめる。同じ文が続いても
  // どのボタンの話か分かるよう、名前を頭に付ける
  el.outputWhy.replaceChildren(...[...new Set(reasons)].map((text) => {
    const p = document.createElement("p");
    p.className = "why";
    p.textContent = text;
    return p;
  }));
}

/* ================================================================
   管理者
   ================================================================ */
function renderAdmin(admin) {
  if (!admin) return;

  el.adminState.textContent = admin.authenticated ? "認証済み" : "未認証";
  // 認証が通っていれば、置き場所を案内する文はもう要らない
  if (el.authWhere) el.authWhere.hidden = admin.authenticated;
  el.savePattern.disabled = !admin.can_save;
  why(el.saveWhy, admin.save_why);
  el.patternsNote.textContent = admin.patterns_note;

  el.patternRows.replaceChildren(...admin.patterns.map((p) => {
    const tr = document.createElement("tr");
    for (const value of [p.id, p.product, p.boards]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(td);
    }
    // ボードの内訳は長い。省略して**「読込」を押せる位置に残す** ──
    // 押せないところへ追いやると、一覧に出ている意味が無い。
    // 全文は押さえたままにする(title)
    const boards = tr.lastElementChild;
    boards.className = "clip";
    boards.title = p.boards;
    const count = document.createElement("td");
    count.className = "n";
    count.textContent = p.usage_count;
    const at = document.createElement("td");
    at.textContent = p.registered_at;

    const cell = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn--find rowdel";
    button.textContent = "読込";
    button.dataset.pattern = p.id;
    cell.appendChild(button);
    tr.append(count, at, cell);
    return tr;
  }));

  el.usageRows.replaceChildren(...admin.usage.map((u) => {
    const tr = document.createElement("tr");
    const type = document.createElement("td");
    type.textContent = u.board_type;
    tr.appendChild(type);
    for (const value of [u.width, u.length, u.usage_count]) {
      const td = document.createElement("td");
      td.className = "n";
      td.textContent = value;
      tr.appendChild(td);
    }
    const at = document.createElement("td");
    at.textContent = u.last_used_at;
    tr.appendChild(at);
    return tr;
  }));
}

/* ================================================================
   ボード
   ================================================================ */
function boardRow(item) {
  const tr = document.createElement("tr");
  tr.dataset.width = item.width;
  tr.dataset.length = item.length;
  tr.dataset.key = `${item.width}x${item.length}`;
  item.values.forEach((value, index) => {
    const td = document.createElement("td");
    // 幅・丈は数。在庫の欄は記号(▲薄)
    if (index < 2) td.className = "n";
    else if (value) td.className = "thin";
    td.textContent = value;
    tr.appendChild(td);
  });
  return tr;
}

/** 選定済みの1行。削除は行の右端に置く(消す対象と押す場所を離さない)。 */
function selectedRow(item, category) {
  const tr = document.createElement("tr");
  tr.dataset.index = item.index;
  // 図の該当ボードと同じ鍵。押すと互いに光る
  tr.dataset.key = item.key || "";
  item.values.forEach((value, index) => {
    const td = document.createElement("td");
    if (index === 0) {
      // 種別は語のまま出す(VBA の '主' / '幅補填' / '丈補填')。
      // 主だけを強く出す ── 補填は数が多く、同じ強さだと本体が埋もれる
      if (value) {
        const tag = document.createElement("span");
        tag.className = `tag tag--${item.tag_kind || "fill"}`;
        tag.textContent = value;
        td.appendChild(tag);
      }
    } else {
      td.className = "n";
      td.textContent = value;
    }
    tr.appendChild(td);
  });

  const cell = document.createElement("td");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn--danger rowdel";
  button.textContent = "削除";
  button.dataset.category = category;
  button.dataset.index = item.index;
  cell.appendChild(button);
  tr.appendChild(cell);
  return tr;
}

function renderBoards(boards) {
  if (!boards) return;

  // 種別。開いているあいだにマスタを取り込み直すと候補が変わる
  el.boardType.replaceChildren(...boards.board_types.map((name) => {
    const option = document.createElement("option");
    option.value = option.textContent = name;
    option.selected = name === boards.board_type;
    return option;
  }));

  replaceKeepingPick(el.boardRows, boards.candidates.map(boardRow));
  el.candidateNote.textContent = boards.candidate_note;

  el.upperRows.replaceChildren(...boards.upper.map((r) => selectedRow(r, "upper")));
  el.lowerRows.replaceChildren(...boards.lower.map((r) => selectedRow(r, "lower")));
  el.upperTitle.textContent = boards.upper_title;
  el.lowerTitle.textContent = boards.lower_title;
  // 上下共用モードでは上用=下用と同サイズ。欄そのものを出さない
  el.upperCard.hidden = !boards.show_upper;
  el.selectedSummary.textContent = boards.summary;

  // 候補の隣に出す、同じ中身の写し(「選定一覧」タブと同じデータ)
  el.boardPickedUpperRows.replaceChildren(...boards.upper.map((r) => selectedRow(r, "upper")));
  el.boardPickedLowerRows.replaceChildren(...boards.lower.map((r) => selectedRow(r, "lower")));
  el.boardPickedUpperTitle.textContent = boards.upper_title;
  el.boardPickedLowerTitle.textContent = boards.lower_title;
  el.boardPickedUpper.hidden = !boards.show_upper;

  setToggle(el.fatigue, boards.fatigue, "btn--fatigue");
  setToggle(el.stockAware, boards.stock_aware);
  // 選定ボタンの色は**操作の種類**(選定)なので変えない。疲労度が効いて
  // いることは語で示す(VBA `SELECT_ON_TEXT` = 「疲労考慮ﾎﾞｰﾄﾞ」)。
  // 色まで疲労度に寄せると、押す先が変わったように読める
  el.autoSelectLabel.textContent = boards.fatigue ? "疲労考慮ボード" : "ボード選定";
  el.fatigueWhy.hidden = !boards.fatigue;
  el.fatigueBase.textContent = boards.base_point;

  el.autoSelect.disabled = !boards.can_select;
  why(el.selectWhy, boards.select_why);
  el.place.disabled = !boards.can_place;
  why(el.placeWhy, boards.place_why);
  // いまA/B/Cのどれが出ているか。**押した結果が変わったのかどうか**は
  // 一覧を見比べるより、ここを読むほうが早い
  el.changeCandidate.disabled = !boards.can_change;
  el.changeAxis.textContent = boards.change_axis;
  why(el.changeWhy, boards.change_why);
}

/* ================================================================
   配置図
   ================================================================ */
function renderPlans(plans) {
  if (!plans) return;

  el.planStatus.textContent = plans.status;
  el.planUsage.textContent = plans.usage;
  el.planUpperTitle.textContent = plans.upper_title;
  el.planLowerTitle.textContent = plans.lower_title;
  // 上下共用モードでは上用=下用と同サイズ。図も1つでよい
  el.planUpperBox.hidden = !plans.show_upper || !plans.upper;
  el.planAngleBox.hidden = !plans.angle;

  for (const [box, plan] of [[el.planLower, plans.lower], [el.planUpper, plans.upper]]) {
    const svg = boardSvg(plan, plans.view_box);
    box.replaceChildren(svg);
    // 入れてから測る。はみ出しているぶんまで見えるように広げる
    fitToContent(svg, plans.view_box);
  }
  const angle = angleSvg(plans.angle, plans.angle_view_box);
  el.planAngle.replaceChildren(angle);
  fitToContent(angle, plans.angle_view_box);

  // 細くて文字が入らないボードは色分けで示す。凡例が無いと読めない
  el.planLowerLegend.replaceChildren(...legendItems(plans.lower));
  el.planUpperLegend.replaceChildren(...legendItems(plans.upper));

  el.planLines.replaceChildren(...plans.lines.map((line) => {
    const p = document.createElement("p");
    // 「カットあり」と「不足あり」は別。同じ見た目にすると、
    // 直さなくてよいものと直すべきものが混ざる
    p.className = `status status--${line.kind === "ok" ? "ok" : "ng"}`;
    p.dataset.kind = line.kind;
    const label = document.createElement("b");
    label.textContent = line.label;
    p.append(label, document.createTextNode(line.text));
    return p;
  }));
  el.planHint.hidden = !plans.placed;
}

/* ================================================================
   図と一覧の相互ハイライト(§設計指針 共通運命の要因)

   どのボードがどこに載るのかは、図と一覧を見比べないと分からない。
   tkinter版には無かった ── Canvas は図形の集まりで、当たり判定を
   自前で書く必要があったため。SVG なら要素なのでそのまま押せる。
   ================================================================ */
let linked = "";

function applyLink() {
  for (const node of document.querySelectorAll("[data-key]")) {
    node.classList.toggle("is-linked", Boolean(linked) && node.dataset.key === linked);
  }
}

/**
 * 同じボードを指すものを全部光らせる。同じものを押したら消す。
 *
 * **面が分かれているので、光らせるだけでは伝わらない。** 図で押したら
 * 選定一覧を、一覧で押したら図を開く ── 押した意図は「これは向こうで
 * どれか」なので、向こうを見せるところまでが返事になる。空を押したのは
 * 「消す」なので、そのときは面を動かさない。
 */
function setLink(key, reveal) {
  linked = (key && key !== linked) ? key : "";
  applyLink();
  if (!linked || !reveal || !el.resultTabs) return;
  tabs.select(el.resultTabs, reveal);
  const shown = document.querySelector(
    `#panel-${reveal} .is-linked`);
  if (shown) shown.scrollIntoView({ block: "nearest" });
}

/* ================================================================
   アングル
   ================================================================ */
/** 候補アングルの1行。選んでから「アングル追加」で選択側へ移す。 */
function angleRow(length, index) {
  const tr = document.createElement("tr");
  tr.dataset.length = length;
  tr.dataset.index = index;
  tr.dataset.key = String(length);
  const td = document.createElement("td");
  td.className = "n";
  td.textContent = length;
  tr.appendChild(td);
  return tr;
}

/** 選択済みアングルの1行。削除はボードと同じく行の右端に置く。 */
function angleSelectedRow(length, index) {
  const tr = angleRow(length, index);
  const cell = document.createElement("td");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn--danger rowdel";
  button.textContent = "削除";
  button.dataset.index = index;
  cell.appendChild(button);
  tr.appendChild(cell);
  return tr;
}

function renderAngles(angles) {
  if (!angles) return;

  // 保護材がアングル以外に確定していれば、この欄は使わない
  el.angleCard.hidden = !angles.show;
  el.hosozaiLabel.hidden = !angles.hosozai_label;
  el.hosozaiLabel.textContent = angles.hosozai_label;
  if (!angles.show) return;

  replaceKeepingPick(el.angleRows, angles.candidates.map(angleRow));
  // 選択側は選ばせない(削除は行のボタンで押す)ので、そのまま入れ替える
  el.angleSelected.replaceChildren(...angles.selected.map(angleSelectedRow));
  el.angleNeedCut.hidden = !angles.need_cut;
  el.autoAngle.disabled = !angles.can_auto;
  el.drawAngle.disabled = !angles.can_draw;
  // 押せない理由はサーバがまとめてある(同じ文を2つ出さない)
  why(el.angleWhy, (angles.why || []).join("　"));
}

/** 一覧の中で1行だけを選んだ状態にする。選び直しは押した行に移る。 */
function pick(tbody, tr) {
  for (const row of tbody.querySelectorAll("tr")) {
    row.classList.toggle("on", row === tr);
  }
}

/** いま選ばれている行。無ければ `null`。 */
function picked(tbody) {
  return tbody.querySelector("tr.on");
}

/**
 * 中身を入れ替えても、選んでいた行は選んだままにする。
 *
 * どの操作もサーバが画面ぜんぶを返すので一覧は毎回作り直される。
 * そのたびに選択が外れると、同じボードを2回足すのに毎回選び直す
 * ことになる(tkinter版の Treeview は選択が残っていた)。
 * 位置ではなく寸法(`data-key`)で覚える ── 在庫考慮を切り替えると
 * 並びが変わることがある。
 */
function replaceKeepingPick(tbody, nodes) {
  const key = picked(tbody)?.dataset.key ?? null;
  tbody.replaceChildren(...nodes);
  if (key === null) return;
  const again = [...tbody.children].find((tr) => tr.dataset.key === key);
  if (again) again.classList.add("on");
}

function applyRibbon(ribbon) {
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

/** 送って、返ってきた画面を描く。断られたら理由をそのまま出す。 */
async function send(path, body) {
  try {
    const next = await api.post(path, body || {});
    render(next);
    if (next.message) toast(next.message, next.found === false ? "ng" : "ok");
    // 結果そのものではないが伝えるべきこと(疲労度マップが引けなかった等)。
    // 黙って通常選定へ倒すと、効いていないことに誰も気づけない
    (next.notes || []).forEach((note) => toast(note, "warn"));
    return next;
  } catch (err) {
    // 422 / 500(業務としての断り・選定の失敗)も本文に画面ぜんぶが入っている。
    // 押した拍子に一覧が消えると、断られたのか壊れたのか区別できない
    if (err.body && err.body.rows) {
      render(err.body);
      (err.body.notes || []).forEach((note) => toast(note, "warn"));
    }
    toastError(err);
    return null;
  }
}

/**
 * 選んだものを持って別の画面へ移る(旧版 `btnMap` / `btnUFMAP`)。
 *
 * **品目や寸法をここから送らない。** 何が決まっているかはサーバ側の
 * 作業状態(`work_context`)が持っているので、押したことだけを伝える
 * ── 送ると、同じ事実が画面とサーバの2か所に生まれる。
 * 断り(まだ何も選んでいない)は 422 で返る。移らずに理由だけを出す。
 */
async function handoff(path) {
  try {
    const result = await api.post(path, {});
    nav.go(result.next);
  } catch (err) {
    toastError(err);
  }
}

export function start(initial) {
  for (const id of ["lotCaption", "banner", "listNote", "palletRows", "rowNote",
                    "palWidth", "palLength", "prodWidth", "prodLength",
                    "sizeStatus", "decideWhy", "productFrom", "exOnlyWhy",
                    "showOnStock",
                    "showAll", "exOnly", "twoStack",
                    "applyPallet", "decide", "clearSizes",
                    // ボード
                    "boardCard", "boardType", "boardRows", "candidateNote",
                    "addCount", "addUpper", "addLower",
                    "autoSelect", "autoSelectLabel", "fatigue", "fatigueWhy",
                    "fatigueBase", "place", "clearBoards", "selectWhy",
                    "changeCandidate", "changeAxis", "changeWhy",
                    "stockAware", "upperCard", "upperTitle", "upperRows",
                    "lowerTitle", "lowerRows", "selectedSummary", "placeWhy",
                    "showOnMap",
                    // 出す前に訊く窓
                    "askDialog", "askTitle", "askBody", "askChoices",
                    // ボードの選定済み(候補の隣、右の余白)
                    "boardPickedUpper", "boardPickedUpperTitle", "boardPickedUpperRows",
                    "boardPickedLowerTitle", "boardPickedLowerRows",
                    // アングル
                    "angleCard", "angleRows", "angleSelected", "angleNeedCut",
                    "addAngle", "autoAngle", "drawAngle", "angleWhy",
                    "hosozaiLabel",
                    // 配置図
                    "planStatus", "planUsage", "planLines", "planHint",
                    "planUpperBox", "planUpperTitle", "planUpper", "planUpperLegend",
                    "planLowerTitle", "planLower", "planLowerLegend",
                    "planAngleBox", "planAngle",
                    // 1P0113 / 出す / 管理者
                    "sizeCard", "p1Card", "p1Materials", "p1Tip", "p1Qty",
                    "p1Why", "force1p",
                    "sendWarehouse", "outputWhy", "useBoards", "useWhy", "useLabel",
                    "adminState", "authWhere", "savePattern",
                    "saveWhy", "patternsNote", "patternRows", "usageRows",
                    // 作業の段と、結果の面
                    "nextHint", "outputCard", "resultTabs"]) {
    el[id] = document.getElementById(id);
  }
  tabs.attachAll();

  linked = "";

  // 決まった段は畳んである。見出しを押せば開く ── 直したくなったときに
  // 辿り着けなくならないようにする(開閉は見た目だけなので画面が持つ)。
  // `document` に付けるので、この画面のあいだだけ有効にする(`nav.js`)
  document.addEventListener("click", (event) => {
    // 見出しの中のボタン(「在庫を見る」)は、押しても畳みを動かさない。
    // **押した意図はそのボタンのもの**で、段の開け閉めではない
    if (event.target.closest("button.btn")) return;
    const header = event.target.closest(
      ".step:not([data-state='current']) > header, .foldable > header");
    if (header) header.parentElement.classList.toggle("is-open");
  }, { signal: pageSignal() });

  // 右寄せにする列は、見出しに付いている印から読む。
  // 並びを2か所に書かない(サーバの `PALLET_COLUMNS` が唯一の出どころ)
  NUMERIC = [...document.querySelectorAll("#sizeCard thead th")]
    .map((th) => th.classList.contains("n"));

  render(initial);

  // 行を押しても、まだ何も決めない。**選ぶ(ハイライト)だけ**にして、
  // 入力欄へ写し、マウスを乗せたときのツールチップ用に note を持つ。
  // サーバへ伝える(決める)のは「パレット決定」を押した瞬間だけ
  // (以前は押した時点で決まっていたため、見比べているだけで確定通知が
  // たまってしまっていた ── 現場の声への対応)。
  el.palletRows.addEventListener("click", (event) => {
    const tr = event.target.closest("tr");
    if (!tr || !tr.dataset.width) return;
    pickedPalletRow = {
      width: tr.dataset.width, length: tr.dataset.length,
      symbol: tr.dataset.symbol || "", key: tr.dataset.key,
    };
    el.palWidth.value = tr.dataset.width;
    el.palLength.value = tr.dataset.length;
    el.rowNote.textContent = tr.dataset.note || "";
    applySelection();
  });

  // 一覧に無い寸法を使うときの逃げ道。押した寸法で決める
  el.applyPallet.addEventListener("click", () =>
    send("/api/selection/pallet/apply",
         { width: el.palWidth.value, length: el.palLength.value }));

  // 製品 幅・丈を打つたびに、**確定させずに**候補を一覧へ出す。
  // 片方だけでもその辺に近いパレットを、両方そろえば「載るか」の
  // 厳密な判定に切り替わる(`list_pallets_by_product_dims`)。
  // 連打で毎回サーバへ聞きに行かないよう間を置く(デバウンス)。
  for (const node of [el.prodWidth, el.prodLength]) {
    node.addEventListener("input", scheduleLiveList);
  }

  // **一覧で行を選んでいるときだけ押せる。** 押した瞬間にその行で
  // 決める(以前は製品サイズが入っていれば押せて、押すたびに自動選定が
  // 走っていたため、「一覧で選んだのか、押して決まったのか」が
  // あいまいだった ── 現場の声への対応)。
  el.decide.addEventListener("click", () => {
    if (!pickedPalletRow) return;
    send("/api/selection/pallet/pick", {
      width: pickedPalletRow.width, length: pickedPalletRow.length,
      symbol: pickedPalletRow.symbol,
      product_width: el.prodWidth.value,
      product_length: el.prodLength.value,
    });
  });

  el.clearSizes.addEventListener("click", () => {
    pickedPalletRow = null;
    el.rowNote.textContent = "";
    send("/api/selection/clear");
  });

  // 決まったパレットの寸法で簡易在庫を見る(旧版 `btnUFMAP`)。
  // 寸法を画面から送らないのはボードMAPと同じ理由 ── 何が決まって
  // いるかはサーバ側の作業状態が持っている
  el.showOnStock.addEventListener("click", () => handoff("/api/selection/stock"));

  for (const [button, name] of [[el.showAll, "show_all"], [el.exOnly, "ex_only"],
                                [el.twoStack, "two_stack"],
                                [el.fatigue, "fatigue"],
                                [el.stockAware, "stock_aware"]]) {
    button.addEventListener("click", () => send(`/api/selection/toggle/${name}`));
  }

  // --- ボード ---------------------------------------------------
  el.boardType.addEventListener("change", () =>
    send("/api/selection/board-type", { board_type: el.boardType.value }));

  // 候補は「選んでから上用/下用へ入れる」。どれを入れるのか決めてから
  // 行き先を選ぶ順で、VBA も候補リストの真下に追加行を置いていた
  el.boardRows.addEventListener("click", (event) => {
    const tr = event.target.closest("tr");
    if (tr && tr.dataset.width) pick(el.boardRows, tr);
  });

  for (const [button, category] of [[el.addUpper, "upper"], [el.addLower, "lower"]]) {
    button.addEventListener("click", () => {
      const tr = picked(el.boardRows);
      if (!tr) {
        // サーバへ投げても同じことを言われるが、往復させる意味が無い
        toast("追加するボードを一覧から選んでください。", "warn");
        return;
      }
      send("/api/selection/boards/add", {
        category, width: tr.dataset.width, length: tr.dataset.length,
        count: el.addCount.value,
      });
    });
  }

  // 候補の隣の写し(`boardPicked*Rows`)も同じ行なので、同じ削除・
  // 図とのリンクを効かせる ── どちらの一覧を触っても同じ場所
  // (サーバの状態)を書き換えるだけ
  for (const tbody of [el.upperRows, el.lowerRows,
                       el.boardPickedUpperRows, el.boardPickedLowerRows]) {
    tbody.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-index]");
      if (button) {
        send("/api/selection/boards/remove",
             { category: button.dataset.category, index: button.dataset.index });
        return;
      }
      // 行を押したら、そのボードが図のどこに載るのかを出す
      const tr = event.target.closest("tr[data-key]");
      if (tr) setLink(tr.dataset.key, "plan");
    });
  }

  el.showOnMap.addEventListener("click", () => handoff("/api/selection/map"));

  el.autoSelect.addEventListener("click", () =>
    send("/api/selection/boards/auto-select"));
  el.clearBoards.addEventListener("click", () =>
    send("/api/selection/boards/clear"));
  el.place.addEventListener("click", () => {
    // **置いたあともボードの段を開いたままにする。** 配置が済むと
    // この段は「決まった」扱いで畳まれ、次の段(アングル)へ進む。
    // だが置いた図を見てから「候補変更」で別案に替えるのが実際の
    // 使い方なので、畳まれるとその手が届かない(現場の声:
    // 「選定→配置で強制的にアングルに移行してしまう。それだと
    //  候補変更する余裕がない」)。段の進み方はそのままで、
    // **開いておくだけ**にする ── 見終わったら見出しを押せば畳める
    el.boardCard.classList.add("is-open");
    send("/api/selection/boards/place");
  });
  // 敷き詰め方式の別解。選定と配置まで一度に入れ替わるので、
  // 「選定」「配置」とは別のエンドポイント
  el.changeCandidate.addEventListener("click", () => {
    // **押した段を開いたままにする。** 配置まで済むとボードの段は
    // 「決まった」扱いで畳まれるが、このボタンは A→B→C と**続けて
    // 押すもの**なので、1回ごとに畳まれると見出しを開き直す手間が
    // 押す回数だけ増える(「配置」は一度押せば終わりなので畳んでよい)
    el.boardCard.classList.add("is-open");
    send("/api/selection/boards/candidate");
  });

  // --- 配置図 ---------------------------------------------------
  for (const box of [el.planUpper, el.planLower]) {
    box.addEventListener("click", (event) => {
      const board = event.target.closest(".plan__board");
      setLink(board ? board.dataset.key : "", "picked");
    });
  }

  // --- アングル -------------------------------------------------
  el.angleRows.addEventListener("click", (event) => {
    const tr = event.target.closest("tr");
    if (tr) pick(el.angleRows, tr);
  });

  el.addAngle.addEventListener("click", () => {
    const tr = picked(el.angleRows);
    if (!tr) {
      toast("追加するアングルを候補から選んでください。", "warn");
      return;
    }
    send("/api/selection/angles/add", { length: tr.dataset.length });
  });

  el.angleSelected.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-index]");
    if (!button) return;
    send("/api/selection/angles/remove", { index: button.dataset.index });
  });

  el.autoAngle.addEventListener("click", () =>
    send("/api/selection/angles/auto"));
  el.drawAngle.addEventListener("click", () =>
    send("/api/selection/angles/draw"));

  // --- 1P0113 -----------------------------------------------------
  el.force1p.addEventListener("click", () => send("/api/selection/1p0113/force"));
  el.p1Qty.addEventListener("change", () =>
    send("/api/selection/1p0113/qty", { qty: el.p1Qty.value }));
  // 資材を押すとフル情報に差し替わる(VBA `Show1P0113LabelTip`)。
  // もう一度押すとコード/単位に戻る
  el.p1Materials.addEventListener("click", (event) => {
    const row = event.target.closest(".p1mat");
    if (!row) return;
    const showing = el.p1Tip.dataset.showing === row.dataset.info;
    el.p1Tip.textContent = showing ? state.p1.tip : row.dataset.info;
    el.p1Tip.dataset.showing = showing ? "" : row.dataset.info;
  });

  // --- 出す -------------------------------------------------------
  for (const button of document.querySelectorAll("[data-report]")) {
    // 帳票はサーバがHTMLをそのまま返す。別窓で開いて印刷する
    button.addEventListener("click", () => {
      const url = tokenUrl(button.dataset.url);
      // 押す前に確かめることがあれば訊く(切断依頼の「丈カットを行いますか」)。
      // **訊くかどうかも文面も選択肢もサーバが決める**
      // (`outputs.len_cut_question`)。切るか切らないかを選べるときだけ
      // 訊く ── パレットをはみ出すなら選択肢が無いので訊かずに切り、
      // 丈カットが無ければ訊く意味が無い
      const ask = asks[button.dataset.report];
      if (!ask) { openReport(url); return; }
      showAsk(ask, (key) => {
        if (key === "cancel") return;               // 本当にやめる
        openReport(url + (url.includes("?") ? "&" : "?")
                       + `use_len_cut=${key === "yes" ? "1" : "0"}`);
      });
    });
  }
  el.useBoards.addEventListener("click", () =>
    send("/api/selection/boards/use"));
  el.sendWarehouse.addEventListener("click", async () => {
    const next = await send("/api/selection/send");
    // 送るのは倉庫連携の画面。組み立てただけでは登録されていない。
    // 差し替えで移るので、出したばかりのトーストが消えない
    if (next && next.next_url) nav.go(next.next_url);
  });

  // --- 管理者 -------------------------------------------------------
  // 認証の入力欄は「設定」画面に移した。ここは結果を
  // 映すだけ(プロセスに1つの状態なので、どちらで通しても同じ)
  el.savePattern.addEventListener("click", () =>
    send("/api/selection/pattern/save"));
  el.patternRows.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-pattern]");
    if (button) send("/api/selection/pattern/load", { id: button.dataset.pattern });
  });
}
