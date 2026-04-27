"""
`python -m soft_binding_api` and `softbinding-api` console-script entry point.

Runs the FastAPI app with uvicorn. For dev work prefer:

    uvicorn soft_binding_api.main:app --reload
"""
import os

import uvicorn


def run() -> None:
    uvicorn.run(
        "soft_binding_api.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    run()
