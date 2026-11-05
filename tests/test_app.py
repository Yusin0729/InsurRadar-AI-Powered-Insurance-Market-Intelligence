"""1A～E2 回歸、情報篩選與 RAG 助手互動測試；外部套件：streamlit、jsonschema。"""

from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch
import warnings

from streamlit.testing.v1 import AppTest

from services.data_service import (
    DatasetLoadError, get_declared_rates, get_evidence, get_insights,
    get_investigations, get_news, get_products, load_dataset, load_schema,
)
from services.validation import validate_dataset, validate_insight_evidence
from ui.comparison import comparison_rows
from ui.dashboard import dashboard_data, ordered_news
from ui.market import filtered_news


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def mock_dataset():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_dataset()


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.dataset = mock_dataset()
        self.loader = patch("ui.common.load_dataset", return_value=self.dataset)
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.app = AppTest.from_file(str(APP_PATH), default_timeout=15).run()
        self.assert_loaded()

    def assert_loaded(self):
        self.assertEqual([item.message for item in self.app.exception], [])

    def switch_to(self, title):
        # AppTest.switch_page 僅支援檔案頁。此一處使用 1.66.1 的真實導覽登錄
        # hash 來切換 callable 頁，不替換 st.navigation 或頁面內容。
        pages = self.app._registered_pages
        self.app._page_hash = next(key for key, page in pages.items() if page["page_name"] == title)
        self.app.run()
        self.assert_loaded()

    def listed_ids(self):
        return list(self.app.dataframe[0].value["商品 ID"])

    def messages(self, category):
        return "\n".join(item.value for item in getattr(self.app, category))


class AppTests(AppTestCase):
    def test_four_real_navigation_pages_and_mock_notice(self):
        expected = ["總覽儀表板", "競品比較庫", "市場情報追蹤", "AI 分析與 RAG 助手"]
        self.assertCountEqual([page["page_name"] for page in self.app._registered_pages.values()], expected)
        for title in expected:
            with self.subTest(title=title):
                self.switch_to(title)
                self.assertIn(title, [item.value for item in self.app.title])
                self.assertIn(self.dataset["mock_notice"], self.messages("warning"))
                self.assertIn("Mock Data／模擬資料", self.messages("warning"))
                self.assertIn(self.dataset["as_of"][:10], self.messages("caption"))

    def test_company_filter_uses_company_id(self):
        company_id = self.dataset["products"][0]["company_id"]
        self.app.multiselect(key="filter_company_ids").set_value([company_id]).run()
        expected = get_products(self.dataset, company_ids=[company_id])
        self.assertEqual(self.listed_ids(), [item["product_id"] for item in expected])

    def test_term_and_age_must_match_same_rule(self):
        self.app.selectbox(key="filter_payment_term_years").set_value(6).run()
        self.app.checkbox(key="filter_issue_age_enabled").check().run()
        self.app.number_input(key="filter_issue_age").set_value(65).run()
        expected = get_products(self.dataset, payment_term_years=6, issue_age=65)
        self.assertEqual(self.listed_ids(), [item["product_id"] for item in expected])
        self.assertNotIn("mock_product_001", self.listed_ids())
        self.assertNotIn("mock_product_006", self.listed_ids())

    def test_zero_term_is_single_payment(self):
        self.app.selectbox(key="filter_payment_term_years").set_value(0).run()
        self.assertEqual(self.listed_ids(), [item["product_id"] for item in get_products(self.dataset, payment_term_years=0)])

    def test_age_zero_is_not_unset(self):
        self.app.checkbox(key="filter_issue_age_enabled").check().run()
        self.app.number_input(key="filter_issue_age").set_value(0).run()
        self.assertEqual(self.listed_ids(), [item["product_id"] for item in get_products(self.dataset, issue_age=0)])

    def test_keyword_and_empty_results_clear_selection(self):
        name = self.dataset["products"][1]["product_name"]
        self.app.text_input(key="filter_keyword").set_value(name).run()
        self.assertEqual(self.listed_ids(), [self.dataset["products"][1]["product_id"]])
        self.app.text_input(key="filter_keyword").set_value("無此商品-xyz").run()
        self.assertIn("沒有符合條件的商品", self.messages("info"))
        self.assertEqual(len(self.app.dataframe), 0)
        self.assertIsNone(self.app.session_state["selected_product_id"])
        self.assertEqual(self.app.session_state["selected_evidence_ids"], [])

    def test_selection_and_comparison_use_product_ids_after_reorder(self):
        selected_id = self.dataset["products"][1]["product_id"]
        self.app.selectbox(key="_selected_product_id").set_value(selected_id).run()
        self.app.multiselect(key="_comparison_product_ids").set_value([selected_id]).run()
        self.dataset["products"].reverse()
        self.app.run()
        self.assert_loaded()
        self.assertEqual(self.app.session_state["selected_product_id"], selected_id)
        self.assertEqual(self.app.selectbox(key="_selected_product_id").value, selected_id)
        self.assertEqual(set(self.app.dataframe[1].value["商品 ID"]), {selected_id})
        selected = next(item for item in self.dataset["products"] if item["product_id"] == selected_id)
        self.assertIn(selected["product_name"], self.messages("markdown"))
        self.assertTrue(set(selected["evidence_ids"]).issubset(self.app.session_state["selected_evidence_ids"]))

    def test_selection_filters_and_comparison_survive_navigation(self):
        selected_id = self.dataset["products"][1]["product_id"]
        self.app.selectbox(key="_selected_product_id").set_value(selected_id).run()
        self.app.multiselect(key="_comparison_product_ids").set_value([selected_id]).run()
        self.app.selectbox(key="filter_payment_term_years").set_value(6).run()
        self.switch_to("市場情報追蹤")
        self.switch_to("競品比較庫")
        self.assertEqual(self.app.session_state["selected_product_id"], selected_id)
        self.assertEqual(self.app.multiselect(key="_comparison_product_ids").value, [selected_id])
        self.assertEqual(self.app.selectbox(key="filter_payment_term_years").value, 6)

    def test_filter_removing_selected_product_clears_id_and_evidence(self):
        first = self.dataset["products"][0]
        other = self.dataset["products"][3]
        self.app.selectbox(key="_selected_product_id").set_value(first["product_id"]).run()
        self.app.multiselect(key="filter_company_ids").set_value([other["company_id"]]).run()
        self.assertIsNone(self.app.session_state["selected_product_id"])
        self.assertIsNone(self.app.selectbox(key="_selected_product_id").value)
        self.assertEqual(self.app.session_state["selected_evidence_ids"], [])

    def test_null_rate_and_applicability_remain_visible(self):
        self.app.selectbox(key="filter_payment_term_years").set_value(6).run()
        frame = self.app.dataframe[1].value
        missing = frame[frame["商品 ID"] == "mock_product_006"].iloc[0]
        self.assertEqual(missing["宣告利率"], "資料不足")
        self.assertEqual(missing["適用條件"], "資料待補")
        restricted = frame[frame["商品 ID"] == "mock_product_001"].iloc[0]
        self.assertIn("不適用同商品六年", restricted["適用條件"])
        self.assertEqual(set(frame["宣告利率月份"]), {"2026-10"})

    def test_month_exact_match_does_not_carry_forward(self):
        prior = deepcopy(self.dataset["rates"][0])
        prior.update(rate_id="mock_rate_previous", period_month="2026-09", rate_percent=2.75,
                     effective_from="2026-09-01", effective_to="2026-09-30")
        self.dataset["rates"].append(prior)
        self.app.run()
        self.app.selectbox(key="filter_period_month").set_value("2026-09").run()
        frame = self.app.dataframe[1].value
        self.assertEqual(set(frame["宣告利率月份"]), {"2026-09"})
        self.assertEqual(list(frame["宣告利率"]).count("2.75%"), 1)
        self.assertEqual(list(frame["宣告利率"]).count("資料不足"), 5)
        self.assertNotIn("3.20%", list(frame["宣告利率"]))
        self.app.selectbox(key="filter_period_month").set_value("2026-10").run()
        self.assertIn("3.20%", list(self.app.dataframe[1].value["宣告利率"]))

    def test_no_rates_keeps_products_visible(self):
        self.dataset["rates"] = []
        self.app.run()
        self.assert_loaded()
        self.assertEqual(len(self.listed_ids()), len(self.dataset["products"]))
        self.assertTrue(self.app.selectbox(key="filter_period_month").disabled)
        self.assertEqual(set(self.app.dataframe[1].value["宣告利率"]), {"資料不足"})

    def test_empty_dataset_loads_without_crashing(self):
        for collection in ("products", "rates", "evidence", "news", "insights", "investigations"):
            self.dataset[collection] = []
        self.app.run()
        self.assert_loaded()
        self.assertIn("沒有符合條件的商品", self.messages("info"))

    def test_reset_filters(self):
        self.app.text_input(key="filter_keyword").set_value("無此商品-xyz").run()
        next(item for item in self.app.button if item.label == "重設篩選").click().run()
        self.assert_loaded()
        self.assertEqual(len(self.listed_ids()), len(self.dataset["products"]))
        self.assertEqual(self.app.text_input(key="filter_keyword").value, "")

    def test_load_error_is_user_visible(self):
        with patch("ui.common.load_dataset", side_effect=DatasetLoadError("測試載入失敗")):
            self.app.run()
        self.assert_loaded()
        self.assertIn("資料快照無法載入", self.messages("error"))
        self.assertEqual(len(self.app.dataframe), 0)

    def test_dashboard_kpis_use_full_snapshot_despite_comparison_filters(self):
        self.app.multiselect(key="filter_company_ids").set_value(["mock_company_001"]).run()
        self.app.selectbox(key="filter_payment_term_years").set_value(6).run()
        self.switch_to("總覽儀表板")
        values = {item.label: int(item.value) for item in self.app.metric}
        self.assertEqual(values, {"保險公司數": 5, "商品數": 6, "觀察數": 3, "未結案待追查數": 3})
        self.assertIn("統計範圍：完整資料快照", self.messages("info"))
        self.assertEqual(int(self.app.dataframe[0].value["商品數"].sum()), 6)
        self.assertEqual(self.app.multiselect(key="filter_company_ids").value, ["mock_company_001"])

    def test_dashboard_observation_refs_match_service(self):
        self.switch_to("總覽儀表板")
        for insight in get_insights(self.dataset):
            with self.subTest(insight=insight["insight_id"]):
                key = f"evidence_open_insight_{insight['insight_id']}"
                self.app.button(key=key).click().run()
                self.assert_loaded()
                context = self.app.session_state["evidence_dialog_context"]
                self.assertEqual(context["source_id"], insight["insight_id"])
                self.assertEqual(context["evidence_ids"], insight["evidence_ids"])
                self.assertIn("引用存在／有效：", self.messages("markdown"))
                self.assertIn("證據完整：", self.messages("markdown"))
                self.assertIn("證據人工複核：", self.messages("markdown"))
                self.assertIn("AI 結論複核：", self.messages("markdown"))
                self.assertEqual(len(self.app.success), 0)
                self.app.button(key="evidence_close").click().run()

    def test_evidence_dialog_close_reopen_rerun_keeps_product_state(self):
        selected_id = "mock_product_002"
        self.app.multiselect(key="filter_company_ids").set_value(["mock_company_001"]).run()
        self.app.selectbox(key="filter_payment_term_years").set_value(6).run()
        self.app.selectbox(key="_selected_product_id").set_value(selected_id).run()
        self.app.multiselect(key="_comparison_product_ids").set_value([selected_id]).run()
        expected_ids = list(self.app.session_state["selected_evidence_ids"])
        self.app.button(key=f"evidence_open_product_{selected_id}").click().run()
        self.assert_loaded()
        self.assertEqual(self.app.session_state["evidence_dialog_context"]["evidence_ids"], expected_ids)
        self.assertIn("AI 結論複核：不適用", self.messages("markdown"))
        self.app.button(key="evidence_close").click().run()
        self.assert_loaded()
        self.assertNotIn("evidence_dialog_context", self.app.session_state)
        self.assertEqual(self.app.session_state["selected_product_id"], selected_id)
        self.assertEqual(self.app.session_state["comparison_product_ids"], [selected_id])
        self.assertEqual(self.app.multiselect(key="filter_company_ids").value, ["mock_company_001"])
        self.assertEqual(self.app.selectbox(key="filter_payment_term_years").value, 6)
        self.app.button(key=f"evidence_open_product_{selected_id}").click().run()
        self.assertIn("_evidence_choice_id", [item.key for item in self.app.selectbox])
        self.app.run()
        self.assert_loaded()
        self.assertNotIn("_evidence_choice_id", [item.key for item in self.app.selectbox])
        self.assertEqual(self.app.session_state["selected_product_id"], selected_id)
        self.assertEqual(self.app.session_state["comparison_product_ids"], [selected_id])

    def test_page_switch_clears_dialog_without_clearing_selection(self):
        self.app.button(key="evidence_open_product_mock_product_001").click().run()
        self.switch_to("總覽儀表板")
        self.assertNotIn("evidence_dialog_context", self.app.session_state)
        self.switch_to("競品比較庫")
        self.assertEqual(self.app.session_state["selected_product_id"], "mock_product_001")
        self.assertNotIn("_evidence_choice_id", [item.key for item in self.app.selectbox])

    def test_product_evidence_missing_id_is_explicit(self):
        actual_lookup = get_evidence
        with patch("ui.evidence.get_evidence", side_effect=lambda data, eid: (
            None if eid == "mock_evidence_001" else actual_lookup(data, eid)
        )):
            self.app.button(key="evidence_open_product_mock_product_001").click().run()
        self.assert_loaded()
        self.assertIn("證據不存在：mock_evidence_001", self.messages("error"))
        self.assertEqual(self.app.session_state["evidence_dialog_context"]["evidence_ids"],
                         ["mock_evidence_001", "mock_evidence_007"])

    def test_incomplete_observation_evidence_is_not_verified(self):
        self.switch_to("總覽儀表板")
        self.app.button(key="evidence_open_insight_mock_insight_003").click().run()
        self.assert_loaded()
        self.assertEqual(self.app.session_state["active_evidence_id"], "mock_evidence_012")
        self.assertIn("證據待補欄位", self.messages("warning"))
        self.assertIn("原文片段待補", self.messages("text"))
        self.assertEqual(len(self.app.success), 0)
        self.assertNotIn("開啟來源網址", [item.label for item in self.app.button])

    def test_mock_confirmed_evidence_does_not_confirm_conclusion(self):
        self.switch_to("總覽儀表板")
        self.app.button(key="evidence_open_insight_mock_insight_001").click().run()
        self.assertIn("全數確認（模擬複核紀錄）", self.messages("markdown"))
        self.assertIn("AI 結論複核：模擬複核紀錄：待複核", self.messages("markdown"))
        self.assertEqual(len(self.app.success), 0)

    def test_mock_confirmed_conclusion_still_cannot_be_verified(self):
        insight = self.dataset["insights"][0]
        insight.update(conclusion_review_status="confirmed", conclusion_reviewed_by="模擬複核者",
                       conclusion_reviewed_at="2026-10-29T09:00:00+08:00")
        self.switch_to("總覽儀表板")
        self.assertEqual(len(self.app.success), 0)
        self.assertIn("不代表真實市場已驗證結論", self.messages("caption"))

    def test_dashboard_empty_sections(self):
        for collection in ("products", "rates", "news", "insights", "evidence", "investigations"):
            self.dataset[collection] = []
        self.switch_to("總覽儀表板")
        self.assertTrue(all(int(item.value) == 0 for item in self.app.metric))
        for message in ("目前沒有觀察資料", "目前沒有市場情報", "目前沒有商品與公司資料", "目前沒有待追查事項"):
            self.assertIn(message, self.messages("info"))

    def test_dataset_change_invalidates_old_dialog(self):
        self.app.button(key="evidence_open_product_mock_product_001").click().run()
        self.dataset["dataset_id"] = "mock_changed_snapshot"
        self.app.run()
        self.assert_loaded()
        self.assertNotIn("evidence_dialog_context", self.app.session_state)
        self.assertNotIn("_evidence_choice_id", [item.key for item in self.app.selectbox])


class Stage1DTests(AppTestCase):
    def market_ids(self):
        return [item.key.removeprefix("evidence_open_news_") for item in self.app.button
                if item.key and item.key.startswith("evidence_open_news_mock_news_")]

    def test_market_combined_filters_and_independent_reset(self):
        self.app.multiselect(key="filter_company_ids").set_value(["mock_company_005"]).run()
        self.switch_to("市場情報追蹤")
        self.assertEqual(len(self.market_ids()), 4)
        self.app.text_input(key="_market_keyword").set_value("不直接作優劣排名").run()
        self.app.selectbox(key="_market_category").set_value("interest_rate").run()
        self.app.multiselect(key="_market_company_ids").set_value(["mock_company_001"]).run()
        self.app.multiselect(key="_market_product_ids").set_value(["mock_product_002"]).run()
        self.app.date_input(key="_market_date_from").set_value(date(2026, 10, 28)).run()
        self.app.date_input(key="_market_date_to").set_value(date(2026, 10, 28)).run()
        self.assertEqual(self.market_ids(), ["mock_news_003"])
        self.app.multiselect(key="_market_company_ids").set_value(["mock_company_005"]).run()
        self.assertEqual(self.market_ids(), [])
        self.assertIn("沒有符合條件的市場情報", self.messages("info"))
        self.app.button(key="market_reset").click().run()
        self.assert_loaded()
        self.assertEqual(len(self.market_ids()), 4)
        self.assertIsNone(self.app.date_input(key="_market_date_from").value)
        self.assertEqual(self.app.session_state["filter_company_ids"], ["mock_company_005"])

    def test_market_dates_unknown_and_invalid_range(self):
        self.switch_to("市場情報追蹤")
        self.assertIn("mock_news_004", self.market_ids())
        self.app.date_input(key="_market_date_from").set_value(date(2026, 10, 27)).run()
        self.assertEqual(self.market_ids(), ["mock_news_003", "mock_news_002"])
        self.app.date_input(key="_market_date_to").set_value(date(2026, 10, 26)).run()
        self.assert_loaded()
        self.assertIn("起日不可晚於迄日", self.messages("warning"))
        self.assertEqual(self.market_ids(), [])

    def test_market_id_lists_use_or_and_obsolete_choices_are_removed(self):
        self.switch_to("市場情報追蹤")
        self.app.multiselect(key="_market_company_ids").set_value(
            ["mock_company_001", "mock_company_002"]).run()
        self.app.multiselect(key="_market_product_ids").set_value(
            ["mock_product_002", "mock_product_003"]).run()
        self.assertEqual(self.market_ids(), ["mock_news_003", "mock_news_002", "mock_news_001"])
        self.dataset["products"] = []
        self.app.run()
        self.assert_loaded()
        self.assertEqual(self.app.session_state["market_company_ids"], [])
        self.assertEqual(self.app.session_state["market_product_ids"], [])

    def test_new_pages_missing_lookup_and_incomplete_sources_are_explicit(self):
        self.switch_to("市場情報追蹤")
        actual_lookup = get_evidence
        with patch("ui.evidence.get_evidence", side_effect=lambda data, eid: (
            None if eid == "mock_evidence_015" else actual_lookup(data, eid)
        )):
            self.app.button(key="evidence_open_news_mock_news_003").click().run()
        self.assert_loaded()
        self.assertIn("證據不存在：mock_evidence_015", self.messages("error"))
        self.app.button(key="evidence_close").click().run()
        self.switch_to("AI 分析與 RAG 助手")
        with patch("ui.assistant.answer_rag_query", return_value={
            "status": "success", "answer": "待補證據測試", "citations": ["mock_evidence_012"]
        }):
            self.app.chat_input[0].set_value("待補利率").run()
        self.app.button(key="evidence_open_rag_answer_msg_1").click().run()
        self.assert_loaded()
        self.assertIn("證據待補欄位", self.messages("warning"))
        self.assertIn("原文片段待補", self.messages("text"))
        self.assertEqual(len(self.app.success), 0)

    def test_market_missing_metadata_and_empty_feed(self):
        self.dataset["news"][0].update(publisher=None, summary_points=[], company_ids=[], product_ids=[])
        self.switch_to("市場情報追蹤")
        self.assertIn("發布者待補", self.messages("caption"))
        self.assertIn("新聞摘要待補", self.messages("info"))
        self.assertIn("未提供商品關聯", self.messages("markdown"))
        self.assertIn("新聞來源網址待補", self.messages("caption"))
        self.dataset["news"] = []
        self.app.run()
        self.assert_loaded()
        self.assertIn("沒有符合條件的市場情報", self.messages("info"))

    def test_market_summary_and_article_citations(self):
        self.switch_to("市場情報追蹤")
        for news in get_news(self.dataset):
            for position, point in enumerate(news["summary_points"], 1):
                key = f"evidence_open_news_summary_{news['news_id']}_{position}"
                self.app.button(key=key).click().run()
                self.assert_loaded()
                self.assertEqual(self.app.session_state["evidence_dialog_context"]["evidence_ids"],
                                 point["evidence_ids"])
                self.app.button(key="evidence_close").click().run()
            self.app.button(key=f"evidence_open_news_{news['news_id']}").click().run()
            self.assertEqual(self.app.session_state["evidence_dialog_context"]["evidence_ids"], news["evidence_ids"])
            self.app.button(key="evidence_close").click().run()

    def test_assistant_answer_matches_original_and_citations(self):
        self.switch_to("AI 分析與 RAG 助手")
        self.assertFalse(self.app.chat_input[0].disabled)
        self.assertIn("語意支持性", self.messages("info"))
        for insight in get_insights(self.dataset):
            self.app.button(key="btn_clear_rag_chat").click().run()
            with patch("ui.assistant.answer_rag_query", return_value={
                "status": "success", "answer": insight["statement"],
                "citations": insight["evidence_ids"], "is_mock": True
            }):
                self.app.chat_input[0].set_value(insight["title"]).run()
            self.assertIn(insight["statement"], self.messages("markdown"))
            self.assertIn("、".join(insight["evidence_ids"]), self.messages("caption"))
            self.assertIn("AI 結論待人工複核", self.messages("caption"))
            self.assertEqual(len(self.app.success), 0)
            self.app.button(key="evidence_open_rag_answer_msg_1").click().run()
            self.assert_loaded()
            self.assertEqual(self.app.session_state["evidence_dialog_context"]["evidence_ids"], insight["evidence_ids"])
            self.app.button(key="evidence_close").click().run()

    def test_assistant_investigation_and_insight_buttons(self):
        self.switch_to("AI 分析與 RAG 助手")
        for kind, records, field in (("insight", get_insights(self.dataset), "insight_id"),
                                     ("investigation", get_investigations(self.dataset), "investigation_id")):
            for item in records:
                self.app.button(key=f"evidence_open_{kind}_{item[field]}").click().run()
                self.assert_loaded()
                self.assertEqual(self.app.session_state["evidence_dialog_context"]["source_id"], item[field])
                self.assertEqual(self.app.session_state["evidence_dialog_context"]["evidence_ids"], item["evidence_ids"])
                self.app.button(key="evidence_close").click().run()

    def test_four_pages_preserve_market_assistant_and_comparison_state(self):
        self.app.selectbox(key="_selected_product_id").set_value("mock_product_002").run()
        self.app.multiselect(key="_comparison_product_ids").set_value(["mock_product_002"]).run()
        self.switch_to("市場情報追蹤")
        self.app.text_input(key="_market_keyword").set_value("利率").run()
        self.app.multiselect(key="_market_company_ids").set_value(["mock_company_001"]).run()
        self.app.date_input(key="_market_date_from").set_value(date(2026, 10, 28)).run()
        self.app.button(key="evidence_open_news_mock_news_003").click().run()
        self.app.button(key="evidence_close").click().run()
        self.switch_to("AI 分析與 RAG 助手")
        self.app.session_state["rag_chat_messages"] = [{"role": "user", "content": "保留對話測試"}]
        self.app.run()
        self.switch_to("總覽儀表板")
        self.switch_to("市場情報追蹤")
        self.assertEqual(self.app.text_input(key="_market_keyword").value, "利率")
        self.assertEqual(self.app.multiselect(key="_market_company_ids").value, ["mock_company_001"])
        self.assertEqual(self.app.date_input(key="_market_date_from").value, date(2026, 10, 28))
        self.switch_to("AI 分析與 RAG 助手")
        self.assertEqual(self.app.session_state["rag_chat_messages"][0]["content"], "保留對話測試")
        self.switch_to("競品比較庫")
        self.assertEqual(self.app.session_state["selected_product_id"], "mock_product_002")
        self.assertEqual(self.app.session_state["comparison_product_ids"], ["mock_product_002"])

    def test_snapshot_change_resets_new_state(self):
        self.switch_to("市場情報追蹤")
        self.app.text_input(key="_market_keyword").set_value("無結果").run()
        self.switch_to("AI 分析與 RAG 助手")
        self.app.session_state["rag_chat_messages"] = [{"role": "user", "content": "保留對話測試"}]
        self.app.run()
        self.dataset["dataset_id"] = "mock_next_snapshot"
        self.app.run()
        self.assertEqual(self.app.session_state["rag_chat_messages"], [])
        self.switch_to("市場情報追蹤")
        self.assertEqual(self.app.text_input(key="_market_keyword").value, "")
        self.assertEqual(len(self.market_ids()), 4)

    def test_assistant_empty_data_and_removed_selection(self):
        self.dataset["insights"] = []
        self.dataset["investigations"] = []
        self.switch_to("AI 分析與 RAG 助手")
        self.assert_loaded()
        self.assertIn("目前沒有觀察資料", self.messages("info"))
        self.assertIn("目前沒有待追查事項", self.messages("info"))
        self.assertEqual(len(self.app.chat_message), 0)
        self.assertFalse(self.app.chat_input[0].disabled)
        self.dataset["evidence"] = []
        with patch("services.rag_service.call_gemini_rag_api") as api:
            self.app.chat_input[0].set_value("利率").run()
            api.assert_not_called()
        self.assertIn("目前未檢索到足夠依據", self.messages("markdown"))


class DemoCacheAppTests(AppTestCase):
    def test_cached_answer_is_labelled_and_citations_open(self):
        self.switch_to("AI 分析與 RAG 助手")
        self.assertIn("Mock 表示來源為模擬資料", self.messages("info"))
        evidence = self.dataset["evidence"][0]
        cached = {"status": "success", "answer": "快取回答內容", "citations": [evidence["evidence_id"]],
                  "retrieved_evidence": [{"evidence_id": evidence["evidence_id"], "excerpt": evidence["excerpt"],
                                          "score": 0.9, "source_title": evidence["source_title"]}],
                  "verification_passed": True, "is_mock": True, "created_at": "2026-11-05T10:00:00+00:00",
                  "from_cache": True, "cache_created_at": "2026-11-05T10:00:00+00:00",
                  "cache_model": "fake-model", "cache_reason": "fallback"}
        with patch("ui.assistant.answer_with_demo_cache", return_value=cached):
            self.app.chat_input[0].set_value("快取題目").run()
        self.assert_loaded()
        captions = self.messages("caption")
        self.assertIn("快取回答（預先產生於 2026-11-05 18:00（台北時間），模型 fake-model）", captions)
        self.assertIn("即時 Gemini 服務忙碌", captions)
        self.assertTrue(self.app.session_state["rag_chat_messages"][-1]["from_cache"])
        self.app.button(key="evidence_open_rag_answer_msg_1").click().run()
        self.assert_loaded()
        self.assertEqual(self.app.session_state["evidence_dialog_context"]["evidence_ids"], [evidence["evidence_id"]])

    def test_live_answer_has_no_cache_label(self):
        self.switch_to("AI 分析與 RAG 助手")
        with patch("ui.assistant.answer_with_demo_cache", return_value={
            "status": "success", "answer": "即時回答", "citations": [], "is_mock": True
        }):
            self.app.chat_input[0].set_value("即時題目").run()
        self.assertNotIn("快取回答", self.messages("caption"))


REAL_DATASET = Path(__file__).resolve().parents[1] / "data" / "real" / "dataset.json"


@unittest.skipUnless(REAL_DATASET.exists(), "真實快照不在版本庫（data/real 已排除），無檔案時略過")
class RealModeAppTests(unittest.TestCase):
    """真實快照四頁不可出現 Mock／模擬字樣或例外。"""

    def test_real_pages_have_no_mock_wording(self):
        with patch.dict("os.environ", {"INSURRADAR_DATASET_PATH": str(REAL_DATASET)}):
            app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
            self.assertEqual([item.message for item in app.exception], [])
            for key, page in app._registered_pages.items():
                app._page_hash = key
                app.run()
                with self.subTest(page=page["page_name"]):
                    self.assertEqual([item.message for item in app.exception], [])
                    texts = [str(item.value) for kind in ("title", "caption", "markdown", "info", "warning",
                                                          "error", "success", "text", "subheader")
                             for item in getattr(app, kind)]
                    self.assertEqual([t for t in texts if "Mock" in t or "模擬" in t], [])


class MarketDataTests(unittest.TestCase):
    def test_invalid_news_citations_still_fail_dataset_validation(self):
        dataset = mock_dataset()
        dataset["news"][0]["summary_points"][0]["evidence_ids"] = ["missing_evidence"]
        report = validate_dataset(dataset, load_schema())
        self.assertTrue(any(item["code"] == "unknown_reference" for item in report["errors"]))
        self.assertTrue(any(item["code"] == "summary_citation_scope" for item in report["errors"]))

    def test_service_filters_timezone_boundaries_and_keyword_without_mutation(self):
        dataset = mock_dataset()
        dataset["news"][0]["published_at"] = "2026-10-25T17:00:00Z"
        before = deepcopy(dataset)
        result = filtered_news(dataset, keyword="比較前須", category="product",
                               company_ids=["mock_company_001"], product_ids=["mock_product_002"],
                               date_from="2026-10-26", date_to="2026-10-26")
        self.assertEqual([item["news_id"] for item in result], ["mock_news_001"])
        self.assertEqual(dataset, before)
        self.assertEqual(filtered_news(dataset, keyword="無此字串"), [])
        self.assertEqual(len(filtered_news(dataset, keyword="  mOcK  ")), 4)
        self.assertEqual(len(filtered_news(dataset, date_to="2026-10-26")), 1)


class EvidenceComponentTests(unittest.TestCase):
    def test_multiple_evidence_switches_by_id_and_preserves_source(self):
        # AppTest 的元件層測試每次重繪 dialog；真實 fragment 與 Esc 由瀏覽器驗證。
        app = AppTest.from_string('''
import streamlit as st
from services.data_service import load_schema
from services.validation import validate_dataset
from ui.evidence import evidence_dialog
dataset = st.session_state["test_dataset"]
context = st.session_state.setdefault("evidence_dialog_context", {
    "dataset_id": dataset["dataset_id"], "source_kind": "insight", "source_id": "mock_insight_001",
    "evidence_ids": ["mock_evidence_007", "mock_evidence_008", "missing_evidence"],
})
evidence_dialog(dataset, context, validate_dataset(dataset, load_schema()))
''', default_timeout=15)
        app.session_state["test_dataset"] = mock_dataset()
        app.run()
        for evidence_id in ("mock_evidence_008", "missing_evidence", "mock_evidence_007"):
            app.selectbox(key="_evidence_choice_id").set_value(evidence_id).run()
            self.assertEqual([item.message for item in app.exception], [])
            self.assertEqual(app.session_state["active_evidence_id"], evidence_id)
            self.assertEqual(app.session_state["evidence_dialog_context"]["source_id"], "mock_insight_001")
            if evidence_id == "missing_evidence":
                self.assertTrue(any("證據不存在" in item.value for item in app.error))
            else:
                expected = get_evidence(app.session_state["test_dataset"], evidence_id)
                self.assertIn(expected["excerpt"], [item.value for item in app.text])

    def test_empty_evidence_list_is_visible(self):
        app = AppTest.from_string('''
import streamlit as st
from services.data_service import load_dataset, load_schema
from services.validation import validate_dataset
from ui.evidence import evidence_dialog
dataset = load_dataset()
context = {"dataset_id": dataset["dataset_id"], "source_kind": "product", "source_id": "test", "evidence_ids": []}
st.session_state["evidence_dialog_context"] = context
evidence_dialog(dataset, context, validate_dataset(dataset, load_schema()))
''', default_timeout=15).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("未提供證據 ID" in item.value for item in app.info))


class DashboardDataTests(unittest.TestCase):
    def test_statistics_are_recomputed_and_resolved_items_excluded(self):
        dataset = mock_dataset()
        before = deepcopy(dataset)
        self.assertEqual(dashboard_data(dataset)["kpis"]["未結案待追查數"], 3)
        self.assertEqual(dataset, before)
        dataset["investigations"][0]["status"] = "resolved"
        self.assertEqual(dashboard_data(dataset)["kpis"]["未結案待追查數"], 2)
        dataset["products"] = []
        self.assertEqual(dashboard_data(dataset)["kpis"]["商品數"], 0)
        self.assertEqual(dashboard_data(dataset)["kpis"]["保險公司數"], 0)

    def test_news_dates_ordered_without_filling_unknown_date(self):
        dataset = mock_dataset()
        dated, undated = ordered_news(get_news(dataset))
        self.assertEqual([item["news_id"] for item in dated], ["mock_news_003", "mock_news_002", "mock_news_001"])
        self.assertEqual([item["news_id"] for item in undated], ["mock_news_004"])
        self.assertIsNone(undated[0]["published_at"])


class ServiceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.dataset = mock_dataset()

    def test_original_snapshot_passes_validation(self):
        report = validate_dataset(self.dataset, load_schema())
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["warnings"])

    def test_read_interfaces_preserve_dataset_and_return_copies(self):
        before = deepcopy(self.dataset)
        products = get_products(self.dataset)
        rates = get_declared_rates(self.dataset, period_month="2026-10")
        self.assertEqual(get_products(self.dataset, company_ids=[]), [])
        self.assertEqual(get_declared_rates(self.dataset, product_ids=[]), [])
        self.assertTrue(get_news(self.dataset))
        self.assertTrue(get_insights(self.dataset))
        self.assertTrue(get_investigations(self.dataset))
        evidence = get_evidence(self.dataset, products[0]["evidence_ids"][0])
        self.assertIsNotNone(evidence)
        self.assertIsNone(get_evidence(self.dataset, "不存在的證據"))
        products[0]["product_name"] = "不應寫回"
        rates[0]["rate_percent"] = -999
        evidence["excerpt"] = "不應寫回"
        self.assertEqual(before, self.dataset)

    def test_mock_insights_and_missing_evidence_cannot_be_verified(self):
        for insight in get_insights(self.dataset):
            self.assertFalse(validate_insight_evidence(insight, self.dataset)["can_display_as_verified"])
        broken = deepcopy(self.dataset["insights"][0])
        broken["evidence_ids"] = ["不存在的證據"]
        self.assertFalse(validate_insight_evidence(broken, self.dataset)["references_valid"])

    def test_multiple_rate_conditions_are_separate_and_null_is_not_zero(self):
        product = self.dataset["products"][0]
        first = deepcopy(self.dataset["rates"][0])
        second = deepcopy(first)
        second.update(rate_id="mock_rate_other", rate_percent=0, applicable_conditions="另一條件")
        third = deepcopy(first)
        third.update(rate_id="mock_rate_null", rate_percent=None, applicable_conditions=None)
        rows = comparison_rows([product], [first, second, third], "2026-10")
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["宣告利率"] for row in rows], ["3.10%", "0.00%", "資料不足"])
        self.assertEqual(comparison_rows([product], [first], "2026-09")[0]["宣告利率"], "資料不足")


if __name__ == "__main__":
    unittest.main()
