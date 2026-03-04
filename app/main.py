from __future__ import annotations

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router as v1_router
from .logging_config import configure_logging
from .runtime_paths import ensure_runtime_dirs
from .store import store
from .worker import worker_engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    log_path = configure_logging()
    logging.getLogger("").info("IBX API startup complete; logs=%s", log_path)
    worker_engine.start_if_enabled()
    try:
        yield
    finally:
        worker_engine.stop()
        store.shutdown()


def create_app() -> FastAPI:
    ensure_runtime_dirs()

    app = FastAPI(
        title="IBX Strategy API",
        version="0.1.0",
        description="Trading strategy orchestration API skeleton for IBX.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(v1_router)

    return app


app = create_app()
