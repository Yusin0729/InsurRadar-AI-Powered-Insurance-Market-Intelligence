"""E2 階段：RAG 智慧問答服務、Retriever 與引用邊界驗證測試。

主要用途與功能：
1. 測試 MockRetriever 中文關鍵字/子字串檢索與 Schema 契約符合性。
2. 測試無檢索結果時安全降級為「目前未檢索到足夠依據」，不發起 LLM 呼叫。
3. 測試正常問答流程與合格引用驗證。
4. 測試幽靈引用（不存在 ID）判定為驗證失敗。
5. 測試越界引用（引用非本次檢索所得之依據）判定為驗證失敗。
6. 測試肯定結論缺失依據引用判定為驗證失敗。
7. 測試 API 異常與金鑰缺失之安全錯誤處理。
8. 測試 Streamlit 介面問答互動、多輪歷史、rerun 防重複呼叫、證據彈窗開窗與清空對話。

外部套件：
- unittest: 標準測試框架。
- unittest.mock: 替身模擬 API 與檢索。
- streamlit.testing.v1.AppTest: 前端元件整合測試。
"""

import json
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from services.data_service import load_dataset
from services.rag_service import (
    answer_rag_query,
    format_retrieved_context,
    load_rag_prompt,
    validate_rag_response,
)
from services.retriever import MockRetriever
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


class RAGServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.dataset = load_dataset()
        self.retriever = MockRetriever(self.dataset)

    def test_mock_retriever_search_and_schema_contract(self):
        """驗證 MockRetriever 檢索輸出完全符合 docs/RETRIEVER_CONTRACT.md 規範。"""
        results = self.retriever.search("宣告利率", top_k=3)
        self.assertTrue(len(results) > 0)
        self.assertLessEqual(len(results), 3)

        required_fields = {
            "evidence_id", "source_title", "document_version", "page",
            "section", "excerpt", "score", "source_type", "source_url", "product_ids",
        }
        for item in results:
            self.assertTrue(required_fields.issubset(item.keys()))
            self.assertIsInstance(item["evidence_id"], str)
            self.assertIsInstance(item["source_title"], str)
            self.assertIsInstance(item["score"], float)
            self.assertGreaterEqual(item["score"], 0.0)
            self.assertLessEqual(item["score"], 1.0)
            self.assertIn(item["source_type"], ["mock", "official_page", "pdf", "news"])

    def test_mock_retriever_chinese_matching(self):
        """驗證中文連續字串匹配能力（如『躉繳』、『六年期』）。"""
        results_single = self.retriever.search("躉繳", top_k=2)
        self.assertTrue(any("躉繳" in r["excerpt"] or "躉繳" in r["source_title"] for r in results_single))

        results_multi = self.retriever.search("甲人壽展示商品", top_k=2)
        self.assertTrue(len(results_multi) > 0)

    def test_mock_retriever_empty_result(self):
        """驗證毫無關聯字串時安全回傳空清單。"""
        results = self.retriever.search("量子力學黑洞超導體XYZ999", top_k=3)
        self.assertEqual(results, [])

    def test_format_retrieved_context_and_load_prompt(self):
        """驗證 RAG Prompt 載入與檢索上下文格式化。"""
        prompt = load_rag_prompt()
        self.assertIn("{retrieved_context}", prompt)
        self.assertIn("{allowed_evidence_ids}", prompt)
        self.assertIn("{query}", prompt)

        retrieved = self.retriever.search("宣告利率", top_k=2)
        context_str, allowed_ids = format_retrieved_context(retrieved)
        self.assertTrue(len(allowed_ids) > 0)
        self.assertIn(allowed_ids[0], context_str)

    def test_rag_normal_success_with_valid_citations(self):
        """測試合格檢索與正確引用，應回傳 success 且 verification_passed=True。"""
        retrieved = self.retriever.search("宣告利率", top_k=2)
        allowed_id = retrieved[0]["evidence_id"]

        mock_json = json.dumps({
            "answer": "根據條款記載，該保單之宣告利率依據每期公告辦理。",
            "citations": [allowed_id],
            "has_sufficient_evidence": True,
        })

        res = answer_rag_query(
            "宣告利率如何計算？",
            self.dataset,
            retriever=self.retriever,
            mock_llm_response=mock_json,
        )
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["verification_passed"])
        self.assertEqual(res["citations"], [allowed_id])
        self.assertIsNone(res["error_message"])

    def test_rag_insufficient_data_when_retriever_empty(self):
        """測試檢索不到任何內容時，安全回傳『目前未檢索到足夠依據』，不呼叫 LLM。"""
        empty_retriever = MagicMock()
        empty_retriever.search.return_value = []

        res = answer_rag_query(
            "不存在的保險條款？",
            self.dataset,
            retriever=empty_retriever,
        )
        self.assertEqual(res["status"], "insufficient_data")
        self.assertIn("目前未檢索到足夠依據", res["answer"])
        self.assertEqual(res["citations"], [])
        self.assertTrue(res["verification_passed"])

    def test_rag_insufficient_data_with_empty_citations(self):
        """測試模型認定依據不足 (has_sufficient_evidence=False) 且無引用時正常回傳。"""
        retrieved = self.retriever.search("宣告利率", top_k=2)
        mock_json = json.dumps({
            "answer": "目前檢索到的依據不足以確認此問題。",
            "citations": [],
            "has_sufficient_evidence": False,
        })
        res = answer_rag_query(
            "國外特定法規？",
            self.dataset,
            retriever=self.retriever,
            mock_llm_response=mock_json,
        )
        self.assertEqual(res["status"], "insufficient_data")
        self.assertTrue(res["verification_passed"])
        self.assertEqual(res["citations"], [])

    def test_rag_ghost_citation_marked_validation_failed(self):
        """測試引用資料集中不存在的幽靈 ID，整則回答判定為驗證失敗。"""
        retrieved = self.retriever.search("宣告利率", top_k=2)
        mock_json = json.dumps({
            "answer": "該保單宣告利率非常優渥。",
            "citations": ["mock_evidence_ghost_9999"],
            "has_sufficient_evidence": True,
        })
        res = answer_rag_query(
            "宣告利率如何計算？",
            self.dataset,
            retriever=self.retriever,
            mock_llm_response=mock_json,
        )
        self.assertEqual(res["status"], "validation_failed")
        self.assertFalse(res["verification_passed"])
        self.assertIn("不存在的幽靈依據 ID", res["error_message"])

    def test_rag_out_of_bounds_citation_marked_validation_failed(self):
        """測試引用了存在於資料集但『未包含於當次檢索結果』的 ID，整則回答判定為驗證失敗。"""
        retrieved = self.retriever.search("宣告利率", top_k=1)
        in_retrieval_id = retrieved[0]["evidence_id"]
        # 挑選一個存在於資料集但絕不在 retrieved 中的 ID
        all_eids = [e["evidence_id"] for e in self.dataset["evidence"]]
        out_of_bounds_id = next(eid for eid in all_eids if eid != in_retrieval_id)

        mock_json = json.dumps({
            "answer": "這是一則越界引用的肯定結論。",
            "citations": [out_of_bounds_id],
            "has_sufficient_evidence": True,
        })
        res = answer_rag_query(
            "宣告利率的相關依據為何？",
            self.dataset,
            retriever=MagicMock(search=MagicMock(return_value=retrieved)),
            mock_llm_response=mock_json,
        )
        self.assertEqual(res["status"], "validation_failed")
        self.assertFalse(res["verification_passed"])
        self.assertIn("未包含於本次檢索結果", res["error_message"])

    def test_rag_empty_citations_with_affirmative_conclusion_fails(self):
        """測試宣稱有足夠依據但 citations 為空，判定為驗證失敗。"""
        self.retriever.search("宣告利率", top_k=2)
        mock_json = json.dumps({
            "answer": "這是一筆肯定性結論，但沒有附上依據。",
            "citations": [],
            "has_sufficient_evidence": True,
        })
        passed, _, reason = validate_rag_response(
            mock_json, retrieved_evidence_ids={"mock_evidence_001"}, dataset=self.dataset
        )
        self.assertFalse(passed)
        self.assertIn("肯定性結論卻未附帶任何依據引用", reason)

    def test_rag_api_failure_error_handling(self):
        """測試 API 拋出異常時安全捕捉，回報明確錯誤訊息。"""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("連線逾時 Connection timed out")

        res = answer_rag_query(
            "宣告利率的計算方式？",
            self.dataset,
            retriever=self.retriever,
            api_key="mock_key",
            client=mock_client,
        )
        self.assertEqual(res["status"], "error")
        self.assertIn("Connection timed out", res["error_message"])
        self.assertFalse(res["verification_passed"])

    def test_rag_missing_api_key_error_handling(self):
        """測試未設定 API Key 時回報明確錯誤，不輸出捏造答案。"""
        with patch("services.rag_service.get_gemini_config", return_value={"api_key": None, "model": "gemini-2.5-flash"}):
            res = answer_rag_query(
                "宣告利率相關依據",
                self.dataset,
                retriever=self.retriever,
                api_key=None,
            )
            self.assertEqual(res["status"], "error")
            self.assertIn("尚未設定 GEMINI_API_KEY", res["answer"])

    def test_strict_response_types_and_shape(self):
        good = {"answer": "回答", "citations": ["mock_evidence_001"], "has_sufficient_evidence": True}
        cases = []
        for field, values in {"answer": [None, 42, [], {}], "citations": [None, "id", [None], [1], [{}], [[]]], "has_sufficient_evidence": [None, 0, 1, "false", [], {}]}.items():
            for value in values:
                cases.append(dict(good, **{field: value}))
        cases += [{}, dict(good, extra=True), {"answer": "回答", "citations": []}]
        for case in cases:
            with self.subTest(case=case):
                passed, _, _ = validate_rag_response(json.dumps(case), {"mock_evidence_001"}, self.dataset)
                self.assertFalse(passed)
        for raw in [None, "oops", "[]", "null"]:
            self.assertFalse(validate_rag_response(raw, set(), self.dataset)[0])

    def test_insufficient_affirmative_text_never_escapes(self):
        for sufficient, citations in [(False, []), (True, []), (False, ["mock_evidence_001"]), (True, ["ghost"])]:
            with self.subTest(sufficient=sufficient, citations=citations):
                result = answer_rag_query("躉繳", self.dataset, mock_llm_response=json.dumps({
                    "answer": "保證獲利 99%", "citations": citations, "has_sufficient_evidence": sufficient}))
                self.assertNotIn("99%", result["answer"])
                self.assertNotIn("raw_answer", result)
                self.assertEqual(result["citations"], [])

    def test_retriever_invalid_items_skip_api(self):
        item = self.retriever.search("躉繳", 1)[0]
        cases = [[], [dict(item, excerpt="")], [dict(item, evidence_id="ghost")],
                 [dict(item, excerpt="保證獲利99%")], [dict(item, source_type="invalid")],
                 [dict(item, document_version="old")], [dict(item, dataset_id="old")],
                 [dict(item, source_title="不同文件")], [dict(item, score=float("nan"))],
                 [dict(item, source_url="https://wrong.example")]]
        for items in cases:
            with self.subTest(items=items), patch("services.rag_service.call_gemini_rag_api") as api:
                result = answer_rag_query("躉繳", self.dataset, retriever=MagicMock(search=MagicMock(return_value=items)), api_key="fake")
                self.assertEqual(result["status"], "insufficient_data")
                api.assert_not_called()

    def test_legal_subexcerpt_reaches_api_and_same_source(self):
        item = self.retriever.search("躉繳", 1)[0]
        item["excerpt"] = item["excerpt"][7:30]
        client = MagicMock()
        client.models.generate_content.return_value.text = json.dumps({
            "answer": "依原文說明", "citations": [item["evidence_id"]], "has_sufficient_evidence": True})
        result = answer_rag_query("躉繳", self.dataset, retriever=MagicMock(search=MagicMock(return_value=[item])), api_key="fake", client=client)
        self.assertEqual(result["status"], "success")
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn(item["excerpt"], prompt)
        source = next(e for e in self.dataset["evidence"] if e["evidence_id"] == item["evidence_id"])
        self.assertIn(result["retrieved_evidence"][0]["excerpt"], source["excerpt"])
        self.assertEqual(result["retrieved_evidence"][0]["source_title"], source["source_title"])
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIsNotNone(config.response_schema)

    def test_prompt_failure_does_not_call_api(self):
        for bad_prompt in ["{unknown}", "{query} {retrieved_context} {allowed_evidence_ids} {broken", "plain"]:
            with self.subTest(prompt=bad_prompt), patch("services.rag_service.load_rag_prompt", return_value=bad_prompt), patch("services.rag_service.call_gemini_rag_api") as api:
                result = answer_rag_query("躉繳", self.dataset, api_key="fake")
                self.assertEqual(result["status"], "error")
                self.assertIn("Prompt", result["answer"])
                api.assert_not_called()
        with patch("services.rag_service.load_rag_prompt", side_effect=FileNotFoundError("missing")), patch("services.rag_service.call_gemini_rag_api") as api:
            self.assertEqual(answer_rag_query("躉繳", self.dataset, api_key="fake")["status"], "error")
            api.assert_not_called()

    def test_top_k_boundaries_and_sorting(self):
        for value in [0, -1, True, 1.5, "3", None]:
            with self.subTest(value=value), patch("services.rag_service.call_gemini_rag_api") as api:
                self.assertEqual(answer_rag_query("躉繳", self.dataset, top_k=value)["status"], "error")
                with self.assertRaises(ValueError):
                    self.retriever.search("躉繳", value)
                api.assert_not_called()
        for value in [1, 3, 100]:
            results = self.retriever.search("宣告利率", value)
            self.assertLessEqual(len(results), value)
            self.assertEqual(results, sorted(results, key=lambda x: (x["score"], x["evidence_id"]), reverse=True))
            self.assertTrue(all(r["excerpt"] for r in results))

    def test_retriever_exception_and_malformed_container(self):
        for retriever in [MagicMock(search=MagicMock(side_effect=RuntimeError("timeout"))), MagicMock(search=MagicMock(return_value={}))]:
            with patch("services.rag_service.call_gemini_rag_api") as api:
                self.assertEqual(answer_rag_query("躉繳", self.dataset, retriever=retriever)["status"], "error")
                api.assert_not_called()

    def test_gemini_model_config_precedence(self):
        from services.llm_service import get_gemini_config, DEFAULT_MODEL
        import os
        for env, secrets, expected in [
            ({"GEMINI_MODEL": "env-model", "GEMINI_API_KEY": "env-key"}, {"GEMINI_MODEL": "secret-model", "GEMINI_API_KEY": "secret-key"}, ("env-model", "env-key")),
            ({"GEMINI_API_KEY": "env-key"}, {"GEMINI_MODEL": "secret-model"}, ("secret-model", "env-key")),
            ({"GEMINI_MODEL": "env-model"}, {"GEMINI_API_KEY": "secret-key"}, ("env-model", "secret-key")),
            ({}, {}, (DEFAULT_MODEL, None)),
        ]:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), patch("streamlit.secrets", secrets):
                config = get_gemini_config()
                self.assertEqual((config["model"], config["api_key"]), expected)


class RAGStreamlitIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.dataset = load_dataset()

    def switch_to(self, app, title):
        pages = app._registered_pages
        app._page_hash = next(key for key, page in pages.items() if page["page_name"] == title)
        app.run()
        self.assertEqual([item.message for item in app.exception], [])

    def test_rag_chat_interaction_multiturn_and_rerun_caching(self):
        """測試在 AI 助手頁發送問答、多輪歷史保存、rerun 不重複呼叫以及清空對話。"""
        app = AppTest.from_file(str(APP_PATH), default_timeout=15).run()
        self.switch_to(app, "AI 分析與 RAG 助手")

        # 模擬 RAG 回應計數器
        call_counter = {"count": 0}

        def mock_rag_fn(query, dataset, *args, **kwargs):
            call_counter["count"] += 1
            return {
                "status": "success",
                "query": query,
                "answer": f"這是針對『{query}』的模擬 RAG 解答。",
                "citations": ["mock_evidence_001"],
                "retrieved_evidence": [
                    {
                        "evidence_id": "mock_evidence_001",
                        "source_title": "測試來源文件",
                        "score": 0.88,
                        "excerpt": "測試依據原文片段",
                    }
                ],
                "verification_passed": True,
                "error_message": None,
                "is_mock": True,
                "created_at": "2026-10-30T10:00:00Z",
            }

        with patch("ui.assistant.answer_rag_query", side_effect=mock_rag_fn):
            # 實際輸入第一輪問題。
            app.chat_input[0].set_value("模擬甲人壽一號商品宣告利率為何？").run()
            self.assertEqual(call_counter["count"], 1)
            self.assertEqual([item.message for item in app.exception], [])

            # 驗證畫面上包含使用者的提問與 Assistant 的回答
            all_text = " ".join(item.value for item in app.markdown)
            self.assertIn("模擬甲人壽一號商品宣告利率為何？", all_text)
            self.assertIn("這是針對『模擬甲人壽一號商品宣告利率為何？』的模擬 RAG 解答", all_text)

            # 2. 觸發 rerun（例如頁面重新渲染）
            app.run()
            # 驗證計數器維持 1，未重複呼叫 API
            self.assertEqual(call_counter["count"], 1)

            # 3. 點擊回答底部的依據按鈕，驗證共用證據彈窗能正常叫起
            evidence_btn_key = "evidence_open_rag_answer_msg_1"
            self.assertIn(evidence_btn_key, [b.key for b in app.button])
            app.button(key=evidence_btn_key).click().run()
            self.assertEqual([item.message for item in app.exception], [])
            self.assertIn("evidence_dialog_context", app.session_state)

            self.assertEqual(call_counter["count"], 1)
            app.button(key="evidence_close").click().run()
            app.chat_input[0].set_value("哪些商品提供六年繳費？").run()
            self.assertEqual(call_counter["count"], 2)
            self.assertEqual(len(app.session_state["rag_chat_messages"]), 4)
            self.switch_to(app, "競品比較庫")
            self.switch_to(app, "AI 分析與 RAG 助手")
            app.run()
            self.assertEqual(call_counter["count"], 2)
            self.assertEqual(len(app.chat_message), 4)
            changed = deepcopy(self.dataset)
            changed["dataset_id"] = "mock_new_snapshot"
            with patch("ui.common.load_dataset", return_value=changed):
                app.run()
                self.assertEqual(len(app.chat_message), 0)
                self.assertNotIn("evidence_open_rag_answer_msg_1", [b.key for b in app.button])
            self.assertEqual(call_counter["count"], 2)
            # 4. 點擊清空對話按鈕
            app.button(key="btn_clear_rag_chat").click().run()
            self.assertEqual([item.message for item in app.exception], [])
            # 對話清單已清空
            self.assertEqual(len(app.session_state["rag_chat_messages"]), 0)


if __name__ == "__main__":
    unittest.main()
