"""Demo 問答快取測試（全部使用假資料與假函式，不連網）；外部套件：無。"""

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings

from services.data_service import load_dataset
from services.demo_cache import (
    DEMO_CACHE_ENV, answer_with_demo_cache, cache_forced, is_cacheable, load_cache, lookup,
    make_entry, normalize_question, save_entries,
)
from services.gemini_retry import BUSY_MESSAGE


def mock_dataset():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_dataset()


class DemoCacheTests(unittest.TestCase):
    def setUp(self):
        self.dataset = mock_dataset()
        self.evidence = self.dataset["evidence"][0]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "demo_answers.json"
        self.question = "測試題目的宣告利率是多少？"
        self.live_calls = []

    def retrieved(self):
        ev = self.evidence
        return [{"evidence_id": ev["evidence_id"], "excerpt": ev["excerpt"], "score": 0.9,
                 "source_title": ev["source_title"], "source_type": ev["source_type"]}]

    def success_result(self, **extra):
        result = {"status": "success", "query": self.question, "answer": "依據原文回答。",
                  "citations": [self.evidence["evidence_id"]], "retrieved_evidence": self.retrieved(),
                  "verification_passed": True, "error_message": None, "is_mock": True,
                  "created_at": "2026-11-05T10:00:00+00:00"}
        result.update(extra)
        return result

    def store(self, result=None):
        entry = make_entry(result or self.success_result(), dataset_id=self.dataset["dataset_id"], model="fake-model")
        save_entries([entry], self.path)
        return entry

    def fake_answer(self, result):
        def answer(query, dataset, *, retriever=None):
            self.live_calls.append(query)
            return result
        return answer

    def busy_result(self):
        return {"status": "error", "query": self.question, "answer": BUSY_MESSAGE, "citations": [],
                "retrieved_evidence": [], "verification_passed": False,
                "error_message": "Gemini API 呼叫過程中發生錯誤：503 UNAVAILABLE"}

    # ---- 收錄規則 ----
    def test_only_verified_success_or_insufficient_is_cacheable(self):
        self.assertTrue(is_cacheable(self.success_result()))
        self.assertTrue(is_cacheable(self.success_result(status="insufficient_data", citations=[])))
        self.assertFalse(is_cacheable(self.success_result(status="validation_failed")))
        self.assertFalse(is_cacheable(self.success_result(status="error")))
        self.assertFalse(is_cacheable(self.success_result(verification_passed=False)))
        self.assertFalse(is_cacheable(self.success_result(citations=[])))
        self.assertFalse(is_cacheable(self.success_result(citations=["not_retrieved"])))
        self.assertFalse(is_cacheable(self.success_result(status="insufficient_data")))  # 不足卻帶引用
        with self.assertRaises(ValueError):
            make_entry(self.success_result(status="error"), dataset_id="x", model="m")

    def test_entry_keeps_required_fields_and_merges_by_question(self):
        entry = self.store()
        for key in ("dataset_id", "question", "answer", "citations", "retrieved_evidence", "created_at", "model"):
            self.assertIn(key, entry)
        self.store(self.success_result(answer="新的回答。"))
        entries = load_cache(self.path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["answer"], "新的回答。")

    def test_missing_or_corrupt_cache_is_empty(self):
        self.assertEqual(load_cache(self.path), [])
        self.path.write_text("{bad json", encoding="utf-8")
        self.assertEqual(load_cache(self.path), [])
        self.assertIsNone(lookup(self.question, self.dataset, self.path))

    # ---- 查詢規則 ----
    def test_lookup_requires_exact_question_and_same_dataset(self):
        self.store()
        self.assertIsNotNone(lookup(f"  {self.question} ", self.dataset, self.path))
        self.assertIsNone(lookup(self.question.replace("？", "?"), self.dataset, self.path))
        self.assertIsNone(lookup("測試題目的宣告利率", self.dataset, self.path))
        other = deepcopy(self.dataset)
        other["dataset_id"] = "another_snapshot"
        self.assertIsNone(lookup(self.question, other, self.path))

    def test_lookup_rejects_changed_source_or_missing_citation(self):
        self.store()
        changed = deepcopy(self.dataset)
        changed["evidence"][0]["excerpt"] = "完全不同的原文"
        self.assertIsNone(lookup(self.question, changed, self.path))
        removed = deepcopy(self.dataset)
        removed["evidence"] = removed["evidence"][1:]
        self.assertIsNone(lookup(self.question, removed, self.path))

    def test_lookup_returns_canonical_evidence_for_dialog(self):
        self.store()
        entry = lookup(self.question, self.dataset, self.path)
        item = entry["retrieved_evidence"][0]
        self.assertEqual(item["evidence_id"], self.evidence["evidence_id"])
        self.assertEqual(item["source_title"], self.evidence["source_title"])
        self.assertIn("local_path", item)

    # ---- 使用時機 ----
    def test_live_success_is_returned_without_cache(self):
        self.store()
        live = self.success_result(answer="即時回答。")
        result = answer_with_demo_cache(self.question, self.dataset, answer_fn=self.fake_answer(live),
                                        path=self.path, forced=False)
        self.assertIs(result, live)
        self.assertNotIn("from_cache", result)

    def test_transient_failure_falls_back_to_cache(self):
        self.store()
        result = answer_with_demo_cache(self.question, self.dataset, answer_fn=self.fake_answer(self.busy_result()),
                                        path=self.path, forced=False)
        self.assertTrue(result["from_cache"])
        self.assertEqual(result["cache_reason"], "fallback")
        self.assertEqual(result["cache_model"], "fake-model")
        self.assertEqual(result["citations"], [self.evidence["evidence_id"]])
        self.assertEqual(self.live_calls, [self.question])

    def test_transient_detected_from_error_message(self):
        self.store()
        busy = dict(self.busy_result(), answer="AI 回答生成失敗：x", error_message="429 RESOURCE_EXHAUSTED")
        result = answer_with_demo_cache(self.question, self.dataset, answer_fn=self.fake_answer(busy),
                                        path=self.path, forced=False)
        self.assertTrue(result.get("from_cache"))

    def test_non_transient_failure_is_not_replaced(self):
        self.store()
        for failure in (
            {"status": "error", "answer": "尚未設定 GEMINI_API_KEY。", "error_message": "未設定 GEMINI_API_KEY。"},
            {"status": "validation_failed", "answer": "攔截", "error_message": "幽靈引用"},
        ):
            with self.subTest(failure=failure["status"]):
                result = answer_with_demo_cache(self.question, self.dataset, answer_fn=self.fake_answer(failure),
                                                path=self.path, forced=False)
                self.assertIs(result, failure)

    def test_transient_failure_without_cache_stays_error(self):
        busy = self.busy_result()
        result = answer_with_demo_cache("沒有快取的題目", self.dataset, answer_fn=self.fake_answer(busy),
                                        path=self.path, forced=False)
        self.assertIs(result, busy)

    def test_forced_flag_uses_cache_without_live_call(self):
        self.store()
        result = answer_with_demo_cache(self.question, self.dataset, answer_fn=self.fake_answer(self.busy_result()),
                                        path=self.path, forced=True)
        self.assertEqual(result["cache_reason"], "forced")
        self.assertEqual(self.live_calls, [])
        live = self.success_result(answer="即時回答。")
        miss = answer_with_demo_cache("未快取題目", self.dataset, answer_fn=self.fake_answer(live),
                                      path=self.path, forced=True)
        self.assertIs(miss, live)

    def test_env_flag(self):
        with patch.dict(os.environ, {DEMO_CACHE_ENV: "1"}):
            self.assertTrue(cache_forced())
        with patch.dict(os.environ, {DEMO_CACHE_ENV: "0"}):
            self.assertFalse(cache_forced())

    def test_normalize_question(self):
        self.assertEqual(normalize_question("  a   b \n"), "a b")
        self.assertEqual(normalize_question(None), "")


if __name__ == "__main__":
    unittest.main()
