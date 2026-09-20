import pytest
from fastapi.testclient import TestClient

from app import tools
from app.main import app, _hits

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)  # run in demo mode (no external calls)
    monkeypatch.setenv("KB_DIR", "kb")
    monkeypatch.delenv("API_KEY", raising=False)
    tools.get_kb.cache_clear()
    _hits.clear()
    yield
    tools.get_kb.cache_clear()


def test_health_reports_demo_mode():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["mode"] == "demo"


def test_homepage_served():
    r = client.get("/")
    assert r.status_code == 200 and "Support Triage Agent" in r.text


def test_triage_requires_api_key_when_configured(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    body = {"subject": "Refund", "body": "Where is my refund for the returned order?"}
    assert client.post("/api/triage", json=body).status_code == 401
    assert client.post("/api/triage", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    ok = client.post("/api/triage", json=body, headers={"X-API-Key": "secret"})
    assert ok.status_code == 200
    assert ok.json()["action"] in {"auto_reply", "escalate"}


def test_refund_ticket_is_answered_in_demo_mode():
    r = client.post("/api/triage", json={
        "subject": "Refund hasn't arrived",
        "body": "I returned my jacket 9 days ago (order ORD-1003), when will my refund be processed?"})
    data = r.json()
    assert data["action"] == "auto_reply"
    assert data["category"] == "refund"
    assert data["mode"] == "demo"


def test_legal_ticket_is_escalated():
    r = client.post("/api/triage", json={"subject": "Broken", "body": "I'm calling my lawyer about this order."})
    assert r.json()["action"] == "escalate"


def test_input_validation():
    assert client.post("/api/triage", json={"subject": "", "body": "x"}).status_code == 422


def test_demo_endpoint_is_rate_limited(monkeypatch):
    monkeypatch.setenv("DEMO_RATE_LIMIT_PER_MIN", "2")
    body = {"subject": "Hi", "body": "How long does shipping take?"}
    assert client.post("/api/demo", json=body).status_code == 200
    assert client.post("/api/demo", json=body).status_code == 200
    assert client.post("/api/demo", json=body).status_code == 429


def test_metrics_counts_tickets():
    before = client.get("/api/metrics").json()["total"]
    client.post("/api/triage", json={"subject": "Hi", "body": "How long does shipping take?"})
    assert client.get("/api/metrics").json()["total"] == before + 1
