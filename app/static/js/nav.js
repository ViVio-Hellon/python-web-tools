/*
  nav.js — 画面を移っても外枠を作り直さない

  【SPA にはしない。それでも再読込はしない】
  `docs/設計.md` §2 の判断は変えない ── ルータも、画面側の状態管理も、
  ビルド工程も入れない。入れるのは1つだけ:

      行き先のHTMLをサーバから貰って、**中身だけ差し替える**。

  ・画面の中身を決めるのは今まで通りサーバ(判断はPython、§1)
  ・画面が新しく持つ状態は無い。URL とサーバの応答が唯一の出どころ(§5)
  ・npm も webpack も要らない。`pip install -r requirements.txt` のまま

  【なぜ差し替えるのか】
  作業はロット検索 → 資材選択 → 倉庫連携 …と行き来する。**そのたびに
  白い瞬間が挟まる**と、
    ・出したばかりのトーストが消える
    ・接続断の帯が一瞬消えてまた出る
    ・押した場所が作り直されて、指の位置がずれる
  速さの話ではなく、**同じ画面の中に居るという感覚**が切れることが
  問題になる(設計指針 §1.4 作業記憶)。外枠を保てば切れない。

  【差し替えるもの / 残すもの】
      差し替える … <title> / 画面ごとの <style> / リボン / レール /
                    <main> / 画面ごとの <script>
      残す       … 接続断の帯・トースト・心拍(health.js)・app.js 本体
  残すものは「今この瞬間の状態」を持っていて、行き先とは関係が無い。

  【失敗したら普通に移る】
  取りに行けない・形が違う・古いブラウザ ── どの場合も
  `location.href` へ落とす。**移れなくなることは無い。**
*/

import * as screen from "./screen.js";

/** 差し替えたい要素。この順に上から入れ替える。 */
const SWAP = ["header.ribbon", "nav.rail", "main#main"];

/** 画面ごとの `<script>` を入れておく箱(base.html)。 */
const SCRIPT_BOX = "#pagescripts";

let seq = 0;                       // 何度目の移動か。古い応答は捨てる
let page = new AbortController();  // この画面のあいだだけ有効
let afterSwap = () => {};          // 外枠を繋ぎ直す係(app.js が渡す)

/**
 * その画面のあいだだけ有効な合図。
 *
 * `<main>` の中に付けた listener は差し替えと一緒に消えるが、
 * **`document` / `window` に付けたものとタイマーは消えない。**
 * 消し忘れると、画面を出たあとも動き続けて二重に効く。
 *
 *     document.addEventListener("click", fn, { signal: pageSignal() });
 */
export function pageSignal() {
  return page.signal;
}

/** 画面を出るときに1度だけ呼ばれる後始末。タイマーはここで止める。 */
export function onLeave(fn) {
  page.signal.addEventListener("abort", fn, { once: true });
}

/** この場所を差し替えで開けるか。開けないものは普通の遷移に任せる。 */
function internal(url) {
  return url.origin === location.origin
      && !url.pathname.startsWith("/api/")
      && !url.pathname.startsWith("/report/")
      && !url.pathname.startsWith("/static/");
}

/**
 * `<a>` を押したときに差し替えで移る。
 *
 * 修飾キー付き(新しいタブで開く)・右クリック・`target` 付き・
 * ダウンロードは**触らない**。利用者がブラウザの機能として期待して
 * いるものを、こちらの都合で奪わない。
 */
function onClick(event) {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const a = event.target.closest("a[href]");
  if (!a || a.target || a.hasAttribute("download")) return;

  const url = new URL(a.href, location.href);
  if (!internal(url)) return;
  if (url.href === location.href) { event.preventDefault(); return; }

  event.preventDefault();
  // 押したことを**先に**見せる。応答を待ってから印を動かすと、
  // 速いときでも「効かなかったのか」と二度押しされる
  markRail(url.pathname);
  go(url.href);
}

/** レールの現在地を先に動かす。中身が来たらサーバの印で上書きされる。 */
function markRail(pathname) {
  for (const link of document.querySelectorAll(".rail a")) {
    const same = new URL(link.href, location.href).pathname === pathname;
    if (same) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

/**
 * 行き先を取ってきて差し替える。
 *
 * @param {string} href    行き先
 * @param {boolean} push   履歴に積むか(戻る/進むのときは積まない)
 */
export async function go(href, push = true) {
  const mine = ++seq;
  const main = document.querySelector("main#main");
  if (main) main.setAttribute("aria-busy", "true");

  let html;
  let landed = href;
  try {
    // **このタブの番号を付ける。** 付けないとサーバは「新しいタブが
    // 開いた」と読み、同じタブの中で画面を移るたびに番号が変わる
    // (`screen_lock.for_request`)。番号は base.html の <script> に
    // 入っていて、差し替えの対象ではないので替わらない
    const res = await fetch(href, {
      cache: "no-store",
      headers: { [screen.HEADER]: screen.id() },
    });
    // 権限が変わった・落ちた等。**普通の遷移に任せる**と、サーバが
    // 用意している案内(401の文言や起動待機画面)がそのまま出る
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    // 転送されていたら、着いた先を履歴に積む
    landed = res.url || href;
    html = await res.text();
  } catch (err) {
    location.href = href;
    return;
  }
  if (mine !== seq) return;             // もっと新しい移動が始まっている

  const next = new DOMParser().parseFromString(html, "text/html");
  if (!next.querySelector("main#main")) { location.href = href; return; }

  // ここから先は差し替え。**前の画面のタイマーと listener を先に切る**
  page.abort();
  page = new AbortController();

  if (push) history.pushState({ nav: 1 }, "", landed);
  document.title = next.title;
  swapStyles(next);
  for (const sel of SWAP) swap(sel, next);
  afterSwap();
  runScripts(next);
  focusMain();
}

/** 画面ごとの `<style>`。base.html は head に `<style>` を持たない。 */
function swapStyles(next) {
  for (const old of document.head.querySelectorAll("style")) old.remove();
  for (const style of next.head.querySelectorAll("style")) {
    document.head.appendChild(document.importNode(style, true));
  }
}

function swap(selector, next) {
  const here = document.querySelector(selector);
  const there = next.querySelector(selector);
  if (!here) return;
  // 別の文書から持ってくるので `importNode`。`cloneNode` だけだと
  // 持ち主の文書が違うまま入り、古いブラウザで例外になる
  if (there) here.replaceWith(document.importNode(there, true));
  else here.remove();
}

/**
 * 画面ごとの `<script>` を動かし直す。
 *
 * `innerHTML` で入れた `<script>` は動かない決まりなので、要素を
 * 作り直して入れる。**モジュールは一度読むと使い回される**ので、
 * `start()` は同じ画面へ何度戻ってきても正しく動く必要がある
 * (要素は毎回引き直す・`document` の listener は `pageSignal()` に繋ぐ)。
 */
function runScripts(next) {
  const box = document.querySelector(SCRIPT_BOX);
  const source = next.querySelector(SCRIPT_BOX);
  if (!box) return;
  box.replaceChildren();
  if (!source) return;
  for (const old of source.querySelectorAll("script")) {
    const script = document.createElement("script");
    for (const { name, value } of old.attributes) script.setAttribute(name, value);
    script.textContent = old.textContent;
    box.appendChild(script);
  }
}

/**
 * 差し替えたあとの焦点。
 *
 * `autofocus` は後から入れた要素には効かないので、ここで当てる。
 * 無ければ `<main>` 自身へ ── 読み上げに「新しい中身になった」ことが
 * 伝わり、キーボードの続きも本文の先頭からになる。
 */
function focusMain() {
  const main = document.querySelector("main#main");
  if (!main) return;
  main.removeAttribute("aria-busy");
  main.scrollTop = 0;
  window.scrollTo(0, 0);
  const wanted = main.querySelector("[autofocus]");
  if (wanted) { wanted.focus(); return; }
  main.tabIndex = -1;
  main.focus({ preventScroll: true });
}

/**
 * 差し替えを始める。History API が無ければ何もしない(普通の遷移)。
 *
 * @param {{onSwap?: () => void}} hooks
 *   `onSwap` … リボンなど**外枠を繋ぎ直す**係。app.js が渡す。
 *   ここから app.js を呼ばないのは、輪になった読み込みを作らないため。
 */
export function start(hooks = {}) {
  if (!window.history || !window.DOMParser) return;
  if (hooks.onSwap) afterSwap = hooks.onSwap;
  document.addEventListener("click", onClick);
  // 戻る/進む。履歴に積んだのはこちらなので、同じ道で戻す
  window.addEventListener("popstate", () => {
    markRail(location.pathname);
    go(location.href, false);
  });
}

/**
 * 帯とレールだけを取り直す。**画面の中身と入力は触らない。**
 *
 * 帯のモード切替は「いま持っている権限」で作られるが、作られるのは
 * 画面を出すときだけ。アクセス権限マスタに行を足しても、そのページに
 * 留まっているかぎり帯は古いままで、**アプリを開き直すまでモードを
 * 切り替えられない**ように見える(現場の声:「一回閉じたらいけました。
 * 閉じなくてもいいようにしたほうがいい」)。
 *
 * `<main>` を差し替えないので、打ちかけの入力も開いている面も残る。
 * 取りに行けなかったときは黙って諦める ── 帯が少し古いだけで、
 * 次の移動で直る。
 */
export async function refreshShell() {
  let next;
  try {
    // **このタブの番号を付ける。** `go()` と同じ理由で、付けないと
    // サーバは「新しいタブが開いた」と読み、新しい番号を振ってそちらを
    // 「いま使っている画面」にする ── **描き直したこのタブ自身が締め
    // 出され**、次の操作で「このタブは操作できません」が出ていた
    // (マスタ管理でアクセス権限を保存した直後に踏む)
    const res = await fetch(location.href, {
      cache: "no-store",
      headers: { [screen.HEADER]: screen.id() },
    });
    if (!res.ok) return false;
    next = new DOMParser().parseFromString(await res.text(), "text/html");
  } catch {
    return false;
  }
  if (!next.querySelector("header.ribbon")) return false;
  for (const sel of ["header.ribbon", "nav.rail"]) swap(sel, next);
  afterSwap();
  return true;
}
