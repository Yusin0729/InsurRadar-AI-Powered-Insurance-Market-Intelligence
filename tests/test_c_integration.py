"""C 組串接測試：快照路徑切換、檢索器快取、RAG 管線與助手頁實際使用 HybridRetriever。

不呼叫真實 Gemini API（INSURRADAR_OFFLINE=1 或注入假的嵌入／模型回應）。
執行：python -B -m unittest discover -s tests -p test_c_integration.py -v
外部套件：streamlit、numpy。
"""

import json
import os
import unittest
import warnings
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from services import c_integration
from services.c_retriever import HybridRetriever, describe_mode
from services.data_service import load_dataset
from services.rag_service import answer_rag_query

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
OFFLINE = {"INSURRADAR_OFFLINE": "1"}


def mock_dataset():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_dataset()


class DatasetPathTest(unittest.TestCase):
    def test_unset_or_blank_means_default_mock(self):
        for value in (None, "", "   "):
            env = {k: v for k, v in os.environ.items() if k != c_integration.DATASET_PATH_ENV}
            if value is not None:
                env[c_integration.DATASET_PATH_ENV] = value
            with self.subTest(value=value), mock.patch.dict(os.environ, env, clear=True):
                self.assertIsNone(c_integration.get_dataset_path())

    def test_env_path_is_returned(self):
        with mock.patch.dict(os.environ, {c_integration.DATASET_PATH_ENV: " data/real/dataset.json "}):
            self.assertEqual(c_integration.get_dataset_path(), "data/real/dataset.json")


class RetrieverCacheTest(unittest.TestCase):
    def setUp(self):
        c_integration.reset_retrievers()
        self.addCleanup(c_integration.reset_retrievers)
        self.dataset = mock_dataset()

    def test_same_dataset_id_reuses_retriever(self):
        with mock.patch.dict(os.environ, OFFLINE):
            first = c_integration.get_retriever(self.dataset)
            second = c_integration.get_retriever(mock_dataset())  # rerun 會產生新的快照物件
        self.assertIsInstance(first, HybridRetriever)
        self.assertIs(first, second)

    def test_mock_snapshot_does_not_write_index(self):
        with mock.patch.dict(os.environ, OFFLINE):
            retriever = c_integration.get_retriever(self.dataset)
        self.assertIsNone(retriever.index_dir)

    def test_new_dataset_id_replaces_retriever(self):
        with mock.patch.dict(os.environ, OFFLINE):
            first = c_integration.get_retriever(self.dataset)
            other = dict(self.dataset, dataset_id="mock_dataset_other")
            second = c_integration.get_retriever(other)
        self.assertIsNot(first, second)
        self.assertEqual(list(c_integration._retrievers), ["mock_dataset_other"])


class RagPipelineTest(unittest.TestCase):
    """HybridRetriever 的結果經 D 組 answer_rag_query 完整驗證流程。"""

    def test_hybrid_results_survive_rag_validation(self):
        dataset = mock_dataset()
        with mock.patch.dict(os.environ, OFFLINE):
            retriever = HybridRetriever(dataset, index_dir=None)
        hits = retriever.search("宣告利率", top_k=3)
        self.assertTrue(hits)
        reply = json.dumps({"answer": "測試回答", "citations": [hits[0]["evidence_id"]],
                            "has_sufficient_evidence": True}, ensure_ascii=False)
        result = answer_rag_query("宣告利率", dataset, retriever=retriever, mock_llm_response=reply)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["verification_passed"])
        self.assertEqual([i["evidence_id"] for i in result["retrieved_evidence"]],
                         [h["evidence_id"] for h in hits])
        self.assertEqual(describe_mode(retriever), "檢索模式：關鍵字（未使用向量）")


class AssistantPageTest(unittest.TestCase):
    """確認前端實際走 C 組的路徑與檢索器。"""

    def setUp(self):
        c_integration.reset_retrievers()
        self.addCleanup(c_integration.reset_retrievers)
        self.dataset = mock_dataset()
        env = mock.patch.dict(os.environ, {**OFFLINE, c_integration.DATASET_PATH_ENV: "data/real/dataset.json"})
        env.start()
        self.addCleanup(env.stop)
        self.loader = mock.patch("ui.common.load_dataset", return_value=self.dataset)
        self.load_mock = self.loader.start()
        self.addCleanup(self.loader.stop)
        self.app = AppTest.from_file(str(APP_PATH), default_timeout=15).run()
        self.assertEqual([e.message for e in self.app.exception], [])

    def test_env_path_is_passed_to_data_service(self):
        self.load_mock.assert_called_with("data/real/dataset.json")

    def test_assistant_uses_hybrid_retriever_and_shows_mode(self):
        pages = self.app._registered_pages
        self.app._page_hash = next(k for k, p in pages.items() if p["page_name"] == "AI 分析與 RAG 助手")
        self.app.run()
        calls = []

        def fake_answer(query, dataset, **kwargs):
            calls.append(kwargs)
            items = kwargs["retriever"].search(query, top_k=3)
            return {"status": "success", "answer": "測試回答", "citations": [items[0]["evidence_id"]],
                    "retrieved_evidence": items, "verification_passed": True, "is_mock": True}

        with mock.patch("ui.assistant.answer_rag_query", side_effect=fake_answer):
            self.app.chat_input[0].set_value("宣告利率").run()
        self.assertEqual([e.message for e in self.app.exception], [])
        self.assertIsInstance(calls[0]["retriever"], HybridRetriever)
        message = self.app.session_state["rag_chat_messages"][-1]
        self.assertEqual(message["retrieval_mode"], "檢索模式：關鍵字（未使用向量）")
        captions = "\n".join(c.value for c in self.app.caption)
        self.assertIn("檢索模式：關鍵字（未使用向量）", captions)


if __name__ == "__main__":
    unittest.main()
