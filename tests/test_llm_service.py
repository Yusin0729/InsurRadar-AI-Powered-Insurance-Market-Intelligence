"""E1 階段：Gemini 智慧分析服務與輸出驗證單元測試。

主要用途與功能：
1. 驗證 LLM 上下文資料組裝與 Prompt 載入。
2. 驗證 JSON 結構、格式錯誤、缺失欄位之防護。
3. 驗證幽靈商品 ID 與懸空證據 ID 之嚴格剔除。
4. 驗證數值一致性檢驗（比對陳述利率與實際利率）。
5. 驗證 API Key 缺失、API 呼叫失敗、額度超額與資料不足之錯誤處理。
6. 驗證 Streamlit 前端整合、Session State 快取與避免 rerun 重複呼叫。

外部套件：
- unittest: Python 內建單元測試框架。
- unittest.mock: 測試替身（Mock 與 Patch），避免消耗真實 API 額度。
- streamlit.testing.v1.AppTest: Streamlit 整合測試。
"""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from services.data_service import load_dataset
from services.llm_service import (
    DEFAULT_MODEL,
    LLMAPIError,
    LLMConfigError,
    LLMValidationError,
    clean_json_text,
    get_gemini_config,
    load_analysis_prompt,
    prepare_analysis_context,
    run_ai_analysis,
    validate_and_sanitize_insights,
)
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


class LLMServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.dataset = load_dataset()

    def test_load_prompt_and_prepare_context(self):
        """驗證 Prompt 載入與結構化上下文組裝。"""
        prompt = load_analysis_prompt()
        self.assertIn("{products_context}", prompt)
        self.assertIn("{rates_context}", prompt)
        self.assertIn("{evidence_context}", prompt)

        context = prepare_analysis_context(self.dataset)
        self.assertIn("mock_product_001", context["valid_product_ids"])
        self.assertIn("mock_evidence_001", context["valid_evidence_ids"])
        self.assertTrue(context["is_mock"])
        self.assertIn("mock_product_001", context["rates_lookup"])

    def test_clean_json_text(self):
        """驗證 Markdown 標記清理功能。"""
        self.assertEqual(clean_json_text('```json\n[{"a": 1}]\n```'), '[{"a": 1}]')
        self.assertEqual(clean_json_text('```\n[{"a": 1}]\n```'), '[{"a": 1}]')
        self.assertEqual(clean_json_text('[{"a": 1}]'), '[{"a": 1}]')

    def test_validate_and_sanitize_valid_json(self):
        """測試正常的 JSON 輸出驗證與欄位規範化。"""
        context = prepare_analysis_context(self.dataset)
        valid_json = json.dumps([
            {
                "insight_id": "llm_insight_001",
                "title": "模擬商品宣告利率比較",
                "statement": "模擬甲人壽一號商品宣告利率為 3.1%，二號商品宣告利率為 3.2%，兩者適用不同繳費年期方案。",
                "product_ids": ["mock_product_001", "mock_product_002"],
                "evidence_ids": ["mock_evidence_001", "mock_evidence_002"],
            }
        ])
        accepted, rejected = validate_and_sanitize_insights(
            valid_json, self.dataset, context["rates_lookup"]
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 0)
        item = accepted[0]
        self.assertEqual(item["insight_id"], "llm_insight_001")
        self.assertEqual(item["generation_mode"], "llm")
        self.assertEqual(item["conclusion_review_status"], "unreviewed")
        self.assertIsNone(item["conclusion_reviewed_by"])
        self.assertIsNone(item["conclusion_reviewed_at"])
        self.assertTrue(item["is_mock"])

    def test_validate_bad_json_raises_validation_error(self):
        """測試格式損毀之 JSON 應正確拋出 LLMValidationError。"""
        context = prepare_analysis_context(self.dataset)
        with self.assertRaises(LLMValidationError):
            validate_and_sanitize_insights("Invalid json string {{{", self.dataset, context["rates_lookup"])
        with self.assertRaises(LLMValidationError):
            validate_and_sanitize_insights('{"not": "a list"}', self.dataset, context["rates_lookup"])

    def test_validate_missing_citation_rejected(self):
        """測試引用不存在的 evidence_id 應被拒絕。"""
        context = prepare_analysis_context(self.dataset)
        ghost_json = json.dumps([
            {
                "title": "幽靈證據測試",
                "statement": "這是一筆具有無效引用的觀察。",
                "product_ids": ["mock_product_001"],
                "evidence_ids": ["non_existent_evidence_999"],
            }
        ])
        accepted, rejected = validate_and_sanitize_insights(ghost_json, self.dataset, context["rates_lookup"])
        self.assertEqual(len(accepted), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("不存在的證據 ID", rejected[0]["reasons"][0])

    def test_validate_missing_product_rejected(self):
        """測試引用不存在的 product_id 應被拒絕。"""
        context = prepare_analysis_context(self.dataset)
        ghost_json = json.dumps([
            {
                "title": "幽靈商品測試",
                "statement": "這是一筆具有無效商品的觀察。",
                "product_ids": ["non_existent_product_999"],
                "evidence_ids": ["mock_evidence_001"],
            }
        ])
        accepted, rejected = validate_and_sanitize_insights(ghost_json, self.dataset, context["rates_lookup"])
        self.assertEqual(len(accepted), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("不存在的商品 ID", rejected[0]["reasons"][0])

    def test_validate_rate_mismatch_rejected(self):
        """測試陳述中宣稱之宣告利率與實際紀錄不合時被拒絕。"""
        context = prepare_analysis_context(self.dataset)
        mismatch_json = json.dumps([
            {
                "title": "利率幻覺測試",
                "statement": "該保單宣告利率高達 9.99%，顯著優於同業。",  # 9.99% 與資料庫中任何紀錄皆不符
                "product_ids": ["mock_product_001"],
                "evidence_ids": ["mock_evidence_001"],
            }
        ])
        accepted, rejected = validate_and_sanitize_insights(mismatch_json, self.dataset, context["rates_lookup"])
        self.assertEqual(len(accepted), 0)
        self.assertEqual(len(rejected), 1)
        self.assertTrue(any("未在關聯商品的實際利率紀錄中找到吻合值" in r for r in rejected[0]["reasons"]))

    def test_run_analysis_insufficient_data(self):
        """測試資料集為空時回傳 insufficient_data 狀態。"""
        empty_dataset = {"data_mode": "mock", "products": [], "rates": [], "news": [], "evidence": []}
        res = run_ai_analysis(empty_dataset)
        self.assertEqual(res["status"], "insufficient_data")
        self.assertIn("無商品或證據資料", res["error_message"])

    def test_run_analysis_missing_api_key(self):
        """測試未設定 API Key 時回報明確錯誤，不捏造假分析。"""
        with patch("services.llm_service.get_gemini_config", return_value={"api_key": None, "model": "gemini-2.5-flash"}):
            res = run_ai_analysis(self.dataset, api_key=None)
            self.assertEqual(res["status"], "error")
            self.assertIn("尚未設定 GEMINI_API_KEY", res["error_message"])
            self.assertEqual(res["insights"], [])

    def test_run_analysis_api_failure(self):
        """測試 API 呼叫拋出異常（如配額超額或連線逾時）時回報明確錯誤。"""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("ResourceExhausted: Quota exceeded")

        res = run_ai_analysis(self.dataset, api_key="fake-test-key", client=mock_client)
        self.assertEqual(res["status"], "error")
        self.assertIn("Quota exceeded", res["error_message"])
        self.assertEqual(res["insights"], [])

    def test_run_analysis_with_mock_response_success(self):
        """測試提供合格 Mock Response 時完整管線執行成功。"""
        mock_json = json.dumps([
            {
                "insight_id": "llm_insight_001",
                "title": "市場利率動態觀察",
                "statement": "模擬甲人壽一號商品宣告利率為 3.1%，在展示資料集中具備明確條件定位。",
                "product_ids": ["mock_product_001"],
                "evidence_ids": ["mock_evidence_001"],
            }
        ])
        res = run_ai_analysis(self.dataset, mock_response=mock_json)
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["insights"]), 1)
        self.assertEqual(res["insights"][0]["title"], "市場利率動態觀察")
        self.assertEqual(res["insights"][0]["generation_mode"], "llm")
        self.assertTrue(res["is_mock_data"])


class LLMStreamlitIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.dataset = load_dataset()

    def switch_to(self, app, title):
        pages = app._registered_pages
        app._page_hash = next(key for key, page in pages.items() if page["page_name"] == title)
        app.run()
        self.assertEqual([item.message for item in app.exception], [])

    def test_app_loads_and_retains_existing_features(self):
        """驗證既有四大頁面與資料服務未受破壞。"""
        app = AppTest.from_file(str(APP_PATH), default_timeout=15).run()
        self.assertEqual([item.message for item in app.exception], [])
        self.assertIn("競品比較庫", [item.value for item in app.title])

    def test_dashboard_gemini_mode_and_rerun_caching(self):
        """測試 Dashboard 切換至 Gemini 模式、產生分析後 rerun 不重複呼叫 API。"""
        app = AppTest.from_file(str(APP_PATH), default_timeout=15).run()
        self.switch_to(app, "總覽儀表板")

        # 切換觀察來源為 Gemini 模式
        app.radio(key="dashboard_insight_mode").set_value("gemini").run()
        self.assertEqual([item.message for item in app.exception], [])

        # 模擬呼叫計數器，確認點擊產生分析
        mock_analysis_result = {
            "status": "success",
            "insights": [
                {
                    "insight_id": "llm_insight_001",
                    "title": "Gemini 測試觀察標題",
                    "statement": "模擬甲人壽一號商品宣告利率為 3.1%，具備市場競爭力。",
                    "product_ids": ["mock_product_001"],
                    "evidence_ids": ["mock_evidence_001"],
                    "generation_mode": "llm",
                    "is_mock": True,
                    "conclusion_review_status": "unreviewed",
                    "conclusion_reviewed_by": None,
                    "conclusion_reviewed_at": None,
                }
            ],
            "rejected_insights": [],
            "error_message": None,
            "model": "gemini-2.5-flash",
            "created_at": "2026-10-30T10:00:00Z",
            "is_mock_data": True,
        }

        call_counter = {"count": 0}

        def mock_run_fn(*args, **kwargs):
            call_counter["count"] += 1
            return mock_analysis_result

        with patch("ui.dashboard.run_ai_analysis", side_effect=mock_run_fn):
            # 點擊「產生 AI 分析」
            app.button(key="dashboard_btn_run_llm").click().run()
            self.assertEqual(call_counter["count"], 1)
            self.assertEqual([item.message for item in app.exception], [])

            # 再次觸發 rerun（例如使用者點擊其他元件或重新渲染）
            app.run()
            # 驗證計數器維持 1，未重複呼叫 API
            self.assertEqual(call_counter["count"], 1)

            # 驗證畫面包含生成的分析標題
            all_text = " ".join(item.value for item in app.markdown)
            self.assertIn("Gemini 測試觀察標題", all_text)

            # 點擊檢視觀察證據按鈕，驗證共用證據彈窗能正常開啟
            evidence_btn_key = "evidence_open_gemini_insight_llm_insight_001"
            self.assertIn(evidence_btn_key, [b.key for b in app.button])
            app.button(key=evidence_btn_key).click().run()
            self.assertEqual([item.message for item in app.exception], [])
            # 驗證視窗已叫起
            self.assertIn("evidence_dialog_context", app.session_state)


if __name__ == "__main__":
    unittest.main()

