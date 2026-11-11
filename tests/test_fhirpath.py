import json


def test_fhirpath_evaluate_empty(client, load_fixture):
    patient_bytes = load_fixture("patient_fixture.json")
    response = client.post(
        "/evaluate?path=Patient.foo",
        content=patient_bytes,
        headers={"Content-Type": "application/fhir+json"},
    )
    assert response.status_code == 200
    assert response.json() == []


def test_fhirpath_evaluate_substring(client, load_fixture):
    patient_bytes = load_fixture("patient_fixture.json")
    response = client.post(
        "/evaluate?path=Patient.id.substring(1,3)",
        content=patient_bytes,
        headers={"Content-Type": "application/fhir+json"},
    )
    assert response.status_code == 200
    assert response.json() == [{"type": "string", "element": "234"}]


def test_fhirpath_evaluate_given_names(client, load_fixture):
    patient_bytes = load_fixture("patient_fixture.json")
    response = client.post(
        "/evaluate?path=Patient.name.given",
        content=patient_bytes,
        headers={"Content-Type": "application/fhir+json"},
    )
    assert response.status_code == 200
    assert response.json() == [
        {"type": "string", "element": "A"},
        {"type": "string", "element": "B"},
        {"type": "string", "element": "C"},
    ]


def test_fhirpath_evaluate_name_element(client, load_fixture):
    patient_bytes = load_fixture("patient_fixture.json")
    response = client.post(
        "/evaluate?path=Patient.name",
        content=patient_bytes,
        headers={"Content-Type": "application/fhir+json"},
    )
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 3
    assert items[0]["type"] == "HumanName"
    assert items[0]["element"] == {"given": ["A"]}


def test_fhirpath_evaluate_communication_filter(client, load_fixture):
    patient_bytes = load_fixture("patient_fixture.json")
    response = client.post(
        "/evaluate?path=Patient.communication.where(preferred = true)",
        content=patient_bytes,
        headers={"Content-Type": "application/fhir+json"},
    )
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    assert items[0]["type"] == "Patient.communication"
    assert items[0]["element"]["preferred"] is True
    assert items[0]["element"]["language"]["coding"][0]["code"] == "en-US"


# ---- types, choice types, bare datatypes, errors ----------------------------------------------------
def test_result_types_follow_the_fhir_model(client):
    obs = {"resourceType": "Observation", "status": "final", "code": {"text": "x"}, "effectiveDateTime": "2020-01-01",
           "valueQuantity": {"value": 1.50, "unit": "mg"}}
    r = client.post("/evaluate?path=Observation.status", json=obs)
    assert r.json() == [{"type": "code", "element": "final"}]
    r = client.post("/evaluate?path=Observation.effective", json=obs)  # choice type resolved
    assert r.json() == [{"type": "dateTime", "element": "2020-01-01"}]
    r = client.post("/evaluate?path=Observation.value", json=obs)
    assert r.json()[0]["type"] == "Quantity"


def test_decimal_precision_is_preserved(client):
    obs = {"resourceType": "Observation", "status": "final", "code": {"text": "x"}, "valueQuantity": {"value": 1.50}}
    r = client.post("/evaluate?path=Observation.value.value", content=json.dumps(obs).replace("1.5", "1.50").encode())
    assert r.text == '[{"type":"decimal","element":1.50}]'


def test_date_primitive_type(client):
    r = client.post("/evaluate?path=Patient.birthDate", json={"resourceType": "Patient", "birthDate": "2000-01-01"})
    assert r.json() == [{"type": "date", "element": "2000-01-01"}]


def test_bare_datatype_with_type_param(client):
    r = client.post("/evaluate?path=given&type=HumanName", json={"family": "X", "given": ["a", "b"]})
    assert r.json() == [{"type": "string", "element": "a"}, {"type": "string", "element": "b"}]


def test_backbone_element_with_type_param(client):
    r = client.post("/evaluate?path=language&type=Patient.communication",
                    json={"language": {"coding": [{"code": "en"}]}, "preferred": True})
    assert r.json() == [{"type": "CodeableConcept", "element": {"coding": [{"code": "en"}]}}]


def test_boolean_and_integer_results(client):
    p = {"resourceType": "Patient", "active": True, "name": [{"given": ["a", "b"]}]}
    assert client.post("/evaluate?path=Patient.active", json=p).json() == [{"type": "boolean", "element": True}]
    assert client.post("/evaluate?path=Patient.name.given.count()", json=p).json() == [{"type": "integer", "element": 2}]


def test_xml_body(client):
    xml = '<Patient xmlns="http://hl7.org/fhir"><name><given value="J"/></name></Patient>'
    r = client.post("/evaluate?path=Patient.name.given", content=xml.encode())
    assert r.json() == [{"type": "string", "element": "J"}]


def test_invalid_expression_is_a_client_error(client):
    r = client.post("/evaluate?path=Patient.name.(", json={"resourceType": "Patient"})
    assert r.status_code == 400
    assert r.json()["resourceType"] == "OperationOutcome"


def test_missing_type_for_bare_element(client):
    r = client.post("/evaluate?path=given", json={"given": ["a"]})
    assert r.status_code == 400


def test_unknown_type_param(client):
    assert client.post("/evaluate?path=x&type=Nope", json={"a": 1}).status_code == 400
    assert client.post("/evaluate?path=x&type=Patient.nope", json={"a": 1}).status_code == 400
    assert client.post("/evaluate?path=x&type=string", json={"a": 1}).status_code == 400


def test_missing_path_param(client):
    assert client.post("/evaluate", json={"resourceType": "Patient"}).status_code == 422
