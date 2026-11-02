"""把 B 組維護的 CSV（試算表各分頁匯出）轉成 InsurRadar 真實快照 dataset.json，並用專案驗證規則檢查。

用法（在專案根目錄）：
    python -m tools.build_real_dataset --dataset-id real_2026_11_v1 --as-of 2026-11-03T09:00:00+08:00

預設讀取 data/real/csv/*.csv，輸出 data/real/dataset.json；驗證有錯誤時不寫檔並列出問題。
欄位格式見 templates/real_csv/ 與 docs/C_DATA_GUIDE.md。清單欄位用半形分號「;」分隔；空白儲存格視為 null。

外部套件：jsonschema（透過 services.validation）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from services.data_service import load_schema
from services.validation import validate_dataset

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "real" / "csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "real" / "dataset.json"


class SheetError(ValueError):
    pass


def read_sheet(folder: Path, name: str, required: bool = True) -> list[dict[str, str]]:
    path = folder / f"{name}.csv"
    if not path.exists():
        if required:
            raise SheetError(f"找不到 {path}")
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]
    return [row for row in rows if any(row.values())]  # 略過整列空白


def text(row, key):
    value = row.get(key, "")
    return value if value else None


def ids(row, key):
    return list(dict.fromkeys(part.strip() for part in row.get(key, "").split(";") if part.strip()))


def integer(row, key, sheet):
    value = row.get(key, "")
    if not value:
        return None
    try:
        return int(float(value)) if float(value).is_integer() else _bad(sheet, row, key, value)
    except ValueError:
        return _bad(sheet, row, key, value)


def number(row, key, sheet):
    value = row.get(key, "").replace("%", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return _bad(sheet, row, key, value)


def _bad(sheet, row, key, value):
    first = next(iter(row.values()), "")
    raise SheetError(f"{sheet}.csv（{first}）欄位 {key} 不是數字：{value!r}")


def build(folder: Path, dataset_id: str, as_of: str) -> dict:
    evidence = [{
        "evidence_id": r["evidence_id"], "is_mock": False,
        "source_type": r["source_type"], "source_title": r["source_title"],
        "source_url": text(r, "source_url"), "local_path": text(r, "local_path"),
        "source_date": text(r, "source_date"), "captured_at": text(r, "captured_at"),
        "document_version": text(r, "document_version"), "page": integer(r, "page", "evidence"),
        "section": text(r, "section"), "excerpt": text(r, "excerpt"),
        "product_ids": ids(r, "product_ids"),
        "human_review_status": r.get("human_review_status") or "unreviewed",
        "reviewed_by": text(r, "reviewed_by"), "reviewed_at": text(r, "reviewed_at"),
    } for r in read_sheet(folder, "evidence")]

    rules: dict[str, list] = {}
    for r in read_sheet(folder, "eligibility"):
        rules.setdefault(r["product_id"], []).append({
            "payment_term_years": integer(r, "payment_term_years", "eligibility"),
            "min_issue_age": integer(r, "min_issue_age", "eligibility"),
            "max_issue_age": integer(r, "max_issue_age", "eligibility"),
            "conditions": text(r, "conditions"),
            "evidence_ids": ids(r, "evidence_ids"),
        })

    products = []
    for r in read_sheet(folder, "products"):
        product_rules = rules.pop(r["product_id"], [])
        # 投保規則引用的證據必須同時列在商品層級，這裡自動合併，避免人工漏填
        evidence_ids = ids(r, "evidence_ids")
        for rule in product_rules:
            evidence_ids += [e for e in rule["evidence_ids"] if e not in evidence_ids]
        products.append({
            "product_id": r["product_id"], "is_mock": False,
            "company_id": r["company_id"], "company_name": r["company_name"],
            "product_name": r["product_name"],
            "product_type": "interest_sensitive_whole_life", "currency": "USD",
            "eligibility_rules": product_rules,
            "benefits": ids(r, "benefits"), "channels": ids(r, "channels"),
            "launched_on": text(r, "launched_on"), "discontinued_on": text(r, "discontinued_on"),
            "evidence_ids": evidence_ids,
        })
    if rules:
        raise SheetError(f"eligibility.csv 有不存在於 products.csv 的商品：{', '.join(rules)}")

    rates = [{
        "rate_id": r["rate_id"], "is_mock": False, "product_id": r["product_id"],
        "rate_type": "declared", "rate_percent": number(r, "rate_percent", "rates"),
        "period_month": r["period_month"],
        "effective_from": text(r, "effective_from"), "effective_to": text(r, "effective_to"),
        "applicable_conditions": text(r, "applicable_conditions"),
        "evidence_ids": ids(r, "evidence_ids"),
    } for r in read_sheet(folder, "rates")]

    points: dict[str, list] = {}
    for r in read_sheet(folder, "news_points", required=False):
        points.setdefault(r["news_id"], []).append({"text": r["text"], "evidence_ids": ids(r, "evidence_ids")})
    news = []
    for r in read_sheet(folder, "news", required=False):
        summary = points.pop(r["news_id"], [])
        evidence_ids = ids(r, "evidence_ids")
        for point in summary:
            evidence_ids += [e for e in point["evidence_ids"] if e not in evidence_ids]
        news.append({
            "news_id": r["news_id"], "is_mock": False, "title": r["title"],
            "publisher": text(r, "publisher"), "published_at": text(r, "published_at"),
            "source_url": text(r, "source_url"), "category": r["category"],
            "company_ids": ids(r, "company_ids"), "product_ids": ids(r, "product_ids"),
            "summary_points": summary, "generation_mode": r.get("generation_mode") or "human",
            "evidence_ids": evidence_ids,
        })
    if points:
        raise SheetError(f"news_points.csv 有不存在於 news.csv 的新聞：{', '.join(points)}")

    insights = [{
        "insight_id": r["insight_id"], "is_mock": False, "title": r["title"],
        "statement": r["statement"], "product_ids": ids(r, "product_ids"),
        "evidence_ids": ids(r, "evidence_ids"),
        "generation_mode": r.get("generation_mode") or "human",
        "conclusion_review_status": r.get("conclusion_review_status") or "unreviewed",
        "conclusion_reviewed_by": text(r, "conclusion_reviewed_by"),
        "conclusion_reviewed_at": text(r, "conclusion_reviewed_at"),
    } for r in read_sheet(folder, "insights", required=False)]

    investigations = [{
        "investigation_id": r["investigation_id"], "is_mock": False, "title": r["title"],
        "reason": r["reason"], "priority": r.get("priority") or "medium",
        "status": r.get("status") or "open", "product_ids": ids(r, "product_ids"),
        "evidence_ids": ids(r, "evidence_ids"), "missing_fields": ids(r, "missing_fields"),
        "suggested_action": r["suggested_action"],
    } for r in read_sheet(folder, "investigations", required=False)]

    return {
        "schema_version": "1.0.0", "dataset_id": dataset_id, "data_mode": "real",
        "mock_notice": None, "as_of": as_of,
        "products": products, "rates": rates, "news": news, "insights": insights,
        "evidence": evidence, "investigations": investigations,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CSV → InsurRadar 真實快照")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dataset-id", required=True, help="不可用 mock_ 開頭；資料內容改變就換新 ID")
    parser.add_argument("--as-of", required=True, help="快照基準時間，例如 2026-11-03T09:00:00+08:00")
    args = parser.parse_args(argv)

    try:
        dataset = build(args.input, args.dataset_id, args.as_of)
    except (SheetError, KeyError) as err:
        print(f"[轉換失敗] {err}（KeyError 表示 CSV 缺少必要欄位標題）", file=sys.stderr)
        return 1

    report = validate_dataset(dataset, load_schema())
    counts = ", ".join(f"{k}={len(dataset[k])}" for k in ("products", "rates", "news", "insights", "evidence", "investigations"))
    print(f"筆數：{counts}")
    for item in report["warnings"]:
        print(f"[提示] {item['code']} {item['path']} {item.get('record_id') or ''}：{item['message']}")
    if report["errors"]:
        for item in report["errors"]:
            print(f"[錯誤] {item['code']} {item['path']} {item.get('record_id') or ''}：{item['message']}", file=sys.stderr)
        print(f"驗證失敗，共 {len(report['errors'])} 項錯誤，未寫出檔案。", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"驗證通過（{len(report['warnings'])} 項提示），已寫出 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
