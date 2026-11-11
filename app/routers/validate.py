from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.di.dependencies import get_validator_service
from app.schemas.operation_outcome import OperationOutcome
from app.services.validator_service import ValidatorService

router = APIRouter(tags=["validation"])


@router.post("/validate", response_model=OperationOutcome)
async def validate_resource(
    request: Request,
    profile: str | None = Query(default=None, description="Comma-separated list of profile URLs"),
    validator: ValidatorService = Depends(get_validator_service),
) -> Response:
    """Validates a FHIR resource (JSON or XML) against the base specification and/or the given profiles."""
    body_bytes = await request.body()
    # Same as Java: split(",") on the raw parameter
    profile_list = profile.split(",") if profile else []

    outcome = await run_in_threadpool(validator.validate, body_bytes, profile_list)
    return Response(
        content=outcome.model_dump_json(exclude_none=True),
        media_type="application/fhir+json",
        status_code=200,
    )
