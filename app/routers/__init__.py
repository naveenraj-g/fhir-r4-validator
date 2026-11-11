from fastapi import APIRouter
from app.routers.fhirpath import router as fhirpath_router
from app.routers.igs import router as igs_router
from app.routers.profiles import router as profiles_router
from app.routers.validate import router as validate_router
from app.routers.version import router as version_router

api_router = APIRouter()

api_router.include_router(version_router)
api_router.include_router(validate_router)
api_router.include_router(profiles_router)
api_router.include_router(igs_router)
api_router.include_router(fhirpath_router)
