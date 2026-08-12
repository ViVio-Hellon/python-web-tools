/*
  jobs.js — いま動いているものを、1か所で見張る

  【なぜ1か所か】
  取り込み・書き戻しは数十秒から数分かかる。始めた画面(設定)を
  離れると、終わったのかどうかが分からなくなる ── 帯にも出したい。
  かといって帯と設定画面がそれぞれ `/api/jobs` を叩くと、**同じ事実を
  二か所で持つ**ことになり、片方だけ古い、が起こる(設計.md §1)。

  見張りはここだけ。欲しい人は `subscribe()` で受け取る。

  【叩く間隔】
      走っていない … 15秒。他の端末が始めたぶんに気づくためだけなので疎くてよい
      走っている   … 0.7秒。進捗は1件ごとに動くので、これより細かくしても読めない

  取りに行くのを止めない ── 帯は全画面に出るので、画面を移っても
  見張りは続く。`nav.js` の差し替えでは止めない(止めたら、移った
  先で「何も動いていない」ように見える)。
*/

import { api } from "./api.js";

const IDLE_MS = 15000;
const BUSY_MS = 700;

/** いちばん新しく受け取った状態。**事実の置き場所はここ1つ。** */
let state = { running: [], recent: [], busy: false };
/** 1度でも本物を掴んだか。掴む前の空を「何も動いていない」と見せない */
let loaded = false;
let timer = null;
let started = false;
const listeners = new Set();

/** いま分かっていること。まだ1度も取れていなければ空の形を返す。 */
export function current() {
  return state;
}

/**
 * サーバが画面に埋めてきた状態を、そのまま最初の1つとして採る。
 *
 * 設定画面は最初の描画に必要なので**テンプレートに同じものが載っている**。
 * それを捨てて取り直すと、開いた瞬間だけ空になる ── 載っているほうが
 * 新しいので、こちらへ写して1つにまとめる。
 */
export function seed(first) {
  if (!first) return;
  state = first;
  loaded = true;
  notify();
}

function notify() {
  for (const fn of [...listeners]) fn(state);
}

/**
 * 状態が変わるたび呼ばれる。**すでに掴んでいれば登録した時点で1回呼ぶ**
 * ── 次の見張りまで「何も無い」を見せないため。
 *
 * @param {(state: object) => void} fn
 * @returns {() => void} やめるときに呼ぶ
 */
export function subscribe(fn) {
  listeners.add(fn);
  if (loaded) fn(state);
  return () => listeners.delete(fn);
}

/** 押した直後など、次の見張りを待たずに取りに行く。 */
export function refresh() {
  schedule(0);
}

function schedule(ms) {
  clearTimeout(timer);
  timer = setTimeout(tick, ms);
}

async function tick() {
  try {
    state = await api.get("/api/jobs");
    loaded = true;
    notify();
    schedule(state.busy ? BUSY_MS : IDLE_MS);
  } catch {
    // 見張りが失敗したことは接続断の帯が知らせる(`health.js`)。
    // ここで毎回トーストを出すと、切れているあいだ画面が埋まる
    schedule(IDLE_MS);
  }
}

/** 見張りを始める。2回呼んでも1本しか走らない。 */
export function start() {
  if (started) return;
  started = true;
  schedule(200);
}
