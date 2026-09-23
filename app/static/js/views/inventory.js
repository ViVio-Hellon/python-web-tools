/*
  簡易在庫。

  判断はサーバが済ませてある。ここは受け取ったものを並べ、押されたら投げるだけ。
  「この棚は検索で当たった」「この行は払い出せる」といった判断は
  `presenters/inventory.py` と `pallet_service.py` が返す。

  【図が主役】
  保管位置の図は `pallet_map.json` の論理座標をそのまま viewBox に載せている。
  拡大しても文字がにじまず、押せる大きさは画面幅に追従する
  (タッチ端末があるので、これが効く)。
*/

import { api, tokenUrl } from "../api.js";
import { toast, toastError } from "../toast.js";
import * as tabs from "../tabs.js";
import * as toggles from "../toggles.js";
import * as unsaved from "../unsaved.js";
// `mapedit.js` は動的に読み込む(理由は `settings.js` が `master.js` を
// 動的に読み込んでいるのと同じ ── このファイル自身と版クエリを合わせ、
// 入れ替えたときに中身も必ず一緒に入れ替わるようにするため)。
// トップレベルで一度だけ確定させておけば、以後は今までどおり
// `mapedit.grip(...)` のように同期的に呼べる。
const VERSION_QUERY = new URL(import.meta.url).search;
const mapedit = await import(`../mapedit.js${VERSION_QUERY}`);

const SVG_NS = "http://www.w3.org/2000/svg";

const el = {};
let selected = null;      // 払い出す対象の行
let lastQuery = null;     // 「最新にする」で同じ表示を取り直すため
let dragger = null;       // 掴む手(`mapedit.js`)
let zoomer = null;        // 拡大縮小(`mapedit.js`)
let arranger = null;      // そろえる・隙間をなくす(`mapedit.js`)
let lastMap = null;       // いまの図。掴んだ箱の元の大きさを引くのに要る
let picked = "";          // 配置編集で選んでいる位置(消すときの対象)

function setStatus(message, kind) {
  if (!message) { el.status.hidden = true; return; }
  el.status.hidden = false;
  el.status.textContent = message;
  el.status.className = `status status--${kind}`;
}

// ------------------------------------------------------------------
// 図
// ------------------------------------------------------------------
function drawMap(map) {
  lastMap = map;
  el.map.setAttribute("viewBox", map.view_box);
  el.map.replaceChildren();

  el.map.classList.toggle("map--editing", Boolean(map.editing));
  // 編集をOFFにしたら複数選択も捨てる。**捨てないと、次にONにしたときも
  // 前回選んでいた箱の輪郭が残ったままになる**(現場の声)。
  // `picked`(単発クリックで選んだ「消す対象」)も同様に捨てる ──
  // `dragger.clearSelection()` は `mapedit.js` 内部の複数選択
  // (Shift+クリック)だけを見ており、こちらの単一選択とは別の状態
  // なので、片方だけ消すと「編集をOFFにしても強調表示が残り続ける」
  // (現場の声)。
  if (!map.editing) {
    dragger?.clearSelection();
    picked = "";
  }

  if (map.background) {
    const image = document.createElementNS(SVG_NS, "image");
    // **ヘッダを付けられない読み込み**なので、トークンはURLに載せる
    image.setAttribute("href", tokenUrl(map.background));
    // **枠いっぱいに引き伸ばさない。** 写真の画角は図の枠と一致しないので、
    // ずらし量と倍率(`background_x/y/scale`)のとおりに置く
    mapedit.applyBackground(image, map);
    el.map.appendChild(image);
  }

  for (const pos of map.positions) {
    const group = document.createElementNS(SVG_NS, "g");
    group.setAttribute("class", `pos pos--${pos.state}`);
    group.setAttribute("tabindex", "0");
    group.setAttribute("role", "button");
    // 掴む手(`mapedit.js`)が名前で引けるようにする
    group.setAttribute("data-name", pos.name);
    if (pos.name === picked) group.classList.add("is-selected");
    // 読み上げと吹き出しの両方に、押すと何が起きるかを書く
    const what = pos.count ? `${pos.count}種類` : "在庫なし";
    group.setAttribute("aria-label", `位置 ${pos.name}(${what})`);

    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", pos.x);
    rect.setAttribute("y", pos.y);
    rect.setAttribute("width", pos.w);
    rect.setAttribute("height", pos.h);
    rect.setAttribute("rx", "2");

    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", pos.x + pos.w / 2);
    text.setAttribute("y", pos.y + pos.h / 2);
    text.textContent = pos.name;

    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = `位置 ${pos.name} — ${what}`;

    group.append(rect, text, title);
    // 掴みしろは**編集中だけ**。出しっぱなしにすると、見るだけのときに
    // 「掴める」と読めてしまう
    if (map.editing) group.appendChild(mapedit.grip(pos));
    // 押したときの動きは掴む手が決める(動かしたのか押したのかを分ける)。
    // キーボードは掴めないので、こちらは直に繋ぐ
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onTapPosition(pos.name);
      }
    });
    el.map.appendChild(group);
  }
  // 複数選択(Shift+クリック)の見た目を付け直す。図はまるごと
  // 作り直されるので、毎回付け直さないと消えたままになる
  dragger?.reapplySelection();
  // 保存していない図があれば、タブを閉じるときに聞く(`unsaved.js`)
  unsaved.mark("inventory", Boolean(map.dirty));

  // 在庫にはあるのに図に無い位置。図から押せないので黙っていない
  el.mapMissing.hidden = !map.missing.length;
  if (map.missing.length) {
    el.mapMissing.textContent =
      `図に無い位置に在庫があります: ${map.missing.join(", ")}`
      + "(この位置は図から押せません)";
  }

  // 受入の位置欄の候補にも使う。打ち間違いを減らす
  el.positions.replaceChildren(...map.positions.map((p) => {
    const option = document.createElement("option");
    option.value = p.name;
    return option;
  }));

  drawEditBar(map);
}

/** 配置編集の道具立て。**編集中だけ出す。** */
function drawEditBar(map) {
  if (!el.mapEditToggle) return;
  const editing = Boolean(map.editing);
  el.mapEditToggle.setAttribute("aria-pressed", String(editing));
  el.mapEditToggle.classList.toggle("btn--on", editing);
  el.mapEditBox.hidden = !editing;
  el.mapEditState.textContent = editing
    ? "編集中(ドラッグで動かせます。Shift+クリックでまとめて選び、"
      + "まとめて動かす・大きさを変えられます)" : "";
  el.mapDirty.hidden = !map.dirty;
  // 背景が無ければ合わせこむものが無い。押せるのに何も起きない欄を作らない
  el.mapBgPlace.hidden = !map.background;
  el.mapBgX.value = map.background_x;
  el.mapBgY.value = map.background_y;
  el.mapBgScale.value = map.background_scale;
}

// ------------------------------------------------------------------
// 一覧
// ------------------------------------------------------------------
function drawRows(view) {
  el.rows.replaceChildren(...view.rows.map((row) => rowElement(row, view.columns)));
  if (el.invTabs) {
    // **開く前に何件あるかが分かる**(§2.4)
    tabs.setBadges(el.invTabs, {
      list: { text: view.rows.length ? String(view.rows.length) : "" },
    });
  }
  el.found.textContent = view.found ? `${view.found} 件` : "";
  el.caption.textContent = view.caption || "";
  el.listNote.textContent = view.message || "";
  el.listNote.hidden = !view.message;
  clearSelection();
}

function rowElement(row, columns) {
  const tr = document.createElement("tr");
  tr.tabIndex = 0;
  for (const column of columns) {
    const td = document.createElement("td");
    if (column.numeric) td.className = "n";
    const value = row[column.label];
    td.textContent = (value === "" || value === null) ? "---" : value;
    tr.appendChild(td);
  }
  const pick = () => select(tr, row);
  tr.addEventListener("click", pick);
  tr.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(); }
  });
  return tr;
}

// ------------------------------------------------------------------
// 払い出しの対象
// ------------------------------------------------------------------
function clearSelection() {
  selected = null;
  for (const tr of el.rows.querySelectorAll("tr")) tr.removeAttribute("aria-selected");
  if (el.actTabs) tabs.setBadges(el.actTabs, { issue: { text: "" } });
  if (!el.issue) return;        // 資材モードには払い出しが無い
  el.picked.hidden = true;
  el.pickWhy.hidden = false;
  el.iqty.value = "";
  el.iqty.disabled = true;
  el.issue.disabled = true;
}

function select(tr, row) {
  for (const other of el.rows.querySelectorAll("tr")) other.removeAttribute("aria-selected");
  tr.setAttribute("aria-selected", "true");
  selected = row;
  // 資材モードには払い出しが無い(受け入れだけ)。行の強調だけで終わる
  if (!el.issue) return;

  // 行を選ぶのは「これを出す」という意思表示。**入力欄が別の面に
  // 隠れていたら開く** ── 選んだのに何も起きないように見せない
  if (el.actTabs) {
    tabs.select(el.actTabs, "issue");
    tabs.setBadges(el.actTabs, { issue: { text: "選択中", level: "ok" } });
  }
  el.pickWhy.hidden = true;
  el.picked.hidden = false;
  el.pkSize.textContent = `${row["幅"]} × ${row["丈"]}`;
  el.pkPos.textContent = row["位置"] || "---";
  el.pkSym.textContent = row["記号"] || "---";
  el.pkStock.textContent = row["在庫数"];
  el.iqty.disabled = false;
  el.iqty.max = row["在庫数"];
  el.issue.disabled = false;
  el.iqty.focus();
}

// ------------------------------------------------------------------
// 通信
// ------------------------------------------------------------------
function render(view) {
  drawMap(view.map);
  drawRows(view);
}

async function runSearch() {
  const w = el.w.value.trim();
  const l = el.l.value.trim();
  if (!w || !l) {
    setStatus("幅と丈の両方を入れてください", "ng");
    return;
  }
  const mode = toggles.value(el.modeGroup, "mode") || "exact";
  lastQuery = () => api.get(
    `/api/inventory/search?w=${encodeURIComponent(w)}&l=${encodeURIComponent(l)}&mode=${mode}`);
  // 自分で打って探した時点で「渡されて来た」ではなくなる
  el.fromSelectionWhy.hidden = true;
  await load();
}

/**
 * 図の位置を押したときに、その位置の在庫を出す。
 *
 * **押したのに何も起きないように見えるのが一番わるい。** 一覧は別の面に
 * あるので、こちらから開く ── 「ここに何がある?」と押した以上、答えは
 * その場に出す。
 */
async function showPosition(name) {
  lastQuery = () => api.get(`/api/inventory/position/${encodeURIComponent(name)}`);
  // 位置で引き直したら、渡された寸法の結果はもう出ていない
  el.fromSelectionWhy.hidden = true;
  await load();
  if (el.invTabs) tabs.select(el.invTabs, "list");
}

/** 図の位置を押した。**編集中は選ぶだけ**(消す対象を決める)。

    編集中に一覧まで開くと、動かすつもりの操作で面が切り替わる。 */
function onTapPosition(name) {
  if (lastMap && lastMap.editing) {
    picked = name;
    for (const group of el.map.querySelectorAll(".pos")) {
      group.classList.toggle("is-selected", group.dataset.name === name);
    }
    return;
  }
  showPosition(name);
}

/* ================================================================
   配置編集

   判断はサーバが持つ(`pallet_map_session`)。ここは掴んだ結果を送り、
   返ってきた画面ぜんぶを描き直すだけ ── 棚検索と同じ約束。
   ================================================================ */
function startDragging() {
  dragger = mapedit.attach(el.map, {
    selector: ".pos",
    editing: () => Boolean(lastMap && lastMap.editing),
    find: (name) => (lastMap
                     ? lastMap.positions.find((p) => p.name === name) || null
                     : null),
    onTap: onTapPosition,
    onMove: (name, x, y) => sendMap("/api/inventory/map/move", { name, x, y }),
    onResize: (name, w, h) => sendMap("/api/inventory/map/resize", { name, w, h }),
    // 見た目(点線の色)だけでは「選べたか分からない」という声があった。
    // 文字でも件数を出す
    onSelectionChange: (count) => {
      if (el.mapMultiNote) {
        el.mapMultiNote.textContent = count ? `${count}件選択中` : "選択なし";
      }
      arranger?.update(count);
    },
  });
  // そろえる・隙間をなくす。計算はサーバ(`map_data.arrange`)
  const box = document.querySelector("#mapEditBox [data-arrange-box]");
  arranger = box ? mapedit.attachArrange({
    box, dragger,
    send: (names, op) => sendMap("/api/inventory/map/arrange", { names, op }),
  }) : null;
}

/** 図を触る操作。**返ってくるのは画面ぜんぶ**なので、そのまま描き直す。 */
async function sendMap(path, body = {}) {
  try {
    const view = await api.post(path, body);
    render(view);
    if (view.message) toast(view.message, "ok");
    return view;
  } catch (err) {
    // 断られても本文に画面ぜんぶが入っていることがある。
    // 「断られた」と「画面が古いまま」を同時に起こさない
    if (err.body && err.body.map) render(err.body);
    toastError(err);
    return null;
  }
}

async function load() {
  if (!lastQuery) return;
  try {
    const view = await lastQuery();
    render(view);
    setStatus(view.message || "", view.found ? "ok" : "warn");
  } catch (err) {
    toastError(err);
  }
}

function numbers(fields) {
  const out = {};
  for (const [key, node] of Object.entries(fields)) {
    const value = node.value.trim();
    if (!/^\d+$/.test(value)) {
      node.focus();
      return null;
    }
    out[key] = Number(value);
  }
  return out;
}

async function doReceive() {
  const values = numbers({ width: el.rw, length: el.rl, qty: el.rqty });
  if (!values) { toast("幅・丈・台数は数字で入れてください", "ng"); return; }
  if (!el.rpos.value.trim()) { el.rpos.focus(); toast("位置を入れてください", "ng"); return; }

  try {
    const body = await api.post("/api/inventory/receive", {
      ...values,
      position: el.rpos.value.trim(),
      symbol: el.rsym.value.trim(),
      industry: el.rind.value.trim(),
      unit: el.runit.value.trim(),
      note: el.rnote.value.trim(),
    });
    toast(body.message, "ok");
    el.rqty.value = "";
    el.rnote.value = "";
    // 動かした先の棚を出す。登録した結果がその場で見える
    await showPosition(el.rpos.value.trim());
  } catch (err) {
    afterFailure(err);
  }
}

async function doIssue() {
  if (!selected || !el.issue) return;
  const value = el.iqty.value.trim();
  if (!/^\d+$/.test(value)) { el.iqty.focus(); toast("数字で入れてください", "ng"); return; }

  try {
    const body = await api.post("/api/inventory/issue", {
      width: selected.key.width,
      length: selected.key.length,
      position: selected.key.position,
      qty: Number(value),
    });
    toast(body.message, "ok");
    await showPosition(selected.key.position);
  } catch (err) {
    afterFailure(err);
  }
}

/** 失敗の知らせ方。競合だけは表示を取り直す。 */
function afterFailure(err) {
  toastError(err);
  if (err.status === 409) {
    // 他の端末が先に動かした。古い数字のまま操作を続けさせない
    load();
  }
}

// ------------------------------------------------------------------
export function start(state) {
  for (const id of ["w", "l", "search", "clear", "status", "map", "mapMissing",
                    "mapWrap", "zoomIn", "zoomOut", "zoomNow", "zoomWhy",
                    "rows", "found", "caption", "listNote", "refresh",
                    "rw", "rl", "rqty", "rpos", "rsym", "rind", "runit", "rnote",
                    "receive", "picked", "pickWhy", "pkSize", "pkPos", "pkSym",
                    "pkStock", "iqty", "issue", "positions",
                    "fromSelectionWhy",
                    "mapEditToggle", "mapEditBox", "mapEditState", "mapDirty",
                    "mapMultiNote",
                    "mapNewPos", "mapAddPos", "mapRemovePos",
                    "mapBgFile", "mapClearBg", "mapBgPlace",
                    "mapBgX", "mapBgY", "mapBgScale", "mapBgFit",
                    "mapSave", "mapReset"]) {
    el[id] = document.getElementById(id);
  }
  el.invTabs = document.getElementById("invTabs");
  el.actTabs = document.getElementById("actTabs");
  el.modeGroup = document.querySelector('.choose[aria-label="一致条件"]');
  tabs.attachAll();
  // 一致条件を変えたら、その場で引き直す。**押してから「検索」を
  // もう一度押させない** ── 条件を変えるのは検索し直すことだから
  toggles.attach(el.modeGroup, "mode", () => { if (lastQuery) runSearch(); });
  // 再入場のたびに真っさらから。モジュールは使い回されるので、
  // 前に来たときの選択や検索条件がここに残っている(`nav.js`)
  selected = null;
  lastQuery = null;
  lastMap = null;
  picked = "";
  dragger?.reset();

  zoomer = mapedit.attachZoom({
    wrap: el.mapWrap, zoomIn: el.zoomIn, zoomOut: el.zoomOut,
    zoomNow: el.zoomNow,
    // 等倍のままだと押しにくいことに気づけないので、そのときだけ案内を出す
    onChange: (percent) => { el.zoomWhy.hidden = percent !== 100; },
  });

  render(state);
  // 資材選択から寸法を持って来たときは、**引いたところから始まっている**
  // (`presenters/inventory.initial`)。「最新にする」で取り直せるよう、
  // 同じ問い合わせをここでも組んでおく
  if (state.from_selection) {
    const w = state.width_text, l = state.length_text;
    lastQuery = () => api.get(
      `/api/inventory/search?w=${encodeURIComponent(w)}&l=${encodeURIComponent(l)}&mode=exact`);
    setStatus(state.message || "", state.found ? "ok" : "warn");
  }

  el.search.addEventListener("click", runSearch);
  el.refresh.addEventListener("click", load);
  for (const node of [el.w, el.l]) {
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); runSearch(); }
    });
  }
  el.clear.addEventListener("click", () => {
    el.w.value = "";
    el.l.value = "";
    el.w.focus();
    setStatus("", "ok");
    el.fromSelectionWhy.hidden = true;
  });
  // --- 配置編集 ---------------------------------------------------
  startDragging();
  el.mapEditToggle.addEventListener("click", () => sendMap(
    "/api/inventory/map/edit", { on: !(lastMap && lastMap.editing) }));
  el.mapAddPos.addEventListener("click", async () => {
    const view = await sendMap("/api/inventory/map/add",
                               { name: el.mapNewPos.value });
    if (view) el.mapNewPos.value = "";
  });
  el.mapRemovePos.addEventListener("click", () => {
    if (!picked) {
      toast("消す位置を図から選んでください。", "warn");
      return;
    }
    sendMap("/api/inventory/map/remove", { name: picked }).then(() => {
      picked = "";
    });
  });

  el.mapBgFile.addEventListener("change", () => {
    const file = el.mapBgFile.files && el.mapBgFile.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      sendMap("/api/inventory/map/background", { image: String(reader.result) });
      el.mapBgFile.value = "";
    };
    reader.onerror = () => toast("画像を読めませんでした。", "warn");
    reader.readAsDataURL(file);
  });
  el.mapClearBg.addEventListener("click", () =>
    sendMap("/api/inventory/map/background", { image: "" }));

  // 背景の合わせこみ。**数で入れる** ── 掴んで動かす形にすると箱を掴む
  // 操作とぶつかり、どちらが動いたのか分からなくなる
  const placeBg = () => sendMap("/api/inventory/map/background/place", {
    x: el.mapBgX.value, y: el.mapBgY.value, scale: el.mapBgScale.value,
  });
  for (const box of [el.mapBgX, el.mapBgY, el.mapBgScale]) {
    box.addEventListener("change", placeBg);
  }
  el.mapBgFit.addEventListener("click", () => sendMap(
    "/api/inventory/map/background/place", { x: 0, y: 0, scale: 1 }));

  el.mapSave.addEventListener("click", () => sendMap("/api/inventory/map/save"));
  el.mapReset.addEventListener("click", () => {
    // 出荷時に戻すと編集した内容は消える。取り消せないので一度確かめる
    if (confirm("出荷時の配置に戻します。編集した内容は消えます。よろしいですか?")) {
      sendMap("/api/inventory/map/reset");
    }
  });

  el.receive.addEventListener("click", doReceive);
  // 払い出しは現場モードだけ。資材モードでは画面に無い
  el.issue?.addEventListener("click", doIssue);
  el.iqty?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); doIssue(); }
  });
}
