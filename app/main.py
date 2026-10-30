"""Application entry point.

Run it with::

    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
import logging.config
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import text

from app.api.errors import register_exception_handlers
from app.admin import router as admin_router
from app.admin import staff as staff_router
from app.admin.no_store import NoStoreMiddleware
from app.api.routers import attendance, compat_v1, diagnostics, persons, registration
from app.api.trusted_network import TrustedNetworkMiddleware
from app.core.clock import SystemClock
from app.core.config import Settings, get_settings
from app.core.errors import FaceError, FaceProcessingException
from app.db.base import Base
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.jobs.probe_retention import ProbeImageRetentionJob
from app.providers.factory import build_image_store, build_recognition_provider

logger = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    level = "DEBUG" if settings.is_development else "INFO"

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                }
            },
            "root": {"handlers": ["console"], "level": "INFO"},
            "loggers": {
                "app": {"level": level, "handlers": ["console"], "propagate": False},
                "sqlalchemy.engine": {"level": "WARNING"},
                "uvicorn.access": {"level": "WARNING" if not settings.is_development else "INFO"},
            },
        }
    )


async def _migrate(settings: Settings) -> None:
    """Create the schema at startup in development only.

    In production this belongs in the deployment pipeline: two instances starting at once
    would otherwise race each other through the same schema change.
    """
    if not settings.is_development and not settings.database.migrate_on_startup:
        return

    from app.db.session import get_engine

    try:
        async with get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

            # The enrollment service already retires the previous row, but a database-level
            # guarantee is what keeps a concurrent double upload from producing two "active"
            # fronts. Filtered-index syntax is dialect specific, so it is only applied where
            # it is known to work.
            dialect = connection.dialect.name

            if dialect == "mssql":
                await connection.execute(
                    text(
                        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = "
                        "'UX_FaceTemplates_Person_Pose_Active') "
                        "CREATE UNIQUE INDEX UX_FaceTemplates_Person_Pose_Active "
                        "ON FaceTemplates (PersonId, Pose) WHERE IsActive = 1"
                    )
                )
            elif dialect == "sqlite":
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "UX_FaceTemplates_Person_Pose_Active "
                        'ON "FaceTemplates" ("PersonId", "Pose") WHERE "IsActive" = 1'
                    )
                )

        logger.info("Database schema is up to date")
    except Exception:
        logger.critical(
            "Could not migrate the database. The API will start but will fail on every "
            "request.",
            exc_info=True,
        )


def _warm_up_recognition_provider(app: FastAPI, settings: Settings) -> None:
    """Build the recognition provider before serving traffic.

    The provider is a singleton, so without this it is constructed lazily on the first
    request - meaning a missing ONNX model file is discovered by the first employee who tries
    to punch in, not by the engineer who deployed it. Failing here is loud and immediate,
    which is what a configuration error deserves.
    """
    try:
        provider = build_recognition_provider(settings)
    except FaceProcessingException as exc:
        if exc.error == FaceError.ProviderNotConfigured:
            logger.critical("Face recognition cannot start: %s", exc.message)
        raise

    app.state.recognition_provider = provider

    logger.info(
        "Face recognition provider ready: %s (%s templates)",
        provider.provider_id,
        provider.template_kind.name,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_engine(settings)
        await _migrate(settings)

        app.state.clock = SystemClock()
        app.state.image_store = build_image_store(settings)
        _warm_up_recognition_provider(app, settings)

        # Create the Rekognition collection on a fresh account before any punch arrives.
        ensure_collection = getattr(app.state.recognition_provider, "ensure_collection", None)
        if callable(ensure_collection):
            try:
                await ensure_collection()
            except Exception:
                logger.warning("Could not ensure the provider collection", exc_info=True)

        retention = ProbeImageRetentionJob(settings, app.state.image_store, app.state.clock)
        retention.start()
        app.state.retention_job = retention

        try:
            yield
        finally:
            await retention.stop()
            app.state.recognition_provider.close()
            await dispose_engine()

    app = FastAPI(
        title="Face Attendance API",
        version="1.0.0",
        summary="Three-pose face registration and attendance verification.",
        description=(
            "Registration collects a front, left and right capture per person; attendance "
            "matches a live frame against them, either 1:1 when the device knows who is "
            "claiming to be there, or 1:N across the tenant."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
    )

    # Set before any request can arrive, so the settings dependency never has to fall back to
    # the process-wide cache.
    app.state.settings = settings

    register_exception_handlers(app)

    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    # No origins configured means no browser is expected to call this directly. Leaving CORS
    # open would let any website post to the attendance endpoint with a stolen key, so the
    # middleware is simply not installed.

    if not settings.is_development:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

    # Added last, so it runs first: an address outside the company network is turned away
    # before authentication, rate limiting or any route code sees the request.
    app.add_middleware(TrustedNetworkMiddleware, networks=settings.trusted_networks)

    app.include_router(persons.router)
    app.include_router(registration.router)
    app.include_router(attendance.router)
    app.include_router(diagnostics.router)

    # The contract the existing iOS kiosk app speaks. Translation only: every request lands
    # on the same services as the native API.
    app.include_router(compat_v1.router)

    # The pages people look at. Only mounted when a password is configured: a page listing
    # everyone's movements must not appear just because someone deployed with stock settings.
    if settings.dashboard_enabled:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret,
            session_cookie="fa_session",
            max_age=settings.session_hours * 3600,
            same_site="lax",
            # The service is HTTPS-only in every deployment, and a session cookie for a page
            # showing attendance has no business travelling in clear.
            https_only=not settings.is_development,
        )
        # A live view of who is in the building, so never a cached one.
        app.add_middleware(NoStoreMiddleware)

        app.include_router(admin_router.router)
        app.include_router(staff_router.router)
        logger.info("Dashboard enabled at /admin (HR) and /me (staff)")

    @app.get("/health", tags=["health"], summary="Liveness and database check")
    async def health() -> dict[str, str]:
        checks: dict[str, str] = {"status": "healthy"}

        try:
            factory = get_session_factory()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "healthy"
        except Exception as exc:
            checks["status"] = "unhealthy"
            checks["database"] = f"unhealthy: {type(exc).__name__}"

        provider = getattr(app.state, "recognition_provider", None)
        checks["provider"] = provider.provider_id if provider else "not initialised"

        return checks

    return app


app = create_app()
