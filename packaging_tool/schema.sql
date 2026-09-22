-- ============================================================
-- 梱包資材総合ツール Python/SQLite版 — DBスキーマ
--
-- VBA版はAccess(.accdb)を使用していたが、本ツールはsqlite3のみで
-- 動作する(pyodbc/Access不要)。移植にあたってのAccess→SQLite型対応:
--
--   Access型               SQLite型                備考
--   ----------------------------------------------------------
--   オートナンバー(Long)     INTEGER PRIMARY KEY     SQLiteのrowidと
--                                                    同一になり、
--                                                    AUTOINCREMENTで
--                                                    採番される。
--                                                    (VBA版は
--                                                    `MAX(管理番号)+1`
--                                                    を手動計算して
--                                                    いたため複数端末
--                                                    同時実行で重複の
--                                                    恐れがあった。
--                                                    SQLite側の採番に
--                                                    任せることで
--                                                    その競合を解消する)
--   長整数型(Long)           INTEGER
--   Yes/No(Boolean)         INTEGER (0/1)
--   日付/時刻型              TEXT                    ISO8601
--                                                    "YYYY-MM-DD HH:MM:SS"
--                                                    形式の文字列で保存
--                                                    (文字列比較で
--                                                    ソート可能な点は
--                                                    VBA版の
--                                                    "yyyy/mm/dd hh:nn:ss"
--                                                    運用を踏襲)
--   短いテキスト/長いテキスト  TEXT                    SQLiteは長さ制限なし
--   通貨型/倍精度浮動小数点数  REAL
--
-- 実際の.accdbファイル(mdbtoolsで解析)からテーブル定義を確認したところ、
-- Access側はほぼ全ての列を(数値・日付に見えるものも含めて)Text型で
-- 保持していたことが判明した。VBA側は読み取り時にCLng/CDbl/Val等で
-- 数値化していたため、Python/SQLite版では実データが本来数値である列は
-- 素直にINTEGER/REALとして格納する(緩い型付けのAccess由来の癖を
-- そのまま持ち込まない、という意図的な改善)。
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------
-- PalletMaster: パレット/ボードの位置別在庫マスタ
-- VBA版 UFMAP フォームが直接SELECT/UPDATE/INSERT/DELETEしていたテーブル。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS PalletMaster (
    管理番号      INTEGER PRIMARY KEY AUTOINCREMENT,
    幅           INTEGER NOT NULL,              -- width (mm)
    丈           INTEGER NOT NULL,              -- length/height (mm)
    巾適合min     INTEGER NOT NULL DEFAULT 0,     -- RunUpdatePalletAll相当で自動再計算
    巾適合max     INTEGER NOT NULL DEFAULT 0,
    丈適合min     INTEGER NOT NULL DEFAULT 0,
    丈適合max     INTEGER NOT NULL DEFAULT 0,
    業界         TEXT NOT NULL DEFAULT '一般',
    記号         TEXT NOT NULL DEFAULT '',
    位置         TEXT NOT NULL,                  -- 保管位置/ロケーションコード
    在庫数        INTEGER NOT NULL DEFAULT 0,
    リスト管理     TEXT NOT NULL DEFAULT '',       -- '要' なら在庫0でも行を残す
    桁数         INTEGER NOT NULL DEFAULT 0,      -- 松板使用数、RunUpdatePalletAll相当で自動再計算
    脚数         INTEGER NOT NULL DEFAULT 0,      -- 同上
    コード       TEXT NOT NULL DEFAULT '',
    単位         TEXT NOT NULL DEFAULT '',        -- 組/台/枚
    備考         TEXT NOT NULL DEFAULT '',
    更新日時      TEXT NOT NULL                    -- ISO8601。楽観ロックのトークンとしても使用
);

CREATE INDEX IF NOT EXISTS idx_pallet_master_pos ON PalletMaster(位置);
CREATE INDEX IF NOT EXISTS idx_pallet_master_wl ON PalletMaster(幅, 丈);

-- ------------------------------------------------------------------
-- パレット入出庫履歴: 受入/払出の追記専用ログ (UFMAP `TBL_HISTORY` 相当)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS パレット入出庫履歴 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    幅          INTEGER NOT NULL,
    丈          INTEGER NOT NULL,
    業界        TEXT NOT NULL DEFAULT '',
    記号        TEXT NOT NULL DEFAULT '',
    位置        TEXT NOT NULL,
    区分        TEXT NOT NULL CHECK (区分 IN ('受入', '払出')),
    数量        INTEGER NOT NULL,
    在庫数_更新後  INTEGER NOT NULL,
    更新日時     TEXT NOT NULL,
    備考        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_stock_history_pos ON パレット入出庫履歴(位置);
CREATE INDEX IF NOT EXISTS idx_stock_history_updated ON パレット入出庫履歴(更新日時);

-- ------------------------------------------------------------------
-- BoardMaster: ボード(板材)候補マスタ。資材選択で候補列挙に使用。
-- 「データラベル」は元Excelフォーム上のラベルコントロール名
-- (lblItem4等、カンマ区切りで複数)で、UI上の表示グルーピングに
-- 使われていたVBA/Excel固有の情報。tkinter版では直接は使わないが、
-- 参照用にそのまま保持する。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS BoardMaster (
    管理番号      INTEGER PRIMARY KEY AUTOINCREMENT,
    ボード幅      INTEGER NOT NULL,
    ボード丈      INTEGER NOT NULL,
    ボードタイプ   TEXT NOT NULL,   -- 'ハードボード' / 'IKボード' / 'プロテックボード'
    データラベル   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_board_master_type ON BoardMaster(ボードタイプ);

-- ------------------------------------------------------------------
-- CornerboardMaster: アングル(角)ボードの長さ候補マスタ。
-- modAngleSelect.LoadAllAngleLengths / SelectAngles で使用。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CornerboardMaster (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    アングル丈   INTEGER NOT NULL,
    データラベル  TEXT NOT NULL DEFAULT ''
);

-- ------------------------------------------------------------------
-- PalletPatterns: 資材配置パターンの保存(SaveNewPattern_v2/GetPatternList等)。
-- A〜Jの10枠、各枠に幅/丈/枚数/用途('下用'/'上用'/'')を持つ。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS PalletPatterns (
    管理番号    INTEGER PRIMARY KEY AUTOINCREMENT,
    パレット幅   INTEGER NOT NULL,
    パレット丈   INTEGER NOT NULL,
    製品幅      INTEGER NOT NULL,
    製品丈      INTEGER NOT NULL,
    登録日時     TEXT NOT NULL,
    更新日時     TEXT NOT NULL,
    使用回数     INTEGER NOT NULL DEFAULT 0,
    A幅 INTEGER, A丈 INTEGER, A枚数 INTEGER, A用途 TEXT NOT NULL DEFAULT '',
    B幅 INTEGER, B丈 INTEGER, B枚数 INTEGER, B用途 TEXT NOT NULL DEFAULT '',
    C幅 INTEGER, C丈 INTEGER, C枚数 INTEGER, C用途 TEXT NOT NULL DEFAULT '',
    D幅 INTEGER, D丈 INTEGER, D枚数 INTEGER, D用途 TEXT NOT NULL DEFAULT '',
    E幅 INTEGER, E丈 INTEGER, E枚数 INTEGER, E用途 TEXT NOT NULL DEFAULT '',
    F幅 INTEGER, F丈 INTEGER, F枚数 INTEGER, F用途 TEXT NOT NULL DEFAULT '',
    G幅 INTEGER, G丈 INTEGER, G枚数 INTEGER, G用途 TEXT NOT NULL DEFAULT '',
    H幅 INTEGER, H丈 INTEGER, H枚数 INTEGER, H用途 TEXT NOT NULL DEFAULT '',
    I幅 INTEGER, I丈 INTEGER, I枚数 INTEGER, I用途 TEXT NOT NULL DEFAULT '',
    J幅 INTEGER, J丈 INTEGER, J枚数 INTEGER, J用途 TEXT NOT NULL DEFAULT ''
);

-- ------------------------------------------------------------------
-- ボード使用実績: 実際に使ったボードの記録(`packaging_tool/board_usage.py`)。
--
-- **1行 = 「使用する」を1回押した、そのときの1寸法。**
-- 集計(どのサイズを何枚使ったか)はこの表を数えて出します ── 同じ
-- 事実を「明細」と「集計」の2か所に持つと、片方だけ直ったときに
-- どちらが本当か分からなくなるためです。
--
-- 【何をもって「使用」とするか】
-- 配置してあり、かつ**「使用する」を押したとき**だけ積みます。配置は
-- 何度でも試せる操作なので、置いてみただけの下書きまで数えると
-- 「よく使うサイズ」が実態からずれます。以前は配置図の印刷で積んで
-- いましたが、確認のために印刷しても積まれ、印刷せずに使えば積まれ
-- ないため、押した人の意図と一致しませんでした(現場の指摘)。
--
-- 【製品とパレットの寸法も一緒に残す】
-- 「この製品・このパレットのときに、どのボードを使ったか」が後から
-- 辿れます。ボードの寸法だけでは、なぜそのサイズが多いのかを説明
-- できません。分からないときは0(未入力のまま押せる場面がある)。
--
-- 【記録するのは「棚から取った板」の寸法】
-- カットして使った場合でも、消費したのは**カット前の1枚**です。
-- カット後の寸法で積むと、ボード一覧に載っていない寸法ばかりが並び、
-- 何を何枚持っておけばよいのかが読めなくなります。カット後の寸法は
-- 別の列に添えます(カットしていなければ0)。
--
-- 集計単位は 幅×丈×ボードタイプ。上用/下用は物理的には同じ板なので
-- 分けない(現場の指示)。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ボード使用実績 (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    ボード幅     INTEGER NOT NULL,
    ボード丈     INTEGER NOT NULL,
    ボードタイプ  TEXT NOT NULL DEFAULT '',
    枚数        INTEGER NOT NULL DEFAULT 0,
    切断後幅     INTEGER NOT NULL DEFAULT 0,
    切断後丈     INTEGER NOT NULL DEFAULT 0,
    製品幅       INTEGER NOT NULL DEFAULT 0,
    製品丈       INTEGER NOT NULL DEFAULT 0,
    パレット幅    INTEGER NOT NULL DEFAULT 0,
    パレット丈    INTEGER NOT NULL DEFAULT 0,
    ロット番号    TEXT NOT NULL DEFAULT '',
    使用日時     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ボード使用実績_寸法
    ON ボード使用実績(ボード幅, ボード丈, ボードタイプ);
CREATE INDEX IF NOT EXISTS idx_ボード使用実績_日時
    ON ボード使用実績(使用日時);

CREATE INDEX IF NOT EXISTS idx_pallet_patterns_wl ON PalletPatterns(パレット幅, パレット丈);

-- ------------------------------------------------------------------
-- 梱包保護材: 保護材選定マトリクス。PalletHistoryModule_v2.GetUpperPartMaterial
-- が包装仕様書No/材質/調質/用途コード/板厚・板幅・板丈の範囲で検索する。
-- 板厚下/板厚上/板厚(および幅/丈も同様)は3列ともNULLなら
-- 「範囲制限なし(常に一致)」という元VBAの仕様を、NULL=無制限として
-- そのままSQLiteでも表現する。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS 梱包保護材 (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    曖昧表現     TEXT,
    包装仕様書   TEXT NOT NULL DEFAULT '',   -- 空欄=汎用行(Pass2で使用)
    材質        TEXT,
    調質        TEXT,
    用途コード   TEXT,
    板厚下      REAL,
    板厚上      REAL,
    板厚        REAL,
    板幅下      INTEGER,
    板幅上      INTEGER,
    板幅        INTEGER,
    板丈下      INTEGER,
    板丈上      INTEGER,
    板丈        INTEGER,
    使用保護材   TEXT NOT NULL,
    更新日      TEXT,
    備考        TEXT
);

CREATE INDEX IF NOT EXISTS idx_hogozai_pack ON 梱包保護材(包装仕様書);

-- ------------------------------------------------------------------
-- 松板角材: 1P0113(裸梱包)モード等で使う松板/角材マスタ。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS 松板角材 (
    管理番号  INTEGER PRIMARY KEY AUTOINCREMENT,
    品名     TEXT NOT NULL,
    厚       REAL NOT NULL,
    幅       INTEGER NOT NULL,
    丈min    INTEGER NOT NULL,
    丈max    INTEGER NOT NULL,
    コード    TEXT,
    単位     TEXT,
    備考     TEXT
);

-- ------------------------------------------------------------------
-- 資材パレット注文管理: 倉庫連携(frmWarehouseOrder/frmSendConfirm)で
-- 使用する発注テーブル。取り消し済/確認済みは元Accessスキーマが
-- Text(1)だったため、そのままTEXTで踏襲する
-- (実際に書き込まれる値はfrmWarehouseOrder調査結果を踏まえて実装する)。
--
-- **「誰が送ったか」の列は無い。** 元Accessスキーマにも無く、足しても
-- いない ── 送れるのは現場だけなので、区別する相手がいない。
--
-- 取り込み元のテーブルを直接見ると `送信ID` 列があるが、**これは人では
-- ない**。書き戻しエンジンが二重登録を防ぐために自分で ALTER TABLE で
-- 足す「送信操作の一意ID」で(`outbox_sync.DEFAULT_OP_ID_COLUMN`)、
-- 手元側の対になる値は `Access同期記録.送信ID` が持つ。だから業務の
-- 列としてここには置かない。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS 資材パレット注文管理 (
    管理番号    INTEGER PRIMARY KEY AUTOINCREMENT,
    登録日時    TEXT,
    LotNo      TEXT NOT NULL,
    品名       TEXT NOT NULL,
    発注コード  TEXT NOT NULL,
    単位       TEXT NOT NULL,
    材質       TEXT,
    調質       TEXT,
    厚         REAL NOT NULL,
    幅         INTEGER NOT NULL,
    丈         INTEGER NOT NULL,
    用途コード  TEXT,
    納入先     TEXT,
    発注数     INTEGER,
    取り消し済  TEXT,
    取り消し日時 TEXT,
    確認済み    TEXT,
    確認日時    TEXT,
    -- 送り先(共有)の同じ行を指す番号。**取り込みで受け取った行だけが持つ。**
    -- 手元の管理番号は取り込みのたびに振り直されるので、共有の行を指す
    -- 手がかりにならない(実測: 共有 41,42 → 手元 1,2)。確認の印を共有へ
    -- 書き戻すとき、どの行かをこれで決める
    取込元管理番号 INTEGER,
    -- 手元で付けた印(確認済み/取り消し済)を、まだ共有へ送れていない。
    -- 送れたら空に戻す。**残っているあいだは取り込みを見送る** ──
    -- 総入れ替えで、まだ送れていない印を消してしまわないため
    印未反映    TEXT
);

CREATE INDEX IF NOT EXISTS idx_warehouse_order_lotno ON 資材パレット注文管理(LotNo);
CREATE INDEX IF NOT EXISTS idx_warehouse_order_status ON 資材パレット注文管理(取り消し済, 確認済み);

-- ------------------------------------------------------------------
-- 看板_*: 在庫薄(欲='〇')/在庫あり(不='〇')を管理する6種の看板テーブル。
-- PalletHistoryModule_v2.BuildBoardStockMap がこの6テーブルを横断して
-- 板材種別ごとの在庫状態マップを作る(資材選択の在庫警告に使用)。
-- 全テーブル同一構造。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS 看板_AIM (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    資材        TEXT NOT NULL,
    サイズ      TEXT NOT NULL,
    欲          TEXT NOT NULL DEFAULT '',
    不          TEXT NOT NULL DEFAULT '',
    更新日      TEXT,
    発送        TEXT,
    倉庫確認日時 TEXT,
    常設品      TEXT
);

CREATE TABLE IF NOT EXISTS 看板_HVC (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    資材        TEXT NOT NULL,
    サイズ      TEXT NOT NULL,
    欲          TEXT NOT NULL DEFAULT '',
    不          TEXT NOT NULL DEFAULT '',
    更新日      TEXT,
    発送        TEXT,
    倉庫確認日時 TEXT,
    常設品      TEXT
);

CREATE TABLE IF NOT EXISTS 看板_LVC (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    資材        TEXT NOT NULL,
    サイズ      TEXT NOT NULL,
    欲          TEXT NOT NULL DEFAULT '',
    不          TEXT NOT NULL DEFAULT '',
    更新日      TEXT,
    発送        TEXT,
    倉庫確認日時 TEXT,
    常設品      TEXT
);

CREATE TABLE IF NOT EXISTS 看板_L1 (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    資材        TEXT NOT NULL,
    サイズ      TEXT NOT NULL,
    欲          TEXT NOT NULL DEFAULT '',
    不          TEXT NOT NULL DEFAULT '',
    更新日      TEXT,
    発送        TEXT,
    倉庫確認日時 TEXT,
    常設品      TEXT
);

CREATE TABLE IF NOT EXISTS 看板_LS (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    資材        TEXT NOT NULL,
    サイズ      TEXT NOT NULL,
    欲          TEXT NOT NULL DEFAULT '',
    不          TEXT NOT NULL DEFAULT '',
    更新日      TEXT,
    発送        TEXT,
    倉庫確認日時 TEXT,
    常設品      TEXT
);

CREATE TABLE IF NOT EXISTS 看板_コイル (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    資材        TEXT NOT NULL,
    サイズ      TEXT NOT NULL,
    欲          TEXT NOT NULL DEFAULT '',
    不          TEXT NOT NULL DEFAULT '',
    更新日      TEXT,
    発送        TEXT,
    倉庫確認日時 TEXT,
    常設品      TEXT
);

CREATE TABLE IF NOT EXISTS 看板_大板小板 (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    資材        TEXT NOT NULL,
    サイズ      TEXT NOT NULL,
    欲          TEXT NOT NULL DEFAULT '',
    不          TEXT NOT NULL DEFAULT '',
    更新日      TEXT,
    発送        TEXT,
    倉庫確認日時 TEXT,
    常設品      TEXT
);

-- ------------------------------------------------------------------
-- Form状態管理: フォーム状態の永続化(用途はVBA側でも軽微)。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS Form状態管理 (
    管理番号  INTEGER PRIMARY KEY AUTOINCREMENT,
    ライン名  TEXT NOT NULL,
    状態     TEXT NOT NULL
);


-- ------------------------------------------------------------------
-- アクセス権限: この端末・この利用者が何をできるか
--
-- 1行 = 1つの許可。ログインID と PC名 のどちらか(または両方)を条件に、
-- 権限コードを1つ与える。空欄は「問わない」。
--
--   ログインID  PC名        権限            有効
--   yamada      (空)        mode:material   1     … 山田はどのPCでも資材
--   (空)        NLM-PC-042  mode:material   1     … この端末は誰でも資材
--   suzuki      NLM-PC-042  mode:material   1     … 鈴木がこの端末のときだけ
--
-- **両方が空の行は誰にも効かない。** それは全員への許可で、権限を設ける
-- 意味が消える(`access_control.Rule.matches` が撥ねる)。書かれていれば
-- 設定画面が「条件の無い行」として警告に出す。
--
-- 権限を増やすときに**この表は変えない**。増えるのは権限コードの値だけで、
-- 使える値は `packaging_tool/access_control.py` が持つ。
-- 該当が1行も無い端末は現場モードだけを持つ(締め出さず、勝手に強い
-- 権限も渡さない)。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS アクセス権限 (
    管理番号    INTEGER PRIMARY KEY AUTOINCREMENT,
    ログインID  TEXT NOT NULL DEFAULT '',
    PC名        TEXT NOT NULL DEFAULT '',
    権限        TEXT NOT NULL,
    有効        INTEGER NOT NULL DEFAULT 1,
    備考        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_アクセス権限_ログインID ON アクセス権限(ログインID);
CREATE INDEX IF NOT EXISTS idx_アクセス権限_PC名 ON アクセス権限(PC名);


-- ==================================================================
-- 仕掛(しかかり)台帳の取り込み先
--
-- ロット一覧は、社内ネットワーク共有
--     \\nlmsrvngy03\Read\【New】仕掛\台帳\
-- 上にある3ファイル(SIKALOT / SIKAHIKI / SIKAODR、いずれも
-- テーブル名は「仕掛」)を出どころにする。梱包資材マスタとは別のDBで、
-- ホスト系から定期的に出力される参照専用データ。
--
-- 共有フォルダを直接読まず、マスタ類と同じく事前に手元のSQLiteへ
-- 取り込む(設定画面、または `scripts/import_source.py --lot-only`)。
-- 列は元のSELECT文が読んでいたものだけを持つ(全列は不要かつ不明)。
-- ==================================================================

-- ------------------------------------------------------------------
-- 仕掛ロット (SIKALOT.仕掛): ロット番号で1件引く
--
-- ロット番号は**一意ではない**。実データ2046行のうち527ロットが重複して
-- おり、BOX工程(BOX設計_設備名・BOX番号)ごとに行が分かれている。
-- 元のAccessテーブルもキー無しのスナップショットで、VBA `SearchLotInfo` は
-- `WHERE ﾛｯﾄ番号 = ...` の先頭レコードを採るだけ。
-- そのため代理キーを立て、取り込み順(=Accessの物理順)の先頭を引けるようにする。
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS 仕掛ロット (
    管理番号          INTEGER PRIMARY KEY AUTOINCREMENT,
    ロット番号        TEXT NOT NULL,
    用途コード        TEXT NOT NULL DEFAULT '',
    用途名           TEXT NOT NULL DEFAULT '',
    製造材質          TEXT NOT NULL DEFAULT '',
    製造調質          TEXT NOT NULL DEFAULT '',
    製造板厚          REAL NOT NULL DEFAULT 0,
    製造板幅          REAL NOT NULL DEFAULT 0,
    製造板丈          REAL NOT NULL DEFAULT 0,
    オーダー板厚      REAL NOT NULL DEFAULT 0,
    オーダー板幅      REAL NOT NULL DEFAULT 0,
    オーダー板丈      REAL NOT NULL DEFAULT 0,
    設計_設備コース    TEXT NOT NULL DEFAULT '',
    実績_設備コース    TEXT NOT NULL DEFAULT '',
    BOX実績_板厚      REAL NOT NULL DEFAULT 0,
    BOX実績_板幅      REAL NOT NULL DEFAULT 0,
    BOX実績_板丈      REAL NOT NULL DEFAULT 0,
    BOX実績_枚本数    INTEGER NOT NULL DEFAULT 0,
    -- ひとつ前・ふたつ前の工程で何枚(何本)流れたか。
    -- **一覧には出していない**(下記 BOX最終実績に置き換えた)。
    -- 1工程目の行では必ず空になるためで、一覧が出すのはロットごとの
    -- 先頭行(=たいてい1工程目)なので、ほぼ全部が空欄になる
    前々工程実績_枚本数  INTEGER NOT NULL DEFAULT 0,
    前工程実績_枚本数    INTEGER NOT NULL DEFAULT 0,
    -- そのロットの**最終工程**の実績。
    --
    -- `当工程_BOX番号 = 最終実績工程_BOX番号` の行の値が、そのロットの
    -- **全行に配られて**いる(実データ1,319ロットで確認。不一致0)。
    -- つまりロット内で1つに定まるので、一覧が出す先頭行にも正しい値が
    -- 載る ── BOX実績_* が「その行の工程」の値で、行によって違うのとは
    -- ここが違う(実データでは8,056行中3,100行で食い違った)。
    --
    -- 寸法3つは実データでは BOX実績_* と全行一致していたが、意味が
    -- 違う以上そろえておく(現場の判断:「新ファイルで統一する」)。
    BOX最終実績_設備名  TEXT NOT NULL DEFAULT '',
    BOX最終実績_板厚    REAL NOT NULL DEFAULT 0,
    BOX最終実績_板幅    REAL NOT NULL DEFAULT 0,
    BOX最終実績_板丈    REAL NOT NULL DEFAULT 0,
    BOX最終実績_枚本数  INTEGER NOT NULL DEFAULT 0,
    -- 試験指示票(先行データ)の要否判定に使う(VBA `AdvanceCheck`)。
    -- 品質グレード_表面処理が"S"/"T"以外で、かつ製造板厚が3mm以下または
    -- 用途コードの先頭が"T"/"D1"/"D2"のいずれかなら要
    品質グレード_表面処理  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sikakari_lot_no ON 仕掛ロット(ロット番号);

-- ------------------------------------------------------------------
-- 仕掛引当 (SIKAHIKI.仕掛): ロット番号で複数件引き、引当番号順に並べる
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS 仕掛引当 (
    管理番号     INTEGER PRIMARY KEY AUTOINCREMENT,
    ロット番号   TEXT NOT NULL,
    受注番号     TEXT NOT NULL DEFAULT '',
    引当数量     REAL NOT NULL DEFAULT 0,
    引当調整NO   TEXT NOT NULL DEFAULT '',
    -- 引当番号は8桁の番号。数値で持つと 6.0717e+07 のような指数表記になり、
    -- 現場が読めないうえ先頭の0も落ちる。計算には一切使わないのでTEXT。
    -- 並び順(ORDER BY 引当番号)は全件8桁固定なので文字列でも数値と一致する
    引当番号     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sikakari_hiki_lot ON 仕掛引当(ロット番号);

-- ------------------------------------------------------------------
-- 仕掛受注 (SIKAODR.仕掛): 引当で得た受注番号で引く
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS 仕掛受注 (
    受注番号        TEXT PRIMARY KEY,
    納入先名称       TEXT NOT NULL DEFAULT '',
    包装仕様NO      TEXT NOT NULL DEFAULT '',
    取引先名称       TEXT NOT NULL DEFAULT '',
    送り先名称       TEXT NOT NULL DEFAULT '',
    納送用コメント    TEXT NOT NULL DEFAULT '',
    工場用コメント    TEXT NOT NULL DEFAULT '',
    VC_表           TEXT NOT NULL DEFAULT '',
    VC_裏           TEXT NOT NULL DEFAULT '',
    EX_輸出区分      TEXT NOT NULL DEFAULT '',
    材質_比重       REAL NOT NULL DEFAULT 0,
    梱包単位_重量    REAL NOT NULL DEFAULT 0,
    梱包単位_枚数    REAL NOT NULL DEFAULT 0
);


-- ==================================================================
-- パレット適合閾値マスタ(7表)
--
-- 【何を持つ表か】
-- 「幅◯◯〜◯◯のパレットは、幅◯◯〜◯◯の製品に使える」という
-- タイトパレット選定基準表そのもの。以前はこの数値が
-- `pallet_service.py` に直接書かれていて、基準が変わるたびに
-- **プログラムを直さないと反映できません**でした。表に出したので、
-- 資材課がマスタ管理画面から直せます。
--
-- 【列の形はVBA(PalletThresholdMaster.accdb)の写し】
-- 移植元と同じ表名・列名にしてあります。名前を変えると、あちらの
-- 生成ツールが作ったファイルをそのまま取り込めなくなります。
--   Access の AUTOINCREMENT/BOOLEAN/DATE は、SQLite では
--   INTEGER PRIMARY KEY AUTOINCREMENT / INTEGER(0,1) / TEXT に対応。
--
-- 【帯(band)の約束】
-- 入力最小値〜入力最大値は 0 から 999999 まで**隙間なく**続けること。
-- 途中が抜けるとその寸法だけ判定できなくなるので、読み込み時に
-- `pallet_threshold.load()` が連続性を確かめ、崩れていれば再計算を
-- 中断します(黙って一部だけ計算しない)。
-- 有効=0 の行は読み飛ばすので、**行を消さずに外せます**。ただし
-- 外した分だけ帯に穴が開くことは上の検査が言います。
-- ==================================================================

-- 丈(長手)の帯 → その帯に載せられる製品丈の範囲
CREATE TABLE IF NOT EXISTS PalletDakeThreshold (
    ID          INTEGER PRIMARY KEY AUTOINCREMENT,
    入力最小値   INTEGER NOT NULL DEFAULT 0,
    入力最大値   INTEGER NOT NULL DEFAULT 0,
    適合最小値   INTEGER NOT NULL DEFAULT 0,
    適合最大値   INTEGER NOT NULL DEFAULT 0,
    有効        INTEGER NOT NULL DEFAULT 1,
    並び順       INTEGER NOT NULL DEFAULT 0,
    備考        TEXT NOT NULL DEFAULT '',
    更新日時     TEXT NOT NULL DEFAULT ''
);

-- 幅(短手)の帯 → その帯に載せられる製品幅の範囲
CREATE TABLE IF NOT EXISTS PalletHabaThreshold (
    ID          INTEGER PRIMARY KEY AUTOINCREMENT,
    入力最小値   INTEGER NOT NULL DEFAULT 0,
    入力最大値   INTEGER NOT NULL DEFAULT 0,
    適合最小値   INTEGER NOT NULL DEFAULT 0,
    適合最大値   INTEGER NOT NULL DEFAULT 0,
    有効        INTEGER NOT NULL DEFAULT 1,
    並び順       INTEGER NOT NULL DEFAULT 0,
    備考        TEXT NOT NULL DEFAULT '',
    更新日時     TEXT NOT NULL DEFAULT ''
);

-- 丈の帯 → 脚の本数
CREATE TABLE IF NOT EXISTS PalletAshiThreshold (
    ID          INTEGER PRIMARY KEY AUTOINCREMENT,
    入力最小値   INTEGER NOT NULL DEFAULT 0,
    入力最大値   INTEGER NOT NULL DEFAULT 0,
    脚数        INTEGER NOT NULL DEFAULT 0,
    有効        INTEGER NOT NULL DEFAULT 1,
    並び順       INTEGER NOT NULL DEFAULT 0,
    備考        TEXT NOT NULL DEFAULT '',
    更新日時     TEXT NOT NULL DEFAULT ''
);

-- 幅の帯 → 桁(松板)の本数
CREATE TABLE IF NOT EXISTS PalletKetaThreshold (
    ID          INTEGER PRIMARY KEY AUTOINCREMENT,
    入力最小値   INTEGER NOT NULL DEFAULT 0,
    入力最大値   INTEGER NOT NULL DEFAULT 0,
    桁数        INTEGER NOT NULL DEFAULT 0,
    有効        INTEGER NOT NULL DEFAULT 1,
    並び順       INTEGER NOT NULL DEFAULT 0,
    備考        TEXT NOT NULL DEFAULT '',
    更新日時     TEXT NOT NULL DEFAULT ''
);

-- 記号(C1/P1…)ごとの固定適合。帯の計算より優先する
CREATE TABLE IF NOT EXISTS PalletSymbolMaster (
    ID          INTEGER PRIMARY KEY AUTOINCREMENT,
    記号        TEXT NOT NULL DEFAULT '',
    巾適合最小値 INTEGER NOT NULL DEFAULT 0,
    巾適合最大値 INTEGER NOT NULL DEFAULT 0,
    丈適合最小値 INTEGER NOT NULL DEFAULT 0,
    丈適合最大値 INTEGER NOT NULL DEFAULT 0,
    桁数        INTEGER NOT NULL DEFAULT 0,
    脚数        INTEGER NOT NULL DEFAULT 0,
    有効        INTEGER NOT NULL DEFAULT 1,
    並び順       INTEGER NOT NULL DEFAULT 0,
    備考        TEXT NOT NULL DEFAULT '',
    更新日時     TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS UX_記号 ON PalletSymbolMaster(記号);

-- 業界(1×2/4×8…)ごとの固定適合。記号が当たらないときに使う
CREATE TABLE IF NOT EXISTS PalletIndustryMaster (
    ID          INTEGER PRIMARY KEY AUTOINCREMENT,
    業界        TEXT NOT NULL DEFAULT '',
    巾適合最小値 INTEGER NOT NULL DEFAULT 0,
    巾適合最大値 INTEGER NOT NULL DEFAULT 0,
    丈適合最小値 INTEGER NOT NULL DEFAULT 0,
    丈適合最大値 INTEGER NOT NULL DEFAULT 0,
    有効        INTEGER NOT NULL DEFAULT 1,
    並び順       INTEGER NOT NULL DEFAULT 0,
    備考        TEXT NOT NULL DEFAULT '',
    更新日時     TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS UX_業界 ON PalletIndustryMaster(業界);

-- 業界と記号の組合せ。3つの中で最優先(同じ業界でも記号で値が変わる)
CREATE TABLE IF NOT EXISTS PalletComboMaster (
    ID          INTEGER PRIMARY KEY AUTOINCREMENT,
    業界        TEXT NOT NULL DEFAULT '',
    記号        TEXT NOT NULL DEFAULT '',
    巾適合最小値 INTEGER NOT NULL DEFAULT 0,
    巾適合最大値 INTEGER NOT NULL DEFAULT 0,
    丈適合最小値 INTEGER NOT NULL DEFAULT 0,
    丈適合最大値 INTEGER NOT NULL DEFAULT 0,
    有効        INTEGER NOT NULL DEFAULT 1,
    並び順       INTEGER NOT NULL DEFAULT 0,
    備考        TEXT NOT NULL DEFAULT '',
    更新日時     TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS UX_業界記号 ON PalletComboMaster(業界, 記号);
