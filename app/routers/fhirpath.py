from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.di.dependencies import get_fhirpath_service
from app.services.fhirpath_engine import FhirPathService

router = APIRouter(tags=["fhirpath"])


@router.post("/evaluate")
async def evaluate_fhirpath(
    request: Request,
    path: str = Query(description="FHIRPath expression"),
    type: str | None = Query(default=None, description="FHIR type of the root element when the body is not a resource"),
    evaluator: FhirPathService = Depends(get_fhirpath_service),
) -> Response:
    """Evaluates a FHIRPath expression against the provided FHIR resource or element."""
    body_bytes = await request.body()
    content = await run_in_threadpool(evaluator.evaluate, body_bytes, path, type)
    return Response(content=content, media_type="application/fhir+json", status_code=200)
