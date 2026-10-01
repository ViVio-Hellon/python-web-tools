/*
  errorlog.js — 画面(ブラウザ)の中で起きたエラーをサーバのログへ送る

  画面のエラーはブラウザの中で消えてしまい、後から何も追えなかった
  (「押しても何も起きない」の正体がこれのことがある)。サーバへ送って
  エラー記録に残し、返ってきた**エラー番号**を画面にも出す ── 現場から
  番号を聞けば、設定 →「ログ」でその1件にたどり着ける。

  サーバが断った(ApiError)ものは送らない。そちらはサーバが理由ごと
  ログに書いている。
*/

import { api, ApiError } from "./api.js";
import { toast } from "./toast.js";

// 1枚の画面から送る上限。壊れた画面が同じエラーを出し続けても、
// ログとサーバを埋めない
const LIMIT = 10;
let sent = 0;
const seen = new Set();

async function report(info) {
  const key = `${info.message}@${info.source}:${info.line}`;
  if (seen.has(key) || sent >= LIMIT) return;
  seen.add(key);
  sent += 1;
  try {
    const body = await api.post("/api/client-error", {
      ...info, page: location.pathname + location.search,
    });
    toast(`画面でエラーが起きました(エラー番号 ${body.ref})。`
          + "続けて困るときは、この番号を担当に伝えてください。", "ng");
  } catch {
    // 送れなくても画面は止めない(送れないこと自体を送る先が無い)
  }
}

export function start() {
  window.addEventListener("error", (event) => {
    // 画像などの読み込み失敗は `error` に来るが、エラーではない
    if (!(event instanceof ErrorEvent)) return;
    report({
      message: String(event.message || "不明なエラー"),
      source: String(event.filename || ""),
      line: event.lineno || 0,
      column: event.colno || 0,
      stack: String(event.error?.stack || ""),
    });
  });
  window.addEventListener("unhandledrejection", (event) => {
    const err = event.reason;
    if (err instanceof ApiError) return;          // サーバが記録済み
    report({
      message: String(err?.message || err || "不明なエラー(Promise)"),
      source: "",
      line: 0,
      column: 0,
      stack: String(err?.stack || ""),
    });
  });
}
