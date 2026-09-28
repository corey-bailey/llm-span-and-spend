import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from agent.app import create_app
from agent.loop import AgentIncompleteError, AgentLoopLimitError, AgentResult


class StubAgent:
    def __init__(self, outcome):
        self._outcome = outcome

    def run(self, question):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def client_for(outcome):
    return TestClient(create_app(agent=StubAgent(outcome)))


def api_error(cls, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("UPSTREAM-DETAIL", response=httpx.Response(status, request=req), body=None)


def test_ask_returns_answer_and_usage():
    r = client_for(AgentResult("Tent is $289.", 2, 3000, 70, 0.0067)).post(
        "/ask", json={"question": "tent price?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Tent is $289."
    assert body["turns"] == 2
    assert body["cost_usd"] == pytest.approx(0.0067)


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "x" * 2001}])
def test_ask_rejects_bad_input(payload):
    assert client_for(AgentResult("", 1, 0, 0, 0)).post("/ask", json=payload).status_code == 422


@pytest.mark.parametrize("exc,status", [
    (api_error(anthropic.RateLimitError, 429), 429),
    (api_error(anthropic.InternalServerError, 500), 502),
    (AgentLoopLimitError("UPSTREAM-DETAIL"), 500),
    (AgentIncompleteError("max_tokens"), 502),
])
def test_ask_maps_failures_to_status_codes(exc, status):
    r = client_for(exc).post("/ask", json={"question": "hi"})
    assert r.status_code == status
    assert "UPSTREAM-DETAIL" not in r.text  # no upstream detail leaks


def test_healthz():
    assert client_for(AgentResult("", 1, 0, 0, 0)).get("/healthz").json() == {"status": "ok"}


def test_connection_error_maps_to_502():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    r = client_for(anthropic.APIConnectionError(request=req)).post("/ask", json={"question": "hi"})
    assert r.status_code == 502


def test_build_app_wires_real_dependencies(monkeypatch):
    import logging
    from agent.app import build_app
    from agent.loop import AGENT_NAME
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    logger = logging.getLogger(AGENT_NAME)
    before = list(logger.handlers)
    try:
        assert TestClient(build_app()).get("/healthz").status_code == 200
        assert len(logger.handlers) == len(before) + 1  # OTLP log handler attached
    finally:
        logger.handlers[:] = before  # don't leak a live exporter into later tests
