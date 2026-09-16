/*
  screen.js — 「このタブは譲りました」の覆い

  【なぜ要るのか】
  このツールは**作業状態をプロセスに1つ**持っています(Lot・パレット・
  選んだ資材)。1台のPCを1人が使う前提だからです。ところがブラウザは
  同じアドレスを何枚でも開けるので、2枚開くとどちらも同じ状態を触ります:

      タブA: ロット 1234567 を引く
      タブB: ロット 9999999 を引く      ← 状態が入れ替わる
      タブA: そのまま「倉庫へ送る」      ← **Bのロットで送られる**

  タブAの画面には 1234567 が出たままなので、押した人は気づけません。
  サーバ側は `packaging_tool/screen_lock.py` が断りますが、**断られた
  ことが見えなければ**「押しても何も起きない」だけになります。

  【何を出すか】
  覆いを1枚かぶせて、操作できないことと、どうすれば続けられるかを
  出します。**行き止まりにはしません** ── 閉じ忘れただけのことも
  あるので、「このタブで続ける」で読み込み直せば取り戻せます
  (開き直したタブが最後になるので、そのまま使えます)。

  文言はサーバから来たものを優先します(設計書 §1)。
*/

const ID = "screen-taken";

/** このタブの番号。サーバが画面を組み立てるときに振った(base.html)。 */
export function id() {
  return (window.APP && window.APP.screenId) || "";
}

/** 要求に付ける見出し。`packaging_tool/screen_lock.HEADER` と同じ名前。 */
export const HEADER = "X-Screen-Id";

/** サーバが返す断りの種別。 */
export const TAKEN = "screen_taken";

let shown = false;

/**
 * 覆いを出す。**何度呼ばれても1枚だけ。**
 *
 * 断りは色々な口から同時に来る(押した操作・見張りの問い合わせ)ので、
 * 呼ばれる回数は数えられない。
 */
export function showTaken(message) {
  if (shown) return;
  shown = true;

  const box = document.createElement("div");
  box.className = "taken";
  box.id = ID;
  box.setAttribute("role", "alertdialog");
  box.setAttribute("aria-modal", "true");
  box.innerHTML = `
    <div class="taken__card">
      <b class="taken__title">このタブは操作できません</b>
      <p class="taken__note"></p>
      <p class="taken__why"></p>
      <div class="taken__acts">
        <button class="btn btn--primary" type="button" id="taken-use">このタブで続ける</button>
        <button class="btn" type="button" id="taken-close">このタブを閉じる</button>
      </div>
    </div>`;
  box.querySelector(".taken__note").textContent = message
    || "あとから開いたタブに操作を譲りました。";
  // **`textContent` で入れる。** 読みやすく折り返して書いた HTML を
  // そのまま入れると、折った場所が日本語の文の途中に空白として出る
  // (「あとから開いたほうが 操作する画面に」)
  box.querySelector(".taken__why").textContent =
    "同じアプリを2枚開くと、あとから開いたほうが操作する画面になります。"
    + "作業中のLotやパレットはアプリが1つだけ覚えているので、2枚から"
    + "触ると、片方の内容でもう片方の操作が通ってしまうためです。";
  document.body.appendChild(box);

  // 読み込み直せば新しい番号が振られ、**このタブが最後に開いた画面に
  // なる**。取り戻す手を別に作らないのは、道が1本なら食い違わないから
  box.querySelector("#taken-use")
     .addEventListener("click", () => location.reload());
  // 閉じるのはブラウザの仕事。こちらから閉じられないことがあるので、
  // 押せなかったときのために案内へ切り替える
  box.querySelector("#taken-close").addEventListener("click", () => {
    window.close();
    box.querySelector(".taken__note").textContent =
      "このタブは、ブラウザの × で閉じてください。";
  });
  box.querySelector("#taken-use").focus();
}

/** 取り戻せたときに引っ込める(見張りが気づいたとき)。 */
export function clearTaken() {
  const box = document.getElementById(ID);
  if (box) box.remove();
  shown = false;
}

/** いま覆いが出ているか。 */
export function taken() {
  return shown;
}
