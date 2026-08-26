import shutil
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer
from fusion_artifacts_engine.storage.sqlite_storage import (
    SQLiteStorage,
    _read_text_chunked,
    _write_text_chunked,
)

# ── chunked file I/O ────────────────────────────────────


def test_write_read_chunked_roundtrip_small(tmp_path):
    p = tmp_path / "small.txt"
    _write_text_chunked(p, "hello world\n")
    assert _read_text_chunked(p) == "hello world\n"


def test_write_read_chunked_roundtrip_large(tmp_path):
    # 3MB 内容跨多个 1MB 分块
    p = tmp_path / "big.txt"
    payload = "x" * (3 * 1024 * 1024) + "END"
    _write_text_chunked(p, payload)
    assert _read_text_chunked(p) == payload


def test_write_read_chunked_multibyte_boundary(tmp_path):
    # 多字节 UTF-8 字符恰好跨块边界（encode 后 1MB 边界落在字符中间）
    # 分块以 encode 后字节切，decode 整体一次，不应产生 UnicodeDecodeError
    p = tmp_path / "mb.txt"
    payload = "界" * (1024 * 1024) + "tail"
    _write_text_chunked(p, payload)
    assert _read_text_chunked(p) == payload


def test_write_chunked_disk_full_maps_resource_limit(tmp_path, monkeypatch):
    # 模拟 ENOSPC：open 写入时抛 OSError(ENOSPC) → ResourceLimitError，并删临时文件
    import errno as _errno

    from fusion_artifacts_engine.rpc.errors import ResourceLimitError

    p = tmp_path / "fail.txt"

    real_open = open
    import builtins

    def fake_open(path, mode, *a, **k):
        f = real_open(path, mode, *a, **k)
        if "wb" in mode and str(path) == str(p):
            f.write = lambda data: (_ for _ in ()).throw(
                OSError(_errno.ENOSPC, "disk full")
            )
        return f

    monkeypatch.setattr(builtins, "open", fake_open)
    with pytest.raises(ResourceLimitError):
        _write_text_chunked(p, "big" * 1024)
    # 临时文件被清理
    assert not p.exists()


# ─_storage large content roundtrip ───────────────────────


def test_storage_large_content_roundtrip(tmp_path):
    # 走真实 _write_content_file / _read_content_file（分块），>small_content_limit 落盘
    st = SQLiteStorage(
        db_path=tmp_path / "m.db",
        content_dir=tmp_path / "c",
        small_content_limit=10240,
    )
    payload = "line of content\n" * 100000  # ~1.7MB
    path = st._write_content_file("art_big", 1, payload, "md")
    assert Path(path).exists()
    assert st._read_content_file(path) == payload


# ─_SSE event size cap ────────────────────────────────────


def test_server_sse_event_size_cap_drops_oversized(tmp_path):
    # F5: 单事件序列化超 sse_max_event_bytes 被丢弃，不写流
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(
        storage_root=tmp / "artifacts",
        allow_no_auth=True,
        sse_heartbeat_interval=1,
        sse_max_event_bytes=256,
    )
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=19920)
    server.start_async()
    time.sleep(0.5)

    received = b""
    try:
        with httpx.stream(
            "GET", "http://127.0.0.1:19920/api/v1/events/stream", timeout=5.0
        ) as resp:
            assert resp.status_code == 200
            time.sleep(0.3)
            # 大 payload 事件（远超 256B cap）
            big_payload = {"marker": "big", "blob": "Z" * 5000}
            httpx.post(
                "http://127.0.0.1:19920/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "artifact.emit_event",
                    "params": {"event_type": "big.event", "payload": big_payload},
                },
                timeout=5.0,
            )
            # 小事件应正常投递
            httpx.post(
                "http://127.0.0.1:19920/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "artifact.emit_event",
                    "params": {"event_type": "small.event", "payload": {"m": 1}},
                },
                timeout=5.0,
            )
            deadline = time.time() + 3
            for chunk in resp.iter_text():
                received += chunk.encode()
                if time.time() > deadline:
                    break
                if b"small.event" in received:
                    time.sleep(0.3)
                    break
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        pass

    # 小事件投递
    assert b"small.event" in received, "small SSE event within cap must be delivered"
    # 大事件被丢弃
    assert b"big.event" not in received, "oversized SSE event must be dropped by cap"

    server.stop()
    engine.close()
    shutil.rmtree(tmp)


def test_server_sse_event_size_cap_zero_unlimited(tmp_path):
    # F5: sse_max_event_bytes=0 不限，大事件照常投递
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(
        storage_root=tmp / "artifacts",
        allow_no_auth=True,
        sse_heartbeat_interval=1,
        sse_max_event_bytes=0,
    )
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=19921)
    server.start_async()
    time.sleep(0.5)

    received = b""
    try:
        with httpx.stream(
            "GET", "http://127.0.0.1:19921/api/v1/events/stream", timeout=5.0
        ) as resp:
            assert resp.status_code == 200
            time.sleep(0.3)
            httpx.post(
                "http://127.0.0.1:19921/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "artifact.emit_event",
                    "params": {"event_type": "big.event", "payload": {"blob": "Z" * 5000}},
                },
                timeout=5.0,
            )
            for chunk in resp.iter_text():
                received += chunk.encode()
                if b"big.event" in received:
                    break
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        pass

    assert b"big.event" in received, "cap=0 must not drop oversized event"

    server.stop()
    engine.close()
    shutil.rmtree(tmp)
