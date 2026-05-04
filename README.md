# Text2SQL — MCP + Agent Runtime

自然語言 → PostgreSQL SQL，透過 MCP Server 讓任何 Agent Runtime 都能使用。

## 架構

```
Web UI (Next.js) ←→ Anthropic API (tool_use) ←→ text2sql MCP Server ←→ PostgreSQL + Chroma
Claude Code                                   ←→ text2sql MCP Server
GitHub Copilot                                ←→ text2sql MCP Server
```

## 快速開始

### 1. 啟動 MCP Server

```bash
cd mcp-server
cp .env.example .env       # 填入 PG 密碼 + OpenAI API Key
npm install
npm run dev                # 啟動 MCP Server（stdio 模式）
```

### 2. 初始化字典（首次）

在 Claude Code 中執行：
```
初始化字典
```
Claude 會自動呼叫 `build_dict()` 將字典寫入向量 DB。

### 3. 開始使用

**CLI（Claude Code）：**
```bash
claude    # 自動載入 .claude/CLAUDE.md + mcp.json
```

**Web UI：**
```bash
cd web
cp .env.example .env       # 填入 ANTHROPIC_API_KEY
npm install
npm run dev                # http://localhost:3000
```

**GitHub Copilot（VS Code）：**
在 VS Code settings.json 加入：
```json
{
  "github.copilot.chat.mcp.enabled": true,
  "mcp": {
    "servers": {
      "text2sql": {
        "command": "npx",
        "args": ["tsx", "src/index.ts"],
        "cwd": "${workspaceFolder}/mcp-server"
      }
    }
  }
}
```

## 新增業務模組字典

1. 在 `mcp-server/dict/` 建立 `module_xxx.yaml`（參考 `module_orders.yaml`）
2. 在 `_index.yaml` 新增模組索引
3. 在 Claude 中說「同步 schema」或「初始化字典」

## 目錄結構

```
text2sql/
├── mcp-server/
│   ├── src/
│   │   ├── index.ts         # MCP Server 入口
│   │   ├── pgClient.ts      # PostgreSQL 連線
│   │   ├── vectorStore.ts   # Chroma 向量 DB
│   │   ├── dictLoader.ts    # YAML 字典載入
│   │   └── tools/
│   │       ├── dbTools.ts       # execute_query, describe_table
│   │       ├── dictTools.ts     # list_modules, get_dict, search_dict
│   │       ├── vectorTools.ts   # find_similar_qa, save_interaction, rate_interaction
│   │       └── adminTools.ts    # build_dict, sync_dict, purge_history
│   └── dict/
│       ├── _index.yaml
│       ├── _global_rules.yaml
│       └── module_orders.yaml
├── web/
│   ├── app/
│   │   ├── page.tsx         # Chat UI
│   │   └── api/chat/route.ts  # Anthropic API + agent loop
│   └── .env.example
├── .claude/CLAUDE.md        # Claude Code agent skills
├── .github/copilot-instructions.md  # Copilot agent skills
└── mcp.json                 # MCP 設定
```
