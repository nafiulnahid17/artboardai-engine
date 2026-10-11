import warnings
import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageCms, ImageOps

from .models import CANVAS, EngineError, PanelRequest

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "TIFF"}


def load_image(path: Path) -> Image.Image:
    if not path.is_file():
        raise EngineError("SOURCE_NOT_FOUND", "The source image does not exist.")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise EngineError("SOURCE_TOO_LARGE", "Source files must be at most 30 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                if source.format not in ALLOWED_FORMATS:
                    raise EngineError("UNSUPPORTED_SOURCE", "Use PNG, JPEG, WebP or TIFF.")
                if source.width * source.height > MAX_PIXELS:
                    raise EngineError("PIXEL_LIMIT", "Source exceeds the 40 megapixel limit.")
                if getattr(source, "n_frames", 1) > 1:
                    raise EngineError("MULTIFRAME_SOURCE", "Upload a single image, not an animation or multipage TIFF.")
                oriented = ImageOps.exif_transpose(source)
                source_mode = oriented.mode
                profile_data = oriented.info.get("icc_profile")
                color_status = "assumed_sRGB" if source_mode != "CMYK" else "unmanaged_CMYK_source_to_RGB"
                if source_mode == "CMYK" and not profile_data:
                    raise EngineError("SOURCE_CMYK_PROFILE_REQUIRED", "A CMYK source needs its embedded ICC profile for accurate conversion. Supply a correctly profiled image; the engine will not guess its color space.")
                if profile_data:
                    try:
                        if len(profile_data) > 4 * 1024 * 1024:
                            raise ValueError("ICC profile too large.")
                        alpha = oriented.convert("RGBA").getchannel("A")
                        profile = ImageCms.ImageCmsProfile(io.BytesIO(profile_data))
                        color_input = oriented if oriented.mode in ("RGB", "CMYK", "LAB") else oriented.convert("RGB")
                        oriented = ImageCms.profileToProfile(
                            color_input, profile, ImageCms.createProfile("sRGB"), outputMode="RGB",
                        ).convert("RGBA")
                        oriented.putalpha(alpha)
                        color_status = "embedded_ICC_to_sRGB"
                    except Exception as exc:
                        raise EngineError("INVALID_SOURCE_PROFILE", "Embedded image ICC profile could not be converted safely; provide a correctly profiled source.") from exc
                normalized = oriented.convert("RGBA")
                normalized.info.clear()
                normalized.info["artboard_source_color"] = {"original_mode": source_mode, "normalization": color_status}
                return normalized
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError("INVALID_IMAGE", "The source could not be decoded safely.") from exc


def analyze_image(image: Image.Image) -> dict:
    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    # Bound analysis workload; scores are heuristics, not a calibrated quality grade.
    small = cv2.resize(gray, (min(gray.shape[1], 1000), min(gray.shape[0], 1000)))
    sharpness = float(cv2.Laplacian(small, cv2.CV_64F).var())
    w, h = image.size
    issues = []
    if w < 1000 or h < 1000:
        issues.append("Low source dimensions: tracing cannot recover missing original details.")
    if sharpness < 40:
        issues.append("Possible blur or very flat artwork; visually inspect fine lettering.")
    issues.append("Flat extraction, OCR and tracing require human comparison with the source.")
    return {
        "width_px": w, "height_px": h, "has_transparency": image.getextrema()[3][0] < 255,
        "source_ppi_at_canvas": {"horizontal": round(w / 22, 2), "vertical": round(h / 31, 2)},
        "sharpness_heuristic": round(sharpness, 2), "issues": issues,
        "source_color": image.info.get("artboard_source_color", {"normalization": "inherited_sRGB"}),
        "canvas": CANVAS.model_dump(),
        "part_detection": {"status": "not_requested", "requires_operator_confirmation": True},
    }


def extract_panel(image: Image.Image, request: PanelRequest) -> Image.Image:
    w, h = image.size
    metadata = dict(image.info)
    if request.flatten:
        from .flattening import flatten_panel
        image, flatten_report = flatten_panel(image, request.flatten, request.reference_patches)
        metadata["artboard_flattening"] = flatten_report
    elif request.crop:
        c = request.crop
        if c.x + c.width > w or c.y + c.height > h:
            raise EngineError("CROP_OUT_OF_BOUNDS", f"{request.side} crop exceeds the source dimensions.")
        image = image.crop((c.x, c.y, c.x + c.width, c.y + c.height))
    elif request.perspective_quad:
        quad = np.array([[p.x, p.y] for p in request.perspective_quad], dtype=np.float32)
        if any(p.x >= w or p.y >= h for p in request.perspective_quad):
            raise EngineError("QUAD_OUT_OF_BOUNDS", "Perspective points exceed the source.")
        if not cv2.isContourConvex(quad) or cv2.contourArea(quad, oriented=True) <= 1:
            raise EngineError("INVALID_QUAD", "Use a non-crossing clockwise TL/TR/BR/BL quadrilateral.")
        width = round(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3])))
        height = round(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1])))
        if width < 2 or height < 2 or width * height > MAX_PIXELS:
            raise EngineError("INVALID_QUAD", "Perspective selection is degenerate or too large.")
        target = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
        transform = cv2.getPerspectiveTransform(quad, target)
        image = Image.fromarray(cv2.warpPerspective(np.asarray(image), transform, (width, height)))
    if min(image.size) < 8:
        raise EngineError("PANEL_TOO_SMALL", "Each selected panel must be at least 8 pixels in each dimension.")
    if request.remove_edge_background:
        image = remove_edge_background(image, request.background_tolerance)
    if image.getextrema()[3][1] == 0:
        raise EngineError("EMPTY_PANEL", "The extraction contains no visible pixels.")
    alpha = np.asarray(image.getchannel("A"))
    if np.any((alpha > 0) & (alpha < 255)):
        if request.alpha_matte_rgb is None:
            raise EngineError("ALPHA_NEEDS_MATTE", "PostScript Level 2 does not support partial transparency. Choose an explicit alpha_matte_rgb background before tracing.")
        matte = Image.new("RGBA", image.size, (*request.alpha_matte_rgb, 255))
        matte.alpha_composite(image)
        image = matte
    image.info.update(metadata)
    return image


def remove_edge_background(image: Image.Image, tolerance: int) -> Image.Image:
    """Remove only near-uniform background components touching image edges."""
    pixels = np.asarray(image).copy()
    corners = np.array([pixels[0, 0, :3], pixels[0, -1, :3], pixels[-1, 0, :3], pixels[-1, -1, :3]])
    background = np.median(corners, axis=0)
    if np.max(np.abs(corners.astype(float) - background)) > tolerance:
        raise EngineError("NONUNIFORM_BACKGROUND", "Automatic edge removal needs a near-uniform background. Use a crop or mask instead.")
    candidate = (np.max(np.abs(pixels[:, :, :3].astype(float) - background), axis=2) <= tolerance).astype(np.uint8)
    _, labels = cv2.connectedComponents(candidate, connectivity=8)
    edge_labels = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    remove = np.isin(labels, edge_labels[edge_labels != 0])
    pixels[remove, 3] = 0
    return Image.fromarray(pixels)
