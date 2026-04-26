"""
Text2SQL 向量搜尋 Demo — 展示 ES 實際回傳哪些 chunks 給 LLM

執行: python search_demo.py
"""

import json
import os
import urllib.request
import urllib.error
import base64

ES_URL  = "http://localhost:1200"
ES_USER = "elastic"
ES_PASS = "infini_rag_flow"
INDEX   = "text2sql_v2"

OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-3-small"


# ── 工具 ──────────────────────────────────────────────
def es_request(method, path, body=None):
    url  = f"{ES_URL}/{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    cred = base64.b64encode(f"{ES_USER}:{ES_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def embed(text: str) -> list[float]:
    payload = json.dumps({"model": EMBEDDING_MODEL, "input": [text]}).encode()
    req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {OPENAI_API_KEY}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["data"][0]["embedding"]


def search(query: str, top_k=5, filter_type=None) -> list[dict]:
    """向量 + 關鍵字混合搜尋"""
    q_vec = embed(query)

    knn = {
        "field": "embedding",
        "query_vector": q_vec,
        "k": top_k,
        "num_candidates": 50,
    }
    if filter_type:
        knn["filter"] = {"term": {"chunk_type": filter_type}}

    body = {
        "size": top_k,
        "knn": knn,
        "_source": ["chunk_type", "module", "tables", "title",
                    "content", "triggers", "parameters",
                    "sample_values", "value_mapping"]
    }
    result = es_request("POST", f"{INDEX}/_search", body)
    return result["hits"]["hits"]


# ── 顯示 ──────────────────────────────────────────────
SEPARATOR = "─" * 64

def show_hit(hit: dict, rank: int):
    src   = hit["_source"]
    score = hit["_score"]
    ctype = src.get("chunk_type", "?")
    title = src.get("title", "")
    content = src.get("content", "")

    print(f"\n  #{rank}  [{ctype}]  score={score:.4f}")
    print(f"      {title}")

    # 依 chunk_type 顯示最有用的欄位
    if ctype == "column":
        if src.get("sample_values"):
            print(f"      sample_values : {src['sample_values']}")
        if src.get("value_mapping"):
            print(f"      value_mapping : {src['value_mapping']}")

    if ctype == "pattern":
        if src.get("parameters"):
            print(f"      parameters    : {src['parameters']}")
        # 顯示 SQL（縮排）
        sql_lines = [l for l in content.split("\n") if l.strip().startswith(
            ("SELECT","FROM","JOIN","WHERE","AND","ORDER","FETCH","GROUP","HAVING")
        )]
        if sql_lines:
            print("      SQL preview:")
            for l in sql_lines[:6]:
                print(f"        {l.strip()}")

    if ctype == "rule":
        for line in content.split("\n"):
            if "SQL 模式" in line or "說明" in line:
                print(f"      {line.strip()}")

    if ctype == "join":
        sql_lines = [l for l in content.split("\n") if l.strip().startswith(
            ("SELECT","FROM","JOIN","WHERE")
        )]
        if sql_lines:
            print("      SQL preview:")
            for l in sql_lines[:4]:
                print(f"        {l.strip()}")

    if ctype == "module":
        for line in content.split("\n"):
            if "包含表" in line or line.strip().startswith("  "):
                print(f"      {line}")


def run_scenario(label: str, query: str, top_k=5, filter_type=None):
    print(f"\n{'=' * 64}")
    print(f"使用者問題: 「{query}」")
    if filter_type:
        print(f"搜尋範圍   : chunk_type = {filter_type}")
    print(f"{'=' * 64}")

    hits = search(query, top_k=top_k, filter_type=filter_type)
    if not hits:
        print("  (無結果)")
        return

    print(f"ES 回傳 {len(hits)} 筆 chunks：")
    for i, hit in enumerate(hits, 1):
        show_hit(hit, i)

    # 顯示 LLM 最終會拿到什麼
    print(f"\n  ▶ LLM 注入的 context 包含：")
    for hit in hits:
        src = hit["_source"]
        ctype = src["chunk_type"]
        if ctype == "column" and src.get("value_mapping"):
            first_col = src["title"].split("—")[0].strip()
            vm = src["value_mapping"]
            print(f"    • Schema Linking: 使用者說的詞 → 實際 SQL 值")
            for k, v in vm.items():
                print(f"        {k!r:12s} → '{v}'")
        elif ctype == "pattern" and src.get("parameters"):
            print(f"    • SQL 樣板: {src['title']}")
            for k, v in src["parameters"].items():
                print(f"        :{k} = {v}（從使用者問題中萃取）")
        elif ctype == "rule":
            sql_pat = [l for l in src["content"].split("\n") if "SQL 模式" in l]
            if sql_pat:
                print(f"    • 業務規則: {sql_pat[0].strip()}")


# ── 主程式 ────────────────────────────────────────────
def main():
    if not OPENAI_API_KEY:
        print("[錯誤] 請設定 OPENAI_API_KEY")
        return

    # ── 情境 1：口語廠別 + 良率 ──────────────────────
    run_scenario(
        label   = "口語廠別 + 良率",
        query   = "查 A廠 上週的晶圓良率",
        top_k   = 5,
    )

    # ── 情境 2：WAT 超規 ─────────────────────────────
    run_scenario(
        label   = "WAT 超規查詢",
        query   = "哪些批次的 VTH_N 量測值超規",
        top_k   = 5,
    )

    # ── 情境 3：只搜 column，驗證 Schema Linking ─────
    run_scenario(
        label   = "Schema Linking 驗證",
        query   = "合格判定",
        top_k   = 3,
        filter_type = "column",
    )

    # ── 情境 4：問跨表關聯 ───────────────────────────
    run_scenario(
        label   = "跨表 JOIN",
        query   = "批次和晶圓要怎麼關聯",
        top_k   = 4,
    )

    # ── 情境 5：模組不存在的詞（測試有無幻覺） ───────
    run_scenario(
        label   = "模組識別",
        query   = "Chip Probing 的 Bin 統計",
        top_k   = 3,
    )


if __name__ == "__main__":
    main()
