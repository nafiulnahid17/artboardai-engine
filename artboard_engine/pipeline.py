import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .exporters import ColorConverter, ensure_formats, export_eps, export_svg
from .fidelity import compare_preview
from .images import analyze_image, extract_panel, load_image
from .models import CANVAS, EngineError, JobRequest
from .ocr import recognize_text
from .preview import render_preview
from .tracing import trace_artwork
from .typography import build_text_paths, erase_confirmed_text


def atomic_json(target: Path, data: dict):
    target.parent.mkdir(parents=True, exist_ok=True)
    # A unique temporary sibling avoids collisions during concurrent reads/writes.
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False, suffix=".tmp") as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        temp = Path(stream.name)
    temp.replace(target)


def process_job(
    request: JobRequest, output: Path, cmyk_profile: Path | None = None,
    progress: Callable[[str, int], None] | None = None,
) -> dict:
    ensure_formats(request.formats, request.native_ai_mode)
    if output.exists() and any(output.iterdir()):
        raise EngineError("OUTPUT_NOT_EMPTY", "Use a new empty output directory to avoid overwriting artwork.")
    output.mkdir(parents=True, exist_ok=True)
    colors = ColorConverter(cmyk_profile)
    manifest = {
        "engine_version": "0.1.0", "status": "processing",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "canvas": CANVAS.model_dump(),
        "requested_dpi": request.dpi,
        "raster_equivalent_px": list(CANVAS.raster_size(request.dpi)),
        "formats": request.formats, "panels": {}, "production_ready": False,
        "production_validation_in_scope": False,
    }
    atomic_json(output / "manifest.json", manifest)
    try:
        for panel_index, panel in enumerate(request.panels):
            if progress:
                progress(f"{panel.side}:extract", panel_index * 45 + 5)
            directory = output / panel.side
            directory.mkdir()
            source = load_image(panel.source)
            extracted = extract_panel(source, panel)
            flattening = extracted.info.get("artboard_flattening")
            if flattening:
                uncertainty = extracted.info.pop("artboard_uncertainty_mask")
                uncertainty.save(output / f"{panel.side}-body-uncertainty.png")
            extracted.save(directory / "extracted.png")
            analysis = analyze_image(extracted)
            if progress:
                progress(f"{panel.side}:ocr", panel_index * 45 + 15)
            ocr = recognize_text(directory / "extracted.png", panel.ocr_language) if panel.run_ocr else {
                "status": "explicitly_disabled", "requires_operator_confirmation": True,
            }
            if progress:
                progress(f"{panel.side}:trace", panel_index * 45 + 25)
            prepared = erase_confirmed_text(extracted, panel.text_layers)
            shapes, trace_report = trace_artwork(prepared, directory, panel.trace)
            text_reports = []
            for text_index, text_layer in enumerate(panel.text_layers):
                rebuilt, text_report = build_text_paths(text_layer, text_index)
                shapes.extend(rebuilt)
                text_reports.append(text_report)
            trace_report["traced_path_count"] = trace_report["path_count"]
            trace_report["path_count"] = len(shapes)
            trace_report["segment_count"] = sum(len(shape.path) for shape in shapes)
            trace_report["rebuilt_glyph_path_count"] = sum(t["glyph_count"] for t in text_reports)
            files = []
            if progress:
                progress(f"{panel.side}:export", panel_index * 45 + 40)
            for fmt in request.formats:
                if fmt == "ai":
                    # Only Illustrator may create this file; do not promise a nonexistent AI.
                    continue
                name = f"{panel.side}-body.{fmt}"
                if fmt == "svg":
                    export_svg(shapes, output / name, colors, panel.side)
                elif fmt == "eps":
                    export_eps(shapes, output / name, colors, panel.side)
                files.append(name)
            preview_eps = output / f"{panel.side}-body.eps"
            if not preview_eps.exists():
                preview_eps = preview_eps if "ai" in request.formats else directory / "preview-source.eps"
                export_eps(shapes, preview_eps, colors, panel.side)
            preview_name = f"{panel.side}-body-preview.png"
            preview_created = render_preview(preview_eps, output / preview_name)
            fidelity = None
            if preview_created and not panel.text_layers:
                fidelity = compare_preview(
                    prepared, output / preview_name, output / f"{panel.side}-body-difference.png",
                )
            manifest["panels"][panel.side] = {
                "analysis": analysis, "ocr": ocr, "trace": trace_report,
                "confirmed_text_layers": text_reports, "fidelity": fidelity,
                "flattening": flattening,
                "fidelity_note": "Original-source comparison skipped for intentional text replacements; review the preview manually." if panel.text_layers else None,
                "exports": files, "preview": preview_name if preview_created else None,
                "selection": {
                    "crop": panel.crop.model_dump() if panel.crop else None,
                    "perspective_quad": [p.model_dump() for p in panel.perspective_quad] if panel.perspective_quad else None,
                },
            }
        blockers = [
            "Operator must confirm text, logos and tracing fidelity against the source.",
        ]
        if not colors.transform:
            blockers.append("Optional ICC not supplied: EPS uses disclosed unmanaged DeviceCMYK. Export is allowed; printer-specific color matching is not claimed.")
        if any(p["trace"]["aspect_ratio_requires_review"] for p in manifest["panels"].values()):
            blockers.append("Artwork was fitted without stretching; verify margins and full-panel coverage.")
        if any(p["fidelity"] and p["fidelity"]["automatic_review_flag"] for p in manifest["panels"].values()):
            blockers.append("Visual fidelity check flagged differences. Inspect the previews and heatmaps before approval.")
        if any(p.get("flattening") and p["flattening"]["requires_reconstruction"] for p in manifest["panels"].values()):
            blockers.append("Hidden or missing artwork remains unresolved. Review or supply references; exported vectors do not recover unseen details.")
        if any(p.get("flattening") for p in manifest["panels"].values()):
            blockers.append("Guided flattening does not remove fabric shadows. Review visible artwork, lighting and reference alignment.")
        desktop_request = None
        if "ai" in request.formats:
            from .desktop import prepare_desktop
            desktop_request = prepare_desktop(output, cmyk_profile)
            blockers.append("Genuine native AI files are awaiting Illustrator Save As and verified return.")
        manifest.update({
            "status": "awaiting_desktop" if desktop_request else "review_required", "color": colors.report(),
            "desktop_handoff": desktop_request,
            "preflight": {
                "canvas_dimensions_locked": True, "true_vector_paths": True,
                "embedded_raster_count": 0, "live_fonts_count": 0,
                "eps_standard": "EPSF 3.0 / PostScript Level 2",
                "blockers": blockers,
                "native_ai_supported": False,
                "native_ai_status": "awaiting_desktop" if desktop_request else "not_requested",
                "native_ai_export_route": "actual_Illustrator_Save_As",
                "automatic_fabric_fold_reconstruction": False,
            },
        })
        atomic_json(output / "manifest.json", manifest)
        if progress:
            progress(manifest["status"], 100)
        return manifest
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = {
            "code": exc.code if isinstance(exc, EngineError) else "PROCESSING_FAILED",
            "message": str(exc) if isinstance(exc, EngineError) else "Processing failed; review server logs.",
        }
        atomic_json(output / "manifest.json", manifest)
        raise
