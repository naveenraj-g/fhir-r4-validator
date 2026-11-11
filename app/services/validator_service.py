from typing import Any

from app.core.logging import get_logger
from app.fhir.fhirpath import FhirPathRunner
from app.fhir.model import Navigator
from app.fhir.parser import ParseError, parse_resource
from app.fhir.terminology import TerminologyService
from app.fhir.validator import InstanceValidator, Issue
from app.schemas.operation_outcome import OperationOutcome
from app.services.profile_registry import ProfileRegistry

logger = get_logger(__name__)


class ValidatorService:
    """FHIR R4 validation: structure, cardinality, types, profiles, slicing, invariants, terminology."""

    def __init__(
        self,
        registry: ProfileRegistry,
        terminology: TerminologyService,
        fhirpath: FhirPathRunner,
        display_issues_are_warnings: bool = False,
    ) -> None:
        self.registry = registry
        self.nav = Navigator(registry)
        self.validator = InstanceValidator(registry, terminology, fhirpath, display_issues_are_warnings)

    def validate(self, resource_bytes: bytes | str, profile_urls: list[str] | None = None) -> OperationOutcome:
        """Validates a raw FHIR resource (JSON or XML) against the base spec and the requested profiles."""
        profiles = [p for p in (profile_urls or []) if p]
        try:
            parsed = parse_resource(resource_bytes, self.nav)
        except ParseError as e:
            return OperationOutcome.from_error(str(e), issue_type="structure", severity="fatal")
        except Exception as e:  # defensive: never let a parser bug escape as a 500
            logger.exception("Unexpected parse failure")
            return OperationOutcome.from_error(str(e) or "Unable to parse the resource", "structure", "fatal")

        resource = parsed.data
        meta = resource.get("meta") if isinstance(resource.get("meta"), dict) else {}
        logger.info(
            "Validating resource with type: %s, selected profile: %s and meta.profile: %s",
            resource.get("resourceType"), profiles, (meta or {}).get("profile", []),
        )

        issues: list[Issue] = []
        for message, key, severity in parsed.issues:
            line, col = parsed.positions.get(key)
            issues.append(Issue(severity, "structure", message, str(resource.get("resourceType", "")), key, line=line, col=col))
        try:
            issues.extend(self.validator.validate(resource, profiles, parsed.positions))
        except Exception as e:
            # mirror the Java wrapper: engine failures become a FATAL outcome
            logger.exception("Validation engine failure")
            return OperationOutcome.from_error(str(e) or type(e).__name__, "structure", "fatal")
        return OperationOutcome.from_issues(issues)
