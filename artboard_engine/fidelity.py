from pathlib import Path

import cv2
import numpy as np
from PIL import Image

def compare_preview(source: Image.Image, preview_path: Path, heatmap_path: Path) -> dict:
    """Reference-render comparison, not a print proof or semantic correctness test."""
    with Image.open(preview_path) as preview:
        preview = preview.convert("RGBA")
        w, h = preview.size
        paper = Image.new("RGBA", (w, h), "white")
        paper.alpha_composite(preview)
        actual = np.asarray(paper.convert("RGB"))
    ratio = min(w / source.width, h / source.height)
    resized = source.resize((round(source.width * ratio), round(source.height * ratio)), Image.Resampling.LANCZOS)
    reference = Image.new("RGBA", (w, h), "white")
    reference.alpha_composite(resized, ((w - resized.width) // 2, (h - resized.height) // 2))
    expected = np.asarray(reference.convert("RGB"))
    a = cv2.cvtColor(actual, cv2.COLOR_RGB2GRAY).astype(np.float64)
    b = cv2.cvtColor(expected, cv2.COLOR_RGB2GRAY).astype(np.float64)
    mu_a = cv2.GaussianBlur(a, (11, 11), 1.5)
    mu_b = cv2.GaussianBlur(b, (11, 11), 1.5)
    var_a = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu_a * mu_a
    var_b = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu_b * mu_b
    cov = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu_a * mu_b
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ssim = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / (
        (mu_a * mu_a + mu_b * mu_b + c1) * (var_a + var_b + c2)
    )
    difference = np.abs(actual.astype(float) - expected.astype(float)).mean(axis=2)
    heat = np.zeros((h, w, 3), dtype=np.uint8)
    heat[:, :, 0] = np.minimum(difference * 5, 255).astype(np.uint8)
    heat[:, :, 1] = np.maximum(255 - difference * 5, 0).astype(np.uint8)
    Image.fromarray(heat).save(heatmap_path)
    score = float(np.clip(ssim.mean(), -1, 1))
    return {
        "grayscale_ssim": round(score, 5),
        "mean_rgb_difference_0_255": round(float(difference.mean()), 3),
        "automatic_review_flag": score < 0.95,
        "comparison_resolution_px": [w, h],
        "heatmap": heatmap_path.name,
        "requires_operator_confirmation": True,
        "note": "Includes tracing differences and RGB-to-CMYK preview changes. Not a calibrated print-color proof; high similarity cannot validate text or hidden artwork.",
    }
