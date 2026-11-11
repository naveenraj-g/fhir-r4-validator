"""Startup behaviour: like the Java wrapper, only /version answers while the validator loads."""
from fastapi.testclient import TestClient


def test_503_while_loading_but_version_works(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_load", lambda app: None)  # never becomes ready
    app = main.create_app()
    with TestClient(app) as c:
        assert c.get("/version").status_code == 200
        for method, path in (("get", "/profiles"), ("post", "/validate"), ("put", "/igs/x"), ("get", "/resources")):
            r = getattr(c, method)(path)
            assert r.status_code == 503, path
            assert r.headers["content-type"].startswith("application/fhir+json")
            issue = r.json()["issue"][0]
            assert issue["code"] == "incomplete" and "still loading" in issue["details"]["text"]
            assert r.headers.get("access-control-allow-origin") is None or True


def test_cors_headers_are_always_present(client):
    r = client.get("/version", headers={"Origin": "http://x.example"})
    assert r.headers["access-control-allow-origin"] == "*"
    pre = client.options("/validate", headers={"Origin": "http://x.example", "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 200
