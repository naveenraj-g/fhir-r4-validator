from fastapi import Request
from fastapi.responses import JSONResponse

from app.fhir.fhirpath import FhirPathError
from app.fhir.parser import ParseError
from app.schemas.operation_outcome import OperationOutcome

FHIR_JSON = "application/fhir+json"


class FHIRValidationError(Exception):
    def __init__(self, message: str, issue_type: str = "structure", severity: str = "error"):
        super().__init__(message)
        self.message = message
        self.issue_type = issue_type
        self.severity = severity


class ResourceParsingError(FHIRValidationError):
    def __init__(self, message: str):
        super().__init__(message, issue_type="structure", severity="fatal")


def _outcome_response(message: str, issue_type: str, severity: str, status: int) -> JSONResponse:
    outcome = OperationOutcome.from_error(message=message, issue_type=issue_type, severity=severity)  # type: ignore[arg-type]
    return JSONResponse(status_code=status, content=outcome.model_dump(exclude_none=True), media_type=FHIR_JSON)


async def fhir_validation_exception_handler(request: Request, exc: FHIRValidationError) -> JSONResponse:
    return _outcome_response(exc.message, exc.issue_type, exc.severity, 400)


async def parse_error_handler(request: Request, exc: ParseError) -> JSONResponse:
    return _outcome_response(str(exc), "structure", "fatal", 400)


async def fhirpath_error_handler(request: Request, exc: FhirPathError) -> JSONResponse:
    return _outcome_response(str(exc), "invalid", "error", 400)


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return _outcome_response(str(exc) or "An internal error occurred", "exception", "fatal", 500)
