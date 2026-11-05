"""C 組檢索排序調整測試；資料為測試用，不代表真實保單內容，不呼叫任何 API。

執行：python -B -m unittest discover -s tests -p test_c_rerank.py -v
外部套件：numpy（HybridRetriever）。
"""

import os
import unittest
from unittest import mock

from services.c_rerank import Reranker, product_aliases, target_month
from services.c_retriever import HybridRetriever


def ev(eid, excerpt, pid, section=None, source_type="official_page"):
    return {"evidence_id": eid, "excerpt": excerpt, "product_ids": [pid] if pid else [],
            "section": section, "source_type": source_type, "source_title": "測試來源"}


DATASET = {
    "dataset_id": "rerank_test",
    "products": [
        {"product_id": "prd_a", "company_name": "甲人壽", "product_name": "甲人壽超好美元利率變動型終身壽險（AAA1）"},
        {"product_id": "prd_b", "company_name": "乙人壽", "product_name": "乙人壽大運外幣利率變動型終身壽險（BBB）"},
    ],
    "rates": [
        {"rate_id": f"r_a_{m}", "product_id": "prd_a", "rate_type": "declared", "period_month": f"2026-{m:02d}",
         "evidence_ids": [f"ev_a_{m}"]} for m in (6, 7, 8)
    ],
    "evidence": [
        *[ev(f"ev_a_{m}", f"宣告年月 115/{m}　商品名稱 甲人壽超好　宣告利率 4.{m}0%", "prd_a", "宣告利率")
          for m in (6, 7, 8)],
        ev("E1", "繳費年期 6年期　投保年齡 0歲~74歲　最低投保金額：6,000美元", "prd_a", "承保條件"),
        ev("E2", "繳費年期 6年期　投保年齡 0歲~70歲　最低保額 1萬美元", "prd_b", "承保條件"),
        ev("doc_a_p001_001", "本契約投保年齡依本公司規定辦理，" + "其他條款說明文字。" * 60, "prd_a",
           "第五條", "pdf"),
    ],
}


def retriever():
    with mock.patch.dict(os.environ, {"INSURRADAR_OFFLINE": "1"}):  # 不呼叫嵌入 API，只用關鍵字
        r = HybridRetriever(DATASET, index_dir=None)
    assert r.embedder is None
    return r


class QueryAnalysisTest(unittest.TestCase):
    def test_aliases_include_code_company_and_short_name(self):
        aliases = product_aliases(DATASET)
        self.assertTrue({"aaa1", "甲人壽", "超好"} <= aliases["prd_a"])
        self.assertIn("大運", aliases["prd_b"])

    def test_target_month_handles_ad_and_roc(self):
        self.assertEqual(target_month("2026 年 8 月的宣告利率"), "2026-08")
        self.assertEqual(target_month("民國115年7月"), "2026-07")
        self.assertEqual(target_month("115/6 宣告利率"), "2026-06")
        self.assertIsNone(target_month("6年期繳費"))
        self.assertIsNone(target_month("投保年齡 0歲~74歲"))


class RankingTest(unittest.TestCase):
    def ids(self, query, k=3):
        return [hit["evidence_id"] for hit in retriever().search(query, top_k=k)]

    def test_latest_month_first_when_no_month_given(self):
        self.assertEqual(self.ids("超好目前的宣告利率")[0], "ev_a_8")

    def test_requested_month_first_in_ad_or_roc(self):
        self.assertEqual(self.ids("超好 2026年7月 宣告利率")[0], "ev_a_7")
        self.assertEqual(self.ids("甲人壽 115年6月宣告利率")[0], "ev_a_6")

    def test_mentioned_product_beats_other_product(self):
        self.assertEqual(self.ids("大運的投保年齡")[0], "E2")
        self.assertEqual(self.ids("超好的投保年齡")[0], "E1")

    def test_rate_rows_do_not_crowd_out_other_questions(self):
        self.assertEqual(self.ids("超好最低投保金額", k=1), ["E1"])

    def test_long_pdf_paragraph_does_not_outrank_curated_evidence(self):
        self.assertEqual(self.ids("超好投保年齡")[0], "E1")

    def test_cross_product_question_covers_each_product(self):
        hits = self.ids("各家商品的投保年齡", k=2)
        self.assertEqual(set(hits), {"E1", "E2"})  # 兩家各一筆，而不是同一家佔滿
        self.assertEqual(len(self.ids("超好的投保年齡", k=2)), 2)  # 單一商品問題不受影響

    def test_scores_stay_between_zero_and_one(self):
        for hit in retriever().search("超好 宣告利率 投保年齡", top_k=10):
            self.assertTrue(0.0 <= hit["score"] <= 1.0)

    def test_keyword_scores_are_normalized_and_uncapped_ties_broken(self):
        reranker = Reranker(DATASET, {e["evidence_id"]: e for e in DATASET["evidence"]})
        scores = reranker.keyword_scores(reranker.profile("超好 投保年齡 最低投保金額"))
        self.assertEqual(max(scores.values()), 1.0)
        self.assertEqual(max(scores, key=scores.get), "E1")

    def test_hybrid_mode_applies_same_adjustments(self):
        def embed(texts, task_type):
            return [[1.0, 0.0] for _ in texts]  # 向量全部相同 → 排序只看關鍵字與加權
        r = HybridRetriever(DATASET, embedder=embed, index_dir=None)
        self.assertEqual(r.search("大運的投保年齡", top_k=1)[0]["evidence_id"], "E2")
        self.assertEqual(r.last_mode, "hybrid")


if __name__ == "__main__":
    unittest.main()
