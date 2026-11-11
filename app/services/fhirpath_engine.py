from decimal import Decimal
from typing import Any

from fhirpathpy.engine.nodes import FP_Quantity, FP_DateTime, FP_Time, ResourceNode

from app.core.logging import get_logger
from app.fhir import jsonio
from app.fhir.fhirpath import FhirPathError, FhirPathRunner
from app.fhir.model import Navigator
from app.fhir.parser import ParseError, parse_resource
from app.services.profile_registry import ProfileRegistry

logger = get_logger(__name__)


class FhirPathService:
    """Evaluates FHIRPath expressions against FHIR resources or complex datatypes / backbone elements."""

    def __init__(self, registry: ProfileRegistry, runner: FhirPathRunner) -> None:
        self.registry = registry
        self.runner = runner
        self.nav = Navigator(registry)

    # -------------------------------------------------------------- helpers
    def _check_type(self, type_name: str) -> None:
        """Mirror the Java parser: ``X`` must be a datatype, ``A.b`` a backbone element path."""
        if "." in type_name:
            root = type_name.split(".")[0]
            sd = self.registry.type_sd(root)
            if sd is None or not sd.get("snapshot"):
                raise ParseError(f"Could not find class for path '{type_name}'")
            tree = self.registry.tree_for(sd)
            el = tree.by_id.get(type_name)
            if el is None or not (tree.children(el) or el.get("contentReference")):
                raise ParseError(f"Could not find class for path '{type_name}'")
            return
        sd = self.registry.type_sd(type_name)
        if sd is None or sd.get("kind") not in ("complex-type", "resource"):
            raise ParseError(f"Unsupported type '{type_name}'. Primitives are not supported as the root element")

    @staticmethod
    def _type_of(node: Any) -> str:
        if isinstance(node, ResourceNode):
            if node.path:
                return node.path
            node = node.data
        if isinstance(node, bool):
            return "boolean"
        if isinstance(node, int):
            return "integer"
        if isinstance(node, (float, Decimal)):
            return "decimal"
        if isinstance(node, FP_Quantity):
            return "Quantity"
        if isinstance(node, FP_DateTime):
            return "dateTime"
        if isinstance(node, FP_Time):
            return "time"
        if isinstance(node, dict):
            return str(node.get("resourceType", "Element"))
        return "string"

    @staticmethod
    def _plain(node: Any) -> Any:
        from fhirpathpy.engine.util import get_data

        data = get_data(node)
        if isinstance(data, FP_Quantity):
            return str(data)
        if hasattr(data, "date_str"):  # FP_TimeBase
            return data.date_str
        return data

    def _scrub(self, node: Any, root: bool = True) -> Any:
        """Drop a stray ``resourceType`` from nested non-resource elements (the Java parser ignores it)."""
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "resourceType" and not root and not self.registry.is_resource_type(str(v)):
                    continue
                out[k] = self._scrub(v, False)
            return out
        if isinstance(node, list):
            return [self._scrub(v, False) for v in node]
        return node

    # -------------------------------------------------------------- API
    def evaluate(self, content: bytes | str, expression: str, type_name: str | None) -> str:
        """Returns the JSON text ``[{"type": ..., "element": ...}, ...]`` (same shape as the Java service)."""
        parsed = parse_resource(content, self.nav)
        data = self._scrub(parsed.data)
        base: str | None = None
        if "resourceType" not in data:
            if not type_name:
                raise ParseError("The 'type' query parameter is required when the body is not a full resource")
            self._check_type(type_name)
            base = type_name
        nodes = self.runner.evaluate(expression, data, base, {"resource": data, "rootResource": data}, None)
        parts: list[str] = []
        for n in nodes:
            parts.append('{"type":%s,"element":%s}' % (jsonio.dumps(self._type_of(n)), jsonio.dumps(self._plain(n))))
        return "[" + ",".join(parts) + "]"


__all__ = ["FhirPathService", "FhirPathError", "ParseError"]
