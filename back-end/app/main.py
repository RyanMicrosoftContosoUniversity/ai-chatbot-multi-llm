import logging
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import Services
from app.api.routes import chat, conversations, health, models
from app.core.auth import TokenValidator
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.middleware import RequestBoundary
from app.services.apim_client import GatewayClient
from app.services.chat_service import ChatService
from app.services.concurrency import Capacity

logger = logging.getLogger("multillm_bff")


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    configuration = settings if settings is not None else Settings()
    logger.setLevel(logging.INFO)
    if not any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(console)
    capacity = Capacity(configuration.max_active_generations)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ready = False
        async with AsyncExitStack() as stack:
            if services is not None:
                app.state.services = services
            else:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            configuration.read_timeout_seconds,
                            connect=configuration.connect_timeout_seconds,
                        ),
                        follow_redirects=False,
                        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                    )
                )
                credential = None
                if configuration.apim_subscription_key is None or configuration.cosmos_endpoint:
                    credential = await stack.enter_async_context(
                        DefaultAzureCredential(
                            managed_identity_client_id=configuration.managed_identity_client_id,
                            exclude_interactive_browser_credential=True,
                        )
                    )
                if configuration.apim_subscription_key is not None:
                    key = configuration.apim_subscription_key.get_secret_value()
                else:
                    secret_client = await stack.enter_async_context(
                        SecretClient(vault_url=configuration.key_vault_url, credential=credential)
                    )
                    secret = await secret_client.get_secret(configuration.apim_secret_name)
                    if not secret.value:
                        raise RuntimeError("The APIM secret has no value.")
                    key = secret.value
                history = None
                if configuration.cosmos_endpoint:
                    from app.repositories.chat_repository import HistoryStore

                    cosmos = await stack.enter_async_context(
                        CosmosClient(configuration.cosmos_endpoint, credential=credential)
                    )
                    database = cosmos.get_database_client(configuration.cosmos_database)
                    await database.get_container_client(
                        configuration.cosmos_conversations_container
                    ).read()
                    await database.get_container_client(
                        configuration.cosmos_messages_container
                    ).read()
                    history = HistoryStore(database, configuration)
                app.state.services = Services(
                    TokenValidator(configuration, client),
                    GatewayClient(configuration, client, key),
                    history,
                )
            app.state.chat_service = ChatService(
                configuration,
                app.state.services.gateway,
                capacity,
                app.state.services.history,
            )
            app.state.ready = True
            try:
                yield
            finally:
                app.state.ready = False

    app = FastAPI(
        title="Multi-LLM BFF",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ready = False
    app.add_middleware(RequestBoundary, max_body_bytes=configuration.max_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=configuration.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        headers = {}
        if exc.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(
            {"error": exc.payload(request.state.request_id)},
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Pydantic error details contain submitted input; do not echo prompts or credentials.
        return JSONResponse(
            {
                "error": ApiError(
                    422,
                    "invalid_request",
                    "The request contains missing, invalid, or unsupported fields.",
                ).payload(request.state.request_id)
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error(
            "request_failed request_id=%s error_type=%s",
            getattr(request.state, "request_id", "unknown"),
            type(exc).__name__,
        )
        headers = {"X-Request-ID": getattr(request.state, "request_id", "unknown")}
        origin = request.headers.get("origin")
        if origin in configuration.cors_origins:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Expose-Headers"] = "X-Request-ID"
            headers["Vary"] = "Origin"
        return JSONResponse(
            {
                "error": ApiError(
                    500, "internal_error", "The request could not be completed."
                ).payload(getattr(request.state, "request_id", "unknown"))
            },
            status_code=500,
            headers=headers,
        )

    app.include_router(health.router)
    app.include_router(models.router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1")
    app.include_router(conversations.router, prefix="/api/v1")

    if configuration.applicationinsights_connection_string:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        configure_azure_monitor(
            connection_string=configuration.applicationinsights_connection_string.get_secret_value(),
            logger_name="multillm_bff",
            instrumentation_options={"fastapi": {"enabled": False}},
            enable_live_metrics=False,
        )
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health/live,health/ready")
    return app
