import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from artboard_engine import assistant
from artboard_engine.models import EngineError


ANALYSIS_JSON = json.dumps({
    "input_type": "flat_artwork",
    "regions": [],
    "issues": [],
    "suggested_tracing": "spline",
    "requires_manual_reconstruction": False,
    "text_observations": [],
})


class AssistantFallbackTests(unittest.TestCase):
    def test_uses_gpt_first_then_gemini_when_primary_fails(self):
        calls = []

        def create_completion(**kwargs):
            calls.append(kwargs)
            if kwargs["model"] == "openai/gpt-6.1-sol-pro":
                raise RuntimeError("simulated provider failure")
            return SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=ANALYSIS_JSON))
            ])

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create_completion)
            )
        )

        with (
            patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "test-openrouter-key",
                "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
            }, clear=True),
            patch.object(assistant, "OpenAI", return_value=fake_client) as openai_client,
        ):
            result = assistant.analyze_with_ai(Image.new("RGB", (32, 32), "white"))

        self.assertEqual(
            [call["model"] for call in calls],
            ["openai/gpt-6.1-sol-pro", "google/gemini-3.1-pro-preview"],
        )
        self.assertTrue(all(call["max_tokens"] == 8192 for call in calls))
        self.assertEqual(result["provider"], "openrouter")
        self.assertEqual(result["model"], "google/gemini-3.1-pro-preview")
        self.assertTrue(result["fallback_used"])
        self.assertTrue(result["requires_operator_confirmation"])
        self.assertFalse(result["changes_applied"])
        openai_client.assert_called_once_with(
            api_key="test-openrouter-key",
            base_url="https://openrouter.ai/api/v1",
            timeout=90,
            max_retries=1,
        )

    def test_primary_success_does_not_call_fallback(self):
        calls = []

        def create_completion(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=ANALYSIS_JSON))
            ])

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create_completion)
            )
        )

        with (
            patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "test-openrouter-key",
            }, clear=True),
            patch.object(assistant, "OpenAI", return_value=fake_client),
        ):
            result = assistant.analyze_with_ai(Image.new("RGB", (32, 32), "white"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["model"], "openai/gpt-6.1-sol-pro")
        self.assertFalse(result["fallback_used"])

    def test_missing_provider_configuration_fails_explicitly(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(assistant.ai_configured())
            with self.assertRaises(EngineError) as raised:
                assistant.analyze_with_ai(Image.new("RGB", (8, 8), "white"))
        self.assertEqual(raised.exception.code, "AI_NOT_CONFIGURED")


if __name__ == "__main__":
    unittest.main()
