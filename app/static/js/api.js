/*
  api.js — サーバとのやりとり

  JSが持つのは「呼ぶ」「受け取ったものを描く」だけ。
  **業務の判断はサーバが行う**(押せるか・隠すか・何と出すか)。
  詳細は docs/設計.md §1。
*/

import * as busy from "./busy.js";
import * as screen from "./screen.js";

const TOKEN = window.APP.token;

/**
 * サーバが返したエラーを、そのまま画面に出せる形で持つ。
 *
 * 断りの返し方は2通りある(設計書 §6.1)。**どちらでも文言はサーバが
 * 持っている**ので、両方から拾う:
 *
 *   入力の形の誤り  … `{"error": {"code", "message"}}`      状態は動いていない
 *   業務としての断り … 画面ぜんぶ + 最上位の `message`       状態は動いていることがある
 *
 * 後者を拾い忘れると、理由(「先にパレットサイズを設定してください。」)の
 * 代わりに「通信に失敗しました (HTTP 422)」と出る ── **通信は成功して
 * いるのに通信の失敗として案内される**ので、現場は直しようがない。
 */
export class ApiError extends Error {
  constructor(status, body) {
    const info = (body && body.error) || {};
    super(info.message || (body && body.message)
          || `通信に失敗しました (HTTP ${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = info.code || "";
    this.field = info.field || "";
    // 断られたときも本文に画面ぜんぶが入っていることがある(422)。
    // 「断られた」と「画面が古いまま」を同時に起こさないために渡す
    this.body = body;
  }
}

async function request(path, options = {}) {
  /*
    **押した本人に、いま待っていることを伝える。**

    どの画面も「押す → サーバへ投げる → 返ってきた画面を描く」で
    出来ているので、投げるところを1か所だけ捕まえれば、画面を
    1つずつ書き換えなくても全部に効く。伝える相手は
    `busy.watchClicks()` が覚えている**いま押されたボタン**。

    250ms より速く返るものには何も出さない ── 一瞬で終わるものに
    待機の姿を出すと、画面がちらつくだけになる。
  */
  const done = busy.mark(busy.pressed());
  try {
    const res = await fetch(path, {
      ...options,
      cache: "no-store",
      headers: {
        "X-Tool-Token": TOKEN,
        // どのタブからの要求か。サーバは**最後に開いたタブ**からの
        // ものだけを通す(`packaging_tool/screen_lock.py`)
        [screen.HEADER]: screen.id(),
        // ファイルを送るとき(FormData)は、ブラウザに区切りごと決めさせる
        ...(typeof options.body === "string" ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });

    let body = null;
    const type = res.headers.get("Content-Type") || "";
    if (type.includes("application/json")) {
      body = await res.json();
    }
    if (!res.ok) {
      const err = new ApiError(res.status, body);
      // **譲ったことは、押した本人に見せる。** トーストだけだと数秒で
      // 消えて、そのあとは「押しても何も起きない画面」になる
      if (err.code === screen.TAKEN) screen.showTaken(err.message);
      throw err;
    }
    return body;
  } finally {
    done();
  }
}

/*
  `<img src>` や `<iframe src>` のように、**ヘッダを付けられない**読み込みの
  ためのURL。起動トークンはクエリでも受け付けるので、そこに載せる
  (`app/__init__.py` の `request.args.get("t")`)。

  トークンはもともとこのページの `window.APP.token` に入っている。
  ここでURLに付けても新しく漏れるものは無い ── 送信先は自分自身で、
  `Referrer-Policy: no-referrer` により外へ持ち出されることもない。
*/
export function tokenUrl(path) {
  return `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(TOKEN)}`;
}

export const api = {
  get: (path) => request(path),
  post: (path, data) => request(path, { method: "POST", body: JSON.stringify(data ?? {}) }),
  // ファイルを送る(「表を持ってくる」のドラッグ&ドロップ)
  postForm: (path, form) => request(path, { method: "POST", body: form }),
};
