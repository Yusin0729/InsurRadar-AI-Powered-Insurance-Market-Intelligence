"""驗證 InsurRadar 快照、資料關聯及引用狀態；外部套件：jsonschema。"""

from calendar import monthrange
from datetime import date, datetime
import math
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker


COLLECTION_IDS = {
    "products": "product_id",
    "rates": "rate_id",
    "news": "news_id",
    "insights": "insight_id",
    "evidence": "evidence_id",
    "investigations": "investigation_id",
}

FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date", raises=ValueError)
def _check_date(value):
    if not isinstance(value, str):
        return True
    return bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)) and bool(date.fromisoformat(value))


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _check_datetime(value):
    if not isinstance(value, str):
        return True
    pattern = (
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
    )
    if not re.fullmatch(pattern, value):
        return False
    return datetime.fromisoformat(value.upper()).utcoffset() is not None


@FORMAT_CHECKER.checks("uri", raises=ValueError)
def _check_url(value):
    if not isinstance(value, str):
        return True
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and not any(character.isspace() for character in value)
        and (parsed.port is None or 0 < parsed.port <= 65535)
    )


def _evidence_gaps(evidence):
    """完整性只代表可追溯資料齊備，不判斷原文是否支持結論。"""
    missing = [
        field for field in ("source_title", "source_date", "document_version", "excerpt")
        if not evidence.get(field)
    ]
    if not evidence.get("source_url") and not evidence.get("local_path"):
        missing.append("source_url|local_path")
    if evidence.get("source_type") == "pdf" and evidence.get("page") is None:
        missing.append("page")
    if evidence.get("local_path") and not evidence.get("page") and not evidence.get("section"):
        missing.append("page|section")
    if not evidence.get("is_mock", True) and not evidence.get("captured_at"):
        missing.append("captured_at")
    return missing


def validate_insight_evidence(insight: dict, dataset: dict) -> dict:
    """分開回報引用、證據複核及結論複核；不修改輸入或自動核准結論。

    呼叫端應先對完整快照執行 validate_dataset。此函式也可檢查新增觀察
    是否引用不存在的證據；Mock 永遠不能取得真實結論的已驗證顯示資格。
    """
    evidence_index = {item["evidence_id"]: item for item in dataset["evidence"]}
    citations = insight.get("evidence_ids", [])
    citations_valid = (
        isinstance(citations, list) and bool(citations)
        and all(isinstance(item, str) and item.strip() for item in citations)
    )
    citation_ids = list(dict.fromkeys(citations)) if citations_valid else []
    missing_ids = [item for item in citation_ids if item not in evidence_index]
    references_valid = bool(citations_valid and len(citation_ids) == len(citations) and not missing_ids)
    evidence_items = [evidence_index[item] for item in citation_ids if item in evidence_index]
    incomplete = {
        item["evidence_id"]: _evidence_gaps(item)
        for item in evidence_items if _evidence_gaps(item)
    }
    human_statuses = {item["evidence_id"]: item["human_review_status"] for item in evidence_items}
    human_confirmed = bool(references_valid and all(
        item["human_review_status"] == "confirmed"
        and item.get("reviewed_by") and item.get("reviewed_at")
        for item in evidence_items
    ))
    conclusion_status = insight.get("conclusion_review_status", "unreviewed")
    conclusion_confirmed = bool(
        conclusion_status == "confirmed"
        and insight.get("conclusion_reviewed_by") and insight.get("conclusion_reviewed_at")
    )
    is_mock = bool(
        dataset.get("data_mode") != "real" or insight.get("is_mock", True)
        or insight.get("generation_mode") == "mock"
        or any(item.get("is_mock", True) or item.get("source_type") == "mock" for item in evidence_items)
    )
    evidence_complete = bool(references_valid and not incomplete)
    return {
        "is_mock": is_mock,
        "references_valid": references_valid,
        "missing_evidence_ids": missing_ids,
        "evidence_complete": evidence_complete,
        "incomplete_evidence": incomplete,
        "evidence_human_review_statuses": human_statuses,
        "all_evidence_human_confirmed": human_confirmed,
        "conclusion_review_status": conclusion_status,
        "can_display_as_verified": bool(
            not is_mock and evidence_complete and human_confirmed and conclusion_confirmed
        ),
    }


def validate_dataset(dataset: dict, schema: dict) -> dict:
    """回傳 errors、warnings 與筆數；結構錯誤時不繼續解讀不可信的欄位。

    errors 應阻止快照載入；warnings 保留合法缺值及尚未複核的觀察。
    日期、時間與 HTTP(S) URL 使用標準函式庫檢查，不依賴額外格式套件。
    """
    report = {"errors": [], "warnings": [], "counts": {}}

    def add(level, code, path, message, record_id=None):
        report[level].append({"code": code, "path": path, "record_id": record_id, "message": message})

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FORMAT_CHECKER)
    for error in sorted(validator.iter_errors(dataset), key=lambda item: tuple(map(str, item.absolute_path))):
        path_parts = list(error.absolute_path)
        record_id = None
        if len(path_parts) >= 2 and path_parts[0] in COLLECTION_IDS and isinstance(path_parts[1], int):
            record = dataset[path_parts[0]][path_parts[1]]
            if isinstance(record, dict):
                record_id = record.get(COLLECTION_IDS[path_parts[0]])
        add("errors", "schema", "/" + "/".join(map(str, path_parts)), error.message, record_id)
    if report["errors"]:
        return report

    report["counts"] = {name: len(dataset[name]) for name in COLLECTION_IDS}
    indexes = {}
    mock_mode = dataset["data_mode"] == "mock"
    snapshot_time = datetime.fromisoformat(dataset["as_of"].upper())
    snapshot_date = snapshot_time.date()
    if mock_mode and not dataset["mock_notice"]:
        add("errors", "mock_notice", "/mock_notice", "Mock 快照必須提供模擬資料說明。")
    if not mock_mode and dataset["mock_notice"] is not None:
        add("errors", "mock_notice", "/mock_notice", "真實快照的 mock_notice 必須為 null。")

    for collection, id_field in COLLECTION_IDS.items():
        indexes[collection] = {}
        for position, record in enumerate(dataset[collection]):
            record_id = record[id_field]
            path = f"/{collection}/{position}"
            if record_id in indexes[collection]:
                add("errors", "duplicate_id", f"{path}/{id_field}", "同類資料的主鍵不得重複。", record_id)
            indexes[collection][record_id] = record
            if record["is_mock"] != mock_mode or record_id.startswith("mock_") != mock_mode:
                add("errors", "mock_mismatch", path, "紀錄標記及 ID 必須與快照的 Mock／真實模式一致。", record_id)
            if "generation_mode" in record and (record["generation_mode"] == "mock") != mock_mode:
                add("errors", "mock_generation", f"{path}/generation_mode", "模擬生成內容不可混入真實快照。", record_id)
            if mock_mode and record.get("source_url") is not None:
                add("errors", "mock_source_url", f"{path}/source_url", "模擬內容不使用冒充真實來源的外部網址。", record_id)

    company_names = {}
    for position, product in enumerate(dataset["products"]):
        product_id = product["product_id"]
        path = f"/products/{position}"
        company_id = product["company_id"]
        if company_id in company_names and company_names[company_id] != product["company_name"]:
            add("errors", "company_name_conflict", f"{path}/company_name", "同一公司 ID 的名稱必須一致。", product_id)
        company_names[company_id] = product["company_name"]
        if company_id.startswith("mock_") != mock_mode:
            add("errors", "mock_company", f"{path}/company_id", "公司 ID 必須與快照模式一致。", product_id)
        if mock_mode and not all(product[field].startswith("模擬") for field in ("company_name", "product_name")):
            add("errors", "mock_product_label", path, "模擬公司及商品名稱必須以「模擬」開頭。", product_id)
        start, end = product["launched_on"], product["discontinued_on"]
        if start and end and start > end:
            add("errors", "date_order", path, "停售日不可早於上市日。", product_id)
        if not product["evidence_ids"]:
            add("warnings", "product_without_evidence", f"{path}/evidence_ids", "商品來源證據待補。", product_id)
        for rule_position, rule in enumerate(product["eligibility_rules"]):
            rule_path = f"{path}/eligibility_rules/{rule_position}"
            minimum, maximum = rule["min_issue_age"], rule["max_issue_age"]
            if minimum is not None and maximum is not None and minimum > maximum:
                add("errors", "age_order", rule_path, "最低投保年齡不可大於最高投保年齡。", product_id)
            if minimum is None or maximum is None:
                add("warnings", "eligibility_incomplete", rule_path, "投保年齡資料不足，不得視為已符合投保條件。", product_id)
            if not set(rule["evidence_ids"]).issubset(product["evidence_ids"]):
                add("errors", "eligibility_citation_scope", rule_path, "投保條件引用必須列於商品的 evidence_ids。", product_id)

    def check_ids(values, target, path, record_id):
        for value in values:
            if value not in target:
                add("errors", "unknown_reference", path, f"引用的 ID 不存在：{value}", record_id)

    for collection, id_field in COLLECTION_IDS.items():
        for position, record in enumerate(dataset[collection]):
            record_id = record[id_field]
            path = f"/{collection}/{position}"
            check_ids(record.get("product_ids", []), indexes["products"], f"{path}/product_ids", record_id)
            if collection == "rates":
                check_ids([record["product_id"]], indexes["products"], f"{path}/product_id", record_id)
            check_ids(record.get("company_ids", []), company_names, f"{path}/company_ids", record_id)
            check_ids(record.get("evidence_ids", []), indexes["evidence"], f"{path}/evidence_ids", record_id)
            if collection == "products":
                for rule_position, rule in enumerate(record["eligibility_rules"]):
                    check_ids(rule["evidence_ids"], indexes["evidence"], f"{path}/eligibility_rules/{rule_position}/evidence_ids", record_id)
            if collection == "news":
                for claim_position, claim in enumerate(record["summary_points"]):
                    claim_path = f"{path}/summary_points/{claim_position}/evidence_ids"
                    check_ids(claim["evidence_ids"], indexes["evidence"], claim_path, record_id)
                    if not set(claim["evidence_ids"]).issubset(record["evidence_ids"]):
                        add("errors", "summary_citation_scope", claim_path, "摘要引用必須列於新聞的 evidence_ids。", record_id)
            related_products = set(record.get("product_ids", []))
            if collection == "products":
                related_products = {record["product_id"]}
            elif collection == "rates":
                related_products = {record["product_id"]}
            for evidence_id in record.get("evidence_ids", []):
                cited = indexes["evidence"].get(evidence_id)
                if cited and related_products and cited["product_ids"] and related_products.isdisjoint(cited["product_ids"]):
                    add("errors", "evidence_scope_mismatch", f"{path}/evidence_ids", "引用證據隸屬其他商品，請確認關聯。", record_id)

    rate_keys = set()
    for position, rate in enumerate(dataset["rates"]):
        path = f"/rates/{position}"
        record_id = rate["rate_id"]
        rate_key = (rate["product_id"], rate["period_month"], rate["applicable_conditions"])
        if rate_key in rate_keys:
            add("errors", "duplicate_rate", path, "同商品、月份與適用條件不可有重複利率紀錄。", record_id)
        rate_keys.add(rate_key)
        if rate["rate_percent"] is None:
            add("warnings", "rate_missing_value", f"{path}/rate_percent", "宣告利率缺值，保留 null，不補零或沿用其他月份。", record_id)
        elif not math.isfinite(rate["rate_percent"]):
            add("errors", "non_finite_rate", f"{path}/rate_percent", "利率必須是有限數值。", record_id)
        start, end = rate["effective_from"], rate["effective_to"]
        if start and end and start > end:
            add("errors", "date_order", path, "利率生效迄日不可早於起日。", record_id)
        year, month = map(int, rate["period_month"].split("-"))
        if year == 0:
            add("errors", "invalid_month", f"{path}/period_month", "年份必須大於零。", record_id)
            continue
        month_start = date(year, month, 1).isoformat()
        month_end = date(year, month, monthrange(year, month)[1]).isoformat()
        if (start and start > month_end) or (end and end < month_start):
            add("errors", "rate_period_mismatch", path, "生效期間與宣告利率月份沒有交集。", record_id)
        if not start or not rate["evidence_ids"]:
            add("warnings", "rate_provenance_incomplete", path, "利率的生效起日或來源證據待補。", record_id)

    def check_review(record, status_field, by_field, time_field, path, record_id):
        reviewed = record[status_field] != "unreviewed"
        has_metadata = bool(record[by_field] and record[time_field])
        if (reviewed and not has_metadata) or (not reviewed and (record[by_field] is not None or record[time_field] is not None)):
            add("errors", "review_metadata", path, "複核狀態與複核者、複核時間不一致。", record_id)

    for position, evidence in enumerate(dataset["evidence"]):
        path = f"/evidence/{position}"
        record_id = evidence["evidence_id"]
        if (evidence["source_type"] == "mock") != mock_mode:
            add("errors", "mock_evidence", f"{path}/source_type", "證據來源類型必須與快照模式一致。", record_id)
        local_path = evidence["local_path"]
        if local_path:
            normalized = local_path.replace("\\", "/")
            if normalized.startswith("/") or ":" in normalized or ".." in PurePosixPath(normalized).parts:
                add("errors", "unsafe_local_path", f"{path}/local_path", "證據路徑必須是無上層跳脫的專案相對路徑。", record_id)
            elif mock_mode != normalized.startswith("data/mock/"):
                add("errors", "evidence_path_mode", f"{path}/local_path", "Mock 證據檔案與真實資料路徑必須分離。", record_id)
        gaps = _evidence_gaps(evidence)
        if gaps:
            add("warnings", "evidence_incomplete", path, "證據待補欄位：" + ", ".join(gaps), record_id)
        check_review(evidence, "human_review_status", "reviewed_by", "reviewed_at", path, record_id)

    for position, news in enumerate(dataset["news"]):
        if news["published_at"] is None:
            add("warnings", "news_date_missing", f"/news/{position}/published_at", "新聞日期缺漏，日期篩選時不視為符合。", news["news_id"])

    for collection, date_fields, time_fields in (
        ("evidence", ("source_date",), ("captured_at", "reviewed_at")),
        ("news", (), ("published_at",)),
        ("insights", (), ("conclusion_reviewed_at",)),
    ):
        for position, record in enumerate(dataset[collection]):
            for field in date_fields + time_fields:
                value = record[field]
                if value is None:
                    continue
                future = date.fromisoformat(value) > snapshot_date if field in date_fields else datetime.fromisoformat(value.upper()) > snapshot_time
                if future:
                    add("errors", "future_source", f"/{collection}/{position}/{field}", "來源或複核時間不得晚於快照基準時間。", record[COLLECTION_IDS[collection]])

    for position, insight in enumerate(dataset["insights"]):
        path = f"/insights/{position}"
        record_id = insight["insight_id"]
        check_review(insight, "conclusion_review_status", "conclusion_reviewed_by", "conclusion_reviewed_at", path, record_id)
        status = validate_insight_evidence(insight, dataset)
        if not status["evidence_complete"]:
            add("warnings", "insight_evidence_incomplete", path, "觀察的引用缺漏或證據不完整，不可顯示為已驗證結論。", record_id)
        if insight["conclusion_review_status"] == "unreviewed":
            add("warnings", "conclusion_unreviewed", path, "結論尚未人工複核；證據複核不會自動核准結論。", record_id)
    return report
