---
name: gen-dict-v3
description: '從 Oracle 資料庫自動生成業務字典 YAML（v3 改良版）。當使用者說「生成字典」、「gen-dict」、「掃描資料庫結構」、「建立字典」時使用。產生 dict/schema/ 和 dict/business/ 兩層 YAML，含中斷恢復與大 schema 分批機制。'
argument-hint: "[schema-name] [module1,module2,...] [--output=dict-v3]"
---

## Step 0：中斷恢復檢查

每次啟動時先檢查是否有未完成的工作：

1. 讀取 repo memory（`/memories/repo/gen-dict-checkpoint.md`）（若存在）
2. 若有 checkpoint：
   - 顯示上次進度（已完成的表清單、目前 step）
   - 詢問使用者：「偵測到上次未完成的字典生成，要繼續還是重新開始？」
   - 繼續 → 跳到 checkpoint 記錄的 step，跳過已產出的 YAML
   - 重新開始 → 刪除 checkpoint，從 Step 1 開始
3. 若無 checkpoint → 正常從 Step 1 開始

**Checkpoint 格式（存在 repo memory，可跨對話存活）：**
```yaml
schema: SEMI
output_dir: dict-v3
current_step: 3
tables_total: [CP_LOT, CP_WAFER, ...]
tables_done: [CP_LOT]
modules: [CP, WAT]
module_mapping: {CP: [CP_LOT, CP_WAFER, CP_BIN_SUMMARY], WAT: [WAT_LOT, WAT_PARAM, WAT_RESULT]}
step2_completed: true
```

> 每完成一個 step 或一批表的 YAML 寫入後，更新 checkpoint。
> 恢復時若 current_step ≥ 3 且 step2_completed = true，先重跑 Step 2（查詢很快）以取回必要資料。

---

## Step 1：確認目標 Schema 和業務分類

若使用者未提供 `$ARGUMENTS`，詢問：

```
請提供以下資訊：
1. Oracle Schema 名稱（例如：SEMICON、HR、SALES）
2. 業務領域分類（填 ALL 表示自動依表名前綴分組）
3. 輸出目錄名稱（預設：dict）
```

若有提供 `$ARGUMENTS`：
- 第一個參數 = schema
- 第二個參數 = 逗號分隔的模組清單（可省略，預設 ALL）
- `--output=xxx` = 輸出目錄名稱（可省略，預設 dict）

---

## Step 1b：驗證 Schema 是否存在（必做）

```sql
SELECT OWNER, COUNT(*) AS TABLE_COUNT
FROM ALL_TABLES
WHERE OWNER = '<USER_INPUT_SCHEMA>'
GROUP BY OWNER
```

- **有結果** → 記錄 TABLE_COUNT，繼續 Step 1c。
- **無結果** → 列出候選清單讓使用者確認：

```sql
SELECT OWNER, COUNT(*) AS TABLE_COUNT
FROM ALL_TABLES
WHERE OWNER NOT IN (
  'SYS','SYSTEM','DBSNMP','APPQOSSYS','AUDSYS','DVSYS',
  'GSMADMIN_INTERNAL','LBACSYS','OUTLN','VECSYS','XDB',
  'DBSFWUSER','WMSYS','OJVMSYS','CTXSYS','MDSYS'
)
GROUP BY OWNER
ORDER BY TABLE_COUNT DESC
```

等待使用者選擇後再繼續。

---

## Step 1c：大 Schema 保護（表數 > 50）

若 Step 1b 的 TABLE_COUNT > 50：

```
⚠️ 此 Schema 有 <N> 張表，完整掃描需要大量查詢。
建議方式：
  1. 用前綴過濾（例如只掃 CP_%, WAT_%）
  2. 分批處理（每批 20 張表，產出後再繼續下一批）
  3. 全部掃描（可能需要較長時間）

請選擇（1/2/3）：
```

- 選 1 → 記錄前綴過濾條件，Step 2 的 SQL 加上 `TABLE_NAME LIKE '<PREFIX>%'`
- 選 2 → 記錄批次大小，Step 3 會分批執行
- 選 3 → 全部執行，但 Step 3 仍限制每波最多 6 張表平行

---

## ⚠️ MCP 已知限制與繞過方式（執行前必讀）

此 MCP（`mcp__oracle__run_sql_query`）存在以下解析 bug，**實測確認**：

| 觸發條件 | 錯誤訊息 | 繞過方式 |
|---------|---------|---------|
| SELECT 欄位含 NULL 值的 NUMBER 欄位（如 DATA_PRECISION）| `not enough values to unpack (expected 2, got 0)` | 用 `NVL(TO_CHAR(欄位), '*')` 將 NULL 轉字串 |
| `ALL_TAB_COLUMNS` 同時 SELECT 超過 4 個欄位 | 同上 | 拆成多個查詢，每次最多 4 欄 |
| `IN ('TABLE1','TABLE2',...)` 批次查詢 `ALL_TAB_COLUMNS` | 同上 | 每張表單獨查，不要用 IN 合併 |

**正確的查詢模式：**
```sql
-- ✅ 每張表獨立查，最多 4 欄
SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE
FROM ALL_TAB_COLUMNS
WHERE OWNER = '<SCHEMA>' AND TABLE_NAME = '<ONE_TABLE>'
ORDER BY COLUMN_ID

-- ✅ NULL 欄位用 NVL(TO_CHAR(...), '*')
SELECT TABLE_NAME, COLUMN_NAME,
       NVL(TO_CHAR(DATA_PRECISION), '*') AS PREC,
       NVL(TO_CHAR(DATA_SCALE), '*')     AS SCAL
FROM ALL_TAB_COLUMNS
WHERE OWNER = '<SCHEMA>' AND DATA_TYPE = 'NUMBER'
ORDER BY TABLE_NAME, COLUMN_ID
```

---

## Step 2：掃描 Schema — 五個獨立查詢（可全部平行執行）

**2a. 表清單和筆數：**
```sql
SELECT TABLE_NAME, NVL(NUM_ROWS, 0) AS NUM_ROWS
FROM ALL_TABLES
WHERE OWNER = '<SCHEMA>'
ORDER BY TABLE_NAME
```

**2b. Table Comment：**
```sql
SELECT TABLE_NAME, COMMENTS AS TABLE_COMMENT
FROM ALL_TAB_COMMENTS
WHERE OWNER = '<SCHEMA>'
  AND COMMENTS IS NOT NULL
ORDER BY TABLE_NAME
```

**2c. 欄位 Comment：**
```sql
SELECT TABLE_NAME, COLUMN_NAME, COMMENTS
FROM ALL_COL_COMMENTS
WHERE OWNER = '<SCHEMA>'
  AND COMMENTS IS NOT NULL
ORDER BY TABLE_NAME, COLUMN_NAME
```

**2d. FK 約束：**
```sql
SELECT ac.TABLE_NAME, acc.COLUMN_NAME,
       arc.TABLE_NAME AS REF_TABLE,
       arcc.COLUMN_NAME AS REF_COLUMN
FROM ALL_CONSTRAINTS ac
JOIN ALL_CONS_COLUMNS acc
  ON acc.OWNER = ac.OWNER AND acc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME
JOIN ALL_CONSTRAINTS arc
  ON arc.CONSTRAINT_NAME = ac.R_CONSTRAINT_NAME AND arc.OWNER = ac.R_OWNER
JOIN ALL_CONS_COLUMNS arcc
  ON arcc.CONSTRAINT_NAME = arc.CONSTRAINT_NAME
 AND arcc.OWNER = arc.OWNER AND arcc.POSITION = acc.POSITION
WHERE ac.CONSTRAINT_TYPE = 'R'
  AND ac.OWNER = '<SCHEMA>'
ORDER BY ac.TABLE_NAME, acc.POSITION
```

**2e. PK / UNIQUE 約束：**
```sql
SELECT ac.TABLE_NAME, acc.COLUMN_NAME, ac.CONSTRAINT_TYPE
FROM ALL_CONSTRAINTS ac
JOIN ALL_CONS_COLUMNS acc
  ON acc.OWNER = ac.OWNER AND acc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME
WHERE ac.OWNER = '<SCHEMA>'
  AND ac.CONSTRAINT_TYPE IN ('P', 'U')
ORDER BY ac.TABLE_NAME, acc.POSITION
```

> 2a-2e 可全部平行發送。完成後更新 checkpoint。
> 若任何查詢失敗（如 2b 觸發 MCP bug），跳過該查詢並在 reviewItems 中標記。

---

## Step 3：取每張表的欄位結構

> **限制**：每波最多 6 張表平行查詢，完成後再發下一波。
> 每完成一波，更新 checkpoint 的 `tables_done` 清單。

**3a. 每張表單獨查基本結構（每次 4 欄）：**
```sql
SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE
FROM ALL_TAB_COLUMNS
WHERE OWNER = '<SCHEMA>' AND TABLE_NAME = '<TABLE_NAME>'
ORDER BY COLUMN_ID
```

**3b. 一次查所有 NUMBER 欄位精度（用 NVL 繞過 NULL bug）：**
```sql
SELECT TABLE_NAME, COLUMN_NAME,
       NVL(TO_CHAR(DATA_PRECISION), '*') AS PREC,
       NVL(TO_CHAR(DATA_SCALE), '*')     AS SCAL
FROM ALL_TAB_COLUMNS
WHERE OWNER = '<SCHEMA>'
  AND DATA_TYPE = 'NUMBER'
ORDER BY TABLE_NAME, COLUMN_ID
```

**3c. 索引資訊（一次取全 Schema）：**
```sql
SELECT i.TABLE_NAME, i.INDEX_NAME, i.UNIQUENESS, ic.COLUMN_NAME
FROM ALL_INDEXES i
JOIN ALL_IND_COLUMNS ic
  ON ic.INDEX_NAME = i.INDEX_NAME AND ic.TABLE_OWNER = i.OWNER
WHERE i.OWNER = '<SCHEMA>'
ORDER BY i.TABLE_NAME, i.INDEX_NAME, ic.COLUMN_POSITION
```

> 3b 和 3c 只需各執行一次（不是逐表）。
> 3a 逐表執行，每波 6 張平行。

**自動偵測特殊欄位：**

| 類型 | 欄位名稱特徵 |
|------|------------|
| 軟刪除 | 含 DEL / DELETE / VALID / INACTIVE / FLAG / ACTIVE |
| 建立時間 | CREATE_TIME / CREATE_DATE / CTIME / INS_DATE |
| 更新時間 | UPDATE_TIME / MOD_DATE / UPD_DATE / MTIME |
| 主鍵代理 | 結尾為 _ID / _NO / _KEY / _SEQ |

---

## Step 4：模組自動分組與 LLM 推理

### 4a. 模組自動分組（當分類為 ALL）

取表名第一個 `_` 前的前綴，相同前綴歸為同一個模組。

**相似前綴合併規則：**
- 一個前綴的表只有 1 張 → 歸入最相似的其他模組（比較前綴字串相似度）
- 若找不到相似模組 → 歸入 `MISC`（雜項）模組
- 無 `_` 的表名 → 全部歸入 `GENERAL` 模組
- 前綴只差 1-2 個字元（如 SYS vs SYSTEM）→ 合併為較長的前綴名

分組完成後列出結果，詢問使用者確認：
```
自動分組結果：
  CP (3 張表): CP_LOT, CP_WAFER, CP_BIN_SUMMARY
  WAT (3 張表): WAT_LOT, WAT_PARAM, WAT_RESULT
  MISC (1 張表): SYSTEM_LOG

是否需要調整分組？（直接按 Enter 表示確認）
```

### 4b. 推理空白的 Table / Column Comment

若 DB comment 為空，依名稱後綴推理，標記 `comment_inferred: true`：

| 後綴 | Table 推理 | Column 推理 |
|------|-----------|------------|
| `_LOT/_BATCH` | 批次/工單主檔 | — |
| `_WAFER/_ITEM` | 明細層資料 | — |
| `_PARAM/_DEF` | 參數/定義主檔 | — |
| `_RESULT/_MEAS` | 量測/結果明細 | — |
| `_SUMMARY/_AGG` | 統計彙總 | — |
| `_SPEC/_RULE` | 規格/規則定義 | — |
| `_HIST/_LOG` | 歷史/稽核紀錄 | — |
| `_MAP/_REL/_CONFIG` | 對應/設定 | — |
| `_ID` | — | 唯一識別碼 |
| `_NO/_NUMBER` | — | 流水號或編號 |
| `_NAME/_DESC` | — | 名稱或說明 |
| `_DATE/_TIME` | — | 日期或時間 |
| `_FLAG/_YN` | — | 旗標（Y/N 或 1/0）|
| `_COUNT/_CNT/_AMOUNT/_AMT` | — | 數量或金額 |
| `_RATE/_PCT` | — | 比率或百分比 |
| `_STATUS/_STATE/_TYPE/_CODE` | — | 狀態/類型/代碼 |
| `USL/LSL/UCL/LCL` | — | 規格/管制上下限 |

Table 推理前綴加模組名（如 `{CP} 批次/工單主檔`）。Column 搭配模組語境調整。
無法判斷 → `"[待補充] 欄位用途不明"` + `inferred: false`。

### 4d. 推理 JOIN 關係（無 FK 時，依優先序執行）

**策略一（高）：欄位名完全符合其他表的 PK 欄位名**
```
WAT_RESULT.PARAM_ID = WAT_PARAM.PARAM_ID（PK）→ confidence: high
```

**策略二（中）：欄位名 = 其他表名去除前綴 + _ID**
```
CP_WAFER.LOT_ID → CP_LOT → confidence: medium
```

**策略三（低）：表名後綴語意推理**
```
_LOT/_MASTER = 主表; _DETAIL/_ITEM = 明細 → confidence: low
```

**策略四（必做）：孤兒資料驗證**
```sql
SELECT COUNT(*) AS ORPHAN_COUNT
FROM <SCHEMA>.<明細表> d
WHERE NOT EXISTS (
  SELECT 1 FROM <SCHEMA>.<主表> m WHERE m.<PK> = d.<FK>
)
```
- ORPHAN_COUNT = 0 → `verified: true`，confidence 升一級
- ORPHAN_COUNT > 0 → `suspicious: true` + 加入 reviewItems

---

## Step 5：寫出 \<output_dir\>/schema/\<TABLE_NAME\>.yaml

> 寫入前檢查檔案是否已存在且 YAML 可解析，若檔案損壞則覆寫。
> 每寫完一張表，更新 checkpoint。

```yaml
# AUTO-GENERATED by /gen-dict-v3 — 請勿手動修改
# Generated: <ISO timestamp>
table: <TABLE_NAME>
db_schema: <SCHEMA>
estimated_rows: <NUM_ROWS>
db_comment: "<TABLE_COMMENT>"
comment_inferred: <true|false>

columns:
  <COLUMN_NAME>:
    type: <DATA_TYPE>
    length: <DATA_LENGTH>
    precision: <DATA_PRECISION or null>
    scale: <DATA_SCALE or null>
    nullable: <Y/N>
    db_comment: "<COMMENT>"
    comment_inferred: <true|false>

indexes:
  - name: <INDEX_NAME>
    unique: <true|false>
    columns: [<COL1>, <COL2>]

foreignKeys:
  - column: <COL>
    refTable: <REF_TABLE>
    refColumn: <REF_COL>
    source: <db_constraint|naming_convention|semantic_inference>
    confidence: <high|medium|low>
    verified: <true|false>
    suspicious: <true|false>

primaryKey: [<PK_COLS>]

autoDetected:
  softDeleteColumn: <欄位名 or null>
  timestampColumns: [<欄位名列表>]
  keyColumns: [<結尾為 _ID/_NO 的欄位列表>]

reviewItems:
  - <需要人工確認的項目描述>
```

---

## Step 6：寫出 \<output_dir\>/business/module_\<prefix\>.yaml

> **規則**：所有 SQL 必須加 `<SCHEMA>.` 前綴在表名前。

```yaml
module: <MODULE_ID>
version: "<YYYY-MM-DD>"
description: <推測的業務說明>
description_inferred: true
triggerKeywords: [<模組ID>, <推測的中文關鍵詞>]

businessRules:
  - rule: <規則名稱>
    sqlPattern: "<Oracle SQL 條件>"
    appliesTo: [<表名>]
    confidence: auto
    note: "<推測依據>"

tables:           # 每張表含 description, description_inferred, columns（結構同 Step 5）
joinPatterns:     # 含 scenario, sql, source, confidence, verified（結構同 Step 5 foreignKeys）

queryPatterns:
  - id: QP-<MODULE>-001
    description: <常見查詢場景>
    triggers: [<觸發關鍵詞>]
    sql: |        # 必須含 FETCH FIRST 100 ROWS ONLY
      SELECT ... FROM <SCHEMA>.<TABLE> ... WHERE ...
    confidence: auto
```

---

## Step 7：寫出 \<output_dir\>/\_index.yaml

```yaml
# AUTO-GENERATED by /gen-dict-v3
# Generated: <ISO timestamp>
db_schema: <SCHEMA>
generated_at: "<YYYY-MM-DD>"

modules:
  - id: <MODULE_ID>
    description: <推測描述>
    triggerKeywords: [<關鍵詞列表>]
    file: business/module_<prefix>.yaml
    tables: [<TABLE_LIST>]
```

---

## Step 8：寫出 \<output_dir\>/\_global\_rules.yaml

**若檔案已存在則跳過**（不覆蓋使用者修改）。

```yaml
dialect: oracle

forbiddenSyntax:
  - "LIMIT n              → 改用 FETCH FIRST n ROWS ONLY"
  - "?                    → 改用 :param_name"
  - "DATE_TRUNC           → 改用 TRUNC(col, 'MM')"
  - "NOW()                → 改用 SYSDATE"
  - "GETDATE()            → 改用 SYSDATE"
  - "TOP n                → 改用 FETCH FIRST n ROWS ONLY"
  - "ISNULL()             → 改用 NVL() 或 COALESCE()"
  - "information_schema   → 改用 ALL_TABLES / ALL_TAB_COLUMNS"

extraRules:
  - "禁止 INSERT / UPDATE / DELETE / DDL（唯讀存取）"
  - "多表查詢必須有明確 JOIN ON 條件，禁止笛卡兒積"
  - "分頁查詢用 FETCH FIRST n ROWS ONLY"
  - "預設最多回傳 100 筆，大量資料需明確說明"
  - "除法運算使用 NULLIF 防止除以零"
  - "VALID_FLAG = 1 應為預設過濾條件（除非明確要查作廢資料）"
  - "所有表名前必須加 Schema 前綴（如 SEMI.CP_LOT）"
```

---

## Step 9：品質驗證

**9a. 完整性檢查**
- [ ] 每張表都有 schema YAML
- [ ] 每個模組都有 business YAML
- [ ] `_index.yaml` 的 tables 清單與 schema YAML 數一致
- [ ] `_global_rules.yaml` 存在

**9b. 欄位品質檢查**
- [ ] 無空字串 `db_comment`（至少有推理值或 `[待補充]`）
- [ ] `comment_inferred: false` 的欄位確實有 DB comment
- [ ] `primaryKey` 清單非空

**9c. JOIN 驗證**
- [ ] 所有推斷 JOIN 都執行過孤兒資料查詢
- [ ] `suspicious: true` 的都在 reviewItems 中

**9d. SQL 語法檢查**
- [ ] queryPatterns 的 SQL 都有 `FETCH FIRST` 限制
- [ ] 無 `LIMIT`、`TOP`、`?` 等非 Oracle 語法
- [ ] 所有表名都帶 `<SCHEMA>.` 前綴

**9e. 抽樣查詢驗證**

對每個模組的 QP-001 查詢執行語法驗證：
```sql
SELECT * FROM (
  <原始 SQL，將所有 :xxx 參數替換為 NULL>
) WHERE ROWNUM = 0
```
- 查詢成功（回傳 0 筆）→ ✅ 語法正確
- 查詢報錯 → 修正 SQL 並更新 YAML

---

## Step 10：清除 checkpoint 並輸出摘要

刪除 repo memory 中的 checkpoint（`/memories/repo/gen-dict-checkpoint.md`），輸出摘要：

```
=== /gen-dict-v3 完成 ===
Schema: <SCHEMA>（<N> 張表）| 輸出: <output_dir>/
schema/: XX 張（✅ DB comment 齊全 / ⚠️ LLM 推理）
business/: X 個模組
JOIN: db_constraint X | naming X (verified/suspicious) | semantic X
驗證: 完整性 ✅ | 欄位 ✅ | SQL ✅ | 抽樣 X/X ✅
⚠️ 需人工確認: [具體 reviewItems]
下一步: 確認 reviewItems 後可擴充 queryPatterns
```
