/*
  ロット検索。

  作業の出発点。**入口は検索欄1つ**で、絞り込んだ一覧から行を開くと
  ロット情報・引当情報・受注情報がモーダルに出る。

  **モーダルの中身はここには無い**(`../lotdetail.js`)。同じものを
  発注一覧の「Lotを開く」からも出すので、置き場所を1つにしてある ──
  2か所に書くと、片方だけ直った図が現場に出る。
*/
import { api } from "../api.js";
import * as nav from "../nav.js";
import { toast, toastError } from "../toast.js";
// `lotlist.js` と `lotdetail.js` は動的に読み込む。理由は `settings.js` が
// `master.js` を動的に読み込んでいるのと同じ(このファイル自身と版クエリを
// 合わせ、入れ替えたときに中身も必ず一緒に入れ替わるようにするため)
const VERSION_QUERY = new URL(import.meta.url).search;
const lotlist = await import(`./lotlist.js${VERSION_QUERY}`);
const detail = await import(`../lotdetail.js${VERSION_QUERY}`);

/** ステータスリボンを書き換える。値はサーバが決める。 */
function applyRibbon(ribbon) {
  if (!ribbon) return;
  document.getElementById("rb-lot").textContent = ribbon.lot;
  document.getElementById("rb-product").textContent = ribbon.product;
  document.getElementById("rb-pallet").textContent = ribbon.pallet;
  document.getElementById("rb-modes").replaceChildren(
    ...ribbon.modes.map((chip) => {
      const span = document.createElement("span");
      span.className = `chip chip--${chip.kind}`;
      span.textContent = chip.text;
      return span;
    }));
}

/** 行を開く(ダブルクリック / Enter)。開くことが「選ぶ」ことでもある。 */
async function openLot(lotNo) {
  try {
    const body = await api.get(`/api/lot/${encodeURIComponent(lotNo)}`);
    detail.show(body);
    applyRibbon(body.ribbon);
    // どの行が作業中かはサーバが決める(応答に一覧ごと入っている)
    lotlist.render(body.list);
  } catch (err) {
    toastError(err);
  }
}

/*
  絞り込みで1件になったとき。サーバが確定まで済ませて `detail` を
  付けて返すので、ここは開くだけ。**番号を打つ専用の欄の代わり**で、
  7桁を打てば必ず1件になる。
*/
function onListChanged(view) {
  if (!view || !view.detail) return;
  detail.show(view.detail);
  applyRibbon(view.ribbon);
}

export function start(options) {
  const expand = async () => {
    if (!detail.currentLot()) return;
    try {
      const body = await api.post("/api/lot/expand", {});
      applyRibbon(body.ribbon);
      toast(body.message, "ok");
      // 渡した先へそのまま移る(VBA も `mp.value = 1` でページを切り替えていた)。
      // 差し替えで移るので、いま出したトーストは消えない
      nav.go(body.next);
    } catch (err) {
      toastError(err);
    }
  };
  detail.mount({ onExpand: expand,
                 expandAbsentWhy: options.expandAbsentWhy,
                 // BOX最終実績寸法を選び直した。帯の製品サイズの元と、一覧の光りを直す
                 onReloaded: (body) => {
                   applyRibbon(body.ribbon);
                   if (body.list) lotlist.render(body.list);
                 } });

  lotlist.start({ view: options.list, onOpen: openLot,
                  onChanged: onListChanged });

  // 発注一覧から「このLotを開く」で来たとき。**番号を打ち直させない**
  // ── 7桁の打ち間違いは、そのまま別のロットを開いてしまう
  if (options.openLot) openLot(options.openLot);

  // 画面を出たら図面の見張りを打ち切る
  nav.onLeave(detail.stop);
}
