/*
  全角の英数字・記号を半角に、英字を大文字にそろえる(「ロットを探す」の欄)。

  仕掛台帳のロット番号は全部「半角の大文字英字と数字の7桁」(実データ 6,948件で確かめた)。
  IME が日本語のまま「Ｒ６５」と打つと見つからなかった。かな・漢字は変えない
  (用途名などで日本語のまま探せるように)。文字数は変わらないので、カーソルの位置はそのまま使える。
*/
export function halfUpperAscii(text) {
  return String(text ?? "")
    .replace(/[！-～]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xFEE0))
    .replace(/　/g, " ")
    .replace(/[a-z]/g, (c) => c.toUpperCase());
}
