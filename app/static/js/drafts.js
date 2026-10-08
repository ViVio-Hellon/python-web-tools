/*
  drafts.js — 打ちかけ(送る・登録する・保存する前の入力)を、止まっても失わない

  【なぜ要るのか】
  ツールが外から止められる(業務ツール統合ランチャーの［ツール停止］・`stop.bat`・
  ブラウザが落ちた・PC の電源)と、送る前のコメントや打ちかけの受け入れが、
  画面ごと送れなくなっていた。統合ツールは「止める前に画面へ打ちかけを置いて
  と頼む」が、ここでは**打つたびにこの端末へ置いておく**。止まる前に頼んで待つ
  必要が無く、強制で止められても、ブラウザが落ちても残る。

  【どう戻すか】
  欄を出すとき(`bind`)に、置いてある打ちかけがあれば戻して知らせる。
  **欄のもとの値が、打ちかけを置いたときと同じときだけ戻す** ── その間に
  別の端末が直した行へ、古い打ちかけを被せない(`base` が違えば捨てる)。

  【いつ消すか】
  - 送った・登録した・保存した(`done`)
  - 打ち直してもとの値に戻した(打ちかけが無くなった)
  - 本人が「やめる」「入力を消す」を選んだ(`done`)
  - 置いてから `MAX_AGE_MS` 過ぎた

  置き場所はこの端末のブラウザ(`localStorage`)。ほかの端末には行かない。
  パスワードの欄は**置かない**。
*/
import { toast } from "./toast.js";

const PREFIX = "pt.draft.";
const MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;

const bound = new WeakMap();      // 欄 → {key, base, label}
let restoredNames = [];
let restoreTimer = 0;

function storage() {
  try { return window.localStorage; } catch { return null; }
}

function read(key) {
  const store = storage();
  if (!store) return null;
  try {
    const raw = JSON.parse(store.getItem(PREFIX + key) || "null");
    if (!raw || typeof raw.value !== "string") return null;
    if (Date.now() - Number(raw.at || 0) > MAX_AGE_MS) { store.removeItem(PREFIX + key); return null; }
    return raw;
  } catch { return null; }
}

function write(key, value, base) {
  const store = storage();
  if (!store) return;
  try {
    if (value === base) store.removeItem(PREFIX + key);
    else store.setItem(PREFIX + key, JSON.stringify({ value, base, at: Date.now() }));
  } catch { /* 満杯・使えない。打ちかけを置けないだけで、画面は動かす */ }
}

function forget(key) {
  const store = storage();
  if (!store) return;
  try { store.removeItem(PREFIX + key); } catch { /* 同上 */ }
}

/** 戻したことを1回にまとめて知らせる(欄ごとに出すとうるさい)。 */
function announce(label) {
  if (label && !restoredNames.includes(label)) restoredNames.push(label);
  clearTimeout(restoreTimer);
  restoreTimer = setTimeout(() => {
    const names = restoredNames;
    restoredNames = [];
    if (!names.length) return;
    toast(`前に打ちかけていた内容を戻しました(${names.join("・")})。まだ送っていません`, "info");
  }, 200);
}

/**
 * 欄を見張り始める。`key` はその欄の打ちかけの名前(どの行の、どの欄か)。
 * 置いてある打ちかけがあれば戻す。戻したら真。
 */
export function bind(input, key, label = "") {
  if (!input || !key || input.type === "password") return false;
  const base = input.value;
  bound.set(input, { key, base, label });
  const saved = read(key);
  if (!saved) return false;
  if (saved.base !== base || saved.value === base) { forget(key); return false; }
  input.value = saved.value;
  // 欄の値で表示や押せる/押せないを決めている画面のために知らせる
  input.dispatchEvent(new Event("input", { bubbles: true }));
  announce(label);
  return true;
}

/** 送った・保存した・やめた。打ちかけを消し、いまの値をもとの値にする。 */
export function done(...inputs) {
  for (const input of inputs) {
    const info = input && bound.get(input);
    if (!info) continue;
    forget(info.key);
    info.base = input.value;
  }
}

/** 見張りをやめる(欄を作り直す前)。打ちかけは残す。 */
export function unbind(...inputs) {
  for (const input of inputs) if (input) bound.delete(input);
}

/** いま打ちかけがある欄か(試験・画面の表示用)。 */
export function pending(input) {
  const info = input && bound.get(input);
  return Boolean(info && read(info.key));
}

// 打つたびに置く。**止まる直前ではなく、いつも**置いておく
document.addEventListener("input", (event) => {
  const info = bound.get(event.target);
  if (info) write(info.key, event.target.value, info.base);
}, true);
