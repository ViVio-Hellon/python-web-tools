/*
  busy.js — 無言で待たせない

  【なぜ要るか】
  押してから応答が返るまでのあいだ、押した本人には**効いたのかどうかが
  分からない**。手元のPCなら速いが、共有フォルダの sqlite3 を開きに行く
  操作(取り込み・書き戻し・図面の取得)は数秒かかることがある。
  そのあいだ画面が何も言わないと、人はもう一度押す ── そして同じ処理が
  2回走る。

  【どう伝えるか】
  伝える場所は**押したものそのもの**。いちばん近くて、探さずに済む。

      押した直後   … 何も変えない(一瞬で終わるものに待機の姿を出すと、
                     画面が忙しなくちらつくだけになる)
      250ms 過ぎ   … そのボタンが待機の姿になる(記号→回る輪)。押せなくする
      1.2秒 過ぎ   … 経過秒を添える。**何秒待っているかが分かれば、
                     待てるかどうかを自分で決められる**

  【何を判断しないか】
  押してよいかどうか・失敗したかどうかは**一切見ません**。
  それはサーバが返すもので(設計.md §1)、ここは「まだ返ってきていない」
  という事実だけを写します。

  【使い方】
      import { press } from "../busy.js";
      button.addEventListener("click", () => press(button, () => send(...)));

  `press` は渡した処理の約束(Promise)をそのまま返すので、
  呼び出し側の書き方は変わりません。
*/

/** 待機の姿にするまでの猶予。これより速く終わるものには何も出さない。 */
const SHOW_AFTER_MS = 250;
/** 経過秒を出し始めるまで。 */
const COUNT_AFTER_MS = 1200;

function mmss(ms) {
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}秒`;
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

/**
 * 押したものを待機の姿にする。**終わったら返ってきた関数を必ず呼ぶこと。**
 *
 * ボタン(`<button>` 等、`disabled` を持てる要素)と、行(`<tr>`。
 * `data-row-action` を付けた押せる行)の両方を面倒みる。`<tr>` は
 * `disabled` プロパティを持たない(HTML標準として効かない)ため、
 * 見た目と押せなくする方法をボタンとは分けている(CSS側は
 * `components.css` の「待機」セクション参照)。
 *
 * @param {HTMLElement} target
 * @returns {() => void} 元の姿へ戻す
 */
export function mark(target) {
  if (!target) return () => {};
  const isRow = target.tagName === "TR";

  const started = performance.now();
  const wasDisabled = target.disabled;
  let clock = null;
  let elapsed = null;
  let cell = null;

  const show = setTimeout(() => {
    target.dataset.busy = "1";
    if (isRow) {
      // `<tr>` に `disabled` は効かない。**もう一度押せてしまうと
      // 二重に送ってしまう**ので、クリックそのものを止める
      target.setAttribute("aria-busy", "true");
      target.style.pointerEvents = "none";
    } else {
      target.disabled = true;
    }
  }, SHOW_AFTER_MS);

  const count = setTimeout(() => {
    elapsed = document.createElement("span");
    elapsed.className = "elapsed";
    // 読み上げには出さない。1秒ごとに読み上げられると本文が追えなくなる
    elapsed.setAttribute("aria-hidden", "true");
    if (isRow) {
      // 行の最後のセルに添える。`<tr>` の直下に置くと表の構造から
      // はみ出し、レイアウトが崩れる
      cell = target.lastElementChild;
      (cell || target).appendChild(elapsed);
    } else {
      target.appendChild(elapsed);
    }
    const tick = () => { elapsed.textContent = mmss(performance.now() - started); };
    tick();
    clock = setInterval(tick, 1000);
  }, COUNT_AFTER_MS);

  return () => {
    clearTimeout(show);
    clearTimeout(count);
    if (clock) clearInterval(clock);
    if (elapsed) elapsed.remove();
    delete target.dataset.busy;
    if (isRow) {
      target.removeAttribute("aria-busy");
      target.style.pointerEvents = "";
    } else {
      // **元が押せなかったものを押せるようにしない。** 押せるかどうかは
      // サーバが返した画面が決めるので、こちらは元へ戻すだけ
      target.disabled = wasDisabled;
    }
  };
}

/**
 * ボタンを押している間の姿を面倒みる。二度押しは黙って捨てる
 * (**同じ処理を2回走らせない**)。
 *
 * @param {HTMLButtonElement} button 押されたボタン
 * @param {() => Promise<any>} work  実際の処理
 * @returns {Promise<any>} `work` の結果をそのまま返す
 */
export function press(button, work) {
  if (!button) return Promise.resolve(work());
  if (button.dataset.busy === "1") return Promise.resolve(undefined);
  const done = mark(button);
  let result;
  try {
    result = work();
  } catch (err) {
    done();
    throw err;
  }
  return Promise.resolve(result).then(
    (value) => { done(); return value; },
    (err) => { done(); throw err; });
}

/**
 * 画面の中の「押したら送るボタン」をまとめて面倒みる。
 *
 * すでに `click` を持っているボタンには後から被せられないので、
 * **`api.js` を通る送信そのものを捕まえて**、いま押されたボタンを
 * 待機の姿にする。個々の画面を書き換えずに全部へ効かせるための道。
 */
let active = null;

/** いま押されているボタンを覚えておく(捕捉は `document` の click)。

    `.btn` / `.choice` / `.seg__btn` はクリックで送信する部品の作法。
    `[data-row-action]` は、押せるボタンではなく**行そのもの**が
    押せる作り(例: ロット情報の引当行 ── クリックでサーバへ取りに
    行くが、見た目はボタンではなく `<tr>`)。これが漏れていると、
    「押したものが待機の姿にならない」まま通信することになり、
    ここで面倒を見ている待機表示が効かない(現場の声:「どの処理でも
    待ちがあるならプログレス出してください」)。 */
export function watchClicks(scope = document) {
  scope.addEventListener("click", (event) => {
    const btn = event.target.closest(
      ".btn, .choice, .seg__btn, [data-row-action]");
    active = btn && !btn.disabled ? btn : null;
    // 同じ押下の中でしか使わない。次のtickで捨てる
    setTimeout(() => { active = null; }, 0);
  }, true);
}

/** 直前に押されたボタン。`api.js` が送信を始めるときに参照する。 */
export function pressed() {
  return active;
}
