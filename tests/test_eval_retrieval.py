"""檢索評估工具測試；資料為測試用，不代表真實保單內容，不連網。

執行：python -B -m unittest discover -s tests -p test_eval_retrieval.py -v
外部套件：無。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.c_retriever import HybridRetriever
from tools import eval_retrieval as er

PROJECT_ROOT = Path(__file__).resolve().parents[1]

QUESTIONS = [
    {"qid": "q1", "question": "A 商品宣告利率", "expected_evidence_ids": ["E1"]},
    {"qid": "q2", "question": "B 商品投保年齡", "expected_evidence_ids": ["E2", "E3"]},
    {"qid": "q3", "question": "C 商品解約金", "expected_evidence_ids": ["E9"]},
]


class FakeRetriever:
    """依問題回傳預先排好的 evidence_id，模擬 last_mode／last_error。"""

    def __init__(self, answers, mode="hybrid", fail_on=None):
        self.answers = answers
        self.last_mode = mode
        self.last_error = None
        self.fail_on = fail_on
        self.calls = []

    def search(self, query, top_k=3):
        self.calls.append((query, top_k))
        if query == self.fail_on:
            raise RuntimeError("連線逾時")
        return [{"evidence_id": eid, "score": 0.9 - i * 0.1, "excerpt": f"原文{eid}", "product_ids": []}
                for i, eid in enumerate(self.answers.get(query, []))][:top_k]


class EvaluateTest(unittest.TestCase):
    def test_hit_rank_and_mrr(self):
        retriever = FakeRetriever({
            "A 商品宣告利率": ["E1", "E5"],          # 第 1 名命中
            "B 商品投保年齡": ["E7", "E3", "E2"],     # 第 2 名命中（E3 也算正確）
            "C 商品解約金": ["E4", "E5", "E6", "E9"],  # E9 在第 4 名，k=3 未命中
        })
        report = er.evaluate(retriever, QUESTIONS, k=3)
        rows = {r["qid"]: r for r in report["results"]}
        self.assertEqual((rows["q1"]["rank"], rows["q2"]["rank"], rows["q3"]["rank"]), (1, 2, None))
        self.assertEqual(len(rows["q3"]["hits"]), 3)
        s = report["summary"]
        self.assertEqual((s["hits"], s["total"]), (2, 3))
        self.assertAlmostEqual(s["hit_rate"], 0.6667, places=4)
        self.assertAlmostEqual(s["mrr"], round((1 + 0.5) / 3, 4))
        self.assertEqual(s["missed"], ["q3"])
        self.assertEqual(s["modes"], {"hybrid": 3})
        self.assertTrue(all(call[1] == 3 for call in retriever.calls))

    def test_larger_k_finds_lower_ranked_answer(self):
        retriever = FakeRetriever({"C 商品解約金": ["E4", "E5", "E6", "E9"]})
        report = er.evaluate(retriever, QUESTIONS[2:], k=5)
        self.assertEqual(report["results"][0]["rank"], 4)
        self.assertAlmostEqual(report["summary"]["mrr"], 0.25)

    def test_search_exception_counts_as_miss_and_continues(self):
        retriever = FakeRetriever({"A 商品宣告利率": ["E1"]}, fail_on="B 商品投保年齡")
        report = er.evaluate(retriever, QUESTIONS, k=3)
        rows = {r["qid"]: r for r in report["results"]}
        self.assertFalse(rows["q2"]["hit"])
        self.assertIn("連線逾時", rows["q2"]["error"])
        self.assertTrue(rows["q1"]["hit"])
        self.assertEqual(len(report["results"]), 3)

    def test_fallback_error_is_recorded(self):
        retriever = FakeRetriever({"A 商品宣告利率": ["E1"]}, mode="keyword")
        retriever.last_error = "向量索引建立失敗，改用關鍵字檢索：429"
        row = er.evaluate(retriever, QUESTIONS[:1], k=3)["results"][0]
        self.assertEqual(row["mode"], "keyword")
        self.assertIn("429", row["error"])

    def test_invalid_k(self):
        for bad in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                er.evaluate(FakeRetriever({}), QUESTIONS, k=bad)

    def test_mode_label(self):
        self.assertEqual(er.mode_label({"keyword": 10}), "keyword")
        self.assertEqual(er.mode_label({"keyword": 2, "hybrid": 8}), "mixed")


class QuestionsTest(unittest.TestCase):
    def _write(self, data):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, tmp, ensure_ascii=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return Path(tmp.name)

    def test_rejects_bad_question_files(self):
        bad = [
            {"questions": []},
            {"questions": [{"qid": "a", "question": " ", "expected_evidence_ids": ["E1"]}]},
            {"questions": [{"qid": "a", "question": "問", "expected_evidence_ids": []}]},
            {"questions": [{"qid": "a", "question": "問", "expected_evidence_ids": ["E1"]},
                           {"qid": "a", "question": "問2", "expected_evidence_ids": ["E2"]}]},
        ]
        for data in bad:
            with self.assertRaises(ValueError):
                er.load_questions(self._write(data))

    def test_missing_expected(self):
        dataset = {"evidence": [{"evidence_id": "E1"}, {"evidence_id": "E2"}]}
        self.assertEqual(er.missing_expected(QUESTIONS, dataset), {"q2": ["E3"], "q3": ["E9"]})

    def test_repo_question_file_is_valid_and_has_no_excerpts(self):
        """正式問題檔：10 題、格式正確；repo 是公開的，只能放問題與 ID，不放原文。"""
        path = PROJECT_ROOT / "tests" / "data" / "retrieval_questions.json"
        questions = er.load_questions(path)
        self.assertEqual(len(questions), 10)
        for q in questions:
            self.assertNotIn("excerpt", q)
            for word in ("報酬率", "最好", "推薦"):  # 團隊用語規則
                self.assertNotIn(word, q["question"])


class EndToEndTest(unittest.TestCase):
    """用真的 HybridRetriever（關鍵字模式）跑小型假快照，並寫出報告檔。"""

    DATASET = {
        "dataset_id": "test_eval",
        "products": [{"product_id": "prd_a", "company_name": "甲人壽", "product_name": "甲美元終身壽險"},
                     {"product_id": "prd_b", "company_name": "乙人壽", "product_name": "乙外幣終身壽險"}],
        "evidence": [
            {"evidence_id": "E1", "source_title": "甲人壽宣告利率", "excerpt": "甲美元終身壽險 宣告利率 4.10%",
             "source_type": "official_page", "product_ids": ["prd_a"]},
            {"evidence_id": "E2", "source_title": "乙人壽商品頁", "excerpt": "投保年齡 0歲~70歲 最低保額 1萬美元",
             "source_type": "official_page", "product_ids": ["prd_b"]},
        ],
    }

    def test_keyword_mode_end_to_end_and_report_files(self):
        with mock.patch.dict(os.environ, {"INSURRADAR_OFFLINE": "1"}):
            retriever = HybridRetriever(self.DATASET, index_dir=None)
        questions = [
            {"qid": "q1", "question": "甲美元終身壽險的宣告利率", "expected_evidence_ids": ["E1"]},
            {"qid": "q2", "question": "乙人壽投保年齡", "expected_evidence_ids": ["E2"]},
        ]
        report = er.evaluate(retriever, questions, k=1)
        self.assertEqual(report["summary"]["hits"], 2)
        self.assertEqual(report["summary"]["modes"], {"keyword": 2})

        with tempfile.TemporaryDirectory() as tmp:
            json_path, csv_path = er.write_report(report, Path(tmp))
            self.assertIn("retrieval_eval_keyword_", json_path.name)
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["summary"]["hit_rate"], 1.0)
            lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("qid,問題"))


class CliTest(unittest.TestCase):
    def test_threshold_controls_exit_code_and_offline_flag(self):
        fake = FakeRetriever({"A 商品宣告利率": ["E1"]}, mode="keyword")  # 3 題中 1 題命中
        qpath = Path(tempfile.mkdtemp()) / "q.json"
        qpath.write_text(json.dumps({"questions": QUESTIONS}, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(lambda: (qpath.unlink(), qpath.parent.rmdir()))
        dataset = {"dataset_id": "t", "evidence": [{"evidence_id": e} for e in ("E1", "E2", "E9")]}
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("services.data_service.load_dataset", return_value=dataset), \
                mock.patch("services.c_integration.get_retriever", return_value=fake), \
                mock.patch("builtins.print"):
            os.environ.pop("INSURRADAR_OFFLINE", None)
            base = ["x.json", "--questions", str(qpath), "--no-write", "--offline"]
            self.assertEqual(er.main(base + ["--threshold", "0.5"]), 1)
            self.assertEqual(os.environ.get("INSURRADAR_OFFLINE"), "1")
            self.assertEqual(er.main(base + ["--threshold", "0.3"]), 0)

    def test_load_failure_returns_2(self):
        with mock.patch("services.data_service.load_dataset", side_effect=ValueError("壞掉")), \
                mock.patch("builtins.print"):
            self.assertEqual(er.main(["missing.json", "--no-write"]), 2)


if __name__ == "__main__":
    unittest.main()
