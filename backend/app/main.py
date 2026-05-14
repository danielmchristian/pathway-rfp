from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.db import engine
from app.logging import RequestContextMiddleware, configure_logging
from app.routers import health, ingredients, restaurants, usage


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
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(restaurants.router)
    app.include_router(ingredients.router)
    app.include_router(usage.router)
    return app


app = create_app()
