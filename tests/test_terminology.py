"""Terminology-server answers are classified correctly (no network: the server is faked)."""
from app.fhir.terminology import INVALID, OK, UNKNOWN, TerminologyService
from app.services.profile_registry import ProfileRegistry

VS = "https://fhir.test.local/ValueSet/conditions"


class _FakeResponse:
    def __init__(self, params):
        self._params = params

    def json(self):
        return {"resourceType": "Parameters", "parameter": self._params}


class _FakeClient:
    def __init__(self, params):
        self.params = params

    def post(self, url, json=None):
        return _FakeResponse(self.params)


def service(params):
    term = TerminologyService(ProfileRegistry(load_base=False), "http://tx.fhir.test.local", disabled=False)
    term._client = _FakeClient(params)
    return term


def ask(params):
    return service(params).validate_in_valueset(VS, "http://snomed.info/sct", "22298006")


def test_code_outside_a_custom_value_set_is_invalid_not_unknown():
    msg = "The provided code 'http://snomed.info/sct#22298006' was not found in the value set '" + VS + "'"
    res = ask([{"name": "result", "valueBoolean": False}, {"name": "message", "valueString": msg}])
    assert res.status == INVALID


def test_code_inside_the_value_set_is_ok():
    assert ask([{"name": "result", "valueBoolean": True}]).status == OK


def test_missing_code_system_or_value_set_is_unknown():
    for msg in ("A definition for CodeSystem 'http://x.test' could not be found, so the code cannot be validated",
                "Unable to find value set 'https://x.test/vs'",
                "ValueSet 'https://x.test/vs' not found"):
        assert ask([{"name": "result", "valueBoolean": False}, {"name": "message", "valueString": msg}]).status == UNKNOWN, msg


def test_unknown_code_in_a_known_system_is_invalid():
    res = ask([{"name": "result", "valueBoolean": False}, {"name": "message", "valueString": "Unknown code 'x' in the CodeSystem 'http://y'"}])
    assert res.status == INVALID
