"""
Text2SQL Retrieval — 從 ES 取出相關 chunks 組裝成 LLM prompt

流程：
  Phase 1: 快取比對（text2sql_qa）
  Phase 2a: Schema Linking（column chunks 向量搜尋）
  Phase 2a: 查詢樣板 + 業務規則（pattern/rule chunks 向量搜尋）
  Phase 2b: 補齊關聯表結構（table chunks keyword 查詢）
  Phase 2b: 補齊 JOIN 路徑（join chunks keyword 查詢）
  Phase 2c: 全域規則注入（讀 _global_rules.yaml）
  Phase 2d: 組裝 prompt
"""

import yaml
import json
import os
import urllib.request
import urllib.error
import base64
from pathlib import Path
from datetime import datetime

# ── 設定（與 load_dict_to_es_v2.py 一致） ──────────────
ES_URL = "http://localhost:1200"
ES_USER = "elastic"
ES_PASS = "infini_rag_flow"
DICT_DIR = Path(r"H:\copilotCli\dict")

INDEX_SCHEMA = "text2sql_schema"
INDEX_QA = "text2sql_qa"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-3-small"


# ── 共用工具 ──────────────────────────────────────────
def es_request(method: str, path: str, body=None):
    url = f"{ES_URL}/{path}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    cred = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_embedding(text: str) -> list[float]:
    url = "https://api.openai.com/v1/embeddings"
    payload = json.dumps({"model": EMBEDDING_MODEL, "input": [text]}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result["data"][0]["embedding"]


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Phase 1: 快取比對 ────────────────────────────────
def search_qa_cache(query_vec: list[float], top_k=3, min_score=0.88):
    """搜尋 text2sql_qa 歷史問答"""
    result = es_request("POST", f"{INDEX_QA}/_search", {
        "size": top_k,
        "knn": {
            "field": "embedding",
            "query_vector": query_vec,
            "k": top_k,
            "num_candidates": 20
        },
        "_source": ["question", "sql", "rating", "is_golden", "tables"]
    })
    hits = []
    for h in result.get("hits", {}).get("hits", []):
        if h["_score"] >= min_score:
            hits.append({**h["_source"], "_score": h["_score"]})
    return hits


# ── Phase 2a: Schema Linking（column chunks）─────────
def search_columns(query_vec: list[float], top_k=8):
    """向量搜尋 column chunks — 找出使用者用語對應的欄位"""
    result = es_request("POST", f"{INDEX_SCHEMA}/_search", {
        "size": top_k,
        "knn": {
            "field": "embedding",
            "query_vector": query_vec,
            "k": top_k,
            "num_candidates": 40,
            "filter": {"term": {"chunk_type": "column"}}
        },
        "_source": ["title", "tables", "column_name", "content",
                     "value_mapping", "sample_values"]
    })
    return [
        {**h["_source"], "_score": h["_score"]}
        for h in result["hits"]["hits"]
        if h["_score"] >= 0.5  # 過低的不要
    ]


# ── Phase 2a: 查詢樣板 + 業務規則 ───────────────────
def search_patterns_and_rules(query_vec: list[float], top_k=5):
    """向量搜尋 pattern + rule chunks"""
    result = es_request("POST", f"{INDEX_SCHEMA}/_search", {
        "size": top_k,
        "knn": {
            "field": "embedding",
            "query_vector": query_vec,
            "k": top_k,
            "num_candidates": 30,
            "filter": {"terms": {"chunk_type": ["pattern", "rule"]}}
        },
        "_source": ["chunk_type", "title", "content", "sql", "tables"]
    })
    patterns = []
    rules = []
    for h in result["hits"]["hits"]:
        if h["_score"] < 0.5:
            continue
        item = {**h["_source"], "_score": h["_score"]}
        if item["chunk_type"] == "pattern":
            patterns.append(item)
        else:
            rules.append(item)
    return patterns, rules


# ── Phase 2b: 補齊表結構（keyword 查詢）──────────────
def fetch_table_chunks(table_names: list[str]):
    """用 keyword 精確查詢 table chunks"""
    if not table_names:
        return []
    result = es_request("POST", f"{INDEX_SCHEMA}/_search", {
        "size": len(table_names),
        "query": {
            "bool": {
                "must": [
                    {"term": {"chunk_type": "table"}},
                    {"terms": {"tables": table_names}}
                ]
            }
        },
        "_source": ["title", "content", "tables"]
    })
    return [h["_source"] for h in result["hits"]["hits"]]


# ── Phase 2b: 補齊 JOIN 路徑 ─────────────────────────
def fetch_join_chunks(table_names: list[str]):
    """查詢涉及這些表的 JOIN 樣板"""
    if len(table_names) < 2:
        return []
    result = es_request("POST", f"{INDEX_SCHEMA}/_search", {
        "size": 5,
        "query": {
            "bool": {
                "must": [
                    {"term": {"chunk_type": "join"}},
                    {"terms": {"tables": table_names}}
                ]
            }
        },
        "_source": ["title", "content", "sql"]
    })
    return [h["_source"] for h in result["hits"]["hits"]]


# ── Phase 2c: 全域規則 ───────────────────────────────
def load_global_rules() -> str:
    """讀取 _global_rules.yaml 並格式化"""
    data = load_yaml(DICT_DIR / "_global_rules.yaml")
    lines = [f"資料庫方言: {data.get('dialect', 'oracle').upper()}", ""]

    lines.append("禁止語法:")
    for rule in data.get("forbiddenSyntax", []):
        lines.append(f"  ✗ {rule}")

    lines.append("")
    lines.append("額外規則:")
    for rule in data.get("extraRules", []):
        lines.append(f"  • {rule}")

    return "\n".join(lines)


# ── Phase 2d: 組裝 Prompt ────────────────────────────
def build_prompt(
    question: str,
    table_chunks: list[dict],
    column_hits: list[dict],
    join_chunks: list[dict],
    rules: list[dict],
    patterns: list[dict],
    qa_refs: list[dict],
    global_rules: str,
) -> str:
    """組裝完整的 LLM prompt"""
    sections = []

    # ── 系統指令 ──
    sections.append("你是 Oracle SQL 專家。根據以下資訊生成正確的 Oracle SQL。")

    # ── 全域規則 ──
    sections.append(f"\n## 全域 SQL 規則\n{global_rules}")

    # ── 可用表結構 ──
    if table_chunks:
        table_text = "\n\n".join(t["content"] for t in table_chunks)
        sections.append(f"\n## 可用表結構\n{table_text}")

    # ── Schema Linking 提示 ──
    if column_hits:
        linking_lines = []
        for col in column_hits:
            line = f"- {col['title']}"
            if col.get("value_mapping"):
                mappings = ", ".join(
                    f"「{k}」→'{v}'" for k, v in col["value_mapping"].items()
                )
                line += f"  值對應: {mappings}"
            if col.get("sample_values"):
                line += f"  範例值: {', '.join(col['sample_values'][:3])}"
            linking_lines.append(line)
        sections.append("\n## Schema Linking 提示（使用者用語→實際欄位）\n" + "\n".join(linking_lines))

    # ── JOIN 路徑 ──
    if join_chunks:
        join_text = "\n\n".join(j["content"] for j in join_chunks)
        sections.append(f"\n## JOIN 路徑\n{join_text}")

    # ── 業務規則 ──
    if rules:
        rule_text = "\n".join(f"- {r['content']}" for r in rules)
        sections.append(f"\n## 業務規則\n{rule_text}")

    # ── 參考查詢 ──
    if patterns:
        pattern_text = "\n\n".join(p["content"] for p in patterns)
        sections.append(f"\n## 參考查詢樣板\n{pattern_text}")

    # ── 歷史快取參考 ──
    if qa_refs:
        qa_lines = []
        for qa in qa_refs:
            strength = "強參考" if qa.get("is_golden") else "弱參考"
            qa_lines.append(
                f"[{strength} score={qa['_score']:.2f}]\n"
                f"  問: {qa['question']}\n"
                f"  SQL: {qa['sql']}"
            )
        sections.append("\n## 歷史問答參考\n" + "\n\n".join(qa_lines))

    # ── 使用者問題 ──
    sections.append(f"\n## 使用者問題\n{question}")
    sections.append("\n請生成 Oracle SQL。只輸出 SQL，不需要解釋。")

    return "\n".join(sections)


# ── 主流程 ────────────────────────────────────────────
def retrieve_and_build_prompt(question: str) -> str:
    """完整的 Phase 1 + Phase 2 流程"""

    print(f"問題: 「{question}」\n")

    # 1. 生成問題的 embedding（只呼叫一次，所有搜尋共用）
    print("[Embedding] 呼叫 OpenAI...")
    query_vec = get_embedding(question)

    # 2. Phase 1: 快取比對
    print("[Phase 1] 搜尋歷史問答快取...")
    qa_refs = search_qa_cache(query_vec)
    if qa_refs:
        for qa in qa_refs:
            golden = " ★golden" if qa.get("is_golden") else ""
            print(f"  命中: score={qa['_score']:.4f}{golden}  {qa['question']}")
    else:
        print("  未命中")

    # 3. Phase 2a: Schema Linking
    print("[Phase 2a] Schema Linking（column 向量搜尋）...")
    column_hits = search_columns(query_vec)
    for col in column_hits:
        extra = ""
        if col.get("value_mapping"):
            extra = f"  mapping={col['value_mapping']}"
        print(f"  score={col['_score']:.4f}  {col['title']}{extra}")

    # 4. Phase 2a: 查詢樣板 + 業務規則
    print("[Phase 2a] 搜尋 pattern + rule...")
    patterns, rules = search_patterns_and_rules(query_vec)
    for p in patterns:
        print(f"  [pattern] score={p['_score']:.4f}  {p['title']}")
    for r in rules:
        print(f"  [rule]    score={r['_score']:.4f}  {r['title']}")

    # 5. Phase 2b: 收集所有涉及的表名 → 補齊 table chunks
    involved_tables = set()
    for col in column_hits:
        involved_tables.update(col.get("tables", []))
    for p in patterns:
        involved_tables.update(p.get("tables", []))
    for r in rules:
        involved_tables.update(r.get("tables", []))

    print(f"[Phase 2b] 涉及的表: {sorted(involved_tables)}")

    print("[Phase 2b] 補齊表結構...")
    table_chunks = fetch_table_chunks(list(involved_tables))
    for t in table_chunks:
        print(f"  載入: {t['title']}")

    print("[Phase 2b] 補齊 JOIN 路徑...")
    join_chunks = fetch_join_chunks(list(involved_tables))
    for j in join_chunks:
        print(f"  載入: {j['title']}")

    # 6. Phase 2c: 全域規則
    print("[Phase 2c] 載入全域規則...")
    global_rules = load_global_rules()

    # 7. Phase 2d: 組裝 prompt
    print("[Phase 2d] 組裝 prompt...\n")
    prompt = build_prompt(
        question=question,
        table_chunks=table_chunks,
        column_hits=column_hits,
        join_chunks=join_chunks,
        rules=rules,
        patterns=patterns,
        qa_refs=qa_refs,
        global_rules=global_rules,
    )
    return prompt


# ── CLI ───────────────────────────────────────────────
def main():
    import sys

    if not OPENAI_API_KEY:
        print("[錯誤] 請設定環境變數 OPENAI_API_KEY", file=sys.stderr)
        sys.exit(1)

    # 模式 1: 帶參數 → 靜默模式，只輸出 prompt（供 Copilot agent 使用）
    # 用法: python retrieve_context.py "查一下 A廠上個月的批次良率"
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        # 靜默模式：把過程日誌導向 stderr，只有 prompt 走 stdout
        import io
        old_stdout = sys.stdout
        sys.stdout = sys.stderr  # 所有 print 都導向 stderr
        prompt = retrieve_and_build_prompt(question)
        sys.stdout = old_stdout  # 還原

        # 自動存檔 prompt
        log_dir = Path(r"H:\copilotCli\logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prompt_file = log_dir / f"{ts}_prompt.md"
        prompt_file.write_text(
            f"# Question\n{question}\n\n# Prompt\n{prompt}",
            encoding="utf-8",
        )
        print(f"[saved] {prompt_file}", file=sys.stderr)

        print(prompt)  # 只有 prompt 走 stdout
        return

    # 模式 2: 無參數 → 測試模式，跑 3 個範例
    test_questions = [
        "查一下 A廠上個月的批次良率",
        "WAT 量測值超規的批次有哪些",
        "CP_LOT 和 CP_WAFER 怎麼 JOIN 查良率低於 0.9 的",
    ]

    for i, q in enumerate(test_questions):
        print("=" * 70)
        print(f"測試 {i+1}/{len(test_questions)}")
        print("=" * 70)

        prompt = retrieve_and_build_prompt(q)

        print("─" * 70)
        print("最終 Prompt:")
        print("─" * 70)
        print(prompt)
        print("\n")


if __name__ == "__main__":
    main()
