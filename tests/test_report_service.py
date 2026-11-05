"""E3 報告內容、範圍隔離與 Streamlit 下載測試；外部套件：streamlit。"""
from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch
import warnings

from streamlit.testing.v1 import AppTest
from services.data_service import load_dataset, get_news
from services.retriever import MockRetriever
from services.report_service import build_markdown_report, text_block, cell, report_fingerprint

NOW = "2026-10-30T12:00:00+08:00"
APP = Path(__file__).resolve().parents[1] / "app.py"


def fixture():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        data = load_dataset()
    retrieved = MockRetriever(data).search("躉繳", 1)
    answer = {"role": "assistant", "dataset_id": data["dataset_id"], "content": "既有 RAG 回答",
              "status": "success", "citations": [retrieved[0]["evidence_id"]],
              "retrieved_evidence": retrieved, "verification_passed": True, "created_at": NOW,
              "product_ids": retrieved[0]["product_ids"], "is_mock": True}
    ai = {"dataset_id": data["dataset_id"], "status": "success", "model": "test-model",
          "created_at": NOW, "insights": [dict(deepcopy(data["insights"][0]), title="既有 Gemini 觀察")]}
    state = {"filter_period_month": "2026-10", "gemini_dashboard_analysis": ai,
             "rag_chat_messages": [{"role": "user", "dataset_id": data["dataset_id"], "content": "我的問題"}, answer]}
    return data, state


class ReportServiceTests(unittest.TestCase):
    def setUp(self):
        self.data, self.state = fixture()
        self.api = patch("services.rag_service.call_gemini_rag_api", side_effect=AssertionError("禁止 API"))
        self.api.start()
        self.addCleanup(self.api.stop)
        self.ai_api = patch("services.llm_service.call_gemini_api", side_effect=AssertionError("禁止 API"))
        self.ai_api.start()
        self.addCleanup(self.ai_api.stop)

    def build(self):
        return build_markdown_report(self.data, self.state, generated_at=NOW)

    def test_full_report_and_utf8(self):
        report = self.build()
        text = report["markdown"]
        for part in ["# InsurRadar", NOW, self.data["dataset_id"], self.data["as_of"],
                     "商品及宣告利率比較", "既有 Gemini 觀察", "既有 RAG 回答", "市場情報摘要", "Mock Data", "語意支持性未驗證"]:
            self.assertIn(part, text)
        self.assertEqual(report["counts"]["rag_answers"], 1)
        self.assertEqual(text.encode("utf-8").decode("utf-8"), text)
        self.assertTrue(set(report["evidence_ids"]).issubset({e["evidence_id"] for e in self.data["evidence"]}))

    def test_deterministic_without_mutation(self):
        before = deepcopy((self.data, self.state))
        self.assertEqual(self.build(), self.build())
        self.assertEqual((self.data, self.state), before)
        self.data["products"].reverse()
        self.data["rates"].reverse()
        self.data["insights"].reverse()
        self.data["news"].reverse()
        self.assertEqual(self.build(), build_markdown_report(before[0], before[1], generated_at=NOW))

    def test_no_generated_ai(self):
        self.state.pop("gemini_dashboard_analysis")
        text = self.build()["markdown"]
        self.assertIn("Gemini AI 分析尚未產生", text)
        self.assertNotIn("既有 Gemini 觀察", text)
        self.assertIn("快照既有觀察", text)

    def test_cached_rag_answer_is_labelled(self):
        self.assertNotIn("Demo 快取回答", self.build()["markdown"])
        self.state["rag_chat_messages"][1]["from_cache"] = True
        report = self.build()
        self.assertIn("Demo 快取回答（預先產生，非本次即時生成）", report["markdown"])
        self.assertEqual(report["counts"]["rag_answers"], 1)

    def test_no_rag(self):
        self.state.pop("rag_chat_messages")
        self.assertIn("RAG 問答尚未產生", self.build()["markdown"])

    def test_missing_data_not_fabricated(self):
        text = self.build()["markdown"]
        self.assertIn("資料待補", text)
        self.assertIn(text_block(self.data["evidence"][0]["document_version"]), text)
        self.state["filter_period_month"] = "2020-01"
        self.assertEqual(self.build()["counts"]["rates"], 0)
        self.assertIn("不沿用其他月份", self.build()["markdown"])

    def test_cross_snapshot_and_unbound_results(self):
        for identity in [None, "other-snapshot"]:
            with self.subTest(identity=identity):
                self.state["gemini_dashboard_analysis"]["dataset_id"] = identity
                self.state["rag_chat_messages"][1]["dataset_id"] = identity
                report = self.build()
                self.assertNotIn("既有 Gemini 觀察", report["markdown"])
                self.assertNotIn("既有 RAG 回答", report["markdown"])
                self.assertTrue(report["warnings"])

    def test_invalid_ai_and_rag_citations(self):
        self.state["gemini_dashboard_analysis"]["insights"][0]["evidence_ids"] = ["ghost"]
        self.state["rag_chat_messages"][1]["citations"] = ["ghost"]
        report = self.build()
        self.assertNotIn("既有 Gemini 觀察", report["markdown"])
        self.assertNotIn("既有 RAG 回答", report["markdown"])
        self.assertNotIn("ghost", report["evidence_ids"])
        self.assertIn("引用失效", report["markdown"])

    def test_independent_product_and_market_filters(self):
        self.state.update(filter_company_ids=["mock_company_001"], comparison_product_ids=["mock_product_002"],
                          market_category="interest_rate", market_keyword="利率")
        report = self.build()
        self.assertEqual(report["product_ids"], ["mock_product_002"])
        expected = [n for n in get_news(self.data, category="interest_rate") if "利率" in " ".join([n["title"], n.get("publisher") or "", *[p["text"] for p in n["summary_points"]]])]
        self.assertEqual(report["counts"]["news"], len(expected))
        self.assertIn("獨立篩選", report["markdown"])
        self.assertIn("並非資料不存在", report["markdown"])

    def test_empty_selection_intersection_stays_empty(self):
        self.state.update(filter_company_ids=["mock_company_001"], comparison_product_ids=["mock_product_006"])
        report = self.build()
        self.assertEqual(report["product_ids"], [])
        self.assertEqual(report["counts"]["rates"], 0)
        self.assertEqual(report["counts"]["rag_answers"], 0)

    def test_rag_scope_uses_retrieval_and_recorded_relationship(self):
        answer = self.state["rag_chat_messages"][1]
        self.state["comparison_product_ids"] = answer["product_ids"]
        self.assertEqual(self.build()["counts"]["rag_answers"], 1)
        for change in [{"product_ids": []}, {"retrieved_evidence": []}, {"citations": ["mock_evidence_014"]}]:
            original = deepcopy(answer)
            answer.update(change)
            self.assertEqual(self.build()["counts"]["rag_answers"], 0)
            answer.clear()
            answer.update(original)
        # 引用本身合法，但當次其他檢索結果超出商品範圍，仍須排除。
        extra = deepcopy(MockRetriever(self.data).search("宣告利率", 100))
        answer["retrieved_evidence"].extend(extra)
        answer["product_ids"] = sorted({p for r in answer["retrieved_evidence"] for p in r["product_ids"]})
        self.assertEqual(self.build()["counts"]["rag_answers"], 0)

    def test_rag_stale_excerpt_or_location(self):
        for key, value in [("excerpt", "另一版本的原文"), ("document_version", "old"), ("source_title", "另一來源")]:
            data, state = fixture()
            state["rag_chat_messages"][1]["retrieved_evidence"][0][key] = value
            report = build_markdown_report(data, state, generated_at=NOW)
            self.assertEqual(report["counts"]["rag_answers"], 0)
            self.assertNotIn("既有 RAG 回答", report["markdown"])

    def test_rag_failure_never_exports_raw_claim(self):
        for status in ["error", "validation_failed", "insufficient_data"]:
            self.state["rag_chat_messages"][1].update(status=status, content="保證獲利99%")
            text = self.build()["markdown"]
            self.assertNotIn("保證獲利99%", text)
            self.assertIn("不收錄模型結論", text)

    def test_verbatim_fences_and_markdown_escaping(self):
        raw = "第一行\n```\n# 假標題 | [link](javascript:evil)\n<script>x</script>\n````\n原文結尾\n"
        self.data["evidence"][0]["excerpt"] = raw
        text = self.build()["markdown"]
        self.assertIn("`````text\n" + raw + "`````", text)
        self.assertEqual(text_block(raw), "`````text\n" + raw + "`````\n")
        escaped = cell("| [x](url) <script>\n中文")
        self.assertIn("\\|", escaped)
        self.assertIn("&lt;script&gt;", escaped)
        self.assertNotIn("[x](url)", escaped)
        self.assertIn("<br>", escaped)

    def test_user_text_is_not_markdown_structure(self):
        raw = "# 標題\n```\n![圖片](x)\n```"
        self.state["rag_chat_messages"][0]["content"] = raw
        self.assertIn(text_block(raw), self.build()["markdown"])

    def test_date_filters_and_invalid_ranges(self):
        self.state.update(market_date_from=date(2026,10,28), market_date_to=date(2026,10,30))
        self.assertEqual(self.build()["counts"]["news"], len(get_news(self.data, date_from=date(2026,10,28), date_to=date(2026,10,30))))
        self.state["market_date_to"] = date(2026,10,1)
        with self.assertRaises(ValueError):
            self.build()

    def test_fingerprint_tracks_relevant_state_only(self):
        first = report_fingerprint(self.data, self.state)
        self.state["GEMINI_API_KEY"] = "not exported"
        self.state["evidence_dialog_context"] = {"test": 1}
        self.assertEqual(first, report_fingerprint(self.data, self.state))
        self.assertNotIn("not exported", self.build()["markdown"])
        self.state["filter_keyword"] = "changed"
        self.assertNotEqual(first, report_fingerprint(self.data, self.state))


class ReportAppTests(unittest.TestCase):
    def setUp(self):
        self.data, self.state = fixture()
        loader = patch("ui.common.load_dataset", return_value=self.data)
        loader.start()
        self.addCleanup(loader.stop)
        self.rag_api = patch("services.rag_service.call_gemini_rag_api", side_effect=AssertionError("禁止 API"))
        self.rag_api.start()
        self.addCleanup(self.rag_api.stop)
        self.llm_api = patch("services.llm_service.call_gemini_api", side_effect=AssertionError("禁止 API"))
        self.llm_api.start()
        self.addCleanup(self.llm_api.stop)
        self.app = AppTest.from_file(str(APP), default_timeout=15).run()

    def switch(self, title):
        self.app._page_hash = next(k for k,p in self.app._registered_pages.items() if p["page_name"] == title)
        self.app.run()
        self.assertEqual(list(self.app.exception), [])

    def test_download_payload_mime_and_unchanged_state(self):
        for key,value in self.state.items():
            self.app.session_state[key] = deepcopy(value)
        self.app.run()
        before = deepcopy(self.app.session_state["rag_chat_messages"])
        import streamlit as st
        with patch("ui.report.st.download_button", wraps=st.download_button) as download:
            self.app.button(key="report_prepare").click().run()
            download_args = download.call_args.kwargs
        self.assertEqual(list(self.app.exception), [])
        prepared = self.app.session_state["report_prepared"]
        self.assertIn("既有 RAG 回答", prepared["data"].decode("utf-8"))
        self.assertTrue(prepared["filename"].endswith(".md"))
        button = self.app.get("download_button")[0]
        self.assertEqual(button.proto.label, "下載 UTF-8 Markdown")
        self.assertIn("report_download", button.proto.id)
        # AppTest 不提供原生下載 click API；檢查真實元件及傳入的 bytes/MIME。
        self.assertTrue(button.proto.url)
        self.assertEqual(download_args["data"], prepared["data"])
        self.assertEqual(download_args["mime"], "text/markdown; charset=utf-8")
        self.assertEqual(download_args["on_click"], "ignore")
        self.app.run()
        self.assertEqual(self.app.session_state["rag_chat_messages"], before)
        self.assertEqual(self.app.session_state["filter_period_month"], "2026-10")
        self.assertEqual(self.app.session_state["report_prepared"]["data"], prepared["data"])

    def test_filter_change_invalidates_download_and_snapshot_clears_ai(self):
        self.app.session_state["gemini_dashboard_analysis"] = deepcopy(self.state["gemini_dashboard_analysis"])
        self.app.button(key="report_prepare").click().run()
        self.assertEqual(len(self.app.get("download_button")), 1)
        self.app.text_input(key="filter_keyword").set_value("無此商品").run()
        self.assertEqual(len(self.app.get("download_button")), 0)
        self.app.button(key="report_prepare").click().run()
        self.data["dataset_id"] = "mock_changed"
        self.app.run()
        self.assertNotIn("gemini_dashboard_analysis", self.app.session_state)
        self.assertNotIn("report_prepared", self.app.session_state)
        self.assertEqual(len(self.app.get("download_button")), 0)

    def test_export_preserves_evidence_request_and_navigation(self):
        self.app.button(key="report_prepare").click().run()
        prepared = self.app.session_state["report_prepared"]["data"]
        product = self.app.session_state["selected_product_id"]
        self.app.button(key=f"evidence_open_product_{product}").click().run()
        self.assertIn("evidence_dialog_context", self.app.session_state)
        self.assertEqual(self.app.session_state["report_prepared"]["data"], prepared)
        self.app.button(key="evidence_close").click().run()
        for title in ["總覽儀表板", "市場情報追蹤", "AI 分析與 RAG 助手"]:
            self.switch(title)
            self.assertIn("report_prepare", [b.key for b in self.app.button])
            self.assertEqual(self.app.session_state["report_prepared"]["data"], prepared)

    def test_new_gemini_and_rag_results_bound_to_snapshot(self):
        self.switch("總覽儀表板")
        self.app.radio(key="dashboard_insight_mode").set_value("gemini").run()
        with patch("ui.dashboard.run_ai_analysis", return_value={k:v for k,v in self.state["gemini_dashboard_analysis"].items() if k != "dataset_id"}):
            self.app.button(key="dashboard_btn_run_llm").click().run()
        self.assertEqual(self.app.session_state["gemini_dashboard_analysis"]["dataset_id"], self.data["dataset_id"])
        self.switch("AI 分析與 RAG 助手")
        answer = self.state["rag_chat_messages"][1]
        with patch("ui.assistant.answer_rag_query", return_value=dict(answer, answer=answer["content"])):
            self.app.chat_input[0].set_value("實際問答").run()
        messages = self.app.session_state["rag_chat_messages"]
        self.assertTrue(all(m["dataset_id"] == self.data["dataset_id"] for m in messages))
        self.assertEqual(messages[1]["product_ids"], answer["product_ids"])
        self.app.button(key="report_prepare").click().run()
        self.assertEqual(self.app.session_state["report_prepared"]["counts"]["rag_answers"], 1)


if __name__ == "__main__":
    unittest.main()
