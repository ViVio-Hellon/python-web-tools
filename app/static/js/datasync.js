/*
  datasync.js — マスタが変わったことを、開いている画面へすぐ伝える

  現場の声:「作業長でない端末でも送れるままですよ ── と思ったらタブ変えたら送れなくなった。
  毎回いうけどマスタ編集したらリアルタイムで読み直してください」

  サーバはマスタを直す・取り込むたびに**世代**を1つ進める(`master_state.py`)。
  画面は開いたときの世代(`window.APP.dataEpoch`)を覚えておき、違う世代を見たら
  `tool:data-changed` を出す。マスタで決まるところ(倉庫送信・作業長・作業ライン・
  蓋を待つか)はそれを聞いて、サーバから引き直して描き直す(打ちかけの入力は触らない)。

  世代を受け取る道:
    - `/api/health` の見張り(`health.js`。数秒おきと、タブや窓に戻ったとき)
    - マスタ管理で直した窓が、ほかのタブ・窓へ**すぐ**知らせる(`tellOthers`。BroadcastChannel)
*/

import * as nav from "./nav.js";

const EVENT = "tool:data-changed";

/** サーバから受け取ったマスタの世代を伝える。同じなら何もしない。 */
export function announce(epoch) {
  if (typeof epoch !== "number") return;
  if (epoch === window.APP.dataEpoch) return;
  window.APP.dataEpoch = epoch;
  window.dispatchEvent(new CustomEvent(EVENT, { detail: { epoch } }));
}

// ほかのタブ・窓との連絡口(同じアプリの画面どうし)。使えないブラウザでは見張りだけで追いつく
let channel = null;
try {
  channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("packaging-tool-data") : null;
  if (channel) {
    channel.onmessage = () => window.dispatchEvent(new CustomEvent(EVENT, { detail: {} }));
  }
} catch {
  channel = null;
}

/** マスタを直した。**この画面にも**すぐ知らせる(同じ設定画面のマスタ管理で直したとき)。ほかのタブ・窓へも。 */
export function changedHere() {
  window.dispatchEvent(new CustomEvent(EVENT, { detail: {} }));
  tellOthers();
}

/** マスタを直した。ほかのタブ・窓へすぐ知らせる(世代は次の見張りでそろう)。 */
export function tellOthers() {
  try {
    if (channel) channel.postMessage({ changed: Date.now() });
  } catch {
    // 知らせられなくても、見張りで追いつく
  }
}

/** マスタが変わったら `fn()` を呼ぶ。画面を移っても外さない(帯・レールのように、どの画面にもあるもの)。 */
export function always(fn) {
  window.addEventListener(EVENT, () => fn());
}

/** マスタが変わったら `fn()` を呼ぶ。**この画面を出ていくときに外す**(`authsync.onChange` と同じ)。 */
export function onChange(fn) {
  const listener = () => fn();
  window.addEventListener(EVENT, listener);
  nav.onLeave(() => window.removeEventListener(EVENT, listener));
}
