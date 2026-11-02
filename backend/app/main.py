"""FastAPI application factory."""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import ServiceUnavailable, register_error_handlers
from app.core.logging import configure_logging, log_extra, request_id_var
from app.db.session import new_session
from app.modules.analytics.router import router as analytics_router
from app.modules.api_keys.router import router as api_keys_router
from app.modules.auth.router import router as auth_router
from app.modules.cases.router import router as cases_router
from app.modules.ingestion.router import router as ingestion_router
from app.modules.labels.router import router as labels_router
from app.modules.models.router import router as models_router
from app.modules.monitoring.router import router as monitoring_router
from app.modules.reference.router import router as reference_router
from app.modules.settings.router import router as settings_router
from app.modules.system.router import router as system_router
from app.modules.transactions.router import router as transactions_router
from app.modules.users.router import router as users_router

logger = logging.getLogger("app.http")

API_DESCRIPTION = """
Score financial transactions for fraud risk, explain each score, and route the
riskiest activity to human analysts.

**Advisory only:** `recommended_action` is `allow` or `review`. The engine never
declines payments or blocks accounts; authorised analysts make every decision.

**Auth:** staff sign in with `POST /api/v1/auth/login` (bearer token, 15 min, plus an
httpOnly refresh cookie). Roles: `admin`, `lead_analyst`, `analyst`, `auditor`.
Machine clients send `X-API-Key` with scopes `transactions:write`, `reference:write`
or `labels:write`.

**Errors** use RFC 9457 problem details (`application/problem+json`). All data in
this system is synthetic.
"""


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    app = FastAPI(
        title="Financial Fraud Detection Engine API",
        version="1.0.0",
        description=API_DESCRIPTION,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-API-Key"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("X-Request-ID", "")[:64] or uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if s.env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if not request.url.path.startswith("/api/health"):
            logger.info(
                "request",
                extra=log_extra(
                    method=request.method,
                    path=request.url.path,
                    status=response.status_code,
                    ms=round((time.perf_counter() - started) * 1000, 1),
                    request_id=rid,
                ),
            )
        return response

    register_error_handlers(app)

    api = APIRouter(prefix="/api/v1")
    for r in (
        auth_router,
        users_router,
        api_keys_router,
        reference_router,
        ingestion_router,
        transactions_router,
        cases_router,
        labels_router,
        models_router,
        monitoring_router,
        settings_router,
        analytics_router,
        system_router,
    ):
        api.include_router(r)
    app.include_router(api)

    @app.get("/api/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/health/ready", tags=["health"])
    def ready() -> dict[str, object]:
        db = new_session()
        try:
            db.execute(text("SELECT 1"))
            from app.ml.registry import has_active

            model_ready = has_active(db)
            streaming = "enabled" if s.streaming_enabled else "disabled"
        except Exception as exc:
            raise ServiceUnavailable("Database unavailable") from exc
        finally:
            db.close()
        return {"status": "ok", "database": "ok", "active_model": model_ready, "streaming": streaming}

    return app


app = create_app()
