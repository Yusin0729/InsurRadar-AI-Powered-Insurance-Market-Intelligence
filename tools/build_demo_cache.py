"""預先產生 Demo 問答快取（D 組）。

用法（專案根目錄）：
    INSURRADAR_DATASET_PATH=data/real/dataset.json python -m tools.build_demo_cache
    python -m tools.build_demo_cache --attempts 3 --wait 40 "美利大運的宣告利率是多少？"

流程：載入 INSURRADAR_DATASET_PATH 指定的快照 → 對每題呼叫真實 answer_rag_query
（C 組檢索器＋Gemini）→ 只把 success／insufficient_data 且通過引用驗證的結果寫入
data/real/demo_cache/demo_answers.json。其他狀態（error、validation_failed）不寫入，
並在輸出中照實列出「無結果」。已快取的題目預設略過，加 --refresh 重新產生。
不會輸出 API 金鑰。外部套件：無（沿用專案服務）。
"""

from __future__ import annotations

import argparse
import sys
import time

from services.c_integration import get_dataset_path, get_retriever
from services.data_service import load_dataset
from services.demo_cache import (
    CACHE_PATH, DEMO_QUESTIONS, is_cacheable, lookup, make_entry, normalize_question, save_entries,
)
from services.llm_service import get_gemini_config
from services.rag_service import DEFAULT_MODEL, answer_rag_query


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="預先產生 Demo 問答快取")
    parser.add_argument("questions", nargs="*", help="要快取的題目；省略時使用預設 Demo 題目")
    parser.add_argument("--attempts", type=int, default=3, help="每題最多嘗試次數（預設 3）")
    parser.add_argument("--wait", type=float, default=30.0, help="失敗後等待秒數（預設 30）")
    parser.add_argument("--gap", type=float, default=8.0, help="題目之間間隔秒數（預設 8）")
    parser.add_argument("--refresh", action="store_true", help="已有快取的題目也重新產生")
    args = parser.parse_args(argv)

    path = get_dataset_path()
    if not path:
        print("未設定 INSURRADAR_DATASET_PATH；Demo 快取只針對真實快照產生，已停止。")
        return 2
    dataset = load_dataset(path)
    config = get_gemini_config()
    model = config["model"] or DEFAULT_MODEL
    if not config["api_key"]:
        print("未設定 GEMINI_API_KEY，無法產生快取。")
        return 2
    print(f"快照：{dataset['dataset_id']}｜模型：{model}｜快取檔：{CACHE_PATH}")

    retriever = get_retriever(dataset)
    questions = [normalize_question(q) for q in (args.questions or DEMO_QUESTIONS)]
    summary = []
    for index, question in enumerate(questions):
        if not args.refresh and lookup(question, dataset):
            summary.append((question, "已有快取，略過"))
            continue
        if index:
            time.sleep(args.gap)
        outcome = "無結果"
        for attempt in range(1, args.attempts + 1):
            result = answer_rag_query(question, dataset, retriever=retriever)
            status = result.get("status")
            print(f"[{attempt}/{args.attempts}] {question} → {status}"
                  + (f"｜{result.get('error_message')}" if result.get("error_message") else ""))
            if is_cacheable(result):
                save_entries([make_entry(result, dataset_id=dataset["dataset_id"], model=model)])
                outcome = f"已快取（{status}，引用 {len(result['citations'])} 筆）"
                print("  回答：" + result["answer"])
                print("  引用：" + ("、".join(result["citations"]) or "無"))
                break
            outcome = f"無結果（最後狀態 {status}）"
            if attempt < args.attempts:
                time.sleep(args.wait)
        summary.append((question, outcome))

    print("\n=== 結果 ===")
    for question, outcome in summary:
        print(f"- {question}：{outcome}")
    return 0 if all(not outcome.startswith("無結果") for _, outcome in summary) else 1


if __name__ == "__main__":
    sys.exit(main())
