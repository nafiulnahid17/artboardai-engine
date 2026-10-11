"""Software checks only. Does NOT run Illustrator/CorelDRAW or validate a printer."""
import argparse
import io
import json
import tempfile
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageCms, ImageDraw
from pypdf import PdfWriter

from artboard_engine.api import create_app
from artboard_engine.desktop import inspect_native_ai, sha256
from artboard_engine.exporters import ColorConverter
from artboard_engine.images import extract_panel, load_image
from artboard_engine.models import EngineError, FlattenSettings, NormalizedBox, PanelRequest, Point, ReferencePatch


def expected_error(code, function):
    try:
        function()
    except EngineError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"Expected {code}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-profile", type=Path, help="Generic CMYK ICC fixture ONLY, never a printer approval.")
    args = parser.parse_args()
    checks = []
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        source = root / "source.png"
        image = Image.new("RGBA", (100, 120), (30, 80, 150, 255))
        ImageDraw.Draw(image).rectangle((30, 0, 50, 119), fill=(200, 100, 20, 255))
        image.save(source)
        grid = [
            [Point(x=0, y=0), Point(x=50, y=0), Point(x=99, y=0)],
            [Point(x=0, y=60), Point(x=55, y=65), Point(x=99, y=60)],
            [Point(x=0, y=119), Point(x=50, y=119), Point(x=99, y=119)],
        ]
        settings = FlattenSettings(control_grid=grid, operator_verified=True, pixels_per_inch=25)
        request = PanelRequest(side="front", source=source, flatten=settings, run_ocr=False)
        flat = extract_panel(load_image(source), request)
        assert flat.size == (550, 775)
        assert flat.info["artboard_flattening"]["unresolved_pixels"] == 0
        assert flat.getpixel((0, 0)) == image.getpixel((0, 0))
        checks.append("nonlinear guided grid produces locked physical aspect ratio")
        bad = settings.model_copy(deep=True)
        bad.control_grid[0][0] = Point(x=100, y=0)
        expected_error("MESH_OUT_OF_BOUNDS", lambda: extract_panel(load_image(source), request.model_copy(update={"flatten": bad})))
        inverted = settings.model_copy(deep=True)
        inverted.control_grid[0][0], inverted.control_grid[0][1] = inverted.control_grid[0][1], inverted.control_grid[0][0]
        expected_error("INVERTED_MESH", lambda: extract_panel(load_image(source), request.model_copy(update={"flatten": inverted})))
        checks.append("out-of-bounds and inverted grids rejected")
        hidden = settings.model_copy(deep=True)
        hidden.occluded_polygons = [[Point(x=20, y=20), Point(x=80, y=20), Point(x=80, y=90), Point(x=20, y=90)]]
        hidden_request = request.model_copy(update={"flatten": hidden, "alpha_matte_rgb": (255, 255, 255)})
        masked = extract_panel(load_image(source), hidden_request)
        report = masked.info["artboard_flattening"]
        assert report["requires_reconstruction"] and report["unresolved_pixels"] > 0
        assert masked.info["artboard_uncertainty_mask"].getextrema() == (0, 255)
        checks.append("occlusion stays unresolved even with an explicit white matte")
        donor = root / "reference.png"
        Image.new("RGB", (100, 120), (20, 200, 40)).save(donor)
        patch = ReferencePatch(
            source=donor, target=NormalizedBox(x=0, y=0, width=1, height=1), operator_verified=True,
        )
        restored = extract_panel(load_image(source), hidden_request.model_copy(update={"reference_patches": [patch]}))
        assert restored.info["artboard_flattening"]["unresolved_pixels"] == 0
        assert restored.getpixel((0, 0)) == image.getpixel((0, 0))
        assert restored.getpixel((275, 390))[:3] == (20, 200, 40)
        checks.append("supplied reference fills hidden areas without changing visible pixels")
        rgb_profile = root / "rgb.icc"
        rgb_profile.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        expected_error("INVALID_CMYK_PROFILE", lambda: ColorConverter(rgb_profile))
        fake_ai = root / "renamed.ai"
        writer = PdfWriter()
        writer.add_blank_page(width=1584, height=2232)
        with fake_ai.open("wb") as stream:
            writer.write(stream)
        expected_error("INVALID_NATIVE_AI", lambda: inspect_native_ai(fake_ai))
        fake_ai.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n")
        expected_error("INVALID_NATIVE_AI", lambda: inspect_native_ai(fake_ai))
        checks.append("RGB ICC and ordinary PDF/EPS renamed .ai rejected")
        with TestClient(create_app(root / "api")) as client:
            bad_profile = client.post("/api/engine/profiles", files={"file": ("rgb.icc", rgb_profile.read_bytes())})
            assert bad_profile.status_code == 422, bad_profile.text
            buf = io.BytesIO()
            image.save(buf, "PNG")
            source_id = client.post("/api/engine/sources", files={"file": ("source.png", buf.getvalue())}).json()["source_id"]
            panels = [{"side": side, "source_id": source_id, "run_ocr": False} for side in ("front", "back")]
            missing = client.post("/api/engine/jobs", json={
                "panels": panels, "formats": ["svg", "eps", "ai"], "native_ai_mode": "desktop_handoff",
            })
            assert missing.status_code == 202, missing.text
            unmanaged_job = missing.json()["job_id"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                unmanaged_state = client.get(f"/api/engine/jobs/{unmanaged_job}").json()
                if unmanaged_state["status"] not in ("queued", "processing"):
                    break
                time.sleep(0.05)
            assert unmanaged_state["status"] == "awaiting_desktop", unmanaged_state
            assert unmanaged_state["result"]["production_validation_in_scope"] is False
            assert unmanaged_state["result"]["color"]["icc_profile"] is None
            assert client.post(f"/api/engine/jobs/{unmanaged_job}/approve", json={}).status_code == 404
            assert client.post(f"/api/engine/jobs/{unmanaged_job}/desktop-checks", json={}).status_code == 404
            unmanaged_bundle = client.get(f"/api/engine/jobs/{unmanaged_job}/bundle")
            with zipfile.ZipFile(io.BytesIO(unmanaged_bundle.content)) as archive:
                assert "desktop/export-front.jsx" in archive.namelist()
                assert "desktop/printer-profile.icc" not in archive.namelist()
                assert json.loads(archive.read("desktop/request.json"))["profile_sha256"] is None
            checks.append("ICC-free native handoff works; production approval and desktop-check routes are absent")
            if args.test_profile:
                color = ColorConverter(args.test_profile)
                assert color.profile_info["sha256"] == sha256(args.test_profile)
                assert len(color.convert((180, 80, 40))) == 4
                uploaded = client.post("/api/engine/profiles", files={"file": ("generic-test.icc", args.test_profile.read_bytes())})
                assert uploaded.status_code == 201, uploaded.text
                accepted = client.post("/api/engine/jobs", json={
                    "profile_id": uploaded.json()["profile_id"], "panels": panels,
                    "formats": ["svg", "eps", "ai"], "native_ai_mode": "desktop_handoff",
                })
                assert accepted.status_code == 202, accepted.text
                job_id = accepted.json()["job_id"]
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    state = client.get(f"/api/engine/jobs/{job_id}").json()
                    if state["status"] not in ("queued", "processing"):
                        break
                    time.sleep(0.05)
                assert state["status"] == "awaiting_desktop", state
                assert state["result"]["production_ready"] is False
                assert client.get(f"/api/engine/jobs/{job_id}/files/front-body.ai").status_code == 404
                bundle = client.get(f"/api/engine/jobs/{job_id}/bundle")
                with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
                    names = archive.namelist()
                    assert "desktop/export-front.jsx" in names and "desktop/export-back.jsx" in names
                    assert "desktop/printer-profile.icc" in names
                    assert not any(n.endswith(".ai") for n in names)
                    package = json.loads(archive.read("desktop/request.json"))
                    assert package["status"] == "native_export_not_run"
                    assert "desktop/check-template.json" not in names
                checks.append("queued CMYK handoff generates EPS/SVG/scripts but never fake native AI")
    print(json.dumps({
        "status": "passed", "checks": checks,
        "generic_cmyk_fixture_used": bool(args.test_profile),
        "not_validated": ["client printer ICC/proof", "actual Illustrator execution", "genuine AI positive import", "actual CorelDRAW X8", "real folded client mockup"],
    }, indent=2))


if __name__ == "__main__":
    main()
