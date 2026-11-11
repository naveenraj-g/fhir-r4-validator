from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool

from app.di.dependencies import get_ig_service
from app.schemas.ig import IgResponse
from app.services.ig_service import IgService

router = APIRouter(tags=["implementation-guides"])


@router.get("/igs")
def get_known_igs(ig_service: IgService = Depends(get_ig_service)) -> dict[str, str]:
    """Map of known IG package ids to their canonical URLs."""
    return ig_service.get_known_igs()


@router.get("/profiles-by-ig")
def get_profiles_by_ig(ig_service: IgService = Depends(get_ig_service)) -> dict[str, list[str]]:
    """Map from IG package id to the profile URLs the IG provides."""
    return ig_service.get_profiles_by_ig()


@router.post("/igs", response_model=IgResponse)
async def load_custom_ig(request: Request, ig_service: IgService = Depends(get_ig_service)) -> IgResponse:
    """Loads the gzipped NPM package tarball (``package.tgz``) in the body into the validator."""
    body_bytes = await request.body()
    return await run_in_threadpool(ig_service.load_package, body_bytes, True)


@router.put("/igs/{package_id}", response_model=IgResponse)
def load_ig_by_id(
    package_id: str,
    version: str | None = Query(default=None),
    ig_service: IgService = Depends(get_ig_service),
) -> IgResponse:
    """Loads an IG from the NPM package registry by package id and optional version."""
    return ig_service.load_ig(package_id, version)
