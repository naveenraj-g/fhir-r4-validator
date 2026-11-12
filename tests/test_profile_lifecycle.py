"""DELETE /profiles and PRELOAD_IGS."""
import gzip
import pytest

from app.services.ig_service import IgError, IgService
from app.services.profile_registry import ProfileRegistry
from conftest import messages_of
from test_igs import make_package

N = {"status": "generated", "div": "<div xmlns='http://www.w3.org/1999/xhtml'>x</div>"}
BASE = "http://hl7.org/fhir/StructureDefinition/Patient"


def sd(url, base, diff):
    return {"resourceType": "StructureDefinition", "id": url.rsplit("/", 1)[-1], "url": url, "name": "P", "status": "active",
            "kind": "resource", "abstract": False, "type": "Patient", "baseDefinition": base, "derivation": "constraint",
            "differential": {"element": diff}}


def listed(client):
    return client.get("/profiles").json()


# ---- DELETE /profiles ------------------------------------------------------------------------
def test_delete_removes_a_profile(client):
    url = "http://fhir.test.local/del/StructureDefinition/one"
    client.post("/profiles", json=sd(url, BASE, [{"id": "Patient.gender", "path": "Patient.gender", "min": 1}]))
    assert url in listed(client)
    r = client.delete("/profiles", params={"url": url})
    assert r.status_code == 204 and r.content == b""
    assert url not in listed(client)
    # it can no longer be used for validation
    out = client.post(f"/validate?profile={url}", json={"resourceType": "Patient", "text": N}).json()
    assert "Unable to locate the profile" in messages_of(out)


def test_delete_unknown_url_is_404(client):
    r = client.delete("/profiles", params={"url": "http://fhir.test.local/nothing/here"})
    assert r.status_code == 404
    assert r.json()["resourceType"] == "OperationOutcome"


def test_hl7_base_definitions_cannot_be_removed(client):
    for url in (BASE, "http://hl7.org/fhir/ValueSet/administrative-gender", "http://hl7.org/fhir/StructureDefinition/vitalsigns"):
        r = client.delete("/profiles", params={"url": url})
        assert r.status_code == 403, url
        assert r.json()["issue"][0]["code"] == "forbidden"
    assert BASE in listed(client)


def test_delete_requires_the_url_parameter(client):
    assert client.delete("/profiles").status_code == 422


def test_version_suffix_is_ignored(client):
    url = "http://fhir.test.local/del/StructureDefinition/versioned"
    client.post("/profiles", json=sd(url, BASE, []))
    assert client.delete("/profiles", params={"url": url + "|1.0.0"}).status_code == 204
    assert url not in listed(client)


def test_value_sets_and_code_systems_can_be_removed(client):
    vs_url, cs_url = "http://fhir.test.local/ValueSet/del-vs", "http://fhir.test.local/CodeSystem/del-cs"
    client.post("/profiles", json={"resourceType": "ValueSet", "id": "v", "url": vs_url, "status": "active",
                                   "compose": {"include": [{"system": cs_url, "concept": [{"code": "a"}]}]}})
    client.post("/profiles", json={"resourceType": "CodeSystem", "id": "c", "url": cs_url, "status": "active",
                                   "content": "complete", "concept": [{"code": "a"}]})
    assert client.delete("/profiles", params={"url": vs_url}).status_code == 204
    assert client.delete("/profiles", params={"url": cs_url}).status_code == 204
    assert client.delete("/profiles", params={"url": vs_url}).status_code == 404


def test_deleting_a_parent_stops_derived_profiles_from_silently_using_old_rules(client):
    parent = "http://fhir.test.local/del/StructureDefinition/parent"
    child = "http://fhir.test.local/del/StructureDefinition/child"
    client.post("/profiles", json=sd(parent, BASE, [{"id": "Patient.gender", "path": "Patient.gender", "min": 1}]))
    client.post("/profiles", json=sd(child, parent, [{"id": "Patient.birthDate", "path": "Patient.birthDate", "min": 1}]))
    empty = {"resourceType": "Patient", "text": N}
    assert "Patient.gender: minimum required = 1" in messages_of(client.post(f"/validate?profile={child}", json=empty).json())
    assert client.delete("/profiles", params={"url": parent}).status_code == 204
    out = client.post(f"/validate?profile={child}", json=empty).json()
    assert "Patient.gender: minimum required = 1" not in messages_of(out)  # the removed parent's rules are gone
    assert "Unable to locate the profile" in messages_of(out)  # and the child says it cannot be used


def test_a_removed_profile_can_be_loaded_again(client):
    url = "http://fhir.test.local/del/StructureDefinition/again"
    body = sd(url, BASE, [{"id": "Patient.gender", "path": "Patient.gender", "min": 1}])
    client.post("/profiles", json=body)
    client.delete("/profiles", params={"url": url})
    client.post("/profiles", json=body)
    out = client.post(f"/validate?profile={url}", json={"resourceType": "Patient", "text": N}).json()
    assert "Patient.gender: minimum required = 1" in messages_of(out)


def test_removed_profile_leaves_the_per_ig_list(client):
    pkg_profile = "http://fhir.test.local/fhir/lifecycle.pkg/StructureDefinition/example-patient"
    assert client.post("/igs", content=make_package("lifecycle.pkg", "1.0.0")).status_code == 200
    assert pkg_profile in client.get("/profiles-by-ig").json()["lifecycle.pkg"]
    assert client.delete("/profiles", params={"url": pkg_profile}).status_code == 204
    assert pkg_profile not in client.get("/profiles-by-ig").json()["lifecycle.pkg"]


# ---- PRELOAD_IGS -----------------------------------------------------------------------------
@pytest.fixture()
def cache(tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "example.fhir.core#1.0.0.tgz").write_bytes(make_package("example.fhir.core", "1.0.0"))
    (tmp_path / "igs").mkdir()
    return tmp_path


def make_service(cache, preload, strict=False):
    return IgService(ProfileRegistry(load_base=False), igs_dir=cache / "igs", cache_dir=cache / "cache",
                     offline=True, preload=preload, preload_strict=strict)


def test_preload_loads_the_listed_guides(cache):
    svc = make_service(cache, ["example.fhir.core#1.0.0"])
    assert "example.fhir.core#1.0.0" in svc.loaded


def test_nothing_is_preloaded_by_default(cache):
    assert make_service(cache, []).loaded == {}


def test_a_failed_preload_is_logged_and_startup_continues(cache):
    svc = make_service(cache, ["missing.pkg#9.9.9", "example.fhir.core#1.0.0"])
    assert "example.fhir.core#1.0.0" in svc.loaded and "missing.pkg#9.9.9" not in svc.loaded


def test_strict_preload_fails_startup(cache):
    with pytest.raises(IgError):
        make_service(cache, ["missing.pkg#9.9.9"], strict=True)
