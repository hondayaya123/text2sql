"""
Text2SQL 資料字典 → Elasticsearch 載入工具 v3

改進點（相對 v2）：
  1. 新索引 text2sql_v2，upsert 寫入，不刪除既有資料
  2. 穩定 doc ID: module_CP / table_CP_LOT / column_CP_LOT_FAB_ID / pattern_QP-CP-001
  3. 新增 MODULE-level chunk（v2 缺少）
  4. TABLE embed_text 來自 business 模組語意描述，非 DDL 欄位堆疊
  5. PATTERN 增加 parameters 欄位（解析 :param_name → 欄位說明）
  6. PATTERN embed_text 只用 pattern 自身 triggers，不混入 module keywords
  7. PATTERN / JOIN metadata.tables 只含 SQL 實際用到的表（FROM/JOIN 解析）
  8. 從 _index.yaml 讀取模組清單，不硬編碼模組名稱
"""

import yaml
import json
import os
import re
import urllib.request
import urllib.error
import base64
from pathlib import Path

# ── 設定 ──────────────────────────────────────────────
ES_URL   = "http://localhost:1200"
ES_USER  = "elastic"
ES_PASS  = "infini_rag_flow"
DICT_DIR = Path(r"H:\copilotCli\dict")

INDEX_DICT = "text2sql_v2"

OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS  = 1536


# ── ES 工具 ───────────────────────────────────────────
def es_request(method: str, path: str, body=None):
    url  = f"{ES_URL}/{path}"
    data = None
    if body is not None:
        raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        data = raw.encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    cred = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        body_text = e.read().decode("utf-8")
        print(f"  HTTP {e.code}: {body_text[:300]}")
        raise


# ── OpenAI Embedding ──────────────────────────────────
def get_embeddings(texts: list[str]) -> list[list[float]]:
    if not OPENAI_API_KEY:
        raise ValueError("請設定 OPENAI_API_KEY 環境變數")

    payload = json.dumps({"model": EMBEDDING_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=payload, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")

    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    return [d["embedding"] for d in sorted(result["data"], key=lambda x: x["index"])]


# ── 索引建立（upsert 模式） ───────────────────────────
INDEX_MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            # 分類欄位
            "chunk_type":   {"type": "keyword"},
            "module":       {"type": "keyword"},
            "tables":       {"type": "keyword"},
            # 搜尋欄位
            "title":        {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "content":      {"type": "text"},
            "triggers":     {"type": "keyword"},
            # Schema Linking 欄位
            "column_name":  {"type": "keyword"},
            "sample_values":{"type": "keyword"},
            "value_mapping":{"type": "object", "enabled": False},
            # Pattern 參數說明
            "parameters":   {"type": "object", "enabled": False},
            # SQL（只儲存，不索引）
            "sql":          {"type": "text", "index": False},
            # 向量
            "embedding": {
                "type": "dense_vector",
                "dims": EMBEDDING_DIMS,
                "index": True,
                "similarity": "cosine"
            },
            # 後設資料
            "metadata":     {"type": "object", "enabled": False}
        }
    }
}


def ensure_index():
    existing = es_request("GET", f"{INDEX_DICT}/_mapping")
    if existing is None:
        es_request("PUT", INDEX_DICT, INDEX_MAPPING)
        print(f"  [建立] 索引 {INDEX_DICT}")
    else:
        print(f"  [存在] 索引 {INDEX_DICT}（保留既有資料，upsert 模式）")


# ── YAML 載入 ─────────────────────────────────────────
def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── SQL 解析輔助 ──────────────────────────────────────
def extract_sql_tables(sql: str) -> list[str]:
    """從 SQL 的 FROM/JOIN 取出實際使用的表名（去除 schema 前綴）"""
    found = re.findall(r'(?:FROM|JOIN)\s+(?:\w+\.)?(\w+)', sql, re.IGNORECASE)
    seen = {}
    for t in found:
        seen[t] = None  # 去重，保序
    return list(seen)


def extract_sql_params(sql: str) -> list[str]:
    """取出 SQL 裡的 :param_name bind 變數"""
    return re.findall(r':([a-zA-Z_][a-zA-Z0-9_]*)', sql)


# ── Chunk 建構 ────────────────────────────────────────

def build_module_chunks(index_data: dict, business_dir: Path) -> list[dict]:
    """每個模組一個 chunk（v2 缺少）"""
    chunks = []
    for mod in index_data.get("modules", []):
        mod_id   = mod["id"]
        desc     = mod["description"]
        keywords = mod.get("triggerKeywords", [])
        tables   = mod.get("tables", [])

        mod_data = load_yaml(DICT_DIR / mod["file"])
        tables_summary = "\n".join(
            f"  {tname}: {tinfo.get('description', '')}"
            for tname, tinfo in mod_data.get("tables", {}).items()
        )

        content = (
            f"[模組] {mod_id} — {desc}\n\n"
            f"關鍵詞: {', '.join(keywords)}\n\n"
            f"包含表:\n{tables_summary}"
        )
        embed_text = f"{mod_id} {desc} {' '.join(keywords)}"

        chunks.append({
            "_id":        f"module_{mod_id}",
            "chunk_type": "module",
            "module":     mod_id,
            "tables":     tables,
            "title":      f"[模組] {mod_id} — {desc}",
            "content":    content,
            "triggers":   keywords,
            "_embed":     embed_text,
            "metadata":   {"version": mod_data.get("version")}
        })
    return chunks


def build_table_chunks(
    schema_dir: Path,
    business_dir: Path,
    enrichment: dict,
    table_to_module: dict
) -> list[dict]:
    """
    每張表一個 chunk。
    - content: DDL 風格（LLM describe_table 用）
    - embed_text: 來自 business module 的語意描述，非原始 DDL 欄位堆疊
    """
    # 從 business module 取每張表的語意描述
    biz_desc: dict[str, str] = {}
    for yaml_file in sorted(business_dir.glob("module_*.yaml")):
        data = load_yaml(yaml_file)
        for tname, tinfo in data.get("tables", {}).items():
            biz_desc[tname] = tinfo.get("description", "")

    chunks = []
    for yaml_file in sorted(schema_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        data      = load_yaml(yaml_file)
        table     = data["table"]
        schema    = data.get("db_schema", "SEMI")
        full_name = f"{schema}.{table}"
        db_comment = data.get("db_comment", "")
        desc      = biz_desc.get(table, db_comment)
        module    = table_to_module.get(table, "OTHER")
        t_enrich  = enrichment.get(table, {})

        # content: DDL-style
        lines = [f"-- {desc}", f"-- 表名: {full_name}", ""]
        for col_name, col_info in data.get("columns", {}).items():
            col_type  = col_info.get("type", "")
            precision = col_info.get("precision")
            scale     = col_info.get("scale")
            length    = col_info.get("length")
            nullable  = col_info.get("nullable", "Y")
            col_cmt   = col_info.get("db_comment", "")

            if col_type == "NUMBER" and precision:
                type_str = f"NUMBER({precision},{scale})" if scale else f"NUMBER({precision})"
            elif col_type == "VARCHAR2" and length:
                type_str = f"VARCHAR2({length})"
            else:
                type_str = col_type

            null_str = "NOT NULL" if nullable == "N" else "NULL"

            samples = t_enrich.get(col_name, {}).get("sampleValues", [])
            sample_str = f"  例: {', '.join(str(v) for v in samples[:5])}" if samples else ""
            lines.append(f"  {col_name:<20s} {type_str:<20s} {null_str:<10s} -- {col_cmt}{sample_str}")

        pk = data.get("primaryKey", [])
        if pk:
            lines.append(f"\n  PRIMARY KEY ({', '.join(pk)})")
        for fk in data.get("foreignKeys", []):
            lines.append(f"  FOREIGN KEY {fk['column']} → {fk['refTable']}.{fk['refColumn']}")

        content = "\n".join(lines)

        # embed_text: 語意描述 + 欄位名（不含 DDL 雜訊）
        col_names  = " ".join(data.get("columns", {}).keys())
        embed_text = f"{full_name} {desc} {col_names}"

        chunks.append({
            "_id":        f"table_{table}",
            "chunk_type": "table",
            "module":     module,
            "tables":     [table],
            "title":      f"{full_name} — {desc}",
            "content":    content,
            "triggers":   [table, full_name, table.lower()],
            "_embed":     embed_text,
            "metadata": {
                "db_schema":       schema,
                "estimated_rows":  data.get("estimated_rows"),
                "primary_key":     pk,
                "soft_delete_col": data.get("autoDetected", {}).get("softDeleteColumn"),
            }
        })
    return chunks


def build_column_chunks(
    schema_dir: Path,
    enrichment: dict,
    table_to_module: dict
) -> list[dict]:
    """欄位級別 chunks — Schema Linking 用"""
    chunks = []
    for yaml_file in sorted(schema_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        data      = load_yaml(yaml_file)
        table     = data["table"]
        schema_nm = data.get("db_schema", "SEMI")
        full_name = f"{schema_nm}.{table}"
        module    = table_to_module.get(table, "OTHER")
        t_enrich  = enrichment.get(table, {})

        for col_name, col_info in data.get("columns", {}).items():
            col_comment = col_info.get("db_comment", "")
            col_type    = col_info.get("type", "")
            c_enrich    = t_enrich.get(col_name, {})

            synonyms      = c_enrich.get("synonyms", [])
            sample_values = c_enrich.get("sampleValues", [])
            value_mapping = c_enrich.get("valueMapping", {})
            notes         = c_enrich.get("notes", "")

            # content
            parts = [
                f"表: {full_name}",
                f"欄位: {col_name} ({col_type})",
                f"說明: {col_comment}",
            ]
            if synonyms:
                parts.append(f"別名: {', '.join(synonyms)}")
            if sample_values:
                parts.append(f"範例值: {', '.join(str(v) for v in sample_values)}")
            if value_mapping:
                parts.append(f"值對應: {', '.join(f'{k}→{v}' for k, v in value_mapping.items())}")
            if notes:
                parts.append(f"備註: {notes}")
            content = "\n".join(parts)

            # embed_text: 所有使用者可能輸入的詞
            embed_parts = [col_name, col_comment] + synonyms
            if sample_values:
                embed_parts += [str(v) for v in sample_values]
            if value_mapping:
                embed_parts += list(value_mapping.keys())
            if notes:
                embed_parts.append(notes)
            embed_text = " ".join(embed_parts)

            doc: dict = {
                "_id":        f"column_{table}_{col_name}",
                "chunk_type": "column",
                "module":     module,
                "tables":     [table],
                "column_name": col_name,
                "title":      f"{full_name}.{col_name} — {col_comment}",
                "content":    content,
                "triggers":   [col_name, col_name.lower(), f"{table}.{col_name}"] + synonyms,
                "_embed":     embed_text,
                "metadata": {
                    "table":    table,
                    "column":   col_name,
                    "type":     col_type,
                    "synonyms": synonyms,
                    "notes":    notes,
                }
            }
            if sample_values:
                doc["sample_values"] = [str(v) for v in sample_values]
            if value_mapping:
                doc["value_mapping"] = value_mapping

            chunks.append(doc)
    return chunks


def build_rule_chunks(business_dir: Path) -> list[dict]:
    chunks = []
    for yaml_file in sorted(business_dir.glob("module_*.yaml")):
        data   = load_yaml(yaml_file)
        module = data["module"]

        for i, rule in enumerate(data.get("businessRules", [])):
            rule_name   = rule.get("rule", "")
            sql_pattern = rule.get("sqlPattern", "")
            applies_to  = rule.get("appliesTo", [])
            note        = rule.get("note", "")

            content    = (
                f"[{module} 業務規則] {rule_name}\n"
                f"SQL 模式: {sql_pattern}\n"
                f"適用表: {', '.join(applies_to)}\n"
                f"說明: {note}"
            )
            embed_text = f"{rule_name} {note} {sql_pattern} {' '.join(applies_to)}"

            chunks.append({
                "_id":        f"rule_{module}_{i}",
                "chunk_type": "rule",
                "module":     module,
                "tables":     applies_to,
                "title":      f"[{module}] 規則: {rule_name}",
                "content":    content,
                "sql":        sql_pattern,
                "triggers":   applies_to,   # 規則由涉及表名觸發，不混 module keywords
                "_embed":     embed_text,
                "metadata":   {"confidence": rule.get("confidence")}
            })
    return chunks


def build_join_chunks(business_dir: Path) -> list[dict]:
    chunks = []
    for yaml_file in sorted(business_dir.glob("module_*.yaml")):
        data   = load_yaml(yaml_file)
        module = data["module"]

        for i, jp in enumerate(data.get("joinPatterns", [])):
            scenario  = jp.get("scenario", "")
            sql       = jp.get("sql", "").strip()
            sql_tables = extract_sql_tables(sql)   # 只含此 JOIN 實際用到的表

            content    = f"[{module} JOIN 樣板] {scenario}\n\n{sql}"
            embed_text = f"{scenario} {' '.join(sql_tables)} 關聯 JOIN"

            chunks.append({
                "_id":        f"join_{module}_{i}",
                "chunk_type": "join",
                "module":     module,
                "tables":     sql_tables,
                "title":      f"[{module}] JOIN: {scenario}",
                "content":    content,
                "sql":        sql,
                "triggers":   [scenario] + sql_tables,
                "_embed":     embed_text,
                "metadata": {
                    "confidence": jp.get("confidence"),
                    "verified":   jp.get("verified"),
                }
            })
    return chunks


def build_pattern_chunks(
    business_dir: Path,
    schema_dir: Path,
) -> list[dict]:
    """
    查詢樣板 chunks。

    改進點：
    - parameters: 解析 :param_name → 欄位說明（從 schema DDL 推斷）
    - embed_text: 只用 pattern 自身 triggers（不混 module keywords）
    - tables: 只含 SQL 實際用到的表
    """
    # 欄位說明 lookup: COL_NAME → db_comment
    col_comments: dict[str, str] = {}
    for yaml_file in sorted(schema_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        data = load_yaml(yaml_file)
        for col_name, col_info in data.get("columns", {}).items():
            # 不覆蓋（不同表可能有同名欄位，先來先得，通常語意相近）
            if col_name not in col_comments:
                col_comments[col_name] = col_info.get("db_comment", col_name)

    chunks = []
    for yaml_file in sorted(business_dir.glob("module_*.yaml")):
        data   = load_yaml(yaml_file)
        module = data["module"]

        for qp in data.get("queryPatterns", []):
            qp_id    = qp.get("id", "")
            desc     = qp.get("description", "")
            sql      = qp.get("sql", "").strip()
            triggers = qp.get("triggers", [])

            sql_tables = extract_sql_tables(sql)

            # parameters: :param_name → 說明
            param_names = extract_sql_params(sql)
            parameters: dict[str, str] = {}
            for pname in param_names:
                col_key = pname.upper()
                parameters[pname] = col_comments.get(col_key, pname)

            # content
            content_lines = [f"[{module} 查詢樣板] {desc}"]
            if triggers:
                content_lines.append(f"觸發詞: {', '.join(triggers)}")
            if parameters:
                param_str = ", ".join(f":{k} = {v}" for k, v in parameters.items())
                content_lines.append(f"參數: {param_str}")
            content_lines += ["", sql]
            content = "\n".join(content_lines)

            # embed_text: 只用 pattern 自身 triggers，不加 module keywords
            embed_text = f"{desc} {' '.join(triggers)}"

            doc: dict = {
                "_id":        f"pattern_{qp_id}",
                "chunk_type": "pattern",
                "module":     module,
                "tables":     sql_tables,
                "title":      f"[{module}] {desc}",
                "content":    content,
                "sql":        sql,
                "triggers":   triggers,
                "_embed":     embed_text,
                "metadata": {
                    "id":         qp_id,
                    "confidence": qp.get("confidence"),
                }
            }
            if parameters:
                doc["parameters"] = parameters

            chunks.append(doc)
    return chunks


# ── 寫入 ES ───────────────────────────────────────────
def bulk_upsert(index: str, chunks: list[dict]):
    embed_texts = [c["_embed"] for c in chunks]
    print(f"    呼叫 OpenAI embedding ({len(embed_texts)} 筆)...")
    embeddings = get_embeddings(embed_texts)

    lines = []
    for i, chunk in enumerate(chunks):
        doc_id = chunk["_id"]
        doc    = {k: v for k, v in chunk.items() if not k.startswith("_") and v is not None}
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
    print("Text2SQL 資料字典 → Elasticsearch v3")
    print(f"Index: {INDEX_DICT}  Embedding: {EMBEDDING_MODEL}")
    print("=" * 60)

    if not OPENAI_API_KEY:
        print("\n[錯誤] 請設定環境變數 OPENAI_API_KEY")
        print("  PowerShell: $env:OPENAI_API_KEY = 'sk-...'")
        return

    schema_dir   = DICT_DIR / "schema"
    business_dir = DICT_DIR / "business"

    # ── Step 1: 索引 ──────────────────────────────────
    print("\n[Step 1] 確認索引...")
    ensure_index()

    # ── Step 2: 載入輔助資料 ──────────────────────────
    print("\n[Step 2] 載入輔助資料...")
    index_data  = load_yaml(DICT_DIR / "_index.yaml")
    enrichment  = load_yaml(schema_dir / "_column_enrichment.yaml") or {}
    print(f"  模組: {[m['id'] for m in index_data.get('modules', [])]}")
    print(f"  enrichment: {sum(len(v) for v in enrichment.values())} 個欄位有補充資料")

    # 建立 table → module 對應（從 _index.yaml）
    table_to_module: dict[str, str] = {}
    for mod in index_data.get("modules", []):
        for tname in mod.get("tables", []):
            table_to_module[tname] = mod["id"]

    # ── Step 3: 建構 chunks ───────────────────────────
    print("\n[Step 3] 建構 chunks...")
    module_chunks  = build_module_chunks(index_data, business_dir)
    table_chunks   = build_table_chunks(schema_dir, business_dir, enrichment, table_to_module)
    column_chunks  = build_column_chunks(schema_dir, enrichment, table_to_module)
    rule_chunks    = build_rule_chunks(business_dir)
    join_chunks    = build_join_chunks(business_dir)
    pattern_chunks = build_pattern_chunks(business_dir, schema_dir)

    print(f"  module:  {len(module_chunks)} 筆  (v2 新增)")
    print(f"  table:   {len(table_chunks)} 筆")
    print(f"  column:  {len(column_chunks)} 筆  (Schema Linking 用)")
    print(f"  rule:    {len(rule_chunks)} 筆")
    print(f"  join:    {len(join_chunks)} 筆")
    print(f"  pattern: {len(pattern_chunks)} 筆")

    all_chunks = module_chunks + table_chunks + column_chunks + rule_chunks + join_chunks + pattern_chunks
    print(f"  共計:    {len(all_chunks)} 筆")

    # 列出所有 doc ID（方便驗證穩定性）
    print("\n  Doc IDs:")
    for c in all_chunks:
        print(f"    {c['_id']}")

    # ── Step 4: 寫入 ES ───────────────────────────────
    print(f"\n[Step 4] 生成 embedding 並 upsert 到 {INDEX_DICT}...")
    result = bulk_upsert(INDEX_DICT, all_chunks)
    print(f"  完成! took={result['took']}ms, errors={result['errors']}")

    # ── Step 5: 驗證 ──────────────────────────────────
    print("\n[Step 5] 驗證...")
    es_request("POST", f"{INDEX_DICT}/_refresh")
    count = es_request("GET", f"{INDEX_DICT}/_count")
    print(f"  {INDEX_DICT}: 共 {count['count']} 筆")

    agg = es_request("POST", f"{INDEX_DICT}/_search", {
        "size": 0,
        "aggs": {"by_type": {"terms": {"field": "chunk_type", "size": 10}}}
    })
    print("  各類型分佈:")
    for b in agg["aggregations"]["by_type"]["buckets"]:
        print(f"    {b['key']:<10s}: {b['doc_count']}")

    # ── Step 6: 向量搜尋測試 ──────────────────────────
    print("\n[Step 6] 向量搜尋測試...")
    test_queries = [
        ("查詢批次良率",        None),
        ("WAT 量測超規",       None),
        ("A廠的資料",          "column"),
        ("良率",               "column"),
        ("WAT 和 CP 有什麼不同", "module"),
    ]
    for q, filter_type in test_queries:
        print(f"\n  Q: \"{q}\"" + (f"  [filter: {filter_type}]" if filter_type else ""))
        q_emb = get_embeddings([q])[0]
        knn_body: dict = {
            "field": "embedding",
            "query_vector": q_emb,
            "k": 3,
            "num_candidates": 30,
        }
        if filter_type:
            knn_body["filter"] = {"term": {"chunk_type": filter_type}}

        hits = es_request("POST", f"{INDEX_DICT}/_search", {
            "size": 3,
            "knn": knn_body,
            "_source": ["chunk_type", "title", "sample_values", "value_mapping", "parameters"]
        })
        for hit in hits["hits"]["hits"]:
            score = hit["_score"]
            src   = hit["_source"]
            extra = ""
            if src.get("parameters"):
                extra = f"  params={src['parameters']}"
            if src.get("value_mapping"):
                extra = f"  mapping={src['value_mapping']}"
            print(f"    [{src['chunk_type']:<8s}] {score:.4f}  {src['title']}{extra}")

    print("\n" + "=" * 60)
    print("完成！")
    print(f"  {INDEX_DICT} — 含 module/table/column/rule/join/pattern 六種 chunk")
    print("  global_rules 不存 ES，查詢時直接讀檔注入:")
    print(f"    {DICT_DIR / '_global_rules.yaml'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
