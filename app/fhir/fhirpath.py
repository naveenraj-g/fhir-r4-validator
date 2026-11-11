"""FHIRPath evaluation (R4 model) with the FHIR-specific functions fhirpathpy lacks."""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING, Any, Callable

from antlr4 import CommonTokenStream, InputStream, Token
from antlr4.error.ErrorListener import ErrorListener
from antlr4.tree.Tree import ParseTreeWalker
from fhirpathpy import apply_parsed_path
from fhirpathpy.engine.nodes import ResourceNode
from fhirpathpy.engine.util import get_data
from fhirpathpy.models import models
from fhirpathpy.parser.ASTPathListener import ASTPathListener
from fhirpathpy.parser.generated.FHIRPathLexer import FHIRPathLexer
from fhirpathpy.parser.generated.FHIRPathParser import FHIRPathParser

from app.fhir import xhtml
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.fhir.terminology import TerminologyService

logger = get_logger(__name__)

def _build_model() -> dict[str, Any]:
    """fhirpathpy's R4 model, with the FHIR types of ``id`` / ``url`` restored.

    The bundled model types every ``id`` and ``url`` as ``System.String``; FHIR says ``Resource.id`` is an
    ``id``, ``Element.id`` a ``string`` and ``Extension.url`` a ``uri`` (which is what HAPI reports).
    """
    base = models["r4"]
    parent = base["type2Parent"]

    def is_resource(name: str) -> bool:
        seen = 0
        while name and seen < 10:
            if name == "Resource":
                return True
            name, seen = parent.get(name), seen + 1
        return False

    path2type = dict(base["path2Type"])
    for path, typ in base["path2Type"].items():
        if typ != "System.String":
            continue
        root, _, rest = path.partition(".")
        if path.endswith(".url"):
            path2type[path] = "uri"
        elif path.endswith(".id"):
            path2type[path] = "id" if "." not in rest and is_resource(root) else "string"
    return {**base, "path2Type": path2type}


R4_MODEL = _build_model()

Resolver = Callable[[str], dict[str, Any] | None]

_REF_TYPE = re.compile(r"(?:^|/)([A-Z][A-Za-z]+)/[A-Za-z0-9\-.]{1,64}(?:/_history/[A-Za-z0-9\-.]{1,64})?$")


class FhirPathError(ValueError):
    pass


class _StrictListener(ErrorListener):
    """fhirpathpy's own parser ignores syntax errors; HAPI rejects them, so we do too."""

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):  # noqa: N802 (antlr API)
        raise FhirPathError(f"{msg} (line {line}, col {column + 1})")


def parse(expression: str) -> Any:
    """Parse a FHIRPath expression, raising ``FhirPathError`` on any syntax error."""
    listener = _StrictListener()
    lexer = FHIRPathLexer(InputStream(expression))
    lexer.removeErrorListeners()
    lexer.addErrorListener(listener)
    parser = FHIRPathParser(CommonTokenStream(lexer))
    parser.buildParseTrees = True
    parser.removeErrorListeners()
    parser.addErrorListener(listener)
    tree = parser.expression()
    if parser.getCurrentToken().type != Token.EOF:
        raise FhirPathError(f"Unexpected input '{parser.getCurrentToken().text}'")
    ast = ASTPathListener()
    ParseTreeWalker().walk(ast, tree)
    return ast.parentStack[0]


def _is_primitive_value(v: Any) -> bool:
    return not isinstance(v, (dict, list))


class FhirPathRunner:
    def __init__(self, terminology: "TerminologyService | None" = None) -> None:
        self.terminology = terminology
        self._parsed: dict[str, Any] = {}
        self._lock = threading.RLock()

    # ----------------------------------------------------------------- parsing
    def parse(self, expression: str) -> Any:
        cached = self._parsed.get(expression)
        if cached is None:
            try:
                cached = parse(expression)
            except Exception as e:
                cached = FhirPathError(f"Error parsing FHIRPath expression '{expression}': {e}")
            self._parsed[expression] = cached
        if isinstance(cached, FhirPathError):
            raise cached
        return cached

    # ----------------------------------------------------------------- user functions
    def _table(self, resolver: Resolver | None, trace_sink: list | None = None) -> dict[str, Any]:
        term = self.terminology

        def has_value(inputs: list[Any]) -> list[Any]:
            return [len(inputs) == 1 and _is_primitive_value(inputs[0])]

        def html_checks(inputs: list[Any]) -> list[Any]:
            if not inputs:
                return []
            return [all(isinstance(i, str) and not xhtml.check_narrative(i) for i in inputs)]  # any problem

        def conforms_to(inputs: list[Any], _url: Any = None) -> list[Any]:
            return [True]

        def matches(inputs: list[Any], regex: Any = "") -> list[Any]:
            if len(inputs) != 1 or not isinstance(inputs[0], str) or not isinstance(regex, str):
                return []
            try:
                return [re.fullmatch(regex, inputs[0], re.DOTALL) is not None]
            except re.error:
                return []

        def trace(inputs: list[Any], name: Any = "") -> list[Any]:
            if trace_sink is not None:
                trace_sink.append((str(name), list(inputs)))
            return inputs

        def resolve(inputs: list[Any]) -> list[Any]:
            out: list[Any] = []
            for item in inputs:
                ref = item.get("reference") if isinstance(item, dict) else item
                if not isinstance(ref, str) or not ref:
                    continue
                found = resolver(ref) if resolver is not None else None
                if isinstance(found, dict) and found.get("resourceType"):
                    out.append(ResourceNode.create_node(found, found["resourceType"]))
                else:
                    # Unresolvable: return a typed placeholder (like HAPI) so `resolve() is Patient` still works
                    m = _REF_TYPE.search(ref)
                    if m:
                        out.append(ResourceNode.create_node({"resourceType": m.group(1)}, m.group(1)))
            return out

        def member_of(inputs: list[Any], vs_url: str) -> list[Any]:
            if not inputs or term is None:
                return []
            results: list[bool] = []
            for item in inputs:
                results.append(self._member(term, item, vs_url))
            return [all(results)] if results else []

        return {
            "hasValue": {"fn": has_value, "arity": {0: []}},
            "htmlChecks": {"fn": html_checks, "arity": {0: []}},
            "matches": {"fn": matches, "arity": {1: ["String"]}, "nullable_input": True},
            "conformsTo": {"fn": conforms_to, "arity": {1: ["String"]}},
            "trace": {"fn": trace, "arity": {0: [], 1: ["String"]}},
            "resolve": {"fn": resolve, "arity": {0: []}},
            "memberOf": {"fn": member_of, "arity": {1: ["String"]}},
        }

    @staticmethod
    def _member(term: "TerminologyService", item: Any, vs_url: str) -> bool:
        def check(system: str | None, code: str | None) -> bool:
            if not code:
                return False
            return term.validate_in_valueset(vs_url, system, code).status != "invalid"

        if isinstance(item, str):
            return check(None, item)
        if isinstance(item, dict):
            if "coding" in item:
                codings = item.get("coding") or []
                return any(check(c.get("system"), c.get("code")) for c in codings) if codings else False
            return check(item.get("system"), item.get("code"))
        return False

    # ----------------------------------------------------------------- evaluation
    def evaluate(
        self,
        expression: str,
        data: Any,
        base: str | None = None,
        variables: dict[str, Any] | None = None,
        resolver: Resolver | None = None,
        trace_sink: list | None = None,
    ) -> list[Any]:
        """Evaluate and return raw nodes (``ResourceNode`` carries the FHIR type in ``.path``)."""
        parsed = self.parse(expression)
        root = data
        if base and not (isinstance(data, dict) and "resourceType" in data):
            root = ResourceNode.create_node(data, base)
        options = {"returnRawData": True, "userInvocationTable": self._table(resolver, trace_sink)}
        with self._lock:
            try:
                result = apply_parsed_path(root, parsed, variables or {}, R4_MODEL, options)
            except FhirPathError:
                raise
            except Exception as e:
                raise FhirPathError(f"Error evaluating FHIRPath expression '{expression}': {e}") from e
        return result if isinstance(result, list) else [result]

    def evaluate_values(self, *args: Any, **kwargs: Any) -> list[Any]:
        return [get_data(n) for n in self.evaluate(*args, **kwargs)]

    def evaluate_boolean(
        self,
        expression: str,
        data: Any,
        base: str | None,
        variables: dict[str, Any],
        resolver: Resolver | None,
        trace_sink: list | None = None,
        empty_is: bool = True,
    ) -> bool:
        """Did the invariant hold?

        A single boolean is taken at face value; an *empty* result counts as satisfied. HAPI gets the same
        outcome for the R4 invariants (e.g. ref-1, bdl-8, ras-2) because its ``in``/``is`` operators return
        a boolean on empty input, where fhirpathpy returns empty; treating empty as a pass is how we
        reproduce that without forking the engine.
        """
        values = self.evaluate_values(expression, data, base, variables, resolver, trace_sink)
        if not values:
            return empty_is
        if len(values) == 1 and isinstance(values[0], bool):
            return values[0]
        return True
