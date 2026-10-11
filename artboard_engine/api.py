"""Private headless API for the future UI. Do not expose without an access layer."""
import hmac
import io
import json
import logging
import os
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import Field, model_validator

from .assistant import analyze_with_ai
from .capabilities import capabilities
from .detection import detect_panels
from .exporters import ColorConverter, ensure_formats
from .images import MAX_UPLOAD_BYTES, analyze_image, load_image
from .models import Crop, EngineError, FlattenSettings, JobRequest, NormalizedBox, PanelRequest, Point, ReferencePatch, StrictModel, TextLayer, TraceSettings
from .pipeline import atomic_json
from .isolation import process_isolated
from .typography import MAX_FONT_BYTES, validate_font

logger = logging.getLogger("artboard_engine")


class APIFontLayer(StrictModel):
    font_id: uuid.UUID
    text: str = Field(min_length=1, max_length=500)
    verified_by_operator: Literal[True]
    x_points: float = Field(ge=0, le=1584)
    baseline_y_points: float = Field(ge=0, le=2232)
    size_points: float = Field(gt=0, le=1000)
    rgb: tuple[int, int, int] = (0, 0, 0)
    erase_source_rect: Crop | None = None
    erase_background_rgb: tuple[int, int, int] = (255, 255, 255)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.text.strip() or any(not 0 <= v <= 255 for v in (*self.rgb, *self.erase_background_rgb)):
            raise ValueError("Use visible text and RGB values between 0 and 255.")
        return self


class APIReferencePatch(StrictModel):
    source_id: uuid.UUID
    source_crop: Crop | None = None
    target: NormalizedBox
    operator_verified: Literal[True]


class APIPanel(StrictModel):
    side: Literal["front", "back"]
    source_id: uuid.UUID
    crop: Crop | None = None
    perspective_quad: list[Point] | None = Field(default=None, min_length=4, max_length=4)
    remove_edge_background: bool = False
    background_tolerance: int = Field(default=18, ge=0, le=80)
    run_ocr: bool = True
    ocr_language: str = Field(default="eng", pattern=r"^[a-zA-Z0-9_+]{1,48}$")
    trace: TraceSettings = Field(default_factory=TraceSettings)
    text_layers: list[APIFontLayer] = Field(default_factory=list, max_length=30)
    alpha_matte_rgb: tuple[int, int, int] | None = None
    flatten: FlattenSettings | None = None
    reference_patches: list[APIReferencePatch] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def selection_exclusive(self):
        if sum(x is not None for x in [self.crop, self.perspective_quad, self.flatten]) > 1:
            raise ValueError("Choose crop, perspective OR guided flattening.")
        if self.reference_patches and not self.flatten:
            raise ValueError("Reference patches require guided flattening.")
        if self.alpha_matte_rgb is not None and any(not 0 <= v <= 255 for v in self.alpha_matte_rgb):
            raise ValueError("Matte RGB values must be between 0 and 255.")
        return self


class APIJob(StrictModel):
    panels: list[APIPanel] = Field(min_length=2, max_length=2)
    dpi: Literal[330, 600] = 600
    formats: list[Literal["svg", "eps", "ai"]] = Field(default_factory=lambda: ["svg", "eps"])
    native_ai_mode: Literal["disabled", "desktop_handoff"] = "disabled"
    profile_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def valid_panel_set(self):
        if {p.side for p in self.panels} != {"front", "back"}:
            raise ValueError("Exactly one front and one back are required.")
        if not self.formats or len(set(self.formats)) != len(self.formats):
            raise ValueError("Select at least one format without duplicates.")
        return self


def create_app(workspace: Path, token: str | None = None, cmyk_profile: Path | None = None) -> FastAPI:
    sources = workspace / "sources"
    jobs = workspace / "jobs"
    fonts = workspace / "fonts"
    profiles = workspace / "profiles"
    sources.mkdir(parents=True, exist_ok=True)
    jobs.mkdir(parents=True, exist_ok=True)
    fonts.mkdir(parents=True, exist_ok=True)
    profiles.mkdir(parents=True, exist_ok=True)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artboard")
    slots = threading.BoundedSemaphore(4)
    lock = threading.RLock()
    futures = {}

    @asynccontextmanager
    async def lifespan(_app):
        # File storage survives restarts, but workers do not. Never leave fake progress.
        for state_path in jobs.glob("*/state.json"):
            state = json.loads(state_path.read_text())
            if state["status"] in ("queued", "processing"):
                state.update(status="failed", error={"code": "ENGINE_RESTARTED", "message": "Engine restarted. Submit a new job; partial files were not approved."})
                atomic_json(state_path, state)
        yield
        executor.shutdown(wait=True, cancel_futures=False)

    app = FastAPI(
        title="Artboard Ai Engine", version="0.1.0", lifespan=lifespan,
        docs_url="/api/engine/docs", redoc_url=None, openapi_url="/api/engine/openapi.json",
    )

    @app.exception_handler(EngineError)
    async def engine_error(_request, exc):
        status = 503 if exc.code in ("AI_NOT_CONFIGURED", "OCR_UNAVAILABLE") else 422
        return JSONResponse(status_code=status, content={"error": {"code": exc.code, "message": str(exc)}})

    async def access(authorization: str | None = Header(default=None)):
        if token:
            supplied = (authorization or "").removeprefix("Bearer ")
            if not hmac.compare_digest(supplied, token):
                raise HTTPException(401, "Engine access token required.")

    router = APIRouter(prefix="/api/engine", dependencies=[Depends(access)])

    def source_path(source_id: uuid.UUID) -> Path:
        path = sources / str(source_id) / "input"
        if not path.is_file():
            raise HTTPException(404, "Source not found.")
        return path

    def read_state(job_id: uuid.UUID) -> dict:
        path = jobs / str(job_id) / "state.json"
        if not path.is_file():
            raise HTTPException(404, "Job not found.")
        return json.loads(path.read_text())

    def font_path(font_id: uuid.UUID) -> Path:
        path = fonts / str(font_id)
        if not path.is_file():
            raise HTTPException(404, "Font not found.")
        return path

    def update_state(job_id, **updates):
        with lock:
            state = read_state(job_id)
            state.update(updates)
            atomic_json(jobs / str(job_id) / "state.json", state)

    @app.get("/api/healthz")
    def health():
        return {"status": "ok", "engine": "Artboard Ai"}

    @router.get("/capabilities")
    def get_capabilities():
        return capabilities()

    @router.post("/sources", status_code=201)
    async def upload(file: UploadFile = File(...)):
        source_id = uuid.uuid4()
        directory = sources / str(source_id)
        directory.mkdir()
        path = directory / "input"
        try:
            total = 0
            with path.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Upload exceeds 30 MiB.")
                    stream.write(chunk)
            # Run decoding outside the async event loop.
            from starlette.concurrency import run_in_threadpool
            image = await run_in_threadpool(load_image, path)
            response = {
                "source_id": str(source_id), "width": image.width, "height": image.height,
                "coordinate_system": "pixels_after_EXIF_orientation",
            }
            atomic_json(directory / "source.json", response)
            return response
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @router.get("/sources/{source_id}/preview")
    def source_preview(source_id: uuid.UUID):
        image = load_image(source_path(source_id))
        image.thumbnail((1200, 1200))
        data = io.BytesIO()
        image.save(data, "PNG")
        return StreamingResponse(iter([data.getvalue()]), media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.post("/sources/{source_id}/detect-panels")
    def detect_source_panels(source_id: uuid.UUID, ai: bool = False):
        image = load_image(source_path(source_id))
        return detect_panels(image, use_ai=ai)

    @router.post("/fonts", status_code=201)
    async def upload_font(file: UploadFile = File(...)):
        font_id = uuid.uuid4()
        path = fonts / str(font_id)
        try:
            total = 0
            with path.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_FONT_BYTES:
                        raise HTTPException(413, "Font exceeds 15 MiB.")
                    stream.write(chunk)
            from starlette.concurrency import run_in_threadpool
            await run_in_threadpool(validate_font, path)
            return {"font_id": str(font_id), "original_font_match": "must_be_confirmed_by_operator"}
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @router.post("/sources/{source_id}/analyze")
    def analyze(source_id: uuid.UUID, ai: bool = False):
        image = load_image(source_path(source_id))
        result = analyze_image(image)
        if ai:
            result["ai"] = analyze_with_ai(image)
        return result

    def execute(job_id: uuid.UUID, request: JobRequest, selected_profile: Path | None):
        try:
            update_state(job_id, status="processing", stage="starting", progress=0)
            result = process_isolated(
                request, jobs / str(job_id) / "output", selected_profile,
                lambda stage, progress: update_state(job_id, stage=stage, progress=progress),
            )
            update_state(job_id, status=result["status"], result=result, progress=100)
        except Exception as exc:
            logger.exception("Artwork job failed: %s", job_id)
            update_state(job_id, status="failed", error={
                "code": exc.code if isinstance(exc, EngineError) else "PROCESSING_FAILED",
                "message": str(exc) if isinstance(exc, EngineError) else "Processing failed. Consult engine logs.",
            })

    @router.post("/jobs", status_code=202)
    def create_job(payload: APIJob):
        ensure_formats(payload.formats, payload.native_ai_mode)
        selected_profile = profiles / str(payload.profile_id) if payload.profile_id else cmyk_profile
        if selected_profile:
            ColorConverter(selected_profile)
        request = JobRequest(
            dpi=payload.dpi, formats=payload.formats, native_ai_mode=payload.native_ai_mode,
            panels=[PanelRequest(
                **p.model_dump(exclude={"source_id", "text_layers", "reference_patches"}), source=source_path(p.source_id),
                text_layers=[TextLayer(
                    **layer.model_dump(exclude={"font_id"}), font=font_path(layer.font_id),
                ) for layer in p.text_layers],
                reference_patches=[ReferencePatch(
                    **patch.model_dump(exclude={"source_id"}), source=source_path(patch.source_id),
                ) for patch in p.reference_patches],
            ) for p in payload.panels],
        )
        # Decode and bounds-check selections before putting any work in the queue.
        from .images import extract_panel
        for panel in request.panels:
            extract_panel(load_image(panel.source), panel)
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Engine queue is full. Try again after an active job completes.")
        job_id = uuid.uuid4()
        directory = jobs / str(job_id)
        try:
            directory.mkdir()
            state = {"job_id": str(job_id), "status": "queued", "progress": 0, "stage": "queued", "production_ready": False}
            atomic_json(directory / "state.json", state)
            atomic_json(directory / "request.json", payload.model_dump(mode="json"))
            future = executor.submit(execute, job_id, request, selected_profile)
            futures[str(job_id)] = future
            def completed(_future):
                with lock:
                    futures.pop(str(job_id), None)
                slots.release()
            future.add_done_callback(completed)
            return state
        except Exception:
            slots.release()
            raise

    @router.get("/jobs/{job_id}")
    def get_job(job_id: uuid.UUID):
        return read_state(job_id)

    @router.post("/jobs/{job_id}/cancel")
    def cancel(job_id: uuid.UUID):
        with lock:
            state = read_state(job_id)
            future = futures.get(str(job_id))
            if not future or not future.cancel():
                raise HTTPException(409, "Only queued, not-yet-started jobs can be cancelled.")
            update_state(job_id, status="cancelled", stage="cancelled")
            return read_state(job_id)

    @router.get("/jobs/{job_id}/files/{filename}")
    def download(job_id: uuid.UUID, filename: str):
        state = read_state(job_id)
        if state["status"] not in ("review_required", "awaiting_desktop", "approved"):
            raise HTTPException(409, "Outputs are not ready.")
        allowed = {"manifest.json", "front-body.svg", "front-body.eps", "back-body.svg", "back-body.eps",
                   "front-body.ai", "back-body.ai", "front-body-uncertainty.png", "back-body-uncertainty.png",
                   "front-body-preview.png", "back-body-preview.png",
                   "front-body-difference.png", "back-body-difference.png"}
        if filename not in allowed:
            raise HTTPException(404, "File not found.")
        path = jobs / str(job_id) / "output" / filename
        if not path.is_file():
            raise HTTPException(404, "Requested format was not generated.")
        return FileResponse(path, filename=filename, headers={"Cache-Control": "no-store"})

    @router.get("/jobs/{job_id}/panels/{side}/source-preview")
    def panel_source_preview(job_id: uuid.UUID, side: Literal["front", "back"]):
        state = read_state(job_id)
        if state["status"] not in ("review_required", "awaiting_desktop", "approved"):
            raise HTTPException(409, "Extraction is not ready.")
        image = load_image(jobs / str(job_id) / "output" / side / "extracted.png")
        image.thumbnail((1200, 1200))
        data = io.BytesIO()
        image.save(data, "PNG")
        return StreamingResponse(iter([data.getvalue()]), media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.get("/jobs/{job_id}/bundle")
    def bundle(job_id: uuid.UUID):
        state = read_state(job_id)
        if state["status"] not in ("review_required", "awaiting_desktop", "approved"):
            raise HTTPException(409, "Outputs are not ready.")
        data = io.BytesIO()
        output = jobs / str(job_id) / "output"
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(output.iterdir()):
                if path.is_file() and path.suffix in (".svg", ".eps", ".ai", ".json", ".png"):
                    archive.write(path, arcname=path.name)
            desktop = output / "desktop"
            if desktop.is_dir():
                for path in sorted(desktop.iterdir()):
                    if path.is_file() and path.suffix in (".jsx", ".json", ".md", ".icc"):
                        archive.write(path, arcname=f"desktop/{path.name}")
        return StreamingResponse(iter([data.getvalue()]), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="artboard-ai-{job_id}.zip"',
            "Cache-Control": "no-store",
        })

    app.include_router(router)
    from .production_api import build_router
    app.include_router(build_router(profiles, jobs, access, read_state, update_state, lock))
    return app


def app_factory():
    workspace = Path(os.environ.get("ARTBOARD_WORKSPACE", "engine/.runtime")).resolve()
    profile = os.environ.get("ARTBOARD_CMYK_PROFILE")
    return create_app(
        workspace, token=os.environ.get("ARTBOARD_ENGINE_TOKEN"),
        cmyk_profile=Path(profile).resolve() if profile else None,
    )
