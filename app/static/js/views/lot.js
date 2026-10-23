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
import { startPlans, progress as planProgress } from "../lotplans_view.js";
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

// 予定ロットを開く手順(サーバの `PLAN_STEPS`)。1つずつ呼んで進み具合を出す
const PLAN_STEPS = [
  ["open", "ロットを開いて資材展開しています"],
  ["orders", "共有から発注を取り込んでいます"],
  ["pallet", "発注したパレットで決めています"],
];

/** 予定ロットを開く。発注したパレットを決めて、ボード選定へ移る(移れなければロット情報を開く)。 */
async function openPlanned(lotNo) {
  const total = PLAN_STEPS.length;
  try {
    let body = null;
    for (const [index, [step, label]] of PLAN_STEPS.entries()) {
      planProgress(index, total, `${index + 1}/${total} ${label}…`);
      body = await api.post("/api/lot/plans/start", { lot_no: lotNo, step });
      if (!body.next_step) break;            // ここで終わり(移る / ロット情報を開く)
    }
    planProgress(total, total, body.next ? "資材選択へ移ります" : "ロット情報を開きます");
    applyRibbon(body.ribbon);
    if (body.next) {
      if (body.message) toast(body.message, body.warn ? "warn" : "ok");
      nav.go(body.next);
      return;
    }
    detail.show(body);
    if (body.list) lotlist.render(body.list);
    if (body.message) toast(body.message, "warn");
  } catch (err) {
    toastError(err);
  } finally {
    planProgress(null);
  }
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

  lotlist.start({ view: options.list, onOpen: openLot });

  // 発注一覧から「このLotを開く」で来たとき。**番号を打ち直させない**
  // ── 7桁の打ち間違いは、そのまま別のロットを開いてしまう
  if (options.openLot) openLot(options.openLot);

  // 予定ロット(この端末のラインに入ったロットの知らせ)。「開く」は発注したパレットを決めて
  // ボード選定へ移る(発注が無いなどで移れなければ、行を開くのと同じ)
  startPlans(options.plans, openPlanned);

  // 画面を出たら図面の見張りを打ち切る
  nav.onLeave(detail.stop);
}
