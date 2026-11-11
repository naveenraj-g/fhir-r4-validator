"""Model-driven FHIR XML -> FHIR JSON conversion (with line/column tracking).

Unlike a generic XML->dict conversion this consults the StructureDefinitions so that
repeating elements become arrays even when they occur once, primitive ``value``
attributes get their JSON types, ``extension/@url`` and ``@id`` survive, contained
resources are unwrapped and the narrative ``div`` is kept as an XHTML string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from xml.parsers import expat

from app.fhir.model import Child, Ctx, Navigator, type_code

FHIR_NS = "http://hl7.org/fhir"
XHTML_NS = "http://www.w3.org/1999/xhtml"

_INT_TYPES = {"integer", "positiveInt", "unsignedInt", "integer64"}


class XmlParseError(ValueError):
    pass


@dataclass
class XNode:
    ns: str
    name: str
    attrs: dict[str, str]
    line: int
    col: int
    items: list[Any] = field(default_factory=list)  # str | XNode

    @property
    def children(self) -> list["XNode"]:
        return [i for i in self.items if isinstance(i, XNode)]

    @property
    def text(self) -> str:
        return "".join(i for i in self.items if isinstance(i, str))


def parse_xml(text: str | bytes) -> XNode:
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = True
    stack: list[XNode] = []
    root: list[XNode] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        ns, _, local = name.rpartition(" ")
        node = XNode(ns, local, dict(attrs), parser.CurrentLineNumber, parser.CurrentColumnNumber + 1)
        if stack:
            stack[-1].items.append(node)
        else:
            root.append(node)
        stack.append(node)

    def end(_name: str) -> None:
        stack.pop()

    def chars(data: str) -> None:
        if stack:
            stack[-1].items.append(data)

    def doctype(*_a: Any) -> None:
        raise XmlParseError("DTD is prohibited in FHIR XML")

    def entity(*_a: Any) -> None:
        raise XmlParseError("XML entities are prohibited in FHIR XML")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(text if isinstance(text, bytes) else text.encode("utf-8"), True)
    except expat.ExpatError as e:
        raise XmlParseError(f"Error parsing XML: {expat.ErrorString(e.code)} (line {e.lineno}, col {e.offset + 1})") from e
    if not root:
        raise XmlParseError("XML document has no root element")
    return root[0]


# --------------------------------------------------------------------------- xhtml
def _esc(s: str, attr: bool = False) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace('"', "&quot;") if attr else s


def serialize_xhtml(node: XNode, top: bool = True) -> str:
    attrs = "".join(f' {k.rpartition(" ")[2]}="{_esc(v, True)}"' for k, v in node.attrs.items())
    if top:
        attrs = f' xmlns="{XHTML_NS}"' + attrs
    inner = "".join(_esc(i) if isinstance(i, str) else serialize_xhtml(i, False) for i in node.items)
    if not node.items:
        return f"<{node.name}{attrs}/>"
    return f"<{node.name}{attrs}>{inner}</{node.name}>"


# --------------------------------------------------------------------------- conversion
@dataclass
class XmlResult:
    data: dict[str, Any]
    positions: dict[tuple, tuple[int, int]]
    issues: list[tuple[str, tuple, str]]  # (message, path-key, severity)


class XmlToJson:
    def __init__(self, nav: Navigator) -> None:
        self.nav = nav
        self.positions: dict[tuple, tuple[int, int]] = {}
        self.issues: list[tuple[str, tuple, str]] = []

    def convert(self, root: XNode) -> XmlResult:
        if root.ns != FHIR_NS:
            self.issues.append(
                (f"Wrong namespace '{root.ns}' on the root element: expected '{FHIR_NS}'", (), "error")
            )
        out = self._resource(root, ())
        return XmlResult(out, self.positions, self.issues)

    def _resource(self, node: XNode, path: tuple) -> dict[str, Any]:
        out: dict[str, Any] = {"resourceType": node.name}
        self.positions[path] = (node.line, node.col)
        ctx = self.nav.resource_ctx(node.name)
        self._fill(node, ctx, out, path, is_resource=True)
        return out

    def _fill(self, node: XNode, ctx: Ctx | None, out: dict[str, Any], path: tuple, is_resource: bool = False) -> None:
        for a, v in node.attrs.items():
            local = a.rpartition(" ")[2]
            if a.startswith("http://www.w3.org/2000/xmlns") or local == "xmlns":
                continue
            if local in ("id", "url") and not is_resource:
                out[local] = v
            elif local == "value":
                continue
            else:
                self.issues.append((f"Undefined attribute '@{local}' on {node.name} for type {ctx.type_path if ctx else '?'}", path, "error"))
        if node.text.strip() and not is_resource and node.name != "div":
            if "value" not in node.attrs:
                self.issues.append((f"Text should not be present (found '{node.text.strip()[:30]}')", path, "error"))

        for c in node.children:
            if c.ns != FHIR_NS and not (c.name == "div" and c.ns == XHTML_NS):
                self.issues.append((f"Wrong namespace '{c.ns}' on element '{c.name}'", path + (c.name,), "error"))
            found = self.nav.find_child(ctx, c.name) if ctx is not None else None
            if found is None:
                self._unknown(c, out, path)
                continue
            child, code = found
            self._child(c, child, code, ctx, out, path)  # type: ignore[arg-type]
        for k in [k for k in out if k.startswith("_") and isinstance(out[k], list) and k[1:] in out]:
            base = out[k[1:]]
            while len(out[k]) < len(base):
                out[k].append(None)

    def _unknown(self, c: XNode, out: dict[str, Any], path: tuple) -> None:
        if "value" in c.attrs and not c.children:
            val: Any = c.attrs["value"]
        else:
            val = {}
            self._fill(c, None, val, path + (c.name,))
        self._store(out, c.name, val, many=False, path=path, node=c)

    def _store(self, out: dict[str, Any], name: str, value: Any, many: bool, path: tuple, node: XNode) -> int:
        if name in out:
            if not isinstance(out[name], list):
                out[name] = [out[name]]
            out[name].append(value)
            idx = len(out[name]) - 1
        elif many:
            out[name] = [value]
            idx = 0
        else:
            out[name] = value
            idx = -1
        key = path + (name,)
        self.positions.setdefault(key, (node.line, node.col))
        if idx >= 0:
            self.positions[key + (idx,)] = (node.line, node.col)
        return idx

    def _child(self, c: XNode, child: Child, code: str | None, ctx: Ctx, out: dict[str, Any], path: tuple) -> None:
        name = c.name
        el = child.el
        is_xhtml = any(
            ext.get("url", "").endswith("elementdefinition-representation") and ext.get("valueCode") == "xhtml"
            for t in child.types
            for ext in (t.get("_code", {}).get("extension", []) or [])
        ) or (code == "xhtml")
        if is_xhtml:
            self._store(out, name, serialize_xhtml(c), child.many, path, c)
            return

        if code is not None and self.nav.is_primitive(code):
            raw = c.attrs.get("value")
            value: Any = self._primitive(raw, code) if raw is not None else None
            ext: dict[str, Any] | None = None
            if c.children or "id" in c.attrs:
                ext = {}
                self._fill(c, self.nav.type_ctx("Element"), ext, path + ("_" + name,))
            if value is None and ext is None:
                self.issues.append((f"Primitive element '{name}' has no value and no children", path + (name,), "error"))
            if child.many:
                lst = out.setdefault(name, [])
                if not isinstance(lst, list):
                    lst = out[name] = [lst]
                lst.append(value)
                i = len(lst) - 1
                self.positions.setdefault(path + (name,), (c.line, c.col))
                self.positions[path + (name, i)] = (c.line, c.col)
                if ext is not None:
                    elst = out.setdefault("_" + name, [])
                    while len(elst) < i:
                        elst.append(None)
                    elst.append(ext)
                    self.positions[path + ("_" + name, i)] = (c.line, c.col)
            else:
                if value is not None:
                    self._store(out, name, value, False, path, c)
                if ext is not None:
                    out["_" + name] = ext
                    self.positions.setdefault(path + ("_" + name,), (c.line, c.col))
            return

        is_resource_type = code in ("Resource", "DomainResource") or any(
            type_code(t) in ("Resource", "DomainResource") for t in child.types
        )
        if is_resource_type:
            inner = c.children[0] if c.children else None
            if inner is None:
                self.issues.append((f"Element '{name}' must contain a resource", path + (name,), "error"))
                return
            idx_hint = len(out.get(name, [])) if isinstance(out.get(name), list) else 0
            sub_path = path + (name,) + ((idx_hint,) if child.many else ())
            val = self._resource(inner, sub_path)
            self._store(out, name, val, child.many, path, c)
            return

        sub_ctx = self.nav.descend(ctx, child, code)
        val = {}
        idx_hint = len(out[name]) if isinstance(out.get(name), list) else (1 if name in out else 0)
        sub_path = path + (name,) + ((idx_hint,) if (child.many or name in out) else ())
        self._fill(c, sub_ctx, val, sub_path)
        self._store(out, name, val, child.many, path, c)

    @staticmethod
    def _primitive(raw: str, code: str) -> Any:
        if code == "boolean":
            if raw == "true":
                return True
            if raw == "false":
                return False
            return raw
        if code in _INT_TYPES:
            try:
                return int(raw)
            except ValueError:
                return raw
        if code == "decimal":
            try:
                return Decimal(raw)
            except InvalidOperation:
                return raw
        return raw


def xml_to_json(text: str | bytes, nav: Navigator) -> XmlResult:
    root = parse_xml(text)
    return XmlToJson(nav).convert(root)
