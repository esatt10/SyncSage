"""Acceptance tests for HTTP health and readiness endpoints."""

from __future__ import annotations

from collections.abc import Callable

from tests.conftest import first_attr, import_any, require_attr


def test_health_and_ready_endpoints_report_ok(loaded_config: object) -> None:
    """The FastAPI app should expose Docker/Kubernetes-friendly probes."""

    fastapi_testclient = import_any(("fastapi.testclient",))
    TestClient = require_attr(fastapi_testclient, ("TestClient",), "FastAPI TestClient")

    api_module = import_any(("syncsage.api", "syncsage.server", "syncsage.http"))
    app = first_attr(api_module, ("app", "application"))
    if app is None:
        factory: Callable[..., object] = require_attr(api_module, ("create_app", "build_app", "get_app"), "API app factory")
        try:
            app = factory(config=loaded_config)
        except TypeError:
            app = factory()

    client = TestClient(app)
    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200
    assert ready.status_code == 200
    assert health.json().get("status", "ok") in {"ok", "healthy", "pass"}
    assert ready.json().get("status", "ok") in {"ok", "ready", "pass"}
