/*
  app.js — 全画面で共通の起動処理

  画面ごとの中身は views/ 以下。ここは外枠の面倒だけを見る。
*/

import { api } from "./api.js";
import { confirmBox } from "./askbox.js";
import { startHeartbeat } from "./health.js";
import { toast, toastError } from "./toast.js";
import * as busy from "./busy.js";
import * as desktop from "./desktop.js";
import * as errorlog from "./errorlog.js";
import * as nav from "./nav.js";
import * as running from "./running.js";
import * as unsaved from "./unsaved.js";

// **押されたボタンを覚えておく。** `api.js` が送信を始めるときに、
// そのボタンを待機の姿にする(無言で待たせない)
busy.watchClicks();

// 画面の中のエラーをサーバのログへ(後から追えるように)
errorlog.start();
// デスクトップ版: 別窓で開くリンクを外枠に頼む(ブラウザ版では何もしない)
desktop.watchLinks();

startHeartbeat();
// 長い処理は帯に出す。**画面を離れても見える**ようにするため
running.start();

/**
 * リボンの中の道具を繋ぐ。
 *
 * **リボンは画面ごとに作り直される**(値が画面で違う)ので、
 * 差し替えのたびに呼ばれる。同じ要素へ二重に付かないよう、
 * 付けた要素に印を残す ── 押すたびに2回終了しようとしないため。
 */
export function wireShell() {
  wireQuit(document.getElementById("quit"));
  wireTheme(document.getElementById("themeToggle"));
  wireManual(document.getElementById("openManual"));
  for (const btn of document.querySelectorAll(".modeswitch__btn")) wireMode(btn);
  // 新しい帯は空で来る。**移った先で「何も動いていない」に見せない**
  running.paint();
}

function once(node) {
  if (!node || node.dataset.wired === "1") return false;
  node.dataset.wired = "1";
  return true;
}

// 「終了」ボタン (基盤仕様書 2.8)。
// 実行中の処理があるときサーバは 409 を返す。勝手に中断しない
function wireQuit(button) {
  if (!once(button)) return;
  button.addEventListener("click", async () => {
    try {
      const body = await api.post("/api/shutdown", {});
      toast(body.message || "終了します", "ok");
      setTimeout(() => {
        document.body.innerHTML =
          '<main style="padding:48px;font-family:var(--sans)">' +
          "<h1>終了しました</h1><p>このタブは閉じてかまいません。</p></main>";
      }, 700);
    } catch (err) {
      if (err.status === 409) {
        // 中断してよいか・保存せずに終えてよいかは利用者が決める。
        // **聞く文はサーバが持つ**(実行中の処理か、保存していない図か)
        if (await confirmBox(err.message || "終了しますか?", { ok: "終了する", danger: true })) {
          // 捨ててよいと答えたので、閉じるときにもう一度は聞かない
          unsaved.allowLeave();
          try {
            await api.post("/api/shutdown", { force: true });
            toast("終了します", "ok");
          } catch (e) { toastError(e); }
        }
        return;
      }
      toastError(err);
    }
  });
}

// ------------------------------------------------------------------
// 画面の色(ライト / ダーク)。**この端末の設定に残す**(`/api/theme`)。
// 選んでいなければ OS の設定に合わせる(`tokens.css` の3つの状態)
// ------------------------------------------------------------------
function currentTheme() {
  const chosen = document.documentElement.dataset.theme;
  if (chosen === "light" || chosen === "dark") return chosen;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
}

function paintTheme(button) {
  const now = currentTheme();
  const label = button.querySelector("#themeLabel");
  if (label) label.textContent = now === "dark" ? "ダーク" : "ライト";
  button.setAttribute("aria-pressed", String(now === "dark"));
  button.title = `いまは${now === "dark" ? "ダーク" : "ライト"}表示です。`
    + `押すと${now === "dark" ? "ライト" : "ダーク"}に切り替えます(この端末に残ります)`;
}

function wireTheme(button) {
  if (!once(button)) return;
  paintTheme(button);
  button.addEventListener("click", async () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    // **先に画面を変える**(押した手応え)。残すのはそのあと
    document.documentElement.dataset.theme = next;
    paintTheme(button);
    try {
      await api.post("/api/theme", { theme: next });
    } catch (err) {
      toastError(err);
    }
  });
}

// 説明書。**別の窓で開く**(作業中の画面を離れずに読めるように)
function wireManual(button) {
  if (!once(button)) return;
  button.addEventListener("click", () => {
    if (!desktop.openWindow("/manual", "操作説明書")) {
      toast("別の窓を開けませんでした。ポップアップの許可を確認してください。", "warn");
    }
  });
}

// モード切替。**選べるモードはサーバが決める**(`アクセス権限` マスタ)。
// ここは押されたことを伝えて、返ってきた行き先へ移るだけ
function wireMode(btn) {
  if (!once(btn)) return;
  btn.addEventListener("click", async () => {
    if (btn.dataset.current === "1") return;
    try {
      const body = await api.post("/api/mode", { mode: btn.dataset.mode });
      // **ここは読み込み直す。** 出せる画面そのものが入れ替わるので、
      // レールも権限も作り直させる(資材モードに資材選択は無い)
      // アプリの中の移動。図の編集はサーバが持っているので消えない
      unsaved.allowLeave();
      location.href = body.next || location.pathname;
    } catch (err) { toastError(err); }
  });
}

// 画面切替のキーボード操作。現場は片手作業が多い
document.addEventListener("keydown", (event) => {
  if (event.altKey || event.ctrlKey || event.metaKey) return;
  const match = /^F([1-7])$/.exec(event.key);
  if (!match) return;
  const link = document.querySelectorAll(".rail a")[Number(match[1]) - 1];
  if (link) { event.preventDefault(); link.click(); }
});

wireShell();
nav.start({ onSwap: wireShell });

export { api, toast, toastError };
