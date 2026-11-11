"""Type navigation over StructureDefinition snapshots (shared by the XML codec and validator)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.fhir.tree import SnapshotTree, last_segment

if TYPE_CHECKING:
    from app.services.profile_registry import ProfileRegistry

FHIRPATH_STRING = "http://hl7.org/fhirpath/System.String"


@dataclass(frozen=True)
class Ctx:
    """A structural position: the element whose children define the allowed properties."""

    tree: SnapshotTree
    el: dict[str, Any]

    @property
    def type_path(self) -> str:
        """FHIR path of the defining element (e.g. ``Patient.contact`` or ``HumanName``)."""
        return self.el["path"]


@dataclass
class Child:
    name: str  # name without [x]
    el: dict[str, Any]
    is_choice: bool
    many: bool
    types: list[dict[str, Any]]

    @property
    def min(self) -> int:
        return int(self.el.get("min", 0) or 0)

    @property
    def max(self) -> str:
        return str(self.el.get("max", "1"))


def type_code(t: dict[str, Any]) -> str:
    """Effective FHIR type code of an ElementDefinition.type entry.

    ``id`` / ``url`` / primitive ``value`` elements carry a FHIRPath system type as ``code`` and the real
    FHIR type in the ``structuredefinition-fhir-type`` extension.
    """
    code = t.get("code")
    if code and code.startswith("http://hl7.org/fhirpath/System."):
        for ext in (t.get("extension") or []) + (t.get("_code", {}).get("extension") or []):
            if ext.get("url", "").endswith("structuredefinition-fhir-type"):
                return ext.get("valueUrl") or ext.get("valueUri") or "string"
        return "string"
    if code:
        return code
    for ext in t.get("_code", {}).get("extension", []) or []:
        if ext.get("url", "").endswith("structuredefinition-fhir-type"):
            return ext.get("valueUrl", "string")
    return "string"


class Navigator:
    def __init__(self, registry: "ProfileRegistry") -> None:
        self.registry = registry
        self._children_cache: dict[tuple[int, str], list[Child]] = {}
        self._cache_generation = -1

    # ------------------------------------------------------------- contexts
    def resource_ctx(self, rtype: str, profile_sd: dict[str, Any] | None = None) -> Ctx | None:
        sd = profile_sd or self.registry.get_by_url(f"http://hl7.org/fhir/StructureDefinition/{rtype}")
        if sd is None or not sd.get("snapshot"):
            return None
        tree = self.registry.tree_for(sd)
        return Ctx(tree, tree.root) if tree.root else None

    def type_ctx(self, type_code_: str, profile_url: str | None = None) -> Ctx | None:
        sd = self.registry.get_by_url(profile_url) if profile_url else None
        if sd is None:
            sd = self.registry.type_sd(type_code_)
        if sd is None or not sd.get("snapshot"):
            return None
        tree = self.registry.tree_for(sd)
        return Ctx(tree, tree.root) if tree.root else None

    # ------------------------------------------------------------- children
    def children(self, ctx: Ctx) -> list[Child]:
        if self._cache_generation != self.registry.generation:
            self._children_cache.clear()
            self._cache_generation = self.registry.generation
        ck = (id(ctx.tree), ctx.el["id"])
        hit = self._children_cache.get(ck)
        if hit is None:
            hit = self._children_cache[ck] = self._compute_children(ctx)
        return hit

    def _compute_children(self, ctx: Ctx) -> list[Child]:
        el = ctx.el
        tree = ctx.tree
        ref = tree.resolve_content_reference(el)
        parent = ref if ref is not None else el
        out: list[Child] = []
        for c in tree.children(parent):
            if c.get("sliceName"):
                continue
            raw = last_segment(c["path"])
            is_choice = raw.endswith("[x]")
            name = raw[:-3] if is_choice else raw
            mx = str(c.get("max", "1"))
            many = mx == "*" or (mx.isdigit() and int(mx) > 1)
            out.append(Child(name, c, is_choice, many, c.get("type", []) or []))
        return out

    def find_child(self, ctx: Ctx, prop: str) -> tuple[Child, str | None] | None:
        """Find the child definition matching JSON/XML property name ``prop``.

        Returns (child, resolved_type_code). For choice elements the type is taken
        from the suffix of the property name (``valueQuantity`` -> ``Quantity``).
        """
        for ch in self.children(ctx):
            if not ch.is_choice:
                if ch.name == prop:
                    return ch, (type_code(ch.types[0]) if len(ch.types) == 1 else None)
                continue
            if prop.startswith(ch.name) and len(prop) > len(ch.name) and prop[len(ch.name)].isupper():
                suffix = prop[len(ch.name):]
                for t in ch.types:
                    code = type_code(t)
                    if code[:1].upper() + code[1:] == suffix:
                        return ch, code
                return ch, None  # right element, type not permitted by the choice
        return None

    def descend(self, ctx: Ctx, child: "Child | dict[str, Any]", code: str | None, profile_url: str | None = None) -> Ctx | None:
        """Context for validating/parsing the complex value of ``child`` typed as ``code``."""
        el = child.el if isinstance(child, Child) else child
        ref = ctx.tree.resolve_content_reference(el)
        if ref is not None:
            return Ctx(ctx.tree, ref)
        if code in (None, "BackboneElement", "Element") or code == "Element":
            if ctx.tree.children(el):
                return Ctx(ctx.tree, el)
            return None
        # complex datatype / resource: its own (core or profiled) structure
        return self.type_ctx(code, profile_url)

    def is_primitive(self, code: str | None) -> bool:
        return bool(code) and self.registry.is_primitive(code)  # type: ignore[arg-type]
