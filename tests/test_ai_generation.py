import base64
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from artboard_engine import ai_generation
from artboard_engine.models import EngineError


def png_data_url(color=(20, 80, 140)):
    image = Image.new("RGB", (22, 31), color)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def response_for(url):
    return SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(images=[
            SimpleNamespace(image_url=SimpleNamespace(url=url)),
        ]))
    ])


class AIArtworkGenerationTests(unittest.TestCase):
    def test_generates_image_and_sends_requested_image_modality(self):
        calls = []
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: (calls.append(kwargs) or response_for(png_data_url()))
        )))
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True),
            patch.object(ai_generation, "OpenAI", return_value=fake_client),
        ):
            result = ai_generation.generate_panel(
                "Blue lightning jersey with geometric waves", "front",
            )

        self.assertEqual((result.width, result.height), (22, 31))
        self.assertEqual(result.model, ai_generation.DEFAULT_PRIMARY_MODEL)
        self.assertEqual(calls[0]["modalities"], ["image", "text"])
        prompt = calls[0]["messages"][0]["content"][0]["text"]
        self.assertIn("FRONT body panel", prompt)
        self.assertIn("Do not add folds", prompt)

    def test_back_generation_receives_front_reference(self):
        calls = []
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: (calls.append(kwargs) or response_for(png_data_url()))
        )))
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True),
            patch.object(ai_generation, "OpenAI", return_value=fake_client),
        ):
            ai_generation.generate_panel(
                "Abstract cyan panels", "back", reference_png=b"front-reference",
            )

        content = calls[0]["messages"][0]["content"]
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_reconstructs_flat_art_from_the_supplied_photo_and_labels_it_as_a_draft(self):
        calls = []
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: (calls.append(kwargs) or response_for(png_data_url()))
        )))
        source = Image.new("RGB", (128, 240), (30, 80, 150))
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True),
            patch.object(ai_generation, "OpenAI", return_value=fake_client),
        ):
            result = ai_generation.reconstruct_panel(source, "back")

        self.assertEqual(result.model, ai_generation.DEFAULT_PRIMARY_MODEL)
        self.assertEqual(calls[0]["modalities"], ["image", "text"])
        content = calls[0]["messages"][0]["content"]
        self.assertIn("BACK body", content[0]["text"])
        self.assertIn("Remove fabric texture, wrinkles, folds", content[0]["text"])
        self.assertIn("do not invent", content[0]["text"])
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_reconstruction_requires_openrouter_and_rejects_invalid_side(self):
        source = Image.new("RGB", (20, 30), "white")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EngineError) as missing:
                ai_generation.reconstruct_panel(source, "front")
        self.assertEqual(missing.exception.code, "AI_GENERATION_NOT_CONFIGURED")

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True):
            with self.assertRaises(EngineError) as invalid:
                ai_generation.reconstruct_panel(source, "sleeve")
        self.assertEqual(invalid.exception.code, "INVALID_PANEL_SIDE")

    def test_falls_back_when_primary_model_fails(self):
        calls = []

        def generate(**kwargs):
            calls.append(kwargs["model"])
            if len(calls) == 1:
                raise RuntimeError("primary unavailable")
            return response_for(png_data_url())

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=generate)))
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True),
            patch.object(ai_generation, "OpenAI", return_value=fake_client),
        ):
            result = ai_generation.generate_panel("Abstract cyan panels", "front")

        self.assertEqual(calls, [
            ai_generation.DEFAULT_PRIMARY_MODEL,
            ai_generation.DEFAULT_FALLBACK_MODEL,
        ])
        self.assertEqual(result.model, ai_generation.DEFAULT_FALLBACK_MODEL)

    def test_rejects_remote_image_urls_and_missing_provider(self):
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True),
            patch.object(ai_generation, "OpenAI", return_value=SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(
                    create=lambda **kwargs: response_for("https://untrusted.example/output.png")
                ))
            )),
        ):
            with self.assertRaises(EngineError) as raised:
                ai_generation.generate_panel("Abstract cyan panels", "front")
        self.assertEqual(raised.exception.code, "AI_GENERATION_FAILED")

        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ai_generation.ai_generation_configured())
            with self.assertRaises(EngineError) as missing:
                ai_generation.generate_panel("Abstract cyan panels", "front")
        self.assertEqual(missing.exception.code, "AI_GENERATION_NOT_CONFIGURED")


if __name__ == "__main__":
    unittest.main()
