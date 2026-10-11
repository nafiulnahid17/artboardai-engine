import json
import math
import hashlib
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageCms
from svgpathtools import CubicBezier, Line, QuadraticBezier

from .models import CANVAS, EngineError
from .tracing import Shape


class ColorConverter:
    def __init__(self, profile: Path | None = None):
        self.profile = profile
        self.cache: dict[tuple, tuple] = {}
        self.transform = None
        self.profile_info = None
        if profile is not None:
            if not profile.is_file():
                raise EngineError("ICC_NOT_FOUND", "Configured CMYK profile does not exist.")
            try:
                if profile.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("ICC profile exceeds size limit.")
                opened = ImageCms.getOpenProfile(str(profile))
                if opened.profile.xcolor_space.strip() != "CMYK":
                    raise ValueError("Target ICC profile is not CMYK.")
                self.transform = ImageCms.buildTransform(
                    ImageCms.createProfile("sRGB"), opened,
                    "RGB", "CMYK", renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                )
                self.profile_info = {
                    "sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
                    "description": ImageCms.getProfileDescription(opened).strip(),
                    "color_space": "CMYK",
                    "rendering_intent": "relative_colorimetric",
                }
            except Exception as exc:
                raise EngineError("INVALID_CMYK_PROFILE", "Could not build an sRGB-to-CMYK transform from the configured profile.") from exc

    def convert(self, rgb: tuple[int, int, int]) -> tuple[float, float, float, float]:
        if rgb not in self.cache:
            if self.transform:
                pixel = Image.new("RGB", (1, 1), rgb)
                cmyk = ImageCms.applyTransform(pixel, self.transform).getpixel((0, 0))
                self.cache[rgb] = tuple(x / 255 for x in cmyk)
            else:
                r, g, b = (x / 255 for x in rgb)
                k = 1 - max(r, g, b)
                self.cache[rgb] = (0, 0, 0, 1) if k >= 1 else (
                    (1 - r - k) / (1 - k), (1 - g - k) / (1 - k),
                    (1 - b - k) / (1 - k), k,
                )
        return self.cache[rgb]

    def report(self) -> dict:
        return {
            "eps_color_space": "DeviceCMYK",
            "method": "ICC_relative_colorimetric" if self.transform else "unmanaged_mathematical_conversion",
            "icc_profile": self.profile.name if self.profile else None,
            "icc_profile_details": self.profile_info,
            "printer_color_review_required": True,
            "svg_color_space": "sRGB_interchange_with_CMYK_metadata",
            "icc_embedded_in_eps": False,
            "palette_max_total_ink_percent": round(max((sum(v) * 100 for v in self.cache.values()), default=0), 3),
            "printer_total_ink_limit": "must_be_supplied_or_confirmed_by_printer",
        }


def export_svg(shapes: list[Shape], target: Path, colors: ColorConverter, side: str):
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    ns = "{http://www.w3.org/2000/svg}"
    root = ET.Element(ns + "svg", {
        "width": "22in", "height": "31in", "viewBox": "0 0 1584 2232",
        "version": "1.1",
    })
    ET.SubElement(root, ns + "title").text = f"Artboard Ai — {side} body"
    ET.SubElement(root, ns + "metadata").text = json.dumps({
        "canvas": CANVAS.model_dump(), "color": colors.report(),
        "notice": "SVG display colors are sRGB, not a guaranteed CMYK production master.",
    })
    # Preserve painting order. Reordering by color would change stacked artwork.
    for index, shape in enumerate(shapes):
        group = ET.SubElement(root, ns + "g", {"id": f"{side}-shape-{index:05}", "data-layer": shape.layer})
        ET.SubElement(group, ns + "path", {
            "d": shape.path.d(), "fill": "#" + "".join(f"{x:02x}" for x in shape.rgb),
            "fill-rule": "nonzero", "data-cmyk": ",".join(f"{x:.6f}" for x in colors.convert(shape.rgb)),
        })
    ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)


def number(value: float) -> str:
    if not math.isfinite(value):
        raise EngineError("INVALID_GEOMETRY", "A path contains non-finite coordinates.")
    return f"{value:.5f}".rstrip("0").rstrip(".") or "0"


def point(value: complex) -> str:
    return f"{number(value.real)} {number(value.imag)}"


def export_eps(shapes: list[Shape], target: Path, colors: ColorConverter, side: str):
    lines = [
        "%!PS-Adobe-3.0 EPSF-3.0",
        f"%%Title: Artboard Ai - {side} body",
        "%%Creator: Artboard Ai Engine",
        "%%BoundingBox: 0 0 1584 2232",
        "%%HiResBoundingBox: 0 0 1584 2232",
        "%%LanguageLevel: 2", "%%DocumentProcessColors: Cyan Magenta Yellow Black",
        "%%Pages: 1", "%%EndComments", "gsave",
        "0 2232 translate", "1 -1 scale",
        "newpath 0 0 moveto 1584 0 lineto 1584 2232 lineto 0 2232 lineto closepath clip",
    ]
    for shape in shapes:
        lines.append(" ".join(number(c) for c in colors.convert(shape.rgb)) + " setcmykcolor")
        lines.append("newpath")
        for subpath in shape.path.continuous_subpaths():
            lines.append(point(subpath.start) + " moveto")
            for segment in subpath:
                if isinstance(segment, Line):
                    lines.append(point(segment.end) + " lineto")
                elif isinstance(segment, CubicBezier):
                    lines.append(" ".join(point(p) for p in [segment.control1, segment.control2, segment.end]) + " curveto")
                elif isinstance(segment, QuadraticBezier):
                    c1 = segment.start + 2 / 3 * (segment.control - segment.start)
                    c2 = segment.end + 2 / 3 * (segment.control - segment.end)
                    lines.append(" ".join(point(p) for p in [c1, c2, segment.end]) + " curveto")
                else:
                    raise EngineError("UNSUPPORTED_EPS_PATH", "Cannot encode this geometry as PostScript Level 2.")
            if subpath.isclosed():
                lines.append("closepath")
        lines.append("fill")
    lines += ["grestore", "showpage", "%%EOF"]
    target.write_text("\n".join(lines) + "\n", encoding="ascii")


def ensure_formats(formats: list[str], native_ai_mode: str = "disabled"):
    if "ai" in formats and native_ai_mode != "desktop_handoff":
        raise EngineError(
            "NATIVE_AI_REQUIRES_ILLUSTRATOR",
            "Native .ai export requires a validated Illustrator Save As workflow. Export SVG/EPS and save as AI in Illustrator; this engine will not rename another format to .ai.",
        )
