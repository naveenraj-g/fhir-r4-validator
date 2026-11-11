from typing import Literal

from pydantic import BaseModel, Field

SOURCE_VALIDATION_SERVICE = "ValidationService"
EXT_LINE = "http://hl7.org/fhir/StructureDefinition/operationoutcome-issue-line"
EXT_COL = "http://hl7.org/fhir/StructureDefinition/operationoutcome-issue-col"
EXT_SOURCE = "http://hl7.org/fhir/StructureDefinition/operationoutcome-issue-source"

Severity = Literal["fatal", "error", "warning", "information"]


class Coding(BaseModel):
    system: str | None = None
    version: str | None = None
    code: str | None = None
    display: str | None = None
    userSelected: bool | None = None


class CodeableConcept(BaseModel):
    coding: list[Coding] | None = None
    text: str | None = None


class Extension(BaseModel):
    url: str
    valueInteger: int | None = None
    valueString: str | None = None
    valueCode: str | None = None
    valueBoolean: bool | None = None


class OperationOutcomeIssue(BaseModel):
    severity: Severity
    code: str
    details: CodeableConcept | None = None
    diagnostics: str | None = None
    location: list[str] | None = None
    expression: list[str] | None = None
    extension: list[Extension] | None = None


EXT_MESSAGE_ID = "http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id"


def _location_extensions(line: int, col: int, source: str, message_id: str | None = None) -> list[Extension]:
    exts = [
        Extension(url=EXT_LINE, valueInteger=line),
        Extension(url=EXT_COL, valueInteger=col),
        Extension(url=EXT_SOURCE, valueString=source),
    ]
    if message_id:
        exts.append(Extension(url=EXT_MESSAGE_ID, valueCode=message_id))
    return exts


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Narrative(BaseModel):
    status: str = "generated"
    div: str


def _narrative(issues: list["OperationOutcomeIssue"]) -> Narrative:
    rows = "".join(
        f"<tr><td>{i.severity}</td><td>{_esc(', '.join(i.expression or []))}</td><td>{i.code}</td>"
        f"<td>{_esc((i.details.text if i.details and i.details.text else ''))}</td>"
        f"<td>{_esc(i.diagnostics) if i.diagnostics else ''}</td>"
        f"<td>{_esc(next((e.valueString for e in (i.extension or []) if e.url == EXT_SOURCE), '') or '')}</td></tr>"
        for i in issues
    )
    div = (
        '<div xmlns="http://www.w3.org/1999/xhtml"><p class="res-header-id"><b>OperationOutcome </b></p>'
        '<table class="grid"><tr><td><b>Severity</b></td><td><b>Location</b></td><td><b>Code</b></td>'
        f"<td><b>Details</b></td><td><b>Diagnostics</b></td><td><b>Source</b></td></tr>{rows}</table></div>"
    )
    return Narrative(div=div)


class OperationOutcome(BaseModel):
    resourceType: Literal["OperationOutcome"] = "OperationOutcome"
    id: str | None = None
    text: Narrative | None = None
    issue: list[OperationOutcomeIssue] = Field(default_factory=list)

    @classmethod
    def success(cls, message: str = "All OK") -> "OperationOutcome":
        return cls(
            issue=[
                OperationOutcomeIssue(
                    severity="information",
                    code="informational",
                    details=CodeableConcept(text=message),
                    extension=[Extension(url=EXT_SOURCE, valueString="InstanceValidator")],
                )
            ]
        )

    @classmethod
    def from_error(
        cls,
        message: str,
        issue_type: str = "structure",
        severity: Severity = "error",
        expression: list[str] | None = None,
        line: int = 1,
        col: int = 1,
        source: str = SOURCE_VALIDATION_SERVICE,
    ) -> "OperationOutcome":
        """Wrapper-generated outcome (same shape as the Java wrapper's own OperationOutcomes)."""
        return cls(
            issue=[
                OperationOutcomeIssue(
                    severity=severity,
                    code=issue_type,
                    diagnostics=message,
                    details=CodeableConcept(text=message),
                    expression=expression,
                    extension=_location_extensions(line, col, source),
                )
            ]
        )

    @classmethod
    def loading(cls) -> "OperationOutcome":
        """Returned with HTTP 503 while the validator is still starting."""
        message = "Validator still loading... please wait."
        return cls.from_error(message, issue_type="incomplete", severity="error")

    @classmethod
    def from_issues(cls, issues: list) -> "OperationOutcome":
        out: list[OperationOutcomeIssue] = []
        for i in issues:
            out.append(
                OperationOutcomeIssue(
                    severity=i.severity,
                    code=i.code,
                    details=CodeableConcept(text=i.message),
                    expression=[i.path] if i.path else None,
                    extension=_location_extensions(i.line, i.col, i.source, i.message_id),
                )
            )
        if not out:
            return cls.success()
        return cls(issue=out, text=_narrative(out))
