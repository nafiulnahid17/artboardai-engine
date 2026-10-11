import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from svgpathtools import Line, Path as VectorPath

from artboard_engine.ai_generation import GeneratedPanel
from artboard_engine.models import JobRequest, PanelRequest
from artboard_engine.pipeline import process_job
from artboard_engine.tracing import Shape


def image_bytes(color):
    stream = io.BytesIO()
    Image.new("RGB", (128, 240), color).save(stream, format="PNG")
    return stream.getvalue()


class FlatArtworkPipelineTests(unittest.TestCase):
    def test_ai_flat_art_mode_traces_draft_and_keeps_it_reviewable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            front = root / "front.png"
            back = root / "back.png"
            Image.new("RGB", (128, 240), (130, 130, 130)).save(front)
            Image.new("RGB", (128, 240), (120, 120, 120)).save(back)
            output = root / "output"
            request = JobRequest(panels=[
                PanelRequest(
                    side="front", source=front, reconstruction_mode="ai_flat_artwork", run_ocr=False,
                ),
                PanelRequest(
                    side="back", source=back, reconstruction_mode="ai_flat_artwork", run_ocr=False,
                ),
            ])
            calls = []

            def reconstruct(image, side):
                calls.append(side)
                color = (20, 110, 160) if side == "front" else (30, 90, 130)
                return GeneratedPanel(
                    png=image_bytes(color), model="test/flat-art", width=128, height=240,
                )

            def trace(image, _directory, _settings, _callback):
                self.assertNotEqual(image.convert("RGB").getpixel((0, 0)), (130, 130, 130))
                path = VectorPath(
                    Line(0 + 0j, 100 + 0j),
                    Line(100 + 0j, 100 + 100j),
                    Line(100 + 100j, 0 + 100j),
                    Line(0 + 100j, 0 + 0j),
                )
                return [Shape(path=path, rgb=(20, 110, 160), layer="palette-color")], {
                    "path_count": 1, "segment_count": 4, "aspect_ratio_requires_review": True,
                }

            with (
                patch("artboard_engine.pipeline.reconstruct_panel", side_effect=reconstruct),
                patch("artboard_engine.pipeline.trace_artwork", side_effect=trace),
                patch("artboard_engine.pipeline.render_preview", return_value=False),
            ):
                result = process_job(request, output)

            self.assertEqual(calls, ["front", "back"])
            self.assertFalse(result["production_ready"])
            self.assertEqual(result["status"], "review_required")
            self.assertEqual(
                result["panels"]["front"]["reconstruction"]["status"], "draft_created",
            )
            self.assertFalse(
                result["panels"]["front"]["reconstruction"]["source_fidelity_verified"],
            )
            self.assertEqual(result["canvas"]["width_mm"], 558.8)
            self.assertEqual(result["canvas"]["height_mm"], 787.4)
            self.assertEqual(result["panels"]["front"]["canvas_fit"]["canvas_fit_dimensions_px"], [170, 240])
            self.assertFalse(result["panels"]["front"]["canvas_fit"]["cropped_source_pixels"])
            self.assertTrue((output / "front-flat-art-draft.png").is_file())
            with Image.open(output / "front-flat-art-draft.png") as draft:
                self.assertEqual(draft.size, (170, 240))
            self.assertTrue((output / "front-body.svg").is_file())
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertTrue(any(
                "AI flat-art reconstruction is an approximation" in blocker
                for blocker in manifest["preflight"]["blockers"]
            ))


if __name__ == "__main__":
    unittest.main()
