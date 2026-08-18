from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
# Dev-only: lets the web app (vite dev server on 8080) call this API (8000)
# cross-origin. Not relevant in production, where they're served same-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api")
