"""起動待機画面 (基盤仕様書 2.2 / 2.3)

【なぜ独立したモジュールなのか】
この画面は **Flask が読み込まれる前に**出さなければなりません。
起動でいちばん時間を食うのは Flask とアプリ本体の import で、
そのあとに待機画面を出していたら「待たせるために見せるもの」を
待たせていることになります ── 出す意味がありません。

そこでここは **標準ライブラリだけ**で組み、`start_app` が最小の
WSGI で先に出せるようにしてあります。本体が組み上がったあとも
同じ関数を Flask 側の `/` が呼ぶので、**骨格は1か所**です。

【1往復で出す】
外部への要求(CSS・画像・書体)を1つも出しません。色は
`app/static/css/tokens.css` を読んで**そのまま埋め込みます** ──
色の出どころを2つにしないための読み込みで、失敗しても手元の
控えで描けます。

【造形】
帯(アプリバー)と同じ濃紺〜ティールの地に置きます。起動直後に
出るものが本体と地続きに見えるので、「別のアプリが開いた」と
受け取られません。段は横に4つ並べ、進み具合は1本の帯で示します。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# `tokens.css` の場所。**色の出どころはここ1つ**(設計指針 §3.8)
_TOKENS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "tokens.css"

# `tokens.css` を読めなかったときの控え。帯の色だけを持つ。
# 起動画面が出ないよりは、色が少し違ってでも出るほうがよい
_FALLBACK = """
:root{
  --bar-a:#153247; --bar-b:#1e6079; --bar-ink:#ffffff; --bar-muted:#c3dbe4;
  --bar-face:#ffffff14; --bar-edge:#ffffff33; --bar-focus:#7fe3ea;
  --bar-ok:#8ff0c4; --bar-alert:#ffc4bf;
  --sans:"Yu Gothic UI","Meiryo UI",system-ui,sans-serif;
  --mono:ui-monospace,Consolas,"Courier New",monospace;
}
"""

_tokens_cache: Optional[str] = None


def tokens_css() -> str:
    """色の定義。1度読んで覚える(起動のたびに何度も読まない)。"""
    global _tokens_cache
    if _tokens_cache is None:
        try:
            _tokens_cache = _TOKENS.read_text(encoding="utf-8")
        except OSError:
            _tokens_cache = _FALLBACK
    return _tokens_cache


# ==================================================================
# 造形
# ==================================================================
_STYLE = """
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; display:grid; place-items:center; padding:24px;
  font-family:var(--sans); color:var(--bar-ink);
  /* 地は帯と同じ濃紺〜ティール。**光は角から差す**ようにして、
     真ん中に置いた札が浮いて見えるようにする。中央を明るくすると
     札と地の境が消え、札が「置いてある」ように見えない */
  background:
    radial-gradient(70% 55% at 8% 0%, #ffffff1a 0%, transparent 60%),
    radial-gradient(60% 50% at 100% 100%,
      color-mix(in srgb, var(--bar-focus) 34%, transparent) 0%, transparent 58%),
    radial-gradient(115% 95% at 50% 45%, transparent 34%, #00121c66 100%),
    linear-gradient(118deg, var(--bar-a) 0%, var(--bar-b) 78%, var(--bar-a) 100%);
  background-color:var(--bar-a);
  -webkit-font-smoothing:antialiased;
}
/* 地に薄く流れる光。**進んでいる感じ**を地のほうで持たせて、
   段の表示は事実だけを出せるようにする */
body::after{
  content:""; position:fixed; inset:-45%;
  background:conic-gradient(from 210deg at 50% 50%,
    transparent 0deg, #ffffff12 70deg, transparent 150deg, #ffffff0a 250deg, transparent 360deg);
  animation:drift 26s linear infinite; pointer-events:none;
}
@keyframes drift{to{transform:rotate(1turn)}}

.boot{
  position:relative; z-index:1;
  width:min(560px, 100%);
  background:var(--bar-face);
  border:1px solid var(--bar-edge);
  border-radius:18px;
  padding:34px 34px 28px;
  box-shadow:0 24px 70px #00131e5c, inset 0 1px 0 #ffffff1f;
  backdrop-filter:blur(14px) saturate(120%);
  -webkit-backdrop-filter:blur(14px) saturate(120%);
}

/* 名前と版 */
.brand{display:flex; align-items:center; gap:12px; margin-bottom:26px}
.logo{
  flex:0 0 auto; width:42px; height:42px; border-radius:12px;
  display:grid; place-items:center;
  background:linear-gradient(150deg,#ffffff2e,#ffffff0d);
  border:1px solid var(--bar-edge);
  box-shadow:inset 0 1px 0 #ffffff33;
}
.logo svg{width:22px; height:22px; display:block}
.names{min-width:0}
.name{
  margin:0; font-size:19px; font-weight:700; letter-spacing:.02em;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.ver{
  display:inline-block; margin-top:3px;
  font-family:var(--mono); font-variant-numeric:tabular-nums;
  font-size:11px; font-weight:700; letter-spacing:.06em;
  padding:2px 8px; border-radius:999px;
  background:var(--bar-face); border:1px solid var(--bar-edge); color:var(--bar-muted);
}

/* いま何をしているか。**1行で言い切る** */
.now{display:flex; align-items:baseline; gap:10px; margin:0 0 10px}
.now b{font-size:15px; font-weight:600}
.pct{
  margin-left:auto; font-family:var(--mono); font-variant-numeric:tabular-nums;
  font-size:12px; color:var(--bar-muted);
}

/* 進み具合の帯。割合が分からないあいだは往復する */
.bar{
  height:5px; border-radius:999px; overflow:hidden; position:relative;
  background:#ffffff1f;
}
.bar i{
  position:absolute; inset:0 auto 0 0; width:32%; border-radius:999px;
  background:linear-gradient(90deg, var(--bar-focus), #ffffff);
  box-shadow:0 0 14px #7fe3ea80;
  transition:width .45s cubic-bezier(.22,.9,.3,1);
}
.bar[data-mode="wait"] i{animation:sweep 1.5s cubic-bezier(.55,0,.35,1) infinite}
@keyframes sweep{
  0%{left:-34%; width:34%} 60%{left:100%; width:34%} 100%{left:100%; width:34%}
}

/* 段。横に4つ。**開く前に全体の長さが見える** */
.steps{
  list-style:none; display:grid; grid-template-columns:repeat(4,1fr);
  gap:8px; margin:22px 0 0; padding:0;
}
.steps li{
  display:flex; flex-direction:column; gap:7px;
  font-size:11px; color:var(--bar-muted); letter-spacing:.02em;
}
.steps li .tick{
  height:3px; border-radius:999px; background:#ffffff26;
  transition:background .3s ease;
}
.steps li[data-state="done"]{color:var(--bar-ink)}
.steps li[data-state="done"] .tick{background:var(--bar-ok)}
.steps li[data-state="active"]{color:var(--bar-ink); font-weight:700}
.steps li[data-state="active"] .tick{background:var(--bar-focus)}
.steps li[data-state="error"]{color:var(--bar-alert,#ffc4bf)}
.steps li[data-state="error"] .tick{background:var(--bar-alert,#ffc4bf)}

/* 待たずに入る道。**主役ではない** */
.skip{margin:20px 0 0; text-align:right}
.skip a{
  display:inline-block; padding:7px 13px; border-radius:999px;
  color:var(--bar-ink); font-size:12px; text-decoration:none;
  border:1px solid var(--bar-edge); background:#ffffff0f;
  transition:background-color .18s ease, color .18s ease, border-color .18s ease;
}
.skip a:hover{color:var(--bar-ink); background:var(--bar-face); border-color:var(--bar-edge)}
.skip a:focus-visible{outline:2px solid var(--bar-focus); outline-offset:2px}

/* 困ったとき */
.trouble{
  margin:20px 0 0; padding:14px 16px; border-radius:12px;
  background:#ffffff1a; border:1px solid var(--bar-edge);
  border-left:3px solid var(--bar-alert,#ffc4bf);
  font-size:13px; line-height:1.7;
}
.trouble[hidden]{display:none}
.trouble h2{margin:0 0 4px; font-size:14px; color:var(--bar-alert,#ffc4bf)}
.trouble p{margin:0}
.trouble code{
  font-family:var(--mono); font-size:11px; word-break:break-all;
  background:#00000033; padding:2px 6px; border-radius:5px;
}

@media (prefers-reduced-motion:reduce){
  body::after{animation:none}
  .bar[data-mode="wait"] i{animation:none; left:0; width:100%}
  .bar i{transition:none}
}
@media (max-width:480px){
  .boot{padding:26px 22px 22px; border-radius:14px}
  .steps{grid-template-columns:repeat(2,1fr); gap:10px}
}
"""

# 段。**4つだけ**にしてある(基盤仕様書 2.2)。
# 進捗率を正確に出すことではなく、無反応に見える時間をなくすのが目的
STEPS = (
    ("env", "実行環境"),
    ("prepare", "準備"),
    ("import", "取り込み"),
    ("done", "完了"),
)

_LOGO = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 2.6 21 7v10l-9 4.4L3 17V7z"/>'
    '<path d="M3 7l9 4.4L21 7"/><path d="M12 11.4v10"/></svg>'
)


def render(*, display_name: str, version_label: str, token: str, app_id: str,
           poll_ms: int, home_url: str) -> str:
    """待機画面のHTML。**呼ぶ側の都合を持たない**ので、起動サーバと
    Flask の両方が同じものを出せる。
    """
    steps = "".join(
        f'<li data-step="{key}" data-state="wait">'
        f'<span class="tick"></span><span>{label}</span></li>'
        for key, label in STEPS)
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{_esc(display_name)} — 起動中</title>
<style>{tokens_css()}</style>
<style>{_STYLE}</style>
</head>
<body>
<main class="boot" role="status" aria-live="polite">
  <div class="brand">
    <span class="logo">{_LOGO}</span>
    <span class="names">
      <h1 class="name">{_esc(display_name)}</h1>
      <span class="ver">{_esc(version_label)}</span>
    </span>
  </div>

  <p class="now"><b id="nowText">起動しています</b><span class="pct" id="pct"></span></p>
  <div class="bar" id="bar" data-mode="wait"
       role="progressbar" aria-label="起動の進み具合"><i id="fill"></i></div>

  <ol class="steps" id="steps">{steps}</ol>

  <p class="skip"><a id="skip" href="{_esc(home_url)}">待たずに使い始める ▸</a></p>

  <div class="trouble" id="trouble" hidden>
    <h2 id="troubleTitle">起動できませんでした</h2>
    <p id="troubleBody"></p>
    <p style="margin-top:8px">ログ: <code>%LOCALAPPDATA%\\PackagingTool\\logs\\</code></p>
  </div>
</main>
<script>{_SCRIPT}</script>
<script>
bootWatch({{
  pollMs: {int(poll_ms)},
  appId: {json.dumps(app_id)},
  token: {json.dumps(token)},
  home: {json.dumps(home_url)}
}});
</script>
</body>
</html>
"""


_SCRIPT = r"""
/*
  起動待機画面のふるまい。

  一定秒数の経過では完了扱いにせず、**必ずバックエンドの応答を確かめる**
  (基盤仕様書 2.2)。さらに app_id を照合して、同じポートに居る別の
  アプリを自分だと誤認しない(同 2.3)。
*/
function bootWatch(opt) {
  var steps = document.getElementById("steps");
  var bar = document.getElementById("bar");
  var fill = document.getElementById("fill");
  var nowText = document.getElementById("nowText");
  var pct = document.getElementById("pct");
  var trouble = document.getElementById("trouble");
  var misses = 0;
  var order = ["env", "prepare", "import", "done"];

  document.getElementById("skip").setAttribute("href", opt.home);

  function setStep(name, state) {
    var li = steps.querySelector('[data-step="' + name + '"]');
    if (li) li.dataset.state = state;
  }
  /** その段まで済み、その段が進行中、という形にまとめて当てる。 */
  function upto(name) {
    var at = order.indexOf(name);
    order.forEach(function (key, i) {
      setStep(key, i < at ? "done" : (i === at ? "active" : "wait"));
    });
  }
  function progress(value) {
    if (value === null) { bar.setAttribute("data-mode", "wait"); pct.textContent = ""; return; }
    bar.setAttribute("data-mode", "run");
    fill.style.left = "0";
    fill.style.width = Math.max(4, Math.min(100, value)) + "%";
    bar.setAttribute("aria-valuenow", String(Math.round(value)));
    pct.textContent = Math.round(value) + "%";
  }
  function fail(title, body) {
    document.getElementById("troubleTitle").textContent = title;
    document.getElementById("troubleBody").textContent = body;
    trouble.hidden = false;
    var active = steps.querySelector('[data-state="active"]');
    if (active) active.dataset.state = "error";
    bar.setAttribute("data-mode", "run");
    fill.style.width = "100%";
    fill.style.background = "var(--bar-alert, #ffc4bf)";
  }

  upto("prepare");

  function poll() {
    fetch("/api/health", { cache: "no-store" }).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    }).then(function (health) {
      if (health.app_id !== opt.appId) {
        fail("別のアプリが同じポートを使っています",
             "期待した " + opt.appId + " ではなく " + health.app_id + " が応答しました。");
        return;
      }
      misses = 0;

      if (health.startup_error) { fail("起動できませんでした", health.startup_error); return; }
      if (health.ready) {
        order.forEach(function (k) { setStep(k, "done"); });
        nowText.textContent = "起動しました";
        progress(100);
        location.replace("/?t=" + encodeURIComponent(opt.token));
        return;
      }

      // **文言はサーバが持っている**ので写すだけ。組み立て直さない
      if (health.job) {
        upto("import");
        nowText.textContent = health.job.message || health.job.label || "取り込み中";
        progress(typeof health.job.pct === "number" ? health.job.pct : null);
      } else {
        upto(health.stage_key || "prepare");
        nowText.textContent = health.stage || "起動しています";
        progress(null);
      }
      setTimeout(poll, opt.pollMs);
    }).catch(function () {
      // 起動直後は数回失敗して当たり前。続くようなら理由を出す
      if (++misses >= 25) {
        fail("バックエンドに接続できません",
             "起動処理が終わらないか、途中で停止した可能性があります。");
        return;
      }
      setTimeout(poll, opt.pollMs);
    });
  }
  poll();
}
"""


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
