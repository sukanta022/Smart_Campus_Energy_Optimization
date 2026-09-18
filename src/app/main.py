"""FastAPI application exposing /health and /optimize-energy."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import Settings, get_settings
from app.logging_setup import configure_logging, get_logger
from app.metrics import REGISTRY, requests_total
from app.models import HealthResponse, OptimizeRequest, OptimizeResponse
from app.orchestrator import Orchestrator
from app.replay import ReplayError
from optimizer.scheduler import OptimizationError


configure_logging()
log = get_logger("gridwise.main")


# ---- App factory ---------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("starting", extra={"app": settings.app_name})
        app.state.settings = settings
        app.state.orchestrator = Orchestrator(settings)
        app.state.start_time = time.perf_counter()
        yield
        log.info("shutting down", extra={"app": settings.app_name})

    app = FastAPI(
        title="GridWise LLM Energy Service",
        version="0.1.0",
        lifespan=lifespan,
    )

    _install_middlewares(app, settings)
    _install_exception_handlers(app)
    _install_routes(app, settings)

    return app


# ---- Middlewares ---------------------------------------------------------


def _install_middlewares(app: FastAPI, settings: Settings) -> None:
    from app.resilience import InMemoryTokenBucket, RedisTokenBucket

    bucket: InMemoryTokenBucket | RedisTokenBucket
    if settings.redis_url:
        try:
            from redis.asyncio import from_url

            redis = from_url(settings.redis_url, decode_responses=True)
            bucket = RedisTokenBucket(redis, settings.rate_limit_per_minute)
        except Exception:  # pragma: no cover - defensive
            bucket = InMemoryTokenBucket(settings.rate_limit_per_minute)
    else:
        bucket = InMemoryTokenBucket(settings.rate_limit_per_minute)

    app.state.rate_bucket = bucket

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        # Don't rate-limit /health and /metrics.
        if request.url.path in {"/health", "/metrics"}:
            return await call_next(request)
        ip = request.client.host if request.client else "unknown"
        if not await bucket.take(f"rl:{ip}"):
            requests_total.labels(outcome="429").inc()
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "rate limit exceeded"},
            )
        return await call_next(request)


# ---- Exception handlers --------------------------------------------------


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        requests_total.labels(outcome="400").inc()
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "malformed request", "errors": exc.errors()},
        )

    @app.exception_handler(OptimizationError)
    async def _infeasible(request: Request, exc: OptimizationError):
        requests_total.labels(outcome="500").inc()
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "optimizer infeasible", "reason": str(exc)},
        )

    @app.exception_handler(ReplayError)
    async def _replay_failed(request: Request, exc: ReplayError):
        requests_total.labels(outcome="500").inc()
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "replay check failed", "reason": str(exc)},
        )


# ---- Routes --------------------------------------------------------------


def _install_routes(app: FastAPI, settings: Settings) -> None:
    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        # PRD §06: GET /health -> 200 with {"status": "ok"} when ready.
        # Readiness = orchestrator constructed. We extend with a 2s liveness
        # window after startup so we don't accept traffic mid-init.
        start = getattr(request.app.state, "start_time", None)
        if start is not None and time.perf_counter() - start < 2.0:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "starting up"},
            )
        return HealthResponse(status="ok")

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.post(
        "/optimize-energy",
        response_model=OptimizeResponse,
        status_code=status.HTTP_200_OK,
    )
    async def optimize_energy(
        payload: OptimizeRequest,
        request: Request,
    ) -> OptimizeResponse:
        orchestrator: Orchestrator = request.app.state.orchestrator
        result = await orchestrator.run(payload)
        requests_total.labels(outcome="ok").inc()
        return result.response


# ---- Module-level app for uvicorn ----------------------------------------


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, log_level="info")
