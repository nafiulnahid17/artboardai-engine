import csv
import io
import shutil
import subprocess
from pathlib import Path

from .models import EngineError


def recognize_text(path: Path, language: str = "eng") -> dict:
    executable = shutil.which("tesseract")
    if not executable:
        raise EngineError("OCR_UNAVAILABLE", "Tesseract is not installed. Install it or explicitly disable OCR.")
    try:
        result = subprocess.run(
            [executable, str(path), "stdout", "-l", language, "--psm", "11", "tsv"],
            capture_output=True, text=True, timeout=90, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EngineError("OCR_TIMEOUT", "OCR exceeded the 90-second limit.") from exc
    if result.returncode != 0:
        raise EngineError("OCR_FAILED", "OCR failed. Check installed Tesseract language data and image quality.")
    words = []
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        confidence = max(0, min(100, float(row["conf"]))) / 100
        words.append({
            "text": text, "confidence": confidence,
            "box": {k: int(row[k]) for k in ("left", "top", "width", "height")},
            "line": [int(row[k]) for k in ("block_num", "par_num", "line_num")],
            "needs_review": confidence < 0.85,
        })
    return {
        "engine": "tesseract", "language": language, "words": words,
        "requires_operator_confirmation": True,
        "text_rebuilt": False, "original_font_recovered": False,
        "note": "OCR is advisory. Traced letters are paths, not editable text. Rebuild confirmed text with a verified font separately.",
    }
