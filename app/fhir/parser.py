"""Format detection and parsing of FHIR payloads (JSON or XML) with position tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.fhir import jsonio
from app.fhir.model import Navigator
from app.fhir.xml_codec import XmlParseError, xml_to_json


class ParseError(ValueError):
    """The payload could not be turned into a FHIR resource at all."""


class _LazyJsonPositions:
    def __init__(self, text: str) -> None:
        self._text = text
        self._pos: jsonio.JsonPositions | None = None

    def get(self, key: tuple) -> tuple[int, int]:
        if self._pos is None:
            self._pos = jsonio.JsonPositions(self._text)
        return self._pos.get(key)


class _MapPositions:
    def __init__(self, pos: dict[tuple, tuple[int, int]]) -> None:
        self._pos = pos

    def get(self, key: tuple) -> tuple[int, int]:
        while key:
            if key in self._pos:
                return self._pos[key]
            key = key[:-1]
        return self._pos.get((), (1, 1))


@dataclass
class Parsed:
    data: dict[str, Any]
    fmt: str  # json | xml
    positions: Any
    issues: list[tuple[str, tuple, str]] = field(default_factory=list)


def decode(content: bytes | str) -> str:
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")
    else:
        text = content
    return text


def determine_format(text: str) -> str:
    stripped = text.lstrip()
    if not stripped:
        raise ParseError("Unable to determine the format of the content: the body is empty")
    if stripped[0] == "{":
        return "json"
    if stripped[0] == "<":
        return "xml"
    raise ParseError("Unable to determine the format of the content: expected '{' (JSON) or '<' (XML)")


def parse_resource(content: bytes | str, nav: Navigator) -> Parsed:
    text = decode(content)
    fmt = determine_format(text)
    if fmt == "json":
        try:
            data = jsonio.loads(text)
        except ValueError as e:
            raise ParseError(f"Error parsing JSON: {e}") from e
        if not isinstance(data, dict):
            raise ParseError("Error parsing JSON: the primitive value must be a JSON object")
        return Parsed(data, "json", _LazyJsonPositions(text))
    try:
        res = xml_to_json(text, nav)
    except XmlParseError as e:
        raise ParseError(str(e)) from e
    return Parsed(res.data, "xml", _MapPositions(res.positions), res.issues)
