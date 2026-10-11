"""Private profile ingestion and external-desktop result endpoints."""
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .desktop import NativeReceipt, import_native
from .exporters import ColorConverter


def build_router(profiles: Path, jobs: Path, access, read_state, update_state, lock) -> APIRouter:
    router = APIRouter(prefix="/api/engine", dependencies=[Depends(access)])

    async def save_upload(file: UploadFile, target: Path, limit: int):
        total = 0
        with target.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, f"File exceeds the {limit // (1024 * 1024)} MiB limit.")
                destination.write(chunk)

    @router.post("/profiles", status_code=201)
    async def upload_profile(file: UploadFile = File(...)):
        profile_id = uuid.uuid4()
        target = profiles / str(profile_id)
        try:
            await save_upload(file, target, 4 * 1024 * 1024)
            converted = await run_in_threadpool(ColorConverter, target)
            return {
                "profile_id": str(profile_id), **converted.profile_info,
                "printer_authorization": "Operator must confirm this is the printer's actual approved profile.",
            }
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    def accept_native(job_id, side, source, receipt):
        with lock:
            read_state(job_id)
            output = jobs / str(job_id) / "output"
            if not (output / "desktop/request.json").is_file():
                raise HTTPException(409, "This job has no native AI desktop handoff.")
            result = import_native(output, side, source, receipt)
            update_state(job_id, status=result["status"], stage=result["status"], result=result)
            return read_state(job_id)

    @router.post("/jobs/{job_id}/native/{side}")
    async def native(
        job_id: uuid.UUID, side: Literal["front", "back"],
        file: UploadFile = File(...), receipt_json: str = Form(...),
    ):
        read_state(job_id)
        if len(receipt_json) > 16000:
            raise HTTPException(413, "Receipt exceeds the JSON limit.")
        try:
            receipt = NativeReceipt.model_validate_json(receipt_json)
        except ValidationError as exc:
            raise HTTPException(422, "Native receipt failed schema validation.") from exc
        directory = jobs / str(job_id)
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".incoming-ai", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            await save_upload(file, temporary, 60 * 1024 * 1024)
            return await run_in_threadpool(accept_native, job_id, side, temporary, receipt)
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()

    return router
