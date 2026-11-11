from fastapi import APIRouter, Depends, Request, Response

from app.di.dependencies import get_profile_registry
from app.fhir.model import Navigator
from app.fhir.parser import parse_resource
from app.services.profile_registry import ProfileRegistry

router = APIRouter(tags=["profiles"])


@router.get("/resources")
def get_resources(registry: ProfileRegistry = Depends(get_profile_registry)) -> list[str]:
    """Lists the names of resources defined for this version of the validator."""
    return registry.get_resources()


@router.get("/profiles")
def get_profiles(registry: ProfileRegistry = Depends(get_profile_registry)) -> list[str]:
    """Lists the StructureDefinitions loaded in the validator."""
    return registry.get_structures()


@router.post("/profiles", status_code=200)
async def load_profile(request: Request, registry: ProfileRegistry = Depends(get_profile_registry)) -> Response:
    """Loads a custom profile (StructureDefinition, or ValueSet / CodeSystem) into the validator."""
    body_bytes = await request.body()
    parsed = parse_resource(body_bytes, Navigator(registry))
    registry.register(parsed.data)
    return Response(content="", status_code=200)
