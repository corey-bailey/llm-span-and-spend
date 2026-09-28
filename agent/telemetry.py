"""OTel SDK setup: traces over OTLP/gRPC to whatever OTEL_EXPORTER_OTLP_ENDPOINT names.

The exporter reads the standard OTEL_* environment variables, so the same code points
at the all-in-one otel-lgtm container now and a dedicated Collector later.
"""
import socket

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
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
