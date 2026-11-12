import gzip
import io
import re
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.fhir import jsonio
from app.schemas.ig import IgResponse
from app.services.profile_registry import ProfileRegistry

logger = get_logger(__name__)

# Dependencies that the built-in R4 definitions already cover
_CORE_PACKAGES = {"hl7.fhir.r4.core", "hl7.fhir.core"}
_CONFORMANCE = {"StructureDefinition", "ValueSet", "CodeSystem"}
_MAX_DEPTH = 6


class IgError(ValueError):
    """The IG package could not be found / read."""


class IgNotFound(IgError):
    pass


@dataclass
class LoadedPackage:
    id: str
    version: str
    canonical: str | None
    profiles: list[str]
    custom: bool = False  # uploaded via POST /igs
    dependencies: dict[str, str] = field(default_factory=dict)


def _version_key(v: str) -> tuple:
    parts = re.split(r"[.\-+]", v)
    return tuple((0, int(p)) if p.isdigit() else (-1, p) for p in parts)


class IgService:
    """Manages FHIR Implementation Guides (NPM packages) and the profiles they contribute."""

    def __init__(
        self,
        registry: ProfileRegistry,
        igs_dir: Path | None = None,
        cache_dir: Path | None = None,
        registry_url: str | None = None,
        offline: bool | None = None,
        preload: list[str] | None = None,
        preload_strict: bool | None = None,
    ) -> None:
        self.registry = registry
        self.igs_dir = igs_dir or settings.igs_dir
        self.cache_dir = cache_dir or settings.package_cache_dir
        self.registry_url = (registry_url or settings.packages_registry_url).rstrip("/")
        self.offline = settings.packages_offline if offline is None else offline
        self.loaded: dict[str, LoadedPackage] = {}  # key: "id#version"
        self._lock = threading.RLock()
        self.catalogue_url = settings.packages_catalogue_url
        self._catalogue_cache: dict[str, str] | None = None
        self._catalogue_at = 0.0
        self._load_local_igs()
        specs = preload if preload is not None else [x.strip() for x in settings.preload_igs.split(",") if x.strip()]
        self._preload_configured(specs, settings.preload_igs_strict if preload_strict is None else preload_strict)

    # ------------------------------------------------------------------ startup
    def _preload_configured(self, specs: list[str], strict: bool) -> None:
        """Load the guides named in PRELOAD_IGS (``id#version`` or just ``id``) from the cache or the registry."""
        for spec in specs:
            pkg_id, _, version = spec.partition("#")
            try:
                self._load_from_registry(pkg_id.strip(), version.strip() or None)
                logger.info(f"Preloaded IG {spec}")
            except Exception as e:
                logger.error(f"Could not preload IG '{spec}': {e}")
                if strict:
                    raise IgError(f"Could not preload IG '{spec}': {e}") from e

    def forget_profile(self, url: str) -> None:
        """Drop a removed profile from the per-IG profile lists."""
        with self._lock:
            for pkg in self.loaded.values():
                if url in pkg.profiles:
                    pkg.profiles.remove(url)

    def _load_local_igs(self) -> None:
        if not self.igs_dir.exists():
            return
        # sorted for a deterministic load order (mirrors the Java wrapper)
        for ig_path in sorted(self.igs_dir.glob("*.tgz")):
            try:
                self.load_package(ig_path.read_bytes(), custom=False, resolve_dependencies=True)
                logger.info(f"Loaded IG package from {ig_path.name}")
            except Exception as e:
                logger.error(f"Failed to load IG from {ig_path}: {e}")

    # ------------------------------------------------------------------ tar handling
    @staticmethod
    def _open_tar(content: bytes) -> tarfile.TarFile:
        data = content
        last: Exception | None = None
        for _ in range(3):  # tolerate a doubly-gzipped body (client + 'Content-Encoding: gzip')
            try:
                return tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
            except tarfile.TarError as e:
                last = e
                if data[:2] == b"\x1f\x8b":
                    try:
                        data = gzip.decompress(data)
                        continue
                    except Exception:
                        break
                break
        raise IgError(f"The body is not a valid NPM package tarball: {last}")

    def load_package(
        self,
        content: bytes,
        custom: bool = True,
        resolve_dependencies: bool = True,
        _depth: int = 0,
    ) -> IgResponse:
        """Load a gzipped FHIR NPM package (``package.tgz``) into the validator."""
        pkg_info: dict[str, Any] = {}
        profiles: list[str] = []
        pending: list[dict[str, Any]] = []
        with self._open_tar(content) as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = member.name
                if not name.endswith(".json"):
                    continue
                base = name.rsplit("/", 1)[-1]
                if name == "package/package.json":
                    try:
                        pkg_info = jsonio.loads(tar.extractfile(member).read())  # type: ignore[union-attr]
                    except Exception:
                        pkg_info = {}
                    continue
                if base.startswith(".") or not name.startswith("package/") or name.count("/") != 1:
                    continue  # only top-level conformance resources (and not .index.json)
                try:
                    res = jsonio.loads(tar.extractfile(member).read())  # type: ignore[union-attr]
                except Exception:
                    continue
                if isinstance(res, dict) and res.get("resourceType") in _CONFORMANCE:
                    pending.append(res)

        pkg_id = pkg_info.get("name")
        if not pkg_id:
            raise IgError("The package does not contain a package/package.json with a 'name'")
        version = pkg_info.get("version") or ""
        canonical = pkg_info.get("canonical") or pkg_info.get("url")
        deps = {k: str(v) for k, v in (pkg_info.get("dependencies") or {}).items()}

        key = f"{pkg_id}#{version}"
        with self._lock:
            existing = self.loaded.get(key)
            for res in pending:
                url = self.registry.register(res)
                if res.get("resourceType") == "StructureDefinition" and url:
                    profiles.append(url)
            profiles = sorted(set(profiles))
            pkg = LoadedPackage(pkg_id, version, canonical, profiles, custom=custom or bool(existing and existing.custom), dependencies=deps)
            self.loaded[key] = pkg
        logger.info(f"Loaded IG {pkg_id}#{version} with {len(profiles)} profiles")

        if resolve_dependencies and _depth < _MAX_DEPTH:
            for dep_id, dep_ver in deps.items():
                if dep_id in _CORE_PACKAGES or self._is_loaded(dep_id, dep_ver):
                    continue
                try:
                    self._load_from_registry(dep_id, dep_ver, _depth + 1)
                except Exception as e:
                    logger.warning(f"Could not load dependency {dep_id}#{dep_ver} of {pkg_id}: {e}")
        return IgResponse(id=pkg_id, version=version, profiles=profiles)

    # ------------------------------------------------------------------ registry access
    def _is_loaded(self, pkg_id: str, version: str | None) -> bool:
        for p in self.loaded.values():
            if p.id == pkg_id and (not version or version in ("latest", "current") or p.version == version or self._matches(p.version, version)):
                return True
        return False

    @staticmethod
    def _matches(version: str, wanted: str) -> bool:
        if wanted.endswith(".x") or wanted.endswith(".*") or "x" in wanted.split("."):
            prefix = [p for p in wanted.split(".") if p not in ("x", "*")]
            return version.split(".")[: len(prefix)] == prefix
        return version == wanted

    def _http(self) -> httpx.Client:
        return httpx.Client(timeout=60.0, follow_redirects=True)

    def _resolve_version(self, pkg_id: str, version: str | None) -> str:
        if version and not any(c in version for c in "x*^~"):
            return version
        if self.offline:
            cached = self._cached_versions(pkg_id)
            if cached:
                return cached[-1]
            raise IgNotFound(f"Package '{pkg_id}' is not cached and PACKAGES_OFFLINE is set")
        with self._http() as client:
            r = client.get(f"{self.registry_url}/{pkg_id}", headers={"Accept": "application/json"})
        if r.status_code == 404:
            raise IgNotFound(f"Unable to find package '{pkg_id}' on {self.registry_url}")
        r.raise_for_status()
        meta = r.json()
        versions = list(meta.get("versions", {}).keys())
        if not version:
            latest = meta.get("dist-tags", {}).get("latest")
            if latest:
                return latest
        else:
            matching = [v for v in versions if self._matches(v, version.lstrip("^~"))]
            if matching:
                return max(matching, key=_version_key)
        if versions:
            return max(versions, key=_version_key)
        raise IgNotFound(f"Unable to find a version of package '{pkg_id}'")

    def _cached_versions(self, pkg_id: str) -> list[str]:
        if not self.cache_dir.exists():
            return []
        out = []
        for p in self.cache_dir.glob(f"{pkg_id}#*.tgz"):
            out.append(p.name[len(pkg_id) + 1 : -4])
        return sorted(out, key=_version_key)

    def _fetch_tgz(self, pkg_id: str, version: str) -> bytes:
        cached = self.cache_dir / f"{pkg_id}#{version}.tgz"
        if cached.exists():
            return cached.read_bytes()
        if self.offline:
            raise IgNotFound(f"Package '{pkg_id}#{version}' is not cached and PACKAGES_OFFLINE is set")
        url = f"{self.registry_url}/{pkg_id}/{version}"
        with self._http() as client:
            r = client.get(url, headers={"Accept": "application/tar+gzip, application/octet-stream"})
        if r.status_code == 404:
            raise IgNotFound(f"Unable to find package '{pkg_id}#{version}' on {self.registry_url}")
        r.raise_for_status()
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(".part")
            tmp.write_bytes(r.content)
            tmp.replace(cached)
        except OSError as e:
            logger.warning(f"Could not cache package {pkg_id}#{version}: {e}")
        return r.content

    def _load_from_registry(self, pkg_id: str, version: str | None, depth: int = 0) -> IgResponse:
        resolved = self._resolve_version(pkg_id, version)
        key = f"{pkg_id}#{resolved}"
        with self._lock:
            existing = self.loaded.get(key)
            if existing is not None:
                return IgResponse(id=existing.id, version=existing.version, profiles=existing.profiles)
        content = self._fetch_tgz(pkg_id, resolved)
        return self.load_package(content, custom=False, resolve_dependencies=True, _depth=depth)

    def load_ig(self, package_id: str, version: str | None = None) -> IgResponse:
        """Load an IG by NPM package id (and optional version) into the validator."""
        # A previously loaded package that matches is reused (like Java's findCustomPackage).
        with self._lock:
            for p in self.loaded.values():
                if p.id == package_id and (version is None or p.version == version):
                    return IgResponse(id=p.id, version=p.version, profiles=p.profiles)
        return self._load_from_registry(package_id, version)

    # ------------------------------------------------------------------ listings
    def _catalogue(self) -> dict[str, str]:
        """Package id -> download URL for every package on the registry (cached for an hour)."""
        now = time.time()
        if self._catalogue_cache is not None and now - self._catalogue_at < 3600:
            return self._catalogue_cache
        out: dict[str, str] = {}
        if not self.offline:
            try:
                with self._http() as client:
                    r = client.get(self.catalogue_url, headers={"Accept": "application/json"})
                r.raise_for_status()
                for entry in r.json():
                    name, url = entry.get("name"), entry.get("url")
                    if name and url and name not in out:  # first entry wins, like the Java wrapper
                        out[name] = url
            except Exception as e:
                logger.warning(f"Could not fetch the package catalogue: {e}")
                if self._catalogue_cache is not None:
                    return self._catalogue_cache
        self._catalogue_cache, self._catalogue_at = out, now
        return out

    def get_known_igs(self) -> dict[str, str]:
        """IG package id -> URL: loaded custom IGs (canonical) plus every package on the registry."""
        igs: dict[str, str] = {}
        for p in self.loaded.values():
            if p.custom and p.canonical:
                igs[p.id] = p.canonical
        for pkg_id, url in self._catalogue().items():
            igs.setdefault(pkg_id, url)
        if not igs:  # offline and nothing uploaded: still report the built-in core
            igs["hl7.fhir.r4.core"] = "http://hl7.org/fhir/R4"
        return igs

    def get_profiles_by_ig(self) -> dict[str, list[str]]:
        """IG package id -> profile (StructureDefinition) URLs for the IGs loaded into the validator."""
        out: dict[str, list[str]] = {}
        for p in self.loaded.values():
            out.setdefault(p.id, p.profiles)  # first loaded wins on duplicates
        return out
