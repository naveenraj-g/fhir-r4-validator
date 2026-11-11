from dependency_injector import containers, providers

from app.core.config import settings
from app.fhir.fhirpath import FhirPathRunner
from app.fhir.terminology import TerminologyService
from app.services.fhirpath_engine import FhirPathService
from app.services.ig_service import IgService
from app.services.profile_registry import ProfileRegistry
from app.services.validator_service import ValidatorService


class Container(containers.DeclarativeContainer):
    profile_registry = providers.Singleton(ProfileRegistry, data_dir=settings.data_dir)

    terminology = providers.Singleton(
        TerminologyService,
        registry=profile_registry,
        tx_url=settings.tx_server_url,
        disabled=settings.tx_disabled,
        timeout=settings.tx_timeout_seconds,
    )

    fhirpath_runner = providers.Singleton(FhirPathRunner, terminology=terminology)

    # IGs are loaded eagerly at startup (see main.lifespan); they populate profile_registry.
    ig_service = providers.Singleton(
        IgService,
        registry=profile_registry,
        igs_dir=settings.igs_dir,
    )

    fhirpath_service = providers.Singleton(
        FhirPathService,
        registry=profile_registry,
        runner=fhirpath_runner,
    )

    validator_service = providers.Singleton(
        ValidatorService,
        registry=profile_registry,
        terminology=terminology,
        fhirpath=fhirpath_runner,
        display_issues_are_warnings=settings.display_issues_are_warnings,
    )
