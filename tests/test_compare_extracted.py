"""抽取結果與人工表比對測試；資料為測試用，不代表真實保單內容。

執行：python -B -m unittest discover -s tests -p test_compare_extracted.py -v
外部套件：無。
"""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from tools import compare_extracted as ce

DATASET = {
    "dataset_id": "test",
    "products": [
        {"product_id": "prd_a", "product_name": "測試商品A", "currency": "USD",
         "eligibility_rules": [
             {"payment_term_years": 6, "min_issue_age": 0, "max_issue_age": 74, "evidence_ids": ["E1"]},
             {"payment_term_years": 8, "min_issue_age": 0, "max_issue_age": 72, "evidence_ids": ["E1"]}],
         "evidence_ids": ["E1", "E2", "E3"]},
        {"product_id": "prd_b", "product_name": "測試商品B", "currency": "USD",
         "eligibility_rules": [], "evidence_ids": []},
    ],
    "evidence": [
        {"evidence_id": "E1", "section": "承保條件",
         "excerpt": "2年期/6年期 0歲~74歲、8年期 0歲~72歲　最低保額 1萬美元"},
        {"evidence_id": "E2", "section": "預定利率（6年期、8年期）", "excerpt": "6年及8年期繳費者為2.25％"},
        {"evidence_id": "E3", "section": "DM 範例假設宣告利率", "excerpt": "宣告利率各年度均以4.35%為例"},
    ],
}


def ext(field, value, term=None, quote="q", status="ok"):
    return {"field": field, "term_years": term, "value": value, "paragraph_id": "p1",
            "page": 1, "quote": quote, "status": status}


RESULT = {"doc_id": "doc_a", "product_id": "prd_a", "items": [
    ext("currency", "美元"),
    ext("issue_age_range", {"min": 0, "max": 75}, term=6),          # 人工 0-74 → 不一致
    ext("min_sum_assured", 1.0, quote="最低保額 1萬美元"),           # 1萬 = 10000 → 一致
    ext("predetermined_rate_percent", 2.25),                        # 年期 None 可對到 6、8 年期
    ext("assumed_declared_rate_percent", 4.35, status="conflict"),  # 抽取矛盾
    ext("assumed_declared_rate_percent", 4.5, status="conflict"),
    ext("payment_term_years", 10, term=10),                         # 人工沒有 → 只有抽取
]}


class ManualRowsTest(unittest.TestCase):
    def test_manual_values_are_parsed_from_products_and_evidence(self):
        rows = {(r["field"], r["term_years"]): r["value"] for r in ce.manual_rows(DATASET)}
        self.assertEqual(rows[("currency", None)], "美元")
        self.assertEqual(rows[("issue_age_range", 8)], {"min": 0, "max": 72})
        self.assertEqual(rows[("min_sum_assured", None)], 10000)   # 原文提到多個年期 → 不限年期
        self.assertEqual(rows[("predetermined_rate_percent", 6)], 2.25)
        self.assertEqual(rows[("predetermined_rate_percent", 8)], 2.25)
        self.assertEqual(rows[("assumed_declared_rate_percent", None)], 4.35)

    def test_to_usd_uses_unit_in_quote(self):
        self.assertEqual(ce.to_usd(1.5, "10年期 0~70歲 1.5萬美元"), 15000)
        self.assertEqual(ce.to_usd(6000, "最低投保金額：6,000美元"), 6000)


class CompareTest(unittest.TestCase):
    def setUp(self):
        names = {p["product_id"]: p["product_name"] for p in DATASET["products"]}
        self.report = ce.compare(ce.manual_rows(DATASET), ce.extracted_rows([RESULT]), names)

    def find(self, field, term=None, status=None):
        return [r for r in self.report if r["field"] == field
                and (term is None or r["term_years"] == term) and (status is None or r["status"] == status)]

    def test_statuses(self):
        self.assertEqual(self.find("currency")[0]["status"], "一致")
        self.assertEqual(self.find("min_sum_assured")[0]["status"], "一致")
        mismatch = self.find("issue_age_range", 6)[0]
        self.assertEqual((mismatch["status"], mismatch["extracted_value"], mismatch["manual_value"]),
                         ("不一致", "0-75", "0-74"))
        self.assertEqual(len(self.find("assumed_declared_rate_percent", status="抽取矛盾")), 2)
        self.assertEqual(self.find("payment_term_years", 10)[0]["status"], "只有抽取")
        # 人工表的 8 年期投保年齡、繳費年期 6/8 抽取沒有 → 只有人工表
        self.assertEqual(self.find("issue_age_range", 8)[0]["status"], "只有人工表")

    def test_predetermined_rate_without_term_matches_one_manual_row(self):
        rows = self.find("predetermined_rate_percent")
        self.assertEqual(sorted(r["status"] for r in rows), ["一致", "只有人工表"])

    def test_products_without_extraction_are_not_listed(self):
        self.assertFalse([r for r in self.report if r["product_id"] == "prd_b"])

    def test_problems_are_listed_first(self):
        order = [ce.STATUS_ORDER.index(r["status"]) for r in self.report]
        self.assertEqual(order, sorted(order))


class MainTest(unittest.TestCase):
    def test_cli_writes_json_and_excel_friendly_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "ext").mkdir()
            (folder / "ext" / "doc_a.json").write_text(json.dumps(RESULT, ensure_ascii=False), encoding="utf-8")
            (folder / "ds.json").write_text(json.dumps(DATASET, ensure_ascii=False), encoding="utf-8")
            out = folder / "out"
            code = ce.main(["--dataset", str(folder / "ds.json"), "--extracted-dir", str(folder / "ext"),
                            "--output-dir", str(out)])
            self.assertEqual(code, 0)
            data = json.loads((out / "compare_report.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["不一致"], 1)
            self.assertEqual(data["products_without_extraction"], ["prd_b"])
            raw = (out / "compare_report.csv").read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            with (out / "compare_report.csv").open(encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], ce.CSV_COLUMNS)
            self.assertEqual(rows[1][4], "不一致")

    def test_no_extraction_results(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(ce.main(["--extracted-dir", folder, "--output-dir", str(Path(folder) / "o")]), 1)


if __name__ == "__main__":
    unittest.main()
