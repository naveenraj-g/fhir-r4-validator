import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.errors import (
    FHIR_JSON,
    FHIRValidationError,
    fhir_validation_exception_handler,
    fhirpath_error_handler,
    generic_exception_handler,
    parse_error_handler,
)
from app.core.logging import get_logger, setup_logging
from app.di.container import Container
from app.fhir.fhirpath import FhirPathError
from app.fhir.parser import ParseError
from app.routers import api_router
from app.schemas.operation_outcome import OperationOutcome
from app.services.ig_service import IgError, IgNotFound
from app.services.profile_registry import ProfileNotFound, ProtectedDefinition

logger = get_logger(__name__)


def _load(app: FastAPI) -> None:
    """Load definitions and IGs. Runs in a background thread so /version answers during startup."""
    try:
        container: Container = app.state.container
        container.profile_registry()
        container.terminology()
        container.ig_service()  # loads ./igs/*.tgz into the registry
        container.fhirpath_service()
        container.validator_service()
        app.state.ready = True
        logger.info("FHIR R4 Validator Service started successfully.")
    except Exception:  # pragma: no cover - fatal startup problem
        logger.exception("There was an error initializing the validator")
        app.state.load_error = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("Starting FHIR R4 Validator Service...")
    app.state.ready = False
    app.state.load_error = False
    app.state.container = Container()
    thread = threading.Thread(target=_load, args=(app,), name="validator-loader", daemon=True)
    thread.start()
    yield
    logger.info("Shutting down FHIR R4 Validator Service...")


def create_app() -> FastAPI:
    app = FastAPI(
        title="FHIR R4 Validation Service",
        description="Pure-Python FastAPI port of inferno-framework/fhir-validator-wrapper (FHIR R4)",
        version=settings.wrapper_version,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def loading_gate(request: Request, call_next):
        # Same behaviour as the Java wrapper: only /version works until the validator is loaded.
        state = request.app.state
        if not getattr(state, "ready", False) and request.url.path != "/version" and request.method != "OPTIONS":
            outcome = OperationOutcome.loading()
            return JSONResponse(
                status_code=503,
                content=outcome.model_dump(exclude_none=True),
                media_type=FHIR_JSON,
            )
        return await call_next(request)

    # Exception handlers emitting FHIR OperationOutcome
    app.add_exception_handler(FHIRValidationError, fhir_validation_exception_handler)
    app.add_exception_handler(ParseError, parse_error_handler)
    app.add_exception_handler(FhirPathError, fhirpath_error_handler)
    app.add_exception_handler(IgNotFound, _ig_not_found_handler)
    app.add_exception_handler(IgError, _ig_error_handler)
    app.add_exception_handler(ProfileNotFound, _profile_not_found_handler)
    app.add_exception_handler(ProtectedDefinition, _protected_handler)
    app.add_exception_handler(Exception, generic_exception_handler)

    # Permissive CORS headers matching the Spark Java wrapper (added last => outermost, so 503s carry them too)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    return app


async def _ig_not_found_handler(request: Request, exc: IgNotFound) -> JSONResponse:
    outcome = OperationOutcome.from_error(str(exc), "not-found", "error")
    return JSONResponse(status_code=404, content=outcome.model_dump(exclude_none=True), media_type=FHIR_JSON)


async def _profile_not_found_handler(request: Request, exc: ProfileNotFound) -> JSONResponse:
    outcome = OperationOutcome.from_error(str(exc), "not-found", "error")
    return JSONResponse(status_code=404, content=outcome.model_dump(exclude_none=True), media_type=FHIR_JSON)


async def _protected_handler(request: Request, exc: ProtectedDefinition) -> JSONResponse:
    outcome = OperationOutcome.from_error(str(exc), "forbidden", "error")
    return JSONResponse(status_code=403, content=outcome.model_dump(exclude_none=True), media_type=FHIR_JSON)


async def _ig_error_handler(request: Request, exc: IgError) -> JSONResponse:
    outcome = OperationOutcome.from_error(str(exc), "invalid", "error")
    return JSONResponse(status_code=400, content=outcome.model_dump(exclude_none=True), media_type=FHIR_JSON)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.validator_port, reload=False)
