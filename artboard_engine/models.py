from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EngineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Canvas(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    width_inches: Literal[22] = 22
    height_inches: Literal[31] = 31
    width_points: Literal[1584] = 1584
    height_points: Literal[2232] = 2232
    color_mode: Literal["CMYK"] = "CMYK"

    def raster_size(self, dpi: int) -> tuple[int, int]:
        if dpi not in (330, 600):
            raise EngineError("INVALID_DPI", "Only 330 and 600 DPI are supported.")
        return self.width_inches * dpi, self.height_inches * dpi


CANVAS = Canvas()


class Crop(StrictModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Point(StrictModel):
    x: float = Field(ge=0)
    y: float = Field(ge=0)


class NormalizedBox(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def inside(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Normalized box must stay inside the panel.")
        return self


class FlattenSettings(StrictModel):
    # EXIF-corrected SOURCE pixels. Rows/columns correspond to a regular flat grid.
    control_grid: list[list[Point]] = Field(min_length=2, max_length=12)
    operator_verified: Literal[True]
    pixels_per_inch: Literal[25, 50, 75] = 50
    occluded_polygons: list[list[Point]] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def rectangular_grid(self):
        cols = len(self.control_grid[0])
        if cols < 2 or cols > 12 or any(len(row) != cols for row in self.control_grid):
            raise ValueError("Control grid must be rectangular, with 2–12 rows and columns.")
        if any(len(poly) < 3 or len(poly) > 100 for poly in self.occluded_polygons):
            raise ValueError("Each occlusion polygon needs 3–100 points.")
        return self


class ReferencePatch(StrictModel):
    source: Path
    source_crop: Crop | None = None
    target: NormalizedBox
    operator_verified: Literal[True]


class TraceSettings(StrictModel):
    mode: Literal["spline", "polygon"] = "spline"
    color_precision: int = Field(default=6, ge=1, le=8)
    filter_speckle: int = Field(default=4, ge=0, le=64)
    corner_threshold: int = Field(default=60, ge=0, le=180)
    length_threshold: float = Field(default=4, ge=3, le=10)
    splice_threshold: int = Field(default=45, ge=0, le=180)
    max_dimension: int = Field(default=1800, ge=128, le=2400)


class TextLayer(StrictModel):
    """Operator-confirmed text and a supplied, verified font; never a guessed font."""
    text: str = Field(min_length=1, max_length=500)
    font: Path
    verified_by_operator: Literal[True]
    x_points: float = Field(ge=0, le=1584)
    baseline_y_points: float = Field(ge=0, le=2232)
    size_points: float = Field(gt=0, le=1000)
    rgb: tuple[int, int, int] = (0, 0, 0)
    # Coordinates in the extracted panel, before tracing/downsampling.
    erase_source_rect: Crop | None = None
    erase_background_rgb: tuple[int, int, int] = (255, 255, 255)

    @model_validator(mode="after")
    def verify_text(self):
        if not self.text.strip():
            raise ValueError("Text must include visible characters.")
        if any(not 0 <= value <= 255 for value in (*self.rgb, *self.erase_background_rgb)):
            raise ValueError("RGB values must be between 0 and 255.")
        return self


class PanelRequest(StrictModel):
    side: Literal["front", "back"]
    source: Path
    crop: Crop | None = None
    # Clockwise source coordinates: top-left, top-right, bottom-right, bottom-left.
    perspective_quad: list[Point] | None = Field(default=None, min_length=4, max_length=4)
    remove_edge_background: bool = False
    background_tolerance: int = Field(default=18, ge=0, le=80)
    run_ocr: bool = True
    ocr_language: str = Field(default="eng", pattern=r"^[a-zA-Z0-9_+]{1,48}$")
    trace: TraceSettings = Field(default_factory=TraceSettings)
    text_layers: list[TextLayer] = Field(default_factory=list, max_length=30)
    alpha_matte_rgb: tuple[int, int, int] | None = None
    flatten: FlattenSettings | None = None
    reference_patches: list[ReferencePatch] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def exclusive_selection(self):
        if sum(x is not None for x in [self.crop, self.perspective_quad, self.flatten]) > 1:
            raise ValueError("Choose a rectangular crop, perspective quad OR guided flattening.")
        if self.reference_patches and not self.flatten:
            raise ValueError("Reference patches require guided flattening.")
        if self.alpha_matte_rgb is not None and any(not 0 <= v <= 255 for v in self.alpha_matte_rgb):
            raise ValueError("Matte RGB values must be between 0 and 255.")
        return self


class JobRequest(StrictModel):
    panels: list[PanelRequest] = Field(min_length=2, max_length=2)
    dpi: Literal[330, 600] = 600
    formats: list[Literal["svg", "eps", "ai"]] = Field(default_factory=lambda: ["svg", "eps"])
    native_ai_mode: Literal["disabled", "desktop_handoff"] = "disabled"

    @model_validator(mode="after")
    def one_of_each(self):
        if {p.side for p in self.panels} != {"front", "back"}:
            raise ValueError("Exactly one front body and one back body are required.")
        if not self.formats or len(set(self.formats)) != len(self.formats):
            raise ValueError("Choose at least one format without duplicates.")
        return self


class Region(StrictModel):
    part: Literal["front", "back", "sleeve", "collar", "logo", "text", "unknown"]
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    confidence: float = Field(ge=0, le=1)
    note: str = Field(max_length=1000)

    @model_validator(mode="after")
    def in_bounds(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("Normalized region must stay inside the source image.")
        return self


class AIAnalysis(StrictModel):
    input_type: Literal["flat_artwork", "mockup", "photograph", "unknown"]
    regions: list[Region] = Field(max_length=50)
    issues: list[str] = Field(max_length=30)
    suggested_tracing: Literal["spline", "polygon"]
    requires_manual_reconstruction: bool
    text_observations: list[str] = Field(max_length=50)


class Approval(StrictModel):
    reviewer_reference: str = Field(min_length=1, max_length=120)
    dimensions_verified: bool
    artwork_and_text_verified: bool
    printer_color_profile_verified: bool
    bleed_and_pattern_verified: bool
    illustrator_coreldraw_compatibility_verified: bool

    def all_verified(self) -> bool:
        return all([
            self.dimensions_verified, self.artwork_and_text_verified,
            self.printer_color_profile_verified, self.bleed_and_pattern_verified,
            self.illustrator_coreldraw_compatibility_verified,
        ])
