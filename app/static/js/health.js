/*
  health.js — 生存監視 (基盤仕様書 2.9)

  ブラウザ画面が開いていることと、バックエンドが動いていることは別。
  切れたら**画面に明示して**再接続の手段を出す。
  黙って古い表示を出し続けるほうが危険。
*/

import { api } from "./api.js";
import * as screen from "./screen.js";

// 何回続けて失敗したら「切れた」と見なすか。
// 1回の取りこぼしで赤帯を出すと、かえって信用されなくなる
const MISSES_BEFORE_OFFLINE = 2;

let misses = 0;
let banner = null;

function setOffline(offline) {
  if (!banner) banner = document.getElementById("offline");
  if (banner) banner.hidden = !offline;
}

async function beat() {
  try {
    const body = await api.get("/api/health");
    if (misses >= MISSES_BEFORE_OFFLINE) setOffline(false);
    misses = 0;
    /*
      **押す前に、譲ったことを見せる。**

      2枚目のタブを開くと、こちらは操作できなくなる
      (`packaging_tool/screen_lock.py`)。押したときにも断りは返るが、
      それだけでは**押すまで分からない** ── 古いLotを出したまま待って
      いて、押した1回目が空振りになる。

      向こうのタブが閉じれば戻ってくる(閉じるとき番号を手放す)ので、
      そのときは覆いを引っ込める。
    */
    if (body && body.screen_ok === false) screen.showTaken();
    else if (body && body.screen_ok === true) screen.clearTaken();
  } catch {
    if (++misses === MISSES_BEFORE_OFFLINE) setOffline(true);
  }
}

/* ================================================================
   こちらが生きていることを伝える (基盤仕様書 2.8「自動終了」)

   **窓が無いアプリなので、タブを閉じたら終わったつもりになる。**
   ところが Python は動いたままで、次の起動が「すでに起動しています」
   と判定し、入れ替えた新しい版がいつまでも動かない。

   そこで、開いているあいだは心拍を送る。閉じたことは `sendBeacon` で
   即座に伝える ── 閉じる瞬間の `fetch` は破棄されることがあるが、
   `sendBeacon` はブラウザが送りきってくれる。
   ================================================================ */
function alive(body) {
  // **閉じたことと一緒に、このタブの番号も渡す。** 受け取った側は
  // 「いま使っている画面」を手放すので、残ったタブが読み込み直さずに
  // 操作へ戻れる。`sendBeacon` はヘッダを付けられないので本文に入れる
  const payload = JSON.stringify({ ...(body || {}), screen_id: screen.id() });
  // 閉じる瞬間は `sendBeacon`。**それ以外も同じ口**へ送る
  if (body && body.leaving && navigator.sendBeacon) {
    navigator.sendBeacon("/api/alive",
      new Blob([payload], { type: "application/json" }));
    return;
  }
  // **`keepalive` は付けない。** 閉じる瞬間は上の `sendBeacon` が担うので
  // ここには要らない。
  // **返ってきた中身は読み捨てる。** 読まないと本体の流れが開いたままで、
  // ブラウザは「まだ終わっていない要求」として接続を抱え続ける
  // (中身に用は無いが、閉じるために1度触る)
  fetch("/api/alive", {
    method: "POST", cache: "no-store",
    headers: { "Content-Type": "application/json" }, body: payload,
  }).then((res) => res.text())
    .catch(() => { /* 届かなくても画面は続く。次の心拍で取り戻す */ });
}

function startAlive() {
  const period = window.APP.alivePollMs || 20000;
  alive();
  setInterval(alive, period);

  // 閉じた/隠れた。`pagehide` は再読込でも飛ぶが、**戻ってくれば
  // 次の心拍で取り消される**(サーバ側が猶予を持っている)
  window.addEventListener("pagehide", () => alive({ leaving: true }));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") alive();
  });
}

export function startHeartbeat() {
  const period = window.APP.healthPollMs || 15000;
  beat();
  setInterval(beat, period);
  startAlive();

  const reconnect = document.getElementById("reconnect");
  if (reconnect) reconnect.addEventListener("click", () => { misses = 0; beat(); });
}
