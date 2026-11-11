def test_get_resources(client):
    response = client.get("/resources")
    assert response.status_code == 200
    resources = response.json()
    assert isinstance(resources, list)
    assert "Patient" in resources
    assert "Observation" in resources
    assert "Encounter" in resources


def test_get_profiles(client):
    response = client.get("/profiles")
    assert response.status_code == 200
    profiles = response.json()
    assert isinstance(profiles, list)
    # Check that base patient profile exists
    assert "http://hl7.org/fhir/StructureDefinition/Patient" in profiles
    # Check that US Core Patient profile from preloaded IG exists
    assert "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient" in profiles


def test_load_profile(client, load_fixture):
    profile_bytes = load_fixture("profile_fixture.json")
    profile_url = "http://hl7.org/fhir/StructureDefinition/blah"

    # Verify not present initially
    before_resp = client.get("/profiles")
    assert profile_url not in before_resp.json()

    # Load profile
    post_resp = client.post(
        "/profiles",
        content=profile_bytes,
        headers={"Content-Type": "application/fhir+json"},
    )
    assert post_resp.status_code == 200

    # Verify present after loading
    after_resp = client.get("/profiles")
    assert profile_url in after_resp.json()


def test_load_profile_file_fixture(client, load_fixture):
    profile_bytes = load_fixture("profile_file_fixture.json")
    profile_url = "http://hl7.org/fhir/StructureDefinition/foo"

    post_resp = client.post(
        "/profiles",
        content=profile_bytes,
        headers={"Content-Type": "application/fhir+json"},
    )
    assert post_resp.status_code == 200

    after_resp = client.get("/profiles")
    assert profile_url in after_resp.json()


# ---- snapshot generation from a differential-only profile, slicing, fixed/pattern -----------------
import pytest
from conftest import errors_of, messages_of

TEST_PATIENT = "http://fhir.test.local/fhir/StructureDefinition/test-patient"
NARRATIVE = {"status": "generated", "div": "<div xmlns='http://www.w3.org/1999/xhtml'>x</div>"}


@pytest.fixture(scope="module")
def test_patient_profile(client):
    from conftest import FIXTURES_DIR

    r = client.post("/profiles", content=(FIXTURES_DIR / "differential_patient_profile.json").read_bytes())
    assert r.status_code == 200
    return TEST_PATIENT


def good(**kw):
    base = {
        "resourceType": "Patient", "text": NARRATIVE,
        "identifier": [{"system": "http://hospital.fhir.test.local/mrn", "value": "1"}],
        "name": [{"family": "Doe"}],
    }
    base.update(kw)
    return base


def validate(client, body):
    r = client.post(f"/validate?profile={TEST_PATIENT}", json=body)
    assert r.status_code == 200
    return r.json()


def test_differential_profile_is_listed(client, test_patient_profile):
    assert TEST_PATIENT in client.get("/profiles").json()


def test_valid_against_generated_snapshot(client, test_patient_profile):
    out = validate(client, good())
    assert errors_of(out) == [], messages_of(out)


def test_required_slice_missing(client, test_patient_profile):
    body = good(identifier=[{"system": "http://other.fhir.test.local", "value": "1"}])
    assert "Slice 'Patient.identifier:mrn': a matching slice is required, but not found" in messages_of(validate(client, body))


def test_slice_max(client, test_patient_profile):
    ident = {"system": "http://hospital.fhir.test.local/mrn", "value": "1"}
    assert "identifier:mrn: max allowed = 1, but found 2" in messages_of(validate(client, good(identifier=[ident, dict(ident, value="2")])))


def test_constraint_on_child_of_unexpanded_type(client, test_patient_profile):
    out = validate(client, good(name=[{"given": ["A"]}]))
    assert "Patient.name.family: minimum required = 1" in messages_of(out)


def test_prohibited_element(client, test_patient_profile):
    out = validate(client, good(birthDate="2000-01-01"))
    assert "Patient.birthDate: max allowed = 0, but found 1" in messages_of(out)


def test_pattern_value(client, test_patient_profile):
    ok = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus", "code": "M", "display": "Married"}]}
    assert errors_of(validate(client, good(maritalStatus=ok))) == []
    bad = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus", "code": "S"}]}
    assert "Unable to find a match for the specified pattern" in messages_of(validate(client, good(maritalStatus=bad)))


def test_profile_applies_through_meta_profile(client, test_patient_profile):
    body = good(meta={"profile": [TEST_PATIENT]}, birthDate="2000-01-01")
    r = client.post("/validate", json=body)
    assert "Patient.birthDate: max allowed = 0" in messages_of(r.json())


def test_load_value_set_and_use_it_as_binding(client):
    r = client.post("/profiles", json={
        "resourceType": "ValueSet", "id": "t", "url": "http://fhir.test.local/fhir/ValueSet/colours", "status": "active",
        "compose": {"include": [{"system": "http://fhir.test.local/colours", "concept": [{"code": "red"}, {"code": "blue"}]}]},
    })
    assert r.status_code == 200
    # like HAPI, a value set can only be evaluated when its code system is known too
    r = client.post("/profiles", json={
        "resourceType": "CodeSystem", "id": "c", "url": "http://fhir.test.local/colours", "status": "active", "content": "complete",
        "concept": [{"code": "red"}, {"code": "blue"}],
    })
    assert r.status_code == 200
    r = client.post("/profiles", json={
        "resourceType": "StructureDefinition", "id": "obs-colour", "url": "http://fhir.test.local/fhir/StructureDefinition/obs-colour",
        "name": "ObsColour", "status": "active", "kind": "resource", "abstract": False, "type": "Observation",
        "baseDefinition": "http://hl7.org/fhir/StructureDefinition/Observation", "derivation": "constraint",
        "differential": {"element": [{"id": "Observation.code", "path": "Observation.code",
                                      "binding": {"strength": "required", "valueSet": "http://fhir.test.local/fhir/ValueSet/colours"}}]},
    })
    assert r.status_code == 200
    obs = lambda code: {"resourceType": "Observation", "text": NARRATIVE, "status": "final",
                        "code": {"coding": [{"system": "http://fhir.test.local/colours", "code": code}]}}
    url = "/validate?profile=http://fhir.test.local/fhir/StructureDefinition/obs-colour"
    assert errors_of(client.post(url, json=obs("red")).json()) == []
    assert "was not found in the value set" in messages_of(client.post(url, json=obs("green")).json()) or \
           "None of the codings provided are in the value set" in messages_of(client.post(url, json=obs("green")).json())


# ---- layered profiles: changing a parent refreshes everything derived from it --------------------------
def test_changing_a_parent_profile_updates_derived_profiles(client):
    def sd(url, base, diff):
        return {"resourceType": "StructureDefinition", "id": url.rsplit("/", 1)[-1], "url": url, "name": "P",
                "status": "active", "kind": "resource", "abstract": False, "type": "Patient",
                "baseDefinition": base, "derivation": "constraint", "differential": {"element": diff}}

    country = "http://fhir.test.local/country/StructureDefinition/c-patient"
    org = "http://fhir.test.local/org/h1/StructureDefinition/h1-patient"
    base = "http://hl7.org/fhir/StructureDefinition/Patient"
    gender = {"id": "Patient.gender", "path": "Patient.gender", "min": 1}
    name = {"id": "Patient.name", "path": "Patient.name", "min": 1}
    birth = {"id": "Patient.birthDate", "path": "Patient.birthDate", "min": 1}
    assert client.post("/profiles", json=sd(country, base, [gender])).status_code == 200
    assert client.post("/profiles", json=sd(org, country, [birth])).status_code == 200
    patient = {"resourceType": "Patient", "text": NARRATIVE, "gender": "male", "birthDate": "2000-01-01"}

    def errors():
        out = client.post(f"/validate?profile={org}", json=patient).json()
        return [i["details"]["text"] for i in out["issue"] if i["severity"] == "error"]

    assert errors() == []  # org profile used once, so its snapshot is generated
    client.post("/profiles", json=sd(country, base, [gender, name]))  # country now also requires a name
    assert any("Patient.name: minimum required = 1" in e for e in errors())  # without reloading the org profile
