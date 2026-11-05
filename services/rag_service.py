"""InsurRadar RAG 智慧問答服務模組。

主要用途與功能：
1. 接收使用者問題，透過 Retriever 檢索候選依據片段。
2. 組裝檢索上下文與限制條件，透過獨立 Prompt 呼叫 Gemini API。
3. 採用 Google GenAI 結構化輸出（Pydantic Schema）約束回傳格式。
4. 嚴格執行引用驗證：
   - 檢核幽靈引用（是否存在於資料集）。
   - 檢核越界引用（是否屬於本次檢索結果）。
   - 檢核必要引用缺失（肯定結論必須具備有效引用，違者整筆標記為驗證失敗）。
5. 確保回答不擅自偽造人工複核標記，妥善隔離模擬資料。

外部套件：
- google.genai: Google 官方 Gemini API SDK。
- pydantic: 用於定義 Gemini 結構化輸出 Schema。
- dotenv: 讀取本機環境設定。
"""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from services.gemini_retry import friendly_message, with_retry
from services.llm_service import DEFAULT_MODEL, clean_json_text, get_gemini_config
from services.retriever import BaseRetriever, MockRetriever

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAG_PROMPT_PATH = PROJECT_ROOT / "prompts" / "rag_qa.txt"
INSUFFICIENT_ANSWER = "目前未檢索到足夠依據，無法確認此問題的答案。請補充商品名稱或查詢條件。"


def validate_retrieved_items(items, dataset, top_k):
    """只接受目前快照可追溯的原文或連續子片段；相關度不是語意驗證。"""
    if not isinstance(items, list):
        raise ValueError("Retriever 必須回傳清單。")
    lookup = {e["evidence_id"]: e for e in dataset.get("evidence", [])}
    accepted, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        eid = item.get("evidence_id")
        if not isinstance(eid, str) or eid not in lookup or eid in seen:
            continue
        source = lookup[eid]
        excerpt = item.get("excerpt")
        original = source.get("excerpt")
        score = item.get("score")
        if (not isinstance(excerpt, str) or not excerpt.strip()
                or not isinstance(original, str)
                or " ".join(excerpt.split()) not in " ".join(original.split())
                or type(score) not in (int, float) or not math.isfinite(score)
                or not 0 <= score <= 1):
            continue
        if source.get("source_type") not in ("mock", "official_page", "pdf", "news"):
            continue
        if source.get("is_mock") != (dataset.get("data_mode") == "mock"):
            continue
        if not source.get("source_title") or not (source.get("source_url") or source.get("local_path")):
            continue
        # 定位資訊必須與彈窗來源一致；可選欄位省略時由快照補齊。
        fields = ("source_title", "source_type", "source_url", "document_version",
                  "page", "section", "product_ids", "local_path", "source_date", "captured_at")
        if any(key in item and item[key] != source.get(key) for key in fields):
            continue
        if "dataset_id" in item and item["dataset_id"] != dataset["dataset_id"]:
            continue
        canonical = {key: source.get(key) for key in fields}
        canonical.update(evidence_id=eid, excerpt=excerpt.strip(), score=score)
        accepted.append(canonical)
        seen.add(eid)
        if len(accepted) == top_k:
            break
    return accepted


class RAGOutputSchema(BaseModel):
    """Gemini API 結構化輸出 Schema。"""
    answer: str = Field(description="針對使用者問題的繁體中文客觀解答")
    citations: list[str] = Field(description="支持回答的依據 ID 清單，必須只能引用本次檢索到的依據 ID")
    has_sufficient_evidence: bool = Field(description="檢索到的依據是否足以支持此回答")


def load_rag_prompt() -> str:
    """載入獨立的 RAG 問答 Prompt 範本。"""
    if not RAG_PROMPT_PATH.exists():
        raise FileNotFoundError(f"找不到 RAG Prompt 範本檔案：{RAG_PROMPT_PATH}")
    with RAG_PROMPT_PATH.open("r", encoding="utf-8") as f:
        return f.read()


def format_retrieved_context(retrieved_items: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """將檢索出的依據片段格式化為 LLM 易於閱讀之結構文字，並回傳可用之 ID 清單。"""
    context_lines = []
    allowed_ids = []

    for item in retrieved_items:
        eid = item["evidence_id"]
        allowed_ids.append(eid)
        title = item.get("source_title", "未命名依據")
        version = f"版本:{item.get('document_version')}" if item.get("document_version") else ""
        # 頁碼與章節都要給模型：章節常載明「保證」或「假設宣告利率 X%」等性質，
        # 只給頁碼時模型分不出保證值與非保證試算值。
        page = f"頁碼:{item['page']}" if item.get("page") else ""
        section = f"章節:{item['section']}" if item.get("section") else ""
        location = "，".join(filter(None, [page, section])) or "頁碼/章節:未註明"
        meta = "，".join(filter(None, [version, location]))
        meta_str = f" ({meta})" if meta else ""
        excerpt = (item.get("excerpt") or "").strip().replace("\n", " ")

        context_lines.append(f"- [{eid}] 《{title}》{meta_str}：\n  「{excerpt}」")

    context_str = "\n".join(context_lines) if context_lines else "（無檢索依據）"
    return context_str, allowed_ids


def validate_rag_response(
    raw_response: str,
    retrieved_evidence_ids: set[str],
    dataset: dict[str, Any],
) -> tuple[bool, dict[str, Any], str | None]:
    """嚴格檢驗 RAG 回應的 JSON 結構、引用邊界與結論支持性。

    回傳：
        (verification_passed, parsed_data, failure_reason)
    """
    if not isinstance(raw_response, str):
        return False, {}, "模型回應必須為 JSON 文字。"
    cleaned_str = clean_json_text(raw_response)
    try:
        data = json.loads(cleaned_str)
    except json.JSONDecodeError as err:
        return False, {}, f"模型回應非合法 JSON 格式：{err}"

    if not isinstance(data, dict):
        return False, {}, f"預期 JSON 格式為物件 (dict)，實際為：{type(data).__name__}"

    if set(data) != {"answer", "citations", "has_sufficient_evidence"}:
        return False, {}, "回應欄位缺漏或包含未定義欄位。"
    answer = data["answer"]
    citations = data["citations"]
    has_sufficient = data["has_sufficient_evidence"]
    if not isinstance(answer, str) or type(has_sufficient) is not bool:
        return False, {}, "answer 必須為字串，has_sufficient_evidence 必須為布林值。"
    answer = answer.strip()

    if not answer:
        return False, data, "回答內容 (answer) 不得為空字串。"

    if not isinstance(citations, list):
        return False, data, "引用依據 (citations) 必須為陣列清單。"
    if any(not isinstance(cid, str) or not cid.strip() for cid in citations):
        return False, {}, "引用 ID 必須為非空字串。"

    all_dataset_evidence_ids = {e["evidence_id"] for e in dataset.get("evidence", [])}

    # 1. 幽靈引用檢查：引用的 ID 必須真實存在於資料集中
    ghost_citations = [cid for cid in citations if cid not in all_dataset_evidence_ids]
    if ghost_citations:
        return False, data, f"回答引用了資料庫中不存在的幽靈依據 ID: {', '.join(ghost_citations)}，整則回答判定為驗證失敗。"

    # 2. 越界引用檢查：引用的 ID 必須完全屬於本次檢索結果
    out_of_bounds = [cid for cid in citations if cid not in retrieved_evidence_ids]
    if out_of_bounds:
        return False, data, f"回答引用了未包含於本次檢索結果的依據 ID: {', '.join(out_of_bounds)}，違反檢索邊界約束，整則回答判定為驗證失敗。"

    # 3. 必要引用缺失檢查：若給出肯定性結論，必須提供至少 1 筆有效引用
    if has_sufficient:
        if not citations:
            return False, data, "回答給出肯定性結論卻未附帶任何依據引用，無法進行追溯查核，判定為驗證失敗。"
    else:
        # 若宣稱依據不足，citations 應為空
        if citations:
            return False, data, "回答標註依據不足 (has_sufficient_evidence=False)，卻同時輸出了引用依據，邏輯衝突，判定為驗證失敗。"

    sanitized = {
        "answer": answer if has_sufficient else INSUFFICIENT_ANSWER,
        "citations": list(dict.fromkeys(citations)),
        "has_sufficient_evidence": has_sufficient,
    }
    return True, sanitized, None


def call_gemini_rag_api(
    prompt: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    client: Any = None,
) -> str:
    """呼叫 Gemini API 進行 RAG 生成。"""
    if not api_key:
        raise ValueError("未提供 GEMINI_API_KEY。")

    if client is None:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
        except Exception as err:
            raise RuntimeError(f"初始化 Google GenAI Client 失敗：{err}") from err

    try:
        from google.genai import types
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RAGOutputSchema,
            temperature=0.1,
        )
        # 503 忙碌、429 額度等暫時性錯誤短暫重試（services/gemini_retry.py）
        response = with_retry(lambda: client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        ))
        if not response or not response.text:
            raise RuntimeError("Gemini API 未回傳有效文字內容。")
        return response.text
    except Exception as err:
        raise RuntimeError(f"Gemini API 呼叫過程中發生錯誤：{err}") from err


def answer_rag_query(
    query: str,
    dataset: dict[str, Any],
    *,
    retriever: BaseRetriever | None = None,
    api_key: str | None = None,
    model: str | None = None,
    client: Any = None,
    mock_llm_response: str | None = None,
    top_k: int = 3,
) -> dict[str, Any]:
    """執行完整 RAG 問答管線：檢索 -> Prompt 組裝 -> 模型生成 -> 嚴格引用驗證。"""
    def failure(message):
        return {"status": "error", "query": query, "answer": message,
                "citations": [], "retrieved_evidence": [], "verification_passed": False,
                "error_message": message, "is_mock": dataset.get("data_mode") == "mock",
                "created_at": datetime.now(timezone.utc).isoformat()}

    if type(top_k) is not int or top_k < 1:
        return failure("top_k 必須為正整數。")
    clean_query = query.strip() if isinstance(query, str) else ""
    if not clean_query:
        return {
            "status": "error",
            "query": query,
            "answer": "請輸入有效的問題內容。",
            "citations": [],
            "retrieved_evidence": [],
            "verification_passed": False,
            "error_message": "問題內容為空。",
            "is_mock": dataset.get("data_mode") == "mock",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    config = get_gemini_config()
    effective_api_key = api_key or config["api_key"]
    effective_model = model or config["model"] or DEFAULT_MODEL
    is_mock = dataset.get("data_mode") == "mock"

    # 1. 檢索步驟
    active_retriever = retriever or MockRetriever(dataset)
    try:
        retrieved_items = active_retriever.search(clean_query, top_k=top_k)
        retrieved_items = validate_retrieved_items(retrieved_items, dataset, top_k)
    except Exception as err:
        return {
            "status": "error",
            "query": clean_query,
            "answer": f"檢索服務發生異常：{err}",
            "citations": [],
            "retrieved_evidence": [],
            "verification_passed": False,
            "error_message": str(err),
            "is_mock": is_mock,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    # 2. 檢索不到任何結果時的處置（不呼叫 LLM 避免幻覺）
    if not retrieved_items:
        return {
            "status": "insufficient_data",
            "query": clean_query,
            "answer": "目前未檢索到足夠依據，無法確認此問題的答案。建議嘗試其他關鍵字、商品全名或保險公司名稱。",
            "citations": [],
            "retrieved_evidence": [],
            "verification_passed": True,
            "error_message": None,
            "is_mock": is_mock,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    # 3. 組裝 Prompt
    try:
        context_str, allowed_ids = format_retrieved_context(retrieved_items)
        raw_prompt = load_rag_prompt()
        if not all("{" + key + "}" in raw_prompt for key in
                   ("retrieved_context", "allowed_evidence_ids", "query")):
            raise ValueError("Prompt 缺少必要輸入欄位。")
        filled_prompt = raw_prompt.format(
            retrieved_context=context_str,
            allowed_evidence_ids=", ".join(allowed_ids),
            query=clean_query,
        )
    except (OSError, ValueError, KeyError, IndexError) as err:
        return failure(f"問答 Prompt 載入或格式化失敗：{err}")

    # 4. 模型生成或使用 Mock 回應
    if mock_llm_response is not None:
        raw_output = mock_llm_response
    else:
        if not effective_api_key:
            return {
                "status": "error",
                "query": clean_query,
                "answer": "尚未設定 GEMINI_API_KEY。請在環境變數或 .streamlit/secrets.toml 中設定金鑰後再發起提問。",
                "citations": [],
                "retrieved_evidence": retrieved_items,
                "verification_passed": False,
                "error_message": "未設定 GEMINI_API_KEY。",
                "is_mock": is_mock,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        try:
            raw_output = call_gemini_rag_api(
                filled_prompt,
                api_key=effective_api_key,
                model=effective_model,
                client=client,
            )
        except Exception as err:
            return {
                "status": "error",
                "query": clean_query,
                "answer": friendly_message(err, "AI 回答生成失敗："),
                "citations": [],
                "retrieved_evidence": retrieved_items,
                "verification_passed": False,
                "error_message": str(err),
                "is_mock": is_mock,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

    # 5. 嚴格引用與結構驗證
    passed, parsed, reason = validate_rag_response(
        raw_output,
        retrieved_evidence_ids=set(allowed_ids),
        dataset=dataset,
    )

    if not passed:
        return {
            "status": "validation_failed",
            "query": clean_query,
            "answer": "回答內容因引用驗證未通過而被安全攔截。",
            "citations": [],
            "retrieved_evidence": retrieved_items,
            "verification_passed": False,
            "error_message": reason,
            "is_mock": is_mock,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    status = "success" if parsed["has_sufficient_evidence"] else "insufficient_data"
    return {
        "status": status,
        "query": clean_query,
        "answer": parsed["answer"],
        "citations": parsed["citations"],
        "retrieved_evidence": retrieved_items,
        "verification_passed": True,
        "error_message": None,
        "is_mock": is_mock,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
