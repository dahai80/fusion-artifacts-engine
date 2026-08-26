import shutil
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.errors import RpcError
from fusion_artifacts_engine.rpc.methods import RPCHandler
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

PORT = 19899


@pytest.fixture(scope="module")
def rpc_server():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=PORT)
    server.start_async()
    time.sleep(0.5)
    yield server, engine
    server.stop()
    engine.close()
    shutil.rmtree(tmp)


def rpc(method, params=None, req_id=1):
    resp = httpx.post(
        f"http://127.0.0.1:{PORT}",
        json={"jsonrpc": "2.0", "method": method, "params": params or {}, "id": req_id},
        timeout=5.0,
    )
    return resp.json()


def test_ping(rpc_server):
    r = rpc("ping")
    assert r["result"]["pong"] is True


def test_rename(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_ren", "name": "old.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.rename", {"artifact_id": art_id, "new_name": "new.py"})
    assert r2["result"]["ok"] is True


def test_star(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_star", "name": "star.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.star", {"artifact_id": art_id, "starred": True})
    assert r2["result"]["ok"] is True


def test_pin(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_pin", "name": "pin.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.pin", {"artifact_id": art_id, "pinned": True, "chat_id": "c1"})
    assert r2["result"]["ok"] is True


def test_duplicate(rpc_server):
    r = rpc(
        "artifact.create",
        {
            "session_id": "s_dup",
            "name": "dup.py",
            "type": "code",
            "content": "dup content",
        },
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.duplicate", {"artifact_id": art_id, "new_name": "dup_copy.py"})
    assert "artifact" in r2["result"]
    assert r2["result"]["artifact"]["name"] == "dup_copy.py"


def test_list_all(rpc_server):
    r = rpc("artifact.list_all", {})
    assert "artifacts" in r["result"]
    assert "total" in r["result"]


def test_list_recycle(rpc_server):
    r = rpc("artifact.list_recycle", {})
    assert "artifacts" in r["result"]


def test_restore(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_rest", "name": "rest.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]
    rpc("artifact.delete", {"artifact_id": art_id, "soft_delete": True})
    r2 = rpc("artifact.restore", {"artifact_id": art_id})
    assert r2["result"]["ok"] is True


def test_purge_expired(rpc_server):
    r = rpc("artifact.purge_expired", {})
    assert "purged" in r["result"]


def test_create_share(rpc_server):
    r = rpc(
        "artifact.create",
        {
            "session_id": "s_share",
            "name": "share.py",
            "type": "code",
            "content": "share me",
        },
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.create_share", {"artifact_id": art_id, "created_by": "user1"})
    assert "share" in r2["result"]
    share_id = r2["result"]["share"]["share_id"]

    r3 = rpc("artifact.get_shared", {"share_id": share_id})
    assert "content" in r3["result"]

    r4 = rpc("artifact.revoke_share", {"share_id": share_id})
    assert r4["result"]["ok"] is True


def test_get_shared_not_found(rpc_server):
    r = rpc("artifact.get_shared", {"share_id": "shr_nonexistent"})
    assert "error" in r


def test_create_snapshot(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_snap", "name": "snap.py", "type": "code", "content": "v1"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc(
        "artifact.create_snapshot",
        {"artifact_id": art_id, "label": "v1-milestone", "author": "user1"},
    )
    assert "version" in r2["result"]

    r3 = rpc("artifact.list_snapshots", {"artifact_id": art_id})
    assert "snapshots" in r3["result"]


def test_folder_crud(rpc_server):
    r = rpc("artifact.create_folder", {"name": "Test Folder", "project_id": "proj-f"})
    folder = r["result"]["folder"]
    folder_id = folder["folder_id"]

    r2 = rpc("artifact.list_folders", {"project_id": "proj-f"})
    assert len(r2["result"]["folders"]) >= 1

    r3 = rpc("artifact.rename_folder", {"folder_id": folder_id, "new_name": "Renamed"})
    assert r3["result"]["ok"] is True

    art = rpc(
        "artifact.create",
        {"session_id": "s_fld", "name": "fld.py", "type": "code", "content": "x"},
    )
    art_id = art["result"]["artifact"]["id"]
    r4 = rpc("artifact.move_to_folder", {"artifact_id": art_id, "folder_id": folder_id})
    assert r4["result"]["ok"] is True

    r5 = rpc("artifact.delete_folder", {"folder_id": folder_id})
    assert r5["result"]["ok"] is True


def test_tag_crud(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_tag", "name": "tagged.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]

    r2 = rpc(
        "artifact.add_tag",
        {"artifact_id": art_id, "tag_name": "important", "color": "#ff0000"},
    )
    assert "tag" in r2["result"]

    r3 = rpc("artifact.list_tags", {})
    assert "tags" in r3["result"]

    r4 = rpc("artifact.list_artifact_tags", {"artifact_id": art_id})
    assert "tags" in r4["result"]

    r5 = rpc("artifact.remove_tag", {"artifact_id": art_id, "tag_name": "important"})
    assert r5["result"]["ok"] is True


def test_event_emit_and_list(rpc_server):
    rpc(
        "artifact.emit_event",
        {"event_type": "test.event", "session_id": "s_evt", "payload": {"k": "v"}},
    )
    r = rpc("artifact.list_events", {"session_id": "s_evt"})
    assert "events" in r["result"]
    assert "total" in r["result"]


def test_move_to_project_kb(rpc_server):
    r = rpc(
        "artifact.create",
        {
            "session_id": "s_kb",
            "name": "kb.py",
            "type": "code",
            "content": "x",
            "project_id": "p1",
        },
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.move_to_project_kb", {"artifact_id": art_id, "project_id": "p1"})
    assert r2["result"]["ok"] is True


def test_create_external(rpc_server):
    r = rpc(
        "artifact.create_external",
        {
            "source_module": "fusion-mlx",
            "workspace_id": "ws-test",
            "name": "ext.py",
            "type": "code",
            "content": "ext content",
            "workflow_run_id": "run-1",
        },
    )
    assert "artifact" in r["result"]
    assert r["result"]["artifact"]["source_module"] == "fusion-mlx"


def test_list_by_source(rpc_server):
    r = rpc("artifact.list_by_source", {"source_module": "fusion-mlx"})
    assert "artifacts" in r["result"]


def test_export_code(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_exp", "name": "exp.py", "type": "code", "content": "code"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.export_code", {"artifact_id": art_id, "language": "python"})
    assert r2["result"]["language"] == "python"


def test_import_code(rpc_server):
    r = rpc(
        "artifact.import_code",
        {
            "session_id": "s_imp",
            "code": "def foo(): pass",
            "language": "python",
            "name": "imp.py",
        },
    )
    assert "artifact" in r["result"]


def test_export(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_ex2", "name": "ex2.py", "type": "code", "content": "export"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.export", {"artifact_id": art_id, "include_versions": True})
    assert "data" in r2["result"]
    assert "versions" in r2["result"]["data"]


def test_export_without_versions(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_ex3", "name": "ex3.py", "type": "code", "content": "export"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.export", {"artifact_id": art_id})
    assert "data" in r2["result"]


def test_watch_register_unregister(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_w", "name": "w.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc(
        "artifact.watch",
        {"artifact_id": art_id, "action": "register", "watcher_id": "w1"},
    )
    assert r2["result"]["registered"] is True

    r3 = rpc(
        "artifact.watch",
        {"artifact_id": art_id, "action": "unregister", "watcher_id": "w1"},
    )
    assert r3["result"]["unregistered"] is True


def test_watch_poll(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_wp", "name": "wp.py", "type": "code", "content": "v1"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc(
        "artifact.watch", {"artifact_id": art_id, "action": "poll", "since_version": 0}
    )
    assert "events" in r2["result"]


def test_watch_register_no_watcher_id(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_wnw", "name": "wnw.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc(
        "artifact.watch",
        {"artifact_id": art_id, "action": "register", "watcher_id": ""},
    )
    assert "error" in r2


def test_watch_invalid_action(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_wia", "name": "wia.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.watch", {"artifact_id": art_id, "action": "invalid"})
    assert "error" in r2


def test_invalid_type(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_err", "name": "err.py", "type": "invalid", "content": "x"},
    )
    assert "error" in r


def test_invalid_kind(rpc_server):
    r = rpc(
        "artifact.create",
        {
            "session_id": "s_err2",
            "name": "err2.py",
            "type": "code",
            "content": "x",
            "kind": "invalid",
        },
    )
    assert "error" in r


def test_invalid_source(rpc_server):
    r = rpc(
        "artifact.create",
        {"session_id": "s_err3", "name": "err3.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc(
        "artifact.update",
        {"artifact_id": art_id, "content": "new", "source": "invalid_source"},
    )
    assert "error" in r2


def test_method_not_found(rpc_server):
    r = rpc("nonexistent.method")
    assert "error" in r
    assert r["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_rpc_handler_dispatch():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    engine = ArtifactEngine(cfg)
    handler = RPCHandler(engine)
    result = await handler.dispatch("ping", {})
    assert result["pong"] is True
    engine.close()
    shutil.rmtree(tmp)


@pytest.mark.asyncio
async def test_rpc_handler_method_not_found():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    engine = ArtifactEngine(cfg)
    handler = RPCHandler(engine)
    with pytest.raises(RpcError, match="Method not found"):
        await handler.dispatch("nonexistent", {})
    engine.close()
    shutil.rmtree(tmp)


def test_export_session(rpc_server):
    rpc(
        "artifact.create",
        {
            "session_id": "s_exp_sess",
            "name": "es.py",
            "type": "code",
            "content": "export session",
        },
    )
    from pathlib import Path

    server_engine = rpc_server[1]
    storage_root = Path(server_engine.config.storage_root).resolve()
    output_dir = storage_root / "export_test"
    r2 = rpc(
        "artifact.export_session",
        {"session_id": "s_exp_sess", "output_dir": str(output_dir)},
    )
    assert "count" in r2["result"]


def test_import_artifact(rpc_server):
    r = rpc(
        "artifact.import",
        {
            "session_id": "s_import",
            "data": {
                "name": "imported.py",
                "type": "code",
                "content": "imported content",
            },
        },
    )
    assert "artifact" in r["result"]


def test_import_artifact_invalid_type(rpc_server):
    r = rpc(
        "artifact.import",
        {
            "session_id": "s_import2",
            "data": {"name": "bad.py", "type": "invalid", "content": "x"},
        },
    )
    assert "error" in r


def test_create_external_invalid_type(rpc_server):
    r = rpc(
        "artifact.create_external",
        {
            "source_module": "mod",
            "workspace_id": "ws",
            "name": "ext.py",
            "type": "invalid",
            "content": "x",
        },
    )
    assert "error" in r


def test_context_budget_with_session(rpc_server):
    rpc(
        "artifact.create",
        {
            "session_id": "s_budget1",
            "name": "budget_doc.md",
            "type": "markdown",
            "content": "# Budget Test\nSome content here",
        },
    )
    r = rpc("context.budget", {"session_id": "s_budget1"})
    result = r["result"]
    assert result["total_artifact_tokens"] > 0
    assert result["artifact_count"] >= 1
    assert "artifacts" in result
    assert result["artifacts"][0]["artifact_id"] is not None
    assert result["artifacts"][0]["name"] is not None
    assert result["artifacts"][0]["tokens"] >= 0
    assert result["context_window"] == 200000
    assert result["utilization_percent"] >= 0
    assert result["warning"] is False
    assert result["recommendation"] is None


def test_context_budget_with_context_window(rpc_server):
    rpc(
        "artifact.create",
        {
            "session_id": "s_budget2",
            "name": "budget_doc2.md",
            "type": "markdown",
            "content": "word " * 500,
        },
    )
    r = rpc(
        "context.budget",
        {"session_id": "s_budget2", "context_window": 131072},
    )
    result = r["result"]
    assert result["context_window"] == 131072
    assert result["utilization_percent"] >= 0
    assert result["warning"] is False


def test_context_budget_no_session(rpc_server):
    r = rpc("context.budget", {})
    result = r["result"]
    assert "total_artifact_tokens" in result
    assert "artifact_count" in result
    assert "artifacts" in result
    assert result["context_window"] == 200000
    assert result["utilization_percent"] >= 0


def test_context_budget_warning_high_utilization(rpc_server):
    big_content = "word " * 8000
    rpc(
        "artifact.create",
        {
            "session_id": "s_budget3",
            "name": "huge_doc.md",
            "type": "markdown",
            "content": big_content,
        },
    )
    r = rpc(
        "context.budget",
        {"session_id": "s_budget3", "context_window": 1000},
    )
    result = r["result"]
    assert result["context_window"] == 1000
    assert result["utilization_percent"] > 70
    assert result["warning"] is True
    assert (
        result["recommendation"]
        == "Consider using preview_only mode for artifact injection."
    )


def test_list_all_bad_page_returns_invalid_params(rpc_server):
    # P1-5: list_all 直传 params（无 int()），storage _clamp_pagination 把非 int 兜底为默认——
    # 不报错，防御性成功。验证不崩溃且返回正常结构。
    r = rpc("artifact.list_all", {"page": "abc", "page_size": "xyz"})
    assert "artifacts" in r["result"]


def test_list_versions_bad_page_returns_invalid_params(rpc_server):
    # P1-5: version_list 用 int(params.get("page"))，非 int → ValueError → dispatch 捕获转 -32602。
    r = rpc(
        "artifact.create",
        {"session_id": "s_pg", "name": "pg.py", "type": "code", "content": "x"},
    )
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.version_list", {"artifact_id": art_id, "page": "xx"})
    assert r2["error"]["code"] == -32602


def test_list_all_huge_page_size_clamped(rpc_server):
    # P1-5: page_size 远超 500 时 storage _clamp_pagination 截到 500，不报错。
    r = rpc("artifact.list_all", {"page": 1, "page_size": 99999})
    assert "artifacts" in r["result"]


def test_rest_list_bad_page_returns_400(rpc_server):
    # P1-5: REST /api/v1/artifacts 非 int page → 400 而非 500。
    resp = httpx.get(
        f"http://127.0.0.1:{PORT}/api/v1/artifacts",
        params={"page": "abc"},
        timeout=5.0,
    )
    assert resp.status_code == 400


def test_rest_list_huge_page_size_ok(rpc_server):
    # P1-5: REST 大 page_size 截到 500，200 OK。
    resp = httpx.get(
        f"http://127.0.0.1:{PORT}/api/v1/artifacts",
        params={"page": "1", "page_size": "99999"},
        timeout=5.0,
    )
    assert resp.status_code == 200


def test_jsonrpc_response_has_request_id_header(rpc_server):
    # P1-3: JSON-RPC 响应必须回写 X-Request-ID，客户端可凭此查服务端日志。
    resp = httpx.post(
        f"http://127.0.0.1:{PORT}",
        json={"jsonrpc": "2.0", "method": "ping", "params": {}, "id": 1},
        timeout=5.0,
    )
    rid = resp.headers.get("X-Request-ID")
    assert rid is not None
    assert len(rid) == 32  # uuid4().hex


def test_rest_response_has_request_id_header(rpc_server):
    # P1-3: REST 响应同样回写 X-Request-ID。
    resp = httpx.get(
        f"http://127.0.0.1:{PORT}/api/v1/artifacts",
        params={"page": "1", "page_size": "20"},
        timeout=5.0,
    )
    rid = resp.headers.get("X-Request-ID")
    assert rid is not None
    assert len(rid) == 32


def test_request_id_unique_per_request(rpc_server):
    # P1-3: 每请求独立 uuid4，两次请求 ID 不同。
    r1 = httpx.post(
        f"http://127.0.0.1:{PORT}",
        json={"jsonrpc": "2.0", "method": "ping", "params": {}, "id": 1},
        timeout=5.0,
    )
    r2 = httpx.post(
        f"http://127.0.0.1:{PORT}",
        json={"jsonrpc": "2.0", "method": "ping", "params": {}, "id": 2},
        timeout=5.0,
    )
    assert r1.headers.get("X-Request-ID") != r2.headers.get("X-Request-ID")
