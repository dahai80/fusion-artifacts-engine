import logging
from contextlib import contextmanager
from functools import wraps

logger = logging.getLogger(__name__)

# P2-5/H12(trace)/M20: optional OTel tracing integration.
# Audit requires "at least wire up the OTel Python SDK, with rpc.server.duration + db.storage.duration spans".
# This middleware is local-first single-tenant on a stdlib HTTP server; OTel must not be a hard dependency —
# most deployments do not need distributed tracing. Hence optional integration:
#   - opentelemetry-api/sdk not installed → tracing is a pure no-op (zero overhead, zero import errors).
#   - installed but tracing.enabled=false → no-op tracer.
#   - installed and enabled=true → real tracer with rpc.server.duration / db.storage.duration spans.
# Callers always use `with tracing.span("name", **attrs):` and need not care whether anything is actually collected.

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
    # Process-level one-time configuration. enabled=False or OTel unavailable → stays no-op.
    # enabled=True and OTel available → builds TracerProvider + ConsoleSpanProcessor.
    # Console exporter is the default output (sufficient for local single-tenant; production can attach an
    # OTLP exporter externally — once the deployer injects OTEL_EXPORTER_OTLP_ENDPOINT the SDK enables it, no code change needed here).
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
    # Unified span entry point. enabled+available → real span (records attributes, exceptions, status).
    # Otherwise → pure no-op contextmanager, invisible to callers.
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
    # P2-5: decorator form — for sync methods awkward to wrap with `with` (large bodies / many returns).
    # Enabled check is deferred to call time (configure_tracing runs after startup while decoration happens at
    # import time, so the deco must not test _ENABLED early, otherwise it stays no-op forever). Calls the original
    # function directly when not enabled.
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
    # Gracefully flush the span processor on process exit. Idempotent.
    global _PROVIDER, _TRACER, _ENABLED
    if _PROVIDER is not None:
        try:
            _PROVIDER.shutdown()
        except Exception as e:
            logger.warning("OTel provider shutdown failed: %s", e)
    _PROVIDER = None
    _TRACER = None
    _ENABLED = False
