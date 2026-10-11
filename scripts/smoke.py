"""Bounded engine smoke checks; not printer or cross-application acceptance testing."""
import io
import json
import tempfile
import time
from pathlib import Path

from defusedxml import ElementTree as ET
from fastapi.testclient import TestClient
from PIL import Image, ImageCms, ImageDraw, ImageFont
from pydantic import ValidationError
from svgpathtools import CubicBezier, Line, Path as VectorPath

from artboard_engine.api import create_app
from artboard_engine.detection import detect_panels
from artboard_engine.exporters import ColorConverter, ensure_formats, export_eps
from artboard_engine.images import extract_panel, load_image
from artboard_engine.models import CANVAS, Crop, EngineError, JobRequest, PanelRequest, TextLayer, TraceSettings
from artboard_engine.pipeline import process_job
from artboard_engine.preview import render_preview
from artboard_engine.tracing import Shape
from artboard_engine.typography import build_text_paths
from artboard_engine.isolation import process_isolated


def sample(path: Path, side: str):
    image = Image.new("RGB", (440, 620), "#ffffff")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 440, 90), fill="#152235")
    draw.rounded_rectangle((60, 130, 380, 450), radius=24, fill="#bfe36a")
    draw.polygon([(110, 510), (220, 460), (330, 510), (330, 575), (110, 575)], fill="#152235")
    font = ImageFont.load_default(size=38)
    draw.text((30, 25), side.upper(), fill="white", font=font)
    draw.text((70, 270), "JERSEY OS", fill="#152235", font=ImageFont.load_default(size=30))
    image.save(path)


def combined_sample(path: Path):
    image = Image.new("RGB", (1000, 500), "#ffffff")
    draw = ImageDraw.Draw(image)
    for center_x, color in ((260, "#17345a"), (740, "#b62939")):
        points = [
            (center_x - 70, 80), (center_x - 42, 42), (center_x + 42, 42),
            (center_x + 70, 80), (center_x + 130, 130), (center_x + 92, 190),
            (center_x + 68, 168), (center_x + 68, 430), (center_x - 68, 430),
            (center_x - 68, 168), (center_x - 92, 190), (center_x - 130, 130),
        ]
        draw.polygon(points, fill=color)
        draw.rectangle((center_x - 42, 210, center_x + 42, 240), fill="#ffffff")
    image.save(path)


def main():
    output_root = Path("generated-artifacts/engine-smoke").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        directory = Path(work)
        for side in ("front", "back"):
            sample(directory / f"{side}.png", side)
        combined_path = directory / "combined.png"
        combined_sample(combined_path)
        uneven_background = Image.new("RGB", (600, 400))
        uneven_draw = ImageDraw.Draw(uneven_background)
        for y in range(400):
            shade = 180 + y // 10
            uneven_draw.line((0, y, 599, y), fill=(shade, shade, shade))
        uneven_draw.rectangle((60, 60, 240, 350), fill=(20, 40, 60))
        uneven_draw.rectangle((360, 60, 540, 350), fill=(130, 20, 30))
        unsafe_detection = detect_panels(uneven_background)
        assert unsafe_detection["status"] == "manual_selection_required"
        assert not unsafe_detection["candidate_pair_found"]
        assert not unsafe_detection["candidates"]
        assert CANVAS.raster_size(330) == (7260, 10230)
        assert CANVAS.raster_size(600) == (13200, 18600)
        try:
            CANVAS.raster_size(300)
            raise AssertionError("Invalid DPI accepted")
        except EngineError:
            pass
        try:
            JobRequest(panels=[PanelRequest(side="front", source=directory / "front.png")] * 2)
            raise AssertionError("Duplicate side accepted")
        except ValidationError:
            pass
        try:
            ensure_formats(["ai"])
            raise AssertionError("Fake native AI accepted")
        except EngineError as exc:
            assert exc.code == "NATIVE_AI_REQUIRES_ILLUSTRATOR"
        try:
            extract_panel(load_image(directory / "front.png"), PanelRequest(
                side="front", source=directory / "front.png", crop=Crop(x=430, y=0, width=20, height=20),
            ))
            raise AssertionError("Out-of-bounds crop accepted")
        except EngineError as exc:
            assert exc.code == "CROP_OUT_OF_BOUNDS"

        request = JobRequest(panels=[
            PanelRequest(side=side, source=directory / f"{side}.png")
            for side in ("front", "back")
        ])
        protected = directory / "existing-approved-output"
        protected.mkdir()
        protected_manifest = protected / "manifest.json"
        protected_manifest.write_text('{"status":"approved","production_ready":true}')
        unchanged = protected_manifest.read_bytes()
        try:
            process_isolated(request, protected)
            raise AssertionError("Existing output was overwritten")
        except EngineError as exc:
            assert exc.code == "OUTPUT_NOT_EMPTY"
        assert protected_manifest.read_bytes() == unchanged
        try:
            process_isolated(request, directory / "invalid-profile-job", directory / "missing.icc")
            raise AssertionError("Missing ICC silently accepted")
        except EngineError as exc:
            assert exc.code == "ICC_NOT_FOUND"
        profiled = directory / "profiled.png"
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        Image.new("RGB", (10, 10), "red").save(profiled, icc_profile=profile)
        assert load_image(profiled).info["artboard_source_color"]["normalization"] == "embedded_ICC_to_sRGB"
        unprofiled_cmyk = directory / "unprofiled-cmyk.jpg"
        Image.new("CMYK", (10, 10), (0, 0, 0, 255)).save(unprofiled_cmyk)
        try:
            load_image(unprofiled_cmyk)
            raise AssertionError("Unprofiled CMYK source was silently converted")
        except EngineError as exc:
            assert exc.code == "SOURCE_CMYK_PROFILE_REQUIRED"
        destination = output_root / str(time.time_ns())
        manifest = process_job(request, destination)
        assert manifest["status"] == "review_required" and not manifest["production_ready"]
        for side in ("front", "back"):
            svg = ET.parse(destination / f"{side}-body.svg").getroot()
            assert svg.attrib["width"] == "22in" and svg.attrib["height"] == "31in"
            assert len(svg.findall(".//{http://www.w3.org/2000/svg}path")) > 0
            assert not svg.findall(".//{http://www.w3.org/2000/svg}image")
            eps = (destination / f"{side}-body.eps").read_text()
            assert eps.startswith("%!PS-Adobe-3.0 EPSF-3.0")
            assert "%%LanguageLevel: 2" in eps and "%%BoundingBox: 0 0 1584 2232" in eps
            assert "setcmykcolor" in eps and "curveto" in eps
            assert manifest["panels"][side]["ocr"]["words"]
            preview = Image.open(destination / f"{side}-body-preview.png")
            assert preview.size == (792, 1116)

        # Regression for separate closed subpaths: preserve holes with nonzero winding.
        outer = VectorPath(Line(0j, 200 + 0j), Line(200 + 0j, 200 + 200j),
                           Line(200 + 200j, 200j), Line(200j, 0j))
        inner = VectorPath(Line(50 + 50j, 50 + 150j), Line(50 + 150j, 150 + 150j),
                           Line(150 + 150j, 150 + 50j), Line(150 + 50j, 50 + 50j))
        shape = Shape(VectorPath(*outer, *inner), (0, 0, 0), "black")
        export_eps([shape], directory / "hole.eps", ColorConverter(), "front")
        assert render_preview(directory / "hole.eps", directory / "hole.png")
        hole_image = Image.open(directory / "hole.png")
        assert hole_image.getpixel((50, 50))[3] == 0
        assert hole_image.getpixel((10, 10))[3] > 0

        # VTracer must not turn transparent pixels into a solid background.
        transparent = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(transparent).rectangle([30, 30, 70, 70], fill=(255, 0, 0, 255))
        from artboard_engine.tracing import trace_artwork
        trace_dir = directory / "alpha-trace"
        trace_dir.mkdir()
        alpha_shapes, _ = trace_artwork(transparent, trace_dir, TraceSettings())
        export_eps(alpha_shapes, directory / "alpha.eps", ColorConverter(), "front")
        render_preview(directory / "alpha.eps", directory / "alpha.png")
        alpha_render = Image.open(directory / "alpha.png")
        assert alpha_render.getpixel((400, 558))[3] > 0
        assert alpha_render.getpixel((100, 558))[3] == 0
        partial = Image.new("RGBA", (10, 10), (255, 0, 0, 128))
        try:
            extract_panel(partial, PanelRequest(side="front", source=directory / "unused"))
            raise AssertionError("Partial transparency silently flattened")
        except EngineError as exc:
            assert exc.code == "ALPHA_NEEDS_MATTE"
        flattened = extract_panel(partial, PanelRequest(
            side="front", source=directory / "unused", alpha_matte_rgb=(255, 255, 255),
        ))
        assert flattened.getextrema()[3] == (255, 255)

        font_data = ImageFont.load_default(size=30).path.getvalue()
        font_path = directory / "verified-smoke-font.ttf"
        font_path.write_bytes(font_data)
        layer = TextLayer(
            text="JERSEY", font=font_path, verified_by_operator=True,
            x_points=150, baseline_y_points=800, size_points=100,
        )
        glyph_shapes, glyph_report = build_text_paths(layer, 0)
        assert glyph_report["text"] == "JERSEY" and len(glyph_shapes) == 6
        export_eps(glyph_shapes, directory / "glyphs.eps", ColorConverter(), "front")
        assert render_preview(directory / "glyphs.eps", directory / "glyphs.png")
        assert manifest["panels"]["front"]["fidelity"]["grayscale_ssim"] > 0.80

        with TestClient(create_app(directory / "api", token="smoke-test-only-token")) as client:
            assert client.get("/api/engine/capabilities").status_code == 401
            client.headers["Authorization"] = "Bearer smoke-test-only-token"
            assert client.get("/api/engine/capabilities").json()["canvas"]["width_inches"] == 22
            assert client.post("/api/engine/sources", files={"file": ("bad.png", b"not an image", "image/png")}).status_code == 422
            ids = {}
            for side in ("front", "back"):
                content = (directory / f"{side}.png").read_bytes()
                response = client.post("/api/engine/sources", files={"file": (f"{side}.png", content, "image/png")})
                assert response.status_code == 201, response.text
                ids[side] = response.json()["source_id"]
            analysis = client.post(f"/api/engine/sources/{ids['front']}/analyze").json()
            assert analysis["width_px"] == 440

            combined_upload = client.post(
                "/api/engine/sources",
                files={"file": ("combined.png", combined_path.read_bytes(), "image/png")},
            )
            assert combined_upload.status_code == 201, combined_upload.text
            combined_id = combined_upload.json()["source_id"]
            detected = client.post(f"/api/engine/sources/{combined_id}/detect-panels").json()
            assert detected["status"] == "two_candidates_found", detected
            assert detected["candidate_pair_found"] and not detected["front_back_classified"]
            assert detected["requires_operator_confirmation"]
            assert {candidate["suggested_side"] for candidate in detected["candidates"]} == {"unknown"}
            detected_panels = [
                {
                    "side": side, "source_id": combined_id,
                    "crop": candidate["source_bbox_px"], "run_ocr": False,
                }
                for side, candidate in zip(("front", "back"), detected["candidates"])
            ]
            combined_job = client.post("/api/engine/jobs", json={
                "panels": detected_panels, "formats": ["svg"], "dpi": 330,
            })
            assert combined_job.status_code == 202, combined_job.text
            combined_job_id = combined_job.json()["job_id"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                combined_state = client.get(f"/api/engine/jobs/{combined_job_id}").json()
                if combined_state["status"] not in ("processing", "queued"):
                    break
                time.sleep(0.05)
            assert combined_state["status"] == "review_required", combined_state
            for side in ("front", "back"):
                assert client.get(
                    f"/api/engine/jobs/{combined_job_id}/files/{side}-body.svg"
                ).status_code == 200

            panels = [{"side": side, "source_id": ids[side], "run_ocr": False} for side in ("front", "back")]
            assert client.post("/api/engine/jobs", json={"panels": [panels[0], panels[0]]}).status_code == 422
            assert client.post("/api/engine/jobs", json={"panels": panels, "formats": ["ai"]}).status_code == 422
            invalid = {"panels": [
                {**panels[0], "crop": {"x": 430, "y": 0, "width": 100, "height": 100}}, panels[1],
            ]}
            assert client.post("/api/engine/jobs", json=invalid).status_code == 422
            response = client.post("/api/engine/jobs", json={"panels": panels, "dpi": 330})
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
            end = time.monotonic() + 30
            while time.monotonic() < end:
                state = client.get(f"/api/engine/jobs/{job_id}").json()
                if state["status"] not in ("processing", "queued"):
                    break
                time.sleep(0.05)
            assert state["status"] == "review_required", state
            assert client.get(f"/api/engine/jobs/{job_id}/files/front-body.svg").status_code == 200
            assert client.get(f"/api/engine/jobs/{job_id}/files/request.json").status_code == 404
            assert client.get(f"/api/engine/jobs/{job_id}/bundle").status_code == 200
            approval = {
                "reviewer_reference": "smoke-reviewer", "dimensions_verified": True,
                "artwork_and_text_verified": True, "printer_color_profile_verified": True,
                "bleed_and_pattern_verified": True, "illustrator_coreldraw_compatibility_verified": True,
            }
            assert client.post(f"/api/engine/jobs/{job_id}/approve", json=approval).status_code == 404
            assert client.get(f"/api/engine/jobs/{job_id}/panels/front/source-preview").status_code == 200
            font_upload = client.post("/api/engine/fonts", files={"file": ("font.ttf", font_data, "font/ttf")})
            assert font_upload.status_code == 201, font_upload.text
            text_panels = [
                {**panels[0], "text_layers": [{
                    "text": "MASTER", "font_id": font_upload.json()["font_id"],
                    "verified_by_operator": True, "x_points": 150,
                    "baseline_y_points": 800, "size_points": 100,
                    "erase_source_rect": {"x": 60, "y": 250, "width": 300, "height": 80},
                    "erase_background_rgb": [191, 227, 106],
                }]},
                panels[1],
            ]
            text_job = client.post("/api/engine/jobs", json={"panels": text_panels})
            assert text_job.status_code == 202, text_job.text
            text_job_id = text_job.json()["job_id"]
            end = time.monotonic() + 30
            while time.monotonic() < end:
                text_state = client.get(f"/api/engine/jobs/{text_job_id}").json()
                if text_state["status"] not in ("queued", "processing"):
                    break
                time.sleep(0.05)
            assert text_state["status"] == "review_required", text_state
            assert text_state["result"]["panels"]["front"]["confirmed_text_layers"][0]["text"] == "MASTER"
        print(json.dumps({
            "status": "passed", "sample_outputs": str(destination),
            "checks": [
                "locked size and DPI", "one front/one back", "invalid sources/crops rejected",
                "actual vector SVG", "CMYK Level-2 EPS rendered", "compound path holes",
                "OCR words", "private API access", "queued processing",
                "downloads and bundle", "production phase not exposed", "native-AI refusal without explicit handoff",
                "transparent tracing and explicit matte", "verified-font glyph outlines",
                "isolated API workers", "source/render fidelity metrics",
                "single-upload local front/back region proposals", "separate outputs from the same uploaded source",
            ],
            "not_validated": ["live AI", "printer ICC", "Illustrator", "CorelDRAW X8", "garment production"],
        }, indent=2))


if __name__ == "__main__":
    main()
