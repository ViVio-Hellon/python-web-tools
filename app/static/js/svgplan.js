/*
  svgplan.js — 描画計画を SVG にする

  **計算はしない。** サーバが `placement_render` / `angle_render` で作った
  計画(矩形・色・キャプション)を、そのまま SVG の要素に置き換えるだけ。
  tkinter版が同じ計画を `Canvas.create_rectangle()` に渡しているのと
  対になっている(設計書 §7.1)。

  ここに座標の計算を1つでも足すと、tkinter版と図が食い違ったときに
  「計画が違うのか、描き方が違うのか」を切り分けられなくなる。

  【拡大縮小はブラウザに任せる】
  サーバは固定の論理キャンバス(980×460 / 980×136)で計画を作り、
  `viewBox` で実寸へ写す。ウィンドウの大きさが変わってもサーバへ
  問い合わせない(tkinter版は `<Configure>` のたびに再計算していた)。

  【色】
  計画は `fill_token` / `line_token`(`--mat-lower-fill` など)を持つ。
  値は `tokens.css` が3つのテーマぶん持っていて、コントラストは
  `scripts/check_contrast.py` が機械検査している。
*/

const NS = "http://www.w3.org/2000/svg";

/** 塗りつぶしなしを `none` にする。空文字だと既定の黒になる。 */
function paint(token) {
  return token ? `var(${token})` : "none";
}

function el(name, attrs = {}) {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  return node;
}

/** 読み上げと吹き出しのための説明。図の中の小さな文字は当てにしない。 */
function title(node, text) {
  if (!text) return node;
  const t = el("title");
  t.textContent = text;
  node.appendChild(t);
  return node;
}

/**
 * 矩形の中央に載せる文字。改行は `<tspan>` に開く。
 *
 * 計画は「高さに応じて 2行 / 1行 / 幅のみ」を選んである(VBA踏襲)ので、
 * ここで行数を決め直さない。
 */
function caption(rect, color) {
  const lines = String(rect.caption || "").split("\n").filter(Boolean);
  if (!lines.length) return null;

  const size = rect.font_size || 7;
  const cx = rect.x + rect.width / 2;
  // 行の塊を縦中央に置く。1行目の基準線を半分ぶん上へ寄せる
  const top = rect.y + rect.height / 2 - (lines.length - 1) * size * 0.6;

  const text = el("text", {
    x: cx, y: top, "font-size": size, fill: paint(color),
    "text-anchor": "middle", "dominant-baseline": "middle",
  });
  lines.forEach((line, index) => {
    const span = el("tspan", { x: cx, dy: index === 0 ? 0 : size * 1.2 });
    span.textContent = line;
    text.appendChild(span);
  });
  return text;
}

/*
  カットの切り落とし部。

  計画は tkinter の都合で `|||`(丈カット)/ `===`(幅カット)という
  文字をハッチの代わりに入れている。SVG には本物の斜線ハッチが引けるので、
  文字ではなく模様で示す ── 塗りだけでは地と 3:1 に届かないぶんを
  ハッチと赤いカット線が補う、という前提でコントラスト検査の
  `DOCUMENTED_EXCEPTIONS` に登録してある。
*/
let hatchSeq = 0;

function hatchPattern(id, vertical) {
  const pattern = el("pattern", {
    id, width: 6, height: 6, patternUnits: "userSpaceOnUse",
    patternTransform: vertical ? "rotate(45)" : "rotate(-45)",
  });
  pattern.appendChild(el("rect", { width: 6, height: 6, fill: "var(--cut-zone)" }));
  pattern.appendChild(el("line", {
    x1: 0, y1: 0, x2: 0, y2: 6,
    stroke: "var(--cut-line)", "stroke-width": 1.4, opacity: 0.9,
  }));
  return pattern;
}

/** 切り落とし部かどうか(塗りが `--cut-zone`)。 */
const isCutZone = (rect) => rect.fill_token === "--cut-zone";

/**
 * ボード配置図。
 *
 * @param {object} plan   サーバが返した描画計画
 * @param {string} viewBox `"0 0 980 460"`
 * @returns {SVGElement}
 */
export function boardSvg(plan, viewBox) {
  const svg = el("svg", {
    viewBox, class: "plan", preserveAspectRatio: "xMidYMid meet",
    role: "img",
  });
  if (!plan) return svg;

  const defs = el("defs");
  svg.appendChild(defs);

  // 基準枠(パレット / 製品)。中の板より先に描く
  if (plan.border) {
    svg.appendChild(el("rect", {
      x: plan.border.x, y: plan.border.y,
      width: plan.border.width, height: plan.border.height,
      fill: "none", stroke: paint(plan.border.line_token), "stroke-width": 1.5,
      class: "plan__frame",
    }));
  }

  for (const rect of plan.boards || []) {
    const group = el("g", { class: "plan__board", "data-key": rect.key || "" });
    group.appendChild(el("rect", {
      x: rect.x, y: rect.y, width: rect.width, height: rect.height,
      fill: paint(rect.fill_token), stroke: paint(rect.line_token),
      "stroke-width": 1,
    }));
    const text = caption(rect, "--ink");
    if (text) group.appendChild(text);
    title(group, rect.title);
    svg.appendChild(group);
  }

  for (const rect of plan.cut_marks || []) {
    if (isCutZone(rect)) {
      // 「どちら向きに切るか」は計画の文字(`|||` / `===`)が持っている
      const vertical = String(rect.caption || "").includes("|");
      const id = `hatch-${(hatchSeq += 1)}`;
      defs.appendChild(hatchPattern(id, vertical));
      svg.appendChild(title(el("rect", {
        x: rect.x, y: rect.y, width: rect.width, height: rect.height,
        fill: `url(#${id})`, class: "plan__cutzone",
      }), "切り落とし"));
      continue;
    }
    // カット線そのもの
    svg.appendChild(el("rect", {
      x: rect.x, y: rect.y, width: rect.width, height: rect.height,
      fill: paint(rect.fill_token),
    }));
  }
  return svg;
}

/**
 * アングル配置図。パレットを横から見た断面。
 *
 * 描く順は計画の並びどおり(パレット → 脚 → 製品の端 → バー → カット)。
 * 後から描いたものが前に来るので、順を変えるとバーが脚に隠れる。
 */
export function angleSvg(plan, viewBox) {
  const svg = el("svg", {
    viewBox, class: "plan plan--angle", preserveAspectRatio: "xMidYMid meet",
    role: "img",
  });
  if (!plan) return svg;

  const box = (rect, extra = {}) => el("rect", {
    x: rect.x, y: rect.y, width: rect.width, height: rect.height,
    fill: paint(rect.fill_token),
    stroke: rect.line_token ? paint(rect.line_token) : null,
    "stroke-width": rect.line_token ? 1 : null,
    ...extra,
  });

  if (plan.pallet_bar) {
    svg.appendChild(title(box(plan.pallet_bar, { class: "plan__extent" }), "パレット"));
  }
  for (const leg of plan.legs || []) svg.appendChild(box(leg, { class: "plan__extent" }));
  for (const edge of plan.product_edges || []) {
    svg.appendChild(title(box(edge, { class: "plan__extent" }), "製品の端"));
  }

  for (const bar of plan.bars || []) {
    const group = el("g", { class: "plan__extent" });
    group.appendChild(box(bar));
    const text = caption(bar, bar.text_token || "--angle-bar-fg");
    if (text) {
      if (bar.bold) text.setAttribute("font-weight", "700");
      group.appendChild(text);
    }
    title(group, bar.caption);
    svg.appendChild(group);
  }

  for (const mark of plan.cut_marks || []) {
    const group = el("g");
    group.appendChild(box(mark));
    const text = caption(mark, mark.text_token || "--ink");
    if (text) group.appendChild(text);
    svg.appendChild(group);
  }

  for (const note of plan.notes || []) {
    const text = el("text", {
      x: note.x, y: note.y, fill: paint(note.color_token),
      "font-size": 11, "text-anchor": "middle", "dominant-baseline": "hanging",
      "font-weight": note.bold ? 700 : null,
    });
    text.textContent = note.text;
    svg.appendChild(text);
  }
  return svg;
}

// 図の広さを決めるときに数えるもの。**切り落とし部は数えない**
const EXTENT_SELECTOR = ".plan__board, .plan__frame, .plan__extent";

/**
 * はみ出したぶんまで見えるように `viewBox` を広げる。
 *
 * 計画は論理キャンバス(980×460)に収める前提で作られているが、
 * `placement_render` は**枠からはみ出したボードを補正しない**
 * (はみ出しをそのまま見せるのがVBA/tkinter版からの仕様)。
 * その結果、はみ出しの大きい配置では板が枠の外へ出る。
 *
 * tkinter の Canvas はそこで切り落としていた ── 「はみ出し37.4%」と
 * 数字では出ていても、**どれだけ出ているのかは図から読めなかった**。
 * SVG は `viewBox` を広げるだけで全体を写せる。
 *
 * 【切り落とし部を数に入れない理由】
 * 幅を1350mmカットするようなときの切り落とし部は板より大きくなる。
 * それに合わせて広げると、**本体の板が豆粒になって寸法が読めない**。
 * 捨てる部分は図の主役ではない(§設計指針・コントラスト検査の
 * `DOCUMENTED_EXCEPTIONS` も同じ前提)。境目は赤いカット線が示し、
 * 量は図の下の実測が数字で出す。
 *
 * 拡大率が変わるだけで座標は1つも触らない(計算はサーバのまま)。
 *
 * **DOMに入れてから呼ぶこと。** `getBBox()` は描画されていない要素だと
 * 0 を返す。
 */
export function fitToContent(svg, viewBox) {
  const base = String(viewBox || "").split(/\s+/).map(Number);
  if (base.length !== 4 || base.some(Number.isNaN)) return;

  let minX = base[0];
  let minY = base[1];
  let maxX = base[0] + base[2];
  let maxY = base[1] + base[3];
  const pad = 6;
  let grew = false;

  for (const node of svg.querySelectorAll(EXTENT_SELECTOR)) {
    let box;
    try {
      box = node.getBBox();
    } catch {
      continue;                               // 描画されていないと例外になる
    }
    if (!box || (!box.width && !box.height)) continue;
    if (box.x - pad < minX) { minX = box.x - pad; grew = true; }
    if (box.y - pad < minY) { minY = box.y - pad; grew = true; }
    if (box.x + box.width + pad > maxX) { maxX = box.x + box.width + pad; grew = true; }
    if (box.y + box.height + pad > maxY) { maxY = box.y + box.height + pad; grew = true; }
  }
  if (!grew) return;                          // 収まっている。tkinter版と同じ図

  svg.setAttribute("viewBox", `${minX} ${minY} ${maxX - minX} ${maxY - minY}`);
  svg.dataset.fitted = "1";
}

/**
 * 細くて文字が入らないボードの色凡例(VBA `DrawFillBoardLegend`)。
 *
 * 図の中に書けないから色で示している以上、**凡例が無いと読めない**。
 */
export function legendItems(plan) {
  const items = (plan && plan.legend) || [];
  return items.map(([width, token]) => {
    const span = document.createElement("span");
    span.className = "legend__item";
    const swatch = document.createElement("i");
    swatch.style.background = `var(${token})`;
    span.appendChild(swatch);
    span.appendChild(document.createTextNode(`${width}mm`));
    return span;
  });
}
