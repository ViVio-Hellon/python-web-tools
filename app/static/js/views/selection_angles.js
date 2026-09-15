/**
 * アングル(コーナーボード)の段 ── 候補・選ぶ・自動選定・図
 *
 * **この段のことはこの中で完結させる。** 描くのも押されたときの
 * 投げ返しもここが持ち、外へ出すのは `mount` と `render` だけ
 * (`lotlist.js` と同じ形)。
 *
 * アングルはボードと別の資材で、丈だけで決まり、本数の上限は製品丈で
 * 変わる。図は「アングル配置」を押すまで描かない ── 選んだ時点で描くと、
 * まだ確かめていない組み合わせが図になる。
 */
import { toast } from "../toast.js";

const el = {};
// 本体から渡してもらうもの。**描き方と配線はこの段が持ち、押された
// ことの投げ返しと行の選び方だけを借りる**
let send = null;
let pick = null;
let picked = null;
let why = null;
let replaceKeepingPick = null;

function angleRow(length, index) {
  const tr = document.createElement("tr");
  tr.dataset.length = length;
  tr.dataset.index = index;
  tr.dataset.key = String(length);
  const td = document.createElement("td");
  td.className = "n";
  td.textContent = length;
  tr.appendChild(td);
  return tr;
}

function angleSelectedRow(length, index) {
  const tr = angleRow(length, index);
  const cell = document.createElement("td");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn--danger rowdel";
  button.textContent = "削除";
  button.dataset.index = index;
  cell.appendChild(button);
  tr.appendChild(cell);
  return tr;
}

export function render(angles) {
  if (!angles) return;

  // 保護材がアングル以外に確定していれば、この欄は使わない
  el.angleCard.hidden = !angles.show;
  el.hosozaiLabel.hidden = !angles.hosozai_label;
  el.hosozaiLabel.textContent = angles.hosozai_label;
  if (!angles.show) return;

  replaceKeepingPick(el.angleRows, angles.candidates.map(angleRow));
  // 選択側は選ばせない(削除は行のボタンで押す)ので、そのまま入れ替える
  el.angleSelected.replaceChildren(...angles.selected.map(angleSelectedRow));
  el.angleNeedCut.hidden = !angles.need_cut;
  el.autoAngle.disabled = !angles.can_auto;
  el.drawAngle.disabled = !angles.can_draw;
  // 押せない理由はサーバがまとめてある(同じ文を2つ出さない)
  why(el.angleWhy, (angles.why || []).join("　"));
}

export function mount(api) {
  ({ send, pick, picked, why, replaceKeepingPick } = api);
  for (const id of ["angleCard", "angleRows", "angleSelected", "angleNeedCut",
                    "addAngle", "autoAngle", "drawAngle", "angleWhy",
                    "hosozaiLabel"]) {
    el[id] = document.getElementById(id);
  }


  // --- アングル -------------------------------------------------
  el.angleRows.addEventListener("click", (event) => {
    const tr = event.target.closest("tr");
    if (tr) pick(el.angleRows, tr);
  });

  el.addAngle.addEventListener("click", () => {
    const tr = picked(el.angleRows);
    if (!tr) {
      toast("追加するアングルを候補から選んでください。", "warn");
      return;
    }
    send("/api/selection/angles/add", { length: tr.dataset.length });
  });

  el.angleSelected.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-index]");
    if (!button) return;
    send("/api/selection/angles/remove", { index: button.dataset.index });
  });

  el.autoAngle.addEventListener("click", () =>
    send("/api/selection/angles/auto"));
  el.drawAngle.addEventListener("click", () =>
    send("/api/selection/angles/draw"));
}
