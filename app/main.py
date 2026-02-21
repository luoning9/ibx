from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router as v1_router
from .logging_config import configure_logging
from .runtime_paths import ensure_runtime_dirs


def create_app() -> FastAPI:
    ensure_runtime_dirs()
    log_path = configure_logging()

    app = FastAPI(
        title="IBX Strategy API",
        version="0.1.0",
        description="Trading strategy orchestration API skeleton for IBX.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(v1_router)

    @app.on_event("startup")
    def on_startup() -> None:
        logging.getLogger("").info("IBX API startup complete; logs=%s", log_path)

    return app


app = create_app()
