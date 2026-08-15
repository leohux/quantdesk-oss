#!/usr/bin/env python3
"""Entrypoint: start uvicorn programmatically (avoids CLI wsproto bug)."""
import uvicorn
from api.main import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        ws="wsproto",
        log_level="info",
    )
