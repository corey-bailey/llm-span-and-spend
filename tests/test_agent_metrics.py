"""The agent must emit GenAI client metrics (token usage, operation duration) and
request-level cost and duration, so SLOs and cost dashboards need no trace queries."""
import anthropic
import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from agent.loop import Agent, AgentIncompleteError
from agent.pricing import Pricing
from agent.telemetry import AgentMetrics
from tests.fakes import FakeClient, text_response, tool_response, usage

PRICING = Pricing({
    "cache_write_multiplier": 1.25,
    "cache_read_multiplier": 0.1,
    "models": {"claude-sonnet-5": {"input_per_mtok": 2.0, "output_per_mtok": 10.0}},
})


@pytest.fixture
def reader():
    return InMemoryMetricReader()


def run_agent(reader, script, question="q"):
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    agent = Agent(client=FakeClient(script), tracer=TracerProvider().get_tracer("test"),
                  pricing=PRICING, model="claude-sonnet-5", metrics=AgentMetrics(meter))
    return agent.run(question)


def points(reader, name):
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    return m, list(m.data.data_points)
    raise AssertionError(f"metric {name} not emitted")


def test_token_usage_histogram_split_by_token_type(reader):
    run_agent(reader, [
        tool_response([("tu_1", "get_product", {"sku": "TENT-2P"})], usage_=usage(1000, 50)),
        text_response("ok", usage_=usage(2000, 20)),
    ])
    metric, pts = points(reader, "gen_ai.client.token.usage")
    assert metric.unit == "{token}"
    sums = {p.attributes["gen_ai.token.type"]: p.sum for p in pts}
    assert sums == {"input": 3000, "output": 70}
    for p in pts:
        assert p.attributes["gen_ai.operation.name"] == "chat"
        assert p.attributes["gen_ai.provider.name"] == "anthropic"
        assert p.attributes["gen_ai.request.model"] == "claude-sonnet-5"


def test_operation_duration_recorded_per_model_call(reader):
    run_agent(reader, [
        tool_response([("tu_1", "get_product", {"sku": "TENT-2P"})]),
        text_response("ok"),
    ])
    metric, pts = points(reader, "gen_ai.client.operation.duration")
    assert metric.unit == "s"
    assert sum(p.count for p in pts) == 2


def test_request_cost_histogram_and_spend_counter(reader):
    run_agent(reader, [text_response("ok", usage_=usage(1000, 100))])
    _, cost_pts = points(reader, "llm.agent.request.cost")
    assert cost_pts[0].sum == pytest.approx(0.003)
    assert cost_pts[0].attributes["outcome"] == "ok"
    _, spend_pts = points(reader, "llm.cost.usd")
    assert spend_pts[0].value == pytest.approx(0.003)
    assert spend_pts[0].attributes["gen_ai.request.model"] == "claude-sonnet-5"


def test_request_duration_carries_outcome(reader):
    run_agent(reader, [text_response("ok")])
    _, pts = points(reader, "llm.agent.request.duration")
    assert pts[0].attributes["outcome"] == "ok"
    assert pts[0].count == 1


def test_failed_request_records_error_outcome_and_partial_spend(reader):
    with pytest.raises(AgentIncompleteError):
        run_agent(reader, [text_response("cut", stop_reason="max_tokens", usage_=usage(500, 16000))])
    _, dur = points(reader, "llm.agent.request.duration")
    assert dur[0].attributes["outcome"] == "AgentIncompleteError"
    # The truncated call was still billed; spend must count it.
    _, spend = points(reader, "llm.cost.usd")
    assert spend[0].value == pytest.approx((500 * 2 + 16000 * 10) / 1e6)


def test_api_error_records_duration_with_error_type(reader):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.RateLimitError("slow", response=httpx.Response(429, request=request), body=None)
    with pytest.raises(anthropic.RateLimitError):
        run_agent(reader, [err])
    _, pts = points(reader, "gen_ai.client.operation.duration")
    assert pts[0].attributes["error.type"] == "RateLimitError"


def test_agent_without_metrics_still_works():
    agent = Agent(client=FakeClient([text_response("ok")]),
                  tracer=TracerProvider().get_tracer("t"), pricing=PRICING, model="claude-sonnet-5")
    assert agent.run("q").answer == "ok"
