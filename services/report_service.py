"""既有快照與生成結果的 Markdown 報告；僅使用標準庫，不生成 AI 內容。"""

from copy import deepcopy
import hashlib
import json
import re

from services.data_service import get_products, get_declared_rates, get_insights, get_news, get_evidence
from services.validation import validate_insight_evidence


STATE_KEYS = (
    "filter_company_ids", "filter_payment_term_years", "filter_issue_age_enabled",
    "filter_issue_age", "filter_keyword", "filter_period_month", "comparison_product_ids",
    "market_keyword", "market_category", "market_company_ids", "market_product_ids",
    "market_date_from", "market_date_to", "gemini_dashboard_analysis", "rag_chat_messages",
)


def report_state(state):
    """只取匯出需要的狀態，不複製金鑰、widget 或彈窗狀態。"""
    defaults = {key: None for key in STATE_KEYS}
    for key in ("filter_company_ids", "comparison_product_ids", "market_company_ids",
                "market_product_ids", "rag_chat_messages"):
        defaults[key] = []
    for key in ("filter_keyword", "market_keyword"):
        defaults[key] = ""
    defaults.update(filter_issue_age_enabled=False, filter_issue_age=30)
    defaults.update({key: state[key] for key in STATE_KEYS if key in state})
    return deepcopy(defaults)


def report_fingerprint(dataset, state):
    payload = json.dumps([dataset, report_state(state)], ensure_ascii=False,
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def text_block(value):
    """圍欄長於內容中的所有反引號串，保留原文與換行、不解析 HTML。"""
    text = "資料待補" if value is None or value == "" else str(value)
    fence = "`" * max(3, 1 + max((len(s) for s in re.findall(r"`+", text)), default=0))
    return f"{fence}text\n{text}" + ("" if text.endswith("\n") else "\n") + f"{fence}\n"


def cell(value):
    """表格中的動態文字只當純文字呈現。"""
    text = "資料待補" if value is None or value == "" else str(value)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def build_markdown_report(dataset, state, *, generated_at):
    """輸入已驗證快照及狀態副本；固定時間時結果可重現，不修改輸入。"""
    state = report_state(state)
    warnings, used = [], set()
    lines = ["# InsurRadar 分析報告\n", "## 報告資訊\n"]

    def field(label, value):
        lines.extend([f"{label}：\n", text_block(value)])

    def references(ids, *, required=True):
        if not isinstance(ids, list) or any(not isinstance(eid, str) for eid in ids):
            return False
        return (bool(ids) or not required) and all(get_evidence(dataset, eid) is not None for eid in ids)

    def cite(ids):
        if references(ids, required=False):
            used.update(ids)
            field("引用證據", "、".join(ids) or "未提供引用")
        else:
            warnings.append("資料項引用失效，未將無效 ID 列為有效引用。")
            field("引用狀態", "引用失效，無法完整追溯。")

    field("產生時間（Asia/Taipei）", generated_at)
    field("Dataset ID", dataset["dataset_id"])
    field("資料基準時間", dataset["as_of"])
    field("資料性質", "Mock Data／模擬資料" if dataset["data_mode"] == "mock" else "真實資料快照")
    products = get_products(dataset, company_ids=state.get("filter_company_ids") or None,
                            payment_term_years=state.get("filter_payment_term_years"),
                            issue_age=state.get("filter_issue_age") if state.get("filter_issue_age_enabled") else None,
                            keyword=state.get("filter_keyword"))
    selected = state.get("comparison_product_ids") or []
    if selected:
        products = [p for p in products if p["product_id"] in selected]
    pids = {p["product_id"] for p in products}
    restricted = bool(selected or state.get("filter_company_ids") or state.get("filter_keyword")
                      or state.get("filter_payment_term_years") is not None
                      or state.get("filter_issue_age_enabled"))
    lines.append("## 商品與市場情報範圍（獨立篩選）\n")
    labels = {"filter_company_ids": "商品公司", "filter_payment_term_years": "繳費年期（0 為躉繳）",
              "filter_keyword": "商品關鍵字", "filter_period_month": "宣告利率月份",
              "comparison_product_ids": "指定比較商品", "market_keyword": "情報關鍵字",
              "market_category": "情報分類", "market_company_ids": "情報公司",
              "market_product_ids": "情報商品", "market_date_from": "情報日期起", "market_date_to": "情報日期迄"}
    for key, label in labels.items():
        field(label, state.get(key) if state.get(key) not in (None, "", []) else "未指定")
    field("投保年齡", state.get("filter_issue_age") if state.get("filter_issue_age_enabled") else "未啟用")
    field("報告商品 ID", "、".join(sorted(pids)) or "沒有符合條件的商品")
    lines.append("商品條件不代表利率適用或核保通過；市場情報不套用商品側欄條件。\n")
    lines.append("## 商品及宣告利率比較\n")
    if not products:
        lines.append("沒有符合條件的商品。\n")
    for product in sorted(products, key=lambda p: p["product_id"]):
        lines.append("### 商品\n")
        for key, label in (("product_id", "商品 ID"), ("company_name", "公司"), ("product_name", "名稱"),
                           ("currency", "幣別"), ("benefits", "保障特色"), ("channels", "通路"),
                           ("launched_on", "上市日期"), ("discontinued_on", "停售日期（缺值不代表仍在售）")):
            field(label, product.get(key))
        cite(product.get("evidence_ids", []))
        for rule in product.get("eligibility_rules", []):
            field("投保規則", json.dumps({k: rule.get(k) for k in
                  ("payment_term_years", "min_issue_age", "max_issue_age", "conditions")}, ensure_ascii=False))
            cite(rule.get("evidence_ids", []))
    month = state.get("filter_period_month")
    rates = get_declared_rates(dataset, product_ids=sorted(pids), period_month=month) if month else []
    lines.extend(["| 商品 ID | 月份 | 宣告利率 % | 適用條件 | 生效起 | 生效迄 |\n",
                  "| --- | --- | --- | --- | --- | --- |\n"])
    for rate in sorted(rates, key=lambda r: (r["product_id"], r["rate_id"])):
        lines.append("| " + " | ".join(cell(rate.get(k)) for k in
                     ("product_id", "period_month", "rate_percent", "applicable_conditions", "effective_from", "effective_to")) + " |\n")
    if not rates:
        lines.append("\n指定範圍無宣告利率資料，不沿用其他月份。\n")
    for rate in sorted(rates, key=lambda r: r["rate_id"]):
        field("利率紀錄 ID", rate["rate_id"])
        cite(rate.get("evidence_ids", []))

    lines.append("## AI 觀察與複核狀態\n")
    lines.append("既有觀察未依本次年期、年齡、關鍵字或月份重新生成；只依商品關聯篩選，適用性仍須人工核對。\n")

    def write_insights(items, origin):
        included = 0
        for item in sorted(items, key=lambda x: x.get("insight_id", "")):
            ident = item.get("insight_id", "未知觀察")
            related = item.get("product_ids", [])
            if restricted and (not related or not set(related).issubset(pids)):
                warnings.append(f"觀察 {ident} 排除：涉及範圍外商品或無法確認商品關聯，並非資料不存在。")
                continue
            review = validate_insight_evidence(item, dataset)
            if not review["references_valid"]:
                warnings.append(f"觀察 {ident} 排除：引用失效或未提供有效引用。")
                continue
            included += 1
            field("觀察來源", origin)
            field("觀察 ID", ident)
            field("觀察標題", item.get("title"))
            field("觀察內容", item.get("statement"))
            field("複核狀態", json.dumps(review, ensure_ascii=False, sort_keys=True))
            field("結論狀態", "已驗證結論" if review["can_display_as_verified"] else "未符合已驗證結論條件；Mock 複核僅為模擬紀錄。" if review["is_mock"] else "尚未符合已驗證結論條件，待人工複核。")
            cite(item["evidence_ids"])
        return included

    counts = {"products": len(products), "rates": len(rates)}
    counts["insights"] = write_insights(get_insights(dataset), "快照既有觀察（非本次生成）")
    ai = state.get("gemini_dashboard_analysis")
    if not ai:
        lines.append("Gemini AI 分析尚未產生。\n")
    elif ai.get("dataset_id") != dataset["dataset_id"]:
        warnings.append("Gemini AI 分析排除：快照不一致或缺少 Dataset ID，無法確認歸屬。")
    elif ai.get("status") != "success":
        lines.append("Gemini AI 分析未成功產生。\n")
    else:
        field("Gemini 模型", ai.get("model"))
        field("Gemini 生成時間", ai.get("created_at"))
        counts["insights"] += write_insights(ai.get("insights", []), "Gemini 既有生成結果")
        if not ai.get("insights"):
            lines.append("Gemini AI 分析沒有可收錄的觀察。\n")

    lines.append("## 既有 RAG 問答\n")
    messages = state.get("rag_chat_messages") or []
    counts["rag_answers"] = 0
    if not messages:
        lines.append("RAG 問答尚未產生。\n")
    pending = None
    for msg in messages:
        if msg.get("role") == "user":
            pending = msg
            continue
        question, pending = pending, None
        if not question or any(m.get("dataset_id") != dataset["dataset_id"] for m in (question, msg)):
            warnings.append("RAG 紀錄排除：問題或回答缺少目前快照歸屬。")
            continue
        if msg.get("status") != "success":
            field("問題", question.get("content"))
            field("回答狀態", "資料不足／生成失敗／引用未通過；不收錄模型結論。")
            continue
        citations, retrieved = msg.get("citations"), msg.get("retrieved_evidence")
        valid = references(citations) and isinstance(retrieved, list) and bool(retrieved)
        retrieval_ids, related = set(), set()
        for entry in retrieved if isinstance(retrieved, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("evidence_id"), str):
                valid = False
                continue
            source = get_evidence(dataset, entry["evidence_id"])
            excerpt = entry.get("excerpt")
            if (source is None or not isinstance(excerpt, str) or not excerpt.strip()
                    or not source.get("excerpt")
                    or " ".join(excerpt.split()) not in " ".join(source["excerpt"].split())
                    or any(entry.get(k) != source.get(k) for k in
                           ("source_title", "source_type", "document_version", "page", "section", "source_url", "product_ids"))):
                valid = False
                continue
            retrieval_ids.add(entry["evidence_id"])
            related.update(source.get("product_ids", []))
            if restricted and not source.get("product_ids"):
                valid = False
        if (not valid or not set(citations or []).issubset(retrieval_ids)
                or not isinstance(msg.get("product_ids"), list)
                or any(not isinstance(pid, str) for pid in msg["product_ids"])
                or set(msg["product_ids"]) != related
                or (restricted and (not related or not related.issubset(pids)))):
            warnings.append("RAG 回答排除：引用、當次檢索或已記錄商品關聯不一致，或超出／無法確認目前商品範圍。")
            continue
        counts["rag_answers"] += 1
        field("問題", question.get("content"))
        field("生成時間", msg.get("created_at"))
        field("回答", msg.get("content"))
        lines.append("引用存在與檢索範圍檢查通過；語意支持性未驗證，結論待人工複核。\n")
        cite(citations)

    lines.append("## 市場情報摘要\n")
    news = get_news(dataset, category=state.get("market_category"),
                    company_ids=state.get("market_company_ids") or None,
                    product_ids=state.get("market_product_ids") or None,
                    date_from=state.get("market_date_from"), date_to=state.get("market_date_to"))
    keyword = (state.get("market_keyword") or "").strip().casefold()
    news = [n for n in news if not keyword or keyword in " ".join([
        n["title"], n.get("publisher") or "", *[p["text"] for p in n.get("summary_points", [])]]).casefold()]
    counts["news"] = len(news)
    if not news:
        lines.append("目前情報範圍無資料。\n")
    for item in sorted(news, key=lambda n: n["news_id"]):
        for key, label in (("news_id", "情報 ID"), ("title", "標題"), ("publisher", "發布者"),
                           ("published_at", "發布時間"), ("source_url", "來源網址")):
            field(label, item.get(key))
        if not item.get("summary_points"):
            lines.append("情報摘要待補。\n")
        for point in item.get("summary_points", []):
            if references(point.get("evidence_ids")):
                field("既有摘要", point["text"])
                cite(point["evidence_ids"])
            else:
                warnings.append(f"情報 {item['news_id']} 摘要排除：引用失效或缺漏。")
        cite(item.get("evidence_ids", []))

    lines.append("## 引用證據與原始來源\n")
    for eid in sorted(used):
        source = get_evidence(dataset, eid)
        lines.append("### 證據\n")
        for key, label in (("evidence_id", "證據 ID"), ("source_title", "文件標題"),
                           ("source_type", "來源類型"), ("source_date", "來源日期"),
                           ("document_version", "文件版本"), ("source_url", "來源網址"),
                           ("local_path", "檔案定位"), ("page", "頁碼"), ("section", "章節"),
                           ("captured_at", "擷取時間"), ("human_review_status", "人工複核狀態"),
                           ("reviewed_by", "複核者"), ("reviewed_at", "複核時間"), ("excerpt", "證據原文")):
            field(label, source.get(key))
        review = validate_insight_evidence({"evidence_ids": [eid]}, dataset)
        if not review["evidence_complete"]:
            field("證據完整性", "資料待補；完整性不代表語意支持性。")
    lines.append("## 排除與資料限制\n")
    for warning in warnings:
        lines.append(text_block(warning))
    if dataset["data_mode"] == "mock":
        field("Mock Data 聲明", dataset.get("mock_notice"))
        lines.append("商品、數值、原文與複核紀錄均為模擬，不代表真實市場資訊。\n")
    lines.append("宣告利率不是保證報酬或 IRR；缺值不補零，不推測日期、來源或版本。匯出只使用既有資料，未呼叫 Gemini。引用存在不等於結論已經人工驗證。\n")
    return {"markdown": "\n".join(lines), "warnings": warnings, "counts": counts,
            "evidence_ids": sorted(used), "product_ids": sorted(pids)}
