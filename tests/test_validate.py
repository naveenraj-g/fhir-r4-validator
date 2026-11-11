from conftest import errors_of, messages_of

US_CORE_PATIENT = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"
NARRATIVE = {"status": "generated", "div": "<div xmlns='http://www.w3.org/1999/xhtml'>x</div>"}


def post(client, body=None, profile=None, **kw):
    url = "/validate" + (f"?profile={profile}" if profile else "")
    r = client.post(url, json=body, **kw) if body is not None else client.post(url, **kw)
    assert r.status_code == 200
    out = r.json()
    assert out["resourceType"] == "OperationOutcome"
    return out


def patient(**extra):
    return {"resourceType": "Patient", "text": NARRATIVE, **extra}


# ---- the original smoke tests -------------------------------------------------
def test_validate_us_core_patient(client, load_fixture):
    out = post(client, profile=US_CORE_PATIENT, content=load_fixture("us_core_patient_example.json"))
    assert errors_of(out) == []


def test_validate_multiple_profiles(client, load_fixture):
    profiles = f"{US_CORE_PATIENT},http://hl7.org/fhir/StructureDefinition/Patient"
    out = post(client, profile=profiles, content=load_fixture("us_core_patient_example.json"))
    assert errors_of(out) == []


def test_validate_valid_resource_is_all_ok(client):
    out = post(client, patient(gender="male"))
    assert [i["severity"] for i in out["issue"]] == ["information"]
    assert out["issue"][0]["details"]["text"] == "All OK"


# ---- structure ----------------------------------------------------------------
def test_wrong_primitive_type(client):
    out = post(client, patient(gender=12345))
    assert "must be a string" in messages_of(out)


def test_unknown_property(client):
    out = post(client, patient(foo=1))
    assert "Unrecognized property 'foo'" in messages_of(out)


def test_invalid_date(client):
    assert "Not a valid date ('not-a-date' doesn't meet format requirements for date)" in messages_of(post(client, patient(birthDate="not-a-date")))
    assert 'Not a valid date (Invalid date/time format: "2020-02-30")' in messages_of(post(client, patient(birthDate="2020-02-30")))


def test_unknown_resource_type(client):
    out = post(client, {"resourceType": "Bogus"})
    assert out["issue"][0]["severity"] == "fatal"
    assert "unknown or unrecognized resource name 'Bogus'" in messages_of(out)


def test_missing_resource_type(client):
    out = post(client, {"gender": "male"})
    assert out["issue"][0]["severity"] == "fatal"


def test_empty_and_garbage_bodies(client):
    for body in (b"", b"{not json", b"plain text"):
        out = post(client, content=body)
        assert out["issue"][0]["severity"] == "fatal"


def test_min_cardinality(client):
    out = post(client, {"resourceType": "Observation"})
    msgs = messages_of(out)
    assert "Observation.status: minimum required = 1, but only found 0" in msgs
    assert "Observation.code: minimum required = 1, but only found 0" in msgs


def test_max_cardinality_and_choice_types(client):
    out = post(client, {"resourceType": "Observation", "status": "final", "code": {"text": "x"},
                        "valueString": "a", "valueQuantity": {"value": 1}})
    assert "Unrecognized property 'valueQuantity'" in messages_of(out)  # HAPI: a second choice type is unrecognized


def test_array_vs_scalar(client):
    out = post(client, patient(name={"family": "x"}))
    assert "must be a JSON Array" in messages_of(out)
    out = post(client, patient(gender=["male"]))
    assert errors_of(out)


def test_invariant_failure(client):
    out = post(client, patient(contact=[{"gender": "male"}]))
    assert "pat-1" in messages_of(out)


def test_invariant_warning_is_a_warning(client):
    out = post(client, {"resourceType": "Patient"})
    dom6 = [i for i in out["issue"] if "dom-6" in i["details"]["text"]]
    assert dom6 and dom6[0]["severity"] == "warning"


def test_contained_must_be_referenced(client):
    out = post(client, patient(contained=[{"resourceType": "Organization", "id": "o1", "name": "x"}]))
    assert "dom-3" in messages_of(out)
    ok = post(client, patient(contained=[{"resourceType": "Organization", "id": "o1", "name": "x"}],
                              managingOrganization={"reference": "#o1"}))
    assert "dom-3" not in messages_of(ok)


# ---- profiles -----------------------------------------------------------------
def test_unknown_profile_is_an_error(client):
    out = post(client, patient(), profile="http://fhir.test.local/nope")
    assert any(i["severity"] == "error" and "Unable to locate the profile" in i["details"]["text"] for i in out["issue"])


def test_unresolvable_meta_profile_is_a_warning(client):
    out = post(client, patient(meta={"profile": ["http://fhir.test.local/nope"]}))
    assert errors_of(out) == []
    assert "could not be found" in messages_of(out)


def test_profile_for_wrong_type(client):
    out = post(client, {"resourceType": "Observation", "status": "final", "code": {"text": "x"}}, profile=US_CORE_PATIENT)
    assert "Specified profile type was 'Patient'" in messages_of(out)


def test_us_core_required_elements(client):
    out = post(client, patient(), profile=US_CORE_PATIENT)
    msgs = messages_of(out)
    for el in ("Patient.identifier", "Patient.name", "Patient.gender"):
        assert f"{el}: minimum required = 1" in msgs


def test_us_core_extension_profile_is_applied(client):
    body = patient(identifier=[{"system": "x", "value": "1"}], name=[{"family": "a"}], gender="male",
                   extension=[{"url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
                               "extension": [{"url": "bogus", "valueString": "x"}]}])
    assert "Slice 'Extension.extension:text': a matching slice is required, but not found" in messages_of(post(client, body, profile=US_CORE_PATIENT))


def test_meta_profile_is_validated(client):
    out = post(client, patient(meta={"profile": [US_CORE_PATIENT]}))
    assert "Patient.identifier: minimum required = 1" in messages_of(out)


# ---- terminology (local value sets) -------------------------------------------
def test_required_binding_violation(client):
    out = post(client, patient(gender="banana"))
    err = errors_of(out)
    assert err and "administrative-gender" in err[0]["details"]["text"]
    assert err[0]["expression"] == ["Patient.gender"]


def test_valid_code_passes(client):
    assert errors_of(post(client, patient(gender="female"))) == []


def test_codeable_concept_required_binding(client):
    base = {"resourceType": "Encounter", "text": NARRATIVE, "status": "finished",
            "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"}}
    assert errors_of(post(client, base)) == []
    bad = dict(base, status="not-a-status")
    assert errors_of(post(client, bad))


def test_unknown_code_in_known_code_system(client):
    body = {"resourceType": "Observation", "text": NARRATIVE, "status": "final",
            "code": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "nonsense"}]}}
    assert "Unknown code 'nonsense'" in messages_of(post(client, body))


def test_wrong_display_is_error_when_code_system_is_local(client):
    body = {"resourceType": "Observation", "text": NARRATIVE, "status": "final", "code": {"text": "x"},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                      "code": "vital-signs", "display": "Totally Wrong"}]}]}
    out = post(client, body)
    assert "Wrong Display Name 'Totally Wrong'" in messages_of(out)
    assert any(i["severity"] == "error" for i in out["issue"] if "Wrong Display" in i["details"]["text"])


def test_unknown_code_system_is_only_a_warning_without_tx(client):
    body = {"resourceType": "Observation", "text": NARRATIVE, "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": "1234-5"}]}}
    out = post(client, body)
    assert errors_of(out) == []
    assert "A definition for CodeSystem 'http://loinc.org' could not be found" in messages_of(out)


# ---- references ---------------------------------------------------------------
def test_reference_target_type(client):
    # like Java (CHECK_TYPE_IF_EXISTS) the type is only checked when the target can be resolved
    body = {"resourceType": "Observation", "text": NARRATIVE, "status": "final", "code": {"text": "x"},
            "contained": [{"resourceType": "Organization", "id": "o", "name": "x"}], "subject": {"reference": "#o"}}
    assert "Invalid Resource target type. Found Organization" in messages_of(post(client, body))
    body["subject"] = {"reference": "Organization/1"}  # unresolvable -> not checked
    assert "Invalid Resource target type" not in messages_of(post(client, body))


def test_contained_reference_must_resolve(client):
    body = patient(managingOrganization={"reference": "#missing"})
    assert "Unable to resolve resource with reference '#missing'" in messages_of(post(client, body))


# ---- narrative ----------------------------------------------------------------
def test_bad_narrative(client):
    body = {"resourceType": "Patient", "text": {"status": "generated", "div": "<div xmlns='http://www.w3.org/1999/xhtml'><script>x</script></div>"}}
    assert "Invalid element name in the XHTML ('script')" in messages_of(post(client, body))


# ---- bundles ------------------------------------------------------------------
def test_bundle_entries_are_validated(client):
    bundle = {"resourceType": "Bundle", "type": "collection",
              "entry": [{"fullUrl": "urn:uuid:1", "resource": {"resourceType": "Patient", "gender": "banana", "text": NARRATIVE}}]}
    out = post(client, bundle)
    assert any("Bundle.entry[0].resource/*Patient/null*/.gender" in (i.get("expression") or [""])[0] for i in errors_of(out))


# ---- positions & output shape ---------------------------------------------------
def test_json_line_and_column_are_reported(client):
    text = '{\n  "resourceType": "Patient",\n  "gender": 5\n}'
    r = client.post("/validate", content=text.encode())
    issue = [i for i in r.json()["issue"] if i["severity"] == "error"][0]
    exts = {e["url"].rsplit("-", 1)[-1]: e for e in issue["extension"]}
    assert exts["line"]["valueInteger"] == 3
    assert exts["source"]["valueString"] == "InstanceValidator"
    assert "location" not in issue
