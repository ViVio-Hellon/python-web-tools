/*
  toast.js — 作業を止めない通知

  止める確認(モーダル)は「取り消せない操作」だけに使う。
  確認が多すぎると、慣れて素通りするようになる。
*/

const HOST = () => document.getElementById("toasts");

/** 種類ごとの表示時間(ms)。読む量に比例させる。0 は自動で消さない。 */
const LIFETIME = { ok: 4000, info: 5000, warn: 8000, error: 0 };

/**
 * @param {string} message 現場向けの日本語。文言はサーバが持つ
 * @param {"ok"|"info"|"warn"|"error"} kind
 * @param {{label:string, onClick:Function}} [action] 「元に戻す」など
 */
export function toast(message, kind = "info", action = null) {
  const host = HOST();
  if (!host) return;

  const el = document.createElement("div");
  el.className = `toast toast--${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");

  const text = document.createElement("span");
  text.textContent = message;
  el.appendChild(text);

  if (action) {
    const button = document.createElement("button");
    button.className = "btn";
    button.type = "button";
    button.textContent = action.label;
    button.style.marginLeft = "auto";
    button.addEventListener("click", () => { action.onClick(); el.remove(); });
    el.appendChild(button);
  }

  // エラーは自動で消さない代わりに、閉じる手段を必ず出す
  if (!LIFETIME[kind]) {
    const close = document.createElement("button");
    close.className = "btn";
    close.type = "button";
    close.textContent = "閉じる";
    close.style.marginLeft = "auto";
    close.addEventListener("click", () => el.remove());
    el.appendChild(close);
  }

  host.appendChild(el);
  const life = LIFETIME[kind];
  if (life) setTimeout(() => el.remove(), life);
  return el;
}

export const toastError = (err) =>
  toast(err && err.message ? err.message : String(err), "error");
