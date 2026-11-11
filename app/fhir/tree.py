"""Indexed view over a StructureDefinition snapshot (ElementDefinition tree)."""

from __future__ import annotations

from typing import Any


def element_id(el: dict[str, Any]) -> str:
    eid = el.get("id")
    if eid:
        return eid
    path = el.get("path", "")
    sname = el.get("sliceName")
    eid = f"{path}:{sname}" if sname else path
    el["id"] = eid
    return eid


def last_segment(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def parent_id_of(eid: str, known: dict[str, Any]) -> str | None:
    """Parent element id: strip trailing '.segment' (tolerating dots inside slice names)."""
    cur = eid
    while "." in cur:
        cur = cur.rsplit(".", 1)[0]
        if cur in known:
            return cur
    return None


class SnapshotTree:
    def __init__(self, sd: dict[str, Any]) -> None:
        self.sd = sd
        self.elements: list[dict[str, Any]] = sd.get("snapshot", {}).get("element", [])
        self.by_id: dict[str, dict[str, Any]] = {}
        self._children: dict[str, list[dict[str, Any]]] = {}
        for el in self.elements:
            eid = element_id(el)
            self.by_id[eid] = el
        for el in self.elements:
            eid = el["id"]
            pid = parent_id_of(eid, self.by_id)
            if pid is not None:
                self._children.setdefault(pid, []).append(el)
        self.root: dict[str, Any] | None = self.elements[0] if self.elements else None

    def children(self, el: dict[str, Any]) -> list[dict[str, Any]]:
        return self._children.get(el["id"], [])

    def resolve_content_reference(self, el: dict[str, Any]) -> dict[str, Any] | None:
        ref = el.get("contentReference")
        if not ref:
            return None
        return self.by_id.get(ref.lstrip("#"))

    def is_slice(self, el: dict[str, Any]) -> bool:
        return bool(el.get("sliceName"))

    def slices_of(self, el: dict[str, Any]) -> list[dict[str, Any]]:
        """Slice elements that belong to the (un-sliced) element ``el`` (siblings sharing its path)."""
        pid = parent_id_of(el["id"], self.by_id)
        siblings = self._children.get(pid, []) if pid else []
        return [s for s in siblings if s is not el and s.get("path") == el.get("path") and s.get("sliceName")]
