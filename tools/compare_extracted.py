"""自動比對：把 LLM 抽取結果和 B 組人工表（已轉入 dataset.json）逐欄對照，列出要給 B 校對的格子。

用法（在專案根目錄）：
    python -m tools.compare_extracted

輸入：
- 抽取結果：data/real/processed/extracted/*.json（tools/extract_fields.py 的輸出）
- 人工表：data/real/dataset.json
    - 幣別、繳費年期、各年期投保年齡 → products[].currency / eligibility_rules
    - 最低投保金額、預定利率、DM 假設宣告利率 → 該商品證據（evidence）的原文
輸出：data/real/processed/compare/compare_report.json 與 compare_report.csv（Excel 可直接開）

比對狀態：
- 一致：兩邊數值相同。
- 不一致：兩邊都有，但數值不同 → B 優先校對。
- 抽取矛盾：同一文件抽出互相矛盾的值 → B 判斷哪個正確。
- 只有抽取：人工表沒有這格，抽取有 → B 確認後可補進人工表。
- 只有人工表：抽取沒有這格（可能是這份文件沒寫）→ B 確認來源文件。

本工具只讀不寫 dataset.json。外部套件：無（標準函式庫）。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_ROOT / "data" / "real" / "dataset.json"
EXTRACTED_DIR = PROJECT_ROOT / "data" / "real" / "processed" / "extracted"
OUTPUT_DIR = PROJECT_ROOT / "data" / "real" / "processed" / "compare"
TAIPEI = timezone(timedelta(hours=8))

FIELD_LABELS = {
    "currency": "幣別",
    "payment_term_years": "繳費年期",
    "issue_age_range": "投保年齡",
    "min_sum_assured": "最低投保金額（美元）",
    "predetermined_rate_percent": "預定利率（%）",
    "assumed_declared_rate_percent": "DM 假設宣告利率（%，非公告利率）",
}
CURRENCY_NAMES = {"USD": "美元", "TWD": "新台幣", "AUD": "澳幣", "CNY": "人民幣"}
STATUS_ORDER = ["不一致", "抽取矛盾", "只有抽取", "只有人工表", "一致"]
CSV_COLUMNS = ["商品", "product_id", "欄位", "年期", "狀態", "抽取值", "抽取頁碼", "抽取段落",
               "抽取引句", "人工值", "人工證據", "人工原文", "說明"]

_NUM = r"\d[\d,]*(?:\.\d+)?"


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return f"{_fmt(value['min'])}-{_fmt(value['max'])}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _single_term(text: str) -> int | None:
    """原文只提到一個繳費年期時回傳它，否則 None（代表適用全部年期）。"""
    terms = {int(t) for t in re.findall(r"(\d+)\s*年期", text)}
    return terms.pop() if len(terms) == 1 else None


# ---------------------------------------------------------------------------
# 人工表（dataset.json）
# ---------------------------------------------------------------------------
def manual_rows(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """把 dataset 中 B 組人工整理的值攤平成 (product_id, field, term_years, value, evidence)。"""
    evidence = {e["evidence_id"]: e for e in dataset.get("evidence", [])}
    rows: list[dict[str, Any]] = []

    def add(pid, field, term, value, eid):
        excerpt = (evidence.get(eid) or {}).get("excerpt") or ""
        rows.append({"product_id": pid, "field": field, "term_years": term, "value": value,
                     "evidence_id": eid, "excerpt": excerpt})

    for product in dataset.get("products", []):
        pid = product["product_id"]
        currency = product.get("currency")
        if currency:
            ids = product.get("evidence_ids") or [None]
            add(pid, "currency", None, CURRENCY_NAMES.get(currency, currency), ids[0])
        for rule in product.get("eligibility_rules") or []:
            term = rule.get("payment_term_years")
            if term is None:
                continue
            eid = (rule.get("evidence_ids") or [None])[0]
            add(pid, "payment_term_years", term, term, eid)
            if rule.get("min_issue_age") is not None and rule.get("max_issue_age") is not None:
                add(pid, "issue_age_range", term,
                    {"min": rule["min_issue_age"], "max": rule["max_issue_age"]}, eid)

        for eid in product.get("evidence_ids") or []:
            ev = evidence.get(eid)
            if not ev:
                continue
            text, section = ev.get("excerpt") or "", ev.get("section") or ""
            m = re.search(rf"最低(?:投保金額|保額|基本保額)[：:\s]*({_NUM})\s*(萬)?\s*美元", text)
            if m:
                usd = _num(m.group(1)) * (10000 if m.group(2) else 1)
                add(pid, "min_sum_assured", _single_term(text), usd, eid)
            rate = re.search(rf"({_NUM})\s*[%％]", text)
            if "假設宣告利率" in section and rate:
                add(pid, "assumed_declared_rate_percent", None, _num(rate.group(1)), eid)
            elif "預定利率" in section and rate:
                terms = sorted({int(t) for t in re.findall(r"(\d+)\s*年期", section)}) or [None]
                for term in terms:
                    add(pid, "predetermined_rate_percent", term, _num(rate.group(1)), eid)
    return rows


# ---------------------------------------------------------------------------
# 抽取結果
# ---------------------------------------------------------------------------
def to_usd(value: Any, quote: str) -> float:
    """抽取的最低投保金額照原文數字填寫（1萬美元寫 1），依引句單位換成美元。"""
    number = float(value)
    for m in re.finditer(rf"({_NUM})\s*(萬)?\s*美元", quote):
        if abs(_num(m.group(1)) - number) < 1e-9:
            return number * (10000 if m.group(2) else 1)
    return number


def extracted_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for result in results:
        for item in result.get("items", []):
            value = item["value"]
            if item["field"] == "min_sum_assured":
                value = to_usd(value, item.get("quote") or "")
            rows.append({"product_id": result["product_id"], "doc_id": result["doc_id"],
                         "field": item["field"], "term_years": item.get("term_years"), "value": value,
                         "page": item.get("page"), "paragraph_id": item.get("paragraph_id"),
                         "quote": item.get("quote"), "conflict": item.get("status") == "conflict"})
    return rows


# ---------------------------------------------------------------------------
# 比對
# ---------------------------------------------------------------------------
def _same(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.get("min") == b.get("min") and a.get("max") == b.get("max")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return str(a).strip() == str(b).strip()


def _terms_match(a: int | None, b: int | None) -> bool:
    return a is None or b is None or a == b


def compare(manual: list[dict[str, Any]], extracted: list[dict[str, Any]],
            product_names: dict[str, str]) -> list[dict[str, Any]]:
    """只比對有抽取結果的商品；每一列是一個要給 B 看的格子。"""
    covered = {r["product_id"] for r in extracted}
    manual = [m for m in manual if m["product_id"] in covered]
    used_manual: set[int] = set()
    report = []

    def row(pid, field, term, status, ext=None, man=None, note=""):
        return {"product_id": pid, "product_name": product_names.get(pid, pid), "field": field,
                "field_label": FIELD_LABELS.get(field, field), "term_years": term, "status": status,
                "extracted_value": _fmt(ext["value"]) if ext else "",
                "extracted_page": ext["page"] if ext else None,
                "extracted_paragraph_id": ext["paragraph_id"] if ext else None,
                "extracted_quote": ext["quote"] if ext else "", "extracted_doc_id": ext["doc_id"] if ext else None,
                "manual_value": _fmt(man["value"]) if man else "",
                "manual_evidence_id": man["evidence_id"] if man else None,
                "manual_excerpt": man["excerpt"] if man else "", "note": note}

    for ext in extracted:
        candidates = [i for i, m in enumerate(manual)
                      if m["product_id"] == ext["product_id"] and m["field"] == ext["field"]
                      and _terms_match(m["term_years"], ext["term_years"])]
        # 年期完全相同的優先
        candidates.sort(key=lambda i: manual[i]["term_years"] != ext["term_years"])
        term = ext["term_years"] if ext["term_years"] is not None else (
            manual[candidates[0]]["term_years"] if candidates else None)
        if not candidates:
            status = "抽取矛盾" if ext["conflict"] else "只有抽取"
            note = "同一文件抽出不同的值，請判斷哪個正確" if ext["conflict"] else "人工表沒有這格，確認後可補進人工表"
            report.append(row(ext["product_id"], ext["field"], term, status, ext=ext, note=note))
            continue
        match = next((i for i in candidates if _same(manual[i]["value"], ext["value"])), None)
        if ext["conflict"]:
            status, note, chosen = "抽取矛盾", "同一文件抽出不同的值，請判斷哪個正確", match if match is not None else candidates[0]
        elif match is not None:
            status, note, chosen = "一致", "", match
        else:
            status, note, chosen = "不一致", "兩邊數值不同，請對照原文校對", candidates[0]
        used_manual.add(chosen)
        report.append(row(ext["product_id"], ext["field"], term, status, ext=ext, man=manual[chosen], note=note))

    for i, man in enumerate(manual):
        if i in used_manual:
            continue
        report.append(row(man["product_id"], man["field"], man["term_years"], "只有人工表", man=man,
                          note="抽取結果沒有這格，可能是抽取的文件沒寫，請確認來源文件"))

    report.sort(key=lambda r: (STATUS_ORDER.index(r["status"]), r["product_id"], r["field"],
                               -1 if r["term_years"] is None else r["term_years"]))
    return report


def load_extracted(folder: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]


def write_outputs(report: list[dict[str, Any]], summary: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = output_dir / "compare_report.json", output_dir / "compare_report.csv"
    json_path.write_text(json.dumps({**summary, "rows": report}, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig：Excel 開啟不亂碼
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for r in report:
            writer.writerow([r["product_name"], r["product_id"], r["field_label"], _fmt(r["term_years"]),
                             r["status"], r["extracted_value"], _fmt(r["extracted_page"]),
                             r["extracted_paragraph_id"] or "", r["extracted_quote"], r["manual_value"],
                             r["manual_evidence_id"] or "", r["manual_excerpt"], r["note"]])
    return json_path, csv_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="比對 LLM 抽取結果與 B 組人工表")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--extracted-dir", type=Path, default=EXTRACTED_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    resolve = lambda p: p if p.is_absolute() else PROJECT_ROOT / p

    results = load_extracted(resolve(args.extracted_dir))
    if not results:
        print("[失敗] 沒有抽取結果，請先執行 tools/extract_fields.py", file=sys.stderr)
        return 1
    dataset = json.loads(resolve(args.dataset).read_text(encoding="utf-8"))
    names = {p["product_id"]: p["product_name"] for p in dataset.get("products", [])}
    report = compare(manual_rows(dataset), extracted_rows(results), names)

    counts = {s: sum(r["status"] == s for r in report) for s in STATUS_ORDER}
    skipped = sorted(set(names) - {r["product_id"] for r in results})
    summary = {"generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
               "dataset_id": dataset.get("dataset_id"),
               "extracted_docs": [r["doc_id"] for r in results], "counts": counts,
               "products_without_extraction": skipped}
    json_path, csv_path = write_outputs(report, summary, resolve(args.output_dir))

    print("[完成] " + "、".join(f"{s} {n}" for s, n in counts.items()) + f" → {csv_path}")
    for r in report:
        if r["status"] == "一致":
            continue
        term = "" if r["term_years"] is None else f"（{r['term_years']} 年期）"
        print(f"  [{r['status']}] {r['product_id']} {r['field_label']}{term}："
              f"抽取 {r['extracted_value'] or '—'} / 人工 {r['manual_value'] or '—'}")
    if skipped:
        print(f"  沒有抽取文件、未比對的商品：{'、'.join(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
