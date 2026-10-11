"""Guided inverse mesh mapping. Never guess occluded artwork or fold geometry."""
import cv2
import numpy as np
from PIL import Image

from .models import CANVAS, EngineError, FlattenSettings, ReferencePatch


def flatten_panel(
    source: Image.Image, settings: FlattenSettings, patches: list[ReferencePatch],
) -> tuple[Image.Image, dict]:
    from .images import load_image
    if source.mode != "RGBA":
        source = source.convert("RGBA")
    grid = np.array([[[p.x, p.y] for p in row] for row in settings.control_grid], dtype=np.float32)
    rows, cols = grid.shape[:2]
    if np.any(grid[:, :, 0] > source.width - 1) or np.any(grid[:, :, 1] > source.height - 1):
        raise EngineError("MESH_OUT_OF_BOUNDS", "All flattening anchors must lie inside the oriented source image.")
    cells = []
    for row in range(rows - 1):
        for col in range(cols - 1):
            quad = np.array([grid[row, col], grid[row, col + 1], grid[row + 1, col + 1], grid[row + 1, col]])
            if not cv2.isContourConvex(quad) or cv2.contourArea(quad, oriented=True) < 2:
                raise EngineError("INVERTED_MESH", "The grid contains a crossed, reversed or degenerate cell. Mark folded-over areas as occluded rather than guessing their geometry.")
            for previous in cells:
                overlap, _ = cv2.intersectConvexConvex(quad, previous)
                if overlap > 0.5:
                    raise EngineError("OVERLAPPING_MESH", "Source grid cells overlap. An occluded fold is not invertible; supply a verified reference.")
            cells.append(quad)
    width = CANVAS.width_inches * settings.pixels_per_inch
    height = CANVAS.height_inches * settings.pixels_per_inch
    xs, ys = np.linspace(0, width - 1, cols), np.linspace(0, height - 1, rows)
    map_x = np.empty((height, width), np.float32)
    map_y = np.empty((height, width), np.float32)
    # Piecewise-affine triangles give a continuous map without cubic overshoot.
    for row in range(rows - 1):
        ya, yb = round(ys[row]), round(ys[row + 1])
        for col in range(cols - 1):
            xa, xb = round(xs[col]), round(xs[col + 1])
            u, v = np.meshgrid(
                np.linspace(0, 1, xb - xa + 1, dtype=np.float32),
                np.linspace(0, 1, yb - ya + 1, dtype=np.float32),
            )
            a, b, c, d = grid[row, col], grid[row, col + 1], grid[row + 1, col + 1], grid[row + 1, col]
            uv = np.where(
                (v <= u)[:, :, None],
                (1 - u)[:, :, None] * a + (u - v)[:, :, None] * b + v[:, :, None] * c,
                (1 - v)[:, :, None] * a + u[:, :, None] * c + (v - u)[:, :, None] * d,
            )
            map_x[ya:yb + 1, xa:xb + 1] = uv[:, :, 0]
            map_y[ya:yb + 1, xa:xb + 1] = uv[:, :, 1]
    pixels = np.asarray(source)
    rgb = cv2.remap(pixels[:, :, :3], map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    alpha = cv2.remap(pixels[:, :, 3], map_x, map_y, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
    source_valid = np.full((source.height, source.width), 255, np.uint8)
    for polygon in settings.occluded_polygons:
        points = np.array([[p.x, p.y] for p in polygon], np.float32)
        if np.any(points[:, 0] > source.width - 1) or np.any(points[:, 1] > source.height - 1):
            raise EngineError("OCCLUSION_OUT_OF_BOUNDS", "Occlusion polygons must stay inside the source.")
        if abs(cv2.contourArea(points)) < 1:
            raise EngineError("INVALID_OCCLUSION", "Occlusion polygon has no measurable area.")
        cv2.fillPoly(source_valid, [np.rint(points).astype(np.int32)], 0)
    validity = cv2.remap(source_valid, map_x, map_y, cv2.INTER_NEAREST)
    alpha[validity == 0] = 0
    output = np.dstack([rgb, alpha]).copy()
    missing_before = int(np.count_nonzero(output[:, :, 3] == 0))
    patch_reports = []
    for patch in patches:
        donor = load_image(patch.source)
        if patch.source_crop:
            crop = patch.source_crop
            if crop.x + crop.width > donor.width or crop.y + crop.height > donor.height:
                raise EngineError("REFERENCE_CROP_OUT_OF_BOUNDS", "Reference artwork crop exceeds its source.")
            donor = donor.crop((crop.x, crop.y, crop.x + crop.width, crop.y + crop.height))
        target = patch.target
        xa, ya = round(target.x * width), round(target.y * height)
        xb, yb = round((target.x + target.width) * width), round((target.y + target.height) * height)
        if xb <= xa or yb <= ya:
            raise EngineError("REFERENCE_TOO_SMALL", "Reference target is smaller than one output pixel.")
        donor_pixels = np.asarray(donor.resize((xb - xa, yb - ya), Image.Resampling.LANCZOS))
        region = output[ya:yb, xa:xb]
        missing = region[:, :, 3] == 0
        region[missing] = donor_pixels[missing]
        patch_reports.append({
            "source_file": patch.source.name, "target": target.model_dump(),
            "filled_pixels": int(np.count_nonzero(missing & (donor_pixels[:, :, 3] > 0))),
            "operator_verified": True, "method": "supplied_reference_only",
        })
    missing_after = int(np.count_nonzero(output[:, :, 3] == 0))
    result = Image.fromarray(output)
    result.info.update(source.info)
    result.info["artboard_uncertainty_mask"] = Image.fromarray(((output[:, :, 3] == 0) * 255).astype(np.uint8))
    return result, {
        "method": "operator_guided_piecewise_affine_mesh", "automatic": False,
        "grid_size": [rows, cols], "output_pixels": [width, height],
        "occlusion_count": len(settings.occluded_polygons),
        "missing_pixels_before_references": missing_before,
        "unresolved_pixels": missing_after,
        "unresolved_fraction": round(missing_after / (width * height), 7),
        "reference_patches": patch_reports,
        "requires_reconstruction": missing_after > 0,
        "fold_geometry_operator_verified": True,
        "shadows_removed": False,
        "notice": "Corrects supplied visible geometry; does not infer hidden designs or remove lighting/shading. Unknown areas cannot be production-approved without a valid reference.",
    }
