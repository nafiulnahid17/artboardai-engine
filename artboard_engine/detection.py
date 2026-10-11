"""Local panel-region proposals and the opt-in semantic AI adapter."""
import math

import cv2
import numpy as np
from PIL import Image

from .assistant import analyze_with_ai
from .models import Region

MAX_DETECTION_DIMENSION = 1200


def _pixel_box(x: float, y: float, width: float, height: float, image_width: int, image_height: int) -> dict:
    left = max(0, min(image_width - 1, math.floor(x * image_width)))
    top = max(0, min(image_height - 1, math.floor(y * image_height)))
    right = max(left + 1, min(image_width, math.ceil((x + width) * image_width)))
    bottom = max(top + 1, min(image_height, math.ceil((y + height) * image_height)))
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def _position_hints(boxes: list[dict]) -> list[str]:
    if len(boxes) != 2:
        return [f"candidate_{index + 1}" for index in range(len(boxes))]
    centers = [
        (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for box in boxes
    ]
    if abs(centers[0][0] - centers[1][0]) >= abs(centers[0][1] - centers[1][1]):
        order = sorted(range(2), key=lambda index: centers[index][0])
        result = [""] * 2
        result[order[0]], result[order[1]] = "left", "right"
    else:
        order = sorted(range(2), key=lambda index: centers[index][1])
        result = [""] * 2
        result[order[0]], result[order[1]] = "top", "bottom"
    return result


def _local_foreground_mask(image: Image.Image) -> tuple[np.ndarray | None, str, str | None]:
    original_width, original_height = image.size
    scale = min(1.0, MAX_DETECTION_DIMENSION / max(original_width, original_height))
    if scale < 1:
        size = (max(1, round(original_width * scale)), max(1, round(original_height * scale)))
        rgba = np.asarray(image.convert("RGBA").resize(size, Image.Resampling.LANCZOS))
    else:
        rgba = np.asarray(image.convert("RGBA"))

    height, width = rgba.shape[:2]
    alpha = rgba[:, :, 3]
    if int(alpha.min()) < 245 and np.count_nonzero(alpha < 245) >= max(8, int(width * height * 0.001)):
        return (alpha > 16).astype(np.uint8), "alpha_foreground", None

    rgb = rgba[:, :, :3]
    patch_size = max(2, int(min(width, height) * 0.05))
    corners = [
        rgb[:patch_size, :patch_size],
        rgb[:patch_size, -patch_size:],
        rgb[-patch_size:, :patch_size],
        rgb[-patch_size:, -patch_size:],
    ]
    medians = np.array([np.median(patch.reshape(-1, 3), axis=0) for patch in corners])
    background = np.median(medians, axis=0)
    corner_drift = float(np.max(np.linalg.norm(medians - background, axis=1)))
    noise = float(np.median([
        np.median(np.linalg.norm(patch.astype(np.float32) - median, axis=2))
        for patch, median in zip(corners, medians)
    ]))
    if corner_drift > 22 or noise > 24:
        return None, "uniform_background_foreground", (
            "Background is not uniform at the image corners; automatic foreground separation is unsafe."
        )

    threshold = max(24.0, min(54.0, noise * 4 + 12))
    distance = np.linalg.norm(rgb.astype(np.float32) - background, axis=2)
    return (distance > threshold).astype(np.uint8), "uniform_background_foreground", None


def _local_candidates(image: Image.Image) -> dict:
    image_width, image_height = image.size
    mask, method, background_warning = _local_foreground_mask(image)
    if mask is None:
        return {
            "status": "manual_selection_required",
            "method": method,
            "source_dimensions": {"width_px": image_width, "height_px": image_height},
            "coordinate_system": "pixels_after_EXIF_orientation",
            "candidates": [],
            "candidate_pair_found": False,
            "front_back_classified": False,
            "requires_operator_confirmation": True,
            "score_type": "uncalibrated_geometry_heuristic",
            "warnings": [background_warning],
        }

    height, width = mask.shape
    kernel_size = max(3, int(round(min(height, width) * 0.018)))
    if kernel_size % 2 == 0:
        kernel_size += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    scale_x = image_width / width
    scale_y = image_height / height
    candidates = []
    minimum_area = max(36, int(width * height * 0.006))
    for label in range(1, count):
        x, y, box_width, box_height, area = (int(value) for value in stats[label])
        box_area = box_width * box_height
        aspect = box_height / max(1, box_width)
        if area < minimum_area or box_area < width * height * 0.012:
            continue
        if not 0.45 <= aspect <= 5.0:
            continue
        fill = area / max(1, box_area)
        if fill < 0.10:
            continue

        source_box = {
            "x": max(0, math.floor(x * scale_x)),
            "y": max(0, math.floor(y * scale_y)),
            "width": 0,
            "height": 0,
        }
        right = min(image_width, math.ceil((x + box_width) * scale_x))
        bottom = min(image_height, math.ceil((y + box_height) * scale_y))
        source_box["width"] = max(1, right - source_box["x"])
        source_box["height"] = max(1, bottom - source_box["y"])

        box_fraction = box_area / max(1, width * height)
        if box_fraction > 0.80:
            continue
        score = min(0.92, 0.35 + 0.30 * fill + 0.20 * min(1.0, box_fraction / 0.20))
        candidates.append({
            "source_bbox_px": source_box,
            "score": round(score, 3),
            "fill_ratio": round(fill, 3),
        })

    candidates.sort(key=lambda candidate: (
        candidate["source_bbox_px"]["y"], candidate["source_bbox_px"]["x"],
    ))
    positions = _position_hints([candidate["source_bbox_px"] for candidate in candidates])
    formatted = []
    for index, (candidate, position) in enumerate(zip(candidates[:8], positions[:8])):
        box = candidate["source_bbox_px"]
        normalized = {
            "x": box["x"] / image_width,
            "y": box["y"] / image_height,
            "width": box["width"] / image_width,
            "height": box["height"] / image_height,
        }
        region = Region(
            part="unknown",
            **normalized,
            confidence=candidate["score"],
            note="Local foreground geometry only; front/back meaning is not classified.",
        )
        formatted.append({
            "candidate_id": f"candidate-{index + 1}",
            "position_hint": position,
            "suggested_side": "unknown",
            "region": region.model_dump(),
            "source_bbox_px": box,
            "geometry_score": candidate["score"],
            "foreground_fill_ratio": candidate["fill_ratio"],
        })

    exact_pair = len(candidates) == 2
    warnings = []
    if not exact_pair:
        warnings.append(
            "Local detection did not find exactly two clear foreground regions. Select or draw both panels manually."
        )
    if exact_pair:
        warnings.append(
            "The two regions are geometric candidates only. Assign front and back before creating the artwork job."
        )
    return {
        "status": "two_candidates_found" if exact_pair else "manual_selection_required",
        "method": method,
        "source_dimensions": {"width_px": image_width, "height_px": image_height},
        "coordinate_system": "pixels_after_EXIF_orientation",
        "candidates": formatted,
        "candidate_pair_found": exact_pair,
        "front_back_classified": False,
        "requires_operator_confirmation": True,
        "score_type": "uncalibrated_geometry_heuristic",
        "warnings": warnings,
    }


def _ai_candidates(image: Image.Image) -> dict:
    analysis = analyze_with_ai(image)
    image_width, image_height = image.size
    regions = [region for region in analysis["regions"] if region["part"] in {"front", "back"}]
    candidates = []
    for index, region in enumerate(regions):
        box = _pixel_box(
            region["x"], region["y"], region["width"], region["height"],
            image_width, image_height,
        )
        candidates.append({
            "candidate_id": f"{region['part']}-{index + 1}",
            "position_hint": None,
            "suggested_side": region["part"],
            "region": region,
            "source_bbox_px": box,
            "geometry_score": region["confidence"],
            "score_type": "provider_confidence_not_independently_calibrated",
        })
    sides = [candidate["suggested_side"] for candidate in candidates]
    side_pair = sides.count("front") == 1 and sides.count("back") == 1
    warnings = list(analysis["issues"])
    if not side_pair:
        warnings.append("AI did not return exactly one front region and one back region.")
    return {
        "status": "ai_side_pair_suggested" if side_pair else "manual_selection_required",
        "method": "configured_ai_region_suggestions",
        "source_dimensions": {"width_px": image_width, "height_px": image_height},
        "coordinate_system": "normalized_exif_corrected_image",
        "candidates": candidates,
        "candidate_pair_found": len(candidates) == 2,
        "front_back_classified": side_pair,
        "requires_operator_confirmation": True,
        "score_type": "provider_confidence_not_independently_calibrated",
        "ai_analysis": {
            "input_type": analysis["input_type"],
            "suggested_tracing": analysis["suggested_tracing"],
            "requires_manual_reconstruction": analysis["requires_manual_reconstruction"],
            "text_observations": analysis["text_observations"],
        },
        "warnings": warnings,
    }


def detect_panels(image: Image.Image, use_ai: bool = False) -> dict:
    """Suggest panel crops. AI is opt-in; local geometry never guesses front vs back."""
    if use_ai:
        return _ai_candidates(image)
    return _local_candidates(image)
