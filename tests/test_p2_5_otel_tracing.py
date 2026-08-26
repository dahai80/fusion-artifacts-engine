import shutil
import tempfile
from pathlib import Path

import pytest

from fusion_artifacts_engine import tracing
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.methods import RPCHandler

# OTel 可选依赖。装了才跑 enabled 路径断言；没装则只跑 no-op 路径。
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
    # 装 InMemory exporter 的 TracerProvider，替换 tracing 模块全局，
    # 让 tracing.span()/traced() 走真 span 路径。返回 (handler, exporter)。
    # 关键：用 provider.get_tracer() 拿 provider 绑定的 tracer，不走 OTel 全局
    # set_tracer_provider（后者进程级幂等，多测试会串到一个 provider，导致隔离失败）。
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
    # P2-5: 默认 tracing.enabled=false → span() no-op，不抛、不采集。
    h, _eng = handler
    assert tracing.is_enabled() is False
    with tracing.span("rpc.server.duration", rpc_method="ping") as s:
        assert s is None, "未启用时 span 上下文 yield None"
    # dispatch 正常工作，不因 tracing 模块存在而崩（dispatch 返 handler 原始结果）
    r = await h.dispatch("ping", {})
    assert r["pong"] is True


async def test_tracing_disabled_dispatch_still_works(handler):
    h, eng = handler
    cr = await eng.create_artifact("s1", "a.py", "code", "print(1)")
    aid = cr[0].id
    # tracing 未启用，get_artifact 内 with tracing.span(...) 走 no-op
    res = await h.dispatch("artifact.get", {"artifact_id": aid})
    assert res["artifact"]["id"] == aid


async def test_tracing_enabled_emits_rpc_and_storage_spans(traced):
    # P2-5: 启用后 dispatch 覆盖 rpc.server.duration，storage 读覆盖 db.storage.duration。
    h, eng, exporter = traced
    cr = await eng.create_artifact("s1", "a.py", "code", "print(1)")
    aid = cr[0].id
    # 清掉 create 阶段的 span，专注 artifact.get 的 rpc + db span
    exporter.clear()

    await h.dispatch("artifact.get", {"artifact_id": aid})

    spans = exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "rpc.server.duration" in span_names, "dispatch 须发 rpc.server.duration span"
    assert "db.storage.duration" in span_names, "get_artifact 须发 db.storage.duration span"

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
    # P2-5: handler 抛 RpcError 时 dispatch span 正常结束（RpcError 被外层 _handle 捕获，
    # dispatch 内 span 不记 ERROR——这是期望行为：业务级 not-found 非 5xx）。
    # 此测试覆盖正常 list 路径：rpc span 出现、无 ERROR span。
    h, _eng, exporter = traced
    res = await h.dispatch("artifact.list", {"session_id": "s_none"})
    assert res["artifacts"] == []
    spans = exporter.get_finished_spans()
    assert any(s.name == "rpc.server.duration" for s in spans)
    # 方法不存在 → dispatch 抛 RpcError，span 记 ERROR（tracing.span 重抛异常路径）。
    exporter.clear()
    from fusion_artifacts_engine.rpc.errors import RpcError

    with pytest.raises(RpcError):
        await h.dispatch("no.such.method", {})
    err_spans = [s for s in exporter.get_finished_spans() if s.name == "rpc.server.duration"]
    assert err_spans, "方法不存在时仍须发 rpc span"
    from opentelemetry.trace import StatusCode

    assert err_spans[0].status.status_code == StatusCode.ERROR


def test_tracing_span_propagates_exception(traced):
    # P2-5: tracing.span 内抛异常须被记录 ERROR 并重新抛出（不吞）。
    _h, _eng, exporter = traced

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with tracing.span("boom.span", k="v"):
            raise _Boom("kaboom")
    spans = [s for s in exporter.get_finished_spans() if s.name == "boom.span"]
    assert spans, "异常 span 须被记录"
    from opentelemetry.trace import StatusCode

    assert spans[0].status.status_code == StatusCode.ERROR


async def test_tracing_decorated_save_version_emits_span(traced):
    # P2-5: @tracing.traced 装饰的写路径也发 db.storage.duration span。
    _h, eng, exporter = traced
    await eng.create_artifact("s1", "a.py", "code", "print(1)")
    # create_artifact 内部走 save_artifact_and_version（首次创建）
    spans = exporter.get_finished_spans()
    db_spans = [s for s in spans if s.name == "db.storage.duration"]
    assert db_spans, "写路径须经 @tracing.traced 发 db.storage.duration span"
    assert any(
        s.attributes.get("db_operation")
        in ("save_artifact_and_version", "save_version", "create_version_atomic")
        for s in db_spans
    )
