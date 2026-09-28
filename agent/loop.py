"""A manual Claude tool-use loop, instrumented with OpenTelemetry GenAI spans.

A manual loop (not the SDK tool runner) so each model call and each tool call gets
its own span. Span names and attributes follow the OTel GenAI semantic conventions
(experimental): https://opentelemetry.io/docs/specs/semconv/gen-ai/

Prompt and response text are never put on spans. Only counts, ids, and outcomes.
"""
import json
from dataclasses import dataclass

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

from agent.pricing import Pricing
from agent.tools import TOOL_DEFINITIONS, ToolError, run_tool

AGENT_NAME = "catalog-agent"
PROVIDER = "anthropic"
MAX_TOKENS = 16000
SYSTEM_PROMPT = (
    "You answer questions about an outdoor-gear catalog. Use the tools for every "
    "price, weight, or availability fact. Keep answers short."
)


FINAL_STOP_REASONS = {"end_turn", "stop_sequence"}


class AgentLoopLimitError(RuntimeError):
    """The model kept calling tools past max_turns."""


class AgentIncompleteError(RuntimeError):
    """The model stopped without a usable answer (max_tokens, refusal, pause_turn, ...)."""

    def __init__(self, stop_reason: str):
        super().__init__(f"model stopped with stop_reason={stop_reason}")
        self.stop_reason = stop_reason


@dataclass(frozen=True)
class AgentResult:
    answer: str
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


def _mark_error(span, exc: BaseException) -> None:
    # The exception type only. Messages can carry user-derived text (tool arguments).
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


class Agent:
    def __init__(self, client, tracer: Tracer, pricing: Pricing, model: str, max_turns: int = 8):
        self._client = client
        self._tracer = tracer
        self._pricing = pricing
        self._model = model
        self._max_turns = max_turns

    def run(self, question: str) -> AgentResult:
        with self._tracer.start_as_current_span(
            f"invoke_agent {AGENT_NAME}", kind=SpanKind.INTERNAL,
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": PROVIDER,
                "gen_ai.agent.name": AGENT_NAME,
                "gen_ai.request.model": self._model,
            },
        ) as root:
            try:
                result = self._loop(question)
            except Exception as exc:
                _mark_error(root, exc)
                raise
            root.set_attribute("gen_ai.usage.input_tokens", result.input_tokens)
            root.set_attribute("gen_ai.usage.output_tokens", result.output_tokens)
            root.set_attribute("llm.cost.usd", result.cost_usd)
            root.set_attribute("llm.agent.turns", result.turns)
            return result

    def _loop(self, question: str) -> AgentResult:
        messages = [{"role": "user", "content": question}]
        totals = {"in": 0, "out": 0, "cost": 0.0}
        for turn in range(1, self._max_turns + 1):
            response = self._chat(messages, totals)
            if response.stop_reason in FINAL_STOP_REASONS:
                answer = "".join(b.text for b in response.content if b.type == "text")
                return AgentResult(answer, turn, totals["in"], totals["out"], totals["cost"])
            if response.stop_reason != "tool_use":
                raise AgentIncompleteError(response.stop_reason)
            messages = [
                *messages,
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": self._run_tools(response.content)},
            ]
        raise AgentLoopLimitError(f"no final answer after {self._max_turns} turns")

    def _chat(self, messages: list, totals: dict):
        with self._tracer.start_as_current_span(
            f"chat {self._model}", kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": PROVIDER,
                "gen_ai.request.model": self._model,
                "gen_ai.request.max_tokens": MAX_TOKENS,
            },
        ) as span:
            try:
                response = self._client.messages.create(
                    model=self._model, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
                    tools=TOOL_DEFINITIONS, messages=messages,
                )
            except Exception as exc:
                _mark_error(span, exc)
                raise
            cost = self._pricing.cost_usd(self._model, response.usage)
            span.set_attribute("gen_ai.response.id", response.id)
            span.set_attribute("gen_ai.response.model", response.model)
            span.set_attribute("gen_ai.response.finish_reasons", [response.stop_reason])
            span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
            span.set_attribute("gen_ai.usage.cache_read.input_tokens",
                               response.usage.cache_read_input_tokens or 0)
            span.set_attribute("llm.cost.usd", cost)
            totals["in"] += response.usage.input_tokens
            totals["out"] += response.usage.output_tokens
            totals["cost"] += cost
            return response

    def _run_tools(self, content) -> list[dict]:
        """Run every tool_use block and return all results in one user message."""
        return [self._run_tool(b) for b in content if b.type == "tool_use"]

    def _run_tool(self, block) -> dict:
        with self._tracer.start_as_current_span(
            f"execute_tool {block.name}", kind=SpanKind.INTERNAL,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": block.name,
                "gen_ai.tool.call.id": block.id,
            },
        ) as span:
            try:
                output = run_tool(block.name, block.input)
            except ToolError as exc:
                _mark_error(span, exc)
                return {"type": "tool_result", "tool_use_id": block.id,
                        "content": str(exc), "is_error": True}
            return {"type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(output)}
