from contextlib import asynccontextmanager

from fastapi import FastAPI

from doctask.api import router
from doctask.runtime import get_services, shutdown_services


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_services()  # fail fast on a bad database URL or missing migrations
    yield
    await shutdown_services()


app = FastAPI(
    title="Vendor Obligations Register",
    version="0.1.0",
    description="Agentic document analyst starter with human-gated commits.",
    lifespan=lifespan,
)
app.include_router(router, prefix="/api")
