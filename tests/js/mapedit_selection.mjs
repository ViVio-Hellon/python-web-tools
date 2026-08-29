/*
  `mapedit.js` の複数選択(`.is-multi`)だけを、Nodeで実際に動かして確かめる。

  【なぜブラウザを立てずにここでやるか】
  この不具合は**画面側にしかない**。サーバの状態を見る Python の試験では
  何度やっても素通りする(実際、塗りだけを見ていて枠の残りを3度見逃した)。
  かといってブラウザ試験の仕組みはこのプロジェクトに無く、npm も持たない
  方針なので、`mapedit.js` が触るぶんだけのごく小さな DOM を手で用意して、
  モジュールをそのまま読み込んで動かす。

  ここで確かめるのは「掴む/押す」に対して**選択集合がどう変わるか**だけ。
  座標計算や実際の描画はブラウザの仕事なので見ない。
*/

const SELECTOR = ".shelf";

/** `classList` / `dataset` / `closest` だけを持つ、ごく小さな箱。 */
function makeBox(name) {
  const classes = new Set([SELECTOR.slice(1)]);
  const box = {
    dataset: { name },
    classList: {
      toggle(cls, on) { if (on) classes.add(cls); else classes.delete(cls); },
      contains(cls) { return classes.has(cls); },
    },
    has(cls) { return classes.has(cls); },
  };
  // 箱そのものを押した場合。`closest` は自分を返す
  box.closest = (sel) => (sel === SELECTOR ? box : null);
  return box;
}

function makeSvg(boxes) {
  const handlers = {};
  return {
    handlers,
    addEventListener(type, fn) { handlers[type] = fn; },
    querySelectorAll() { return boxes; },
    querySelector() { return null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 100, height: 100 }; },
    getAttribute() { return "0 0 100 100"; },
    setPointerCapture() {},
    releasePointerCapture() {},
  };
}

/** 箱の外(SVGの地の部分)を押したときの `event.target`。 */
const VOID_TARGET = { closest: () => null, classList: { contains: () => false } };

function press(svg, target, { shift = false } = {}) {
  svg.handlers.pointerdown({
    target,
    shiftKey: shift,
    clientX: 10,
    clientY: 10,
    pointerId: 1,
    preventDefault() {},
  });
}

const results = [];
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  results.push({ label, ok, got, want });
}

const { attach } = await import("../../app/static/js/mapedit.js");

function setup() {
  const boxes = [makeBox("A"), makeBox("B"), makeBox("C")];
  const svg = makeSvg(boxes);
  let counted = -1;
  attach(svg, {
    selector: SELECTOR,
    editing: () => true,
    find: () => ({ x: 0, y: 0, w: 10, h: 10 }),
    onSelectionChange: (n) => { counted = n; },
  });
  const marked = () => boxes.filter((b) => b.has("is-multi")).map((b) => b.dataset.name);
  return { boxes, svg, marked, count: () => counted };
}

// 1) Shiftを押さずに掴んだだけでは、選択中の枠を付けない。
//    付けていたため、1つ動かすたびに枠が残り、別の箱を掴むまで消えなかった
{
  const t = setup();
  press(t.svg, t.boxes[0]);
  check("掴んだだけでは枠が付かない", t.marked(), []);
}

// 2) 何も無いところを押したら、Shiftで選んだ枠を捨てる。
//    以前は何もしていなかったため、掴む以外に解除する手段が無かった
{
  const t = setup();
  press(t.svg, t.boxes[0], { shift: true });
  press(t.svg, t.boxes[1], { shift: true });
  check("Shiftで2つ選べる", t.marked(), ["A", "B"]);
  press(t.svg, VOID_TARGET);
  check("空き領域を押すと解除される", t.marked(), []);
  check("件数の表示も0になる", t.count(), 0);
}

// 3) 選んでいない箱を掴んだら、前の選択は捨てる(まとめて動かすつもりが
//    ないのに、前の選択が紛れ込まないように)
{
  const t = setup();
  press(t.svg, t.boxes[0], { shift: true });
  press(t.svg, t.boxes[2]);
  check("別の箱を掴むと前の選択は消える", t.marked(), []);
}

// 4) **まとめて動かす機能は壊さない。** 選んでいる箱を掴んだときは、
//    選択をそのまま保つ(これが複数選択の存在理由)
{
  const t = setup();
  press(t.svg, t.boxes[0], { shift: true });
  press(t.svg, t.boxes[1], { shift: true });
  press(t.svg, t.boxes[0]);
  check("選んだ箱を掴めば選択は保たれる", t.marked(), ["A", "B"]);
}

// 5) 編集中でなければ、空き領域を押しても何も起きない(押しただけで
//    状態が動くと、見るだけのつもりの操作で変化する)
{
  const boxes = [makeBox("A")];
  const svg = makeSvg(boxes);
  let editing = true;
  attach(svg, {
    selector: SELECTOR,
    editing: () => editing,
    find: () => ({ x: 0, y: 0, w: 10, h: 10 }),
  });
  press(svg, boxes[0], { shift: true });
  editing = false;
  press(svg, VOID_TARGET);
  check("編集中でなければ空き領域を押しても触らない",
        boxes.filter((b) => b.has("is-multi")).map((b) => b.dataset.name), ["A"]);
}

for (const r of results) {
  console.log(`${r.ok ? "ok  " : "FAIL"} ${r.label}`
    + (r.ok ? "" : `  got=${JSON.stringify(r.got)} want=${JSON.stringify(r.want)}`));
}
process.exit(results.every((r) => r.ok) ? 0 : 1);
