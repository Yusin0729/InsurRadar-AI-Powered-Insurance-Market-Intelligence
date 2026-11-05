"""Gemini 暫時性錯誤重試與中文提示測試；不呼叫任何 API。

執行：python -B -m unittest discover -s tests -p test_gemini_retry.py -v
外部套件：無。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import gemini_retry as gr
from services.rag_service import call_gemini_rag_api


class Busy(Exception):
    code = 503


class RetryTest(unittest.TestCase):
    def test_transient_detection_by_code_message_and_cause(self):
        self.assertTrue(gr.is_transient(Busy("x")))
        self.assertTrue(gr.is_transient(RuntimeError("429 RESOURCE_EXHAUSTED")))
        wrapped = RuntimeError("Gemini API 呼叫過程中發生錯誤")
        wrapped.__cause__ = Busy("busy")
        self.assertTrue(gr.is_transient(wrapped))
        self.assertFalse(gr.is_transient(ValueError("API key not valid")))

    def test_retries_then_succeeds(self):
        calls, waits = [], []

        def call():
            calls.append(1)
            if len(calls) < 3:
                raise Busy("503 UNAVAILABLE")
            return "ok"
        self.assertEqual(gr.with_retry(call, sleep=waits.append), "ok")
        self.assertEqual((len(calls), waits), (3, list(gr.ONLINE_DELAYS)))

    def test_gives_up_after_delays_and_skips_permanent_errors(self):
        calls = []

        def busy():
            calls.append(1)
            raise Busy("503")
        with self.assertRaises(Busy):
            gr.with_retry(busy, sleep=lambda _: None)
        self.assertEqual(len(calls), len(gr.ONLINE_DELAYS) + 1)

        calls.clear()

        def bad_key():
            calls.append(1)
            raise ValueError("API key not valid")
        with self.assertRaises(ValueError):
            gr.with_retry(bad_key, sleep=lambda _: None)
        self.assertEqual(len(calls), 1)

    def test_friendly_message(self):
        self.assertEqual(gr.friendly_message(Busy("503 high demand"), "失敗："), gr.BUSY_MESSAGE)
        self.assertEqual(gr.friendly_message(ValueError("bad key"), "失敗："), "失敗：bad key")


class RagCallTest(unittest.TestCase):
    def test_rag_call_retries_busy_model(self):
        attempts = []

        def generate_content(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise Busy("503 UNAVAILABLE")
            return SimpleNamespace(text='{"answer": "ok", "citations": [], "has_sufficient_evidence": false}')
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        with patch("services.gemini_retry.time.sleep"):
            self.assertIn("ok", call_gemini_rag_api("p", api_key="k", model="m", client=client))
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
