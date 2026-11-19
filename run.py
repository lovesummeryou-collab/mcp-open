from __future__ import annotations

import os

import uvicorn

from app.config import DEBUG

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("MCPOPEN_HOST", "0.0.0.0"),
        port=int(os.getenv("MCPOPEN_PORT", "8765")),
        reload=DEBUG,
        log_level="debug" if DEBUG else "info",
    )
