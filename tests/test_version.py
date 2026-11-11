import re

def test_get_version(client):
    response = client.get("/version")
    assert response.status_code == 200
    data = response.json()
    assert "org.hl7.fhir.validation" in data
    assert "inferno-framework/fhir-validator-wrapper" in data
    assert data["org.hl7.fhir.validation"] == "6.5.18"
    assert re.fullmatch(r"\d+\.\d+\.\d+", data["inferno-framework/fhir-validator-wrapper"])
