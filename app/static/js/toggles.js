/*
  toggles.js — 択一の見た目を1か所にまとめる

  選択カード(`.choice`)とセグメント(`.seg__btn`)は、どちらも
  **「いくつかの中から1つ」**を表す部品です。押されたことを
  `aria-pressed` で示し、実際にどうなるかはサーバが返す画面で決まります。

  ここがするのは2つだけ:
    1. 同じ組の中で、押されたものだけを `aria-pressed="true"` にする
    2. 呼び出し側へ「これが選ばれた」と渡す

  **押せるかどうか・選んでよいかどうかは判断しません。**
  それはサーバの仕事で(設計.md §1)、ここは写すだけです。
*/

/**
 * 組の中でいま選ばれている値。無ければ空文字。
 *
 * 組そのものが無い画面(その択一を出していないモード)でも空を返す。
 * ここで投げると、**呼び出し側の検索や読み込みごと落ちる** ──
 * 選択肢が出ていないなら「何も選ばれていない」でよく、既定は
 * 呼び出し側が持っている。
 */
export function value(group, attr) {
  const on = group && group.querySelector('[aria-pressed="true"]');
  return on ? (on.dataset[attr] || "") : "";
}

/** 見た目だけを合わせる。サーバが返した値を写すときに使う。 */
export function mark(group, attr, chosen) {
  if (!group) return;
  for (const btn of group.querySelectorAll("[aria-pressed]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset[attr] === chosen));
  }
}

/**
 * 1組の択一を動かす。
 *
 * @param {Element} group  `.choose` か `.seg`
 * @param {string} attr    値を持つ data 属性名(camelCase)
 * @param {(value:string)=>void} onPick  選ばれたときに呼ぶ
 */
export function attach(group, attr, onPick) {
  if (!group || group.dataset.toggleReady === "1") return;
  group.dataset.toggleReady = "1";
  group.addEventListener("click", (event) => {
    const btn = event.target.closest("[aria-pressed]");
    if (!btn || btn.disabled || !group.contains(btn)) return;
    // 同じものをもう一度押しても何もしない。**択一は必ず1つ選ばれている**
    if (btn.getAttribute("aria-pressed") === "true") return;
    mark(group, attr, btn.dataset[attr]);
    onPick(btn.dataset[attr]);
  });
}
