from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import logger
from app.db.database import engine
from app.api.router import api_router
from app.services.ai_client import close_ai_client

settings = get_settings()

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# Lifespan: startup + shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "SpecForge backend started: port=%s env=%s",
        settings.APP_PORT, settings.APP_ENV,
    )
    yield
    # Shutdown
    await close_ai_client()
    await engine.dispose()
    logger.info("SpecForge backend shut down")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SpecForge Backend",
    version="2.0.0",
    description="AI-Powered API Documentation Generator — FastAPI + SQLAlchemy + MySQL",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.CORS_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = datetime.now(timezone.utc)
    response = await call_next(request)
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    logger.debug(
        "%s %s → %s (%.3fs)",
        request.method, request.url.path, response.status_code, elapsed,
    )
    return response


# ---------------------------------------------------------------------------
# Exception handlers (replaces Express errorHandler middleware)
# ---------------------------------------------------------------------------
@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
            },
        },
    )


@app.exception_handler(Exception)
async def generic_error_handler(_request: Request, exc: Exception):
    logger.error("Unhandled error: %s", exc, exc_info=True)
    message = str(exc) if settings.is_development else "An unexpected error occurred"
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": message,
            },
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
app.include_router(api_router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
