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

import { api, tokenUrl } from "../api.js";
import { toast, toastError } from "../toast.js";
import * as tabs from "../tabs.js";
import * as toggles from "../toggles.js";
// `mapedit.js` は動的に読み込む(理由は `settings.js` が `master.js` を
// 動的に読み込んでいるのと同じ ── このファイル自身と版クエリを合わせ、
// 入れ替えたときに中身も必ず一緒に入れ替わるようにするため)。
// トップレベルで一度だけ確定させておけば、以後は今までどおり
// `mapedit.grip(...)` のように同期的に呼べる。
const VERSION_QUERY = new URL(import.meta.url).search;
const mapedit = await import(`../mapedit.js${VERSION_QUERY}`);

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
  // 編集をOFFにしたら複数選択も捨てる。**捨てないと、次にONにしたときも
  // 前回選んでいた箱の輪郭が残ったままになる**(現場の声)
  if (!next.editing) dragger?.clearSelection();

  const parts = [];
  // 背景 → 区画 → 置き場 → 拠点 の順。後に描いたものが前に来る
  if (next.background) {
    // **枠いっぱいに引き伸ばさない。** 写真の画角は図の枠と一致しないので、
    // ずらし量と倍率(`background_x/y/scale`)のとおりに置く
    const image = node("image", {
      // **ヘッダを付けられない読み込み**なので、トークンはURLに載せる。
      // 付け忘れると401で、図には枠だけが出て写真が出ない
      href: tokenUrl(next.background),
      preserveAspectRatio: "xMidYMid meet", opacity: 0.85,
    });
    mapedit.applyBackground(image, next);
    parts.push(image);
  }
  for (const area of next.areas) parts.push(box(area, "area"));
  for (const shelf of next.shelves) {
    const group = box(shelf, `shelf shelf--${shelf.state}`);
    if (shelf.name === next.selected) group.classList.add("is-selected");
    // 掴みしろは**編集中だけ**。出しっぱなしにすると、見るだけのときに
    // 「掴める」と読めてしまう
    if (next.editing) group.appendChild(mapedit.grip(shelf));
    parts.push(group);
  }
  for (const base of next.bases) {
    const group = box(base, "shelf shelf--base");
    if (next.editing) group.appendChild(mapedit.grip(base));
    parts.push(group);
  }
  el.map.replaceChildren(...parts);
  // 複数選択(Shift+クリック)の見た目を付け直す。図はまるごと
  // 作り直されるので、毎回付け直さないと消えたままになる
  dragger?.reapplySelection();

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
  el.searchWidth.value = next.width_text;
  el.searchLength.value = next.length_text;

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
  // 背景が無ければ合わせこむものが無い。**押せるのに何も起きない欄を作らない**
  el.bgPlaceBox.hidden = !next.background;
  el.bgX.value = next.background_x;
  el.bgY.value = next.background_y;
  el.bgScale.value = next.background_scale;
  el.editState.textContent = next.editing
    ? "編集中(ドラッグで動かせます。Shift+クリックでまとめて選び、"
      + "まとめて動かす・大きさを変えられます)" : "";
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

/**
 * 図の置き場を押した。**編集中は選ぶだけ**(消す対象を決める)。
 *
 * 編集中に中身の面まで開くと、動かす/消す対象を選ぼうとしただけの
 * 操作で面が切り替わる(現場の声:「配置編集中にクリックすると
 * 配置編集中の挙動を行わないで通常挙動を行う」)。選ぶこと自体は
 * `/api/layout/select` を叩く ── ハイライト(`state.selected`)と
 * 「消す置き場」の対象決めは、編集中でも要るため。ここが
 * `簡易在庫`(`inventory.js` の `onTapPosition`)と対になる。
 */
async function onTapShelf(name) {
  if (state && state.editing) {
    await send("/api/layout/select", { name });
    return;
  }
  await showContents(name);
}

/* ================================================================
   ドラッグ

   ポインタ座標 → 図の論理座標に直して送るだけ。掴んだ位置との差を
   保つのは、掴んだ点が箱の左上へ飛ぶのを防ぐため(ずれた分だけ
   置き場が動いてしまう)。
   ================================================================ */
// 図の縮尺。**背景の写真に合わせこむときに要る** ── 等倍のままだと
// 置き場1つが小さく、指で狙った場所へ置けない。拡大縮小そのものは
// `mapedit.attachZoom` が持つ(簡易在庫と共通の作法・共通の実装)
let dragger = null;
let zoomer = null;

/** 掴む手は `mapedit.js` が持つ。ここは**何を送るか**だけを決める。

    拠点も動かせる ── 距離=疲労度は拠点との差で決まるので、現場で拠点が
    動いたら図でも直せないと、以後のスコアがずっとずれる。 */
function startDragging() {
  dragger = mapedit.attach(el.map, {
    selector: ".shelf",
    editing: () => Boolean(state && state.editing),
    find: (name) => (state.shelves.find((s) => s.name === name)
                     || state.bases.find((s) => s.name === name) || null),
    onTap: (name, group) => {
      // 拠点は「中身」を持たないので、押しても何も出さない
      if (group && !group.classList.contains("shelf--base")) onTapShelf(name);
    },
    onMove: (name, x, y) => send("/api/layout/move", { name, x, y }),
    onResize: (name, w, h) => send("/api/layout/resize", { name, w, h }),
    // 見た目(点線の色)だけでは「選べたか分からない」という声があった。
    // 文字でも件数を出す
    onSelectionChange: (count) => {
      if (el.multiNote) {
        el.multiNote.textContent = count ? `${count}件選択中` : "選択なし";
      }
    },
  });
}

/* ================================================================ */
export function start(initial) {
  for (const id of ["map", "mapLegend", "basePoint", "searchWidth",
                    "searchLength", "search", "result",
                    "fromSelection", "handoffLabel",
                    "editToggle", "editState", "editBox", "dirtyWhy", "multiNote",
                    "newLabel", "addLabel", "removeLabel", "bgFile", "clearBg",
                    "bgX", "bgY", "bgScale", "bgFit", "bgPlaceBox",
                    "mapWrap", "zoomIn", "zoomOut", "zoomNow",
                    "save", "reset",
                    "materialsNote", "materialRows",
                    "unplacedCard", "unplacedNote", "unplacedList"]) {
    el[id] = document.getElementById(id);
  }
  el.layoutTabs = document.getElementById("layoutTabs");
  tabs.attachAll();
  dragger?.reset();     // 再入場のたびに真っさらから(`nav.js`)

  render(initial);

  el.basePoint.addEventListener("change", () =>
    send("/api/layout/base-point", { name: el.basePoint.value }));

  // **種別は送らない。** 打った寸法で、ボードとアングルの両方を見る
  const doSearch = () => send("/api/layout/search", {
    width: el.searchWidth.value,
    length: el.searchLength.value,
  });
  el.search.addEventListener("click", doSearch);
  for (const field of [el.searchWidth, el.searchLength]) {
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); doSearch(); }
    });
  }
  // 資材選択から渡ってきた資材を、まとめて光らせる(旧版 `btnMap`)。
  // **品目を画面から送らない。** 何を渡したかはサーバ側の作業状態が
  // 持っているので、押したことだけを伝える(同じ事実を2か所に置かない)
  el.fromSelection.addEventListener("click", () =>
    send("/api/layout/from-selection"));

  // --- 図 ---------------------------------------------------------
  startDragging();
  zoomer = mapedit.attachZoom({
    wrap: el.mapWrap, zoomIn: el.zoomIn, zoomOut: el.zoomOut,
    zoomNow: el.zoomNow,
  });

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

  // 背景の合わせこみ。**数で入れる** ── 掴んで動かす形にすると箱を掴む
  // 操作とぶつかり、どちらが動いたのか分からなくなる
  const placeBg = () => send("/api/layout/background/place", {
    x: el.bgX.value, y: el.bgY.value, scale: el.bgScale.value,
  });
  for (const box of [el.bgX, el.bgY, el.bgScale]) {
    box.addEventListener("change", placeBg);
  }
  el.bgFit.addEventListener("click", () => send(
    "/api/layout/background/place", { x: 0, y: 0, scale: 1 }));

  el.save.addEventListener("click", () => send("/api/layout/save"));
  el.reset.addEventListener("click", () => {
    // 出荷時に戻すと編集した内容は消える。取り消せないので一度確かめる
    if (confirm("出荷時の配置に戻します。編集した内容は消えます。よろしいですか?")) {
      send("/api/layout/reset");
    }
  });
}
