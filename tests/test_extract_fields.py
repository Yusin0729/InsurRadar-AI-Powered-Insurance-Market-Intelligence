"""LLM 結構化抽取測試；以假的模型回應執行，不呼叫 Gemini API。

文件段落為測試用文字，不代表真實保單內容。
執行：python -B -m unittest discover -s tests -p test_extract_fields.py -v
外部套件：無（不載入 google-genai）。
"""

import json
import unittest

from tools import extract_fields as ef

DOC = {
    "doc_id": "doc_test", "sha256": "0" * 64, "source_file": "data/real/docs/test.pdf", "source_url": None,
    "paragraphs": [
        {"paragraph_id": "doc_test_p001_001", "page": 1, "index": 1, "article": None,
         "text": "本保險為外幣保險單，本公司所收付之款項均以美元計價。"},
        {"paragraph_id": "doc_test_p002_001", "page": 2, "index": 1, "article": "第五條",
         "text": "繳費年期／投保年齡：2年期/6年期 0歲~74歲、8年期 0歲~72歲 最低保額 1萬美元"},
        {"paragraph_id": "doc_test_p002_002", "page": 2, "index": 2, "article": "第五條",
         "text": "增值回饋分享金：6年及8年期繳費者為2.25％，2年期為1.5%"},
    ],
}
PRODUCT = {"product_id": "prd_test", "product_name": "測試美元利率變動型終身壽險"}


def item(field, value, pid, quote, term=None):
    return {"field": field, "term_years": term, "value": value, "paragraph_id": pid, "quote": quote}


def response(*items):
    return json.dumps({"items": list(items)}, ensure_ascii=False)


class ValidateTest(unittest.TestCase):
    def test_valid_items_are_accepted_with_page(self):
        raw = response(
            item("currency", "美元", "doc_test_p001_001", "均以美元計價"),
            item("payment_term_years", "6", "doc_test_p002_001", "2年期/6年期 0歲~74歲"),
            item("issue_age_range", "0-74", "doc_test_p002_001", "2年期/6年期 0歲~74歲", term=6),
            item("issue_age_range", "0-72", "doc_test_p002_001", "8年期 0歲~72歲", term=8),
            item("min_sum_assured", "1", "doc_test_p002_001", "最低保額 1萬美元"),
            item("predetermined_rate_percent", "2.25", "doc_test_p002_002", "6年及8年期繳費者為2.25％", term=6),
        )
        accepted, rejected = ef.validate_items(raw, DOC)
        self.assertEqual(rejected, [])
        values = {(a["field"], a["term_years"]): a["value"] for a in accepted}
        self.assertEqual(values[("issue_age_range", 6)], {"min": 0, "max": 74})
        self.assertEqual(values[("payment_term_years", 6)], 6)
        self.assertEqual(values[("predetermined_rate_percent", 6)], 2.25)  # 全形 ％ 也能比對
        self.assertEqual(values[("currency", None)], "美元")
        self.assertTrue(all(a["page"] in (1, 2) and a["status"] == "ok" for a in accepted))

    def test_hallucinations_are_rejected(self):
        raw = response(
            item("issue_age_range", "0-75", "doc_test_p002_001", "2年期/6年期 0歲~74歲", term=6),  # 數字不在引句
            item("currency", "美元", "doc_test_p001_001", "以美元計價之保單"),               # 引句被改寫
            item("currency", "美元", "doc_test_p999_001", "均以美元計價"),                   # 段落不存在
            item("guaranteed_return", "3", "doc_test_p001_001", "均以美元計價"),             # 未定義欄位
            item("issue_age_range", "0-74", "doc_test_p002_001", "0歲~74歲"),                # 缺年期
            item("predetermined_rate_percent", "2.5", "doc_test_p002_002", "繳費者為2.25％"),  # 2.5 ≠ 2.25
            {"field": "currency", "value": "美元", "paragraph_id": "doc_test_p001_001", "quote": "美元", "extra": 1},
        )
        accepted, rejected = ef.validate_items(raw, DOC)
        self.assertEqual(accepted, [])
        reasons = [r["reason"] for r in rejected]
        self.assertEqual(len(reasons), 7)
        self.assertIn("年齡數字沒有出現在引句中", reasons)
        self.assertIn("引句不是該段落的原文", reasons)
        self.assertTrue(any("段落編號不存在" in r for r in reasons))
        self.assertTrue(any("未定義的欄位" in r for r in reasons))
        self.assertIn("投保年齡需指定 term_years", reasons)
        self.assertIn("數字沒有出現在引句中", reasons)
        self.assertIn("欄位缺漏或包含未定義欄位", reasons)

    def test_conflicting_values_are_flagged_and_duplicates_removed(self):
        raw = response(
            item("predetermined_rate_percent", "2.25", "doc_test_p002_002", "2.25％", term=2),
            item("predetermined_rate_percent", "1.5", "doc_test_p002_002", "2年期為1.5%", term=2),
            item("currency", "美元", "doc_test_p001_001", "均以美元計價"),
            item("currency", "美元", "doc_test_p001_001", "以美元計價"),
        )
        accepted, _ = ef.validate_items(raw, DOC)
        rates = [a for a in accepted if a["field"] == "predetermined_rate_percent"]
        self.assertEqual({a["status"] for a in rates}, {"conflict"})
        self.assertEqual(len([a for a in accepted if a["field"] == "currency"]), 1)

    def test_malformed_response_raises(self):
        for raw in ("not json", "[]", '{"items": {}}', '{"items": [], "note": "x"}'):
            with self.assertRaises(ef.ExtractionError):
                ef.validate_items(raw, DOC)
        accepted, rejected = ef.validate_items("```json\n{\"items\": []}\n```", DOC)
        self.assertEqual((accepted, rejected), ([], []))


class ExtractTest(unittest.TestCase):
    def test_prompt_contains_paragraph_ids_and_result_is_marked_unreviewed(self):
        seen = {}

        def fake_generate(prompt):
            seen["prompt"] = prompt
            return response(item("currency", "美元", "doc_test_p001_001", "均以美元計價"))

        result = ef.extract(DOC, PRODUCT, generate=fake_generate, model="fake-model")
        self.assertIn("[doc_test_p002_001]（第 2 頁）", seen["prompt"])
        self.assertIn(PRODUCT["product_name"], seen["prompt"])
        self.assertIn("assumed_declared_rate_percent", seen["prompt"])
        self.assertEqual(result["generation_mode"], "llm")
        self.assertEqual(result["review_status"], "unreviewed")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(result["prompt_sha256"]), 64)

    def test_long_documents_send_only_keyword_paragraphs(self):
        doc = {"paragraphs": [{"paragraph_id": f"p{i}", "page": 1, "text": "無關內容" * 50} for i in range(10)]
                             + [{"paragraph_id": "hit", "page": 2, "text": "投保年齡 0歲~74歲"}]}
        picked = ef.select_paragraphs(doc, limit=500)
        self.assertEqual([p["paragraph_id"] for p in picked], ["hit"])
        self.assertEqual(len(ef.select_paragraphs(doc, limit=10**6)), 11)

    def test_main_without_api_key_does_nothing(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            doc_path = Path(folder) / "doc.json"
            ds_path = Path(folder) / "ds.json"
            out = Path(folder) / "out"
            doc_path.write_text(json.dumps(DOC, ensure_ascii=False), encoding="utf-8")
            ds_path.write_text(json.dumps({"products": [PRODUCT]}, ensure_ascii=False), encoding="utf-8")
            with mock.patch("services.llm_service.get_gemini_config", return_value={"api_key": None, "model": "m"}):
                code = ef.main([str(doc_path), "--product-id", "prd_test", "--dataset", str(ds_path),
                                "--output-dir", str(out)])
            self.assertEqual(code, 1)
            self.assertFalse(out.exists())

    def test_main_all_runs_every_job_with_placeholder_key_rejected(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder:
            doc_path = Path(folder) / "doc.json"
            ds_path = Path(folder) / "ds.json"
            out = Path(folder) / "out"
            doc_path.write_text(json.dumps(DOC, ensure_ascii=False), encoding="utf-8")
            ds_path.write_text(json.dumps({"products": [PRODUCT]}, ensure_ascii=False), encoding="utf-8")
            argv = ["--all", "--dataset", str(ds_path), "--output-dir", str(out)]
            fake = lambda key, model: (lambda prompt: response(item("currency", "美元", "doc_test_p001_001", "均以美元計價")))
            with mock.patch.object(ef, "JOBS", [(str(doc_path), "prd_test")]),                     mock.patch.object(ef, "gemini_generate", fake):
                with mock.patch("services.llm_service.get_gemini_config",
                                return_value={"api_key": "請貼上你的金鑰", "model": "m"}):
                    self.assertEqual(ef.main(argv), 1)
                self.assertFalse(out.exists())
                with mock.patch("services.llm_service.get_gemini_config",
                                return_value={"api_key": "test-key", "model": "m"}):
                    self.assertEqual(ef.main(argv), 0)
            data = json.loads((out / "doc_test.json").read_text(encoding="utf-8"))
            self.assertEqual(data["items"][0]["value"], "美元")


if __name__ == "__main__":
    unittest.main()
