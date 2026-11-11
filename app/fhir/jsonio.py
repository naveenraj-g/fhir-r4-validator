"""JSON helpers: Decimal-preserving parse/serialise and source-position tracking.

FHIR decimals must keep their lexical precision (``1.50`` stays ``1.50``), which
the stdlib float round-trip loses.  We therefore parse floats as ``Decimal`` and
use a small custom serialiser.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

PathKey = tuple  # tuple of str | int


def loads(text: str | bytes) -> Any:
    return json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"Invalid JSON constant '{name}'")


def dumps(value: Any) -> str:
    """Compact serialiser that preserves Decimal formatting."""
    parts: list[str] = []
    _write(value, parts)
    return "".join(parts)


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, Decimal):
        s = str(value)
        out.append(format(value, "f") if "E" in s.upper() else s)
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(repr(value))
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, dict):
        out.append("{")
        first = True
        for k, v in value.items():
            if not first:
                out.append(",")
            first = False
            out.append(json.dumps(str(k), ensure_ascii=False))
            out.append(":")
            _write(v, out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, v in enumerate(value):
            if i:
                out.append(",")
            _write(v, out)
        out.append("]")
    else:
        out.append(json.dumps(str(value), ensure_ascii=False))


class JsonPositions:
    """Maps a JSON path (tuple of keys / indexes) to a 1-based (line, col)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos: dict[PathKey, tuple[int, int]] = {}  # START of each value
        self.end: dict[PathKey, tuple[int, int]] = {}  # position just AFTER each value (what HAPI reports)
        self._i = 0
        self._line = 1
        self._line_start = 0
        try:
            self._ws()
            self._value(())
        except Exception:
            # Positions are best-effort; never fail validation because of them.
            pass

    def get(self, path: PathKey) -> tuple[int, int]:
        while path:
            if path in self.end:
                return self.end[path]
            path = path[:-1]
        # HAPI's root position depends on its lexer read-ahead; for a one-line document it is the end of the line
        if "\n" not in self.text.strip("\n"):
            return (1, len(self.text.rstrip("\n")) + 1)
        return (1, 2)

    # -- tiny recursive-descent scanner (structure only) ------------------
    def _mark(self, path: PathKey) -> None:
        self.pos[path] = (self._line, self._i - self._line_start + 1)

    def _ws(self) -> None:
        t = self.text
        n = len(t)
        while self._i < n:
            c = t[self._i]
            if c == "\n":
                self._line += 1
                self._i += 1
                self._line_start = self._i
            elif c in " \t\r":
                self._i += 1
            else:
                break

    def _string(self) -> str:
        t = self.text
        i = self._i + 1
        while t[i] != '"':
            i += 2 if t[i] == "\\" else 1
        s = t[self._i : i + 1]
        self._i = i + 1
        return json.loads(s)

    def _value(self, path: PathKey) -> None:
        self._value_inner(path)
        self.end[path] = (self._line, self._i - self._line_start + 1)

    def _value_inner(self, path: PathKey) -> None:
        t = self.text
        c = t[self._i]
        self._mark(path)
        if c == "{":
            self._i += 1
            self._ws()
            if t[self._i] == "}":
                self._i += 1
                return
            while True:
                self._ws()
                self._mark(path + ("\0key",))
                key_pos = (self._line, self._i - self._line_start + 1)
                key = self._string()
                self._ws()
                self._i += 1  # ':'
                self._ws()
                self._value(path + (key,))
                self.pos[path + (key,)] = key_pos
                self._ws()
                if t[self._i] == ",":
                    self._i += 1
                    continue
                self._i += 1  # '}'
                return
        elif c == "[":
            self._i += 1
            self._ws()
            if t[self._i] == "]":
                self._i += 1
                return
            idx = 0
            while True:
                self._ws()
                self._value(path + (idx,))
                idx += 1
                self._ws()
                if t[self._i] == ",":
                    self._i += 1
                    continue
                self._i += 1  # ']'
                return
        elif c == '"':
            self._string()
        else:
            n = len(t)
            while self._i < n and t[self._i] not in ",]} \t\r\n":
                self._i += 1
