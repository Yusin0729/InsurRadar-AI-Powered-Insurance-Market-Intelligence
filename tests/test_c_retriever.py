"""C 組 HybridRetriever 與 CSV 轉換工具測試；不呼叫真實 Gemini API。

執行：python -B -m unittest discover -s tests -p test_c_retriever.py -v
"""

import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from services.c_retriever import HybridRetriever, split_excerpt
from services.data_service import load_dataset, load_schema
from services.rag_service import validate_retrieved_items
from services.validation import validate_dataset
from tools.build_real_dataset import build


def fake_embedder(texts, task_type):
    """以字元雜湊成 64 維向量的假嵌入，結果固定、可重現。"""
    vectors = []
    for text in texts:
        vec = np.zeros(64, dtype=np.float32)
        for ch in text:
            vec[ord(ch) % 64] += 1.0
        vectors.append(vec.tolist())
    return vectors


def broken_embedder(texts, task_type):
    raise RuntimeError("模擬網路中斷")


class SplitExcerptTest(unittest.TestCase):
    def test_chunks_are_contiguous_substrings(self):
        text = "第一條 本契約之宣告利率每月公告。" * 30
        chunks = split_excerpt(text, size=80, overlap=20)
        self.assertGreater(len(chunks), 1)
        for piece in chunks:
            self.assertIn(piece, text)
            self.assertLessEqual(len(piece), 80)

    def test_short_and_empty(self):
        self.assertEqual(split_excerpt("短句"), ["短句"])
        self.assertEqual(split_excerpt("   "), [])


class HybridRetrieverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset()  # Mock 快照

    def make(self, embedder=fake_embedder, **kwargs):
        return HybridRetriever(self.dataset, embedder=embedder, index_dir=None, **kwargs)

    def test_results_pass_rag_contract_check(self):
        retriever = self.make(min_score=0.0)
        hits = retriever.search("宣告利率", top_k=3)
        self.assertEqual(retriever.last_mode, "hybrid")
        self.assertTrue(hits)
        self.assertLessEqual(len(hits), 3)
        accepted = validate_retrieved_items(hits, self.dataset, 3)
        self.assertEqual([h["evidence_id"] for h in hits], [a["evidence_id"] for a in accepted])
        for hit in hits:
            self.assertIsInstance(hit["score"], float)
            self.assertTrue(0.0 <= hit["score"] <= 1.0)
            self.assertEqual(hit["dataset_id"], self.dataset["dataset_id"])

    def test_no_duplicate_evidence_ids(self):
        hits = self.make(min_score=0.0).search("終身壽險 身故保障", top_k=10)
        ids = [h["evidence_id"] for h in hits]
        self.assertEqual(len(ids), len(set(ids)))

    def test_falls_back_to_keyword_when_embedding_fails(self):
        retriever = self.make(embedder=broken_embedder)
        hits = retriever.search("宣告利率", top_k=3)
        self.assertEqual(retriever.last_mode, "keyword")
        self.assertIsNotNone(retriever.last_error)
        self.assertEqual(len(validate_retrieved_items(hits, self.dataset, 3)), len(hits))

    def test_keyword_mode_without_embedder(self):
        with mock.patch.dict(os.environ, {"INSURRADAR_OFFLINE": "1"}):
            retriever = HybridRetriever(self.dataset, index_dir=None)
        self.assertIsNone(retriever.matrix)
        hits = retriever.search("宣告利率", top_k=2)
        self.assertEqual(retriever.last_mode, "keyword")
        self.assertLessEqual(len(hits), 2)

    def test_invalid_inputs(self):
        retriever = self.make()
        self.assertEqual(retriever.search("   ", top_k=3), [])
        for bad in (0, -1, 1.5, "3", True):
            with self.assertRaises(ValueError):
                retriever.search("利率", top_k=bad)

    def test_index_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            first = HybridRetriever(self.dataset, embedder=fake_embedder, index_dir=Path(folder))
            self.assertIsNone(first.matrix)  # 建構時不呼叫嵌入服務
            first.build_index()
            self.assertTrue(first._cache_path().exists())
            second = HybridRetriever(self.dataset, embedder=broken_embedder, index_dir=Path(folder))
            second.build_index()  # 讀快取，不需重新嵌入文件
            self.assertIsNotNone(second.matrix)
            np.testing.assert_allclose(first.matrix, second.matrix)


class BuildRealDatasetTest(unittest.TestCase):
    """以臨時 CSV 測試轉換流程；內容為測試用假資料，不寫入專案。"""

    SHEETS = {
        "evidence": [{
            "evidence_id": "ev_t001", "source_type": "pdf", "source_title": "測試條款",
            "source_url": "https://example.com/t.pdf", "local_path": "", "source_date": "2026-10-01",
            "captured_at": "2026-10-20T10:00:00+08:00", "document_version": "v1", "page": "3",
            "section": "第五條", "excerpt": "本商品繳費年期為六年，投保年齡 0 至 70 歲。",
            "product_ids": "prd_t001", "human_review_status": "unreviewed", "reviewed_by": "", "reviewed_at": "",
        }],
        "products": [{
            "product_id": "prd_t001", "company_id": "cmp_t001", "company_name": "測試人壽",
            "product_name": "測試美元終身壽險", "benefits": "身故保險金;完全失能保險金",
            "channels": "銀行保險", "launched_on": "2026-01-01", "discontinued_on": "", "evidence_ids": "",
        }],
        "eligibility": [{
            "product_id": "prd_t001", "payment_term_years": "6", "min_issue_age": "0",
            "max_issue_age": "70", "conditions": "", "evidence_ids": "ev_t001",
        }],
        "rates": [{
            "rate_id": "rate_t001", "product_id": "prd_t001", "rate_percent": "4.1%",
            "period_month": "2026-10", "effective_from": "2026-10-01", "effective_to": "2026-10-31",
            "applicable_conditions": "", "evidence_ids": "ev_t001",
        }],
    }

    def write(self, folder):
        for name, rows in self.SHEETS.items():
            with (folder / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

    def test_build_passes_project_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write(Path(folder))
            dataset = build(Path(folder), "test_dataset_v1", "2026-11-02T09:00:00+08:00")
        report = validate_dataset(dataset, load_schema())
        self.assertEqual(report["errors"], [])
        product = dataset["products"][0]
        self.assertEqual(product["evidence_ids"], ["ev_t001"])  # 投保規則引用已自動併入商品
        self.assertEqual(product["benefits"], ["身故保險金", "完全失能保險金"])
        self.assertEqual(dataset["rates"][0]["rate_percent"], 4.1)
        self.assertEqual(dataset["evidence"][0]["page"], 3)

    def test_retriever_on_real_mode_dataset(self):
        with tempfile.TemporaryDirectory() as folder:
            self.write(Path(folder))
            dataset = build(Path(folder), "test_dataset_v1", "2026-11-02T09:00:00+08:00")
        retriever = HybridRetriever(dataset, embedder=fake_embedder, index_dir=None, min_score=0.0)
        hits = retriever.search("繳費年期", top_k=3)
        self.assertEqual(len(validate_retrieved_items(hits, dataset, 3)), len(hits))
        self.assertEqual(hits[0]["evidence_id"], "ev_t001")


if __name__ == "__main__":
    unittest.main()
