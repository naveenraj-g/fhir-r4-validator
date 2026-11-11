from fastapi import APIRouter
from app.core.config import settings

router = APIRouter(tags=["version"])


@router.get("/version")
def get_version() -> dict[str, str]:
    """Returns library versions used by this validator service."""
    return {
        "org.hl7.fhir.validation": settings.hl7_validator_version,
        "inferno-framework/fhir-validator-wrapper": settings.wrapper_version,
    }
