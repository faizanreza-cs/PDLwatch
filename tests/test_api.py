import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdlwatch.api import app

client = TestClient(app)


def _pdf(built, family: str) -> Path:
    doc = next(d for d in built["truth"]["documents"]
               if d["family"] == family and not d["scanned"])  # fmt: skip
    return built["root"] / "corpus" / doc["file"]


def test_read_endpoints(built):
    assert client.get("/health").json()["data"]["source"] == "synthetic"
    state = built["truth"]["documents"][0]["state"]
    body = client.get(f"/states/{state}").json()
    assert body["row_count"] > 0 and all("p." in r["source"] for r in body["rows"])
    product = body["rows"][0]["product"]
    assert client.get(f"/drugs/{product}").json()["row_count"] >= 1
    assert client.get("/drugs/not-a-drug").status_code == 404
    assert client.get("/states/Atlantis").status_code == 404
    assert client.get("/classes/GLP-1").json()["rows"]
    assert client.get("/changes").json()["row_count"] > 0
    assert client.get("/review", params={"limit": 5}).json()["row_count"] <= 5
    assert client.get("/review", params={"limit": 500}).status_code == 422
    assert client.get("/documents").json()["row_count"] == len(built["truth"]["documents"])
    assert "overall" in {r["group"] for r in client.get("/metrics").json()["rows"]}
    names = [q["name"] for q in client.get("/queries").json()["rows"]]
    assert "state_scorecard" in names
    assert client.get("/queries/state_scorecard").json()["rows"]
    assert client.get("/queries/nope").status_code == 404
    r = client.post("/ask", json={"question": f"What does {state} cover?"})
    assert r.status_code == 200 and r.json()["tools_used"]


def test_extract_an_uploaded_pdf(built):
    path = _pdf(built, "grid")
    r = client.post("/extract", files={"file": (path.name, path.read_bytes(), "application/pdf")})
    assert r.status_code == 200
    body = r.json()
    assert body["document"]["doc_type"] == "pdl" and body["document"]["path"] is None
    assert len(body["rows"]) > 50 and all(row["page"] >= 1 for row in body["rows"])


@pytest.mark.parametrize(
    ("data", "status"),
    [(b"hello", 415), (b"%PDF-1.7 not really a pdf", 422)],
)
def test_extract_rejects_what_it_cant_read(data, status):
    r = client.post("/extract", files={"file": ("x.pdf", data, "application/pdf")})
    assert r.status_code == status


def test_extract_size_limit(monkeypatch):
    import pdlwatch.api as api

    monkeypatch.setattr(api, "MAX_UPLOAD_MB", 0)
    r = client.post("/extract", files={"file": ("x.pdf", b"%PDF-1.7", "application/pdf")})
    assert r.status_code == 413


def test_api_key_is_enforced_when_set(monkeypatch, built):
    monkeypatch.setenv("PDLWATCH_API_TOKEN", "s3cret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/metrics", headers={"X-API-Key": "s3cret"}).status_code == 200
    odd = {"X-API-Key": "café".encode("latin-1")}  # non-ASCII: a clean 401, not a 500
    assert client.get("/metrics", headers=odd).status_code == 401


def test_each_customer_has_a_key_and_the_log_names_them(monkeypatch, built, caplog):
    monkeypatch.setenv("PDLWATCH_API_KEYS", "acme:key-acme, beta:key-beta")
    monkeypatch.setenv("PDLWATCH_API_TOKEN", "s3cret")  # still works, as "default"
    with caplog.at_level("INFO", logger="pdlwatch.access"):
        assert client.get("/metrics", headers={"X-API-Key": "key-beta"}).status_code == 200
        assert client.get("/metrics", headers={"X-API-Key": "s3cret"}).status_code == 200
        assert client.get("/metrics", headers={"X-API-Key": "key-gamma"}).status_code == 401
    logged = [json.loads(r.getMessage()) for r in caplog.records]
    assert [(e["customer"], e["status"]) for e in logged] == [
        ("beta", 200), ("default", 200), (None, 401)]  # fmt: skip
    text = " ".join(r.getMessage() for r in caplog.records)
    assert not any(k in text for k in ("key-acme", "key-beta", "key-gamma", "s3cret"))
