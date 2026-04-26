"""
Text2SQL 資料字典 → Elasticsearch 載入工具 v2

配合流程設計：
- text2sql_schema: 資料字典（table/rule/join/pattern），支援向量+關鍵字混合搜尋
- text2sql_qa:     歷史問答快取（Phase 1 快取比對 + Phase 5 回饋飛輪）

Embedding: OpenAI text-embedding-3-small (1536 dims)
"""

import yaml
import json
import os
import urllib.request
import urllib.error
import base64
from pathlib import Path

# ── 設定 ──────────────────────────────────────────────
ES_URL = "http://localhost:1200"
ES_USER = "elastic"
ES_PASS = "infini_rag_flow"
DICT_DIR = Path(r"H:\copilotCli\dict")

INDEX_SCHEMA = "text2sql_schema"
INDEX_QA = "text2sql_qa"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = 1536


# ── ES 工具 ───────────────────────────────────────────
def es_request(method: str, path: str, body=None):
    url = f"{ES_URL}/{path}"
    data = None
    if body is not None:
        if isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    cred = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8")
        if e.code == 404:
            return None
        print(f"  HTTP {e.code}: {body_text[:300]}")
        raise


# ── OpenAI Embedding ─────────────────────────────────
def get_embeddings(texts: list[str]) -> list[list[float]]:
    """批量取得 OpenAI embeddings"""
    if not OPENAI_API_KEY:
        raise ValueError("請設定 OPENAI_API_KEY 環境變數")

    url = "https://api.openai.com/v1/embeddings"
    payload = json.dumps({
        "model": EMBEDDING_MODEL,
        "input": texts
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    # 按 index 排序確保順序正確
    sorted_data = sorted(result["data"], key=lambda x: x["index"])
    return [d["embedding"] for d in sorted_data]


def get_embedding(text: str) -> list[float]:
    return get_embeddings([text])[0]


# ── 索引建立 ──────────────────────────────────────────
def create_schema_index():
    """
    text2sql_schema 索引設計

    chunk_type 對應流程 Phase 2 的分組:
      - table    → tables[]    : 表結構定義（describe_table 用）
      - rule     → rules[]     : 業務規則
      - join     → joins[]     : JOIN 樣板
      - pattern  → patterns[]  : 查詢樣板
    """
    mapping = {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0
        },
        "mappings": {
            "properties": {
                # ── 分類欄位（用於 filter） ──
                "chunk_type": {"type": "keyword"},       # table | rule | join | pattern | column
                "module": {"type": "keyword"},            # CP | WAT | GLOBAL
                "tables": {"type": "keyword"},            # 涉及的表名列表（Phase 2b 補齊用）

                # ── 搜尋欄位 ──
                "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "content": {"type": "text"},              # LLM 可讀的完整內容
                "triggers": {"type": "keyword"},          # 精確觸發詞

                # ── Schema Linking 欄位 ──
                "column_name": {"type": "keyword"},       # 欄位名（column chunk 用）
                "sample_values": {"type": "keyword"},     # 實際值範例（用於 value matching）
                "value_mapping": {"type": "object", "enabled": False},  # 口語→實際值

                # ── SQL 欄位 ──
                "sql": {"type": "text", "index": False},  # 不索引，只儲存

                # ── 向量欄位（語意搜尋） ──
                "embedding": {
                    "type": "dense_vector",
                    "dims": EMBEDDING_DIMS,
                    "index": True,
                    "similarity": "cosine"
                },

                # ── 後設資料 ──
                "metadata": {"type": "object", "enabled": False}
            }
        }
    }

    try:
        es_request("DELETE", INDEX_SCHEMA)
        print(f"  [刪除] 舊索引 {INDEX_SCHEMA}")
    except:
        pass

    es_request("PUT", INDEX_SCHEMA, mapping)
    print(f"  [建立] 索引 {INDEX_SCHEMA}")


def create_qa_index():
    """
    text2sql_qa 索引設計

    Phase 1 快取比對 + Phase 5 回饋飛輪
    """
    mapping = {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0
        },
        "mappings": {
            "properties": {
                # ── 問答內容 ──
                "question": {"type": "text"},             # 使用者原始問題
                "sql": {"type": "text", "index": False},  # 生成的 SQL
                "module": {"type": "keyword"},            # 所屬模組
                "tables": {"type": "keyword"},            # 涉及的表

                # ── 向量欄位（Phase 1 相似度比對） ──
                "embedding": {
                    "type": "dense_vector",
                    "dims": EMBEDDING_DIMS,
                    "index": True,
                    "similarity": "cosine"
                },

                # ── 回饋飛輪 ──
                "rating": {"type": "integer"},            # 使用者評分 1-5
                "is_golden": {"type": "boolean"},         # 黃金範例標記
                "time_sensitive": {"type": "boolean"},    # 時間敏感（不快取）
                "feedback": {"type": "text", "index": False},  # 使用者回饋文字

                # ── 時間戳 ──
                "created_at": {"type": "date"},
                "updated_at": {"type": "date"}
            }
        }
    }

    try:
        es_request("DELETE", INDEX_QA)
        print(f"  [刪除] 舊索引 {INDEX_QA}")
    except:
        pass

    es_request("PUT", INDEX_QA, mapping)
    print(f"  [建立] 索引 {INDEX_QA}")


# ── YAML 解析 ─────────────────────────────────────────
def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_column_enrichment(schema_dir: Path) -> dict:
    """載入欄位補充資料（synonyms, sampleValues, valueMapping）"""
    enrich_file = schema_dir / "_column_enrichment.yaml"
    if enrich_file.exists():
        data = load_yaml(enrich_file)
        print(f"  [enrichment] 載入 {sum(len(v) for v in data.values())} 個欄位補充")
        return data or {}
    print("  [enrichment] 未找到 _column_enrichment.yaml，跳過")
    return {}


def build_table_chunks(schema_dir: Path, enrichment: dict = None) -> list[dict]:
    """
    每張表一個 chunk，content 格式化為 LLM 可直接讀取的 DDL 風格

    這是 describe_table() 的資料來源
    """
    enrichment = enrichment or {}
    chunks = []
    for yaml_file in sorted(schema_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue  # 跳過 _column_enrichment.yaml 等輔助檔
        data = load_yaml(yaml_file)
        table = data["table"]
        schema = data.get("db_schema", "SEMI")
        full_name = f"{schema}.{table}"
        comment = data.get("db_comment", "")
        table_enrich = enrichment.get(table, {})

        # 格式化為 LLM 友好的表定義
        lines = [f"-- {comment}", f"-- 表名: {full_name}", ""]
        for col_name, col_info in data.get("columns", {}).items():
            col_type = col_info.get("type", "")
            precision = col_info.get("precision")
            scale = col_info.get("scale")
            length = col_info.get("length")
            nullable = col_info.get("nullable", "Y")
            col_comment = col_info.get("db_comment", "")

            # 型態字串
            if col_type == "NUMBER" and precision:
                type_str = f"NUMBER({precision},{scale})" if scale else f"NUMBER({precision})"
            elif col_type == "VARCHAR2" and length:
                type_str = f"VARCHAR2({length})"
            else:
                type_str = col_type

            null_str = "NOT NULL" if nullable == "N" else "NULL"

            # 加入 enrichment 的 sampleValues
            col_enrich = table_enrich.get(col_name, {})
            sample_str = ""
            if col_enrich.get("sampleValues"):
                sample_str = f"  例: {', '.join(str(v) for v in col_enrich['sampleValues'][:5])}"

            lines.append(f"  {col_name:<20s} {type_str:<20s} {null_str:<10s} -- {col_comment}{sample_str}")

        content = "\n".join(lines)

        # 用於向量搜尋的文字（表名+說明+所有欄位名和說明）
        search_text = f"{full_name} {comment} " + " ".join(
            f"{col} {info.get('db_comment', '')}"
            for col, info in data.get("columns", {}).items()
        )

        module = "CP" if table.startswith("CP_") else "WAT" if table.startswith("WAT_") else "OTHER"

        chunks.append({
            "chunk_type": "table",
            "module": module,
            "tables": [table],
            "title": f"{full_name} - {comment}",
            "content": content,
            "sql": None,
            "triggers": [table, full_name, table.lower()],
            "_search_text": search_text,  # 用於生成 embedding，不寫入 ES
            "metadata": {"db_schema": schema, "estimated_rows": data.get("estimated_rows")}
        })
    return chunks


def build_column_chunks(schema_dir: Path, enrichment: dict = None) -> list[dict]:
    """
    欄位級別 chunks — Schema Linking 用

    每個欄位一個 chunk，搜尋文字包含:
    - 欄位名 + 說明 + 同義詞 + 範例值
    讓向量搜尋能精準到「使用者的某個詞 → 哪張表的哪個欄位」
    """
    enrichment = enrichment or {}
    chunks = []
    for yaml_file in sorted(schema_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        data = load_yaml(yaml_file)
        table = data["table"]
        schema_name = data.get("db_schema", "SEMI")
        full_name = f"{schema_name}.{table}"
        module = "CP" if table.startswith("CP_") else "WAT" if table.startswith("WAT_") else "OTHER"
        table_enrich = enrichment.get(table, {})

        for col_name, col_info in data.get("columns", {}).items():
            col_comment = col_info.get("db_comment", "")
            col_type = col_info.get("type", "")
            col_enrich = table_enrich.get(col_name, {})

            synonyms = col_enrich.get("synonyms", [])
            sample_values = col_enrich.get("sampleValues", [])
            value_mapping = col_enrich.get("valueMapping", {})
            notes = col_enrich.get("notes", "")

            # content: LLM 可讀的完整欄位資訊
            content_parts = [
                f"表: {full_name}",
                f"欄位: {col_name} ({col_type})",
                f"說明: {col_comment}",
            ]
            if synonyms:
                content_parts.append(f"別名: {', '.join(synonyms)}")
            if sample_values:
                content_parts.append(f"範例值: {', '.join(str(v) for v in sample_values)}")
            if value_mapping:
                mapping_str = ", ".join(f"{k}→{v}" for k, v in value_mapping.items())
                content_parts.append(f"值對應: {mapping_str}")
            if notes:
                content_parts.append(f"備註: {notes}")

            content = "\n".join(content_parts)

            # 搜尋文字: 把所有可能的使用者用語都放進去
            search_parts = [col_name, col_comment] + synonyms
            if sample_values:
                search_parts += [str(v) for v in sample_values]
            if value_mapping:
                search_parts += list(value_mapping.keys())
            if notes:
                search_parts.append(notes)
            search_text = " ".join(search_parts)

            # triggers: 欄位名 + 同義詞（精確匹配用）
            triggers = [col_name, col_name.lower(), f"{table}.{col_name}"] + synonyms

            chunks.append({
                "chunk_type": "column",
                "module": module,
                "tables": [table],
                "column_name": col_name,
                "title": f"{full_name}.{col_name} - {col_comment}",
                "content": content,
                "sql": None,
                "triggers": triggers,
                "sample_values": [str(v) for v in sample_values] if sample_values else None,
                "value_mapping": value_mapping if value_mapping else None,
                "_search_text": search_text,
                "metadata": {
                    "table": table,
                    "column": col_name,
                    "type": col_type,
                    "synonyms": synonyms,
                    "notes": notes,
                }
            })
    return chunks


def build_rule_chunks(business_dir: Path) -> list[dict]:
    """業務規則 chunks"""
    chunks = []
    for yaml_file in sorted(business_dir.glob("module_*.yaml")):
        data = load_yaml(yaml_file)
        module = data["module"]
        keywords = data.get("triggerKeywords", [])

        for rule in data.get("businessRules", []):
            rule_name = rule.get("rule", "")
            sql_pattern = rule.get("sqlPattern", "")
            applies_to = rule.get("appliesTo", [])
            note = rule.get("note", "")

            content = f"[{module} 業務規則] {rule_name}\nSQL 模式: {sql_pattern}\n適用表: {', '.join(applies_to)}\n說明: {note}"
            search_text = f"{rule_name} {note} {sql_pattern} {' '.join(applies_to)}"

            chunks.append({
                "chunk_type": "rule",
                "module": module,
                "tables": applies_to,
                "title": f"[{module}] {rule_name}",
                "content": content,
                "sql": sql_pattern,
                "triggers": keywords + applies_to,
                "_search_text": search_text,
                "metadata": {"confidence": rule.get("confidence")}
            })
    return chunks


def build_join_chunks(business_dir: Path) -> list[dict]:
    """JOIN 樣板 chunks — Phase 2b 用 metadata.tables 補齊關聯表"""
    chunks = []
    for yaml_file in sorted(business_dir.glob("module_*.yaml")):
        data = load_yaml(yaml_file)
        module = data["module"]
        keywords = data.get("triggerKeywords", [])
        all_tables = list(data.get("tables", {}).keys())

        for jp in data.get("joinPatterns", []):
            scenario = jp.get("scenario", "")
            sql = jp.get("sql", "")

            content = f"[{module} JOIN 樣板] {scenario}\n\n{sql}"
            search_text = f"{scenario} {' '.join(all_tables)} JOIN 關聯"

            chunks.append({
                "chunk_type": "join",
                "module": module,
                "tables": all_tables,  # Phase 2b 用這個欄位找關聯表
                "title": f"[{module}] JOIN: {scenario}",
                "content": content,
                "sql": sql,
                "triggers": keywords + [scenario],
                "_search_text": search_text,
                "metadata": {
                    "confidence": jp.get("confidence"),
                    "verified": jp.get("verified"),
                    "tables": all_tables  # 冗餘存一份在 metadata 供程式取用
                }
            })
    return chunks


def build_pattern_chunks(business_dir: Path) -> list[dict]:
    """查詢樣板 chunks — Phase 1 的弱參考 + Phase 2 的 patterns"""
    chunks = []
    for yaml_file in sorted(business_dir.glob("module_*.yaml")):
        data = load_yaml(yaml_file)
        module = data["module"]
        keywords = data.get("triggerKeywords", [])
        all_tables = list(data.get("tables", {}).keys())

        for qp in data.get("queryPatterns", []):
            qp_id = qp.get("id", "")
            desc = qp.get("description", "")
            sql = qp.get("sql", "")
            triggers = qp.get("triggers", [])

            content = f"[{module} 查詢樣板] {desc}\n觸發詞: {', '.join(triggers)}\n\n{sql}"
            search_text = f"{desc} {' '.join(triggers)}"

            chunks.append({
                "chunk_type": "pattern",
                "module": module,
                "tables": all_tables,
                "title": f"[{module}] {desc}",
                "content": content,
                "sql": sql,
                "triggers": keywords + triggers,
                "_search_text": search_text,
                "metadata": {
                    "id": qp_id,
                    "confidence": qp.get("confidence"),
                    "tables": all_tables
                }
            })
    return chunks


# ── 寫入 ES ───────────────────────────────────────────
def bulk_index(index: str, chunks: list[dict]):
    """批量寫入，含 embedding"""
    # 1. 批量生成 embeddings
    search_texts = [c.get("_search_text", c["title"]) for c in chunks]
    print(f"    呼叫 OpenAI embedding ({len(search_texts)} 筆)...")
    embeddings = get_embeddings(search_texts)

    # 2. 組裝 bulk body
    lines = []
    for i, chunk in enumerate(chunks):
        doc_id = f"{chunk['chunk_type']}_{chunk.get('module', 'X')}_{i+1}"
        # 移除內部欄位、None 值
        doc = {k: v for k, v in chunk.items() if v is not None and not k.startswith("_")}
        doc["embedding"] = embeddings[i]

        lines.append(json.dumps({"index": {"_id": doc_id}}, ensure_ascii=False))
        lines.append(json.dumps(doc, ensure_ascii=False))

    body = "\n".join(lines) + "\n"

    req = urllib.request.Request(
        f"{ES_URL}/{index}/_bulk",
        data=body.encode("utf-8"),
        method="POST"
    )
    req.add_header("Content-Type", "application/json")
    cred = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        if result.get("errors"):
            for item in result["items"]:
                idx = item.get("index", {})
                if "error" in idx:
                    print(f"    錯誤: {idx['_id']}: {idx['error']['reason']}")
        return result


# ── 主程式 ────────────────────────────────────────────
def main():
    print("=" * 60)
    print("Text2SQL 資料字典 → Elasticsearch v2")
    print(f"Embedding: {EMBEDDING_MODEL} ({EMBEDDING_DIMS} dims)")
    print("=" * 60)

    if not OPENAI_API_KEY:
        print("\n[錯誤] 請設定環境變數 OPENAI_API_KEY")
        print("  PowerShell: $env:OPENAI_API_KEY = 'sk-...'")
        return

    # ── Step 1: 建立索引 ──
    print("\n[Step 1] 建立索引...")
    create_schema_index()
    create_qa_index()

    # ── Step 2: 解析 YAML ──
    print("\n[Step 2] 解析資料字典...")
    enrichment = load_column_enrichment(DICT_DIR / "schema")
    table_chunks = build_table_chunks(DICT_DIR / "schema", enrichment)
    column_chunks = build_column_chunks(DICT_DIR / "schema", enrichment)
    rule_chunks = build_rule_chunks(DICT_DIR / "business")
    join_chunks = build_join_chunks(DICT_DIR / "business")
    pattern_chunks = build_pattern_chunks(DICT_DIR / "business")

    print(f"  table:   {len(table_chunks)} 筆")
    print(f"  column:  {len(column_chunks)} 筆 (Schema Linking 用)")
    print(f"  rule:    {len(rule_chunks)} 筆")
    print(f"  join:    {len(join_chunks)} 筆")
    print(f"  pattern: {len(pattern_chunks)} 筆")

    all_chunks = table_chunks + column_chunks + rule_chunks + join_chunks + pattern_chunks
    print(f"  共計:    {len(all_chunks)} 筆")

    # ── Step 3: 寫入 ES（含 embedding） ──
    print(f"\n[Step 3] 生成 embedding 並寫入 {INDEX_SCHEMA}...")
    result = bulk_index(INDEX_SCHEMA, all_chunks)
    print(f"  寫入完成! took={result['took']}ms, errors={result['errors']}")

    # ── Step 4: 驗證 ──
    print("\n[Step 4] 驗證...")
    es_request("POST", f"{INDEX_SCHEMA}/_refresh")
    count = es_request("GET", f"{INDEX_SCHEMA}/_count")
    print(f"  {INDEX_SCHEMA}: {count['count']} 筆")

    agg = es_request("POST", f"{INDEX_SCHEMA}/_search", {
        "size": 0,
        "aggs": {"by_type": {"terms": {"field": "chunk_type"}}}
    })
    print("  各類型:")
    for b in agg["aggregations"]["by_type"]["buckets"]:
        print(f"    {b['key']}: {b['doc_count']}")

    # ── Step 5: 測試向量搜尋 ──
    print("\n[Step 5] 測試向量搜尋...")
    test_queries = [
        "查詢批次良率",
        "WAT 量測超規",
        "CP_LOT 和 CP_WAFER 怎麼 JOIN",
    ]
    for q in test_queries:
        print(f"\n  Q: \"{q}\"")
        q_emb = get_embedding(q)
        result = es_request("POST", f"{INDEX_SCHEMA}/_search", {
            "size": 3,
            "knn": {
                "field": "embedding",
                "query_vector": q_emb,
                "k": 3,
                "num_candidates": 25
            },
            "_source": ["chunk_type", "title", "tables"]
        })
        for hit in result["hits"]["hits"]:
            score = hit["_score"]
            src = hit["_source"]
            print(f"    [{src['chunk_type']:<8s}] score={score:.4f}  {src['title']}")

    # ── Step 6: 測試 Schema Linking（column-level 搜尋） ──
    print("\n[Step 6] 測試 Schema Linking（column-level 搜尋）...")
    linking_tests = [
        "A廠",         # 應找到 FAB_ID + valueMapping
        "良率",         # 應找到 CP_WAFER.YIELD
        "上個月",       # 應找到 TEST_TIME / TEST_DATE
        "量測值超規",   # 應找到 MEAS_VALUE + USL/LSL
    ]
    for q in linking_tests:
        print(f"\n  Q: \"{q}\"")
        q_emb = get_embedding(q)
        result = es_request("POST", f"{INDEX_SCHEMA}/_search", {
            "size": 3,
            "knn": {
                "field": "embedding",
                "query_vector": q_emb,
                "k": 5,
                "num_candidates": 30,
                "filter": {"term": {"chunk_type": "column"}}
            },
            "_source": ["title", "sample_values", "value_mapping", "content"]
        })
        for hit in result["hits"]["hits"]:
            score = hit["_score"]
            src = hit["_source"]
            extra = ""
            if src.get("sample_values"):
                extra = f"  vals={src['sample_values'][:3]}"
            if src.get("value_mapping"):
                extra += f"  mapping={src['value_mapping']}"
            print(f"    score={score:.4f}  {src['title']}{extra}")

    print("\n" + "=" * 60)
    print("完成！")
    print(f"  {INDEX_SCHEMA} — 資料字典（向量+關鍵字混合搜尋）")
    print(f"  {INDEX_QA}     — 歷史問答（Phase 1 快取 + Phase 5 回饋）")
    print("=" * 60)

    # ── 印出 global_rules 提示 ──
    print("\n[提醒] global_rules 不存 ES，Phase 2c 直接讀檔注入:")
    print(f"  {DICT_DIR / '_global_rules.yaml'}")


if __name__ == "__main__":
    main()
