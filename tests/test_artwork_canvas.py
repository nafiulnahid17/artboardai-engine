import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from PIL import Image
from svgpathtools import Line, Path as VectorPath

from artboard_engine.exporters import export_svg
from artboard_engine.images import fit_image_to_canvas
from artboard_engine.models import CANVAS, TraceSettings
from artboard_engine.tracing import Shape, trace_artwork


class CanvasColorMap:
    def report(self):
        return {}

    def convert(self, _rgb):
        return (0.0, 0.0, 0.0, 1.0)


class ArtworkCanvasTests(unittest.TestCase):
    def test_canvas_has_exact_metric_dimensions_and_raster_targets(self):
        self.assertEqual((CANVAS.width_mm, CANVAS.height_mm), (558.8, 787.4))
        self.assertEqual(CANVAS.raster_size(330), (7260, 10230))
        self.assertEqual(CANVAS.raster_size(600), (13200, 18600))

    def test_canvas_fit_retains_all_input_content_without_cropping(self):
        source = Image.new("RGB", (128, 240), (30, 80, 150))
        fitted, report = fit_image_to_canvas(source)

        self.assertEqual(fitted.size, (170, 240))
        self.assertEqual(fitted.getpixel((0, 0)), (30, 80, 150))
        self.assertEqual(fitted.getpixel((169, 239)), (30, 80, 150))
        self.assertFalse(report["cropped_source_pixels"])
        self.assertEqual(report["placement"], "stretch_to_full_canvas_no_crop")
        self.assertTrue(report["requires_operator_review"])

    def test_vector_tracing_maps_all_input_edges_to_the_full_artboard(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = Image.new("RGB", (100, 200), (10, 20, 30))

            def write_trace(_source_path, target_path, **_kwargs):
                Path(target_path).write_text(
                    '<svg xmlns="http://www.w3.org/2000/svg">'
                    '<path d="M 0,0 L 100,0 L 100,200 L 0,200 Z" fill="#0a141e"/>'
                    '</svg>',
                    encoding="utf-8",
                )

            with patch("artboard_engine.tracing.vtracer.convert_image_to_svg_py", side_effect=write_trace):
                shapes, report = trace_artwork(source, directory, TraceSettings())

            xmin, xmax, ymin, ymax = shapes[0].path.bbox()
            self.assertAlmostEqual(xmin, 0)
            self.assertAlmostEqual(xmax, CANVAS.width_points)
            self.assertAlmostEqual(ymin, 0)
            self.assertAlmostEqual(ymax, CANVAS.height_points)
            self.assertEqual(report["placement"], "stretch_to_full_canvas_no_crop")
            self.assertFalse(report["source_content_cropped"])
            self.assertTrue(report["aspect_ratio_requires_review"])

    def test_svg_declares_the_exact_metric_artboard_size(self):
        path = VectorPath(
            Line(0 + 0j, 1584 + 0j),
            Line(1584 + 0j, 1584 + 2232j),
        )
        shape = Shape(path=path, rgb=(20, 40, 80), layer="test")
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "front-body.svg"
            export_svg([shape], target, CanvasColorMap(), "front")
            root = ElementTree.parse(target).getroot()

        self.assertEqual(root.attrib["width"], "558.8mm")
        self.assertEqual(root.attrib["height"], "787.4mm")
        self.assertEqual(root.attrib["viewBox"], "0 0 1584 2232")


if __name__ == "__main__":
    unittest.main()
