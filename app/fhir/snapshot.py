"""Snapshot generation for StructureDefinitions that only carry a differential.

This is a pragmatic implementation of the algorithm HAPI's ProfileUtilities uses:
start from the base snapshot, expand type children on demand, create slice
elements, and overlay each differential element.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.fhir.tree import element_id, parent_id_of

if TYPE_CHECKING:
    from app.services.profile_registry import ProfileRegistry

logger = get_logger(__name__)

# Keys that are merged rather than replaced
_APPEND_KEYS = {"constraint"}
_SKIP_KEYS = {"id", "path", "mapping"}


def _subtree_end(elements: list[dict[str, Any]], idx: int) -> int:
    """Index just past the last descendant of elements[idx]."""
    eid = elements[idx]["id"]
    j = idx + 1
    while j < len(elements):
        nid = elements[j]["id"]
        if nid.startswith(eid + ".") or nid.startswith(eid + ":"):
            # ':' only counts when it is a slice of this element's own path (same path prefix)
            j += 1
        else:
            break
    return j


def _group_end(elements: list[dict[str, Any]], idx: int) -> int:
    """End of an element, all its descendants and all its slices (and their descendants)."""
    base = elements[idx]
    path = base["path"]
    j = _subtree_end(elements, idx)
    while j < len(elements):
        el = elements[j]
        if el["path"] == path or el["path"].startswith(path + "."):
            j += 1
        else:
            break
    return j


def _find(elements: list[dict[str, Any]], eid: str) -> int:
    for i, el in enumerate(elements):
        if el["id"] == eid:
            return i
    return -1


def _merge(target: dict[str, Any], diff: dict[str, Any]) -> None:
    for k, v in diff.items():
        if k in _SKIP_KEYS:
            continue
        if k in _APPEND_KEYS:
            existing = target.setdefault(k, [])
            keys = {c.get("key") for c in existing}
            for c in v:
                if c.get("key") in keys:
                    existing[:] = [c if e.get("key") == c.get("key") else e for e in existing]
                else:
                    existing.append(copy.deepcopy(c))
            continue
        target[k] = copy.deepcopy(v)


def _expand_children(
    registry: "ProfileRegistry", elements: list[dict[str, Any]], parent_idx: int
) -> bool:
    """Insert the children of ``elements[parent_idx]`` (from its type or contentReference)."""
    parent = elements[parent_idx]
    source: list[dict[str, Any]] = []
    src_root_id = ""
    ref = parent.get("contentReference")
    if ref:
        rid = ref.lstrip("#")
        ridx = _find(elements, rid)
        if ridx < 0:
            return False
        end = _subtree_end(elements, ridx)
        source = [e for e in elements[ridx + 1 : end] if ":" not in e["id"][len(rid) :]]
        src_root_id = rid
        src_root_path = elements[ridx]["path"]
    else:
        types = parent.get("type") or []
        if not types:
            return False
        tcode = types[0].get("code")
        profiles = types[0].get("profile") or []
        tsd = registry.get_by_url(profiles[0]) if profiles else None
        if tsd is None and tcode and "/" not in tcode:
            tsd = registry.get_by_url(f"http://hl7.org/fhir/StructureDefinition/{tcode}")
        if tsd is None or not tsd.get("snapshot"):
            return False
        telems = tsd["snapshot"]["element"]
        source = telems[1:]
        src_root_id = telems[0].get("id") or telems[0]["path"]
        src_root_path = telems[0]["path"]
    new_elems = []
    for e in source:
        ne = copy.deepcopy(e)
        suffix = (e.get("id") or e["path"])[len(src_root_id) :]
        ne["id"] = parent["id"] + suffix
        ne["path"] = parent["path"] + e["path"][len(src_root_path) :]
        new_elems.append(ne)
    elements[parent_idx + 1 : parent_idx + 1] = new_elems
    return True


def _ensure_element(registry: "ProfileRegistry", elements: list[dict[str, Any]], eid: str) -> int:
    idx = _find(elements, eid)
    if idx >= 0:
        return idx

    last = eid.rsplit(".", 1)[-1] if "." in eid else eid
    # --- slice of an existing element ---------------------------------
    if ":" in last:
        unsliced_last = last.split(":", 1)[0]
        prefix = eid[: len(eid) - len(last)]
        bid = prefix + unsliced_last
        bidx = _ensure_element(registry, elements, bid)
        if bidx < 0:
            return -1
        new_el = copy.deepcopy(elements[bidx])
        new_el.pop("slicing", None)
        new_el["id"] = eid
        new_el["sliceName"] = last.split(":", 1)[1]
        # children of the base are not carried to the slice (re-expanded on demand from type)
        insert_at = _group_end(elements, bidx)
        elements.insert(insert_at, new_el)
        return insert_at

    # --- child of an element that is not yet expanded -----------------
    known = {e["id"]: e for e in elements}
    pid = parent_id_of(eid, known)
    if pid is None:
        # try to find a parent by trimming segments even if unknown yet
        parent_guess = eid.rsplit(".", 1)[0] if "." in eid else None
        if parent_guess is None:
            return -1
        pidx = _ensure_element(registry, elements, parent_guess)
    else:
        pidx = _find(elements, pid)
    if pidx < 0:
        return -1
    if _expand_children(registry, elements, pidx):
        return _find(elements, eid)
    return -1


def generate_snapshot(sd: dict[str, Any], registry: "ProfileRegistry") -> bool:
    """Populate ``sd['snapshot']`` from the differential. Returns True on success."""
    if sd.get("snapshot", {}).get("element"):
        return True
    diff = sd.get("differential", {}).get("element")
    base_url = sd.get("baseDefinition")
    if not diff or not base_url:
        return False
    base = registry.get_by_url(base_url)  # recursion generates the base's snapshot first
    if base is None or not base.get("snapshot", {}).get("element"):
        logger.warning("Cannot generate snapshot for %s: base %s unavailable", sd.get("url"), base_url)
        return False

    elements = copy.deepcopy(base["snapshot"]["element"])
    for el in elements:
        element_id(el)
        el.pop("mapping", None)

    for d in diff:
        eid = element_id(copy.copy(d)) if not d.get("id") else d["id"]
        idx = _ensure_element(registry, elements, eid)
        if idx < 0:
            logger.warning("Snapshot generation: could not place %s in %s", eid, sd.get("url"))
            continue
        _merge(elements[idx], d)

    sd["snapshot"] = {"element": elements}
    return True
