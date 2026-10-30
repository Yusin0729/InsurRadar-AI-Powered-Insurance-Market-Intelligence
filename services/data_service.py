"""集中讀取及唯讀篩選 InsurRadar 快照；透過 validation 使用外部套件 jsonschema。"""

from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
import re
import warnings

from jsonschema.exceptions import SchemaError

from .validation import validate_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_PATH = PROJECT_ROOT / "data" / "mock" / "dataset.json"
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "dataset.schema.json"


class DatasetLoadError(ValueError):
    """檔案不存在、JSON 格式錯誤或 Schema 定義無效。"""


class DatasetValidationError(DatasetLoadError):
    """快照未通過驗證；完整結果保留於 report。"""

    def __init__(self, report):
        self.report = report
        details = "; ".join(f"{item['path']}: {item['message']}" for item in report["errors"])
        super().__init__(f"資料快照驗證失敗：{details}")


class DatasetWarning(UserWarning):
    """合法缺值或待複核資訊，不阻止快照載入。"""


def _reject_constant(value):
    raise ValueError(f"JSON 不接受非有限數值：{value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 欄位重複：{key}")
        result[key] = value
    return result


def _read_json(path):
    try:
        with Path(path).open(encoding="utf-8-sig") as file:
            return json.load(file, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError) as error:
        raise DatasetLoadError(f"無法讀取 JSON 檔案 {path}：{error}") from error


def load_schema() -> dict:
    """取得專案 Schema，供驗證及後續 C 組轉接共用；不依賴目前工作目錄。"""
    return _read_json(SCHEMA_PATH)


def load_dataset(dataset_path: str | Path | None = None) -> dict:
    """載入並驗證快照；預設為 Mock，自訂路徑須明確傳入，不自動降級或補值。

    Schema／關聯錯誤會拋出 DatasetValidationError；合法缺值透過
    DatasetWarning 提醒。需要逐筆報告時，使用 validate_dataset(data, load_schema())。
    """
    path = DEFAULT_DATASET_PATH if dataset_path is None else Path(dataset_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    dataset = _read_json(path)
    schema = load_schema()
    try:
        report = validate_dataset(dataset, schema)
    except SchemaError as error:
        raise DatasetLoadError(f"專案 Schema 定義無效：{error.message}") from error
    if report["errors"]:
        raise DatasetValidationError(report)
    if report["warnings"]:
        warning_codes = ", ".join(sorted({item["code"] for item in report["warnings"]}))
        warnings.warn(
            f"快照 {dataset['dataset_id']} 有 {len(report['warnings'])} 項待補／複核提示：{warning_codes}",
            DatasetWarning,
            stacklevel=2,
        )
    return dataset


def _filter_ids(values, name):
    if values is None:
        return None
    if isinstance(values, str):
        raise TypeError(f"{name} 請傳入 ID 清單，不接受單一字串。")
    result = set(values)
    if not all(isinstance(value, str) and value.strip() for value in result):
        raise ValueError(f"{name} 必須包含非空白的字串 ID。")
    return result


def _matches_any(values, selected):
    return selected is None or bool(set(values) & selected)


def get_products(
    dataset: dict, *, company_ids=None, payment_term_years: int | None = None,
    issue_age: int | None = None, keyword: str | None = None,
) -> list[dict]:
    """依公司、同一條投保規則中的年期／年齡及文字篩選；僅代表初步條件。

    0 年代表躉繳。年齡任一上下限未知時，不視為符合；額外條件仍須
    由使用者查看，不能以此函式宣稱已通過保險公司核保。
    """
    companies = _filter_ids(company_ids, "company_ids")
    for field, value in (("payment_term_years", payment_term_years), ("issue_age", issue_age)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{field} 必須是非負整數或 None。")
    if keyword is not None and not isinstance(keyword, str):
        raise TypeError("keyword 必須是字串或 None。")
    term = (keyword or "").strip().casefold()
    result = []
    for product in dataset["products"]:
        if companies is not None and product["company_id"] not in companies:
            continue
        if payment_term_years is not None or issue_age is not None:
            matching_rule = any(
                (payment_term_years is None or rule["payment_term_years"] == payment_term_years)
                and (issue_age is None or (
                    rule["min_issue_age"] is not None and rule["max_issue_age"] is not None
                    and rule["min_issue_age"] <= issue_age <= rule["max_issue_age"]
                ))
                for rule in product["eligibility_rules"]
            )
            if not matching_rule:
                continue
        searchable = " ".join([
            product["product_id"], product["company_name"], product["product_name"],
            *product["benefits"], *product["channels"],
        ]).casefold()
        if term and term not in searchable:
            continue
        result.append(product)
    return deepcopy(result)


def get_declared_rates(dataset: dict, *, product_ids=None, period_month: str | None = None) -> list[dict]:
    """精確篩選宣告利率月份，保留缺值及所有適用條件，不排名、不沿用月份。"""
    products = _filter_ids(product_ids, "product_ids")
    if period_month is not None:
        if not isinstance(period_month, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period_month) or period_month.startswith("0000"):
            raise ValueError("period_month 必須是有效的 YYYY-MM。")
    return deepcopy([
        item for item in dataset["rates"]
        if item["rate_type"] == "declared"
        and (products is None or item["product_id"] in products)
        and (period_month is None or item["period_month"] == period_month)
    ])


def _date_bound(value, name):
    if value is None:
        return None
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return date.fromisoformat(value)
    raise ValueError(f"{name} 必須是 YYYY-MM-DD、date 或 None。")


def get_news(
    dataset: dict, *, category: str | None = None, company_ids=None,
    product_ids=None, date_from: str | date | None = None, date_to: str | date | None = None,
) -> list[dict]:
    """各篩選條件以 AND 組合，ID 清單內以 OR 比對；日期採快照時區、含起迄日。

    沒有日期篩選時保留日期未知的新聞；有日期篩選時排除，不補上今天日期。
    回傳順序與快照一致，不修改新聞或其摘要。
    """
    companies = _filter_ids(company_ids, "company_ids")
    products = _filter_ids(product_ids, "product_ids")
    if category is not None and category not in {"regulatory", "product", "interest_rate", "market"}:
        raise ValueError("不支援的新聞分類。")
    start, end = _date_bound(date_from, "date_from"), _date_bound(date_to, "date_to")
    if start and end and start > end:
        raise ValueError("date_from 不可晚於 date_to。")
    timezone = datetime.fromisoformat(dataset["as_of"].upper()).tzinfo
    result = []
    for news in dataset["news"]:
        if category is not None and news["category"] != category:
            continue
        if not _matches_any(news["company_ids"], companies) or not _matches_any(news["product_ids"], products):
            continue
        if start or end:
            if news["published_at"] is None:
                continue
            published = datetime.fromisoformat(news["published_at"].upper()).astimezone(timezone).date()
            if (start and published < start) or (end and published > end):
                continue
        result.append(news)
    return deepcopy(result)


def get_insights(dataset: dict, *, product_ids=None) -> list[dict]:
    """僅納入涉及商品全數位於指定範圍的觀察；不重算分析、不更新複核狀態。

    None 表示不篩選，空清單表示沒有商品；無商品關聯的全域觀察在指定
    商品篩選下不會被當成該範圍的分析。顯示前另用 validate_insight_evidence。
    """
    products = _filter_ids(product_ids, "product_ids")
    return deepcopy([
        item for item in dataset["insights"]
        if products is None or (
            bool(item["product_ids"]) and set(item["product_ids"]).issubset(products)
        )
    ])


def get_evidence(dataset: dict, evidence_id: str) -> dict | None:
    """只回傳指定證據的副本，不推測 ID、不讀取 local_path 或外部網址。"""
    for item in dataset["evidence"]:
        if item["evidence_id"] == evidence_id:
            return deepcopy(item)
    return None


def get_investigations(dataset: dict, *, product_ids=None, priority=None, status=None) -> list[dict]:
    """依商品交集、優先級及處理狀態篩選；不執行指派或寫回資料。"""
    products = _filter_ids(product_ids, "product_ids")
    if priority is not None and priority not in {"high", "medium", "low"}:
        raise ValueError("不支援的待查優先級。")
    if status is not None and status not in {"open", "in_review", "resolved"}:
        raise ValueError("不支援的待查狀態。")
    return deepcopy([
        item for item in dataset["investigations"]
        if _matches_any(item["product_ids"], products)
        and (priority is None or item["priority"] == priority)
        and (status is None or item["status"] == status)
    ])
