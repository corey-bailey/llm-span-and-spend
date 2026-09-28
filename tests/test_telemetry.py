from agent.telemetry import build_resource


def test_resource_carries_service_identity():
    attrs = build_resource("catalog-agent", "0.1.0").attributes
    assert attrs["service.name"] == "catalog-agent"
    assert attrs["service.version"] == "0.1.0"
