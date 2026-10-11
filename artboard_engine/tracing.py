import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import vtracer
from defusedxml import ElementTree as SafeET
from PIL import Image
from svgpathtools import CubicBezier, Line, QuadraticBezier, parse_path

from .models import CANVAS, EngineError, TraceSettings

NS = "{http://www.w3.org/2000/svg}"
MAX_PATHS = 60_000
MAX_SEGMENTS = 1_000_000


@dataclass
class Shape:
    path: object
    rgb: tuple[int, int, int]
    layer: str


def trace_artwork(
    image: Image.Image,
    directory: Path,
    settings: TraceSettings,
    on_vectorized: Callable[[], None] | None = None,
) -> tuple[list[Shape], dict]:
    work = image.copy()
    work.thumbnail((settings.max_dimension, settings.max_dimension))
    work.save(directory / "trace-input.png")
    output = directory / "trace-source.svg"
    vtracer.convert_image_to_svg_py(
        str(directory / "trace-input.png"), str(output),
        colormode="color", hierarchical="stacked", mode=settings.mode,
        filter_speckle=settings.filter_speckle,
        color_precision=settings.color_precision, layer_difference=16,
        corner_threshold=settings.corner_threshold,
        length_threshold=settings.length_threshold, max_iterations=10,
        splice_threshold=settings.splice_threshold, path_precision=3,
    )
    if on_vectorized:
        on_vectorized()
    root = SafeET.parse(output).getroot()
    width, height = work.size
    # Fit without distortion. Any unused canvas area stays transparent.
    scale = min(CANVAS.width_points / width, CANVAS.height_points / height)
    offset = complex((CANVAS.width_points - width * scale) / 2, (CANVAS.height_points - height * scale) / 2)
    shapes = []
    segment_count = 0
    removed_degenerate = 0
    for element in root:
        if element.tag != NS + "path":
            continue
        color = element.attrib.get("fill", "")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise EngineError("INVALID_TRACE_COLOR", "The tracer returned an unsupported color.")
        path = parse_path(element.attrib["d"])
        transform = element.attrib.get("transform", "").strip()
        if transform:
            match = re.fullmatch(r"translate\(\s*([-\d.eE]+)[,\s]+([-\d.eE]+)\s*\)", transform)
            if not match:
                raise EngineError("INVALID_TRACE_TRANSFORM", "Unsupported tracer transform.")
            path = path.translated(complex(float(match[1]), float(match[2])))
        clean = []
        for segment in path:
            if not isinstance(segment, (Line, CubicBezier, QuadraticBezier)):
                raise EngineError("INVALID_TRACE_PATH", "Unsupported trace geometry; expected line/quadratic/cubic paths.")
            if isinstance(segment, Line) and abs(segment.end - segment.start) < 1e-8:
                removed_degenerate += 1
                continue
            clean.append(segment)
        if not clean:
            continue
        # Keep continuity/closure and the winding order of compound paths.
        from svgpathtools import Path as VectorPath
        path = VectorPath(*clean).scaled(scale).translated(offset)
        segment_count += len(path)
        if segment_count > MAX_SEGMENTS or len(shapes) >= MAX_PATHS:
            raise EngineError("TRACE_TOO_COMPLEX", "Trace exceeds complexity limits. Reduce colors, increase speckle filtering, or crop the source.")
        shapes.append(Shape(path, tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)), f"color-{color[1:].lower()}"))
    if not shapes:
        raise EngineError("EMPTY_TRACE", "No vector shapes were produced.")
    aspect_warning = abs(width / height - 22 / 31) / (22 / 31) > 0.03
    return shapes, {
        "engine": "vtracer", "path_count": len(shapes), "segment_count": segment_count,
        "removed_zero_length_lines": removed_degenerate,
        "trace_resolution_px": [width, height], "placement": "contain_centered_no_stretch",
        "aspect_ratio_requires_review": aspect_warning,
        "embedded_raster_count": 0,
        "note": "Raster tracing is an approximation. Original editable design layers and fonts cannot be recovered automatically.",
    }
