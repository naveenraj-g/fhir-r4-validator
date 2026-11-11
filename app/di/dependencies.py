from fastapi import Request

from app.di.container import Container
from app.services.fhirpath_engine import FhirPathService
from app.services.ig_service import IgService
from app.services.profile_registry import ProfileRegistry
from app.services.validator_service import ValidatorService


def _container(request: Request) -> Container:
    return request.app.state.container


def get_validator_service(request: Request) -> ValidatorService:
    return _container(request).validator_service()


def get_profile_registry(request: Request) -> ProfileRegistry:
    return _container(request).profile_registry()


def get_ig_service(request: Request) -> IgService:
    return _container(request).ig_service()


def get_fhirpath_service(request: Request) -> FhirPathService:
    return _container(request).fhirpath_service()
