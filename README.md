# Artboard Ai Engine

**A Tool Of Jersey OS**

Headless engine for preparing front and back jersey artwork. No web UI is included.

## Locked contract

- **Two panels:** front body and back body.
- **Each canvas:** 22 inches wide × 31 inches high (558.8 × 787.4 mm).
- **SVG page:** `width="22in" height="31in" viewBox="0 0 1584 2232"`.
- **EPS bounding box:** `0 0 1584 2232` PostScript points.
- **EPS standard:** EPSF 3.0 / PostScript Level 2, actual DeviceCMYK vector paths.
- **DPI choices:** 330 (7260 × 10230 px) or 600 (13200 × 18600 px).
- Entirely vector output has no raster DPI. These pixel dimensions are equivalents,
  not a reason to allocate enormous raster images.

## Implemented

1. Validated PNG/JPEG/WebP/single-frame TIFF input with EXIF orientation.
2. Local geometric proposals for two foreground regions on transparent or uniform
   backgrounds, plus optional structured AI-assisted front/back suggestions.
3. Operator-confirmed rectangular crops or four-point perspective correction. One
   uploaded source can be reused for both the front and back panel jobs.
4. Optional uniform edge-background removal (not general garment segmentation).
5. Tesseract OCR with confidence scores and word positions.
   Operator-confirmed text can be rebuilt using a supplied verified TTF/OTF font,
   HarfBuzz shaping and real glyph outlines. No font is silently guessed.
6. Real color vector tracing with VTracer: lines, fitted curves, speckle filtering.
7. Path cleanup and complexity limits. Stacked painting order is preserved.
8. Aspect-preserving placement on the two locked physical canvases.
9. Editable path-only SVG, CMYK EPS, rendered comparison PNG previews.
10. JSON preflight reports, a bounded background-job API, ZIP downloads and
    artwork review notes and export diagnostics.
11. Source/render fidelity metrics and difference heatmaps (not print-color proofs).
12. Disposable Linux/POSIX workers with a five-minute wall-clock timeout, CPU/file
    limits and process-group termination. A native tracer crash cannot crash the API.

## Important boundaries

**No raster-in-SVG shortcut.** SVG/EPS exports contain actual paths, no embedded
images. Tracing remains an approximation of the input; it does not recover the
original designer's shapes, fonts, or editable layers. Group IDs identify traced
shapes/colors, not reconstructed semantic design layers.

**Mockup extraction is not solved by cropping.** Perspective correction and simple
background removal are implemented. Automatic removal of complex fabric folds,
shadows, collar/armhole details and hidden-artwork reconstruction are not. A clear
flat source or manual reconstruction is still needed for accurate production.

**Local panel detection is geometric, not semantic.** It can suggest two separate
foreground boxes when the image has transparency or a uniform background. Without
configured AI it cannot tell which box is front versus back; both boxes must be
assigned and confirmed before a job is submitted. Busy backgrounds, overlapping
views, white-on-white garments and more or fewer than two clear regions require
manual selection. Detection scores are heuristics, not calibrated probabilities.

**OCR is advisory.** It neither recovers the original font nor automatically trusts
detected lettering. For text rebuilding, provide confirmed text, a verified font,
position and size. The engine produces editable glyph paths; it retains text
parameters in the job report for engine-level re-editing. EPS does not depend on
installed fonts. Choose an explicit source erase rectangle/background when replacing
old lettering; otherwise both original traced lettering and the new layer remain.

**Transparency:** EPS Level 2 cannot retain partial opacity. Partially transparent
inputs require an explicit `alpha_matte_rgb`; they are never silently flattened.
Binary transparency is supported. Embedded input ICC profiles are converted to
sRGB before tracing; invalid embedded profiles fail explicitly. Unprofiled CMYK
source images are rejected rather than converted with guessed print colors.

**CMYK:** an ICC/ICM profile is optional. Without it, EPS uses a disclosed
unmanaged mathematical conversion; exports are still available. ICC values
are converted, but the ICC profile is not embedded in EPS. SVG uses sRGB for display
and records the CMYK values as metadata; standard SVG is not a reliable CMYK master.

**AI:** the provider adapter is implemented but is not automatically provisioned.
It suggests part regions, quality issues, text observations and a tracing mode.
Suggestions never change crops or exports without operator confirmation.
Configuration and paid live-AI validation belong to the configuration phase.

**Native AI:** default `.ai` requests still fail with `NATIVE_AI_REQUIRES_ILLUSTRATOR`.
Explicit `native_ai_mode: "desktop_handoff"` enables the
real Illustrator Save As workflow described below. Linux does not create AI files.
Open the EPS/SVG in Illustrator and use Save As AI. Another file is never merely
renamed to `.ai`.

**Printer production phase is out of scope.** ICC/ICM conversion is optional and
does not gate artwork exports. No printer approval, proofing, bleed, seam allowance
or cutting pattern is claimed or invented. Actual Illustrator/CorelDRAW X8
compatibility still requires testing in those applications.

## Installation

Requires Python 3.11+, Tesseract with desired language data, and Ghostscript.
Install the package using your normal Python package manager:

```sh
python -m pip install .
```

In the provided Replit project, dependencies are installed by the platform.
From the project root, run directly:

```sh
PYTHONPATH=engine python -m artboard_engine.cli canvas
PYTHONPATH=engine python -m artboard_engine.cli analyze /path/to/image.png
PYTHONPATH=engine python -m artboard_engine.cli process \
  --request /path/to/job.json --output generated-artifacts/my-job \
  --cmyk-profile /path/to/printer-cmyk.icc
```

For a standalone checkout use `PYTHONPATH=.` instead of `PYTHONPATH=engine`.
Example job specification: `examples/job.json`. Relative sources resolve against
the JSON file's directory. A job must include one front and one back. Use a new,
empty output directory; existing artwork is never overwritten.

For a source containing multiple panels, select each side using pixel coordinates:

```json
{
  "side": "front",
  "source": "mockup.png",
  "crop": {"x": 10, "y": 20, "width": 400, "height": 600}
}
```

Four-point perspective input is an alternative to a crop:

```json
{
  "side": "back",
  "source": "photo.png",
  "perspective_quad": [
    {"x": 10, "y": 10}, {"x": 400, "y": 20},
    {"x": 390, "y": 620}, {"x": 20, "y": 610}
  ]
}
```

Coordinates refer to the EXIF-oriented image. Order: TL, TR, BR, BL.

## API for the later web UI

```sh
PYTHONPATH=engine python -m artboard_engine.server
```

Defaults to **localhost**, port 8000 (or `PORT`). No public service is configured
in this engine-first phase. Network binding requires a server-side engine token.
This is a private worker credential, not a user-account authentication system.
The later web backend should own user authentication and project authorization;
do not expose worker credentials in browser code.

### Railway / Railpack

`railpack.json` defines the server start command, sets `ARTBOARD_HOST=0.0.0.0`,
and installs Tesseract plus Ghostscript in the runtime image. Railway supplies the
`PORT` value read by the server. Before deploying, add `ARTBOARD_ENGINE_TOKEN` as a
Railway secret variable. The server intentionally exits if it is asked to bind
publicly without that token. This worker token does not provide per-user
authorization; put the engine behind an authenticated web/API gateway before
accepting client artwork.

`requirements.txt` installs the local project (`.`), so Railpack's pip provider
also installs the dependencies maintained in `pyproject.toml` without duplicating
the dependency list.

| Endpoint | Purpose |
|---|---|
| `GET /api/healthz` | Health |
| `GET /api/engine/capabilities` | Locked canvas, real capabilities/limits |
| `POST /api/engine/sources` | Multipart image upload (`file`) |
| `POST /api/engine/fonts` | Validated TTF/OTF upload; returns `font_id` |
| `GET /api/engine/sources/{uuid}/preview` | EXIF-corrected image preview |
| `POST /api/engine/sources/{uuid}/detect-panels?ai=false` | Local geometry candidates; set `ai=true` only after configuring the provider |
| `POST /api/engine/sources/{uuid}/analyze?ai=false` | Local analysis; AI opt-in |
| `POST /api/engine/jobs` | Enqueue front/back processing |
| `GET /api/engine/jobs/{uuid}` | Stage, progress, result/preflight |
| `POST /api/engine/jobs/{uuid}/cancel` | Cancel queued work only |
| `GET /api/engine/jobs/{uuid}/files/{filename}` | Allowlisted output download |
| `GET /api/engine/jobs/{uuid}/panels/{front-or-back}/source-preview` | Extracted source for comparison |
| `GET /api/engine/jobs/{uuid}/bundle` | SVG/EPS/preview/report ZIP |
| `GET /api/engine/openapi.json` | Generated API contract for UI integration |

For one combined image, upload it once, call `detect-panels`, then assign the
confirmed front/back regions to two entries in `/jobs`. Both entries may use the
same `source_id` with different pixel `crop` boxes. Local detection never assigns
front/back semantics; configured AI can suggest those labels, but operator review
remains required.

API job payload: use `source_id` instead of a local `source` path. Arbitrary local
paths are never accepted through HTTP. The API validates selections before queueing.
One worker and four outstanding jobs bound memory pressure. Job/source records
persist to a local workspace; interrupted jobs are marked failed on restart,
not silently resumed or falsely completed. Object storage and a durable distributed
queue are later configuration work, not built-in claims.

### Confirmed text layers

CLI example inside a panel (coordinates are PostScript points, 72 points per inch):

```json
{
  "text_layers": [{
    "text": "TEAM NAME",
    "font": "verified-font.ttf",
    "verified_by_operator": true,
    "x_points": 240,
    "baseline_y_points": 500,
    "size_points": 90,
    "rgb": [15, 25, 40],
    "erase_source_rect": {"x": 60, "y": 100, "width": 260, "height": 60},
    "erase_background_rgb": [255, 255, 255]
  }]
}
```

For HTTP use `font_id` returned by `/fonts` instead of a local `font` path.
No arbitrary local font paths are accepted over HTTP. Erase coordinates reference
the extracted panel before tracing/downsampling. Text extending beyond the locked
artboard fails rather than silently clipping.

### Smoke verification

```sh
PYTHONPATH=engine python engine/scripts/smoke.py
```

Verifies the engine's local contract on generated fixtures and writes ignored
sample outputs under `generated-artifacts/`. This is not final production testing.

### Configuration interface (no secret values committed)

- `ARTBOARD_WORKSPACE`: private source/job storage (default `engine/.runtime`).
- `ARTBOARD_CMYK_PROFILE`: path to a printer-supplied CMYK ICC profile.
- `ARTBOARD_HOST`: localhost by default.
- `PORT`: API port.
- `ARTBOARD_ENGINE_TOKEN`: required for network binding; configure using secrets.
- `OPENROUTER_API_KEY`: OpenRouter secret used by the Railway engine.
- `OPENROUTER_BASE_URL`: optional OpenRouter-compatible endpoint; defaults to
  `https://openrouter.ai/api/v1`.
- `ARTBOARD_AI_MODEL`: primary vision model; defaults to
  `openai/gpt-6.1-sol-pro`.
- `ARTBOARD_AI_FALLBACK_MODEL`: OpenRouter fallback; defaults to
  `google/gemini-3.1-pro-preview`. The fallback is attempted only after the
  primary request fails or returns invalid structured data.
- `ARTBOARD_AI_GENERATION_MODEL`: optional primary image model for new artwork;
  defaults to `openai/gpt-5.4-image-2`.
- `ARTBOARD_AI_GENERATION_FALLBACK_MODEL`: optional image-generation fallback;
  defaults to `google/gemini-3-pro-image`. Both use the same OpenRouter key.
- For compatibility, `AI_INTEGRATIONS_OPENAI_BASE_URL` and
  `AI_INTEGRATIONS_OPENAI_API_KEY` are still accepted when the base URL is
  OpenRouter.

Do not commit keys, uploaded client artwork, runtime jobs or generated outputs.

## Development sequence

User-approved order: **engine → web UI → configuration → testing**.
The web UI now exposes source-photo rebuild and new-brief generation workflows;
the new generation route still requires the engine code to be deployed and
verified against the live OpenRouter account.
Printer production approval, physical proofs and a "Go for production" phase are excluded.
ICC/ICM transforms remain optional engineering support, not export prerequisites.
Licensed Illustrator is still required to execute genuine native AI Save As.

## AI artwork generation

`POST /api/engine/generate` accepts a written design brief, optional front/back
notes, a 330 or 600 DPI raster-content target, and SVG/EPS output selection. The
engine calls the configured OpenRouter image models for one front and one back
draft, then runs each generated raster through the existing local OCR, trace,
CMYK conversion and export pipeline. The back request receives the generated
front as a visual reference to encourage a matching set.

This route is available only when `OPENROUTER_API_KEY` is configured. Generated
image files are drafts; text, logos, details and traced paths require operator
review. The capability endpoint reports the configured model names, but that
does not guarantee live provider availability. It does not create a native AI
file or certify an EPS/ICC/printer workflow.

## Guided flattening and native export

The web UI does not claim automatic garment dewarping. Guided mesh flattening
remains a separate, operator-controlled engine capability; it is not applied
automatically to uploaded photos or generated artwork.

- `PanelRequest.flatten`: an operator-verified 2–12 × 2–12 control grid, in
  EXIF-corrected source pixels. Grid rows/columns map to regular points on the
  locked 22 × 31 inch flat panel. Piecewise-affine inverse mapping handles
  visible nonuniform distortion without extrapolating outside the selected
  source. Degenerate, inverted, crossed and overlapping cells are rejected.
- `pixels_per_inch`: 25, 50 or 75 for the intermediate analysis image, NOT
  raster production DPI. Exports remain paths at the locked physical size.
- `occluded_polygons`: source-pixel polygons for folds, obstructions and hidden
  artwork. The engine exports an uncertainty mask and retains the unresolved
  count even if an explicit white alpha matte is selected.
- `reference_patches`: approved reference image + optional crop + normalized
  target box + `operator_verified: true`. Only unknown pixels are replaced.
  The API uses existing `source_id` values, never arbitrary local paths.
- Missing artwork without a supplied reference is flagged for artwork review.
  No image-only algorithm can recover exactly what is not visible.
- The guided mesh does not automatically detect cloth geometry or remove
  fabric lighting/shadows. An operator must confirm anchors, geometry and
  reference alignment. Real client mockups still need acceptance testing.

To request genuine native AI, use all three formats and opt in explicitly:

```json
{
  "formats": ["svg", "eps", "ai"],
  "native_ai_mode": "desktop_handoff",
  "dpi": 600,
  "panels": [
    {"side": "front", "source": "front.png"},
    {"side": "back", "source": "back.png"}
  ]
}
```

Optionally pass `--cmyk-profile /path/to/a-cmyk-profile.icc` (or `.icm`) to the CLI. The API
accepts `POST /api/engine/profiles` and a returned `profile_id` in the job body.
Profiles are checked for CMYK transform support, size, description and SHA-256.
CMYK palette ink totals are reported; actual printer ink limits are not guessed.

Such a job finishes the Linux stages as **`awaiting_desktop`**.
The download bundle includes original EPS/SVG, separate front/back Illustrator
scripts and export instructions; an ICC is included only if explicitly supplied.
**It does not contain AI files until Illustrator actually saves them.**

1. On a licensed Illustrator installation, open the original EPS. If an ICC was
   supplied, assign it while preserving CMYK numbers. Otherwise explicitly
   confirm unmanaged CMYK export. Run the matching script.
   It uses `IllustratorSaveOptions`, targets Illustrator CS6, includes
   PDF compatibility (ICC embedding only when supplied), reopens the AI, checks dimensions/CMYK/paths/no raster
   or fonts, and writes a desktop receipt. It refuses to overwrite existing AI.
2. Return the saved AI and receipt:

```sh
python -m artboard_engine native-import --output outputs/order \
  --side front --ai returned/front-body.ai \
  --receipt returned/front-illustrator-receipt.json
```

3. API alternatives: `POST /api/engine/jobs/{uuid}/native/{front|back}`
   (multipart `file`, `receipt_json`). Receipt validation binds each AI to the
   source EPS and bundle; any supplied profile must match.
4. SVG/EPS export does not wait for native AI, ICC, printer sign-off or desktop
   validation matrices. Native AI alone waits for Illustrator execution.
   Printer-production approval and desktop-check endpoints are not exposed.

Native imports reject ordinary PDFs/EPS renamed `.ai`, require PDF-compatible
Illustrator private editing data, check page size and reject PDF image objects.
These are structural checks, not cryptographic proof that Adobe executed.
Desktop receipts are authenticated, operator-verified
evidence; they are not remote attestation. Profile assignment in Illustrator
is operator-confirmed, not automatically measured through its scripting DOM.
Do not mix CLI and API writes on the same job.

Run `python scripts/production_checks.py --test-profile /path/to/generic-test.icc`
for software checks. A generic ICC fixture is **not** your client's printer ICC.
Actual Illustrator execution and positive import of its real native output
remain unverified here. CorelDRAW X8 interoperability has not been live-tested;
printer-specific color matching and physical production are outside this scope.
