"""檢索評估：用固定的測試問題檢查 search(query, k) 是否找得到正確的證據段落。

用法（在專案根目錄）：
    python -m tools.eval_retrieval                 # 預設：向量＋關鍵字（有 GEMINI_API_KEY 時）
    python -m tools.eval_retrieval --offline       # 強制關鍵字模式（INSURRADAR_OFFLINE=1）
    python -m tools.eval_retrieval -k 5 --threshold 0.8

輸入：
- 快照：預設 data/real/dataset.json（用 D 組 load_dataset() 載入並驗證，與前端相同）
- 測試問題：預設 tests/data/retrieval_questions.json（只有問題與預期 evidence_id，不含原文）
檢索器：services.c_integration.get_retriever(dataset)，與前端問答使用同一個。
輸出：data/real/processed/eval/retrieval_eval_<模式>_<時間>.json 與 .csv

指標：
- 命中（hit@k）：預期 evidence_id 任一出現在前 k 筆。
- 名次：第一個正確證據的名次（1 起算），沒找到為空。
- MRR：各題 1/名次 的平均，沒找到記 0。
- 模式：每題實際使用的檢索模式（hybrid／keyword），取自 retriever.last_mode；
  沒有 API Key 或嵌入失敗時會自動退回 keyword，報告會如實標示。
命中率低於 --threshold 時結束碼為 1；快照或問題檔載入失敗為 2。

本工具只讀不寫 dataset.json。外部套件：無（沿用專案既有模組）。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_ROOT / "data" / "real" / "dataset.json"
QUESTIONS_PATH = PROJECT_ROOT / "tests" / "data" / "retrieval_questions.json"
OUTPUT_DIR = PROJECT_ROOT / "data" / "real" / "processed" / "eval"
TAIPEI = timezone(timedelta(hours=8))
DEFAULT_K = 3
DEFAULT_THRESHOLD = 0.7

CSV_COLUMNS = ["qid", "問題", "主題", "預期證據", "命中", "名次", "模式", "檢索結果", "錯誤"]


# ---------------------------------------------------------------------------
# 測試問題
# ---------------------------------------------------------------------------
def load_questions(path: Path) -> list[dict[str, Any]]:
    """讀取並檢查問題檔：qid 不可重複、問題不可空白、至少一個預期證據。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(questions, list) or not questions:
        raise ValueError("問題檔必須包含非空的 questions 清單。")
    seen: set[str] = set()
    for i, q in enumerate(questions, 1):
        qid = q.get("qid") or f"q{i:02d}"
        q["qid"] = qid
        if qid in seen:
            raise ValueError(f"qid 重複：{qid}")
        seen.add(qid)
        if not isinstance(q.get("question"), str) or not q["question"].strip():
            raise ValueError(f"{qid} 的 question 不可空白。")
        expected = q.get("expected_evidence_ids")
        if not isinstance(expected, list) or not expected or not all(isinstance(x, str) and x for x in expected):
            raise ValueError(f"{qid} 的 expected_evidence_ids 必須是非空的字串清單。")
    return questions


def missing_expected(questions: list[dict[str, Any]], dataset: dict[str, Any]) -> dict[str, list[str]]:
    """列出快照中不存在的預期證據（快照改版、ID 變動時提醒更新問題檔）。"""
    ids = {e.get("evidence_id") for e in dataset.get("evidence", [])}
    result = {}
    for q in questions:
        absent = [eid for eid in q["expected_evidence_ids"] if eid not in ids]
        if absent:
            result[q["qid"]] = absent
    return result


# ---------------------------------------------------------------------------
# 評估
# ---------------------------------------------------------------------------
def evaluate_question(retriever: Any, question: dict[str, Any], k: int) -> dict[str, Any]:
    """跑一題：記錄前 k 筆結果、第一個正確證據的名次與實際檢索模式。檢索例外視為未命中。"""
    expected = set(question["expected_evidence_ids"])
    row: dict[str, Any] = {
        "qid": question["qid"],
        "question": question["question"],
        "topic": question.get("topic"),
        "product_id": question.get("product_id"),
        "expected_evidence_ids": list(question["expected_evidence_ids"]),
        "hits": [],
        "rank": None,
        "hit": False,
        "mode": None,
        "error": None,
    }
    try:
        results = retriever.search(question["question"], k)
    except Exception as err:  # 單題失敗不中斷整批評估
        row["error"] = f"{type(err).__name__}: {err}"
        results = []
    row["mode"] = getattr(retriever, "last_mode", None)
    if row["error"] is None and getattr(retriever, "last_error", None):
        row["error"] = retriever.last_error  # 例如嵌入失敗後退回關鍵字

    for rank, item in enumerate(results[:k], 1):
        eid = item.get("evidence_id")
        row["hits"].append({
            "rank": rank,
            "evidence_id": eid,
            "score": item.get("score"),
            "product_ids": list(item.get("product_ids") or []),
            "excerpt": (item.get("excerpt") or "")[:80],
        })
        if row["rank"] is None and eid in expected:
            row["rank"] = rank
    row["hit"] = row["rank"] is not None
    return row


def summarize(rows: list[dict[str, Any]], k: int) -> dict[str, Any]:
    total = len(rows)
    hits = sum(r["hit"] for r in rows)
    modes: dict[str, int] = {}
    for r in rows:
        key = r["mode"] or "unknown"
        modes[key] = modes.get(key, 0) + 1
    return {
        "k": k,
        "total": total,
        "hits": hits,
        "hit_rate": round(hits / total, 4) if total else 0.0,
        "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / total, 4) if total else 0.0,
        "modes": modes,
        "missed": [r["qid"] for r in rows if not r["hit"]],
    }


def evaluate(retriever: Any, questions: list[dict[str, Any]], k: int = DEFAULT_K) -> dict[str, Any]:
    if type(k) is not int or k < 1:
        raise ValueError("k 必須為正整數。")
    rows = [evaluate_question(retriever, q, k) for q in questions]
    return {"summary": summarize(rows, k), "results": rows}


def mode_label(modes: dict[str, int]) -> str:
    """報告檔名用：全部同一模式時用該模式，否則 mixed。"""
    return next(iter(modes)) if len(modes) == 1 else "mixed"


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------
def write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TAIPEI).strftime("%Y%m%d_%H%M%S")
    base = output_dir / f"retrieval_eval_{mode_label(report['summary']['modes'])}_{stamp}"
    json_path, csv_path = base.with_suffix(".json"), base.with_suffix(".csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:  # utf-8-sig：Excel 直接開不亂碼
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in report["results"]:
            writer.writerow({
                "qid": r["qid"],
                "問題": r["question"],
                "主題": r.get("topic") or "",
                "預期證據": ";".join(r["expected_evidence_ids"]),
                "命中": "是" if r["hit"] else "否",
                "名次": r["rank"] or "",
                "模式": r["mode"] or "",
                "檢索結果": ";".join(f"{h['evidence_id']}({h['score']})" for h in r["hits"]),
                "錯誤": r["error"] or "",
            })
    return json_path, csv_path


def print_report(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(f"快照：{report.get('dataset_id')}　k={s['k']}　模式：{s['modes']}")
    last_error = None
    for r in report["results"]:
        mark = "命中" if r["hit"] else "未中"
        got = ", ".join(h["evidence_id"] for h in r["hits"]) or "（無結果）"
        print(f"[{mark}] {r['qid']} 名次={r['rank'] or '-'} 模式={r['mode']} 預期={r['expected_evidence_ids']} 得到=[{got}]")
        print(f"       {r['question']}")
        if r["error"]:
            # 索引失敗時每題都是同一則錯誤，只印第一次且截短；完整內容在 JSON 報告
            note = "同上" if r["error"] == last_error else r["error"][:160]
            print(f"       錯誤／備註：{note}")
        last_error = r["error"]
    print(f"hit@{s['k']} = {s['hits']}/{s['total']} = {s['hit_rate']:.2f}　MRR = {s['mrr']:.3f}")
    if s["missed"]:
        print("未命中：" + ", ".join(s["missed"]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="檢索評估：測試問題是否找得到正確證據段落。")
    parser.add_argument("dataset", nargs="?", default=str(DATASET_PATH), help="快照路徑（預設 data/real/dataset.json）")
    parser.add_argument("--questions", default=str(QUESTIONS_PATH), help="測試問題檔")
    parser.add_argument("-k", "--top-k", type=int, default=DEFAULT_K, help="取前幾筆（預設 3）")
    parser.add_argument("--offline", action="store_true", help="強制關鍵字模式（設定 INSURRADAR_OFFLINE=1）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"命中率低於此值時結束碼為 1（預設 {DEFAULT_THRESHOLD}）")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="報告輸出目錄")
    parser.add_argument("--no-write", action="store_true", help="只印出結果，不寫報告檔")
    args = parser.parse_args(argv)
    if args.top_k < 1:
        parser.error("-k 必須為正整數。")

    if args.offline:
        os.environ["INSURRADAR_OFFLINE"] = "1"  # 必須在建立檢索器之前設定

    from services.c_integration import get_retriever, reset_retrievers
    from services.data_service import load_dataset

    try:
        dataset = load_dataset(args.dataset)
        questions = load_questions(Path(args.questions))
    except Exception as err:
        print(f"載入失敗：{err}", file=sys.stderr)
        return 2

    absent = missing_expected(questions, dataset)
    for q in questions:
        if len(absent.get(q["qid"], [])) == len(q["expected_evidence_ids"]):
            # 只有全部預期證據都不存在才警告（部分缺少多半是舊快照沒有 PDF 段落，不影響評估）
            print(f"警告：{q['qid']} 的預期證據都不在快照中：{absent[q['qid']]}（快照可能已改版，請更新問題檔）")

    reset_retrievers()
    retriever = get_retriever(dataset)
    report = evaluate(retriever, questions, args.top_k)
    report.update({
        "dataset_id": dataset.get("dataset_id"),
        "dataset_path": str(args.dataset),
        "questions_path": str(args.questions),
        "offline_flag": os.getenv("INSURRADAR_OFFLINE") == "1",
        "missing_expected": absent,
        "evaluated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
    })
    print_report(report)

    modes = report["summary"]["modes"]
    report["vector_unavailable"] = not args.offline and "hybrid" not in modes
    if report["vector_unavailable"]:
        print("注意：本次沒有任何一題使用向量檢索（全部退回關鍵字），結果不代表向量模式。")

    if not args.no_write:
        json_path, csv_path = write_report(report, Path(args.output_dir))
        print(f"報告：{json_path}\n      {csv_path}")

    return 0 if report["summary"]["hit_rate"] >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
