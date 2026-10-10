/*
  「ロットを探す」の欄で、打った文字が差し戻らないか ── `lotlist.js` を Node で動かす。

  【何が起きていたか】
  打つそばから検索する(間を置いて送る)。「12」を送った返事が、「3」を
  打ち足した後に届くと、欄が「12」に戻され、待っていた検索も戻った「12」で
  送られて「3」が消えていた。返事が追い越して届いたときも、一覧と欄が
  古いほうに戻っていた(現場の声: 文字が差し戻る)。

  サーバの試験では見えない(画面側の順番の問題)。`mapedit_selection.mjs` と
  同じく、触るぶんだけの小さな DOM を用意して、モジュールをそのまま動かす。
  `lotlist.js` が読み込む `api.js` などは、写した先の隣に置いた差し替えで代える
  (返事を出す順番をこちらで決めるため)。
*/
import { mkdtempSync, mkdirSync, copyFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const work = mkdtempSync(join(tmpdir(), "lotlist_"));
mkdirSync(join(work, "views"));
copyFileSync(join(ROOT, "app", "static", "js", "views", "lotlist.js"),
             join(work, "views", "lotlist.js"));
// 全角→半角の直しは本物を使う(差し替えない)
copyFileSync(join(ROOT, "app", "static", "js", "halfwidth.js"), join(work, "halfwidth.js"));

// 返事は試験側が手で出す(`globalThis.pending` に溜める)
writeFileSync(join(work, "api.js"), `
export const api = {
  post(path, body) {
    return new Promise((resolve) => globalThis.pending.push({ path, body, resolve }));
  },
  get() { return Promise.resolve({ items: [] }); },
};`);
writeFileSync(join(work, "askbox.js"), "export async function promptBox() { return null; }");
writeFileSync(join(work, "nav.js"),
  "export function onLeave() {}\nexport function pageSignal() { return undefined; }");
writeFileSync(join(work, "toast.js"), "export function toast() {}\nexport function toastError() {}");

// --- ごく小さな DOM ---
function makeEl() {
  const handlers = {};
  return {
    value: "", hidden: false, textContent: "", disabled: false, handlers,
    addEventListener(type, fn) { handlers[type] = fn; },
    dispatchEvent(event) { if (handlers[event.type]) handlers[event.type](event); return true; },
    replaceChildren() {}, setAttribute() {}, querySelectorAll() { return []; },
    classList: { toggle() {}, add() {}, remove() {} },
  };
}
const els = {};
globalThis.document = {
  getElementById(id) { return (els[id] ||= makeEl()); },
  createElement() { return makeEl(); },
  addEventListener() {},
};
globalThis.pending = [];

function view(text, rows, lots = []) {
  return {
    available: true, headers: [], rows: lots.map((lot_no) => ({ lot_no, values: [] })),
    total: rows, conditions: [], saved: [],
    count_note: `${rows}件`, text, page_size: 200, can_save: false,
    save_why: "", empty_why: "",
  };
}

const lotlist = await import(pathToFileURL(join(work, "views", "lotlist.js")).href);
const opened = [];
lotlist.start({ view: view("", 553), onOpen: (lotNo) => opened.push(lotNo) });

const box = els.listText;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const tick = () => sleep(0);
let failed = 0;
function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  console.log(`${ok ? "ok  " : "NG  "} ${label}: ${JSON.stringify(actual)}`
              + (ok ? "" : ` (期待 ${JSON.stringify(expected)})`));
  if (!ok) failed += 1;
}
function type(text) { box.value = text; box.handlers.input(); }

// 1) 「12」を打つ → 送られる → 返事待ちのあいだに「3」を打ち足す
type("12");
await sleep(260);
check("「12」が送られた", pending.map((p) => p.body.text), ["12"]);
type("123");
const first = pending.shift();
first.resolve(view("12", 553));        // 打ち足した後に「12」の返事が届く
await tick();
check("「12」の返事で欄が戻らない", box.value, "123");
await sleep(260);
await tick();
check("待っていた検索は「123」で送る", pending.map((p) => p.body.text), ["123"]);
pending.shift().resolve(view("123", 14));
await tick();
check("「123」の返事で一覧を出す", els.countNote.textContent, "14件");
check("欄はそのまま", box.value, "123");

// 2) 追い越し: 「A」の返事が「AB」の返事より後に届いても、古いほうを出さない
//    (順に送るので「AB」は「A」の返事が届いてから送られる。それでも、
//    「A」の返事の時点で「AB」を打っていれば「A」は出さない)
type("A");
await sleep(260);
type("AB");
await sleep(260);
check("1つずつ順に送る(「AB」は「A」の返事を待つ)", pending.map((p) => p.body.text), ["A"]);
pending.shift().resolve(view("A", 300));
await tick(); await tick();
check("追い越された「A」の返事は出さない", els.countNote.textContent, "14件");
check("続けて「AB」を送る", pending.map((p) => p.body.text), ["AB"]);
pending.shift().resolve(view("AB", 5));
await tick();
check("最後の返事だけ出す", [box.value, els.countNote.textContent], ["AB", "5件"]);
check("打っただけでは開かない", opened, []);

// 3) 打っていないときは、サーバの文字に合わせる(条件を外した など)
const clear = els.clearFilter.handlers.click;
clear();
await tick();
pending.shift().resolve(view("", 553));
await tick();
check("サーバが文字を空にしたら欄も空", box.value, "");

// 4) 1件に絞れても打っただけでは開かない。開くのは検索欄の Enter(1件のとき)だけ
//    (現場の声:「7桁目を入れる前に画面がLOT詳細に移行する」「開きたくないこともある」)
type("R6545E");
await sleep(260);
pending.shift().resolve(view("R6545E", 1, ["R6545E0"]));
await tick();
check("1件に絞れても開かない", opened, []);
const enter = { key: "Enter", preventDefault() {} };
await box.handlers.keydown(enter);
check("Enter で開く", opened, ["R6545E0"]);
// 2件以上なら Enter でも開かない
type("R65");
await sleep(260);
pending.shift().resolve(view("R65", 22, ["R6500A0", "R6501A0"]));
await tick();
await box.handlers.keydown(enter);
check("2件以上なら Enter でも開かない", opened, ["R6545E0"]);

// 5) 全角で入った英数字は半角の大文字に直して探す(現場の声: IME が日本語のまま打つと見つからない)
pending.splice(0);
type("ｒ６５４５ｅ");
check("全角の英数字は半角の大文字に直す", box.value, "R6545E");
await sleep(260);
check("直した文字で探す", pending.map((p) => p.body.text), ["R6545E"]);
pending.shift().resolve(view("R6545E", 1, ["R6545E0"]));
await tick();

// 6) IME で変換中のあいだは触らない。確定したら直して探し直す
box.value = "ｒ６";
box.handlers.input({ isComposing: true });
check("変換中は直さない", box.value, "ｒ６");
box.handlers.compositionend({ type: "compositionend" });
check("確定したら直す", box.value, "R6");
await sleep(260);
check("確定した文字で探す", pending.map((p) => p.body.text), ["R6"]);
pending.shift().resolve(view("R6", 3, []));
await tick();

// 7) かな・漢字は変えない(用途名で探せるように)
type("ｼﾞﾃﾝｼﾔ 自転車");
check("かな・漢字はそのまま", box.value, "ｼﾞﾃﾝｼﾔ 自転車");
// 打ってすぐ Enter: いまの文字で絞ってから決める(古い一覧で開かない)
type("L816X51");
const pressed = box.handlers.keydown(enter);
await tick();
check("打ってすぐ Enter は、いまの文字で検索する", pending.map((p) => p.body.text), ["L816X51"]);
pending.shift().resolve(view("L816X51", 1, ["L816X51"]));
await pressed;
check("その結果が1件なら開く", opened, ["R6545E0", "L816X51"]);

if (failed) {
  console.log(`${failed}件 失敗`);
  process.exit(1);
}
console.log("すべて通過");
