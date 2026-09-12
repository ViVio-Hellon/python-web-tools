/*
  tabs.js — スクロールの代わりにタブで分ける

  【なぜタブか】
  縦に積んだものは、下にあるほど「無いもの」として扱われる。
  スクロールしないと見えないということは、**見えていないあいだは
  存在を思い出せない**ということで、設定画面のように「どこかに
  あるはず」を探す画面では特に効く(設計指針 §4.2 / §1.4)。

  タブなら、**中身を隠しても見出しは常に見えている**。何があるかは
  一覧できて、いま見ていないものも忘れずに済む。

  【タブが守らなければならないこと】
  1. **問題を隠さない。** 中身に「足りません」があるタブは、開いて
     いなくても見出しがそう言う。隠したせいで気づけなくなるなら、
     スクロールのほうがまだましになる
  2. **その先に何があるかを示す**(情報の匂い、§2.4)。件数や状態を
     見出しに添える
  3. **色だけで伝えない**(§3.8)。選ばれているタブは下線と太字でも
     分かる。状態は文言でも出す
  4. **キーボードで回れる**。矢印キーで移動、Home/End で端へ

  【どこまでが画面の仕事か】
  タブの**並び・見出し・状態・バッジはサーバが決めた値**を写すだけ。
  「いまどのタブを見ているか」は業務の事実ではないので画面が持つ
  (画面ごとに `sessionStorage` へ覚え、戻ってきたとき続きから見られる)。
*/

const STORE_PREFIX = "tab:";

/** 選んでいるタブを覚える。業務の状態ではないので画面側で持つ。 */
function remember(group, key) {
  try {
    sessionStorage.setItem(STORE_PREFIX + group, key);
  } catch (e) {
    // プライベートモード等で使えないことがある。覚えられないだけで
    // 動きは変わらないので、黙って諦める
  }
}

function recall(group) {
  try {
    return sessionStorage.getItem(STORE_PREFIX + group);
  } catch (e) {
    return null;
  }
}

/**
 * 外部リンクが名指ししてきたタブ(`?tab=status` 等)。
 *
 * これが要るのは、帯のリンク(`base.html`)のように「この面を直接
 * 開かせたい」外部リンクがあるため。**前回覚えていたタブより優先する**
 * ── 覚えていた面を出しても、リンクが指した先が見えなければ、
 * リンクを踏んだ意味が無い。
 */
function requested() {
  try {
    return new URLSearchParams(location.search).get("tab");
  } catch (e) {
    return null;
  }
}

/** タブを1つ選ぶ。`key` が無ければ何もしない(消えたタブを覚えていた等)。 */
export function select(root, key) {
  const tabs = [...root.querySelectorAll(":scope > .tabs__bar > .tab")];
  const target = tabs.find((t) => t.dataset.key === key);
  if (!target) return false;

  for (const tab of tabs) {
    const on = tab === target;
    tab.setAttribute("aria-selected", String(on));
    // 選ばれていないタブはタブ順から外す。Tab キーは**タブ列を1つ**として
    // 扱い、中の移動は矢印キー ── これが tablist の作法
    tab.tabIndex = on ? 0 : -1;
    const panel = root.querySelector(`#${CSS.escape(tab.getAttribute("aria-controls"))}`);
    if (panel) panel.hidden = !on;
  }
  remember(root.dataset.tabs, key);
  return true;
}

/** いま選ばれているタブのキー。 */
export function current(root) {
  const on = root.querySelector(':scope > .tabs__bar > .tab[aria-selected="true"]');
  return on ? on.dataset.key : "";
}

function move(root, from, step) {
  const tabs = [...root.querySelectorAll(":scope > .tabs__bar > .tab")];
  if (!tabs.length) return;
  const at = tabs.indexOf(from);
  const next = tabs[(at + step + tabs.length) % tabs.length];
  select(root, next.dataset.key);
  next.focus();
}

/** 1組のタブを動かす。すでに動かしてあるものは触らない。 */
export function attach(root) {
  if (!root || root.dataset.tabsReady === "1") return;
  root.dataset.tabsReady = "1";

  // `closest()` に `:scope` は効かない(呼び出した要素自身を指すため、
  // `:scope > ...` は決して一致しない)。素直に `.tab` を拾ってから、
  // **この組のものか**を確かめる ── 入れ子のタブがあっても混ざらない
  const own = (node) => {
    const tab = node.closest(".tab");
    return tab && tab.parentElement
        && tab.parentElement.parentElement === root ? tab : null;
  };

  root.addEventListener("click", (event) => {
    const tab = own(event.target);
    if (tab) select(root, tab.dataset.key);
  });

  root.addEventListener("keydown", (event) => {
    const tab = own(event.target);
    if (!tab) return;
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (step) { event.preventDefault(); move(root, tab, step); return; }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const tabs = [...root.querySelectorAll(":scope > .tabs__bar > .tab")];
      const target = event.key === "Home" ? tabs[0] : tabs[tabs.length - 1];
      select(root, target.dataset.key);
      target.focus();
    }
  });

  // URLが名指ししたタブ最優先、次に前回見ていたタブ、
  // 無ければ**サーバが既定にした**もの
  const wanted = requested();
  if (wanted && select(root, wanted)) return;
  const saved = recall(root.dataset.tabs);
  if (!saved || !select(root, saved)) {
    const first = root.querySelector(':scope > .tabs__bar > .tab[data-default="1"]')
               || root.querySelector(":scope > .tabs__bar > .tab");
    if (first) select(root, first.dataset.key);
  }
}

/** 画面の中のタブを全部動かす。 */
export function attachAll(scope = document) {
  for (const root of scope.querySelectorAll(".tabs")) attach(root);
}

/**
 * タブ見出しの状態とバッジを書き換える。
 *
 * **中身の一番重い状態を見出しが背負う。** 開いていないタブに
 * 「足りません」があることが、見出しだけで分かるようにする。
 * 値の出どころはサーバで、ここは写すだけ。
 */
export function setBadges(root, badges) {
  // **先に全部消してから付け直す。**
  //
  // 渡されたぶんだけ書いていたので、**消えた印が消えなかった**
  // ── 取り込み元が見つかるようになっても「できません」が出たままで、
  // 画面を丸ごと読み込み直すまで消えない(現場の指摘:「今の状態を
  // リロードしたら できません が消えた」)。直っても直ったと言わない
  // 画面は、次から誰も信じない
  for (const tab of root.querySelectorAll(":scope > .tabs__bar > .tab")) {
    delete tab.dataset.level;
    const badge = tab.querySelector(".tab__badge");
    if (!badge) continue;
    badge.textContent = "";
    badge.hidden = true;
    delete badge.dataset.level;
  }

  for (const [key, info] of Object.entries(badges || {})) {
    const tab = root.querySelector(
      `:scope > .tabs__bar > .tab[data-key="${CSS.escape(key)}"]`);
    if (!tab) continue;
    if (info.level) tab.dataset.level = info.level;
    const badge = tab.querySelector(".tab__badge");
    if (!badge) continue;
    badge.textContent = info.text || "";
    badge.hidden = !info.text;
    if (info.level) badge.dataset.level = info.level;
  }
}
