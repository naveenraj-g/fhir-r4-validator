import gzip
import io
import json
import tarfile

from conftest import errors_of, messages_of


def make_package(pkg_id="example.fhir.core", version="1.0.0", with_index=False, extra=None):
    sd = {
        "resourceType": "StructureDefinition", "id": "example-patient", "url": f"http://fhir.test.local/fhir/{pkg_id}/StructureDefinition/example-patient",
        "name": "ExamplePatient", "status": "active", "kind": "resource", "abstract": False, "type": "Patient",
        "baseDefinition": "http://hl7.org/fhir/StructureDefinition/Patient", "derivation": "constraint",
        "differential": {"element": [{"id": "Patient.gender", "path": "Patient.gender", "min": 1}]},
    }
    files = {
        "package/package.json": json.dumps({"name": pkg_id, "version": version, "canonical": f"http://fhir.test.local/fhir/{pkg_id}",
                                            "dependencies": {"hl7.fhir.r4.core": "4.0.1"}}),
        "package/StructureDefinition-example-patient.json": json.dumps(sd),
    }
    files.update(extra or {})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_get_known_igs(client):
    igs = client.get("/igs").json()
    assert isinstance(igs, dict)
    assert "hl7.fhir.r4.core" in igs  # built-in fallback when the registry catalogue is unreachable


def test_get_profiles_by_ig(client):
    by_ig = client.get("/profiles-by-ig").json()
    assert len(by_ig["hl7.fhir.us.core"]) > 0
    assert "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient" in by_ig["hl7.fhir.us.core"]


def test_load_custom_ig_package(client, load_fixture):
    r = client.post("/igs", content=load_fixture("hl7.fhir.au.base.tgz"), headers={"Content-Type": "application/gzip"})
    assert r.status_code == 200
    ig = r.json()
    assert ig["id"] == "hl7.fhir.au.base" and ig["version"] and len(ig["profiles"]) > 0
    assert ig["profiles"] == sorted(ig["profiles"])
    assert any("au" in url for url in client.get("/profiles").json())
    assert "hl7.fhir.au.base" in client.get("/igs").json()
    assert "hl7.fhir.au.base" in client.get("/profiles-by-ig").json()


def test_custom_ig_response_and_profile_is_usable(client):
    r = client.post("/igs", content=make_package())
    assert r.status_code == 200
    assert r.json() == {"id": "example.fhir.core", "version": "1.0.0",
                        "profiles": ["http://fhir.test.local/fhir/example.fhir.core/StructureDefinition/example-patient"]}
    out = client.post("/validate?profile=http://fhir.test.local/fhir/example.fhir.core/StructureDefinition/example-patient",
                      json={"resourceType": "Patient"}).json()
    assert "Patient.gender: minimum required = 1" in messages_of(out)


def test_double_gzipped_body_is_accepted(client):
    # clients that send 'Content-Encoding: gzip' on top of the .tgz produce a doubly gzipped body
    r = client.post("/igs", content=gzip.compress(make_package("example.double", "2.0.0")))
    assert r.status_code == 200 and r.json()["id"] == "example.double"


def test_garbage_package_is_a_client_error(client):
    r = client.post("/igs", content=b"definitely not a tarball")
    assert r.status_code == 400
    assert r.json()["resourceType"] == "OperationOutcome"


def test_package_without_package_json_is_rejected(client):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("package/x.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    assert client.post("/igs", content=buf.getvalue()).status_code == 400


def test_put_ig_already_loaded_does_not_need_network(client):
    r = client.put("/igs/hl7.fhir.us.core?version=3.1.1")
    assert r.status_code == 200
    assert r.json()["version"] == "3.1.1"


def test_put_unknown_ig_is_404_when_offline(client):
    r = client.put("/igs/does.not.exist?version=9.9.9")
    assert r.status_code == 404
    assert r.json()["resourceType"] == "OperationOutcome"
