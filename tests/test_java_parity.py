"""Behaviours confirmed against the real Java wrapper (HAPI 6.5.18) and kept as regression tests.

Every expected string below was read from the running Java service, not invented.
"""
import pytest
from conftest import messages_of

N = {"status": "generated", "div": "<div xmlns='http://www.w3.org/1999/xhtml'>x</div>"}


def issues(client, body, profile=None):
    r = client.post("/validate" + (f"?profile={profile}" if profile else ""), json=body)
    assert r.status_code == 200
    return r.json()["issue"]


def texts(client, body, profile=None):
    return [i["details"]["text"] for i in issues(client, body, profile)]


def patient(**kw):
    return {"resourceType": "Patient", "text": N, **kw}


# ---- message wording ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body, expected",
    [
        (patient(foo=1), "Unrecognized property 'foo'"),
        (patient(birthDate="2020-02-30"), 'Not a valid date (Invalid date/time format: "2020-02-30")'),
        (patient(active="yes"), "Boolean values must be 'true' or 'false'"),
        (patient(gender=None), "This property must be a simple value, not a Null (gender at Patient.gender)"),
        (patient(name=[{}]), "Object must have some content"),
        (patient(name=[{"family": ""}]), "value cannot be empty"),
        (patient(id="a b"), "Invalid Resource id: Invalid Characters ('a b')"),
        (patient(id="a" * 70), "Invalid Resource id: Too long (70 chars)"),
        (patient(gender=" male"), "The code ' male' is not valid (whitespace rules)"),
        (patient(identifier=[{"system": "not a uri", "value": "1"}]), "URI values cannot have whitespace('not a uri')"),
        (patient(identifier=[{"system": "urn:uuid:1", "value": "1"}]), "UUIDs must be valid and lowercase (1)"),
        (patient(identifier=[{"system": "http://example.org/x", "value": "1"}]),
         "Example URLs are not allowed in this context (http://example.org/x)"),
        (patient(identifier=[{"system": "urn:ietf:rfc:3986", "value": "notauri"}]),
         "if identifier.system is 'urn:ietf:rfc:3986', then the identifier.value must be a full URI (e.g. start with a scheme), not 'notauri'"),
        (patient(maritalStatus={"coding": [{"code": "M"}]}),
         "Coding has no system. A code with no system has no defined meaning, and it cannot be validated. A system should be provided"),
        (patient(language="en"),
         "Resource has a language, but the XHTML does not have an lang or an xml:lang tag (needs both - see https://www.w3.org/TR/i18n-html-tech-lang/#langvalues)"),
        (patient(managingOrganization={"reference": "#nope"}), "Unable to resolve resource with reference '#nope'"),
        (patient(contained=[{"resourceType": "Organization", "id": "o", "name": "x"}]),
         "The contained resource 'o' is not referenced to from elsewhere in the containing resource nor does it refer to the containing resource (dom-3)"),
        (patient(extension=[{"valueString": "x"}]), "Extension.url is required in order to identify, use and validate the extension"),
        ({"resourceType": "Bogus"}, "This content cannot be parsed (unknown or unrecognized resource name 'Bogus')"),
    ],
)
def test_message_matches_java(client, body, expected):
    assert expected in texts(client, body)


def test_oid_rules(client):
    ok = lambda oid: "OIDs must be valid" not in " ".join(texts(client, patient(identifier=[{"system": f"urn:oid:{oid}", "value": "1"}])))
    for good in ("1.2.3.4", "2.16.840", "2.25.123456", "1.3.6.1.4.1.19376.1.2.3"):
        assert ok(good), good
    for bad in ("1.2.3", "1.2.840", "1.0", "3.1.2.3", "1.02.3.4", "1.2.3."):
        assert not ok(bad), bad


def test_canonical_must_be_absolute(client):
    body = {"resourceType": "PlanDefinition", "text": N, "status": "draft", "library": ["Library/x"]}
    assert "Canonical URLs must be absolute URLs if they are not fragment references (Library/x)" in texts(client, body)


# ---- structure: choice types, paths, cardinality text ---------------------------------------------
def test_second_choice_type_is_unrecognized(client):
    body = {"resourceType": "Observation", "text": N, "status": "final", "code": {"text": "x"},
            "valueString": "a", "valueQuantity": {"value": 1}}
    assert "Unrecognized property 'valueQuantity'" in texts(client, body)


def test_cardinality_message_names_the_defining_profile(client):
    t = texts(client, {"resourceType": "Observation", "text": N})
    assert "Observation.status: minimum required = 1, but only found 0 (from http://hl7.org/fhir/StructureDefinition/Observation|4.0.1)" in t


def test_choice_elements_use_oftype_paths(client):
    iss = issues(client, patient(multipleBirthInteger="x"))
    exprs = {i["expression"][0] for i in iss}
    assert "Patient.multipleBirth[x]" in exprs and "Patient.multipleBirth.ofType(integer)" in exprs


def test_nested_resource_paths(client):
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": "urn:uuid:1", "resource": {"resourceType": "Patient", "id": "p1", "gender": "banana", "text": N}},
        {"fullUrl": "urn:uuid:2", "resource": {"resourceType": "Patient", "gender": "banana", "text": N}}]}
    exprs = {i["expression"][0] for i in issues(client, bundle) if i["severity"] == "error"}
    assert "Bundle.entry[0].resource/*Patient/p1*/.gender" in exprs
    assert "Bundle.entry[1].resource/*Patient/null*/.gender" in exprs


# ---- HAPI special rules ------------------------------------------------------------------------------
def test_observation_best_practice_warnings(client):
    t = texts(client, {"resourceType": "Observation", "text": N, "status": "final", "code": {"text": "x"}})
    bp = "Best Practice Recommendation: In general, all observations should have "
    assert bp + "a performer" in t and bp + "a subject" in t and bp + "an effective[x] ()" in t


def test_best_practice_suffix_on_dom_6(client):
    t = texts(client, {"resourceType": "Patient"})
    assert ("Constraint failed: dom-6: 'A resource should have narrative for robust management' "
            "(defined in http://hl7.org/fhir/StructureDefinition/DomainResource) (Best Practice Recommendation)") in t


def test_vital_signs_profile_is_applied_automatically(client):
    body = {"resourceType": "Observation", "text": N, "status": "final", "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]}}
    t = " | ".join(texts(client, body))
    assert ("Validate Observation against the Blood pressure systolic and diastolic profile "
            "(http://hl7.org/fhir/StructureDefinition/bp) which is required by the FHIR specification "
            "because the LOINC code 85354-9 was found") in t
    assert "Observation.component: minimum required = 2, but only found 0 (from http://hl7.org/fhir/StructureDefinition/bp|4.0.1)" in t
    assert "Slice 'Observation.component:SystolicBP': a matching slice is required, but not found" in t


def test_valid_blood_pressure_passes_its_auto_profile(client):
    body = {"resourceType": "Observation", "text": N, "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
            "subject": {"reference": "Patient/1"}, "performer": [{"reference": "Practitioner/1"}], "effectiveDateTime": "2020-01-01",
            "component": [
                {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]}, "valueQuantity": {"value": 120, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}},
                {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]}, "valueQuantity": {"value": 80, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]"}}]}
    assert [i for i in issues(client, body) if i["severity"] in ("error", "fatal")] == []


# ---- extensions ----------------------------------------------------------------------------------------
BIRTH_PLACE = "http://hl7.org/fhir/StructureDefinition/patient-birthPlace"


def test_extension_context_is_enforced(client):
    obs = {"resourceType": "Observation", "text": N, "status": "final", "code": {"text": "x"},
           "extension": [{"url": BIRTH_PLACE, "valueAddress": {"city": "X"}}]}
    assert (f"The extension {BIRTH_PLACE} is not allowed to be used at this point (allowed = e:Patient; this element is [Observation])"
            in texts(client, obs))
    ok = patient(extension=[{"url": BIRTH_PLACE, "valueAddress": {"city": "X"}}])
    assert not [t for t in texts(client, ok) if "not allowed to be used" in t]


def test_extension_value_type_is_enforced(client):
    t = texts(client, patient(extension=[{"url": BIRTH_PLACE, "valueString": "x"}]))
    assert f"The Extension '{BIRTH_PLACE}' definition allows for the types [Address] but found type string" in t


def test_unknown_extension_is_information(client):
    iss = issues(client, patient(extension=[{"url": "http://foo.org/x", "valueString": "x"}]))
    unknown = [i for i in iss if i["details"]["text"] == "Unknown extension http://foo.org/x"]
    assert unknown and unknown[0]["severity"] == "information"


# ---- recursive structures (contentReference) -----------------------------------------------------------
def test_nested_questionnaire_items_are_validated(client):
    q = {"resourceType": "Questionnaire", "text": N, "status": "draft",
         "item": [{"linkId": "1", "type": "group", "item": [{"type": "string"}]}]}
    assert "Questionnaire.item.linkId: minimum required = 1, but only found 0 (from http://hl7.org/fhir/StructureDefinition/Questionnaire|4.0.1)" in texts(client, q)


# ---- bundles ---------------------------------------------------------------------------------------------
def test_bundle_fullurl_must_match_type_and_id(client):
    b = {"resourceType": "Bundle", "type": "collection",
         "entry": [{"fullUrl": "http://x.org/fhir/Patient/2", "resource": {"resourceType": "Patient", "id": "1", "text": N}}]}
    assert ("The fullUrl 'http://x.org/fhir/Patient/2' looks like a RESTful server URL, so it must end with the correct "
            "type and id (/Patient/1)") in texts(client, b)


# ---- output shape -----------------------------------------------------------------------------------------
def test_outcome_shape_matches_java(client):
    r = client.post("/validate", json={"resourceType": "Patient", "foo": 1}).json()
    assert r["text"]["status"] == "generated" and "<table" in r["text"]["div"]
    i = r["issue"][0]
    assert "location" not in i
    sources = [e for e in i["extension"] if e["url"].endswith("issue-source")]
    assert sources[0]["valueString"] == "InstanceValidator"
    dom6 = [x for x in r["issue"] if "dom-6" in x["details"]["text"]][0]
    mid = [e for e in dom6["extension"] if e["url"].endswith("operationoutcome-message-id")][0]
    assert mid["valueCode"] == "http://hl7.org/fhir/StructureDefinition/DomainResource#dom-6"


def test_json_positions_follow_java(client):
    doc = '{\n  "resourceType": "Patient",\n  "gender": "banana",\n  "foo": 1\n}'
    iss = client.post("/validate", content=doc.encode()).json()["issue"]
    pos = {i["details"]["text"][:12]: [(e["valueInteger"]) for e in i["extension"] if e["url"].endswith(("line", "col"))] for i in iss}
    assert pos["The value pr"] == [3, 21]  # just after the offending value
    assert pos["Unrecognized"] == [4, 11]


def test_one_line_root_position(client):
    iss = client.post("/validate", content=b'{"resourceType":"Patient"}').json()["issue"]
    dom6 = [i for i in iss if "dom-6" in i["details"]["text"]][0]
    assert [e["valueInteger"] for e in dom6["extension"] if e["url"].endswith(("line", "col"))] == [1, 27]
