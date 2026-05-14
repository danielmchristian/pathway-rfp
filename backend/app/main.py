from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.db import engine
from app.logging import RequestContextMiddleware, configure_logging
from app.routers import distributors, health, ingredients, restaurants, rfps, usage


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Pathway RFP",
        version=__version__,
        lifespan=lifespan,
    )
    # Browser → backend is cross-origin (localhost:3000 → localhost:8000).
    # Without CORS the buttons fail with "Failed to fetch" while curl/CLI
    # works fine. SSE EventSource is subject to the same policy.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(restaurants.router)
    app.include_router(ingredients.router)
    app.include_router(distributors.router)
    app.include_router(usage.router)
    app.include_router(rfps.send_router)
    app.include_router(rfps.view_router)
    app.include_router(rfps.list_router)
    return app


app = create_app()
