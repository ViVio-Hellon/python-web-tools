/*
  unsaved.js — 保存していない図があるうちは、黙って閉じさせない

  【なぜ要るのか】
  現場の声:「配置編集保存されてないよ」。

  配置編集は**「配置を保存」を押すまでファイルに書きません**(間違えて
  動かしても、開き直せば元に戻る、という約束)。ところがタブを閉じると
  8秒でアプリが自動終了し、**そこで保存していない編集は消えます**。
  以前は閉じるときに何も言わなかったので、「配置編集をOFFにした」
  「動かして閉じた」だけで、保存したつもりの配置が消えていました。

  【勝手に保存はしない】
  保存してしまうと、上の約束(間違えても開き直せば戻る)が壊れます。
  **消える前に聞く**だけにします。

  【いつ聞くか】
  アプリの外へ出るとき(タブを閉じる・別のページへ行く・読み込み直す)。
  アプリの中の移動(モードの切替・「このタブで続ける」)では聞きません
  ── 図の編集はサーバが持っているので、中の移動では消えないためです。
  中から移るときは、移る側が `allowLeave()` を呼んでから移ります。

  何が未保存かは2か所から入ります:
    - 見張り(`/api/health` の `unsaved`)… どの画面にいても分かる
    - 図の画面を描き直したとき(`mark`)… 動かした直後から分かる
*/

const state = {};          // 鍵("layout" / "inventory") → 未保存か
let leaving = false;       // アプリの中の移動なので聞かない

/** 図の画面が描き直したとき。**見張りを待たずに**その場で覚える。 */
export function mark(key, dirty) {
  state[key] = Boolean(dirty);
}

/** 見張りの答え({鍵: 呼び名})。**サーバが正**なので丸ごと置き換える。 */
export function fromServer(map) {
  for (const key of Object.keys(state)) state[key] = false;
  for (const key of Object.keys(map || {})) state[key] = true;
}

/** いま保存していない図があるか。 */
export function any() {
  return Object.values(state).some(Boolean);
}

/** アプリの中で移る直前に呼ぶ。編集は消えないので聞かない。 */
export function allowLeave() {
  leaving = true;
}

window.addEventListener("beforeunload", (event) => {
  if (leaving || !any()) return;
  // 文言はブラウザが決める(最近のブラウザは独自の文を出さない)。
  // 「このサイトを離れますか? 行った変更が保存されない可能性があります」
  event.preventDefault();
  event.returnValue = "";
});
