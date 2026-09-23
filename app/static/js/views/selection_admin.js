/**
 * 管理者・実績パターン・ボード使用実績の段
 *
 * **この段のことはこの中で完結させる。** 描くのも押されたときの
 * 投げ返しもここが持ち、外へ出すのは `mount` と `render` だけ。
 *
 * 認証の入力欄は「設定」画面にある。ここは結果を映すだけで、
 * **パスワードは1文字も扱わない**(設計書 §3.6)。
 */
const el = {};
// 本体から渡してもらうもの
let send = null;
let showAsk = null;   // 取り返しのつかない操作の前に訊く
let why = null;
// 保存の前に訊く文。**文はサーバが持つ**(`admin.save_confirm`)
let saveConfirm = null;

export function render(admin) {
  if (!admin) return;
  saveConfirm = admin.save_confirm || null;

  el.adminState.textContent = admin.authenticated ? "認証済み" : "未認証";
  // 認証が通っていれば、置き場所を案内する文はもう要らない
  if (el.authWhere) el.authWhere.hidden = admin.authenticated;
  el.savePattern.disabled = !admin.can_save;
  why(el.saveWhy, admin.save_why);
  el.patternsNote.textContent = admin.patterns_note;

  el.patternRows.replaceChildren(...admin.patterns.map((p) => {
    const tr = document.createElement("tr");
    for (const value of [p.id, p.product, p.boards]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(td);
    }
    // ボードの内訳は長い。省略して**「読込」を押せる位置に残す** ──
    // 押せないところへ追いやると、一覧に出ている意味が無い。
    // 全文は押さえたままにする(title)
    const boards = tr.lastElementChild;
    boards.className = "clip";
    boards.title = p.boards;
    const count = document.createElement("td");
    count.className = "n";
    count.textContent = p.usage_count;
    const at = document.createElement("td");
    at.textContent = p.registered_at;
    // まだ共有(取り込み元)へ届いていない実績。**ほかの端末からは
    // まだ見えない**ことを、保存した人が分かるようにする
    if (p.unsent) {
      const mark = document.createElement("span");
      mark.className = "st";
      mark.textContent = " この端末だけ";
      mark.title = "共有(取り込み元)へまだ送れていません。送れるとほかの端末からも見えます";
      at.appendChild(mark);
    }

    const cell = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn--find rowdel";
    button.textContent = "読込";
    button.dataset.pattern = p.id;
    cell.appendChild(button);
    // 削除(VBA `frmPatterns.btnDelete_Click`)。**認証したときだけ出す**
    // ── 押せないボタンを並べても操作が増えるだけ。
    //
    // 見るのは `can_save` ではなく `authenticated`。`can_save` は
    // 「認証してある**かつ**配置してある」で、消すのに配置は要らない
    // (消したい実績は、たいてい今の作業とは別物)
    if (admin.authenticated) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn--danger rowdel";
      del.textContent = "削除";
      del.dataset.deletePattern = p.id;
      del.dataset.label = `No.${p.id}  ${p.product}  ${p.boards}`;
      cell.appendChild(del);
    }
    tr.append(count, at, cell);
    return tr;
  }));

  el.usageRows.replaceChildren(...admin.usage.map((u) => {
    const tr = document.createElement("tr");
    const type = document.createElement("td");
    type.textContent = u.board_type;
    tr.appendChild(type);
    for (const value of [u.width, u.length, u.usage_count]) {
      const td = document.createElement("td");
      td.className = "n";
      td.textContent = value;
      tr.appendChild(td);
    }
    const at = document.createElement("td");
    at.textContent = u.last_used_at;
    tr.appendChild(at);
    return tr;
  }));
}

export function mount(api) {
  ({ send, showAsk, why } = api);
  for (const id of ["adminState", "authWhere", "savePattern", "saveWhy",
                    "patternsNote", "patternRows", "usageRows"]) {
    el[id] = document.getElementById(id);
  }

  // --- 管理者 -------------------------------------------------------
  // 認証の入力欄は「設定」画面に移した。ここは結果を
  // 映すだけ(プロセスに1つの状態なので、どちらで通しても同じ)
  // 保存の前に、何を保存するのかを見せて訊く(VBA の確認 MsgBox)
  el.savePattern.addEventListener("click", () => {
    if (!saveConfirm || !saveConfirm.title) {
      send("/api/selection/pattern/save");  // 断りの理由はサーバが返す
      return;
    }
    showAsk({
      title: saveConfirm.title,
      body: saveConfirm.body,
      choices: [{ key: "cancel", label: "やめる" },
                { key: "save", label: "保存する" }],
    }, (key) => {
      if (key === "save") send("/api/selection/pattern/save");
    });
  });
  el.patternRows.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-pattern]");
    if (button) {
      send("/api/selection/pattern/load", { id: button.dataset.pattern });
      return;
    }
    // 削除は取り消せない。**何を消すのかを見せてから訊く**
    // (VBA も 管理番号 を出して Yes/No、既定は「いいえ」だった)
    const del = event.target.closest("button[data-delete-pattern]");
    if (!del) return;
    showAsk({
      title: "この実績を削除しますか？",
      body: `${del.dataset.label}\n削除すると元に戻せません。`,
      choices: [{ key: "cancel", label: "やめる" },
                { key: "delete", label: "削除する", note: "元に戻せません" }],
    }, (key) => {
      if (key !== "delete") return;
      send("/api/selection/pattern/delete", { id: del.dataset.deletePattern });
    });
  });
}
