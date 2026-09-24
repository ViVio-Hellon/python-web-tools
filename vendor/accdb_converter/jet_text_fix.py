# -*- coding: utf-8 -*-
"""jet_text_fix.py  ── access_parser の日本語文字化けを直す

【何が起きているか】
Access(Jet)はテキストを2通りで持ちます。

    非圧縮 … UTF-16LE そのまま。**全部が日本語**の値はこちら
    圧縮   … 先頭に FF FE を置き、1文字1バイトで詰める。**全部がASCII**は
             こちら。ASCIIと日本語が混ざる値は、値の途中に 00 を挟んで
             「1バイト ⇔ 2バイト」を切り替えながら詰める

access_parser 0.0.6 は3つ目(切り替えあり)を読めません。FF FE を外した
残り全部を1バイト文字とみなし、UTF-8で読めなければ **latin1** で読みます。
latin1 はどんなバイトでも例外を出さないので、**気づかないまま化けます**。

    本当の値          JISHﾌﾗ ﾗﾃANF
    access_parser     JISH\x8cÿ\x97ÿ \x97ÿ\x83ÿANF   ← 画面では JISH□y□y □y□yANF

「全部が日本語」の値(ｼｬｰｼ / 大ラベル)は非圧縮なので無事です。だから
**混在する値を持つ表だけ**が化けます(仕掛台帳の用途名など)。

【使い方】
このファイルを accdb_converter フォルダへ置き、engine.py の
`_read_with_access_parser` の先頭に1行足してください。

    def _read_with_access_parser(path):
        import jet_text_fix; jet_text_fix.apply()      # ← これ
        from access_parser import AccessParser
        ...

pyodbc + ACE 経路で変換している場合は、この問題は起きません(そちらが
読めるならそちらが忠実です)。
"""

TEXT_TYPE = 0x0A          # access_parser の TYPE_TEXT


def decode_jet_text(buffer: bytes) -> str:
    """Jet のテキスト1つを、格納の仕方どおりに読む。"""
    if not buffer:
        return ""
    # 圧縮の印。**ここから先は1バイト文字**で始まる
    if buffer[:2] in (b"\xff\xfe", b"\xfe\xff"):
        out = []
        wide = False                      # いまが2バイト(UTF-16LE)かどうか
        i = 2
        while i < len(buffer):
            if buffer[i] == 0x00:         # 切り替えの合図
                wide = not wide
                i += 1
                continue
            if wide:
                pair = buffer[i:i + 2]
                if len(pair) < 2:
                    break
                out.append(pair.decode("utf-16-le", "replace"))
                i += 2
            else:
                out.append(chr(buffer[i]))
                i += 1
        return "".join(out)
    # 非圧縮。UTF-16LE そのまま
    text = buffer.decode("utf-16-le", "replace")
    return text.split("\x00")[0] if "\x00" in text else text


def apply() -> bool:
    """access_parser のテキスト読み取りを差し替える。**1回だけ効く。**"""
    from access_parser import utils

    if getattr(utils, "_jet_text_fixed", False):
        return False

    original = utils.parse_type

    def parse_type(data_type, buffer, length=None, version=4, **kwargs):
        if data_type == TEXT_TYPE:
            raw = buffer[:length] if length is not None else buffer
            return decode_jet_text(bytes(raw))
        return original(data_type, buffer, length=length, version=version,
                        **kwargs)

    utils.parse_type = parse_type
    utils._jet_text_fixed = True

    # 表を読む側が `from .utils import parse_type` で取り込んでいる場合に備える
    try:
        from access_parser import access_parser as ap_module
        if hasattr(ap_module, "parse_type"):
            ap_module.parse_type = parse_type
    except Exception:                      # noqa: BLE001 - 差し替えで止めない
        pass
    return True
