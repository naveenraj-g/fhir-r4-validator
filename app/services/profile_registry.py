import threading
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.fhir import jsonio
from app.fhir.snapshot import generate_snapshot
from app.fhir.tree import SnapshotTree

logger = get_logger(__name__)

CORE_PREFIX = "http://hl7.org/fhir/StructureDefinition/"

# ElementDefinition properties the validator never reads. Dropping them keeps RAM low
# (the R4 core definitions are ~35 MB of JSON otherwise).
_ELEMENT_DROP = {
    "mapping",
    "comment",
    "requirements",
    "alias",
    "example",
    "isSummary",
    "meaningWhenMissing",
    "orderMeaning",
    "isModifierReason",
    "short",
    "definition",
    "condition",
}
_SD_DROP = {
    "text",
    "mapping",
    "contact",
    "publisher",
    "description",
    "purpose",
    "copyright",
    "useContext",
    "jurisdiction",
}
_VS_DROP = {"text", "contact", "publisher", "copyright", "purpose"}


def slim_structure_definition(
    sd: dict[str, Any], keep_differential: bool = False
) -> dict[str, Any]:
    for k in _SD_DROP:
        sd.pop(k, None)
    snap = sd.get("snapshot")
    if snap:
        for el in snap.get("element", []):
            for k in _ELEMENT_DROP:
                el.pop(k, None)
        if not keep_differential:
            sd.pop("differential", None)
    return sd


class ProfileRegistry:
    """In-memory registry of conformance resources (StructureDefinition, ValueSet, CodeSystem)."""

    def __init__(self, data_dir: Path | None = None, load_base: bool = True) -> None:
        self.data_dir = data_dir or settings.data_dir
        self._lock = threading.RLock()
        self._sds: dict[str, dict[str, Any]] = {}
        self._sds_versioned: dict[str, dict[str, Any]] = {}
        self._value_sets: dict[str, dict[str, Any]] = {}
        self._code_systems: dict[str, dict[str, Any]] = {}
        self._others: dict[tuple[str, str], dict[str, Any]] = {}
        self._trees: dict[int, SnapshotTree] = {}
        self._resource_names: set[str] = set()
        self._core_urls: set[str] = set()
        self._resource_names.add(
            "DomainResource"
        )  # the Java validator lists the abstract DomainResource too
        self._generating: set[str] = set()
        self._generated: set[str] = (
            set()
        )  # SDs whose snapshot we generated (rebuilt when an ancestor changes)
        self.generation = 0  # bumped on every registration (cache invalidation key)
        if load_base:
            self._load_base_definitions()

    # ------------------------------------------------------------------ loading
    def _load_bundle(self, path: Path, is_core: bool = True) -> None:
        if not path.exists():
            return
        try:
            bundle = jsonio.loads(path.read_bytes())
            for entry in bundle.get("entry", []):
                res = entry.get("resource", {})
                if res:
                    self.register(res, core=is_core)
            logger.info(f"Loaded base definitions from {path.name}")
        except Exception as e:
            logger.error(f"Failed to load {path}: {e}")

    def _load_base_definitions(self) -> None:
        """Loads HL7 base FHIR R4 StructureDefinitions, ValueSets and CodeSystems."""
        # Together these are the contents of the hl7.fhir.r4.core#4.0.1 package that the Java validator loads.
        for name in (
            "profiles-resources.json",  # resource definitions
            "profiles-types.json",  # datatype definitions
            "profiles-others.json",  # core profiles (vitalsigns, bmi, ...)
            "profiles-core-extra.json",  # Base, patterns (Event/Request/...), example profiles
            "extension-definitions.json",  # core extensions
            "valuesets.json",  # core value sets + code systems
            "v2-tables.json",  # HL7 v2 tables
            "v3-codesystems.json",  # HL7 v3 code systems
        ):
            self._load_bundle(self.data_dir / name)

    # ------------------------------------------------------------------ registration
    def register(self, res: dict[str, Any], core: bool = False) -> str:
        """Registers a conformance resource. Returns its canonical URL."""
        rtype = res.get("resourceType")
        url = res.get("url")
        with self._lock:
            self.generation += 1
            if rtype == "StructureDefinition":
                if not url:
                    name = res.get("id") or res.get("name") or "unknown"
                    url = f"{CORE_PREFIX}{name}"
                    res["url"] = url
                slim_structure_definition(res)
                old = self._sds.get(url)
                if old is not None:
                    self._trees.pop(id(old), None)
                self._generated.discard(url)
                self._sds[url] = res
                self._invalidate_descendants(url)
                version = res.get("version")
                if version:
                    self._sds_versioned[f"{url}|{version}"] = res
                if core:
                    self._core_urls.add(url)
                if (
                    res.get("kind") == "resource"
                    and res.get("derivation") != "constraint"
                    and res.get("abstract") is not True
                ):
                    if res.get("type"):
                        self._resource_names.add(res["type"])
                return url
            if rtype == "ValueSet":
                if url:
                    for k in _VS_DROP:
                        res.pop(k, None)
                    self._value_sets[url] = res
                    if res.get("version"):
                        self._value_sets[f"{url}|{res['version']}"] = res
                return url or ""
            if rtype == "CodeSystem":
                if url:
                    for k in _VS_DROP:
                        res.pop(k, None)
                    self._code_systems[url] = res
                    if res.get("version"):
                        self._code_systems[f"{url}|{res['version']}"] = res
                return url or ""
            if url and rtype:
                self._others[(rtype, url)] = res
                return url
            return url or ""

    def _invalidate_descendants(self, url: str) -> None:
        """A profile changed: drop the generated snapshots of every profile derived from it."""
        for gen_url in list(self._generated):
            sd = self._sds.get(gen_url)
            if sd is None or gen_url == url:
                continue
            base, hops = sd.get("baseDefinition"), 0
            while base and hops < 12:
                base = base.split("|")[0]
                if base == url:
                    self._trees.pop(id(sd), None)
                    sd.pop("snapshot", None)
                    self._generated.discard(gen_url)
                    break
                parent = self._sds.get(base)
                base, hops = (
                    (parent.get("baseDefinition") if parent else None),
                    hops + 1,
                )

    # ------------------------------------------------------------------ lookups
    def get_by_url(self, url: str | None) -> dict[str, Any] | None:
        """StructureDefinition by canonical (optionally ``url|version``), with snapshot ensured."""
        if not url:
            return None
        sd = self._sds_versioned.get(url) or self._sds.get(url.split("|")[0])
        if sd is None:
            return None
        if "snapshot" not in sd or not sd["snapshot"].get("element"):
            with self._lock:
                key = sd.get("url", url)
                if key not in self._generating:  # guard against cyclic baseDefinition
                    self._generating.add(key)
                    try:
                        if generate_snapshot(sd, self):
                            self._generated.add(key)
                            slim_structure_definition(sd, keep_differential=True)
                        else:
                            slim_structure_definition(sd)
                    finally:
                        self._generating.discard(key)
        return sd

    def tree_for(self, sd: dict[str, Any]) -> SnapshotTree:
        tree = self._trees.get(id(sd))
        if tree is None:
            with self._lock:
                tree = self._trees.get(id(sd))
                if tree is None:
                    tree = SnapshotTree(sd)
                    self._trees[id(sd)] = tree
        return tree

    def type_sd(self, type_code: str | None) -> dict[str, Any] | None:
        if not type_code:
            return None
        if type_code.startswith("http://") or type_code.startswith("https://"):
            return self.get_by_url(type_code)
        return self.get_by_url(CORE_PREFIX + type_code)

    def get_value_set(self, url: str | None) -> dict[str, Any] | None:
        if not url:
            return None
        return self._value_sets.get(url) or self._value_sets.get(url.split("|")[0])

    def get_code_system(self, url: str | None) -> dict[str, Any] | None:
        if not url:
            return None
        return self._code_systems.get(url) or self._code_systems.get(url.split("|")[0])

    def code_system_available(self, url: str | None) -> bool:
        """A CodeSystem whose concepts we actually hold (``content: not-present`` stubs do not count)."""
        cs = self.get_code_system(url)
        return cs is not None and cs.get("content") != "not-present"

    def is_primitive(self, type_code: str) -> bool:
        if type_code.startswith("http://hl7.org/fhirpath/System."):
            return True
        sd = self._sds.get(CORE_PREFIX + type_code)
        return bool(sd and sd.get("kind") == "primitive-type")

    def is_resource_type(self, name: str) -> bool:
        return name in self._resource_names

    # ------------------------------------------------------------------ listings
    def get_structures(self) -> list[str]:
        """Sorted, distinct list of all StructureDefinition canonical URLs."""
        return sorted(self._sds.keys())

    def get_core_structures(self) -> list[str]:
        return sorted(self._core_urls)

    def get_resources(self) -> list[str]:
        """Sorted, distinct list of all known FHIR resource names."""
        return sorted(self._resource_names)
