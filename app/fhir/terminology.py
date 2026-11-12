"""Terminology: local CodeSystem / ValueSet evaluation with optional tx-server fallback.

Resolution order for every question ("is code X in value set Y?", "is the display right?"):

1. Local definitions (core R4 + any loaded IG): authoritative when the CodeSystem is complete.
2. Remote terminology server (``TX_SERVER_URL``) for anything that cannot be decided locally
   (SNOMED CT, LOINC, RxNorm, UCUM, ...). Disabled when ``DISABLE_TX`` is set.
3. Otherwise the answer is "unknown" and the validator reports a hint rather than a false error.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.services.profile_registry import ProfileRegistry

logger = get_logger(__name__)

OK, INVALID, UNKNOWN = "ok", "invalid", "unknown"


@dataclass
class CodeCheck:
    status: str  # ok | invalid | unknown
    message: str | None = None
    display: str | None = None  # canonical display (when known)
    wrong_display: bool = False
    system_known: bool = True
    source: str = "local"  # local | tx
    extra: dict[str, Any] = field(default_factory=dict)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


class _CsIndex:
    """Flattened view of a CodeSystem's concept hierarchy."""

    def __init__(self, cs: dict[str, Any]) -> None:
        self.cs = cs
        self.concepts: dict[str, dict[str, Any]] = {}
        self.parent: dict[str, str | None] = {}
        self.children: dict[str, list[str]] = {}
        self.case_sensitive = cs.get("caseSensitive", True)
        self.complete = cs.get("content") in (None, "complete")
        self._walk(cs.get("concept", []), None)

    def _walk(self, concepts: list[dict[str, Any]], parent: str | None) -> None:
        for c in concepts:
            code = c.get("code")
            if code is None:
                continue
            key = code if self.case_sensitive else code.lower()
            self.concepts[key] = c
            self.parent[key] = parent
            if parent is not None:
                self.children.setdefault(parent, []).append(key)
            self._walk(c.get("concept", []), key)

    def key(self, code: str) -> str:
        return code if self.case_sensitive else code.lower()

    def lookup(self, code: str) -> dict[str, Any] | None:
        return self.concepts.get(self.key(code))

    def descendants(self, code: str, include_self: bool) -> set[str]:
        out: set[str] = set()
        start = self.key(code)
        if include_self:
            out.add(start)
        stack = list(self.children.get(start, []))
        while stack:
            c = stack.pop()
            if c not in out:
                out.add(c)
                stack.extend(self.children.get(c, []))
        return out

    def displays(self, concept: dict[str, Any]) -> list[str]:
        out = [concept.get("display")] if concept.get("display") else []
        for d in concept.get("designation", []) or []:
            if d.get("value"):
                out.append(d["value"])
        return out


class TerminologyService:
    def __init__(
        self,
        registry: "ProfileRegistry",
        tx_url: str | None,
        disabled: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self.registry = registry
        self.disabled = disabled or not tx_url
        self.tx_url = self._normalise_tx(tx_url) if tx_url else None
        self.timeout = timeout
        self._cs_index: dict[int, _CsIndex] = {}
        self._cache: dict[tuple, CodeCheck] = {}
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._tx_down_until = 0.0
        self._cache_gen = -1

    @staticmethod
    def _normalise_tx(url: str) -> str:
        url = url.rstrip("/")
        # tx.fhir.org serves several FHIR versions below /rX
        if re.match(r"^https?://tx\.fhir\.org$", url):
            return url + "/r4"
        return url

    # ----------------------------------------------------------------- local helpers
    def _index(self, cs: dict[str, Any]) -> _CsIndex:
        idx = self._cs_index.get(id(cs))
        if idx is None or idx.cs is not cs:
            idx = _CsIndex(cs)
            self._cs_index[id(cs)] = idx
        return idx

    def _local_cs(self, system: str) -> _CsIndex | None:
        if not self.registry.code_system_available(system):
            return None
        return self._index(self.registry.get_code_system(system))  # type: ignore[arg-type]

    def _check_display(self, idx: _CsIndex, concept: dict[str, Any], display: str | None) -> CodeCheck:
        canonical = concept.get("display")
        if display and canonical and _norm(display) not in {_norm(d) for d in idx.displays(concept)}:
            return CodeCheck(OK, display=canonical, wrong_display=True)
        return CodeCheck(OK, display=canonical)

    def validate_in_system(self, system: str, code: str, display: str | None = None) -> CodeCheck:
        """Validate that ``system#code`` exists (and that the display is right)."""
        idx = self._local_cs(system)
        if idx is not None:
            concept = idx.lookup(code)
            if concept is not None:
                return self._check_display(idx, concept, display)
            if idx.complete:
                ver = idx.cs.get("version")
                return CodeCheck(
                    INVALID,
                    message=f"Unknown code '{code}' in the CodeSystem '{system}'" + (f" version '{ver}'" if ver else ""),
                )
        return self._remote_validate(system=system, code=code, display=display, value_set=None)

    # ----------------------------------------------------------------- value sets
    def _vs_contains(self, vs: dict[str, Any], system: str | None, code: str, depth: int = 0) -> bool | None:
        """True / False / None (cannot decide locally)."""
        if depth > 8:
            return None
        compose = vs.get("compose")
        if compose and depth == 0 and self.undecidable_include(vs.get("url", "") or "") is not None:
            return None  # without the code systems the value set cannot be expanded (HAPI says so too)
        if not compose:
            exp = vs.get("expansion", {}).get("contain")
            if exp is not None:
                return self._in_expansion(exp, system, code)
            return None
        includes = compose.get("include", [])
        excludes = compose.get("exclude", [])
        result: bool | None = False
        for inc in includes:
            r = self._include_matches(inc, system, code, depth)
            if r is True:
                result = True
                break
            if r is None:
                result = None
        if result is not False:
            if result is True:
                for exc in excludes:
                    if self._include_matches(exc, system, code, depth) is True:
                        return False
            return result
        return False

    def _in_expansion(self, contains: list[dict[str, Any]], system: str | None, code: str) -> bool:
        for c in contains:
            if c.get("code") == code and (system is None or c.get("system") == system):
                return True
            if self._in_expansion(c.get("contain", []), system, code):
                return True
        return False

    def _include_matches(self, inc: dict[str, Any], system: str | None, code: str, depth: int) -> bool | None:
        # valueSet imports (intersection)
        for vs_url in inc.get("valueSet", []) or []:
            sub = self.registry.get_value_set(vs_url)
            if sub is None:
                return None
            r = self._vs_contains(sub, system, code, depth + 1)
            if r is not True:
                if not inc.get("system"):
                    return r
                return r if r is None else False
        isys = inc.get("system")
        if not isys:
            return True if inc.get("valueSet") else False
        if system is not None and system != isys:
            return False
        concepts = inc.get("concept")
        idx = self._local_cs(isys)
        if idx is None and (system is None or system == isys):
            return None  # HAPI needs the code system itself, even when the include lists its concepts
        if concepts:
            cs_case = idx.case_sensitive if idx else True
            for c in concepts:
                a, b = c.get("code", ""), code
                if (a == b) or (not cs_case and a.lower() == b.lower()):
                    return True
            return False
        filters = inc.get("filter")
        if idx is None:
            return None
        if filters:
            allowed: set[str] | None = None
            for f in filters:
                s = self._apply_filter(idx, f)
                if s is None:
                    return None
                allowed = s if allowed is None else allowed & s
            return idx.key(code) in (allowed or set())
        if idx.lookup(code) is not None:
            return True
        return False if idx.complete else None

    def _apply_filter(self, idx: _CsIndex, f: dict[str, Any]) -> set[str] | None:
        prop, op, val = f.get("property"), f.get("op"), f.get("value", "")
        if prop in ("concept", "code") and op in ("is-a", "descendent-of"):
            return idx.descendants(val, include_self=(op == "is-a"))
        if prop in ("concept", "code") and op == "is-not-a":
            return set(idx.concepts) - idx.descendants(val, include_self=True)
        if op == "=" and prop in ("code", "concept"):
            return {idx.key(val)} if idx.lookup(val) else set()
        if op == "regex" and prop in ("code", "concept"):
            try:
                rx = re.compile(val)
            except re.error:
                return None
            return {k for k in idx.concepts if rx.fullmatch(k)}
        if op == "in" and prop in ("code", "concept"):
            wanted = {idx.key(v.strip()) for v in val.split(",")}
            return {k for k in idx.concepts if k in wanted}
        if op in ("=", "in", "exists") and prop:
            out: set[str] = set()
            for key, c in idx.concepts.items():
                for p in c.get("property", []) or []:
                    if p.get("code") != prop:
                        continue
                    pv = next((p[k] for k in p if k.startswith("value")), None)
                    if op == "=" and str(pv) == str(val):
                        out.add(key)
                    elif op == "in" and str(pv) in {v.strip() for v in val.split(",")}:
                        out.add(key)
                    elif op == "exists" and (str(val).lower() == "true"):
                        out.add(key)
            return out
        return None

    def validate_in_valueset(
        self, vs_url: str, system: str | None, code: str, display: str | None = None
    ) -> CodeCheck:
        """Is ``system#code`` (with optional display) a member of the value set ``vs_url``?"""
        if self._cache_gen != self.registry.generation:
            with self._lock:
                self._cache.clear()
                self._cache_gen = self.registry.generation
        key = (vs_url, system, code, display, "vs")
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        vs = self.registry.get_value_set(vs_url)
        result: CodeCheck | None = None
        if vs is not None:
            member = self._vs_contains(vs, system, code)
            if member is True:
                result = CodeCheck(OK)
                if system:
                    idx = self._local_cs(system)
                    if idx is not None and (c := idx.lookup(code)) is not None:
                        result = self._check_display(idx, c, display)
            elif member is False:
                result = CodeCheck(
                    INVALID,
                    message=self._not_in_vs_message(system, code),
                )
        if result is None:
            result = self._remote_validate(system=system, code=code, display=display, value_set=vs_url, vs_resource=vs)
        self._remember(key, result)
        return result

    def _not_in_vs_message(self, system: str | None, code: str) -> str:
        if system:
            idx = self._local_cs(system)
            if idx is not None and idx.lookup(code) is None and idx.complete:
                return f"Unknown code '{code}' in the CodeSystem '{system}'"
            return f"The code '{code}' from the system '{system}' is not in the value set"
        return f"The code '{code}' is not in the value set"

    def enumerated_membership(self, vs_url: str, system: str, code: str, _depth: int = 0) -> bool | None:
        """True if some include lists ``system#code`` explicitly, False if listed includes exist but not the code, else None."""
        vs = self.registry.get_value_set(vs_url)
        if vs is None or _depth > 4:
            return None
        saw_list = False
        for inc in (vs.get("compose") or {}).get("include", []):
            if inc.get("system") == system and inc.get("concept"):
                saw_list = True
                if any(c.get("code") == code for c in inc["concept"]):
                    return True
            for sub in inc.get("valueSet", []) or []:
                r = self.enumerated_membership(sub, system, code, _depth + 1)
                if r is True:
                    return True
                saw_list = saw_list or r is False
        return False if saw_list else None

    def undecidable_include(self, vs_url: str, _depth: int = 0) -> tuple[int, str] | None:
        """(index, system) of the first compose.include (recursively) whose code system is not available."""
        vs = self.registry.get_value_set(vs_url)
        if vs is None or _depth > 6:
            return None
        for i, inc in enumerate((vs.get("compose") or {}).get("include", [])):
            system = inc.get("system")
            if system and not self.registry.code_system_available(system):
                return i, system
            for sub in inc.get("valueSet", []) or []:
                found = self.undecidable_include(sub, _depth + 1)
                if found is not None:
                    return found
        return None

    def value_set_known(self, vs_url: str) -> bool:
        return self.registry.get_value_set(vs_url) is not None

    # ----------------------------------------------------------------- remote tx server
    def _remember(self, key: tuple, value: CodeCheck) -> None:
        with self._lock:
            if len(self._cache) > 20000:
                self._cache.clear()
            self._cache[key] = value

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={"Accept": "application/fhir+json", "Content-Type": "application/fhir+json"},
            )
        return self._client

    def _remote_validate(
        self,
        system: str | None,
        code: str,
        display: str | None,
        value_set: str | None,
        vs_resource: dict[str, Any] | None = None,
    ) -> CodeCheck:
        unknown = CodeCheck(UNKNOWN, system_known=False, message="Code could not be validated: terminology server unavailable")
        if self.disabled or not self.tx_url:
            return unknown
        import time

        if time.time() < self._tx_down_until:
            return unknown
        key = ("tx", system, code, display, value_set)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        params: list[dict[str, Any]] = []
        coding: dict[str, Any] = {"code": code}
        if system:
            coding["system"] = system
        if display:
            coding["display"] = display
        params.append({"name": "coding", "valueCoding": coding})
        if value_set:
            endpoint = "/ValueSet/$validate-code"
            params.append({"name": "url", "valueUri": value_set.split("|")[0]})
            if "|" in value_set:
                params.append({"name": "valueSetVersion", "valueString": value_set.split("|", 1)[1]})
            if vs_resource is not None and not value_set.startswith("http://hl7.org/fhir/ValueSet/"):
                params.append({"name": "valueSet", "resource": vs_resource})
        else:
            endpoint = "/CodeSystem/$validate-code"
            params = [
                {"name": "url", "valueUri": system},
                {"name": "code", "valueCode": code},
            ] + ([{"name": "display", "valueString": display}] if display else [])
        body = {"resourceType": "Parameters", "parameter": params}
        try:
            r = self._http().post(self.tx_url + endpoint, json=body)
            data = r.json()
        except Exception as e:  # network / timeout / bad json
            logger.warning(f"Terminology server request failed: {e}")
            self._tx_down_until = time.time() + 30
            return unknown
        if data.get("resourceType") == "OperationOutcome":
            msg = "; ".join(i.get("details", {}).get("text", "") or i.get("diagnostics", "") for i in data.get("issue", []))
            res = CodeCheck(UNKNOWN, message=msg or None, system_known=False, source="tx")
            self._remember(key, res)
            return res
        out: dict[str, Any] = {}
        for p in data.get("parameter", []):
            out[p.get("name")] = next((p[k] for k in p if k.startswith("value")), None)
        ok = out.get("result") is True
        message = out.get("message")
        server_display = out.get("display")
        wrong_display = False
        if ok and display and server_display and _norm(display) != _norm(server_display):
            wrong_display = True
        if not ok and message and "display" in message.lower() and "wrong" in message.lower():
            # some servers report a display mismatch as a failed result
            wrong_display, ok = True, True
        # "was not found in the value set" / "Unknown code" are real verdicts; only a missing code system or
        # value set (the server cannot answer at all) is "unknown".
        verdict = bool(message) and bool(re.search(r"was not found in the value set|Unknown code|not valid in the code system", message, re.I))
        cannot_answer = (
            not ok and not verdict and bool(message)
            and bool(re.search(r"could not be found|unable to (find|resolve|locate)|unknown (code ?system|value ?set)|(code ?system|value ?set).{0,200}?\bnot (found|known)", message, re.I))
        )
        if cannot_answer:
            res = CodeCheck(UNKNOWN, message=message, system_known=False, source="tx")
        else:
            res = CodeCheck(OK if ok else INVALID, message=message, display=server_display, wrong_display=wrong_display, source="tx")
        self._remember(key, res)
        return res
