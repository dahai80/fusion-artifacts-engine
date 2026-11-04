import shutil
import tempfile
from pathlib import Path

import pytest

from fusion_artifacts_engine import tracing
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.methods import RPCHandler

# OTel is an optional dependency. Enabled-path assertions run only when installed; otherwise only the no-op path runs.
otel = pytest.importorskip("opentelemetry.sdk.trace", reason="opentelemetry-sdk not installed")
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)


@pytest.fixture
def handler():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    eng = ArtifactEngine(cfg)
    h = RPCHandler(eng)
    yield h, eng
    eng.close()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def traced(handler):
    # Install a TracerProvider with an in-memory exporter, replacing the tracing module globals,
    # so tracing.span()/traced() take the real span path. Returns (handler, exporter).
    # Key: use provider.get_tracer() to get the provider-bound tracer instead of the OTel global
    # set_tracer_provider (the latter is process-level idempotent; multiple tests would collapse onto one provider and break isolation).
    h, eng = handler
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test-fae"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    saved = (tracing._TRACER, tracing._ENABLED, tracing._PROVIDER)
    tracing._TRACER = provider.get_tracer("test-fae")
    tracing._ENABLED = True
    tracing._PROVIDER = provider
    try:
        yield h, eng, exporter
    finally:
        tracing._TRACER, tracing._ENABLED, tracing._PROVIDER = saved
        try:
            provider.shutdown()
        except Exception:
            pass


async def test_tracing_disabled_is_noop(handler):
    # P2-5: default tracing.enabled=false → span() is a no-op: no raise, no collection.
    h, _eng = handler
    assert tracing.is_enabled() is False
    with tracing.span("rpc.server.duration", rpc_method="ping") as s:
        assert s is None, "span context must yield None when tracing is disabled"
    # dispatch works normally, does not crash merely because the tracing module exists (dispatch returns the handler's raw result)
    r = await h.dispatch("ping", {})
    assert r["pong"] is True


async def test_tracing_disabled_dispatch_still_works(handler):
    h, eng = handler
    cr = await eng.create_artifact("s1", "a.py", "code", "print(1)")
    aid = cr[0].id
    # Tracing disabled, so with tracing.span(...) inside get_artifact is a no-op
    res = await h.dispatch("artifact.get", {"artifact_id": aid})
    assert res["artifact"]["id"] == aid


async def test_tracing_enabled_emits_rpc_and_storage_spans(traced):
    # P2-5: when enabled, dispatch covers rpc.server.duration and storage reads cover db.storage.duration.
    h, eng, exporter = traced
    cr = await eng.create_artifact("s1", "a.py", "code", "print(1)")
    aid = cr[0].id
    # Drop spans from the create phase to focus on artifact.get's rpc + db spans
    exporter.clear()

    await h.dispatch("artifact.get", {"artifact_id": aid})

    spans = exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "rpc.server.duration" in span_names, "dispatch must emit an rpc.server.duration span"
    assert "db.storage.duration" in span_names, "get_artifact must emit a db.storage.duration span"

    rpc_span = next(
        s for s in spans
        if s.name == "rpc.server.duration" and s.attributes.get("rpc_method") == "artifact.get"
    )
    assert rpc_span.attributes.get("rpc_method") == "artifact.get"
    db_span = next(
        s for s in spans
        if s.name == "db.storage.duration" and s.attributes.get("db_operation") == "get_artifact"
    )
    assert db_span.attributes.get("db_operation") == "get_artifact"


async def test_tracing_records_error_span_status(traced):
    # P2-5: when the handler raises RpcError the dispatch span ends normally (the outer _handle catches the RpcError,
    # so the span inside dispatch does not record ERROR — expected: business-level not-found is not a 5xx).
    # This test covers the normal list path: rpc span present, no ERROR span.
    h, _eng, exporter = traced
    res = await h.dispatch("artifact.list", {"session_id": "s_none"})
    assert res["artifacts"] == []
    spans = exporter.get_finished_spans()
    assert any(s.name == "rpc.server.duration" for s in spans)
    # Unknown method → dispatch raises RpcError and the span records ERROR (tracing.span re-raise path).
    exporter.clear()
    from fusion_artifacts_engine.rpc.errors import RpcError

    with pytest.raises(RpcError):
        await h.dispatch("no.such.method", {})
    err_spans = [s for s in exporter.get_finished_spans() if s.name == "rpc.server.duration"]
    assert err_spans, "an rpc span must still be emitted for unknown methods"
    from opentelemetry.trace import StatusCode

    assert err_spans[0].status.status_code == StatusCode.ERROR


def test_tracing_span_propagates_exception(traced):
    # P2-5: an exception raised inside tracing.span must be recorded as ERROR and re-raised (never swallowed).
    _h, _eng, exporter = traced

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with tracing.span("boom.span", k="v"):
            raise _Boom("kaboom")
    spans = [s for s in exporter.get_finished_spans() if s.name == "boom.span"]
    assert spans, "the exception span must be recorded"
    from opentelemetry.trace import StatusCode

    assert spans[0].status.status_code == StatusCode.ERROR


async def test_tracing_decorated_save_version_emits_span(traced):
    # P2-5: write paths decorated with @tracing.traced also emit db.storage.duration spans.
    _h, eng, exporter = traced
    await eng.create_artifact("s1", "a.py", "code", "print(1)")
    # create_artifact internally goes through save_artifact_and_version (first creation)
    spans = exporter.get_finished_spans()
    db_spans = [s for s in spans if s.name == "db.storage.duration"]
    assert db_spans, "write path must emit a db.storage.duration span via @tracing.traced"
    assert any(
        s.attributes.get("db_operation")
        in ("save_artifact_and_version", "save_version", "create_version_atomic")
        for s in db_spans
    )
