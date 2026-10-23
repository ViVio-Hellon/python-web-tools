/*
  予定ロットの「確認済み」を畳む(VER4.8.91)── `lotplans_view.js` を Node で動かす。

  現場の声:「確認済みは畳んで、押したときだけ出すようにしてください」
  見ること: 未確認だけが上の一覧に出る / 確認済みは「確認済み N件」を押したときだけ出る /
  見張りで出し直しても開け閉めが変わらない / 確認済みが0件ならボタンは出ない。
*/
import { mkdtempSync, copyFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const work = mkdtempSync(join(tmpdir(), "lotplans_"));
copyFileSync(join(ROOT, "app", "static", "js", "lotplans_view.js"), join(work, "lotplans_view.js"));
writeFileSync(join(work, "api.js"), `
export const api = { get() { return new Promise(() => {}); }, post() { return new Promise(() => {}); } };`);
writeFileSync(join(work, "nav.js"), "export function onLeave() {}");
writeFileSync(join(work, "toast.js"), "export function toast() {}\nexport function toastError() {}");

function makeEl() {
  const handlers = {};
  const attrs = {};
  return {
    hidden: false, textContent: "", className: "", title: "", type: "", dataset: {}, children: [],
    handlers, attrs,
    addEventListener(type, fn) { handlers[type] = fn; },
    click() { if (handlers.click) handlers.click(); },
    replaceChildren(...nodes) { this.children = nodes; },
    append(...nodes) { this.children.push(...nodes); },
    setAttribute(k, v) { attrs[k] = v; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
}
const els = {};
globalThis.document = {
  getElementById(id) { return (els[id] ||= makeEl()); },
  createElement() { return makeEl(); },
  querySelector() { return null; },
};
globalThis.window = { setInterval() { return 1; }, clearInterval() {} };

const mod = await import(pathToFileURL(join(work, "lotplans_view.js")).href);
const item = (id, seen) => ({ plan_id: id, lot_no: id, line: "AIM", seen, opened: seen,
                              sent_label: "10/23 09:15", terminal: "PC", summary: "" });
const lots = (node) => node.children.map((li) => li.dataset.plan);
let failed = 0;
function check(what, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) { failed += 1; console.log(`NG ${what}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`); }
}

const state = { lines: ["AIM"], label: "この端末のライン: AIM", days: 7, why: "",
                items: [item("N1", false), item("S1", true), item("S2", true)] };
mod.startPlans(state, async () => {});

check("未確認だけ上に", lots(els.planList), ["N1"]);
check("確認済みは畳んである", els.planSeenList.hidden, true);
check("畳んだ一覧は空", lots(els.planSeenList), []);
check("ボタンは出る", els.planFold.hidden, false);
check("件数", els.planFold.textContent.startsWith("確認済み 2件"), true);

els.planFold.click();
check("押すと出る", els.planSeenList.hidden, false);
check("確認済みの中身", lots(els.planSeenList), ["S1", "S2"]);
check("aria", els.planFold.attrs["aria-expanded"], "true");

mod.render({ ...state, items: [item("N1", false), item("S1", true), item("S2", true), item("S3", true)] });
check("見張りで出し直しても開いたまま", els.planSeenList.hidden, false);
check("増えた分も出る", lots(els.planSeenList), ["S1", "S2", "S3"]);

els.planFold.click();
check("もう一度押すと畳む", els.planSeenList.hidden, true);

mod.render({ ...state, items: [item("S1", true)] });
check("未確認0件なら上の一覧は隠す", els.planList.hidden, true);
check("新しいものは無いと言う", [els.planEmpty.hidden, els.planEmpty.textContent],
      [false, "新しい予定ロットはありません"]);

mod.render({ ...state, items: [item("N2", false)] });
check("確認済み0件ならボタンは出さない", els.planFold.hidden, true);

if (failed) { console.log(`${failed} 件 NG`); process.exit(1); }
console.log("OK");
