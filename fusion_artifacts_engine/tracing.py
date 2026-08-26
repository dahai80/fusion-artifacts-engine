import logging
from contextlib import contextmanager
from functools import wraps

logger = logging.getLogger(__name__)

# P2-5/H12(trace)/M20: 可选 OTel tracing 集成。
# 审计要求「至少接 OTel Python SDK，标 rpc.server.duration + db.storage.duration」。
# 本中间件是 local-first 单租户、stdlib HTTP server，不应把 OTel 设为硬依赖——
# 大多数部署无需分布式追踪。故采用可选集成：
#   - opentelemetry-api/sdk 未安装 → tracing 纯 no-op（零开销、零 import 错误）。
#   - 安装但 tracing.enabled=false → no-op tracer。
#   - 安装且 enabled=true → 真 tracer，rpc.server.duration / db.storage.duration span。
# 调用方一律 `with tracing.span("name", **attrs):`，无需关心是否真采集。

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    trace = None  # type: ignore[assignment]

_PROVIDER = None
_TRACER = None
_ENABLED = False


def configure_tracing(enabled: bool, service_name: str = "fusion-artifacts-engine") -> None:
    # 进程级一次性配置。enabled=False 或 OTel 不可用 → 保持 no-op。
    # enabled=True 且 OTel 可用 → 建 TracerProvider + ConsoleSpanProcessor。
    # Console exporter 是默认出口（本地单租户场景足够；生产可经 OTLP exporter 外接，
    # 由部署方注入 OTEL_EXPORTER_OTLP_ENDPOINT 后 SDK 自动启用，无需改本代码）。
    global _PROVIDER, _TRACER, _ENABLED
    if not enabled:
        _ENABLED = False
        logger.info("OTel tracing disabled (tracing.enabled=false)")
        return
    if not _OTEL_AVAILABLE:
        _ENABLED = False
        logger.warning(
            "OTel tracing enabled but opentelemetry-sdk not installed; spans are no-op. "
            "Install via: pip install 'fusion-artifacts-engine[otel]'"
        )
        return
    if _TRACER is not None:
        return
    resource = Resource.create(
        {"service.name": service_name, "service.version": "fusion-artifacts-engine"}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _PROVIDER = provider
    _TRACER = trace.get_tracer("fusion_artifacts_engine")
    _ENABLED = True
    logger.info("OTel tracing enabled: service=%s", service_name)


@contextmanager
def span(name: str, **attributes):
    # 统一 span 入口。enabled+可用 → 真 span（记录属性、异常、状态）。
    # 否则 → 纯 no-op contextmanager，调用方无感知。
    if not _ENABLED or _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as s:
        for k, v in attributes.items():
            try:
                s.set_attribute(k, v)
            except Exception as e:
                logger.debug("set_attribute %s failed: %s", k, e)
        try:
            yield s
        except Exception as e:
            try:
                s.record_exception(e)
                s.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            except Exception:
                pass
            raise


def is_enabled() -> bool:
    return _ENABLED


def traced(name: str, **attributes):
    # P2-5: 装饰器形式——用于不便用 `with` 包裹（方法体大/多 return）的同步方法。
    # 启用判定延迟到调用时（configure_tracing 在启动后调，装饰在导入期，
    # 故不能在 deco 里早判 _ENABLED，否则永远 no-op）。未启用时直接调原函数。
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not _ENABLED or _TRACER is None:
                return fn(*args, **kwargs)
            with _TRACER.start_as_current_span(name) as s:
                for k, v in attributes.items():
                    try:
                        s.set_attribute(k, v)
                    except Exception:
                        pass
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    try:
                        s.record_exception(e)
                        s.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                    except Exception:
                        pass
                    raise

        return wrapper

    return deco


def shutdown() -> None:
    # 进程退出时优雅 flush span processor。幂等。
    global _PROVIDER, _TRACER, _ENABLED
    if _PROVIDER is not None:
        try:
            _PROVIDER.shutdown()
        except Exception as e:
            logger.warning("OTel provider shutdown failed: %s", e)
    _PROVIDER = None
    _TRACER = None
    _ENABLED = False
