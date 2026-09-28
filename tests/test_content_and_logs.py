"""Opt-in prompt capture (for the Collector redaction demo) and per-request log lines."""
import json
import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.app import capture_content_enabled
from types import SimpleNamespace

from agent.loop import AGENT_NAME, Agent, AgentIncompleteError, _part
from agent.pricing import Pricing
from tests.fakes import FakeClient, text_response, tool_response, usage

PRICING = Pricing({
    "cache_write_multiplier": 1.25,
    "cache_read_multiplier": 0.1,
    "models": {"claude-sonnet-5": {"input_per_mtok": 2.0, "output_per_mtok": 10.0}},
})


def make(script, capture=False):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    agent = Agent(client=FakeClient(script), tracer=provider.get_tracer("t"), pricing=PRICING,
                  model="claude-sonnet-5", capture_content=capture)
    return agent, exporter


def chat_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]


def test_capture_on_records_input_and_output_messages():
    script = [
        tool_response([("tu_1", "get_product", {"sku": "TENT-2P"})]),
        text_response("It is $289."),
    ]
    agent, exporter = make(script, capture=True)
    agent.run("PROMPT-MARKER price?")

    first, second = chat_spans(exporter)
    assert "PROMPT-MARKER" in first.attributes["gen_ai.input.messages"]
    assert json.loads(second.attributes["gen_ai.output.messages"]) == [
        {"role": "assistant", "parts": [{"type": "text", "content": "It is $289."}],
         "finish_reason": "end_turn"}]
    # Second call's input shows the tool round trip, as readable parts.
    parts = [p["type"] for m in json.loads(second.attributes["gen_ai.input.messages"]) for p in m["parts"]]
    assert parts == ["text", "tool_call", "tool_call_response"]


def test_capture_off_by_default_records_no_content():
    agent, exporter = make([text_response("ok")])
    agent.run("q")
    for s in chat_spans(exporter):
        assert "gen_ai.input.messages" not in s.attributes
        assert "gen_ai.output.messages" not in s.attributes


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("false", False), ("true", True), ("TRUE", True),
])
def test_capture_flag_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    else:
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", value)
    assert capture_content_enabled() is expected


def test_request_log_line_has_outcome_and_cost_but_no_prompt(caplog):
    agent, _ = make([text_response("ok", usage_=usage(1000, 100))])
    with caplog.at_level(logging.INFO, logger=AGENT_NAME):
        agent.run("PROMPT-MARKER")
    [rec] = [r for r in caplog.records if r.name == AGENT_NAME]
    assert rec.getMessage() == "agent request finished"
    assert rec.outcome == "ok"
    assert rec.cost_usd == pytest.approx(0.003)
    assert rec.turns == 1
    assert "PROMPT-MARKER" not in str(vars(rec))


def test_failed_request_logs_at_error_with_outcome(caplog):
    agent, _ = make([text_response("x", stop_reason="max_tokens")])
    with caplog.at_level(logging.INFO, logger=AGENT_NAME), pytest.raises(AgentIncompleteError):
        agent.run("q")
    [rec] = [r for r in caplog.records if r.name == AGENT_NAME]
    assert rec.levelno == logging.ERROR
    assert rec.outcome == "AgentIncompleteError"


def test_unknown_block_types_keep_only_their_type():
    # e.g. a thinking block: record that it happened, never its content
    assert _part(SimpleNamespace(type="thinking", thinking="secret")) == {"type": "thinking"}
