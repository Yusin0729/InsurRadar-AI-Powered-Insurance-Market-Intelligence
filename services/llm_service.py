"""InsurRadar LLM 智慧分析服務與驗證模組。

主要用途與功能：
1. 讀取商品、當月宣告利率、新聞與證據等結構化資料，組裝為 LLM 輸入上下文。
2. 透過 Google GenAI 官方 SDK 呼叫 Gemini 模型，取得結構化 JSON 分析。
3. 嚴格驗證 AI 產出之商品 ID、證據 ID、必要欄位與數值一致性，防範幻覺。
4. 強制約束生成狀態（generation_mode='llm', conclusion_review_status='unreviewed'）。

外部套件：
- google.genai: Google 官方 Gemini API SDK，用於呼叫模型與設定結構化輸出。
- dotenv: 讀取本機環境變數（.env）。
- streamlit: 讀取本機 secrets 及管理前台快取狀態。
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from dotenv import load_dotenv

from services.gemini_retry import BUSY_MESSAGE, is_transient, with_retry

# 嘗試載入 .env 檔案
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = PROJECT_ROOT / "prompts" / "analysis.txt"
# 新申請的免費金鑰已不能用 gemini-2.5-flash（回 404）；2026-11 實測可用。GEMINI_MODEL 仍可覆寫。
DEFAULT_MODEL = "gemini-3.5-flash-lite"


class LLMConfigError(ValueError):
    """LLM 設定錯誤（如未提供 API Key 或模型設定無效）。"""


class LLMAPIError(RuntimeError):
    """Gemini API 呼叫失敗（網路逾時、配額不足或模型異常）。"""


class LLMValidationError(ValueError):
    """LLM 輸出無法解析為有效 JSON 或不符合結構定義。"""


def get_gemini_config() -> dict[str, str | None]:
    """取得 Gemini API 設定，優先順序：環境變數 > Streamlit Secrets。"""
    api_key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL")
    if not api_key or not model:
        try:
            import streamlit as st
            api_key = api_key or st.secrets.get("GEMINI_API_KEY")
            model = model or st.secrets.get("GEMINI_MODEL")
        except Exception:
            pass
    return {"api_key": api_key, "model": model or DEFAULT_MODEL}


def load_analysis_prompt() -> str:
    """載入獨立的 AI 分析 Prompt 範本。"""
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"找不到 Prompt 範本檔案：{PROMPT_PATH}")
    with PROMPT_PATH.open("r", encoding="utf-8") as f:
        return f.read()


def prepare_analysis_context(dataset: dict[str, Any], *, product_ids: list[str] | None = None) -> dict[str, Any]:
    """自資料集提取事實資料，格式化為簡潔結構化文字與供驗證使用的查找表。"""
    all_products = dataset.get("products", [])
    if product_ids:
        target_product_ids = set(product_ids)
        products = [p for p in all_products if p["product_id"] in target_product_ids]
    else:
        products = all_products
        target_product_ids = {p["product_id"] for p in products}

    valid_product_ids = {p["product_id"] for p in all_products}
    valid_evidence_ids = {e["evidence_id"] for e in dataset.get("evidence", [])}

    # 1. 商品規格文字
    products_lines = []
    for p in products:
        rules_str = "；".join(
            f"{r['payment_term_years']}年期(投保年齡:{r['min_issue_age']}~{r['max_issue_age']}歲, 條件:{r.get('conditions') or '無'})"
            for r in p.get("eligibility_rules", [])
        )
        products_lines.append(
            f"- [{p['product_id']}] {p['company_name']} - {p['product_name']} "
            f"(幣別: {p['currency']}, 通路: {', '.join(p.get('channels', []))}, 規則: {rules_str})"
        )
    products_context = "\n".join(products_lines) or "無商品資料"

    # 2. 利率文字與數值檢索表
    rates_lines = []
    rates_lookup: dict[str, list[dict]] = {}  # product_id -> list of rates
    for r in dataset.get("rates", []):
        pid = r["product_id"]
        if pid in target_product_ids:
            rates_lookup.setdefault(pid, []).append(r)
            rate_val = f"{r['rate_percent']}%" if r.get("rate_percent") is not None else "資料不足"
            rates_lines.append(
                f"- [商品:{pid}] 月份:{r['period_month']} 宣告利率:{rate_val} "
                f"(條件:{r.get('applicable_conditions') or '標準'}, 引用:{', '.join(r.get('evidence_ids', []))})"
            )
    rates_context = "\n".join(rates_lines) or "無宣告利率資料"

    # 3. 新聞情報摘要
    news_lines = []
    for n in dataset.get("news", [])[:6]:  # 取最多前 6 則重要新聞
        points = "；".join(sp["text"] for sp in n.get("summary_points", []))
        news_lines.append(
            f"- [{n['news_id']}] {n['title']} ({n.get('publisher') or '未知來源'}, {n.get('published_at') or '日期待查'}): {points}"
        )
    news_context = "\n".join(news_lines) or "無新聞情報"

    # 4. 證據原文文字
    evidence_lines = []
    for e in dataset.get("evidence", []):
        excerpt = (e.get("excerpt") or "").strip().replace("\n", " ")
        evidence_lines.append(
            f"- [{e['evidence_id']}] 《{e['source_title']}》 (版本:{e.get('document_version') or '未知'}, 頁碼/章節:{e.get('page') or e.get('section') or '未註明'}): {excerpt}"
        )
    evidence_context = "\n".join(evidence_lines) or "無依據原文"

    return {
        "products_context": products_context,
        "rates_context": rates_context,
        "news_context": news_context,
        "evidence_context": evidence_context,
        "valid_product_ids": sorted(valid_product_ids),
        "valid_evidence_ids": sorted(valid_evidence_ids),
        "rates_lookup": rates_lookup,
        "is_mock": dataset.get("data_mode") == "mock",
    }


def clean_json_text(text: str) -> str:
    """清理 LLM 可能夾帶的 Markdown 程式碼區塊標記。"""
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def validate_and_sanitize_insights(
    raw_json_str: str,
    dataset: dict[str, Any],
    rates_lookup: dict[str, list[dict]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """解析並驗證 LLM 產生的觀察列表，分開回傳合格與被拒絕之項目。"""
    cleaned_str = clean_json_text(raw_json_str)
    try:
        data = json.loads(cleaned_str)
    except json.JSONDecodeError as err:
        raise LLMValidationError(f"Gemini 回應內容非合法 JSON：{err}") from err

    if not isinstance(data, list):
        raise LLMValidationError(f"預期的 JSON 結構應為陣列（Array），實際為：{type(data).__name__}")

    valid_pids = {p["product_id"] for p in dataset.get("products", [])}
    valid_eids = {e["evidence_id"] for e in dataset.get("evidence", [])}
    is_mock = dataset.get("data_mode") == "mock"

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for index, item in enumerate(data, start=1):
        rejection_reasons = []

        if not isinstance(item, dict):
            rejected.append({"raw_item": item, "reasons": ["資料項並非物件 (dict)"]})
            continue

        title = str(item.get("title", "")).strip()
        statement = str(item.get("statement", "")).strip()
        product_ids = item.get("product_ids")
        evidence_ids = item.get("evidence_ids")

        # 1. 欄位存在與非空檢核
        if not title:
            rejection_reasons.append("缺少 title 或為空字串")
        if not statement:
            rejection_reasons.append("缺少 statement 或為空字串")

        # 2. 商品 ID 檢核
        if not isinstance(product_ids, list) or not product_ids:
            rejection_reasons.append("product_ids 必須為非空清單")
        else:
            invalid_pids = [pid for pid in product_ids if pid not in valid_pids]
            if invalid_pids:
                rejection_reasons.append(f"引用了不存在的商品 ID: {', '.join(invalid_pids)}")

        # 3. 證據 ID 檢核
        if not isinstance(evidence_ids, list) or not evidence_ids:
            rejection_reasons.append("evidence_ids 必須為非空清單（至少 1 個支持引用）")
        else:
            invalid_eids = [eid for eid in evidence_ids if eid not in valid_eids]
            if invalid_eids:
                rejection_reasons.append(f"引用了不存在的證據 ID: {', '.join(invalid_eids)}")

        # 4. 數值一致性檢驗（比對陳述中提及的百分比數字）
        mentioned_percentages = re.findall(r"(\d+(?:\.\d+)?)\s*%", statement)
        if mentioned_percentages and isinstance(product_ids, list):
            # 收集引用商品的實際宣告利率
            actual_rates = set()
            for pid in product_ids:
                for r in rates_lookup.get(pid, []):
                    if r.get("rate_percent") is not None:
                        actual_rates.add(f"{float(r['rate_percent']):.2f}")
                        actual_rates.add(f"{float(r['rate_percent']):.1f}")
                        actual_rates.add(f"{r['rate_percent']}")

            for pct in mentioned_percentages:
                try:
                    pct_float = f"{float(pct):.2f}"
                    pct_float_1 = f"{float(pct):.1f}"
                    # 若為利率相關數值且與資料庫宣告利率皆不吻合，列出警示/檢查
                    if actual_rates and (pct not in actual_rates and pct_float not in actual_rates and pct_float_1 not in actual_rates):
                        # 如果是微小利差分析（例如 15 個基點或 0.15%），排除誤殺，但若陳述宣稱該保單為某百分比則防護
                        if "宣告利率" in statement or "利率為" in statement or "達" in statement:
                            rejection_reasons.append(f"陳述中的宣告利率 {pct}% 未在關聯商品的實際利率紀錄中找到吻合值")
                except ValueError:
                    pass

        if rejection_reasons:
            rejected.append({"raw_item": item, "reasons": rejection_reasons})
            continue

        # 5. 合格項目正規化與嚴格審查狀態賦予
        insight_id = item.get("insight_id") or f"llm_insight_{index:03d}"
        if not insight_id.startswith("llm_insight_"):
            insight_id = f"llm_insight_{index:03d}"

        sanitized_insight = {
            "insight_id": insight_id,
            "is_mock": is_mock,
            "title": title,
            "statement": statement,
            "product_ids": list(dict.fromkeys(product_ids)),
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "generation_mode": "llm",
            "conclusion_review_status": "unreviewed",
            "conclusion_reviewed_by": None,
            "conclusion_reviewed_at": None,
        }
        accepted.append(sanitized_insight)

    return accepted, rejected


def call_gemini_api(
    prompt: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    client: Any = None,
) -> str:
    """呼叫 Gemini API 並取得純文字回應。"""
    if not api_key:
        raise LLMConfigError("未提供 GEMINI_API_KEY，無法呼叫 Gemini API。")

    if client is None:
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
        except Exception as err:
            raise LLMConfigError(f"初始化 Google GenAI Client 失敗：{err}") from err

    try:
        from google.genai import types
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.15,
        )
        # 503 忙碌、429 額度等暫時性錯誤短暫重試（services/gemini_retry.py）
        response = with_retry(lambda: client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        ))
        if not response or not response.text:
            raise LLMAPIError("Gemini API 未回傳有效文字內容。")
        return response.text
    except Exception as err:
        if isinstance(err, (LLMConfigError, LLMAPIError)):
            raise
        if is_transient(err):
            raise LLMAPIError(BUSY_MESSAGE) from err
        raise LLMAPIError(f"Gemini API 呼叫過程中發生錯誤：{err}") from err


def run_ai_analysis(
    dataset: dict[str, Any],
    *,
    product_ids: list[str] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    client: Any = None,
    mock_response: str | None = None,
) -> dict[str, Any]:
    """執行完整 AI 分析管線。

    支援提供 mock_response 供測試使用，避免無謂消耗實際 API 額度。
    """
    config = get_gemini_config()
    effective_api_key = api_key or config["api_key"]
    effective_model = model or config["model"] or DEFAULT_MODEL

    # 1. 檢查資料是否充足
    if not dataset.get("products") or not dataset.get("evidence"):
        return {
            "status": "insufficient_data",
            "insights": [],
            "rejected_insights": [],
            "error_message": "資料集中無商品或證據資料，無法進行 AI 分析。",
            "model": effective_model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    # 2. 組裝上下文
    context = prepare_analysis_context(dataset, product_ids=product_ids)
    raw_prompt = load_analysis_prompt()
    filled_prompt = raw_prompt.format(
        products_context=context["products_context"],
        rates_context=context["rates_context"],
        news_context=context["news_context"],
        evidence_context=context["evidence_context"],
        valid_product_ids=", ".join(context["valid_product_ids"]),
        valid_evidence_ids=", ".join(context["valid_evidence_ids"]),
    )

    # 3. 呼叫 API 或使用 Mock 回應
    if mock_response is not None:
        raw_output = mock_response
    else:
        if not effective_api_key:
            return {
                "status": "error",
                "insights": [],
                "rejected_insights": [],
                "error_message": "尚未設定 GEMINI_API_KEY。請於系統環境變數或 .streamlit/secrets.toml 中設定金鑰後重試。",
                "model": effective_model,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        try:
            raw_output = call_gemini_api(
                filled_prompt,
                api_key=effective_api_key,
                model=effective_model,
                client=client,
            )
        except Exception as err:
            return {
                "status": "error",
                "insights": [],
                "rejected_insights": [],
                "error_message": f"AI 分析呼叫失敗：{err}",
                "model": effective_model,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

    # 4. 驗證與過濾
    try:
        accepted, rejected = validate_and_sanitize_insights(
            raw_output,
            dataset=dataset,
            rates_lookup=context["rates_lookup"],
        )
    except LLMValidationError as err:
        return {
            "status": "error",
            "insights": [],
            "rejected_insights": [],
            "error_message": f"分析結果格式驗證失敗：{err}",
            "model": effective_model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    if not accepted and rejected:
        return {
            "status": "error",
            "insights": [],
            "rejected_insights": rejected,
            "error_message": f"模型產生的所有觀察均因不符合驗證規範而遭剔除（共 {len(rejected)} 項遭拒）。",
            "model": effective_model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    return {
        "status": "success",
        "insights": accepted,
        "rejected_insights": rejected,
        "error_message": None,
        "model": effective_model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_mock_data": context["is_mock"],
    }
