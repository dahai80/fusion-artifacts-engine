import io
import logging
import shutil
import socket
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer
from fusion_artifacts_engine.utils import LogSanitizerFilter, setup_logging


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def server_factory():
    made = []

    def _make(cfg_overrides=None):
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        tmp = Path(tempfile.mkdtemp())
        cfg = ArtifactEngineConfig(
            storage_root=tmp / "artifacts", allow_no_auth=True
        )
        if cfg_overrides:
            for k, v in cfg_overrides.items():
                setattr(cfg, k, v)
        engine = ArtifactEngine(cfg)
        server = ArtifactRPCServer(engine, host="127.0.0.1", port=port)
        server.start_async()
        time.sleep(0.4)
        made.append((server, engine, tmp))
        return server, engine, base

    yield _make
    for server, engine, tmp in made:
        try:
            server.stop()
            engine.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


# ── LOW-1: share_id format validation ────────────────────


def test_share_id_valid_format_passes(server_factory):
    _server, _engine, base = server_factory()
    art = httpx.post(
        base,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "artifact.create",
            "params": {"session_id": "s", "name": "x.html", "type": "html", "content": "<p>hi</p>"},
        },
        timeout=5,
    ).json()["result"]["artifact"]["id"]
    share_id = httpx.post(
        base,
        json={"jsonrpc": "2.0", "id": 2, "method": "artifact.create_share", "params": {"artifact_id": art}},
        timeout=5,
    ).json()["result"]["share"]["share_id"]
    # 引擎生成的 share_id 必须满足 shr_<12> 格式，否则公开端点会自拒
    assert share_id.startswith("shr_")
    resp = httpx.get(f"{base}/api/v1/share/{share_id}", timeout=5)
    assert resp.status_code == 200


def test_share_id_garbage_rejected_400(server_factory):
    _server, _engine, base = server_factory()
    # 各种非法格式 → 400，不到 DB
    for bad in ("shr_short", "shr_", "notshr_abcabcabcabc", "shr_" + "a" * 13, "shr_!!", "SHR_abcabcabcabc"):
        resp = httpx.get(f"{base}/api/v1/share/{bad}", timeout=5)
        assert resp.status_code == 400, f"{bad!r} must be 400"
        assert resp.json().get("code") == -32602


# ── LOW-3: log injection (CWE-117) ────────────────────────


def test_log_sanitizer_strips_newlines():
    rec = logging.LogRecord(
        "t", logging.WARNING, __file__, 1,
        "user input: %s", ("injected\n[ERROR] fake\nline2",), None
    )
    flt = LogSanitizerFilter()
    assert flt.filter(rec) is True
    msg = rec.getMessage()
    # 换行被替换为字面 \\n，注入文本留在同一逻辑行
    assert "\n" not in msg
    assert "\\n" in msg
    assert "[ERROR] fake" in msg


def test_log_sanitizer_msg_string():
    rec = logging.LogRecord(
        "t", logging.WARNING, __file__, 1,
        "direct\rmultiline\nmsg", None, None
    )
    flt = LogSanitizerFilter()
    flt.filter(rec)
    assert "\r" not in rec.msg
    assert "\n" not in rec.msg
    assert "\\r" in rec.msg
    assert "\\n" in rec.msg


def test_setup_logging_installs_sanitizer():
    # setup_logging 后 root logger 应有 LogSanitizerFilter 实例
    setup_logging(logging.WARNING)
    root = logging.getLogger()
    assert any(isinstance(f, LogSanitizerFilter) for f in root.filters)


def test_log_sanitizer_handler_emits_single_line():
    # 端到端：捕获 stderr handler 输出，确认注入换行不出现在输出行
    setup_logging(logging.WARNING)
    log = logging.getLogger("low3.inject.test")
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(h)
    log.propagate = False
    log.addFilter(LogSanitizerFilter())
    log.warning("name=%s", "evil\nFAKE")
    out = buf.getvalue()
    assert out.count("\n") == 1  # 仅末尾换行
    assert "FAKE" in out
    assert "evil" in out
    log.removeHandler(h)


# ── LOW-4: /metrics optional token ────────────────────────


def test_metrics_no_token_open(server_factory):
    _server, _engine, base = server_factory(cfg_overrides={"metrics_enabled": True})
    # 未设 token → /metrics 直接可读
    resp = httpx.get(f"{base}/metrics", timeout=5)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_metrics_token_required_when_set(server_factory):
    _server, _engine, base = server_factory(
        cfg_overrides={"metrics_enabled": True, "metrics_token": "secret-metrics-123"}
    )
    # 无 token → 401
    resp = httpx.get(f"{base}/metrics", timeout=5)
    assert resp.status_code == 401
    # 错 token → 401
    resp = httpx.get(f"{base}/metrics", headers={"X-Metrics-Token": "wrong"}, timeout=5)
    assert resp.status_code == 401
    # 对 token → 200
    resp = httpx.get(f"{base}/metrics", headers={"X-Metrics-Token": "secret-metrics-123"}, timeout=5)
    assert resp.status_code == 200


def test_metrics_disabled_returns_404(server_factory):
    _server, _engine, base = server_factory(cfg_overrides={"metrics_enabled": False})
    resp = httpx.get(f"{base}/metrics", timeout=5)
    assert resp.status_code == 404


# ── LOW-5: CSP nonce, no unsafe-inline ────────────────────


def test_render_share_csp_has_nonce_no_unsafe_inline():
    import types

    from fusion_artifacts_engine.render import render_share_html

    art = types.SimpleNamespace(type="html", name="t")
    out = render_share_html(art, "<p>hi</p>")
    assert "unsafe-inline" not in out
    assert "style-src 'nonce-" in out
    # CSP nonce 与 <style nonce> 必须一致
    import re

    csp_nonce = re.search(r"style-src 'nonce-([^']+)'", out)
    style_nonce = re.search(r'<style nonce="([^"]+)"', out)
    assert csp_nonce and style_nonce
    assert csp_nonce.group(1) == style_nonce.group(1)


def test_render_share_csp_nonce_unique_per_render():
    import re
    import types

    from fusion_artifacts_engine.render import render_share_html

    art = types.SimpleNamespace(type="markdown", name="t")
    n1 = re.search(r"style-src 'nonce-([^']+)'", render_share_html(art, "# H")).group(1)
    n2 = re.search(r"style-src 'nonce-([^']+)'", render_share_html(art, "# H2")).group(1)
    assert n1 != n2, "nonce must be random per render"


def test_render_share_svg_csp_nonce():
    import types

    from fusion_artifacts_engine.render import render_share_html

    art = types.SimpleNamespace(type="svg", name="t")
    out = render_share_html(art, "<svg/>")
    assert "unsafe-inline" not in out
    assert "style-src 'nonce-" in out
    assert '<style nonce="' in out


def test_render_share_data_has_style_nonce():
    import types

    from fusion_artifacts_engine.render import render_share_html

    art = types.SimpleNamespace(type="data", name="t")
    out = render_share_html(art, '{"a": 1}')
    # data 分支无 CSP meta，但 style 标签仍带 nonce（如被嵌入 srcdoc 也不破 CSP）
    assert '<style nonce="' in out
