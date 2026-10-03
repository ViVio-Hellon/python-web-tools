/*
  authsync.js — 管理者認証が変わったことを、開いている画面へすぐ伝える

  現場の声:「パスワード認証してもマスタ編集がすぐできない。タブを切り替えて
  戻ってもまだ無い。認証系はすぐにどこにでも反映させないと分からない」。

  サーバは認証の状態が変わるたびに**世代**を1つ進める(`auth_state.py`)。
  画面は開いたときの世代(`window.APP.authEpoch`)を覚えておき、違う世代を
  見たら `tool:auth-changed` を出す。認証に関わるところ(マスタ管理の
  「直せる/直せない」、資材選択の実績の保存 など)はそれを聞いて描き直す。

  世代を受け取る道は2つ:
    - 認証した画面: 認証の応答(その場で)
    - ほかの画面: `/api/health` の見張り(`health.js`)
*/

import * as nav from "./nav.js";

const EVENT = "tool:auth-changed";

/** サーバから受け取った認証の状態を伝える。世代が同じなら何もしない。 */
export function announce(auth) {
  if (!auth || typeof auth.epoch !== "number") return;
  if (auth.epoch === window.APP.authEpoch) return;
  window.APP.authEpoch = auth.epoch;
  window.dispatchEvent(new CustomEvent(EVENT, { detail: auth }));
}

/**
 * 認証が変わったら `fn(auth)` を呼ぶ。**この画面を出ていくときに外す**
 * (画面を移っても window は同じなので、外さないと前の画面の分が残る)。
 */
export function onChange(fn) {
  const listener = (event) => fn(event.detail);
  window.addEventListener(EVENT, listener);
  nav.onLeave(() => window.removeEventListener(EVENT, listener));
}
