"""Narrative (XHTML) validation per the FHIR R4 spec (limited HTML subset)."""

from __future__ import annotations

from xml.parsers import expat

XHTML_NS = "http://www.w3.org/1999/xhtml"

ALLOWED_ELEMENTS = {
    "a", "abbr", "acronym", "address", "b", "big", "blockquote", "br", "caption", "cite", "code", "col",
    "colgroup", "dd", "dfn", "div", "dl", "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
    "li", "ol", "p", "pre", "q", "samp", "small", "span", "strong", "sub", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "tt", "ul", "var",
}
ALLOWED_ATTRIBUTES = {
    "abbr", "accesskey", "align", "alt", "axis", "bgcolor", "border", "cellhalign", "cellpadding",
    "cellspacing", "cellvalign", "char", "charoff", "charset", "cite", "class", "colspan", "compact",
    "coords", "dir", "frame", "headers", "height", "href", "hreflang", "hspace", "id", "lang", "longdesc",
    "name", "nowrap", "rel", "rev", "rowspan", "rules", "scope", "shape", "span", "src", "start", "style",
    "summary", "tabindex", "title", "type", "valign", "value", "vspace", "width", "xml:lang", "xmlns",
}

# problem kinds
ELEMENT, ATTRIBUTE, NAMESPACE, ROOT, PARSE, EMPTY = "element", "attribute", "namespace", "root", "parse", "empty"


def check_narrative(div: str) -> list[tuple[str, str]]:
    """Return ``(kind, message)`` problems with the narrative XHTML (empty list = valid)."""
    problems: list[tuple[str, str]] = []
    depth = 0
    has_text = False
    root_seen = False
    parser = expat.ParserCreate(namespace_separator=" ")

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, root_seen
        ns, _, local = name.rpartition(" ")
        if depth == 0:
            root_seen = True
            if local != "div":
                problems.append((ROOT, f"Wrong name for the root element: expected 'div', found '{local}'"))
            if ns != XHTML_NS:
                problems.append((NAMESPACE, f"Wrong namespace on the XHTML ('{ns or 'null'}', should be '{XHTML_NS}')"))
        if local not in ALLOWED_ELEMENTS:
            problems.append((ELEMENT, f"Invalid element name in the XHTML ('{local}')"))
        for a, v in attrs.items():
            a_local = a.rpartition(" ")[2]
            if a_local not in ALLOWED_ATTRIBUTES and a != "http://www.w3.org/XML/1998/namespace lang":
                problems.append((ATTRIBUTE, f"Invalid attribute name in the XHTML ('{a_local}')"))
            if a_local == "href" and v.strip().lower().startswith("javascript:"):
                problems.append((ATTRIBUTE, "javascript: URLs are not allowed in the narrative"))
        depth += 1

    def end(_n: str) -> None:
        nonlocal depth
        depth -= 1

    def chars(data: str) -> None:
        nonlocal has_text
        if data.strip():
            has_text = True

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = lambda *a: problems.append((PARSE, "DTD is not allowed in the narrative"))
    try:
        parser.Parse(div.encode("utf-8"), True)
    except expat.ExpatError as e:
        problems.append((PARSE, f"Error parsing the narrative XHTML: {expat.ErrorString(e.code)}"))
        return problems
    if root_seen and not has_text and "<img" not in div:
        problems.append((EMPTY, "The narrative has no text content"))
    return problems
