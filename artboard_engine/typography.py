import io
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw
from svgpathtools import parse_path

from .models import CANVAS, EngineError, TextLayer
from .tracing import Shape

MAX_FONT_BYTES = 15 * 1024 * 1024


def validate_font(path: Path) -> bytes:
    if not path.is_file() or path.stat().st_size > MAX_FONT_BYTES:
        raise EngineError("INVALID_FONT", "Supply a valid TTF/OTF file no larger than 15 MiB.")
    data = path.read_bytes()
    if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise EngineError("INVALID_FONT", "Only a single TrueType/OpenType outline font is accepted.")
    try:
        with TTFont(io.BytesIO(data), lazy=False) as font:
            if "glyf" not in font and "CFF " not in font:
                raise ValueError("No supported outline table.")
            if len(font.getGlyphOrder()) > 100_000:
                raise ValueError("Font exceeds glyph limits.")
    except Exception as exc:
        raise EngineError("INVALID_FONT", "The font has no readable supported vector outlines.") from exc
    return data


def erase_confirmed_text(image: Image.Image, layers: list[TextLayer]) -> Image.Image:
    work = image.copy()
    draw = ImageDraw.Draw(work)
    for layer in layers:
        if layer.erase_source_rect:
            rect = layer.erase_source_rect
            if rect.x + rect.width > work.width or rect.y + rect.height > work.height:
                raise EngineError("TEXT_ERASE_OUT_OF_BOUNDS", "Text erase rectangle exceeds the extracted panel.")
            draw.rectangle(
                [rect.x, rect.y, rect.x + rect.width - 1, rect.y + rect.height - 1],
                fill=(*layer.erase_background_rgb, 255),
            )
    return work


def build_text_paths(layer: TextLayer, index: int) -> tuple[list[Shape], dict]:
    data = validate_font(layer.font)
    face = hb.Face(data)
    shaping_font = hb.Font(face)
    shaping_font.scale = (face.upem, face.upem)
    hb.ot_font_set_funcs(shaping_font)
    buffer = hb.Buffer()
    buffer.add_str(layer.text)
    buffer.guess_segment_properties()
    hb.shape(shaping_font, buffer)
    scale = layer.size_points / face.upem
    shapes = []
    cursor_x = layer.x_points
    cursor_y = layer.baseline_y_points
    with TTFont(io.BytesIO(data)) as font:
        glyphs = font.getGlyphSet()
        glyph_order = font.getGlyphOrder()
        for info, position in zip(buffer.glyph_infos, buffer.glyph_positions):
            if info.codepoint == 0:
                raise EngineError("FONT_MISSING_GLYPH", "The supplied font cannot render every character in the confirmed text.")
            pen = SVGPathPen(glyphs)
            glyphs[glyph_order[info.codepoint]].draw(pen)
            commands = pen.getCommands()
            if commands:
                path = parse_path(commands).scaled(scale, -scale).translated(complex(
                    cursor_x + position.x_offset * scale,
                    cursor_y - position.y_offset * scale,
                ))
                xmin, xmax, ymin, ymax = path.bbox()
                if xmin < -0.01 or ymin < -0.01 or xmax > CANVAS.width_points + 0.01 or ymax > CANVAS.height_points + 0.01:
                    raise EngineError("TEXT_OUT_OF_CANVAS", "The rebuilt text exceeds the locked canvas. Adjust its position or size.")
                shapes.append(Shape(path, layer.rgb, f"verified-text-{index}"))
            cursor_x += position.x_advance * scale
            cursor_y -= position.y_advance * scale
    if not shapes:
        raise EngineError("EMPTY_TEXT_OUTLINE", "Confirmed text produced no visible vector outlines.")
    return shapes, {
        "text": layer.text, "font_file": layer.font.name, "operator_verified": True,
        "x_points": layer.x_points, "baseline_y_points": layer.baseline_y_points,
        "size_points": layer.size_points, "glyph_count": len(shapes),
        "output": "editable_glyph_paths", "shaping": "HarfBuzz",
        "source_text_erased": layer.erase_source_rect is not None,
        "warning": None if layer.erase_source_rect else "Original traced lettering was not removed. Check for duplicate text.",
    }
