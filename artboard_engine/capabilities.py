import shutil

from .assistant import ai_configured
from .ai_generation import ai_generation_configured, generation_model_names
from .models import CANVAS


def capabilities() -> dict:
    return {
        "engine": "Artboard Ai", "version": "0.1.0",
        "canvas": CANVAS.model_dump(),
        "formats": {
            "svg": {"supported": True, "color_space": "sRGB", "true_paths": True},
            "eps": {"supported": True, "color_space": "DeviceCMYK", "standard": "EPSF 3.0 / PostScript Level 2"},
            "ai": {
                "supported": False, "available_locally": False,
                "desktop_handoff": True, "real_save_operation": "IllustratorSaveOptions",
                "reason": "Licensed Illustrator must execute the handoff and return the native AI; not available on the Linux worker.",
            },
        },
        "ocr": {"available": bool(shutil.which("tesseract")), "engine": "tesseract", "advisory_only": True},
        "ai_assistance": {
            "configured": ai_configured(), "features": ["part_suggestions", "input_analysis", "text_observations", "tracing_mode_suggestions"],
            "automatic_export_or_approval": False,
        },
        "ai_artwork_generation": {
            "configured": ai_generation_configured(),
            "models": generation_model_names(),
            "output": "generated raster draft, then locally traced to editable vector paths",
            "requires_operator_review": True,
            "automatic_export_or_approval": False,
        },
        "panel_detection": {
            "local_candidate_detection": True,
            "local_method": "transparent_alpha_or_uniform_corner_background",
            "front_back_classification_without_ai": False,
            "requires_confirmation": True,
            "ai_opt_in": True,
        },
        "processing": {
            "crop": True, "four_point_perspective": True, "uniform_edge_background_removal": True,
            "color_vector_tracing": True, "path_cleanup": "speckle filtering, curve fitting and zero-length line removal",
            "automatic_fabric_flattening": False, "hidden_artwork_reconstruction": False,
            "guided_mesh_flattening": True, "operator_occlusion_masks": True,
            "supplied_reference_recovery": True, "unresolved_artwork_requires_review": True,
            "automatic_shadow_removal": False,
            "verified_font_text_rebuild": True, "output_text_as_glyph_paths": True,
            "original_font_recovery": False, "fidelity_comparison": True, "isolated_workers": True,
        },
        "preview_renderer": bool(shutil.which("gs")),
        "print_validation": {
            "in_scope": False, "production_approval_enabled": False,
            "icc_transform_and_fingerprint": True, "icc_optional": True,
            "icc_file_extensions": [".icc", ".icm"],
            "actual_application_evidence_required": [],
            "desktop_checks_executed_here": False,
            "evidence_type": "authenticated_operator_verified_records",
            "physical_printer_proof": "requires_printer_and_operator",
        },
        "limits": {"upload_mib": 30, "source_megapixels": 40, "trace_max_dimension": 2400},
    }
