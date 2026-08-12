/*
  running.js — 帯の「いま動いているもの」

  【なぜ帯に出すか】
  取り込みは数分かかる。始めた画面(設定)を離れたら終わったかどうかが
  分からない ── 確かめるには戻るしかなく、戻ってしまえば元の作業が
  途切れる。帯に出しておけば、**離れたまま待てる**。

  【何を出すか】
      脈打つ点 … 動いていることそのもの(色を知覚できなくても、
                 動きで「止まっていない」と分かる)
      名前     … 何が動いているか(「梱包資材マスタの取り込み」)
      経過と段 … いま何をしていて、何秒経ったか
      横棒     … どこまで進んだか

  【何を判断しないか】
  文言も進み具合もサーバが返したものをそのまま写します
  (`presenters/settings.job_dict`)。ここでは良し悪しを決めません。
*/

import * as jobs from "./jobs.js";

const el = {};

function mmss(sec) {
  const whole = Math.floor(sec);
  if (whole < 60) return `${whole}秒`;
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function show(state) {
  // 帯は画面ごとに作り直される(`nav.js`)ので、要素は毎回引き直す
  el.box = document.getElementById("rb-running");
  if (!el.box) return;
  el.what = document.getElementById("rb-run-what");
  el.how = document.getElementById("rb-run-how");
  el.fill = document.getElementById("rb-run-fill");

  const job = state.running && state.running[0];
  el.box.hidden = !job;
  if (!job) return;

  el.what.textContent = job.label;
  // 段の名前 + 経過。**何秒待っているかが分かれば、待てるかどうかを
  // 自分で決められる**(離席するか、そばで待つか)
  const note = job.message || job.state_label || "";
  el.how.textContent = note ? `${note} ・ ${mmss(job.elapsed_sec)}`
                            : mmss(job.elapsed_sec);
  el.fill.style.width = `${Math.max(0, Math.min(100, job.pct))}%`;
  // 読み上げにも同じことが伝わるようにする。1秒ごとに読み上げられると
  // 本文が追えなくなるので、読み上げは `title` の1行だけに絞る
  el.box.title = `${job.label} — ${note} ${job.pct}%`;
}

/** 見張りに繋ぐ。`app.js` から1回だけ呼ぶ。 */
export function start() {
  jobs.subscribe(show);
  jobs.start();
}

/** 画面が差し替わったあと、新しい帯に今の状態を書き戻す。 */
export function paint() {
  show(jobs.current());
}
