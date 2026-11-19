/*
  設定画面の「作業ライン」(`packaging_tool/work_lines.py`)。

  実績ダッシュボードの条件表を並べ、ロットNO でどの作業ラインになるか(なぜか)を試す。
  判定はサーバが持つ。ここは並べるだけ。何も書かない。
*/

import { api } from "../api.js";
import { toastError } from "../toast.js";

const el = {};

function cell(text, cls) {
  const td = document.createElement("td");
  td.textContent = text === undefined || text === null ? "" : String(text);
  if (cls) td.className = cls;
  return td;
}

function condText(cond) {
  return Object.entries(cond || {}).map(([k, v]) => `${k} ${v}`).join("  ");
}

function paintTable(view) {
  el.wlWhy.hidden = Boolean(view.ok);
  el.wlWhy.textContent = view.ok ? "" : view.why;
  const info = view.info || {};
  el.wlSource.textContent = view.ok
    ? `条件表 版${info["版"] || "?"}(${info["保存日時"] || "日時不明"} ${info["保存した人"] || ""})・寸法の基準 ${view.basis}`
    : "条件表を読めません";
  el.wlGroups.replaceChildren(...(view.groups || []).map((g) => {
    const tr = document.createElement("tr");
    if (!g.line) tr.style.color = "var(--muted, #888)";
    tr.append(cell(g.rank), cell(g.name, "t"), cell(g.line || "(作業ラインではない)", "t"),
      cell(g.vc_first ? "○" : ""), cell(condText(g.cond) || "(条件なし)", "t"),
      cell(g.use_not ? condText(g.not_cond) : "", "t"));
    return tr;
  }));
}

function paintResult(res) {
  el.wlResult.hidden = false;
  if (res.line) {
    el.wlResult.className = "status status--ok";
    el.wlResult.textContent = `Lot ${res.lot_no} → ${res.line}(グループ「${res.group}」)`;
  } else {
    el.wlResult.className = `status status--${res.known ? "warn" : "ng"}`;
    el.wlResult.textContent = `Lot ${res.lot_no || "(なし)"}: ${res.why}`;
  }
  const v = res.values || {};
  el.wlValues.hidden = !Object.keys(v).length;
  el.wlValues.textContent = Object.keys(v).length
    ? `比べた値: 材質 ${v["オーダー材質"]} / 製造材質 ${v["製造材質"]} / オーダー寸法 ${v["オーダー板厚"]}×${v["オーダー板幅"]}×${v["オーダー板丈"]}`
      + ` / 製造寸法 ${v["製造板厚"]}×${v["製造板幅"]}×${v["製造板丈"]} / 比重 ${v["比重"]} / 計算単重 ${v["計算単重"]}`
      + ` / 形状 ${v["形状"]} / VC ${v["VC"]}(寸法の基準: ${v["寸法の基準"]})`
    : "";
  el.wlSteps.replaceChildren(...(res.steps || []).map((s) => {
    const tr = document.createElement("tr");
    if (s.ok) tr.style.fontWeight = "700";
    tr.append(cell(s.rank), cell(s.name, "t"), cell(s.line || "", "t"), cell(s.result, "t"),
      cell((s.failed || []).join("・"), "t"));
    return tr;
  }));
  el.wlStepsWrap.hidden = !(res.steps || []).length;
}

async function test() {
  const lot = el.wlLot.value.trim();
  if (!lot) { el.wlLot.focus(); return; }
  el.wlTest.disabled = true;
  try {
    paintResult(await api.post("/api/settings/work-lines/test", { lot_no: lot }));
  } catch (err) {
    toastError(err);
  } finally {
    el.wlTest.disabled = false;
  }
}

export function attach() {
  for (const id of ["wlSource", "wlWhy", "wlLot", "wlTest", "wlResult", "wlValues", "wlSteps", "wlStepsWrap",
                    "wlGroups"]) {
    el[id] = document.getElementById(id);
  }
  if (!el.wlGroups) return;
  el.wlTest.addEventListener("click", test);
  el.wlLot.addEventListener("keydown", (e) => { if (e.key === "Enter") test(); });
  api.get("/api/settings/work-lines").then(paintTable).catch(() => {
    el.wlSource.textContent = "条件表を読めません";
  });
}
