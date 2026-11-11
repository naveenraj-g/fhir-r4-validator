"""FHIR R4 instance validator (pure Python).

Two kinds of pass are made over every resource:

* **base pass** (``overlay=False``): validates the instance against the core
  definitions of its type(s): unknown properties, JSON shapes, primitive formats,
  cardinality, choice types, nested structures, invariants, bindings, references.
* **profile pass** (``overlay=True``): re-walks the instance with a profile's
  snapshot and applies only what the profile *adds*: narrowed cardinality, fixed /
  pattern values, slicing, stricter bindings, extra invariants, extension and type
  profiles.  Structure was already checked by the base pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.fhir import xhtml
from app.fhir.fhirpath import FhirPathError, FhirPathRunner
from app.fhir.model import Ctx, Navigator, type_code
from app.fhir.tree import SnapshotTree
from app.fhir.vitals import LOINC, VITAL_SIGN_PROFILES

if TYPE_CHECKING:
    from app.fhir.terminology import TerminologyService
    from app.services.profile_registry import ProfileRegistry

logger = get_logger(__name__)

RESOURCE_TYPES = ("Resource", "DomainResource")
_SIMPLE_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_REL_REF = re.compile(r"(?:^|/)([A-Z][A-Za-z]+)/[A-Za-z0-9\-.]{1,64}(?:/_history/[A-Za-z0-9\-.]{1,64})?$")
_INT_TYPES = {"integer", "positiveInt", "unsignedInt", "integer64"}
_EXAMPLE_URL = re.compile(r"^https?://[^/]*(example\.org|acme\.org|acme\.com)", re.I)
_OID_ARCS = re.compile(r"[0-2](\.(0|[1-9][0-9]*))+")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_RESOURCE_ID_PATH = re.compile(r"^[A-Za-z]+\.id$")
_URI_TYPES = {"uri", "url", "canonical"}
# Without a terminology server HAPI says "running without terminology services" for these...
_TX_ONLY_SYSTEMS = {
    "http://loinc.org", "http://snomed.info/sct", "http://unitsofmeasure.org",
    "http://www.nlm.nih.gov/research/umls/rxnorm",
}
# ...and stays silent about these
_SILENT_SYSTEMS = {
    "http://hl7.org/fhir/sid/icd-10", "http://hl7.org/fhir/sid/icd-10-cm", "http://hl7.org/fhir/sid/icd-9-cm",
    "http://hl7.org/fhir/sid/ndc", "http://hl7.org/fhir/sid/cvx",
}
# Extensions defined for one conformance (definitional) resource are accepted on the others (as HAPI does)
_CONFORMANCE_RESOURCES = {
    "StructureDefinition", "ValueSet", "CodeSystem", "OperationDefinition", "SearchParameter", "CapabilityStatement",
    "ConceptMap", "NamingSystem", "ImplementationGuide", "StructureMap", "GraphDefinition", "CompartmentDefinition",
    "MessageDefinition", "ExampleScenario", "TerminologyCapabilities",
}
_QUANTITY_TYPES = {"Quantity", "SimpleQuantity", "Age", "Distance", "Duration", "Count", "MoneyQuantity"}
_STRING_TYPES_WITH_BINDING = {"code", "string", "uri", "url", "canonical", "id"}


@dataclass
class Issue:
    severity: str  # fatal | error | warning | information
    code: str
    message: str
    path: str
    key: tuple
    source: str = "InstanceValidator"
    line: int = 1
    col: int = 1
    message_id: str | None = None


class _Run:
    def __init__(self, root: dict[str, Any], positions: Any) -> None:
        self.root = root
        self.positions = positions
        self.issues: list[Issue] = []
        self._seen: set[tuple] = set()
        self.memo: set[tuple] = set()
        self.res_stack: list[dict[str, Any]] = []
        self.container_stack: list[dict[str, Any]] = []  # nearest non-contained resource
        self.sd_stack: list[str] = []  # canonical of the profile/base definition of the current pass
        self.host: dict[str, Any] | None = None  # object that owns the property being validated
        self.host_def: str = ""  # definition path of that object, e.g. Patient.name or Questionnaire.item.item
        self.host_prim: str | None = None  # type code when the owner is a primitive (extension on _x)
        self.entry_without_fullurl = False  # inside a document/message Bundle.entry that lacks fullUrl
        self.bundles: list[dict[str, Any]] = []
        self.warned_systems: set[str] = set()
        self.bad_fp: set[str] = set()

    def add(self, severity: str, code: str, message: str, path: str, key: tuple, message_id: str | None = None) -> None:
        ident = (severity, code, path, message)
        if ident in self._seen:
            return
        self._seen.add(ident)
        line, col = (1, 1)
        if self.positions is not None:
            try:
                line, col = self.positions.get(key)
            except Exception:
                pass
        self.issues.append(Issue(severity, code, message, path, key, line=line, col=col, message_id=message_id))


def _num(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    return v


def _equal(a: Any, b: Any) -> bool:
    a, b = _num(a), _num(b)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


def _subset(pattern: Any, actual: Any) -> bool:
    """Does ``actual`` contain everything specified by ``pattern``?"""
    if isinstance(pattern, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in pattern.items())
    if isinstance(pattern, list):
        if not isinstance(actual, list):
            return False
        return all(any(_subset(p, a) for a in actual) for p in pattern)
    return _equal(pattern, actual)


def _short(v: Any) -> str:
    from app.fhir import jsonio

    s = v if isinstance(v, str) else jsonio.dumps(v)
    return s if len(s) < 120 else s[:117] + "..."


def _json_kind(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float, Decimal)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "null"


class InstanceValidator:
    def __init__(
        self,
        registry: "ProfileRegistry",
        terminology: "TerminologyService",
        fhirpath: FhirPathRunner,
        display_issues_are_warnings: bool = False,
    ) -> None:
        self.registry = registry
        self.terminology = terminology
        self.fp = fhirpath
        self.nav = Navigator(registry)
        self.display_sev = "warning" if display_issues_are_warnings else "error"
        self._regex_cache: dict[str, re.Pattern[str] | None] = {}

    # =================================================================== entry points
    def validate(self, resource: dict[str, Any], profiles: list[str], positions: Any = None) -> list[Issue]:
        run = _Run(resource, positions)
        self._validate_resource(resource, run, str(resource.get("resourceType", "")), (), profiles)
        return run.issues

    # =================================================================== resources
    def _validate_resource(
        self, res: dict[str, Any], run: _Run, path: str, key: tuple, explicit_profiles: list[str],
        contained: bool = False,
    ) -> None:
        rtype = res.get("resourceType")
        if not isinstance(rtype, str) or not rtype:
            run.add("fatal", "structure", "Unable to find resourceType property", path or "(root)", key)
            return
        base_ctx = self.nav.resource_ctx(rtype) if self.registry.is_resource_type(rtype) else None
        if base_ctx is None:
            run.add("fatal", "structure", f"This content cannot be parsed (unknown or unrecognized resource name '{rtype}')", path or rtype, key)
            return

        run.res_stack.append(res)
        run.container_stack.append(run.container_stack[-1] if contained and run.container_stack else res)
        if rtype == "Bundle":
            run.bundles.append(res)
        try:
            run.sd_stack.append(base_ctx.tree.sd.get("url", ""))
            try:
                self._struct(res, base_ctx, run, path, key, overlay=False, is_resource=True)
            finally:
                run.sd_stack.pop()
            self._check_contained_referenced(res, run, path, key)
            if rtype == "Observation":
                self._observation_best_practice(res, run, path, key)
            self._language_check(res, run, path, key)
            if rtype == "Bundle":
                self._bundle_rules(res, run, path, key)
            elif rtype == "Measure":
                self._measure_rules(res, run, path, key)

            profiles = list(explicit_profiles)
            meta = res.get("meta")
            declared: list[str] = []
            if isinstance(meta, dict) and isinstance(meta.get("profile"), list):
                declared = [p for p in meta["profile"] if isinstance(p, str)]
            for p in declared:
                if p not in profiles:
                    profiles.append(p)
            if rtype == "Observation":
                for url in self._vital_sign_profiles(res, run, path, key):
                    if url not in profiles:
                        profiles.append(url)
                        declared.append(url)  # not an explicit request: a missing definition is only a warning
            profiles = self._order_descendants_first(profiles)
            for url in profiles:
                sd = self.registry.get_by_url(url)
                if sd is None or not sd.get("snapshot"):
                    if url in explicit_profiles:
                        run.add(
                            "error", "not-found",
                            f"Unable to locate the profile '{url}' in order to validate against it",
                            path, key,
                        )
                    else:
                        run.add(
                            "warning", "not-found",
                            f"Profile reference '{url}' has not been checked because it could not be found",
                            f"{path}.meta.profile", key + ("meta", "profile"),
                        )
                    continue
                self._apply_profile(res, sd, run, path, key)
        finally:
            if rtype == "Bundle":
                run.bundles.pop()
            run.container_stack.pop()
            run.res_stack.pop()

    def _bundle_rules(self, bundle: dict[str, Any], run: _Run, path: str, key: tuple) -> None:
        entries = bundle.get("entry")
        if not isinstance(entries, list):
            return
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                continue
            res = e.get("resource") if isinstance(e.get("resource"), dict) else None
            full = e.get("fullUrl")
            epath, ekey = f"{path}.entry[{i}]", key + ("entry", i)
            if isinstance(full, str) and res is not None and res.get("id"):
                m = re.search(r"/([A-Z][A-Za-z]+)/([A-Za-z0-9\-.]{1,64})$", full)
                if m and (m.group(1) != res.get("resourceType") or m.group(2) != res.get("id")):
                    run.add("error", "invalid",
                            f"The fullUrl '{full}' looks like a RESTful server URL, so it must end with the correct type and id "
                            f"(/{res.get('resourceType')}/{res.get('id')})", epath, ekey)
            if isinstance(full, str) and full and ":" not in full:
                run.add("error", "invalid", f"The fullUrl must be an absolute URL (not '{full}')", epath, ekey)
            if not full and bundle.get("type") in ("document", "message"):
                run.add("error", "required", "Bundle entry missing fullUrl", epath, ekey)

    def _measure_rules(self, m: dict[str, Any], run: _Run, path: str, key: tuple) -> None:
        for gi, g in enumerate(m.get("group") or []):
            if not isinstance(g, dict):
                continue
            if not g.get("population"):
                run.add("warning", "required", "Measure Groups should have at least one population", f"{path}.group[{gi}]", key + ("group", gi))
            for si, st in enumerate(g.get("stratifier") or []):
                crit = st.get("criteria") if isinstance(st, dict) else None
                if isinstance(crit, dict) and str(crit.get("language", "")).startswith("text/cql"):
                    run.add("error", "invalid", "No CQL Libraries found on this Measure",
                            f"{path}.group[{gi}].stratifier[{si}].criteria", key + ("group", gi, "stratifier", si, "criteria"))

    def _order_descendants_first(self, urls: list[str]) -> list[str]:
        """Most specific profile first: its invariant messages (naming their source) win the de-duplication."""
        def depth(u: str) -> int:
            d, sd = 0, self.registry.get_by_url(u)
            while sd is not None and d < 12 and sd.get("baseDefinition"):
                d, sd = d + 1, self.registry.get_by_url(sd["baseDefinition"])
            return d

        return sorted(urls, key=lambda u: -depth(u))

    def _vital_sign_profiles(self, res: dict[str, Any], run: _Run, path: str, key: tuple) -> list[str]:
        """Profiles HAPI applies on its own because of a vital-sign LOINC code (reported as information)."""
        code = res.get("code")
        out: list[str] = []
        if not isinstance(code, dict):
            return out
        for c in code.get("coding") or []:
            if isinstance(c, dict) and c.get("system") == LOINC and c.get("code") in VITAL_SIGN_PROFILES:
                name, url = VITAL_SIGN_PROFILES[c["code"]]
                if url not in out:
                    out.append(url)
                    run.add(
                        "information", "informational",
                        f"Validate Observation against the {name} profile ({url}) which is required by the FHIR "
                        f"specification because the LOINC code {c['code']} was found",
                        path, key,
                    )
        return out

    def _language_check(self, res: dict[str, Any], run: _Run, path: str, key: tuple) -> None:
        div = (res.get("text") or {}).get("div") if isinstance(res.get("text"), dict) else None
        if res.get("language") and isinstance(div, str):
            head = div[: div.find(">") + 1] if ">" in div else div
            if not (re.search(r"(?<![:\w])lang\s*=", head) and "xml:lang" in head):
                run.add(
                    "warning", "business-rule",
                    "Resource has a language, but the XHTML does not have an lang or an xml:lang tag "
                    "(needs both - see https://www.w3.org/TR/i18n-html-tech-lang/#langvalues)",
                    path, key,
                )

    def _observation_best_practice(self, res: dict[str, Any], run: _Run, path: str, key: tuple) -> None:
        """HAPI's hard-coded best-practice warnings for Observation."""
        bp = "Best Practice Recommendation: In general, all observations should have "
        if not res.get("performer"):
            run.add("warning", "invalid", bp + "a performer", path, key)
        if not res.get("subject"):
            run.add("warning", "invalid", bp + "a subject", path, key)
        if not any(k.startswith("effective") and len(k) > 9 and k[9].isupper() for k in res):
            run.add("warning", "invalid", bp + "an effective[x] ()", path, key)
        m = re.match(r"^(.*\.resource)/\*Observation/[^*]+\*/$", path)
        if m:  # Bundle.entry.resource is visited twice by HAPI
            for cond, text in ((res.get("performer"), "a performer"), (res.get("subject"), "a subject"),
                               (any(k.startswith("effective") and len(k) > 9 and k[9].isupper() for k in res), "an effective[x] ()")):
                if not cond:
                    run.add("warning", "invalid", bp + text, m.group(1), key)

    def _apply_profile(self, res: dict[str, Any], sd: dict[str, Any], run: _Run, path: str, key: tuple) -> None:
        rtype = res.get("resourceType")
        if sd.get("type") != rtype:
            run.add(
                "error", "invalid",
                f"Specified profile type was '{sd.get('type')}', but found type '{rtype}'",
                path, key,
            )
            return
        memo = ("profile", key, sd.get("url"))
        if memo in run.memo:
            return
        run.memo.add(memo)
        tree = self.registry.tree_for(sd)
        if tree.root is None:
            return
        run.sd_stack.append(sd.get("url", ""))
        try:
            self._struct(res, Ctx(tree, tree.root), run, path, key, overlay=True, is_resource=True)
        finally:
            run.sd_stack.pop()

    def _check_contained_referenced(self, res: dict[str, Any], run: _Run, path: str, key: tuple) -> None:
        """dom-3: every contained resource must be referenced from elsewhere in the resource."""
        contained = res.get("contained")
        if not isinstance(contained, list) or not contained:
            return
        refs: set[str] = set()
        refers_to_container = False

        def walk(node: Any) -> None:
            nonlocal refers_to_container
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str):
                        refs.add(v)
                        if k == "reference" and v == "#":
                            refers_to_container = True
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    if isinstance(v, str):
                        refs.add(v)
                    else:
                        walk(v)

        walk({k: v for k, v in res.items()})
        for i, c in enumerate(contained):
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if cid and f"#{cid}" in refs:
                continue
            if refers_to_container:
                continue
            run.add(
                "error", "invalid",
                f"The contained resource '{cid}' is not referenced to from elsewhere in the containing resource "
                "nor does it refer to the containing resource (dom-3)",
                f"{path}.contained[{i}]", key + ("contained", i),
            )

    # =================================================================== structure
    def _struct(
        self,
        obj: dict[str, Any],
        ctx: Ctx,
        run: _Run,
        path: str,
        key: tuple,
        overlay: bool,
        is_resource: bool = False,
        host_def: str | None = None,
        host_prim: str | None = None,
    ) -> None:
        nav = self.nav
        matched: dict[str, list[tuple[str, str | None]]] = {}
        for prop in obj:
            if is_resource and prop == "resourceType":
                continue
            base = prop[1:] if prop.startswith("_") and len(prop) > 1 else prop
            found = nav.find_child(ctx, base)
            if found is None:
                if not overlay:
                    run.add("error", "structure", f"Unrecognized property '{prop}'", path, key + (prop,))
                continue
            ch, code = found
            lst = matched.setdefault(ch.el["id"], [])
            if any(b == base for b, _ in lst):
                continue
            if ch.is_choice and lst:
                # a second type for the same choice element is "unrecognized" (HAPI behaviour)
                if not overlay:
                    run.add("error", "structure", f"Unrecognized property '{prop}'", path, key + (prop,))
                continue
            lst.append((base, code))

        for ch in nav.children(ctx):
            run.host, run.host_def, run.host_prim = obj, host_def or ctx.el["path"], host_prim
            self._check_child(obj, ch, matched.get(ch.el["id"], []), ctx, run, path, key, overlay)

        # constraints defined on the structure root (e.g. dom-*, ext-1, ref-1, qty-3)
        self._constraints(obj, ctx.el, ctx.el["path"], run, path, key, ctx.tree)

    def _check_child(
        self,
        obj: dict[str, Any],
        ch: Any,
        entries: list[tuple[str, str | None]],
        ctx: Ctx,
        run: _Run,
        path: str,
        key: tuple,
        overlay: bool,
    ) -> None:
        el = ch.el
        tree = ctx.tree
        hs = (run.host, run.host_def, run.host_prim)

        # gather items (value, ext, code, display path, key) across matching properties
        items: list[tuple[Any, Any, str | None, str, tuple]] = []
        for base, code in entries:
            disp = f"{ch.name}.ofType({code})" if ch.is_choice and code else base
            values = obj.get(base)
            exts = obj.get("_" + base)
            if ch.many:
                if values is not None and not isinstance(values, list):
                    if not overlay:
                        what = "an Object" if isinstance(values, dict) else "a primitive property"
                        run.add("error", "invalid", f"The property {base} must be a JSON Array, not {what} (at {path})", f"{path}.{base}", key + (base,))
                    values = [values]
                if exts is not None and not isinstance(exts, list):
                    exts = [exts]
                n = max(len(values or []), len(exts or []))
                if base in obj and values is None and exts is None:
                    n = 1  # explicit JSON null instead of an array
                for i in range(n):
                    v = values[i] if values and i < len(values) else None
                    e = exts[i] if exts and i < len(exts) else None
                    items.append((v, e, code, f"{path}.{disp}[{i}]", key + (base, i)))
            else:
                if isinstance(values, list):
                    if not overlay:
                        run.add("error", "invalid", f"The property {base} must not be a JSON Array (at {path})", f"{path}.{base}", key + (base,))
                    for i, v in enumerate(values):
                        items.append((v, None, code, f"{path}.{disp}[{i}]", key + (base, i)))
                elif base in obj or ("_" + base) in obj:
                    items.append((values, exts, code, f"{path}.{disp}", key + (base,)))

        # cardinality
        count = len(items)
        mn = ch.min
        mx = ch.max
        label = el["path"]
        frm = self._from(tree)
        if count < mn:
            run.add("error", "structure", f"{label}: minimum required = {mn}, but only found {count}{frm}", path, key)
        if mx != "*" and mx.isdigit() and count > int(mx):
            run.add("error", "structure", f"{label}: max allowed = {mx}, but found {count}{frm}", items[int(mx)][3] if int(mx) < count else path, items[int(mx)][4] if int(mx) < count else key)

        if not items:
            slices = tree.slices_of(el)
            for s in slices:
                self._slice_cardinality(s, 0, path, key, tree, run, (el.get('slicing') or {}).get('rules', 'open'))
            return

        slices = tree.slices_of(el)
        slicing = el.get("slicing")
        if slices:
            groups: dict[str, list[tuple]] = {s["id"]: [] for s in slices}
            unmatched: list[tuple] = []
            for it in items:
                target = None
                if isinstance(it[0], (dict, str, int, bool, Decimal)):
                    for s in slices:
                        if self._match_slice(it[0], it[2], s, slicing or {}, tree, el, run):
                            target = s
                            break
                if target is None:
                    unmatched.append(it)
                else:
                    groups[target["id"]].append(it)
            for s in slices:
                g = groups[s["id"]]
                self._slice_cardinality(s, len(g), path, key, tree, run, (slicing or {}).get('rules', 'open'))
                for it in g:
                    self._validate_value_h(hs, it[0], it[1], s, it[2], ctx, it[3], it[4], run, overlay)
            for it in unmatched:
                rules = (slicing or {}).get("rules", "open")
                if rules == "closed":
                    run.add(
                        "error", "invalid",
                        f"This element does not match any known slice defined in the profile {tree.sd.get('url')}{self._ver(tree)} and slicing is CLOSED",
                        it[3], it[4],
                    )
                self._validate_value_h(hs, it[0], it[1], el, it[2], ctx, it[3], it[4], run, overlay)
            return

        for v, e, code, p, k in items:
            self._validate_value_h(hs, v, e, el, code, ctx, p, k, run, overlay)

    def _slice_cardinality(self, s: dict[str, Any], count: int, path: str, key: tuple, tree: SnapshotTree, run: _Run, rules: str = "open") -> None:
        mn = int(s.get("min", 0) or 0)
        mx = str(s.get("max", "*"))
        label = f"{s['path']}:{s.get('sliceName')}"
        if count < mn:
            magic = None
            for k in ("patternCoding", "fixedCoding"):
                v = s.get(k)
                if isinstance(v, dict) and v.get("system") == "http://loinc.org" and v.get("code"):
                    magic = v["code"]
            if magic is None:
                kids = tree.children(s)
                sys_ok = any(c["path"].endswith(".system") and (c.get("fixedUri") or c.get("patternUri")) == "http://loinc.org" for c in kids)
                code_el = next((c for c in kids if c["path"].endswith(".code") and (c.get("fixedCode") or c.get("patternCode"))), None)
                if sys_ok and code_el is not None:
                    magic = code_el.get("fixedCode") or code_el.get("patternCode")
            if magic:
                run.add("error", "structure",
                        f"{s.get('sliceName')}: magic LOINC code {magic} required, but not found{self._from(tree)}. "
                        "Note that other Observation codes are allowed in addition to this required magic code", path, key)
                return
            note = "" if rules == "closed" else ". Note that other slices are allowed in addition to this required slice"
            run.add(
                "error", "structure",
                f"Slice '{label}': a matching slice is required, but not found{self._from(tree)}{note}",
                path, key,
            )
        if mx != "*" and mx.isdigit() and count > int(mx):
            run.add("error", "structure", f"{label}: max allowed = {mx}, but found {count}{self._from(tree)}", path, key)

    @staticmethod
    def _ver(tree: SnapshotTree) -> str:
        v = tree.sd.get("version")
        return f"|{v}" if v else ""

    def _from(self, tree: SnapshotTree) -> str:
        sd = tree.sd
        return f" (from {sd.get('url')}{self._ver(tree)})"

    def _validate_value_h(self, hs: tuple, *args: Any) -> None:
        run = args[7]
        run.host, run.host_def, run.host_prim = hs
        self._validate_value(*args)

    def _validate_value(
        self,
        value: Any,
        ext: Any,
        el: dict[str, Any],
        code: str | None,
        ctx: Ctx,
        path: str,
        key: tuple,
        run: _Run,
        overlay: bool,
    ) -> None:
        nav = self.nav
        host_state = (run.host, run.host_def, run.host_prim)
        types = el.get("type", []) or []
        if code is None and types:
            if len(types) == 1:
                code = type_code(types[0])
            elif isinstance(value, dict) and "resourceType" in value:
                code = "Resource"
            else:
                code = None
        if code is None and not types and el.get("contentReference"):
            code = "BackboneElement"
        is_choice_error = code is None and len(types) > 1
        if is_choice_error and not overlay:
            allowed = ", ".join(sorted({type_code(t) for t in types}))
            run.add("error", "structure", f"The type of this element is not permitted here (allowed: {allowed})", path, key)

        is_primitive = code is not None and nav.is_primitive(code)
        tree = ctx.tree

        # The parent's element definition carries the constraints (with their `source`) that HAPI reports,
        # so evaluate those before descending into the type's own root constraints.
        if value is not None:
            self._constraints(value, el, el["path"] if code in (None, "BackboneElement", "Element") else code, run, path, key, tree)

        if is_primitive:
            if value is None:
                if ext is None:
                    if not overlay:
                        name = path.rsplit(".", 1)[-1].split("[")[0]
                        run.add("error", "invalid", f"This property must be a simple value, not a Null ({name} at {path})", path, key)
            else:
                is_xhtml = code == "xhtml" or self._is_xhtml(types)
                if not overlay:
                    if is_xhtml:
                        self._check_xhtml(value, run, path, key)
                    else:
                        self._check_primitive(value, code, run, path, key, el)
            if isinstance(ext, dict) and not overlay:
                ectx = nav.type_ctx("Element")
                if ectx is not None:
                    self._struct(ext, ectx, run, path, key[:-1] + ("_" + str(key[-1]),) if key and isinstance(key[-1], str) else key,
                                 overlay=False, host_def=el["path"], host_prim=code)
            elif ext is not None and not isinstance(ext, dict) and not overlay:
                run.add("error", "structure", "This property must be an Object, not a primitive", path, key)
        else:
            if value is None:
                if not overlay:
                    name = path.rsplit(".", 1)[-1].split("[")[0]
                    run.add("error", "invalid", f"This property must be an Object, not a Null ({name} at {path})", path, key)
                return
            if not isinstance(value, dict):
                if not overlay:
                    run.add("error", "invalid", f"This property must be an Object, not {_a(_json_kind(value))}", path, key)
                return
            if not value:
                if not overlay:
                    run.add("error", "invalid", "Object must have some content", path, key)
                return

            if code in RESOURCE_TYPES:
                rt = value.get("resourceType", "")
                rid = value.get("id")
                npath = f"{path}/*{rt}/{rid if isinstance(rid, str) and rid else 'null'}*/" if rt else path
                if not overlay:
                    prev = run.entry_without_fullurl
                    if el.get("path") == "Bundle.entry.resource" and run.bundles and len(key) >= 2 and isinstance(key[-2], int):
                        try:
                            ent = run.bundles[-1]["entry"][key[-2]]
                            run.entry_without_fullurl = not ent.get("fullUrl") and run.bundles[-1].get("type") in ("document", "message")
                        except (KeyError, IndexError, TypeError):
                            pass
                    try:
                        self._validate_resource(value, run, npath, key, [], contained=el.get("path", "").endswith(".contained"))
                    finally:
                        run.entry_without_fullurl = prev
                else:
                    for t in types:
                        for purl in t.get("profile", []) or []:
                            psd = self.registry.get_by_url(purl)
                            if psd is not None and psd.get("snapshot"):
                                self._apply_profile(value, psd, run, npath, key)
            elif code is not None:
                if not overlay:
                    sub = nav.descend(ctx, el, code)
                    if sub is not None:
                        self._struct(value, sub, run, path, key, overlay=False, host_def=el["path"])
                else:
                    if tree.children(el) or el.get("contentReference"):
                        sub = nav.descend(ctx, el, code if code in ("BackboneElement", "Element") else None)
                        if sub is None and tree.children(el):
                            sub = Ctx(tree, el)
                        if sub is not None:
                            self._struct(value, sub, run, path, key, overlay=True, host_def=el["path"])
                    # type profiles, e.g. extension slices -> us-core-race
                    if len(types) == 1:
                        for purl in types[0].get("profile", []) or []:
                            self._apply_type_profile(value, purl, run, path, key)
                if code == "Attachment" and not (value.get("data") or value.get("url") or value.get("contentType") or value.get("language")):
                    run.add("warning", "structure", "Attachments have data and/or url, or else SHOULD have either contentType and/or language", path, key)
                if code in _QUANTITY_TYPES:
                    q_sys, q_code = value.get("system"), value.get("code")
                    if isinstance(q_sys, str) and isinstance(q_code, str):
                        if q_sys in _TX_ONLY_SYSTEMS:
                            if code in ("Quantity", "SimpleQuantity") and self.terminology.disabled:
                                run.add("warning", "business-rule",
                                        f"Unable to validate code '{q_code}' in system '{q_sys}' because the validator is running without terminology services",
                                        path, key)
                        elif self.registry.code_system_available(q_sys):
                            qres = self.terminology.validate_in_system(q_sys, q_code)
                            if qres.status == "invalid":
                                run.add("error", "code-invalid", qres.message or f"Unknown code '{q_code}' in the CodeSystem '{q_sys}'", f"{path}.code", key + ("code",))
                if code == "Reference":
                    self._check_reference(value, types, run, path, key)
                elif code == "Extension":
                    self._check_extension(value, run, path, key, ctx, host_state)

        if code == "Identifier" and isinstance(value, dict) and value.get("system") == "urn:ietf:rfc:3986":
            v = value.get("value")
            if isinstance(v, str) and not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", v):
                run.add("error", "code-invalid",
                        f"if identifier.system is 'urn:ietf:rfc:3986', then the identifier.value must be a full URI (e.g. start with a scheme), not '{v}'",
                        path, key)
        # fixed / pattern
        self._check_fixed_pattern(value, el, run, path, key)
        # binding
        if value is not None and ("binding" in el or code in ("Coding", "CodeableConcept")):
            self._check_coded(value, code, el, run, path, key)


    @staticmethod
    def _is_xhtml(types: list[dict[str, Any]]) -> bool:
        for t in types:
            for ext in (t.get("_code", {}).get("extension", []) or []):
                if ext.get("url", "").endswith("elementdefinition-representation") and ext.get("valueCode") == "xhtml":
                    return True
        return False

    def _apply_type_profile(self, value: dict[str, Any], purl: str, run: _Run, path: str, key: tuple) -> None:
        psd = self.registry.get_by_url(purl)
        if psd is None or not psd.get("snapshot"):
            return
        memo = ("tp", key, psd.get("url"))
        if memo in run.memo:
            return
        run.memo.add(memo)
        tree = self.registry.tree_for(psd)
        if tree.root is not None:
            self._struct(value, Ctx(tree, tree.root), run, path, key, overlay=True)

    def _host_ids(self, ctx: Ctx, run: _Run) -> list[str]:
        """HAPI's description of the element an extension sits on, e.g. [HumanName, Patient.name]."""
        d = run.host_def or ctx.el["path"]
        if run.host_prim:
            return [d, run.host_prim]
        root = ctx.el["path"]
        if "." not in d:
            return [d]
        if "." not in root:
            return [root, d]
        return ["BackboneElement", d] + ([root] if root != d else [])

    def _context_allows(self, contexts: list[dict[str, Any]], host_ids: list[str], url: str, ext: dict[str, Any],
                        host: Any, host_base: str, run: _Run) -> tuple[bool, str | None]:
        """(allowed, failing fhirpath invariant)."""
        failing: str | None = None
        for c in contexts:
            ctype, expr = c.get("type"), c.get("expression", "")
            if ctype == "element":
                if expr in ("Element", "Base") or expr in host_ids:
                    return True, None
                if expr in _CONFORMANCE_RESOURCES and host_ids and host_ids[0] in _CONFORMANCE_RESOURCES:
                    return True, None
                if expr in ("Resource", "DomainResource") and host_ids and self.registry.is_resource_type(host_ids[0]):
                    return True, None
                if expr == "BackboneElement" and host_ids and host_ids[0] == "BackboneElement":
                    return True, None
            elif ctype == "fhirpath":
                try:
                    variables = {"extension": ext, "resource": run.res_stack[-1] if run.res_stack else None, "rootResource": run.root}
                    if self.fp.evaluate_boolean(expr, host, host_base, variables, self._resolver(run), empty_is=False):
                        return True, None
                    failing = expr
                except FhirPathError:
                    return True, None
            else:  # 'extension' contexts: nested extensions are resolved against their parent definition
                return True, None
        return False, failing

    def _check_extension(self, value: dict[str, Any], run: _Run, path: str, key: tuple,
                         ctx: Ctx | None = None, host_state: tuple | None = None) -> None:
        host = host_state[0] if host_state else None
        url = value.get("url")
        if not isinstance(url, str) or not url:
            run.add("error", "invalid", "Extension.url is required in order to identify, use and validate the extension", path, key)
            return
        sd = self.registry.get_by_url(url)
        if sd is None or sd.get("type") != "Extension" or not sd.get("snapshot"):
            if "://" in url or url.startswith("urn:"):
                run.add("information", "structure", f"Unknown extension {url}", path, key)
            return
        self._apply_type_profile(value, url, run, path, key)
        # context (the type-annotation extensions are used on whatever element carries a type)
        contexts = [] if re.search(r"structuredefinition-(fhir|json|xml|rdf)-type$|structuredefinition-normative-version$|/regex$", url) else (sd.get("context") or [])
        if contexts and ctx is not None and host is not None:
            run.host, run.host_def, run.host_prim = host_state  # type: ignore[misc]
            host_ids = self._host_ids(ctx, run)
            host_path = re.sub(r"\.(extension|modifierExtension)(\[\d+\])?$", "", path)
            base = ctx.el["path"]
            ok, failing = self._context_allows(contexts, host_ids, url, value, host, base, run)
            if not ok:
                parent_path, parent_key = host_path, key[:-2] if len(key) >= 2 and isinstance(key[-1], int) else key[:-1]
                if failing:
                    detail = f"based on context invariant '{failing}'"
                else:
                    allowed = ", ".join(f"e:{c.get('expression')}" for c in contexts if c.get("type") == "element")
                    detail = f"allowed = {allowed}; this element is [{', '.join(host_ids)}]"
                run.add("error", "structure", f"The extension {url} is not allowed to be used at this point ({detail})", parent_path, parent_key)
        # context invariants (R4 StructureDefinition.contextInvariant)
        if contexts and ctx is not None and host_state is not None and host_state[0] is not None:
            for inv in sd.get("contextInvariant") or []:
                try:
                    # HAPI does not bind %extension while checking contextInvariant, so it evaluates to empty
                    variables = {"extension": [], "resource": run.res_stack[-1] if run.res_stack else None, "rootResource": run.root}
                    ok_inv = self.fp.evaluate_boolean(inv, host_state[0], ctx.el["path"], variables, self._resolver(run), empty_is=False)
                except FhirPathError:
                    ok_inv = True
                if not ok_inv:
                    hp = re.sub(r"\.(extension|modifierExtension)(\[\d+\])?$", "", path)
                    hk = key[:-2] if len(key) >= 2 and isinstance(key[-1], int) else key[:-1]
                    run.add("error", "structure", f"The extension {url} is not allowed to be used at this point (based on context invariant '{inv}')", hp, hk)
        # value type
        tree = self.registry.tree_for(sd)
        vel = next((e for e in tree.elements if e["path"] == "Extension.value[x]"), None)
        prop = next((k for k in value if k.startswith("value") and len(k) > 5 and k[5].isupper()), None)
        if vel is not None and prop is not None:
            allowed_types = [type_code(t) for t in vel.get("type", [])]
            suffix = prop[5:]
            if allowed_types and not any(t[:1].upper() + t[1:] == suffix for t in allowed_types):
                found = next((t for t in [suffix[:1].lower() + suffix[1:]] if self.nav.is_primitive(t)), suffix)
                run.add("error", "structure",
                        f"The Extension '{url}' definition allows for the types [{', '.join(allowed_types)}] but found type {found}",
                        path, key)

    def _regex_for(self, code: str) -> re.Pattern[str] | None:
        if code in self._regex_cache:
            return self._regex_cache[code]
        pat: re.Pattern[str] | None = None
        sd = self.registry.type_sd(code)
        if sd is not None:
            for el in sd.get("snapshot", {}).get("element", []):
                if el.get("path") == f"{code}.value":
                    for t in el.get("type", []):
                        for ext in t.get("extension", []) or []:
                            if ext.get("url", "").endswith("/regex"):
                                try:
                                    pat = re.compile(ext.get("valueString", ""), re.ASCII)
                                except re.error:
                                    pat = None
        self._regex_cache[code] = pat
        return pat

    def _check_primitive(self, value: Any, code: str, run: _Run, path: str, key: tuple, el: dict[str, Any] | None = None) -> None:
        kind = _json_kind(value)
        # Java reports choice-typed primitives at `Parent.name[x]` (JSON shape) / `Parent.name.ofType(type)` (value)
        shape_path = value_path = path
        m_of = re.match(r"^(.*)\.ofType\([^)]*\)(\[\d+\])?$", path)
        if m_of and el is not None and el.get("path", "").endswith("[x]"):
            shape_path = f"{m_of.group(1)}[x]"
        if code == "boolean":
            if kind != "boolean":
                run.add("error", "invalid", "Boolean values must be 'true' or 'false'", path, key)
                run.add("error", "invalid", "Error parsing JSON: the primitive value must be a boolean", shape_path, key)
            return
        if code in _INT_TYPES or code == "decimal":
            if kind != "number":
                run.add("error", "invalid", "Error parsing JSON: the primitive value must be a number", shape_path, key)
                if kind != "string":
                    return
        elif kind != "string":
            run.add("error", "invalid", "Error parsing JSON: the primitive value must be a string", path, key)
            return
        text = str(value)
        if kind == "string" and text == "":
            run.add("error", "invalid", "value cannot be empty", path, key)
            run.add(
                "error", "invariant",
                "Constraint failed: ele-1: 'All FHIR elements must have a @value or children' "
                "(defined in http://hl7.org/fhir/StructureDefinition/Element)",
                path, key, message_id="http://hl7.org/fhir/StructureDefinition/Element#ele-1",
            )
            return
        el_path = el.get("path", "") if el else ""
        if code in _URI_TYPES:
            if re.search(r"\s", text):
                run.add("error", "invalid", f"URI values cannot have whitespace('{text}')", value_path, key)
                if el_path == "Identifier.system":
                    run.add("error", "code-invalid", "Identifier.system must be an absolute reference, not a local reference", path.rpartition(".")[0], key[:-1])
                return
            is_own_url = el_path.endswith(".fullUrl") or (
                el_path.count(".") == 1 and el_path.endswith(".url") and self.registry.is_resource_type(el_path.split(".")[0]))
            if _EXAMPLE_URL.match(text) and not is_own_url:
                run.add("error", "invalid", f"Example URLs are not allowed in this context ({text})", value_path, key)
                return
            if text.startswith("urn:oid:"):
                oid = text[8:]
                arcs = oid.count(".") + 1
                if not (_OID_ARCS.fullmatch(oid) and ((oid[0] == "2" and arcs >= 3) or (oid[0] in "01" and arcs >= 4))):
                    run.add("error", "invalid", f"OIDs must be valid ({oid})", value_path, key)
                    return
            if text.startswith("urn:uuid:") and not _UUID.fullmatch(text[9:]):
                run.add("error", "invalid", f"UUIDs must be valid and lowercase ({text[9:]})", value_path, key)
                return
            if el_path == "Identifier.system" and ":" not in text:
                run.add("error", "code-invalid", "Identifier.system must be an absolute reference, not a local reference", path.rpartition(".")[0], key[:-1])
            if code == "canonical":
                base_url = text.split("|")[0]
                if base_url and not base_url.startswith("#") and ":" not in base_url:
                    run.add("error", "invalid", f"Canonical URLs must be absolute URLs if they are not fragment references ({text})", value_path, key)
                    return
                if not self._canonical_resolves(base_url, run):
                    run.add("warning" if base_url.startswith("#") else "information", "invalid", f"Canonical URL '{base_url}' does not resolve", value_path, key)
        if _RESOURCE_ID_PATH.match(el_path) and self.registry.is_resource_type(el_path.split(".")[0]):
            if len(text) > 64:
                run.add("error", "invalid", f"Invalid Resource id: Too long ({len(text)} chars)", value_path, key)
                return
            if not re.fullmatch(r"[A-Za-z0-9\-.]+", text):
                run.add("error", "invalid", f"Invalid Resource id: Invalid Characters ('{_short(text)}')", value_path, key)
                return
        if code == "string" and text != text.strip():
            run.add("warning", "invalid", f"value should not start or finish with whitespace '{_short(text)}'", value_path, key)
        rx = self._regex_for(code) if code != "base64Binary" else None
        if rx is not None and not rx.fullmatch(text):
            if code == "code":
                run.add("error", "invalid", f"The code '{_short(text)}' is not valid (whitespace rules)", value_path, key)
            elif code in ("date", "dateTime", "instant", "time"):
                run.add("error", "invalid", f"Not a valid {code} ('{_short(text)}' doesn't meet format requirements for {code})", value_path, key)
            else:
                run.add("error", "invalid", f"The value '{_short(text)}' is not a valid {code}", value_path, key)
            return
        if code in ("date", "dateTime", "instant"):
            m = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text)
            if m:
                y, mo, d = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
                try:
                    date(y, mo, d)
                except ValueError:
                    run.add("error", "invalid", f'Not a valid {code} (Invalid date/time format: "{text}")', value_path, key)

    def _canonical_resolves(self, url: str, run: _Run | None = None) -> bool:
        reg = self.registry
        if not url:
            return True
        if url.startswith("#"):
            cont = run.container_stack[-1] if run is not None and run.container_stack else None
            return bool(cont) and any(isinstance(c, dict) and c.get("id") == url[1:] for c in (cont.get("contained") or []))
        return bool(
            reg.get_by_url(url) or reg.get_value_set(url) or reg.get_code_system(url)
            or any(k[1] == url for k in reg._others)
        )

    def _check_xhtml(self, value: Any, run: _Run, path: str, key: tuple) -> None:
        if not isinstance(value, str):
            run.add("error", "invalid", "Error parsing JSON: the primitive value must be a string", path, key)
            return
        for kind, message in xhtml.check_narrative(value):
            if kind != xhtml.EMPTY:  # emptiness is reported through txt-2
                run.add("error", "invalid", message, path, key)

    def _check_fixed_pattern(self, value: Any, el: dict[str, Any], run: _Run, path: str, key: tuple) -> None:
        if value is None:
            return
        for k, v in el.items():
            if k.startswith("fixed") and len(k) > 5 and k[5].isupper():
                if not _equal(v, value):
                    msg = (
                        f"Value is '{_short(value)}' but must be '{_short(v)}'"
                        if not isinstance(value, (dict, list))
                        else f"Value does not match fixed value '{_short(v)}'"
                    )
                    run.add("error", "value", msg, path, key)
            elif k.startswith("pattern") and len(k) > 7 and k[7].isupper():
                if not _subset(v, value):
                    msg = (
                        f"Value is '{_short(value)}' but must be '{_short(v)}'"
                        if not isinstance(value, (dict, list))
                        else f"Unable to find a match for the specified pattern '{_short(v)}'"
                    )
                    run.add("error", "value", msg, path, key)

    # =================================================================== slicing
    def _slice_value(self, item: Any, path: str) -> list[Any]:
        """Evaluate a discriminator path to a list of plain values."""
        cur = [item]
        for seg in path.split("."):
            nxt: list[Any] = []
            for c in cur:
                if isinstance(c, list):
                    c_list = c
                else:
                    c_list = [c]
                for d in c_list:
                    if not isinstance(d, dict):
                        continue
                    if seg in d:
                        v = d[seg]
                        nxt.extend(v if isinstance(v, list) else [v])
                    else:
                        for k, v in d.items():
                            if k.startswith(seg) and len(k) > len(seg) and k[len(seg)].isupper():
                                nxt.extend(v if isinstance(v, list) else [v])
            cur = nxt
        return cur

    def _find_descendant(self, tree: SnapshotTree, start: dict[str, Any], path: str) -> dict[str, Any] | None:
        segs = [re.sub(r"\(.*?\)", "", sg) for sg in path.split(".")]
        segs = [sg for sg in segs if sg and sg != "$this"]

        def has_value(e: dict[str, Any]) -> bool:
            return any((k.startswith("fixed") and len(k) > 5 and k[5].isupper()) or (k.startswith("pattern") and len(k) > 7 and k[7].isupper()) for k in e)

        def walk(cur: dict[str, Any], rest: list[str]) -> dict[str, Any] | None:
            if not rest:
                return cur
            first: dict[str, Any] | None = None
            matches = []
            for c in tree.children(cur):
                name = c["path"].rsplit(".", 1)[-1]
                if name == rest[0] or (name.endswith("[x]") and name[:-3] == rest[0]):
                    matches.append(c)
            matches.sort(key=lambda c: bool(c.get("sliceName")))  # unsliced first
            for c in matches:
                found = walk(c, rest[1:])
                if found is not None:
                    if has_value(found):
                        return found
                    first = first or found
            return first

        return walk(start, segs)

    def _match_slice(
        self, item: Any, code: str | None, slice_el: dict[str, Any], slicing: dict[str, Any], tree: SnapshotTree,
        base_el: dict[str, Any], run: _Run,
    ) -> bool:
        discs = slicing.get("discriminator") or []
        if not discs:
            return False
        if code and not self.nav.is_primitive(code) and code not in ("BackboneElement", "Element"):
            base_path = code
        else:
            base_path = self._base_path(base_el)
        for d in discs:
            if not self._disc_match(d, item, code, slice_el, tree, base_path, run):
                return False
        return True

    def _expected_from_type_profile(self, slice_el: dict[str, Any], dpath: str) -> Any:
        """Fixed/pattern value for ``dpath`` taken from the slice's type profile (e.g. an extension's url)."""
        for t in slice_el.get("type", []) or []:
            for purl in t.get("profile", []) or []:
                psd = self.registry.get_by_url(purl)
                if psd is None or not psd.get("snapshot"):
                    continue
                tr = self.registry.tree_for(psd)
                if tr.root is None:
                    continue
                el = self._find_descendant(tr, tr.root, dpath)
                if el is None:
                    continue
                for k, v in el.items():
                    if (k.startswith("fixed") and len(k) > 5 and k[5].isupper()) or (k.startswith("pattern") and len(k) > 7 and k[7].isupper()):
                        return v
                if dpath == "url" and psd.get("type") == "Extension":
                    return psd.get("url")
        return None

    def _base_path(self, el: dict[str, Any]) -> str:
        """FHIRPath base type for an element definition (datatype name, or element path for backbones)."""
        types = el.get("type") or []
        if len(types) == 1:
            code = type_code(types[0])
            if code not in ("BackboneElement", "Element"):
                return code
        return el["path"]

    def _disc_match(self, d: dict[str, Any], item: Any, code: str | None, slice_el: dict[str, Any], tree: SnapshotTree, base_path: str, run: _Run) -> bool:
        dtype, dpath = d.get("type"), d.get("path", "$this")
        target = slice_el if dpath in ("$this", "") else self._find_descendant(tree, slice_el, dpath)
        simple = dpath == "$this" or _SIMPLE_PATH.match(dpath)
        if simple:
            vals = [item] if dpath in ("$this", "") else self._slice_value(item, dpath)
            raw_nodes = None
        else:
            try:
                raw_nodes = self.fp.evaluate(dpath, item, base_path, {"resource": run.res_stack[-1] if run.res_stack else None, "rootResource": run.container_stack[-1] if run.container_stack else run.root}, self._resolver(run))
                from fhirpathpy.engine.util import get_data

                vals = [get_data(n) for n in raw_nodes]
            except FhirPathError:
                return False
        if dtype in ("value", "pattern"):
            expected = None
            if target is not None:
                for k, v in target.items():
                    if (k.startswith("fixed") and len(k) > 5 and k[5].isupper()) or (k.startswith("pattern") and len(k) > 7 and k[7].isupper()):
                        expected = v
                        break
            if expected is None:
                expected = self._expected_from_type_profile(slice_el, dpath)
            if expected is None:
                if target is None:
                    return False
                b = target.get("binding")
                if b and b.get("valueSet") and b.get("strength") == "required":
                    for v in vals:
                        if self._value_in_vs(v, b["valueSet"]):
                            return True
                return False
            return any(_subset(expected, v) if dtype == "pattern" else _equal(expected, v) or _subset(expected, v) for v in vals)
        if dtype == "exists":
            if target is None:
                return False
            want = True if int(target.get("min", 0) or 0) > 0 else (False if str(target.get("max")) == "0" else True)
            return (len(vals) > 0) == want
        if dtype == "type":
            t = target if target is not None else slice_el
            codes = {type_code(x) for x in t.get("type", [])}
            if dpath in ("$this", ""):
                item_code = code or (item.get("resourceType") if isinstance(item, dict) else None)
                return bool(item_code) and item_code in codes
            if raw_nodes is None:
                try:
                    raw_nodes = self.fp.evaluate("$this" if dpath in ("$this", "") else dpath, item, base_path, {}, None)
                except FhirPathError:
                    return False
            for n in raw_nodes:
                npath = getattr(n, "path", None)
                if npath and (npath in codes or npath.split(".")[0] in codes):
                    return True
            return False
        if dtype == "profile":
            t = target if target is not None else slice_el
            for tt in t.get("type", []):
                for purl in tt.get("profile", []) or []:
                    psd = self.registry.get_by_url(purl)
                    if psd is not None and psd.get("type") == "Extension" and isinstance(item, dict):
                        if item.get("url") == purl:
                            return True
                    elif psd is not None and isinstance(item, dict):
                        sub = _Run(run.root, None)
                        tr = self.registry.tree_for(psd)
                        if tr.root is not None:
                            self._struct(item, Ctx(tr, tr.root), sub, "", (), overlay=True)
                            if not any(i.severity in ("error", "fatal") for i in sub.issues):
                                return True
            return False
        return False

    def _value_in_vs(self, v: Any, vs: str) -> bool:
        if isinstance(v, str):
            return self.terminology.validate_in_valueset(vs, None, v).status == "ok"
        if isinstance(v, dict):
            if "coding" in v:
                return any(self.terminology.validate_in_valueset(vs, c.get("system"), c.get("code", "")).status == "ok" for c in v["coding"] or [])
            return self.terminology.validate_in_valueset(vs, v.get("system"), v.get("code", "")).status == "ok"
        return False

    # =================================================================== references
    def _resolver(self, run: _Run):
        def resolve(ref: str) -> dict[str, Any] | None:
            if ref.startswith("#"):
                cur = run.container_stack[-1] if run.container_stack else None
                if cur is None:
                    return None
                if ref == "#":
                    return cur
                for c in cur.get("contained", []) or []:
                    if isinstance(c, dict) and c.get("id") == ref[1:]:
                        return c
                return None
            for b in reversed(run.bundles):
                for e in b.get("entry", []) or []:
                    r = e.get("resource") if isinstance(e, dict) else None
                    if not isinstance(r, dict):
                        continue
                    full = e.get("fullUrl")
                    if full == ref or (full and ref and full.endswith("/" + ref)):
                        return r
                    if r.get("id") and f"{r.get('resourceType')}/{r['id']}" == ref:
                        return r
            return None

        return resolve

    def _allowed_targets(self, types: list[dict[str, Any]]) -> set[str] | None:
        out: set[str] = set()
        for t in types:
            if type_code(t) != "Reference":
                continue
            profiles = t.get("targetProfile") or []
            if not profiles:
                return None
            for p in profiles:
                if p.endswith("/Resource") or p.endswith("/DomainResource"):
                    return None
                sd = self.registry.get_by_url(p)
                out.add(sd["type"] if sd and sd.get("type") else p.rsplit("/", 1)[-1])
        return out or None

    def _check_reference(self, value: dict[str, Any], types: list[dict[str, Any]], run: _Run, path: str, key: tuple) -> None:
        ref = value.get("reference")
        if not isinstance(ref, str) or not ref:
            return
        if run.bundles and run.entry_without_fullurl and "://" not in ref and not ref.startswith(("#", "urn:")):
            run.add("error", "required", "Relative Reference appears inside Bundle whose entry is missing a fullUrl", path, key)
        found = self._resolver(run)(ref)
        if found is None:
            if ref.startswith("#"):
                run.add("error", "structure", f"Unable to resolve resource with reference '{ref}'", path, key)
            return  # Java policy CHECK_TYPE_IF_EXISTS: unresolvable references are not type-checked
        rtype = found.get("resourceType") if isinstance(found, dict) else None
        allowed = self._allowed_targets(types)
        if rtype and allowed and rtype not in allowed:
            run.add(
                "error", "invalid",
                f"Invalid Resource target type. Found {rtype}, but expected one of ({sorted(allowed)})",
                f"{path}.reference", key + ("reference",),
            )

    def _report_display(self, res: Any, system: str | None, code: str, display: str | None, run: _Run, path: str, key: tuple) -> None:
        if res.wrong_display:
            run.add(
                self.display_sev, "invalid",
                f"Wrong Display Name '{display}' for {system}#{code}. Valid display is '{res.display}'",
                path, key,
            )

    def _report_unknown(self, res: Any, system: str | None, vs: str | None, run: _Run, path: str, key: tuple,
                        code: str = "", in_cc: bool = False, report_path: str | None = None, report_key: tuple = (),
                        version: str | None = None) -> None:
        """What HAPI says when a code cannot be checked because no terminology service can answer."""
        term = self.terminology
        if not system:
            return
        if self.registry.code_system_available(system) and not vs:
            return
        if not term.disabled and res.source == "tx" and res.system_known:
            return
        if not term.disabled and res.source != "tx":
            return  # a terminology server is configured: stay silent like HAPI does for systems it cannot resolve
        if re.match(r"^https?://([A-Za-z0-9-]+\.)*example\.org(/|$)", system):
            return
        bound = bool(vs)
        known_missing = f"A definition for CodeSystem '{system}'" + (f" version '{version}'" if version else "") + " could not be found, so the code cannot be validated" + (". Valid versions: []" if version else "")
        if in_cc or bound:
            run.add("warning", "not-found", known_missing, f"{path}.system", key + ("system",))
            if vs and self.terminology.undecidable_include(vs) is not None:
                name, label = self._vs_ref(vs)
                run.add("warning", "not-found",
                        f"Unable to check whether the code is in the value set '{label}' because the code system {system} was not found",
                        report_path or path, report_key if report_path else key)
            return
        if system in _TX_ONLY_SYSTEMS:
            run.add("warning", "business-rule",
                    f"Unable to validate code '{code}' in system '{system}' because the validator is running without terminology services",
                    path, key)
        elif system in _SILENT_SYSTEMS:
            return
        else:
            run.add("information", "unknown", known_missing, path, key)

    _SEVERITY = {"required": "error", "extensible": "warning", "preferred": "information"}
    _TAIL = {
        "required": "and a code is required from this value set ",
        "extensible": "and a code should come from this value set unless it has no suitable code "
        "(note that the validator cannot judge what is suitable)",
        "preferred": "and a code is recommended to come from this value set",
    }
    _CC_TAIL = {
        "required": "and a code from this value set is required",
        "extensible": "and a coding should come from this value set unless it has no suitable code "
        "(note that the validator cannot judge what is suitable)",
        "preferred": "and a coding is recommended to come from this value set",
    }

    def _vs_ref(self, vs: str) -> tuple[str, str]:
        """(display name, 'canonical|version') of a binding's value set, as HAPI prints it."""
        obj = self.registry.get_value_set(vs)
        base = vs.split("|")[0]
        ver = vs.split("|", 1)[1] if "|" in vs else (obj.get("version") if obj else None)
        name = (obj.get("title") or obj.get("name")) if obj else None
        return name or base, (f"{base}|{ver}" if ver else base)

    def _unknown_primitive_code(self, res: Any, vs: str, strength: str | None, text: str, run: _Run, path: str, key: tuple) -> None:
        term = self.terminology
        if not term.disabled:
            return
        if vs.split("|")[0].endswith("/ValueSet/mimetypes") and text in ("xml", "json"):
            return
        undecidable = term.undecidable_include(vs)
        name, label = self._vs_ref(vs)
        if undecidable is None:
            return
        idx, system = undecidable
        if strength == "required" and system in _TX_ONLY_SYSTEMS:
            nf = f"A definition for CodeSystem '{system}' could not be found, so the code cannot be validated"
            chk = f"Unable to check whether the code is in the value set '{label}' because the code system {system} was not found"
            run.add("warning", "code-invalid", f"{nf}; {chk}", path, key)
            run.add("warning", "not-found", nf, f"{path}.system", key + ("system",))
            run.add("warning", "not-found", chk, path, key)
        elif strength == "required":
            reason = f"include #{idx} has system {system} which could not be found, and the server returned error [No server available]"
            base = f"The System URI could not be determined for the code '{text}' in the ValueSet '{label}'"
            run.add("error", "code-invalid",
                    f"The value provided ('{text}') was not found in the value set '{name}' ({label}), and a code is required from this value set "
                    f" (error message = {base}: {reason}; {base})", path, key)
            run.add("error", "unknown", f"{base}: {reason}", path, key)
        else:
            run.add("warning", "code-invalid",
                    f"A definition for CodeSystem '{system}' could not be found, so the code cannot be validated; "
                    f"Unable to check whether the code is in the value set '{label}' because the code system {system} was not found",
                    path, key)

    def _check_coded(self, value: Any, code: str | None, el: dict[str, Any], run: _Run, path: str, key: tuple) -> None:
        term = self.terminology
        b = el.get("binding") or {}
        vs, strength = b.get("valueSet"), b.get("strength")
        active = bool(vs) and strength in self._SEVERITY
        sev = self._SEVERITY.get(strength or "", "error")
        memo = ("coded", key, vs or "-", code)
        if memo in run.memo:
            return
        run.memo.add(memo)

        if b and not vs and value is not None and code in ("code", "Coding", "CodeableConcept"):
            run.add("information", "code-invalid", f"Binding for path {path} has no source, so can't be checked", path, key)
            return
        if active and term.disabled and self.registry.get_value_set(vs) is None:
            run.add("warning", "code-invalid", f"ValueSet '{vs.split('|')[0]}' not found", path, key)
            return
        if code == "code" and active and value == "":
            if strength == "required":
                run.add("error", "code-invalid", "No code provided, and a code is required from the value set", path, key)
            return
        if code == "code" and active and isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
            text = str(value)
            res = term.validate_in_valueset(vs, None, text)
            if res.status == "invalid":
                name, label = self._vs_ref(vs)
                err = (
                    f"The System URI could not be determined for the code '{text}' in the ValueSet '{label}'; "
                    f"The provided code '#{text}' was not found in the value set '{label}'"
                    if res.source == "local"
                    else res.message
                )
                run.add(
                    sev, "code-invalid",
                    f"The value provided ('{text}') was not found in the value set '{name}' ({label}), "
                    f"{self._TAIL[strength]} (error message = {err})",
                    path, key,
                )
            elif res.status == "unknown":
                self._unknown_primitive_code(res, vs, strength, text, run, path, key)
            return

        if code in _QUANTITY_TYPES and active and isinstance(value, dict) and value.get("system") and value.get("code"):
            q_sys, q_code = str(value["system"]), str(value["code"])
            listed = term.enumerated_membership(vs, q_sys, q_code)
            res = term.validate_in_valueset(vs, q_sys, q_code)
            if listed is True:
                res = type(res)("ok")
            if res.status != "ok":
                name, label = self._vs_ref(vs)
                if res.status == "unknown" and term.disabled:
                    undec = term.undecidable_include(vs)
                    sysname = undec[1] if undec else q_sys
                    why = (f"A definition for CodeSystem '{sysname}' could not be found, so the code cannot be validated; "
                           f"Unable to check whether the code is in the value set '{label}' because the code system {sysname} was not found")
                elif res.status == "invalid":
                    why = res.message or f"The provided code '{q_sys}#{q_code}' was not found in the value set '{label}'"
                else:
                    why = ""
                if why:
                    need = {"required": "a code from this value set is required",
                            "extensible": "a code should come from this value set unless it has no suitable code",
                            "preferred": "a code is recommended to come from this value set"}[strength]
                    run.add(sev, "code-invalid",
                            f"The code provided ({q_sys}#{q_code}) was not found in the value set '{name}' ({label}), and {need}: {why}",
                            path, key)
            return

        if code == "Coding" and isinstance(value, dict):
            inside_cc = bool(re.search(r"\.coding(\[\d+\])?$", path))
            self._check_one_coding(value, vs if active else None, strength, run, path, key, in_cc=inside_cc, no_draft=inside_cc,
                                   report_path=path if inside_cc else None)
            return

        if code == "CodeableConcept" and isinstance(value, dict):
            codings = [c for c in (value.get("coding") or []) if isinstance(c, dict)]
            with_code = [(i, c) for i, c in enumerate(codings) if c.get("code")]
            results = []
            for i, c in with_code:
                results.append(
                    self._check_one_coding(c, vs if active else None, strength, run, f"{path}.coding[{i}]", key + ("coding", i), in_cc=True, report_path=path, report_key=key)
                )
            if active:
                name, label = self._vs_ref(vs)
                if not with_code:
                    if strength == "required" or value.get("text") or True:
                        verb = "is required" if strength == "required" else "should be provided"
                        run.add(sev if strength != "preferred" else "information", "code-invalid",
                                f"No code provided, and a code {verb} from the value set '{name}' ({label})", path, key)
                elif not any(r in ("ok", "unknown") for r in results):
                    codes = ", ".join(f"{c.get('system') or 'null'}#{c.get('code')}" for _, c in with_code)
                    run.add(sev, "code-invalid",
                            f"None of the codings provided are in the value set '{name}' ({label}), "
                            f"{self._CC_TAIL[strength]} (codes = {codes})", path, key)

    def _draft_note(self, system: str, run: _Run, path: str, key: tuple) -> None:
        """HAPI notes (information) when a validated code comes from a draft CodeSystem."""
        cs = self.terminology.registry.get_code_system(system)
        if cs is None or cs.get("content") == "not-present":
            return
        standards = next(
            (e.get("valueCode") for e in cs.get("extension", []) or [] if e.get("url", "").endswith("structuredefinition-standards-status")),
            None,
        )
        if cs.get("status") == "draft" or standards == "draft":
            ver = cs.get("version")
            run.add("information", "business-rule", f"Reference to draft CodeSystem {system}" + (f"|{ver}" if ver else ""), path, key)

    def _check_one_coding(
        self, c: dict[str, Any], vs: str | None, strength: str | None, run: _Run, path: str, key: tuple, in_cc: bool = False,
        report_path: str | None = None, report_key: tuple = (), no_draft: bool = False,
    ) -> str:
        term = self.terminology
        system, code, display = c.get("system"), c.get("code"), c.get("display")
        if not isinstance(code, str) or not code:
            return "skip"
        if not isinstance(system, str):
            system = None
        if system is None:
            run.add(
                "warning", "invalid",
                "Coding has no system. A code with no system has no defined meaning, and it cannot be validated. "
                "A system should be provided",
                path, key,
            )
            return "invalid"
        if vs:
            res = term.validate_in_valueset(vs, system, code, display)
            if res.status == "ok":
                self._report_display(res, system, code, display, run, f"{path}.display", key + ("display",))
                return "ok"
            if res.status == "unknown":
                self._report_unknown(res, system, vs, run, path, key, code, in_cc, report_path, report_key, c.get("version"))
                return "unknown"
            # not in the value set: say so at the code level when the code system is local and lacks the code
            if system and term.registry.code_system_available(system):
                sres = term.validate_in_system(system, code, display)
                if sres.status == "invalid":
                    run.add("error", "code-invalid", sres.message or f"Unknown code '{code}' in the CodeSystem '{system}'", f"{path}.code", key + ("code",))
                elif sres.status == "ok" and not no_draft:
                    self._draft_note(system, run, report_path or path, report_key if report_path else key)
            if not in_cc:
                name, label = self._vs_ref(vs)
                err = res.message or f"The provided code '{system}#{code}' was not found in the value set '{label}'"
                if not term.registry.code_system_available(system):
                    err = (f"A definition for CodeSystem '{system}' could not be found, so the code cannot be validated; "
                           f"The provided code '{system}#{code}' was not found in the value set '{label}'")
                run.add(
                    self._SEVERITY.get(strength or "", "error"), "code-invalid",
                    f"The Coding provided ({system}#{code}) was not found in the value set '{name}' ({label}), "
                    f"{self._TAIL[strength or 'required']} (error message = {err})",
                    path, key,
                )
            return "invalid"
        if system:
            res = term.validate_in_system(system, code, display)
            if res.status == "invalid":
                run.add("error", "code-invalid", res.message or f"Unknown code '{code}' in the CodeSystem '{system}'", f"{path}.code", key + ("code",))
                return "invalid"
            if res.status == "unknown":
                self._report_unknown(res, system, None, run, path, key, code, in_cc, report_path, report_key, c.get("version"))
                return "unknown"
            self._report_display(res, system, code, display, run, f"{path}.display", key + ("display",))
            if not no_draft:
                self._draft_note(system, run, report_path or path, report_key if report_path else key)
            return "ok"
        return "skip"

    def _constraints(
        self, value: Any, el: dict[str, Any], base_path: str, run: _Run, path: str, key: tuple, tree: SnapshotTree
    ) -> None:
        cons = el.get("constraint")
        if not cons:
            return
        is_contained = bool(run.res_stack and run.container_stack and run.res_stack[-1] is not run.container_stack[-1])
        variables = {
            "resource": run.res_stack[-1] if run.res_stack else value,
            "rootResource": run.container_stack[-1] if run.container_stack else run.root,
        }
        resolver = self._resolver(run)
        sd_url = run.sd_stack[-1] if run.sd_stack else tree.sd.get("url")
        for c in cons:
            ckey, expr = c.get("key"), c.get("expression")
            if not expr or ckey in ("ele-1", "dom-3"):
                continue
            if ckey == "dom-6" and is_contained:
                continue
            source = c.get("source") or tree.sd.get("url")
            memo = ("c", key, ckey, source)
            if memo in run.memo:
                continue
            run.memo.add(memo)
            trace: list[tuple[str, list[Any]]] = []
            try:
                if ckey in ("txt-1", "txt-2") and isinstance(value, str):
                    problems = xhtml.check_narrative(value)
                    bad = {xhtml.ELEMENT, xhtml.ATTRIBUTE} if ckey == "txt-1" else {xhtml.EMPTY}
                    ok = not any(k in bad for k, _ in problems)
                else:
                    ok = self.fp.evaluate_boolean(expr, value, base_path, variables, resolver, trace)
            except FhirPathError as e:
                if expr not in run.bad_fp:
                    run.bad_fp.add(expr)
                    logger.debug("Skipping invariant %s: %s", ckey, e)
                continue
            if ok:
                continue
            severity = "warning" if c.get("severity") == "warning" else "error"
            human = c.get("human") or ckey
            declared = c.get("source")  # HAPI only names the source when the constraint declares one
            where = f" (defined in {declared})" if declared and declared != sd_url else ""
            best = any(
                x.get("url", "").endswith("elementdefinition-bestpractice") and x.get("valueBoolean") is True
                for x in (c.get("extension") or [])
            )
            suffix = " (Best Practice Recommendation)" if best else ""
            if trace and " or " in expr and " and " not in expr:
                log = "; ".join(f"{n}: {', '.join(str(x) for x in vals)}" for n, vals in trace)
                suffix += f" ({log}) (log:  ({log}))"
            run.add(severity, "invariant", f"Constraint failed: {ckey}: '{human}'{where}{suffix}", path, key,
                    message_id=f"{source}#{ckey}" if source else None)

def _a(kind: str) -> str:
    return f"{'an' if kind[0] in 'aeiou' else 'a'} {kind}"
