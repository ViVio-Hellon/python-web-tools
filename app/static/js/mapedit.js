/*
  図の配置編集(掴んで動かす・角を掴んで大きさを変える)

  棚検索(`views/layout.js`)と簡易在庫(`views/inventory.js`)の2つが
  同じ図を持っている。どちらも「背景の写真の上に名前つきの箱を並べ、
  現場が変わったらずらす」という同じ作りなので、**掴み方はここ1か所**に
  置く。別々に書くと、片方だけ直った操作感が残る。

  【ここが持たないもの】
  動かしてよいかどうか・枠に収める計算・保存するかどうかは**サーバ**が
  持つ(`layout_session` / `pallet_map_session`)。ここがやるのは

      ポインタ座標 → 図の論理座標 の変換
      離すまでのあいだ、その場で見せること
      離したときに「動かした」のか「押した」のかを分けること

  の3つだけ。**離すまでサーバへ送らない**のは、1歩ごとに送ると応答を
  待つあいだ箱が指から遅れるため。
*/

const NS = "http://www.w3.org/2000/svg";

// 角の掴みしろ(図の論理座標)。小さすぎると指で掴めない
const GRIP = 7;

/** 画面座標を図の論理座標へ。`viewBox` の縮尺はブラウザに聞く。 */
export function toPlan(svg, event) {
  const rect = svg.getBoundingClientRect();
  const [minX, minY, width, height] = svg.getAttribute("viewBox")
    .split(/\s+/).map(Number);
  return {
    x: minX + (event.clientX - rect.left) / rect.width * width,
    y: minY + (event.clientY - rect.top) / rect.height * height,
  };
}

/** 箱の右下に付ける掴みしろ。**編集中だけ描く。**

    出しっぱなしにすると、見るだけのときに「掴める」と読めてしまう。 */
export function grip(item) {
  const mark = document.createElementNS(NS, "rect");
  mark.setAttribute("class", "grip");
  mark.setAttribute("x", item.x + item.w - GRIP);
  mark.setAttribute("y", item.y + item.h - GRIP);
  mark.setAttribute("width", GRIP);
  mark.setAttribute("height", GRIP);
  mark.setAttribute("data-grip", item.name);
  return mark;
}

/*
  掴む手を図に付ける。

  opts:
    selector   箱のCSS選択子(".shelf" / ".pos")
    editing()  いま編集中か
    find(name) その箱の {x, y, w, h}。無ければ null
    canDrag(group)  掴んでよいか(省略なら全部掴める)
    onTap(name)     動かさずに離した = 押した
    onMove(name, x, y)
    onResize(name, w, h)
    onSelectionChange(count)  複数選択の件数が変わった(省略可)

  【複数選択(Shift+クリック)】
  1つずつしか動かせない・大きさを変えられないと、同じ列の棚をまとめて
  ずらしたいときに1つずつ掴み直すことになる。Shiftを押しながら箱を
  押すたびに複数選択へ足す/外す(掴みには入らない)。複数選択している
  箱のどれかを掴んで動かす/大きさを変えると、**その分の差**を選んで
  いる箱ぜんぶに掛ける。

  見た目は点線の色を変えるだけ(`.is-multi`)なので、「押しても選べた
  かどうか分からない」という声があった。`onSelectionChange` で件数を
  受け取り、呼び出し側が文字でも出す。
*/
export function attach(svg, opts) {
  let holding = null;
  // 複数選択している箱の名前。Shift+クリックで足す/外す
  const selected = new Set();

  function boxOf(name) {
    return svg.querySelector(
      `${opts.selector}[data-name="${CSS.escape(name)}"]`);
  }

  /** 複数選択の見た目を、いま図にある箱へ付け直す。
   *
   * 呼び出し側は**再描画のたびに**呼ぶこと ── 状態が変わるとSVGは
   * まるごと作り直されるので(サーバが画面ぜんぶを返す設計)、
   * クラスも毎回付け直さないと消えたままになる。
   */
  function markSelected() {
    for (const group of svg.querySelectorAll(opts.selector)) {
      group.classList.toggle("is-multi", selected.has(group.dataset.name));
    }
    // 見た目(点線の色)だけだと、選べたのかどうか分からないという声が
    // あった。文字でも「いま何件選んでいるか」を出す(任意)
    opts.onSelectionChange?.(selected.size);
  }

  /** 掴んでいるあいだ、その場で見せる。サーバには離すまで送らない。 */
  function paint(name, x, y, w, h) {
    const group = boxOf(name);
    if (!group) return;
    const rect = group.querySelector("rect");
    const text = group.querySelector("text");
    rect.setAttribute("x", x);
    rect.setAttribute("y", y);
    rect.setAttribute("width", w);
    rect.setAttribute("height", h);
    if (text) {
      text.setAttribute("x", x + w / 2);
      text.setAttribute("y", y + h / 2);
    }
    const mark = group.querySelector(".grip");
    if (mark) {
      mark.setAttribute("x", x + w - GRIP);
      mark.setAttribute("y", y + h - GRIP);
    }
  }

  function onDown(event) {
    const group = event.target.closest(opts.selector);
    if (!group) return;
    const name = group.dataset.name;

    if (!opts.editing()) {
      if (!opts.canDrag || opts.canDrag(group)) opts.onTap?.(name, group);
      return;
    }
    if (opts.canDrag && !opts.canDrag(group)) return;

    // Shift+クリックは複数選択に足す/外すだけ。掴みには入らない
    // (掴みと同時にやると、選ぼうとしただけで動いてしまう)
    if (event.shiftKey && !event.target.classList.contains("grip")) {
      if (selected.has(name)) selected.delete(name);
      else selected.add(name);
      markSelected();
      event.preventDefault();
      return;
    }

    const item = opts.find(name);
    if (!item) return;

    // 掴んだ箱が複数選択に入っていれば、選択ぜんぶをまとめて動かす。
    // 入っていなければ、複数選択はこの1つに絞ってから掴む
    // (まとめて動かすつもりがないのに、前の選択が紛れ込まないように)
    let names;
    if (selected.has(name) && selected.size > 1) {
      names = [...selected];
    } else {
      selected.clear();
      selected.add(name);
      markSelected();
      names = [name];
    }
    const items = {};
    for (const n of names) {
      const it = n === name ? item : opts.find(n);
      if (it) items[n] = it;
    }

    const point = toPlan(svg, event);
    // 角を掴んだら大きさ、それ以外は位置。**掴んだ場所で決まる**ので、
    // どちらをするのか先に選ばせない
    const resizing = event.target.classList.contains("grip");
    holding = {
      name, resizing, item, items, moved: false,
      dx: point.x - item.x, dy: point.y - item.y,
    };
    svg.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  function onMove(event) {
    if (!holding) return;
    const point = toPlan(svg, event);
    const { item, items } = holding;
    const last = {};
    if (holding.resizing) {
      // 左上は動かさない。掴んだ角だけが動く。複数選択のときは、
      // 掴んだ箱の増減分(差)を選んでいる箱ぜんぶに掛ける
      const w = Math.max(1, point.x - item.x);
      const h = Math.max(1, point.y - item.y);
      const dw = w - item.w, dh = h - item.h;
      for (const [n, it] of Object.entries(items)) {
        const nw = Math.max(1, it.w + dw);
        const nh = Math.max(1, it.h + dh);
        paint(n, it.x, it.y, nw, nh);
        last[n] = { w: nw, h: nh };
      }
    } else {
      const x = point.x - holding.dx;
      const y = point.y - holding.dy;
      const dx = x - item.x, dy = y - item.y;
      for (const [n, it] of Object.entries(items)) {
        const nx = it.x + dx, ny = it.y + dy;
        paint(n, nx, ny, it.w, it.h);
        last[n] = { x: nx, y: ny };
      }
    }
    holding.last = last;
    holding.moved = true;
  }

  async function onUp(event) {
    if (!holding) return;
    const { name, resizing, moved, last } = holding;
    holding = null;
    svg.releasePointerCapture?.(event.pointerId);
    if (!moved || !last) {
      // 動かさずに離したのは「押した」
      opts.onTap?.(name, boxOf(name));
      return;
    }
    // 枠に収めるのはサーバの仕事。返ってきた座標で描き直す。
    // 複数のときは**順に**送る ── 同時に送ると応答が前後して、
    // 最後に描き直った図がどの箱の分か分からなくなる
    for (const [n, value] of Object.entries(last)) {
      if (resizing) await opts.onResize?.(n, value.w, value.h);
      else await opts.onMove?.(n, value.x, value.y);
    }
  }

  svg.addEventListener("pointerdown", onDown);
  svg.addEventListener("pointermove", onMove);
  svg.addEventListener("pointerup", onUp);
  svg.addEventListener("pointercancel", onUp);

  return {
    /** 画面を出直したときに掴みかけと複数選択を捨てる(`nav.js` の再入場)。 */
    reset() { holding = null; selected.clear(); },
    /** 再描画のたびに呼ぶ。複数選択の見た目(`.is-multi`)を付け直す。 */
    reapplySelection() { markSelected(); },
    /**
     * 配置編集をOFFにしたときに呼ぶ。複数選択を捨てないと、次にONに
     * したときも前の選択の輪郭が残ったままになる(現場の声:
     * 「配置編集のシフト+クリック後に配置編集をoffにしてもシフト+クリック
     * の強調表示のままになっている」)。
     */
    clearSelection() { selected.clear(); markSelected(); },
  };
}

/*
  背景の写真の置き方(ずらし量と倍率)

  **箱ではなく写真の側を動かす。** 写真の画角は図の枠と一致しないので、
  枠いっぱいに引き伸ばすと必ずずれる。箱を全部動かして合わせるより、
  下敷きを合わせるほうが早いし、合わせ直しても箱の位置は壊れない。

  掴んで動かす形にはしない ── 箱を掴む操作とぶつかり、どちらが動いたのか
  分からなくなる。数で入れるほうが**同じ量を何度でも**やり直せる。
*/
export function applyBackground(image, map) {
  if (!image) return;
  const scale = Number(map.background_scale) || 1;
  image.setAttribute("x", Number(map.background_x) || 0);
  image.setAttribute("y", Number(map.background_y) || 0);
  image.setAttribute("width", map.width * scale);
  image.setAttribute("height", map.height * scale);
}

/*
  図の拡大縮小(棚検索・簡易在庫で共通の作法)。

  以前は [1, 1.5, 2, 3] の4段階で、押すたびに大きく跳ねた
  (100%→150%のように一気に50%動く)。「1回の変動が大きすぎて
  微調整できない」という現場の声を受けて、**1%刻みで連続的に**
  変えられる形に直した。

  - `+`/`-` ボタンは押した瞬間に1%動き、**押し続けると連続して**動く
    (キーリピートと同じ感覚)
  - 数値(`100%`)を直接クリックすると入力欄に変わり、打ち込める

  範囲は 25%〜400%。25%未満は図が小さすぎて掴めず、400%を超えると
  枠外へすぐスクロールしてしまい実用にならない。
*/
export const ZOOM_MIN = 25;
export const ZOOM_MAX = 400;
const ZOOM_STEP = 1;              // ボタン1回・1ティックあたり(%)
const ZOOM_HOLD_DELAY_MS = 400;   // 押しっぱなし判定までの猶予
const ZOOM_HOLD_INTERVAL_MS = 40; // 連続変化の間隔

function clampZoom(percent) {
  return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(percent)));
}

/**
 * ズームの道具一式を組み立てる。
 *
 * opts:
 *   wrap        図を囲む要素(`--zoom` を持たせる)
 *   zoomIn      `+` ボタン
 *   zoomOut     `-` ボタン
 *   zoomNow     いまの倍率を表示する要素(クリックで入力欄に変わる)
 *   initial     初期値(%)。省略時 100
 *   onChange(percent)  変わるたびに呼ぶ(任意)
 */
export function attachZoom(opts) {
  let percent = clampZoom(opts.initial ?? 100);
  let holdTimer = null;
  let holdInterval = null;

  function paint() {
    opts.wrap.style.setProperty("--zoom", percent / 100);
    opts.zoomNow.textContent = `${percent}%`;
    opts.zoomOut.disabled = percent <= ZOOM_MIN;
    opts.zoomIn.disabled = percent >= ZOOM_MAX;
    opts.onChange?.(percent);
  }

  function setPercent(next) {
    percent = clampZoom(next);
    paint();
  }

  function step(direction) {
    setPercent(percent + direction * ZOOM_STEP);
  }

  /** 押しっぱなしで連続変化。離す/カーソルが外れたら止める。 */
  function startHold(direction) {
    step(direction);
    stopHold();
    holdTimer = setTimeout(() => {
      holdInterval = setInterval(() => step(direction), ZOOM_HOLD_INTERVAL_MS);
    }, ZOOM_HOLD_DELAY_MS);
  }

  function stopHold() {
    if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
    if (holdInterval) { clearInterval(holdInterval); holdInterval = null; }
  }

  for (const [button, direction] of [[opts.zoomIn, 1], [opts.zoomOut, -1]]) {
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      startHold(direction);
    });
    button.addEventListener("pointerup", stopHold);
    button.addEventListener("pointerleave", stopHold);
    button.addEventListener("pointercancel", stopHold);
  }

  // 数値を直接編集。**クリックで入力欄に変わる** ── 常に <input> だと
  // 見た目が数値表示のときと揃わないので、押したときだけ入力欄にする
  opts.zoomNow.setAttribute("role", "button");
  opts.zoomNow.setAttribute("tabindex", "0");
  opts.zoomNow.title = "クリックすると数値を直接入力できます";
  function openInput() {
    const input = document.createElement("input");
    input.type = "number";
    input.className = "zoom__input";
    input.min = String(ZOOM_MIN);
    input.max = String(ZOOM_MAX);
    input.value = String(percent);
    input.inputMode = "numeric";
    const finish = (commit) => {
      if (commit) {
        const value = Number(input.value);
        if (Number.isFinite(value)) setPercent(value);
        else paint();
      } else {
        paint();
      }
    };
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); input.blur(); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
    });
    opts.zoomNow.replaceChildren(input);
    input.focus();
    input.select();
  }
  opts.zoomNow.addEventListener("click", openInput);
  opts.zoomNow.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openInput();
    }
  });

  paint();
  return {
    get: () => percent,
    set: setPercent,
    reset: () => setPercent(100),
  };
}
