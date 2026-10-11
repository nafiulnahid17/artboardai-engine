import json
import resource
import sys
from pathlib import Path

from .models import EngineError, JobRequest
from .pipeline import atomic_json, process_job


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    # Limits apply only to the disposable worker and its child processes.
    resource.setrlimit(resource.RLIMIT_CPU, (config["cpu_seconds"], config["cpu_seconds"] + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (150 * 1024 * 1024, 150 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    request = JobRequest.model_validate(config["request"])
    try:
        process_job(
            request, Path(config["output"]),
            Path(config["cmyk_profile"]) if config["cmyk_profile"] else None,
            lambda stage, progress: atomic_json(Path(config["progress_file"]), {"stage": stage, "progress": progress}),
        )
    except EngineError as exc:
        atomic_json(Path(config["error_file"]), {"code": exc.code, "message": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
