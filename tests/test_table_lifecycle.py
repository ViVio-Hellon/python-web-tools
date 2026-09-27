"""表まわりを通しで(表を足す・消す・行を足す・直す・消す・入れ替える・作り直す)。

1つずつの操作は `test_web_settings.TableBringTests` / `TableRefreshTests` と
`test_web_master` が見ている。ここでは**続けて使ったとき**に食い違いが
出ないかを見る ── 足した表を直してから入れ替える、作り直してから直す、
消してからもう一度持ってくる、画面を開いたまま別の操作が入る、など。
"""
from __future__ import annotations

import sqlite3
import unittest

from tests.test_web_settings import TableBringTests

BROUGHT = "新しい表"
REGISTRY = "ツールで足した表"


class LifecycleBase(TableBringTests):
    # 親の試験は親のクラスで流れている。ここでは道具(setUp と手)だけ借りる
    locals().update({name: None for name in dir(TableBringTests) if name.startswith("test_")})

    def setUp(self) -> None:
        super().setUp()
        self.session.admin = True

    # -- 道具 ---------------------------------------------------------
    def access(self, *sqls: str) -> None:
        """Access 側(変換したファイル)を変える。"""
        conn = sqlite3.connect(self.converted)
        for sql in sqls:
            conn.execute(sql)
        conn.commit()
        conn.close()

    def shared(self, *sqls: str) -> None:
        conn = sqlite3.connect(self.master)
        for sql in sqls:
            conn.execute(sql)
        conn.commit()
        conn.close()

    def post(self, path: str, body: dict, expect: int = 200) -> dict:
        res = self.client.post(path, headers=self.auth(), json=body)
        self.assertEqual(res.status_code, expect, res.get_json())
        return res.get_json()

    def browse(self, table: str) -> dict:
        res = self.client.get("/api/master/browse", query_string={"table": table},
                              headers=self.auth())
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def keys(self, table: str, column: str) -> dict:
        """画面に出ている行の {列の値: 行の鍵}。"""
        from packaging_tool import master_admin
        return {r[column]: r[master_admin.ROW_KEY]
                for r in self.browse(table)["page"]["rows"]}

    def add(self, table: str, values: dict, expect: int = 200) -> dict:
        return self.post("/api/master/row/add", {"table": table, "values": values}, expect)

    def save(self, table: str, key, values: dict, expect: int = 200, *, was=None) -> dict:
        return self.post("/api/master/row/save",
                         {"table": table, "key": key, "values": values, "was": was}, expect)

    def delete(self, table: str, key, expect: int = 200, *, was=None) -> dict:
        return self.post("/api/master/row/delete",
                         {"table": table, "key": key, "was": was}, expect)

    def rows_seen(self, table: str, column: str) -> dict:
        """画面に出ている行の {列の値: (行の鍵, 行)}。画面はこの行を `was` で送る。"""
        from packaging_tool import master_admin
        out = {}
        for row in self.browse(table)["page"]["rows"]:
            key = row.pop(master_admin.ROW_KEY)
            out[row[column]] = (key, row)
        return out

    def drop(self, table: str, expect: int = 200) -> dict:
        return self.post("/api/master/table/drop", {"table": table, "confirm": table}, expect)

    def refresh(self, tables: list, expect: int = 200) -> dict:
        return self.post("/api/settings/table-refresh",
                         {"path": str(self.converted), "tables": tables}, expect)

    def tables(self) -> set:
        return {r[0] for r in self.master_rows(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}


class BringEditDropTests(LifecycleBase):
    def test_持ってきて直して消してもう一度持ってくる(self) -> None:
        self.bring([BROUGHT])
        self.add(BROUGHT, {"名前": "う"})
        keys = self.keys(BROUGHT, "名前")
        self.save(BROUGHT, keys["あ"], {"名前": "あ2"})
        self.delete(BROUGHT, keys["い"])
        self.assertEqual(self.master_rows(f'SELECT 名前 FROM "{BROUGHT}" ORDER BY ID'),
                         [("あ2",), ("う",)])

        self.drop(BROUGHT)
        self.assertNotIn(BROUGHT, self.tables())
        self.assertEqual(self.master_rows(f'SELECT * FROM "{REGISTRY}"'), [])
        # 消した表は直せない(一覧を開いたままの画面から押しても)。消えたと言う
        body = self.add(BROUGHT, {"名前": "え"}, expect=409)
        self.assertIn("もう梱包資材マスタにありません", body["error"]["message"])
        self.save(BROUGHT, keys["う"], {"名前": "え"}, expect=409)
        self.delete(BROUGHT, keys["う"], expect=409)
        # 中を見ると、また「無い表」として持ってこられる
        plan = {t["name"]: t for t in self.plan()["tables"]}
        self.assertFalse(plan[BROUGHT]["exists"])
        self.bring([BROUGHT])
        self.assertEqual(self.master_rows(f'SELECT 名前 FROM "{BROUGHT}" ORDER BY ID'),
                         [("あ",), ("い",)])
        self.assertEqual([r[0] for r in self.master_rows(f'SELECT 表 FROM "{REGISTRY}"')],
                         [BROUGHT])
        self.assertTrue(self.browse(BROUGHT)["page"]["editable"])

    def test_必須の列を空で足そうとすると断る(self) -> None:
        self.bring([BROUGHT])
        body = self.add(BROUGHT, {"名前": ""}, expect=400)
        self.assertIn("名前", body["error"]["message"])
        self.assertEqual(len(self.master_rows(f'SELECT * FROM "{BROUGHT}"')), 2)

    def test_同じ行を2つの画面から消すと後のほうは断る(self) -> None:
        self.bring([BROUGHT])
        key = self.keys(BROUGHT, "名前")["あ"]
        self.delete(BROUGHT, key)
        body = self.delete(BROUGHT, key, expect=409)
        self.assertIn("ほかで変わっています", body["error"]["message"])
        self.save(BROUGHT, key, {"名前": "x"}, expect=409)

    def test_変わった名前の表も持ってきて消せる(self) -> None:
        name = 'A "B" [C]'
        self.access(f'CREATE TABLE "A ""B"" [C]" (x TEXT)',
                    f'INSERT INTO "A ""B"" [C]" VALUES (\'1\')')
        self.bring([name])
        self.add(name, {"x": "2"})
        self.assertEqual(self.master_rows('SELECT x FROM "A ""B"" [C]"'), [("1",), ("2",)])
        self.drop(name)
        self.assertNotIn(name, self.tables())


class NameClashTests(LifecycleBase):
    """名前がぶつかるとき。sqlite3 の表の名前は大文字と小文字を区別しない。"""

    def test_大文字小文字だけ違う表は今ある表として扱う(self) -> None:
        self.shared('CREATE TABLE "Test" (a TEXT)', 'INSERT INTO "Test" VALUES (\'共有\')')
        self.access('CREATE TABLE "test" (a TEXT)', 'INSERT INTO "test" VALUES (\'Access\')')
        plan = {t["name"]: t for t in self.plan()["tables"]}
        self.assertTrue(plan["test"]["exists"], plan["test"])
        body = self.bring(["test"], expect=409)
        self.assertIn("もうある", body["message"])
        self.assertEqual(self.master_rows('SELECT a FROM "Test"'), [("共有",)])

    def test_索引と同じ名前の表は持ってこられないと言う(self) -> None:
        self.shared('CREATE INDEX "IX_名前" ON BoardMaster(ボード幅)')
        self.access('CREATE TABLE "IX_名前" (a TEXT)')
        body = self.bring(["IX_名前"], expect=422)
        self.assertIn("IX_名前", body["message"])
        self.assertIn("変えていません", body["message"])
        self.assertNotIn(REGISTRY, self.tables())

    def test_大文字小文字だけ違うパレットの表でも在庫の行を残す(self) -> None:
        self.shared("CREATE TABLE PalletMaster (管理番号, 幅, 丈, 位置, 在庫数)",
                    "INSERT INTO PalletMaster VALUES (1, 1000, 1000, '', ''),"
                    " (2, 1000, 1000, 'A1', 5)")
        self.access("CREATE TABLE palletmaster (管理番号, 幅, 丈, 位置, 在庫数)",
                    "INSERT INTO palletmaster VALUES (1, 1100, 1100, '', '')")
        plan = {t["name"]: t for t in self.plan()["tables"]}["palletmaster"]
        self.assertTrue(plan["exists"])
        self.assertIn("在庫の行", plan["keeps"])
        self.refresh(["palletmaster"])
        self.assertEqual(self.master_rows("SELECT 幅, 位置 FROM PalletMaster ORDER BY 幅"),
                         [(1000, "A1"), (1100, "")])

    def test_大文字小文字だけ違う表を作り直しても名前は梱包資材マスタのまま(self) -> None:
        self.shared("CREATE TABLE PalletMaster (管理番号, 幅, 丈, 位置, 在庫数, 備考)",
                    "INSERT INTO PalletMaster VALUES (2, 1000, 1000, 'A1', 5, '')")
        self.access("CREATE TABLE palletmaster (管理番号, 幅, 丈, 位置, 在庫数)",
                    "INSERT INTO palletmaster VALUES (1, 1100, 1100, '', '')")
        body = self.refresh(["palletmaster"])
        self.assertEqual(len(body["kept_old"]), 1)
        names = [r[0] for r in self.master_rows(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'pallet%'")]
        self.assertIn("PalletMaster", names)
        self.assertNotIn("palletmaster", names)
        self.assertEqual(self.master_rows("SELECT 幅, 位置 FROM PalletMaster ORDER BY 幅"),
                         [(1000, "A1"), (1100, "")])


class BroughtRefreshTests(LifecycleBase):
    """持ってきた表を、あとで Access の最新にする。"""

    def test_Accessで列を足した表を入れ替えると足した列にも打てる(self) -> None:
        self.bring([BROUGHT])
        self.access(f'ALTER TABLE "{BROUGHT}" ADD COLUMN 数 INTEGER',
                    f'INSERT INTO "{BROUGHT}"(名前, 数) VALUES (\'う\', 3)')
        plan = {t["name"]: t for t in self.plan()["tables"]}[BROUGHT]
        self.assertEqual((plan["action"], plan["added_columns"]), ("refresh", ["数"]))
        self.refresh([BROUGHT])
        columns = [c["name"] for c in self.browse(BROUGHT)["columns"]]
        self.assertEqual(columns, ["ID", "名前", "数"])
        self.add(BROUGHT, {"名前": "え", "数": "4"})
        self.assertEqual(self.master_rows(f'SELECT 名前, 数 FROM "{BROUGHT}" ORDER BY ID'),
                         [("あ", None), ("い", None), ("う", 3), ("え", 4)])
        body = self.add(BROUGHT, {"名前": "お", "数": "たくさん"}, expect=400)
        self.assertIn("数", body["error"]["message"])

    def test_Accessで列名を変えた表を作り直してもそのまま直せる(self) -> None:
        self.bring([BROUGHT])
        self.access(f'DROP TABLE "{BROUGHT}"',
                    f'CREATE TABLE "{BROUGHT}" (ID INTEGER PRIMARY KEY, 品名 TEXT NOT NULL)',
                    f'INSERT INTO "{BROUGHT}"(品名) VALUES (\'新あ\')')
        body = self.refresh([BROUGHT])
        old = body["kept_old"][0]["old"]
        # 作り直した表は「足した表」のまま直せる
        page = self.browse(BROUGHT)
        self.assertTrue(page["page"]["editable"])
        self.assertEqual([c["name"] for c in page["columns"]], ["ID", "品名"])
        self.add(BROUGHT, {"品名": "新い"})
        self.assertEqual(self.master_rows(f'SELECT 品名 FROM "{BROUGHT}" ORDER BY ID'),
                         [("新あ",), ("新い",)])
        # 前の表は見るだけ。要らなければ消せる。消しても作り直した表の記録は残る
        old_page = self.browse(old)["page"]
        self.assertFalse(old_page["editable"])
        self.assertTrue(old_page["droppable"])
        self.drop(old)
        self.assertNotIn(old, self.tables())
        self.assertEqual([r[0] for r in self.master_rows(f'SELECT 表 FROM "{REGISTRY}"')],
                         [BROUGHT])
        self.assertTrue(self.browse(BROUGHT)["page"]["editable"])

    def test_消した表は入れ替えられない(self) -> None:
        self.bring([BROUGHT])
        self.drop(BROUGHT)
        body = self.refresh([BROUGHT], expect=400)
        self.assertIn("持ってくる", body["message"])


class StaleScreenTests(LifecycleBase):
    """マスタ管理を開いたまま、ほかで表の中身が入れ替わった。

    行は取り込み元の `rowid` で指す。Access から変換した表には主キーが無い
    (列に型も無い)ので、中身を入れ替えると rowid は 1 から振り直される。
    """

    PLAIN = "番号の無い表"

    def setUp(self) -> None:
        super().setUp()
        self.access(f'CREATE TABLE "{self.PLAIN}" (名前, 数)',
                    f'INSERT INTO "{self.PLAIN}" VALUES (\'あ\', 1), (\'い\', 2), (\'う\', 3)')
        self.bring([self.PLAIN])

    def test_入れ替えのあとに古い画面から直しても別の行を書き換えない(self) -> None:
        stale = self.rows_seen(self.PLAIN, "名前")      # 画面を開いた時点の行
        self.access(f'DELETE FROM "{self.PLAIN}" WHERE 名前 = \'あ\'')
        self.refresh([self.PLAIN])
        # 「あ」はもう無い。その鍵は「い」に付き直っている。直そう・消そうとしたら断る
        key, was = stale["あ"]
        body = self.save(self.PLAIN, key, {"数": "99"}, expect=409, was=was)
        self.assertIn("ほかで変わっています", body["error"]["message"])
        key, was = stale["い"]
        self.delete(self.PLAIN, key, expect=409, was=was)
        self.assertEqual(self.master_rows(f'SELECT 名前, 数 FROM "{self.PLAIN}" ORDER BY 1'),
                         [("い", 2), ("う", 3)])
        # 一覧を出し直せば直せる
        key, was = self.rows_seen(self.PLAIN, "名前")["い"]
        self.save(self.PLAIN, key, {"数": "20"}, was=was)
        self.assertEqual(self.master_rows(f'SELECT 数 FROM "{self.PLAIN}" WHERE 名前 = \'い\''),
                         [(20,)])

    def test_最後の行を消して足したあとに古い画面から直しても新しい行を書き換えない(self) -> None:
        stale = self.rows_seen(self.PLAIN, "名前")
        key, was = stale["う"]
        self.delete(self.PLAIN, key, was=was)                 # 別の画面が消して
        self.add(self.PLAIN, {"名前": "え", "数": "5"})       # 足した(同じ rowid が付く)
        self.assertEqual(self.master_rows(
            f'SELECT rowid FROM "{self.PLAIN}" WHERE 名前 = \'え\''), [(key,)])
        self.save(self.PLAIN, key, {"数": "99"}, expect=409, was=was)
        self.delete(self.PLAIN, key, expect=409, was=was)
        self.assertEqual(self.master_rows(f'SELECT 名前, 数 FROM "{self.PLAIN}" ORDER BY 1'),
                         [("あ", 1), ("い", 2), ("え", 5)])

    def test_2つの画面で同じ行を直すと後から押したほうは断る(self) -> None:
        """先に直した人の値を、見ていない人が黙って上書きしない。"""
        key, was = self.rows_seen(self.PLAIN, "名前")["い"]
        self.save(self.PLAIN, key, {"数": "20"}, was=was)       # 画面A
        self.save(self.PLAIN, key, {"数": "30"}, expect=409, was=was)   # 画面B(古い)
        self.assertEqual(self.master_rows(f'SELECT 数 FROM "{self.PLAIN}" WHERE 名前 = \'い\''),
                         [(20,)])

    def test_見ていたとおりなら数と文字の違いで断らない(self) -> None:
        """型の無い列に数で入った値・小数・空(NULL)も、見たとおりなら書ける。"""
        self.shared(f'INSERT INTO "{self.PLAIN}" VALUES (\'お\', NULL)',
                    f'INSERT INTO "{self.PLAIN}" VALUES (\'か\', 1.5)',
                    f'INSERT INTO "{self.PLAIN}" VALUES (\'き\', \'7\')')
        seen = self.rows_seen(self.PLAIN, "名前")
        for name in ("あ", "お", "か", "き"):
            key, was = seen[name]
            self.save(self.PLAIN, key, {"数": "8"}, was=was)
        self.assertEqual(self.master_rows(
            f'SELECT COUNT(*) FROM "{self.PLAIN}" WHERE CAST(数 AS INTEGER) = 8'), [(4,)])

    def test_小数の1_0を画面が1で送り返しても断らない(self) -> None:
        """ブラウザは JSON の 1.0 を 1 として持つ。送り返すと数の 1 になる。"""
        self.shared(f'INSERT INTO "{self.PLAIN}" VALUES (\'こ\', 2.0)')
        key, was = self.rows_seen(self.PLAIN, "名前")["こ"]
        was["数"] = int(was["数"])
        self.save(self.PLAIN, key, {"数": "3"}, was=was)


class UntypedColumnTests(LifecycleBase):
    """Access から変換した表は列に型が無い。打った値は、入っている値に合わせる。"""

    def columns(self, table: str) -> dict:
        return {c["name"]: c["kind"] for c in self.browse(table)["columns"]}

    def test_数ばかりの列には数で入れる(self) -> None:
        self.access('CREATE TABLE "数の表" (名前, 個数, 重さ)',
                    'INSERT INTO "数の表" VALUES (\'あ\', 1, 0.5), (\'い\', 2, 1)')
        self.bring(["数の表"])
        self.assertEqual(self.columns("数の表"), {"名前": "text", "個数": "int", "重さ": "real"})
        self.add("数の表", {"名前": "う", "個数": "20", "重さ": "1.25"})
        self.assertEqual(self.master_rows(
            'SELECT typeof(個数), typeof(重さ) FROM "数の表" WHERE 名前 = \'う\''),
            [("integer", "real")])
        # 並べ替えても数の順(文字の '20' が後ろへ回らない)
        order = [r["名前"] for r in self.client.get(
            "/api/master/browse", query_string={"table": "数の表", "sort": "個数"},
            headers=self.auth()).get_json()["page"]["rows"]]
        self.assertEqual(order, ["あ", "い", "う"])
        self.add("数の表", {"名前": "え", "個数": "たくさん"}, expect=400)

    def test_文字で入っている列は文字のまま(self) -> None:
        """access_parser で変換すると数も文字で入る(実物の VC重量 がそう)。"""
        self.access('CREATE TABLE "文字の表" (管理番号, 単位質量)',
                    'INSERT INTO "文字の表" VALUES (\'1\', \'0.118\')')
        self.bring(["文字の表"])
        self.assertEqual(self.columns("文字の表"), {"管理番号": "text", "単位質量": "text"})
        self.add("文字の表", {"管理番号": "2", "単位質量": "0.2"})
        self.assertEqual(self.master_rows('SELECT typeof(管理番号) FROM "文字の表"'),
                         [("text",), ("text",)])


if __name__ == "__main__":
    unittest.main()
