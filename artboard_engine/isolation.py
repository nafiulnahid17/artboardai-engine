import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from .models import EngineError, JobRequest
from .pipeline import atomic_json
from .exporters import ensure_formats

logger = logging.getLogger("artboard_engine")


def process_isolated(
    request: JobRequest, output: Path, cmyk_profile: Path | None = None,
    progress: Callable[[str, int], None] | None = None, timeout_seconds: int = 300,
) -> dict:
    """Kill the full process group on timeout; native tracing cannot stall the API."""
    ensure_formats(request.formats, request.native_ai_mode)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise EngineError("OUTPUT_NOT_EMPTY", "Use a new empty output directory. Existing artwork will not be changed.")
    with tempfile.TemporaryDirectory(prefix="artboard-worker-") as work:
        temp = Path(work)
        config = temp / "request.json"
        progress_file = temp / "progress.json"
        error_file = temp / "error.json"
        config.write_text(json.dumps({
            "request": request.model_dump(mode="json"), "output": str(output.resolve()),
            "cmyk_profile": str(cmyk_profile.resolve()) if cmyk_profile else None,
            "progress_file": str(progress_file), "error_file": str(error_file), "cpu_seconds": timeout_seconds,
        }))
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
        # Bound numerical-library thread use within the single tracing worker.
        env["OPENBLAS_NUM_THREADS"] = "1"
        env["OMP_NUM_THREADS"] = "1"
        with (temp / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "artboard_engine.worker", str(config)],
                stdout=log, stderr=log, env=env, start_new_session=True,
            )
            deadline = time.monotonic() + timeout_seconds
            previous = None
            try:
                while process.poll() is None:
                    if time.monotonic() > deadline:
                        raise EngineError("JOB_TIMEOUT", f"Artwork processing exceeded {timeout_seconds} seconds. No outputs were approved.")
                    if progress and progress_file.exists():
                        payload = progress_file.read_text()
                        if payload != previous:
                            report = json.loads(payload)
                            progress(report["stage"], report["progress"])
                            previous = payload
                    time.sleep(0.1)
            except BaseException as exc:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                manifest_path = output / "manifest.json"
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text())
                    manifest.update(status="failed", production_ready=False, error={
                        "code": getattr(exc, "code", "WORKER_INTERRUPTED"),
                        "message": str(exc) if isinstance(exc, EngineError) else "Worker interrupted.",
                    })
                    atomic_json(manifest_path, manifest)
                raise
        manifest_path = output / "manifest.json"
        if process.returncode != 0:
            if error_file.is_file():
                error = json.loads(error_file.read_text())
                raise EngineError(error["code"], error["message"])
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                error = manifest.get("error")
                if error:
                    raise EngineError(error["code"], error["message"])
            logger.error("Artwork worker failed with exit code %s", process.returncode)
            if manifest_path.exists():
                manifest.update(status="failed", production_ready=False, error={
                    "code": "WORKER_FAILED", "message": "Isolated worker exited unexpectedly.",
                })
                atomic_json(manifest_path, manifest)
            raise EngineError("WORKER_FAILED", "The isolated worker exited unexpectedly. Reduce input complexity and retry; partial outputs are not approved.")
        if not manifest_path.is_file():
            raise EngineError("WORKER_FAILED", "Worker did not produce a result manifest.")
        result = json.loads(manifest_path.read_text())
        if result["status"] not in ("review_required", "awaiting_desktop"):
            raise EngineError("WORKER_FAILED", "Worker did not complete the required processing stages.")
        if progress:
            progress(result["status"], 100)
        return result
