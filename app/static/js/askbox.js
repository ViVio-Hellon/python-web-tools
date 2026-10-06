/*
  askbox.js — 確かめの窓(はい / やめる)と、名前を打ってもらう窓。

  【なぜブラウザの confirm / prompt を使わないのか】
  デスクトップ版(Tauri の窓)では `window.confirm` / `window.prompt` の窓が**出ないまま**
  「OK」と答えた扱いで処理が進んだ(別の道具での指摘)。表を消す・作り直す・入れ替える・
  配置図を出荷時に戻す・切断依頼を取り消す ── どれも**取り返しのつかない操作の前の
  確かめ**で、そこが黙って素通りになるのがいちばん危ない。

  画面の中に自分で窓を出すので、ブラウザ版でもデスクトップ版でも同じに動く。
  答えは Promise で返す(`await confirmBox(...)`)。Esc・「やめる」・窓の外を押す は「やめる」。

  使い方:
      if (!(await confirmBox("消します。よろしいですか？", { ok: "消す", danger: true }))) return;
      const name = await promptBox("名前を付けてください");   // やめたら null
*/

let box = null;

function ensure() {
  if (box && document.body.contains(box.dialog)) return box;
  const style = document.createElement("style");
  style.textContent = `
    .askbox { border: 1px solid var(--rule); border-radius: var(--radius, 10px); padding: 0;
              background: var(--surface); color: var(--ink); width: min(520px, 92vw);
              box-shadow: var(--shadow-pop, 0 10px 30px rgb(0 0 0 / 30%)); }
    .askbox::backdrop { background: rgb(0 0 0 / 45%); }
    .askbox__body { padding: 18px 20px 8px; }
    .askbox__text { margin: 0; white-space: pre-wrap; line-height: 1.7;
                    font-size: var(--fs-md, 13px); }
    .askbox__input { margin-top: 12px; width: 100%; }
    .askbox__foot { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 20px 16px; }
  `;
  const dialog = document.createElement("dialog");
  dialog.className = "askbox";
  dialog.setAttribute("aria-labelledby", "askboxText");
  dialog.innerHTML = `
    <div class="askbox__body">
      <p class="askbox__text" id="askboxText"></p>
      <input class="input askbox__input" id="askboxInput" spellcheck="false" autocomplete="off" hidden>
    </div>
    <div class="askbox__foot">
      <button class="btn" type="button" data-answer="no">やめる</button>
      <button class="btn btn--commit" type="button" data-answer="yes">OK</button>
    </div>`;
  document.head.appendChild(style);
  document.body.appendChild(dialog);
  box = {
    dialog,
    text: dialog.querySelector(".askbox__text"),
    input: dialog.querySelector(".askbox__input"),
    yes: dialog.querySelector('[data-answer="yes"]'),
    no: dialog.querySelector('[data-answer="no"]'),
  };
  return box;
}

function open(message, { ok = "OK", cancel = "やめる", danger = false, input = null } = {}) {
  const b = ensure();
  // 前の窓が開いたまま次を訊かれたら、前のものは「やめる」で閉じる
  if (b.dialog.open) b.dialog.close("no");
  b.text.textContent = message;
  b.yes.textContent = ok;
  b.no.textContent = cancel;
  b.yes.className = `btn ${danger ? "btn--danger" : "btn--commit"}`;
  b.input.hidden = input === null;
  b.input.value = input ?? "";
  return new Promise((resolve) => {
    const finish = (answer) => {
      b.yes.removeEventListener("click", onYes);
      b.no.removeEventListener("click", onNo);
      b.dialog.removeEventListener("close", onClose);
      b.dialog.removeEventListener("click", onBackdrop);
      b.input.removeEventListener("keydown", onEnter);
      if (b.dialog.open) b.dialog.close();
      resolve(answer);
    };
    const onYes = () => finish(true);
    const onNo = () => finish(false);
    const onClose = () => finish(false);                 // Esc
    const onBackdrop = (event) => { if (event.target === b.dialog) finish(false); };
    const onEnter = (event) => {
      if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); finish(true); }
    };
    b.yes.addEventListener("click", onYes);
    b.no.addEventListener("click", onNo);
    b.dialog.addEventListener("close", onClose);
    b.dialog.addEventListener("click", onBackdrop);
    b.input.addEventListener("keydown", onEnter);
    b.dialog.showModal();
    // 取り返しのつかない操作は「やめる」に最初の焦点を置く(Enter の押し間違いで進めない)
    if (input !== null) b.input.focus();
    else (danger ? b.no : b.yes).focus();
  });
}

/** はい / やめる。はいなら true。 */
export function confirmBox(message, options = {}) {
  return open(message, { ...options, input: null });
}

/** 1行を打ってもらう。やめたら null(空のまま OK なら ""。ブラウザの prompt と同じ)。 */
export async function promptBox(message, { value = "", ...options } = {}) {
  const b = ensure();
  const ok = await open(message, { ...options, input: String(value) });
  return ok ? b.input.value : null;
}
