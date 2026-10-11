import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .assistant import analyze_with_ai
from .images import analyze_image, load_image
from .models import CANVAS, EngineError, JobRequest
from .isolation import process_isolated


def main():
    parser = argparse.ArgumentParser(prog="artboard-engine")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("canvas", help="Show the locked canvas and raster equivalents.")
    analyze = sub.add_parser("analyze")
    analyze.add_argument("source", type=Path)
    analyze.add_argument("--ai", action="store_true", help="Opt in to configured AI analysis.")
    process = sub.add_parser("process")
    process.add_argument("--request", type=Path, required=True)
    process.add_argument("--output", type=Path, required=True)
    process.add_argument("--cmyk-profile", type=Path)
    native = sub.add_parser("native-import", help="Import real Illustrator-saved AI and its desktop receipt.")
    native.add_argument("--output", type=Path, required=True)
    native.add_argument("--side", choices=["front", "back"], required=True)
    native.add_argument("--ai", type=Path, required=True)
    native.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "canvas":
            result = {**CANVAS.model_dump(), "raster_sizes": {str(d): CANVAS.raster_size(d) for d in [330, 600]}}
        elif args.command == "analyze":
            image = load_image(args.source)
            result = analyze_image(image)
            if args.ai:
                result["ai"] = analyze_with_ai(image)
        elif args.command == "native-import":
            from .desktop import NativeReceipt, import_native
            result = import_native(args.output, args.side, args.ai, NativeReceipt.model_validate_json(args.receipt.read_text()))
        else:
            request = JobRequest.model_validate_json(args.request.read_text())
            # Relative source paths are resolved against the request document.
            for panel in request.panels:
                if not panel.source.is_absolute():
                    panel.source = (args.request.parent / panel.source).resolve()
                for layer in panel.text_layers:
                    if not layer.font.is_absolute():
                        layer.font = (args.request.parent / layer.font).resolve()
                for patch in panel.reference_patches:
                    if not patch.source.is_absolute():
                        patch.source = (args.request.parent / patch.source).resolve()
            result = process_isolated(request, args.output, args.cmyk_profile)
        print(json.dumps(result, indent=2))
    except (EngineError, ValidationError, OSError) as exc:
        print(json.dumps({
            "error": getattr(exc, "code", "INVALID_REQUEST"),
            "message": str(exc),
        }), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
