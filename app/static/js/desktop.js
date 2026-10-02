/*
  desktop.js — デスクトップ版(Tauri)のときだけ、窓まわりを外枠(Rust)に頼む

  ブラウザ版では今までどおり `window.open` を使う。デスクトップ版の画面は
  外枠の窓の中にあるので、別の窓を開く・閉じる・社内サイトを既定のブラウザで
  開く は外枠の仕事になる(`src-tauri/src/main.rs` の `open_window` ほか)。
  どちらで動いているかは `window.__TAURI__` があるかで分かる。
*/

const tauri = window.__TAURI__;

/** デスクトップ版で動いているか。 */
export const isDesktop = Boolean(tauri && tauri.core && tauri.core.invoke);

/**
 * アプリの中のページ(帳票など)を別の窓で開く。開けたら true。
 * `url` は `/report/plan?t=…` のようなアプリの中の経路。
 */
export function openWindow(url, title = "") {
  if (isDesktop) {
    tauri.core.invoke("open_window", { url, title: title || null })
      .catch((err) => console.error("窓を開けませんでした", err));
    return true;
  }
  return Boolean(window.open(url, "_blank"));
}

/** アプリの外のページ(社内の閲覧システムなど)を開く。 */
export function openExternal(url) {
  if (isDesktop) {
    tauri.core.invoke("open_external", { url })
      .catch((err) => console.error("開けませんでした", err));
    return;
  }
  window.open(url, "_blank", "noopener");
}

/**
 * `target="_blank"` のリンクを、デスクトップ版では外枠に開いてもらう。
 * (外枠の窓は新しいタブを持たないので、そのままでは何も起きない)
 */
export function watchLinks() {
  if (!isDesktop) return;
  document.addEventListener("click", (event) => {
    const link = event.target.closest && event.target.closest('a[target="_blank"]');
    if (!link || !link.href) return;
    event.preventDefault();
    const url = new URL(link.href, location.href);
    if (url.origin === location.origin) openWindow(url.pathname + url.search);
    else openExternal(url.href);
  });
}
