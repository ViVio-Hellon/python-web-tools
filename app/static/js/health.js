/*
  health.js — 生存監視 (基盤仕様書 2.9)

  ブラウザ画面が開いていることと、バックエンドが動いていることは別。
  切れたら**画面に明示して**再接続の手段を出す。
  黙って古い表示を出し続けるほうが危険。
*/

import { api } from "./api.js";
import * as nav from "./nav.js";
import * as screen from "./screen.js";
import * as unsaved from "./unsaved.js";

// 何回続けて失敗したら「切れた」と見なすか。
// 1回の取りこぼしで赤帯を出すと、かえって信用されなくなる
const MISSES_BEFORE_OFFLINE = 2;

let misses = 0;
let banner = null;

function setOffline(offline) {
  if (!banner) banner = document.getElementById("offline");
  if (banner) banner.hidden = !offline;
}

/*
  **使えるモードが変わったら、帯を描き直す。**

  帯のモード切替は画面を出すときにしか作られない。権限があとから
  増えても(起動時の取り込みが終わった・資材課が行を足した)、画面を
  移るまで「現場?」のまま ── 現場の声:「触ることで 現場・資材 に
  なった」。切り替えられるようになった時点で、触らなくても出す。

  描き直すのは帯とレールだけで、**画面の中身と打ちかけの入力は
  触らない**(`nav.refreshShell`)。同じ答えで何度も描き直さないよう、
  帯に書いてある「描いたときのモード」と比べる。
*/
let redrawing = false;

async function followModes(modes) {
  const end = document.querySelector(".ribbon__end");
  if (!end || redrawing) return;
  const drawn = (end.dataset.modes || "").split(",").filter(Boolean).sort().join(",");
  const now = [...modes].sort().join(",");
  if (drawn === now) return;
  redrawing = true;
  try {
    await nav.refreshShell();
  } finally {
    redrawing = false;
  }
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
    if (body && Array.isArray(body.modes)) followModes(body.modes);
    // 保存していない図。タブを閉じるときに聞くため(`unsaved.js`)
    if (body && body.unsaved && typeof body.unsaved === "object") {
      unsaved.fromServer(body.unsaved);
    }
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
  // 閉じる・裏に回る瞬間は `sendBeacon`。**このあとタブが固まっても**
  // ブラウザが送りきってくれる(`fetch` は捨てられることがある)
  if (body && (body.leaving || body.visible === false) && navigator.sendBeacon) {
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

/*
  **裏に回っても、落とさせない。**

  ブラウザは裏に回ったタブのタイマーを間引く(Chrome は5分を過ぎると
  1分に1回以下、Edge の「スリープ中のタブ」やメモリ節約では止める)。
  別のツールで、これで心拍が途切れて**使っている最中にセッションが
  終わった**ことがあった。そこで:

    裏に回る   … 「隠れます」を送る(`visible:false`)。サーバはこの画面を
                   心拍が途切れても生きているものとして数える
    固まる     … Chrome の `freeze`。同じく「隠れます」
    表に戻る   … 「戻りました」を送り(`resumed`)、接続もすぐ確かめる
    スリープ   … タイマーの間隔が大きく空いたら、PCが寝ていたとみなして
                   「戻りました」(止まっていた長さも添える)
    戻り方いろいろ … `resume`(固まりから)・`pageshow`(戻る/進むの控えから)・
                   `focus`・`online` でも「戻りました」
*/
let lastTick = Date.now();
let lastResume = 0;

function isVisible() {
  return document.visibilityState === "visible";
}

function comeBack(gapMs) {
  // 立て続けに来る(visibilitychange と focus と pageshow が同時に飛ぶ)ので間引く
  const now = Date.now();
  if (now - lastResume < 1000 && !gapMs) return;
  lastResume = now;
  lastTick = now;
  alive({ resumed: true, visible: isVisible(), gap_ms: gapMs || 0 });
  // **戻ってきたらすぐ確かめる。** 別のタブや窓で権限を直してから
  // 戻ったとき、15秒の見張りを待たずに帯が追いつく。スリープ明けで
  // 繋がっていなければ、ここで分かる
  misses = 0;
  beat();
}

function goAway() {
  alive({ visible: false });
}

function startAlive() {
  const period = window.APP.alivePollMs || 20000;
  alive({ visible: isVisible() });
  setInterval(() => {
    const now = Date.now();
    const gap = now - lastTick;
    lastTick = now;
    // 間隔が大きく空いた = PCが寝ていた(または固まっていた)。裏のタブは
    // 間引かれて1分ほど空くのが普通なので、それより十分長いときだけ
    if (gap > Math.max(period * 3, 120000)) {
      comeBack(gap);
      return;
    }
    alive({ visible: isVisible() });
  }, period);

  // 閉じた。`pagehide` は再読込でも飛ぶが、**戻ってくれば次の心拍で
  // 取り消される**(サーバ側が猶予を持っている)
  window.addEventListener("pagehide", (event) => {
    // 戻る/進むの控えに入るだけなら閉じたのではない。裏に回ったのと同じ
    if (event.persisted) goAway();
    else alive({ leaving: true });
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) comeBack(0);
  });
  document.addEventListener("visibilitychange", () => {
    if (isVisible()) comeBack(0);
    else goAway();
  });
  // Chrome のページ ライフサイクル。裏で固められる直前と、戻った直後
  document.addEventListener("freeze", goAway);
  document.addEventListener("resume", () => comeBack(0));
  window.addEventListener("focus", () => comeBack(0));
  window.addEventListener("online", () => comeBack(0));
}

export function startHeartbeat() {
  const period = window.APP.healthPollMs || 15000;
  beat();
  setInterval(beat, period);
  startAlive();

  const reconnect = document.getElementById("reconnect");
  if (reconnect) reconnect.addEventListener("click", () => { misses = 0; beat(); });
}
