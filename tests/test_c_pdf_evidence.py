"""C 組建向量庫測試：PDF 段落轉證據、索引與嵌入重試；不呼叫真實 Gemini API。

執行：python -B -m unittest discover -s tests -p test_c_pdf_evidence.py -v
"""

import csv
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from services.c_retriever import HybridRetriever, call_with_retry, is_transient_error
from services.data_service import load_schema
from services.rag_service import validate_retrieved_items
from services.validation import validate_dataset
from tools import add_pdf_evidence
from tools.add_pdf_evidence import check_source_file, merge_pdf_evidence, paragraph_evidence
from tools.build_real_dataset import build

LONG_TEXT = ("本公司於本契約有效期間內，被保險人身故者，按身故日之保單價值準備金給付身故保險金。" * 8).strip()

DOC = {
    "doc_id": "doc_t001",
    "title": "測試人壽 測試條款",
    "source_url": "https://example.com/t.pdf",
    "source_file": "data/real/docs/t001.pdf",
    "sha256": "0" * 64,
    "parsed_at": "2026-10-21T10:00:00+08:00",
    "paragraphs": [
        {"paragraph_id": "doc_t001_p001_001", "page": 1, "index": 1, "article": None, "text": "測試美元終身壽險"},
        {"paragraph_id": "doc_t001_p002_001", "page": 2, "index": 1, "article": "第六條",
         "text": "要保人於保險單送達的翌日起算十日內，得以書面檢同保險單向本公司撤銷本契約。"},
        {"paragraph_id": "doc_t001_p003_001", "page": 3, "index": 1, "article": "第十五條", "text": LONG_TEXT},
        {"paragraph_id": "doc_t001_p003_002", "page": 3, "index": 2, "article": "第十五條", "text": "   "},
    ],
}


SHEETS = {  # 測試用假資料，格式同 templates/real_csv/
    "evidence": {
        "evidence_id": "ev_t001", "source_type": "official_page", "source_title": "測試官網商品頁",
        "source_url": "https://example.com/p", "local_path": "", "source_date": "2026-10-01",
        "captured_at": "2026-10-20T10:00:00+08:00", "document_version": "", "page": "",
        "section": "商品特色", "excerpt": "本商品繳費年期為六年。",
        "product_ids": "prd_t001", "human_review_status": "unreviewed", "reviewed_by": "", "reviewed_at": "",
    },
    "products": {
        "product_id": "prd_t001", "company_id": "cmp_t001", "company_name": "測試人壽",
        "product_name": "測試美元終身壽險", "benefits": "身故保險金", "channels": "銀行保險",
        "launched_on": "2026-01-01", "discontinued_on": "", "evidence_ids": "ev_t001",
    },
    "eligibility": {
        "product_id": "prd_t001", "payment_term_years": "6", "min_issue_age": "0",
        "max_issue_age": "70", "conditions": "", "evidence_ids": "ev_t001",
    },
    "rates": {
        "rate_id": "rate_t001", "product_id": "prd_t001", "rate_percent": "4.1",
        "period_month": "2026-10", "effective_from": "2026-10-01", "effective_to": "2026-10-31",
        "applicable_conditions": "", "evidence_ids": "ev_t001",
    },
}


def fake_embedder(texts, task_type):
    """以字元雜湊成 64 維向量的假嵌入，結果固定、可重現。"""
    vectors = []
    for text in texts:
        vec = np.zeros(64, dtype=np.float32)
        for ch in text:
            vec[ord(ch) % 64] += 1.0
        vectors.append(vec.tolist())
    return vectors


def real_dataset():
    with tempfile.TemporaryDirectory() as folder:
        for name, row in SHEETS.items():
            with (Path(folder) / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
        return build(Path(folder), "test_dataset_v1", "2026-11-02T09:00:00+08:00")


class ParagraphEvidenceTest(unittest.TestCase):
    def test_fields_are_copied_verbatim(self):
        records = paragraph_evidence(DOC, ["prd_t001"], "v1")
        self.assertEqual(len(records), 3)  # 空白段落不收
        item = records[1]
        self.assertEqual(item["evidence_id"], "doc_t001_p002_001")
        self.assertEqual(item["excerpt"], DOC["paragraphs"][1]["text"])
        self.assertEqual((item["source_type"], item["page"], item["section"]), ("pdf", 2, "第六條"))
        self.assertEqual(item["source_url"], DOC["source_url"])
        self.assertEqual(item["local_path"], DOC["source_file"])
        self.assertEqual(item["captured_at"], DOC["parsed_at"])
        self.assertEqual(item["document_version"], "v1")
        self.assertEqual(item["product_ids"], ["prd_t001"])
        self.assertEqual(item["human_review_status"], "unreviewed")
        self.assertIsNone(item["reviewed_by"])

    def test_merge_is_idempotent_and_valid(self):
        dataset = real_dataset()
        once, counts = merge_pdf_evidence(dataset, [(DOC, ["prd_t001"], "v1")])
        twice, _ = merge_pdf_evidence(once, [(DOC, ["prd_t001"], "v1")])
        self.assertEqual(counts, {"doc_t001": 3})
        self.assertEqual(len(once["evidence"]), len(dataset["evidence"]) + 3)
        self.assertEqual(once, twice)
        self.assertEqual(len(dataset["evidence"]), 1)  # 不修改輸入
        self.assertEqual(validate_dataset(once, load_schema())["errors"], [])

    def test_rejects_unknown_product(self):
        with self.assertRaises(ValueError):
            merge_pdf_evidence(real_dataset(), [(DOC, ["prd_missing"], None)])

    def test_rejects_stale_parse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = root / DOC["source_file"]
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-test")
            with self.assertRaises(ValueError):
                check_source_file(DOC, root)
            check_source_file(dict(DOC, sha256=hashlib.sha256(b"%PDF-test").hexdigest()), root)


class PdfEvidenceRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.dataset, _ = merge_pdf_evidence(real_dataset(), [(DOC, ["prd_t001"], "v1")])

    def test_pdf_paragraphs_are_indexed_and_pass_rag_check(self):
        retriever = HybridRetriever(self.dataset, embedder=fake_embedder, index_dir=None, min_score=0.0)
        self.assertGreater(len([eid for eid, _ in retriever.chunks if eid == "doc_t001_p003_001"]), 1)  # 長段落切塊
        hits = retriever.search("撤銷本契約", top_k=3)
        self.assertEqual(retriever.last_mode, "hybrid")
        self.assertEqual(len(validate_retrieved_items(hits, self.dataset, 3)), len(hits))
        ids = [h["evidence_id"] for h in hits]
        self.assertIn("doc_t001_p002_001", ids)
        hit = hits[ids.index("doc_t001_p002_001")]
        self.assertEqual((hit["page"], hit["section"], hit["source_url"]), (2, "第六條", DOC["source_url"]))

    def test_keyword_fallback_finds_pdf_paragraph(self):
        with mock.patch.dict(os.environ, {"INSURRADAR_OFFLINE": "1"}):
            retriever = HybridRetriever(self.dataset, index_dir=None)
        self.assertIsNone(retriever.embedder)
        hits = retriever.search("撤銷本契約", top_k=3)
        self.assertEqual(retriever.last_mode, "keyword")
        self.assertEqual(hits[0]["evidence_id"], "doc_t001_p002_001")
        self.assertEqual(len(validate_retrieved_items(hits, self.dataset, 3)), len(hits))


class AddPdfEvidenceCliTest(unittest.TestCase):
    def run_cli(self, folder, dataset_id):
        args = [str(folder / "dataset.json"), "--dataset-id", dataset_id,
                "--as-of", "2026-11-05T09:00:00+08:00", "--backup", str(folder / "backup.json")]
        return add_pdf_evidence.main(args)

    def test_backup_new_id_and_rerun(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            original = real_dataset()
            (folder / "dataset.json").write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            doc_path = folder / "doc.json"
            doc_path.write_text(json.dumps(dict(DOC, source_file=None), ensure_ascii=False), encoding="utf-8")
            jobs = [(doc_path, ["prd_t001"], "v1")]
            with mock.patch.object(add_pdf_evidence, "JOBS", jobs):
                self.assertEqual(self.run_cli(folder, "test_dataset_v1"), 1)  # 內容變動卻沿用舊 ID
                self.assertFalse((folder / "backup.json").exists())
                self.assertEqual(self.run_cli(folder, "test_dataset_v2"), 0)
                saved = json.loads((folder / "dataset.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["dataset_id"], "test_dataset_v2")
                self.assertEqual(saved["as_of"], "2026-11-05T09:00:00+08:00")
                self.assertEqual(len(saved["evidence"]), 4)
                backup = json.loads((folder / "backup.json").read_text(encoding="utf-8"))
                self.assertEqual(backup, original)
                self.assertEqual(self.run_cli(folder, "test_dataset_v2"), 0)  # 重跑：內容相同不寫檔
                self.assertEqual(json.loads((folder / "dataset.json").read_text(encoding="utf-8")), saved)


class RetryTest(unittest.TestCase):
    class ApiError(Exception):
        def __init__(self, code):
            super().__init__(f"{code} error")
            self.code = code

    def test_retries_transient_then_succeeds(self):
        calls, waits = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise self.ApiError(503)
            return "ok"

        self.assertEqual(call_with_retry(flaky, attempts=5, sleep=waits.append), "ok")
        self.assertEqual(waits, [2.0, 4.0])

    def test_gives_up_and_skips_permanent_errors(self):
        waits = []
        with self.assertRaises(self.ApiError):
            call_with_retry(lambda: (_ for _ in ()).throw(self.ApiError(429)), attempts=3, sleep=waits.append)
        self.assertEqual(len(waits), 2)
        with self.assertRaises(self.ApiError):
            call_with_retry(lambda: (_ for _ in ()).throw(self.ApiError(400)), attempts=3, sleep=waits.append)
        self.assertEqual(len(waits), 2)  # 400 不重試

    def test_transient_detection(self):
        self.assertTrue(is_transient_error(RuntimeError("503 UNAVAILABLE")))
        self.assertTrue(is_transient_error(self.ApiError(429)))
        self.assertFalse(is_transient_error(ValueError("API key not valid")))


if __name__ == "__main__":
    unittest.main()
