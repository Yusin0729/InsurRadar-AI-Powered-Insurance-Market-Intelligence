"""Demo 預先產生回答快取：現場 Gemini 忙碌時，改顯示同一快照、同一題目的預存回答。

規則（安全優先）：
1. 只收錄 answer_rag_query 真實執行後 status 為 success／insufficient_data 且
   verification_passed 為 True 的結果；不手寫、不改寫回答。
2. 查詢須「題目完全相同（只正規化前後與連續空白）」且「dataset_id 相同」；
   不跨快照使用，也不做模糊比對。
3. 取出時重新檢查：引用必須存在於目前快照且屬於保存的檢索片段，檢索片段須仍是
   目前快照原文的連續子片段（沿用 rag_service.validate_retrieved_items）；
   任一不符即視為沒有快取，不顯示。
4. 使用時機：即時呼叫因暫時性錯誤（503／429 等）失敗時自動改用；或設定
   INSURRADAR_DEMO_CACHE=1 時優先使用（命中才用，未命中照常即時呼叫）。
5. 回傳結果帶 from_cache、cache_created_at、cache_model、cache_reason，UI 必須明確標示。

快取檔：data/real/demo_cache/demo_answers.json（data/real 已被 .gitignore 排除）。
產生方式：python -m tools.build_demo_cache
外部套件：無。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable

from services.gemini_retry import BUSY_MESSAGE, is_transient
from services.rag_service import answer_rag_query, validate_retrieved_items

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = PROJECT_ROOT / "data" / "real" / "demo_cache" / "demo_answers.json"
DEMO_CACHE_ENV = "INSURRADAR_DEMO_CACHE"
CACHEABLE_STATUSES = ("success", "insufficient_data")
CACHE_VERSION = 1

DEMO_QUESTIONS = (
    "美利大運的宣告利率是多少？",
    "哪些保險商品提供 6 年期繳費方案？",
    "各家美元保單投保年齡限制為何？",
    "超美利第 10 年末的保證解約金是多少？",
    "南山美滿臻愛的保單貸款利率是多少？",
    # C 組 11/5 實測過的題目（C_to_D.md 第二節）；避開「富邦美利大運 6 年期的預定利率」。
    "南山美滿臻愛最新一期的宣告利率是多少？",
    "超美利的宣告利率是多少？",
    "哪一家最好？",
)


def normalize_question(question: Any) -> str:
    """只去除前後空白並合併連續空白；其餘字元（含全形標點）必須完全相同。"""
    return " ".join(question.split()) if isinstance(question, str) else ""


def cache_forced() -> bool:
    return (os.getenv(DEMO_CACHE_ENV) or "").strip() == "1"


def is_cacheable(result: dict[str, Any]) -> bool:
    """只收錄已通過引用驗證的成功或資料不足結果。"""
    if not isinstance(result, dict) or result.get("status") not in CACHEABLE_STATUSES:
        return False
    if result.get("verification_passed") is not True:
        return False
    citations = result.get("citations")
    retrieved = result.get("retrieved_evidence")
    if not isinstance(citations, list) or not isinstance(retrieved, list):
        return False
    if not isinstance(result.get("answer"), str) or not result["answer"].strip():
        return False
    if result["status"] == "success":
        retrieved_ids = {item.get("evidence_id") for item in retrieved if isinstance(item, dict)}
        return bool(citations) and all(cid in retrieved_ids for cid in citations)
    return not citations


def make_entry(result: dict[str, Any], *, dataset_id: str, model: str) -> dict[str, Any]:
    if not is_cacheable(result):
        raise ValueError("只能快取通過驗證的 success／insufficient_data 結果。")
    return {
        "dataset_id": dataset_id,
        "question": normalize_question(result.get("query")),
        "status": result["status"],
        "answer": result["answer"],
        "citations": list(result["citations"]),
        "retrieved_evidence": list(result["retrieved_evidence"]),
        "verification_passed": True,
        "is_mock": bool(result.get("is_mock")),
        "model": model,
        "created_at": result.get("created_at") or datetime.now(timezone.utc).isoformat(),
    }


def load_cache(path: Path | str | None = None) -> list[dict[str, Any]]:
    """讀取快取；檔案不存在或格式錯誤時回傳空清單（等同沒有快取）。"""
    target = Path(path) if path else CACHE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []


def save_entries(new_entries: list[dict[str, Any]], path: Path | str | None = None) -> list[dict[str, Any]]:
    """依 (dataset_id, 題目) 合併後寫回；新結果取代舊結果。"""
    target = Path(path) if path else CACHE_PATH
    merged = {(item.get("dataset_id"), item.get("question")): item for item in load_cache(target)}
    for item in new_entries:
        merged[(item["dataset_id"], item["question"])] = item
    entries = sorted(merged.values(), key=lambda item: (str(item.get("dataset_id")), str(item.get("question"))))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": CACHE_VERSION, "entries": entries}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return entries


def lookup(question: str, dataset: dict[str, Any], path: Path | str | None = None) -> dict[str, Any] | None:
    """完全相同題目且同一 dataset_id 才命中；取出時重新驗證引用與原文。"""
    key = normalize_question(question)
    dataset_id = dataset.get("dataset_id")
    if not key or not dataset_id:
        return None
    for entry in load_cache(path):
        if entry.get("dataset_id") != dataset_id or entry.get("question") != key:
            continue
        retrieved = entry.get("retrieved_evidence")
        if not is_cacheable({**entry, "query": key}) or not isinstance(retrieved, list):
            return None
        checked = validate_retrieved_items(retrieved, dataset, max(len(retrieved), 1))
        if len(checked) != len(retrieved):
            return None  # 快照原文已變動或片段不可追溯，不使用
        evidence_ids = {item["evidence_id"] for item in dataset.get("evidence", [])}
        if any(cid not in evidence_ids for cid in entry["citations"]):
            return None
        return {**entry, "retrieved_evidence": checked}
    return None


def _is_transient_failure(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict) or result.get("status") != "error":
        return False
    if result.get("answer") == BUSY_MESSAGE:
        return True
    message = result.get("error_message")
    return bool(message) and is_transient(RuntimeError(message))


def _as_result(entry: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "status": entry["status"],
        "query": entry["question"],
        "answer": entry["answer"],
        "citations": list(entry["citations"]),
        "retrieved_evidence": entry["retrieved_evidence"],
        "verification_passed": True,
        "error_message": None,
        "is_mock": entry.get("is_mock", False),
        "created_at": entry.get("created_at"),
        "from_cache": True,
        "cache_created_at": entry.get("created_at"),
        "cache_model": entry.get("model"),
        "cache_reason": reason,
    }


def answer_with_demo_cache(
    query: str,
    dataset: dict[str, Any],
    *,
    retriever: Any = None,
    answer_fn: Callable[..., dict[str, Any]] = answer_rag_query,
    path: Path | str | None = None,
    forced: bool | None = None,
) -> dict[str, Any]:
    """先依旗標或即時結果決定是否使用快取；未命中時一律回傳即時結果原樣。"""
    use_first = cache_forced() if forced is None else forced
    if use_first:
        entry = lookup(query, dataset, path)
        if entry:
            return _as_result(entry, "forced")
    result = answer_fn(query, dataset, retriever=retriever)
    if _is_transient_failure(result):
        entry = lookup(query, dataset, path)
        if entry:
            return _as_result(entry, "fallback")
    return result
