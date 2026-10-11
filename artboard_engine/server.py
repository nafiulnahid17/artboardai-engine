import os

import uvicorn


def main():
    host = os.environ.get("ARTBOARD_HOST", "127.0.0.1")
    if host != "127.0.0.1" and not os.environ.get("ARTBOARD_ENGINE_TOKEN"):
        raise SystemExit("Network binding requires ARTBOARD_ENGINE_TOKEN. Keep localhost binding until access controls are configured.")
    uvicorn.run(
        "artboard_engine.api:app_factory", factory=True,
        host=host, port=int(os.environ.get("PORT", "8000")),
        workers=1, log_level="info",
    )


if __name__ == "__main__":
    main()
