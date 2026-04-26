---
name: text2sql
description: '查詢 Oracle 資料庫。Use when: 使用者要查 Oracle、查資料庫、查SQL、查良率、查批次、查 WAT、查 CP、text2sql、幫我查、查一下、跑 SQL、執行查詢。Converts natural language to Oracle SQL, executes via MCP, returns results.'
---

# Text2SQL — 自然語言查詢 Oracle

將使用者的自然語言問題轉為 Oracle SQL，自動執行並回傳結果。

## 觸發條件

使用者提出資料查詢需求，例如：
- 「查一下 A廠上個月的批次良率」
- 「WAT 量測值超規的批次有哪些」
- 「CP 良率低於 0.9 的 wafer」
- 任何涉及 Oracle / SQL / 資料庫 / 查詢 的請求

## 前置條件

- Oracle MCP server 必須已啟動（`http://127.0.0.1:8000/mcp`）
- 環境變數 `OPENAI_API_KEY` 已設定

## 執行流程

### Step 1：從 ES 取得上下文

在終端機執行 retrieve_context.py，將使用者的原始問題作為參數傳入：

```powershell
cd H:\copilotCli; python scripts/retrieve_context.py "<使用者的原始問題>"
```

**讀取 stdout 輸出**，那是組裝好的完整 prompt（包含表結構、欄位對應、JOIN 路徑、業務規則、查詢樣板）。stderr 的日誌可忽略。

### Step 2：根據上下文產生 Oracle SQL

閱讀 Step 1 拿到的 prompt 全文，嚴格遵守其中的規則產生 **一條** Oracle SQL。

核心規則（prompt 中會有更完整版本）：
- 方言：Oracle — 禁止 `LIMIT`（用 `FETCH FIRST n ROWS ONLY`）、禁止 `NOW()`（用 `SYSDATE`）
- 所有表名加 Schema 前綴：`SEMI.<TABLE_NAME>`
- 多表必須有明確 `JOIN ON`，禁止笛卡兒積
- 除法用 `NULLIF()` 防除以零
- 預設加 `VALID_FLAG = 1`（除非使用者要查作廢資料）
- 預設 `FETCH FIRST 100 ROWS ONLY`
- 禁止 `INSERT` / `UPDATE` / `DELETE` / DDL
- 禁止 `SELECT *`，只選需要的欄位

### Step 2.5：儲存產生的 SQL

將產生的 SQL 存檔，在終端機執行：

```powershell
$sql = @"
<產生的 SQL>
"@
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$sql | Out-File -Encoding utf8 "H:\copilotCli\logs\${ts}_sql.sql"
```

這樣 `H:\copilotCli\logs\` 裡會有成對的檔案：
- `*_prompt.md` — retrieve_context.py 自動儲存的 prompt
- `*_sql.sql` — 產生的 SQL

### Step 3：透過 Oracle MCP 執行 SQL

使用 Oracle MCP 的 **`run_sql_query`** 工具執行 Step 2 產生的 SQL。

如果報錯：
1. 閱讀錯誤訊息（常見：表名錯誤 `ORA-00942`、欄位不存在 `ORA-00904`、語法錯誤 `ORA-00933`）
2. 根據 Step 1 的 prompt 修正 SQL
3. 重新執行，**最多重試 2 次**

### Step 4：整理結果回覆

回覆格式：

1. **SQL**：用 ` ```sql ` 程式碼區塊顯示最終執行的 SQL
2. **結果**：用 Markdown 表格呈現查詢結果
3. **摘要**：一句話總結查詢結果的含義
4. 如果結果為空，說明可能原因（條件太嚴、時間範圍無資料等）
5. 如果問題太模糊無法產生 SQL，向使用者提問釐清

## 錯誤處理

| 狀況 | 處理 |
|------|------|
| retrieve_context.py 執行失敗 | 檢查 OPENAI_API_KEY 是否設定、ES 是否運行中 |
| Oracle MCP 連線失敗 | 提醒使用者啟動 Oracle MCP server |
| SQL 執行錯誤（ORA-*） | 根據 prompt 上下文修正 SQL，最多重試 2 次 |
| 結果超過 100 筆 | 詢問使用者是否需要調整條件或增加回傳筆數 |
