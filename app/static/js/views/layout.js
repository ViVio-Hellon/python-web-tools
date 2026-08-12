/*
  棚検索の画面。

  どの操作もサーバが**画面ぜんぶ**を返すので、ここは受け取ったものを
  そのまま描き直すだけ。業務の判断(どの置き場が最寄りか、何が置いて
  あるか、押せるか)は1つも持たない。

  【ドラッグ】
  tkinter版は Canvas の座標から当たり判定を自前で書いていた。SVG は
  要素なのでそのまま掴める。画面がやるのは**ポインタ座標 → 図の論理座標**
  の変換だけで、動かしてよいかどうかも、枠に収める計算もサーバが持つ。
*/

import { api } from "../api.js";
import { toast, toastError } from "../toast.js";
import * as tabs from "../tabs.js";
import * as toggles from "../toggles.js";

const NS = "http://www.w3.org/2000/svg";
const el = {};
let state = null;

function node(name, attrs = {}) {
  const created = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) created.setAttribute(key, value);
  }
  return created;
}

function why(target, text) {
  target.hidden = !text;
  target.textContent = text || "";
}

/* ================================================================
   図
   ================================================================ */
function box(item, className) {
  const group = node("g", { class: className, "data-name": item.name });
  group.appendChild(node("rect", {
    x: item.x, y: item.y, width: item.w, height: item.h, rx: 2,
  }));
  const text = node("text", {
    x: item.x + item.w / 2, y: item.y + item.h / 2,
  });
  text.textContent = item.name;
  group.appendChild(text);

  // 図の中の文字は小さい。押さなくても分かる説明を添える
  const title = node("title");
  title.textContent = [
    item.name,
    item.category ? `種別: ${item.category}` : "",
    item.distance === null || item.distance === undefined
      ? "" : `拠点から ${item.distance}`,
  ].filter(Boolean).join(" / ");
  group.appendChild(title);
  return group;
}

function renderMap(next) {
  el.map.setAttribute("viewBox", next.view_box);
  el.map.classList.toggle("map--editing", next.editing);

  const parts = [];
  // 背景 → 区画 → 置き場 → 拠点 の順。後に描いたものが前に来る
  if (next.background) {
    parts.push(node("image", {
      href: next.background, x: 0, y: 0,
      width: next.width, height: next.height,
      preserveAspectRatio: "xMidYMid meet", opacity: 0.85,
    }));
  }
  for (const area of next.areas) parts.push(box(area, "area"));
  for (const shelf of next.shelves) {
    const group = box(shelf, `shelf shelf--${shelf.state}`);
    if (shelf.name === next.selected) group.classList.add("is-selected");
    parts.push(group);
  }
  for (const base of next.bases) parts.push(box(base, "shelf shelf--base"));
  el.map.replaceChildren(...parts);

  // 凡例。色の意味を figure の外に書く(色だけで伝えない)
  el.mapLegend.replaceChildren(...[
    ["shelf--idle", "資材あり"],
    ["shelf--empty", "割り当てなし"],
    ["shelf--hit", "検索で当たった"],
    ["shelf--base", "拠点"],
  ].map(([kind, label]) => {
    const span = document.createElement("span");
    span.className = "legend__item";
    const swatch = document.createElementNS(NS, "svg");
    swatch.setAttribute("width", 12);
    swatch.setAttribute("height", 12);
    swatch.setAttribute("class", kind.replace("shelf--", "shelf shelf--"));
    swatch.appendChild(node("rect", { width: 12, height: 12, rx: 2 }));
    span.append(swatch, document.createTextNode(label));
    return span;
  }));
}

function render(next) {
  state = next;

  renderMap(next);
  el.basePoint.value = next.base_point;
  toggles.mark(el.kindGroup, "kind", next.kind);
  el.searchWidth.value = next.width_text;
  el.searchLength.value = next.length_text;
  // ボードは幅×丈、アングルは丈だけ。使わない欄を残すと入れてしまう
  el.searchWidth.disabled = next.kind !== "board";

  el.result.hidden = !next.result;
  el.result.textContent = next.result;
  el.result.className = `status status--${next.found ? "ok" : "ng"}`;

  // 資材選択から渡ってきた資材。**渡っていなければボタンごと出さない**
  // (押せるのに何も起きないボタンを作らない)。件数を出しておくのは、
  // 押す前に「何を光らせるのか」が読めるようにするため(§2.4)
  el.fromSelection.hidden = !next.handoff;
  el.handoffLabel.textContent = `選定した資材 ${next.handoff}件をまとめて`;

  el.editToggle.setAttribute("aria-pressed", String(next.editing));
  el.editToggle.classList.toggle("btn--on", next.editing);
  el.editBox.hidden = !next.editing;
  el.editState.textContent = next.editing ? "編集中(ドラッグで動かせます)" : "";
  el.dirtyWhy.hidden = !next.dirty;

  el.materialsNote.textContent = next.materials_note;
  el.materialRows.replaceChildren(...next.materials.map((m) => {
    const tr = document.createElement("tr");
    const category = document.createElement("td");
    category.textContent = m.category;
    const size = document.createElement("td");
    size.className = "n";
    size.textContent = m.size;
    tr.append(category, size);
    return tr;
  }));
  if (el.layoutTabs) {
    // **開く前に何件あるか**を見出しが言う(情報の匂い、§2.4)。
    // 置き場を押したのに空だった、を開いてから知るのは遅い
    tabs.setBadges(el.layoutTabs, {
      contents: { text: next.materials.length ? String(next.materials.length) : "" },
    });
  }

  // マスタが指しているのに図に無いラベル。検索に当たらないので知らせる
  el.unplacedCard.hidden = !next.unplaced.length;
  el.unplacedNote.textContent = next.unplaced.length
    ? `${next.unplaced.length} 件` : "";
  el.unplacedList.replaceChildren(...next.unplaced.map((name) => {
    const span = document.createElement("span");
    span.className = "legend__item";
    span.textContent = name;
    return span;
  }));
}

/** 送って、返ってきた画面を描く。断られたら理由をそのまま出す。 */
async function send(path, body) {
  try {
    const next = await api.post(path, body || {});
    render(next);
    if (next.message) toast(next.message, "ok");
    return next;
  } catch (err) {
    // 422 も本文に画面ぜんぶが入っている。押した拍子に図が消えない
    if (err.body && err.body.shelves) render(err.body);
    toastError(err);
    return null;
  }
}

/**
 * 押した置き場の中身を出す。
 *
 * **押したのに何も起きないように見えるのが一番わるい。** 中身は別の面に
 * あるので、こちらから開く ── 「ここに何がある?」と押した以上、答えは
 * 押した場所のそばに出す。断られたときは開かない(出すものが無い)。
 */
async function showContents(name) {
  if (await send("/api/layout/select", { name })
      && el.layoutTabs) tabs.select(el.layoutTabs, "contents");
}

/* ================================================================
   ドラッグ

   ポインタ座標 → 図の論理座標に直して送るだけ。掴んだ位置との差を
   保つのは、掴んだ点が箱の左上へ飛ぶのを防ぐため(ずれた分だけ
   置き場が動いてしまう)。
   ================================================================ */
let dragging = null;

/** 画面座標を図の論理座標へ。`viewBox` の縮尺はブラウザに聞く。 */
function toPlan(event) {
  const rect = el.map.getBoundingClientRect();
  const [minX, minY, width, height] = el.map.getAttribute("viewBox")
    .split(/\s+/).map(Number);
  return {
    x: minX + (event.clientX - rect.left) / rect.width * width,
    y: minY + (event.clientY - rect.top) / rect.height * height,
  };
}

function onPointerDown(event) {
  const group = event.target.closest(".shelf");
  if (!group) return;
  const name = group.dataset.name;
  const isBase = group.classList.contains("shelf--base");

  if (!state.editing) {
    // 見るだけ。押した置き場の中身を出す。
    // 拠点は「中身」を持たないので、見るだけのときは何もしない
    if (!isBase) showContents(name);
    return;
  }
  // **編集中は拠点も動かせる。** 距離=疲労度は拠点との差で決まるので、
  // 現場で拠点が動いたら図でも直せないと、以後のスコアがずっとずれる
  // (サーバの `plan.move_item` は最初から拠点も動かせる)
  const shelf = (isBase ? state.bases : state.shelves)
    .find((s) => s.name === name);
  if (!shelf) return;

  const point = toPlan(event);
  dragging = { name, isBase, dx: point.x - shelf.x, dy: point.y - shelf.y, moved: false };
  el.map.setPointerCapture(event.pointerId);
  event.preventDefault();
}

function onPointerMove(event) {
  if (!dragging) return;
  const point = toPlan(event);
  // 動かしている最中はその場で見せる。1歩ごとにサーバへ送ると、
  // 応答を待つあいだ箱が指から遅れる
  const group = el.map.querySelector(`.shelf[data-name="${CSS.escape(dragging.name)}"]`);
  if (!group) return;
  const rect = group.querySelector("rect");
  const text = group.querySelector("text");
  const x = point.x - dragging.dx;
  const y = point.y - dragging.dy;
  rect.setAttribute("x", x);
  rect.setAttribute("y", y);
  text.setAttribute("x", x + Number(rect.getAttribute("width")) / 2);
  text.setAttribute("y", y + Number(rect.getAttribute("height")) / 2);
  dragging.moved = true;
  dragging.last = { x, y };
}

function onPointerUp(event) {
  if (!dragging) return;
  const { name, isBase, moved, last } = dragging;
  dragging = null;
  el.map.releasePointerCapture?.(event.pointerId);
  if (!moved || !last) {
    // 動かさずに離したのは「押した」。中身を出す。
    // 拠点は中身を持たないので何もしない
    if (!isBase) showContents(name);
    return;
  }
  // 枠に収めるのはサーバの仕事。返ってきた座標で描き直す
  send("/api/layout/move", { name, x: last.x, y: last.y });
}

/* ================================================================ */
export function start(initial) {
  for (const id of ["map", "mapLegend", "basePoint", "searchWidth",
                    "searchLength", "search", "result",
                    "fromSelection", "handoffLabel",
                    "editToggle", "editState", "editBox", "dirtyWhy",
                    "newLabel", "addLabel", "removeLabel", "bgFile", "clearBg",
                    "save", "reset",
                    "materialsNote", "materialRows",
                    "unplacedCard", "unplacedNote", "unplacedList"]) {
    el[id] = document.getElementById(id);
  }
  el.layoutTabs = document.getElementById("layoutTabs");
  el.kindGroup = document.querySelector('.choose[aria-label="探す資材の種別"]');
  tabs.attachAll();
  dragging = null;      // 再入場のたびに真っさらから(`nav.js`)

  render(initial);

  el.basePoint.addEventListener("change", () =>
    send("/api/layout/base-point", { name: el.basePoint.value }));

  const doSearch = () => send("/api/layout/search", {
    kind: toggles.value(el.kindGroup, "kind") || "board",
    width: el.searchWidth.value,
    length: el.searchLength.value,
  });
  el.search.addEventListener("click", doSearch);
  for (const field of [el.searchWidth, el.searchLength]) {
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); doSearch(); }
    });
  }
  // 種別を変えただけでは探さない。幅が要るかどうかだけ切り替える
  toggles.attach(el.kindGroup, "kind", (kind) => {
    el.searchWidth.disabled = kind !== "board";
  });

  // 資材選択から渡ってきた資材を、まとめて光らせる(旧版 `btnMap`)。
  // **品目を画面から送らない。** 何を渡したかはサーバ側の作業状態が
  // 持っているので、押したことだけを伝える(同じ事実を2か所に置かない)
  el.fromSelection.addEventListener("click", () =>
    send("/api/layout/from-selection"));

  // --- 図 ---------------------------------------------------------
  el.map.addEventListener("pointerdown", onPointerDown);
  el.map.addEventListener("pointermove", onPointerMove);
  el.map.addEventListener("pointerup", onPointerUp);
  el.map.addEventListener("pointercancel", onPointerUp);

  // --- 配置編集 ---------------------------------------------------
  el.editToggle.addEventListener("click", () =>
    send("/api/layout/edit", { on: !state.editing }));
  el.addLabel.addEventListener("click", () => {
    send("/api/layout/add", { name: el.newLabel.value }).then((next) => {
      if (next) el.newLabel.value = "";
    });
  });
  el.removeLabel.addEventListener("click", () => {
    if (!state.selected) {
      toast("消す置き場を図から選んでください。", "warn");
      return;
    }
    send("/api/layout/remove", { name: state.selected });
  });

  el.bgFile.addEventListener("change", () => {
    const file = el.bgFile.files && el.bgFile.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      send("/api/layout/background", { image: String(reader.result) });
      el.bgFile.value = "";
    };
    reader.onerror = () => toast("画像を読めませんでした。", "warn");
    reader.readAsDataURL(file);
  });
  el.clearBg.addEventListener("click", () =>
    send("/api/layout/background", { image: "" }));

  el.save.addEventListener("click", () => send("/api/layout/save"));
  el.reset.addEventListener("click", () => {
    // 出荷時に戻すと編集した内容は消える。取り消せないので一度確かめる
    if (confirm("出荷時の配置に戻します。編集した内容は消えます。よろしいですか?")) {
      send("/api/layout/reset");
    }
  });
}
