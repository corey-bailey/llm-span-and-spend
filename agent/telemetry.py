"""OTel SDK setup: traces, metrics, and logs over OTLP/gRPC to whatever OTEL_EXPORTER_OTLP_ENDPOINT names.

The exporter reads the standard OTEL_* environment variables, so the same code points
at the all-in-one otel-lgtm container now and a dedicated Collector later.
"""
import logging
import socket

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def build_resource(service_name: str, service_version: str) -> Resource:
    return Resource.create({
        "service.name": service_name,
        "service.version": service_version,
        "host.name": socket.gethostname(),
    })


def init_tracing(service_name: str, service_version: str) -> trace.Tracer:
    provider = TracerProvider(resource=build_resource(service_name, service_version))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name, service_version)


# Bucket boundaries from the GenAI semantic conventions for client metrics.
TOKEN_BUCKETS = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304]
DURATION_BUCKETS = [0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92]
# Per-request dollar cost; chosen for this demo's range (fractions of a cent to $1).
COST_BUCKETS = [0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]


class AgentMetrics:
    """Instruments the agent records into. Built from any Meter, so tests use an
    in-memory reader and production uses OTLP."""

    def __init__(self, meter: metrics.Meter):
        self._tokens = meter.create_histogram(
            "gen_ai.client.token.usage", unit="{token}",
            description="Tokens used per model call, split by gen_ai.token.type",
            explicit_bucket_boundaries_advisory=TOKEN_BUCKETS)
        self._op_duration = meter.create_histogram(
            "gen_ai.client.operation.duration", unit="s",
            description="Duration of one model call",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS)
        self._req_duration = meter.create_histogram(
            "llm.agent.request.duration", unit="s",
            description="End-to-end duration of one agent request",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS)
        self._req_cost = meter.create_histogram(
            "llm.agent.request.cost", unit="{USD}",
            description="Dollar cost of one agent request at list price",
            explicit_bucket_boundaries_advisory=COST_BUCKETS)
        self._spend = meter.create_counter(
            "llm.cost.usd", unit="{USD}",
            description="Cumulative model spend at list price")

    @classmethod
    def noop(cls) -> "AgentMetrics":
        return cls(metrics.NoOpMeter("noop"))

    def record_chat(self, duration_s: float, input_tokens: int, output_tokens: int,
                    attributes: dict) -> None:
        self._op_duration.record(duration_s, attributes)
        self._tokens.record(input_tokens, {**attributes, "gen_ai.token.type": "input"})
        self._tokens.record(output_tokens, {**attributes, "gen_ai.token.type": "output"})

    def record_chat_error(self, duration_s: float, attributes: dict) -> None:
        self._op_duration.record(duration_s, attributes)

    def record_request(self, duration_s: float, cost_usd: float, attributes: dict) -> None:
        self._req_duration.record(duration_s, attributes)
        self._req_cost.record(cost_usd, attributes)
        model_only = {"gen_ai.request.model": attributes["gen_ai.request.model"]}
        self._spend.add(cost_usd, model_only)


def init_metrics(service_name: str, service_version: str) -> AgentMetrics:
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10_000)
    provider = MeterProvider(resource=build_resource(service_name, service_version),
                             metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return AgentMetrics(metrics.get_meter(service_name, service_version))


def init_logging(service_name: str, service_version: str) -> None:
    """Send the service logger's records over OTLP. Records written inside a span
    carry its trace id, which is what links a Loki line to its Tempo trace."""
    provider = LoggerProvider(resource=build_resource(service_name, service_version))
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(provider)
    logger = logging.getLogger(service_name)
    logger.setLevel(logging.INFO)
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))
