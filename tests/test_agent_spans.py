"""The agent loop must emit OTel GenAI spans: one invoke_agent root, one chat span per
model call, one execute_tool span per tool call, with token counts and cost."""
import anthropic
import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agent.loop import Agent, AgentIncompleteError, AgentLoopLimitError
from agent.pricing import Pricing
from tests.fakes import FakeClient, text_response, tool_response, usage

PRICING = Pricing({
    "cache_write_multiplier": 1.25,
    "cache_read_multiplier": 0.1,
    "models": {"claude-sonnet-5": {"input_per_mtok": 2.0, "output_per_mtok": 10.0}},
})


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def make_agent(script, tracer, max_turns=8):
    client = FakeClient(script)
    return Agent(client=client, tracer=tracer, pricing=PRICING,
                 model="claude-sonnet-5", max_turns=max_turns), client


def by_name(finished):
    return {s.name: s for s in finished}


def test_single_turn_answer(spans):
    exporter, tracer = spans
    agent, _ = make_agent([text_response("Hi.", usage_=usage(1000, 100))], tracer)

    result = agent.run("hello")

    assert result.answer == "Hi."
    names = [s.name for s in exporter.get_finished_spans()]
    assert names == ["chat claude-sonnet-5", "invoke_agent catalog-agent"]
    chat = by_name(exporter.get_finished_spans())["chat claude-sonnet-5"]
    assert chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["gen_ai.provider.name"] == "anthropic"
    assert chat.attributes["gen_ai.request.model"] == "claude-sonnet-5"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 1000
    assert chat.attributes["gen_ai.usage.output_tokens"] == 100
    assert chat.attributes["gen_ai.response.finish_reasons"] == ("end_turn",)
    assert chat.attributes["llm.cost.usd"] == pytest.approx(0.003)


def test_tool_loop_emits_nested_spans_and_rolls_up_cost(spans):
    exporter, tracer = spans
    script = [
        tool_response([("tu_1", "get_product", {"sku": "TENT-2P"})], usage_=usage(1000, 50)),
        text_response("The tent is $289.", usage_=usage(2000, 20)),
    ]
    agent, client = make_agent(script, tracer)

    result = agent.run("How much is the 2P tent?")

    assert result.answer == "The tent is $289."
    assert result.turns == 2
    finished = exporter.get_finished_spans()
    tool = by_name(finished)["execute_tool get_product"]
    root = by_name(finished)["invoke_agent catalog-agent"]
    assert tool.attributes["gen_ai.tool.name"] == "get_product"
    assert tool.attributes["gen_ai.tool.call.id"] == "tu_1"
    assert tool.parent.span_id == root.context.span_id
    assert root.attributes["gen_ai.usage.input_tokens"] == 3000
    assert root.attributes["gen_ai.usage.output_tokens"] == 70
    # (3000 * 2 + 70 * 10) / 1e6
    assert root.attributes["llm.cost.usd"] == pytest.approx(0.0067)
    # The tool result went back to the model with the matching id
    last_user = client.messages.requests[1]["messages"][-1]
    assert last_user["content"][0]["tool_use_id"] == "tu_1"


def test_prompt_text_is_not_recorded_on_spans(spans):
    exporter, tracer = spans
    agent, _ = make_agent([text_response("ok")], tracer)
    agent.run("SECRET-PROMPT-TEXT")
    for s in exporter.get_finished_spans():
        assert "SECRET-PROMPT-TEXT" not in str(dict(s.attributes))


def test_tool_error_is_returned_to_model_and_marked_on_span(spans):
    exporter, tracer = spans
    script = [
        tool_response([("tu_1", "get_product", {"sku": "NOPE"})]),
        text_response("That SKU does not exist."),
    ]
    agent, client = make_agent(script, tracer)

    agent.run("price of NOPE?")

    tool = by_name(exporter.get_finished_spans())["execute_tool get_product"]
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.attributes["error.type"] == "ToolError"
    result_block = client.messages.requests[1]["messages"][-1]["content"][0]
    assert result_block["is_error"] is True


def test_api_error_marks_chat_and_root_spans(spans):
    exporter, tracer = spans
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
    agent, _ = make_agent([err], tracer)

    with pytest.raises(anthropic.RateLimitError):
        agent.run("hi")

    finished = by_name(exporter.get_finished_spans())
    assert finished["chat claude-sonnet-5"].status.status_code == StatusCode.ERROR
    assert finished["chat claude-sonnet-5"].attributes["error.type"] == "RateLimitError"
    assert finished["invoke_agent catalog-agent"].status.status_code == StatusCode.ERROR


def test_loop_limit_stops_runaway_agent(spans):
    exporter, tracer = spans
    script = [tool_response([(f"tu_{i}", "search_catalog", {"query": "x"})]) for i in range(3)]
    agent, _ = make_agent(script, tracer, max_turns=3)

    with pytest.raises(AgentLoopLimitError):
        agent.run("loop forever")

    root = by_name(exporter.get_finished_spans())["invoke_agent catalog-agent"]
    assert root.attributes["error.type"] == "AgentLoopLimitError"


@pytest.mark.parametrize("reason", ["max_tokens", "refusal", "pause_turn"])
def test_incomplete_stop_reasons_raise_instead_of_returning_partial_answer(spans, reason):
    exporter, tracer = spans
    agent, _ = make_agent([text_response("partial ans", stop_reason=reason)], tracer)

    with pytest.raises(AgentIncompleteError) as err:
        agent.run("q")

    assert err.value.stop_reason == reason
    root = by_name(exporter.get_finished_spans())["invoke_agent catalog-agent"]
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["error.type"] == "AgentIncompleteError"


def test_stop_sequence_is_a_normal_finish(spans):
    exporter, tracer = spans
    agent, _ = make_agent([text_response("done", stop_reason="stop_sequence")], tracer)
    assert agent.run("q").answer == "done"


def test_tool_error_text_does_not_reach_span_status(spans):
    exporter, tracer = spans
    script = [
        tool_response([("tu_1", "get_product", {"sku": "USER-TYPED-SECRET"})]),
        text_response("No such SKU."),
    ]
    agent, _ = make_agent(script, tracer)
    agent.run("q")
    for s in exporter.get_finished_spans():
        assert "USER-TYPED-SECRET" not in (s.status.description or "")
        assert "USER-TYPED-SECRET" not in str(dict(s.attributes))
