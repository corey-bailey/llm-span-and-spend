"""HTTP front door for the agent. Run: uv run uvicorn --factory agent.app:build_app --port 8000"""
import logging
import os

import anthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.loop import AGENT_NAME, Agent, AgentIncompleteError, AgentLoopLimitError

SERVICE_VERSION = "0.1.0"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_QUESTION_CHARS = 2000

log = logging.getLogger(AGENT_NAME)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


class AskResponse(BaseModel):
    answer: str
    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


def create_app(agent) -> FastAPI:
    app = FastAPI(title=AGENT_NAME, version=SERVICE_VERSION)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/ask", response_model=AskResponse)
    def ask(req: AskRequest):
        try:
            r = agent.run(req.question)
        except anthropic.RateLimitError:
            log.warning("upstream rate limit")
            raise HTTPException(429, "rate limited, retry later")
        except anthropic.APIStatusError as exc:
            log.error("upstream API error status=%s", exc.status_code)
            raise HTTPException(502, "upstream model error")
        except anthropic.APIConnectionError:
            log.error("upstream connection error")
            raise HTTPException(502, "upstream unreachable")
        except AgentIncompleteError as exc:
            log.error("model did not complete stop_reason=%s", exc.stop_reason)
            raise HTTPException(502, "model did not complete an answer")
        except AgentLoopLimitError:
            log.error("agent loop limit hit")
            raise HTTPException(500, "agent did not finish")
        return AskResponse(answer=r.answer, turns=r.turns, input_tokens=r.input_tokens,
                           output_tokens=r.output_tokens, cost_usd=r.cost_usd)

    return app


def build_app() -> FastAPI:
    """Factory for uvicorn --factory. Kept out of import time so tests need no API key."""
    from agent.pricing import Pricing
    from agent.telemetry import init_tracing

    tracer = init_tracing(AGENT_NAME, SERVICE_VERSION)
    agent = Agent(client=anthropic.Anthropic(), tracer=tracer, pricing=Pricing.from_file(),
                  model=os.environ.get("AGENT_MODEL", DEFAULT_MODEL))
    return create_app(agent)
