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
