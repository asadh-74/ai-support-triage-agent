"""Tests use a scripted fake LLM, so they are free, fast and deterministic.

They prove the *safety behaviour* of the system, which is what matters in production:
  - grounded, confident answers are sent automatically
  - low-confidence or unsupported answers are escalated
  - legal/fraud tickets never reach the LLM
  - if the AI service dies, tickets go to a human instead of being lost
"""
import json

import pytest

from app import agent, llm, tools
from app.schemas import TicketIn

TRIAGE = json.dumps({"category": "refund", "urgency": "medium", "sentiment": "neutral"})
SEARCH = json.dumps({"thought": "check refund policy", "action": "search_kb", "input": "refund processing time"})


def final(conf, text="Refunds take 5 to 7 business days.\n\nShopNova Support"):
    return json.dumps({"thought": "policy found", "action": "final_reply", "input": text, "confidence": conf})


@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setenv("KB_DIR", "kb")

    def no_embeddings(texts):
        raise llm.LLMUnavailable("embeddings disabled in tests")

    monkeypatch.setattr(llm, "embed", no_embeddings)  # forces the keyword fallback path
    tools.get_kb.cache_clear()
    yield
    tools.get_kb.cache_clear()


def script(monkeypatch, replies):
    it = iter(replies)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: next(it))


def ticket(body="Where is my refund? I returned the item last week."):
    return TicketIn(subject="Refund", body=body)


def test_grounded_confident_answer_is_sent(monkeypatch):
    script(monkeypatch, [TRIAGE, SEARCH, final(0.92)])
    r = agent.handle_ticket(ticket())
    assert r.action == "auto_reply"
    assert r.confidence == 0.92
    assert r.sources and "refunds_returns.md" in r.sources[0]
    assert [t["action"] for t in r.trace] == ["triage", "search_kb", "final_reply"]


def test_low_confidence_is_escalated(monkeypatch):
    script(monkeypatch, [TRIAGE, SEARCH, final(0.4)])
    r = agent.handle_ticket(ticket())
    assert r.action == "escalate"
    assert "below" in r.reason


def test_answer_without_evidence_is_escalated(monkeypatch):
    # Model tries to answer immediately without searching -> could be a hallucination
    script(monkeypatch, [TRIAGE, final(0.99)])
    r = agent.handle_ticket(ticket())
    assert r.action == "escalate"
    assert "not backed" in r.reason


def test_agent_can_choose_to_escalate(monkeypatch):
    esc = json.dumps({"thought": "needs sales", "action": "escalate", "input": "Bulk pricing is a sales decision"})
    script(monkeypatch, [TRIAGE, esc])
    r = agent.handle_ticket(ticket())
    assert r.action == "escalate"
    assert "sales" in r.reason.lower()


def test_order_lookup_counts_as_evidence(monkeypatch):
    lookup = json.dumps({"thought": "check order", "action": "lookup_order", "input": "ORD-1002"})
    script(monkeypatch, [TRIAGE, lookup, final(0.9, "Your order is delayed, we're investigating.")])
    r = agent.handle_ticket(ticket("Where is ORD-1002?"))
    assert r.action == "auto_reply"


def test_legal_threat_never_reaches_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LLM must not be called for legal/fraud tickets")

    monkeypatch.setattr(llm, "chat", boom)
    r = agent.handle_ticket(ticket("I will contact my lawyer about this."))
    assert r.action == "escalate"
    assert r.trace[0]["action"] == "guardrail"


def test_llm_outage_fails_safe_to_human(monkeypatch):
    def down(*a, **k):
        raise llm.LLMUnavailable("503")

    monkeypatch.setattr(llm, "chat", down)
    r = agent.handle_ticket(ticket())
    assert r.action == "escalate"
    assert "unavailable" in r.reason.lower()


def test_garbage_model_output_is_survivable(monkeypatch):
    script(monkeypatch, [TRIAGE, "sure! here you go", SEARCH, final(0.9)])
    r = agent.handle_ticket(ticket())
    assert r.action == "auto_reply"
    assert any(t["action"] == "invalid_output" for t in r.trace)


def test_step_limit_prevents_infinite_loops(monkeypatch):
    script(monkeypatch, [TRIAGE] + [SEARCH] * 10)
    r = agent.handle_ticket(ticket())
    assert r.action == "escalate"
    assert "step limit" in r.reason


def test_redact_pii():
    out = agent.redact_pii("Card 4111 1111 1111 1111, mail me at jo@example.com or +1 415 555 0100")
    assert "4111" not in out and "example.com" not in out and "555" not in out
    assert "[CARD]" in out and "[EMAIL]" in out and "[PHONE]" in out


def test_parse_json_handles_code_fences():
    assert agent.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert agent.parse_json("no json here") is None
