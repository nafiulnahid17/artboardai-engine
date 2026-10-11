import shutil
import subprocess
from pathlib import Path

from .models import EngineError


def render_preview(eps: Path, target: Path) -> bool:
    """Render only engine-generated EPS; never accept arbitrary uploaded PS."""
    executable = shutil.which("gs")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dEPSCrop",
             "-sDEVICE=pngalpha", "-r36", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
             f"-sOutputFile={target}", str(eps)],
            capture_output=True, timeout=90, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineError("PREVIEW_TIMEOUT", "EPS preview generation exceeded the time limit.") from exc
    if result.returncode != 0:
        raise EngineError("EPS_RENDER_FAILED", "Generated EPS failed to render. Exports have not been approved.")
    return True
