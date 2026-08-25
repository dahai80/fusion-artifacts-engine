import logging
import threading

logger = logging.getLogger(__name__)

# 运维2: 自实现 Prometheus 文本格式，无外部依赖。
# 指标：requests_total / error_total（counter），active_conns（gauge），
# request_latency_seconds（histogram，固定桶）。

_DEFAULT_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


class MetricsRegistry:
    def __init__(self, buckets=_DEFAULT_BUCKETS):
        self._lock = threading.Lock()
        self._counters: dict[str, dict[str, float]] = {}
        self._gauges: dict[str, float] = {}
        self._hist_buckets: dict[str, tuple[float, ...]] = {}
        self._hist_counts: dict[str, list[int]] = {}
        self._hist_sum: dict[str, float] = {}
        self._hist_total: dict[str, int] = {}
        self._buckets = tuple(sorted(buckets))
        # 内建指标预声明，便于 exposition 输出稳定顺序
        self._register_counter("rpc_requests_total")
        self._register_counter("rpc_error_total")
        self._register_gauge("rpc_active_conns")
        self._register_histogram("rpc_request_latency_seconds")

    def _register_counter(self, name: str) -> None:
        self._counters[name] = {}

    def _register_gauge(self, name: str) -> None:
        self._gauges[name] = 0

    def _register_histogram(self, name: str) -> None:
        self._hist_buckets[name] = self._buckets
        self._hist_counts[name] = [0] * (len(self._buckets) + 1)
        self._hist_sum[name] = 0.0
        self._hist_total[name] = 0

    def inc_counter(self, name: str, labels: dict | None = None, amount: float = 1) -> None:
        with self._lock:
            counters = self._counters.setdefault(name, {})
            key = self._label_key(labels)
            counters[key] = counters.get(key, 0.0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def inc_gauge(self, name: str, delta: float = 1) -> None:
        with self._lock:
            self._gauges[name] = self._gauges.get(name, 0) + delta

    def dec_gauge(self, name: str, delta: float = 1) -> None:
        with self._lock:
            self._gauges[name] = self._gauges.get(name, 0) - delta

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            if name not in self._hist_counts:
                self._register_histogram(name)
            buckets = self._hist_buckets[name]
            counts = self._hist_counts[name]
            placed = False
            for i, b in enumerate(buckets):
                if value <= b:
                    counts[i] += 1
                    placed = True
                    break
            if not placed:
                counts[-1] += 1
            self._hist_sum[name] += value
            self._hist_total[name] += 1

    @staticmethod
    def _label_key(labels: dict | None) -> str:
        if not labels:
            return ""
        return ",".join(f"{k}={labels[k]}" for k in sorted(labels))

    @staticmethod
    def _label_str(labels: dict | None) -> str:
        if not labels:
            return ""
        pairs = ",".join(f'{k}="{labels[k]}"' for k in sorted(labels))
        return "{" + pairs + "}"

    def expose(self) -> str:
        # Prometheus text exposition format 0.0.4
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                lines.append(f"# HELP {name} Counter")
                lines.append(f"# TYPE {name} counter")
                for key, val in sorted(self._counters[name].items()):
                    if not key:
                        lines.append(f"{name} {val}")
                    else:
                        # key 形如 method=xxx -> 还原成 {method="xxx"}
                        parts = key.split(",")
                        label_str = "{" + ",".join(
                            f'{p.split("=", 1)[0]}="{p.split("=", 1)[1]}"' for p in parts
                        ) + "}"
                        lines.append(f"{name}{label_str} {val}")
            for name in sorted(self._gauges):
                lines.append(f"# HELP {name} Gauge")
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name} {self._gauges[name]}")
            for name in sorted(self._hist_counts):
                lines.append(f"# HELP {name} Histogram")
                lines.append(f"# TYPE {name} histogram")
                buckets = self._hist_buckets[name]
                counts = self._hist_counts[name]
                cum = 0
                for i, b in enumerate(buckets):
                    cum += counts[i]
                    lines.append(f'{name}_bucket{{le="{b}"}} {cum}')
                cum += counts[-1]
                lines.append(f'{name}_bucket{{le="+Inf"}} {cum}')
                lines.append(f"{name}_sum {self._hist_sum[name]}")
                lines.append(f"{name}_count {self._hist_total[name]}")
        return "\n".join(lines) + "\n"


_metrics: MetricsRegistry | None = None
_metrics_lock = threading.Lock()


def get_metrics() -> MetricsRegistry:
    global _metrics
    with _metrics_lock:
        if _metrics is None:
            _metrics = MetricsRegistry()
        return _metrics


def reset_metrics() -> None:
    # 测试用：清空全局单例
    global _metrics
    with _metrics_lock:
        _metrics = None
