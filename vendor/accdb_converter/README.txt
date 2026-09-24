accdb_converter の読み取り部品(同梱)

出どころ: ViVio-Hellon/accdb_converter  コミット b051628 (2026-09-14)
写したファイル(中身は変えていない):
  engine.py  jet_text_fix.py  writers.py  xlsx_writer.py  xlsx_reader.py

設定画面の「表を持ってくる」で Access(.accdb / .mdb)を選んだとき、
packaging_tool/table_bring.py がここを別のプロセスで動かして sqlite3 に変換する
(engine.read_source → writers.write_sqlite)。変換ツールと同じ部品なので、
型や中身の扱いはいつもの変換と同じになる。

Access を読むには、このPCの Python に次のどちらかが要る(変換ツールと同じ):
  - pyodbc + Microsoft Access Driver(ACE)… 最も正確。Office の Access があれば入っている
  - access_parser(construct, tabulate)   … ドライバ不要の予備。まれに行を読み違える

変換ツール側を直したら、上の5ファイルをここへ写し直し、コミットを書き換えること。
