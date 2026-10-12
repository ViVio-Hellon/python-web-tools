/*
  「ロットを探す」の欄の文字を、サーバが探すときと同じ形にそろえる(見比べるためだけ)。

  サーバ(`lot_browse_session.set_text`)は全角の英数字・記号を半角に、全角の空白を半角にし、
  前後の空白を取ってから探す。画面は**欄の文字を書き換えない** ── IME が変換前の文字を
  持っているあいだに欄を書き換えると、IME の文字がもう一度差し込まれて「hh」「hhh」と
  重なった(現場の声)。この関数は「サーバの文字と同じものか」を
  見比べるときにだけ使う。
*/
export function searchText(text) {
  return String(text ?? "")
    .replace(/[！-～]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xFEE0))
    .replace(/　/g, " ")
    .trim();
}
