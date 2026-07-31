import time
import tempfile
import shutil
from pathlib import Path
import pytest
import httpx
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

PORT = 19897


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


def test_create_with_metadata(rpc_server):
    r = rpc("artifact.create", {
        "session_id": "s_meta",
        "name": "btn.tsx",
        "type": "react",
        "content": "export default Button()",
        "metadata": {
            "component_name": "Button",
            "framework": "react",
            "design_tokens": {"color": "blue"},
        },
    })
    assert "result" in r
    meta = r["result"]["artifact"]["metadata"]
    assert meta["component_name"] == "Button"
    assert meta["framework"] == "react"


def test_create_without_metadata(rpc_server):
    r = rpc("artifact.create", {
        "session_id": "s_meta",
        "name": "plain.py",
        "type": "code",
        "content": "x = 1",
    })
    assert "result" in r
    assert r["result"]["artifact"]["metadata"] is None


def test_get_artifact_with_metadata(rpc_server):
    r = rpc("artifact.create", {
        "session_id": "s_meta_get",
        "name": "card.vue",
        "type": "html",
        "content": "<div>card</div>",
        "metadata": {"framework": "vue", "layout_type": "card"},
    })
    art_id = r["result"]["artifact"]["id"]
    r2 = rpc("artifact.get", {"artifact_id": art_id})
    assert r2["result"]["artifact"]["metadata"]["framework"] == "vue"


def test_list_filter_by_metadata(rpc_server):
    rpc("artifact.create", {
        "session_id": "s_meta_filter",
        "name": "react_btn.tsx",
        "type": "react",
        "content": "btn",
        "metadata": {"framework": "react", "component_name": "Button"},
    })
    rpc("artifact.create", {
        "session_id": "s_meta_filter",
        "name": "vue_card.vue",
        "type": "html",
        "content": "card",
        "metadata": {"framework": "vue", "component_name": "Card"},
    })
    rpc("artifact.create", {
        "session_id": "s_meta_filter",
        "name": "react_input.tsx",
        "type": "react",
        "content": "input",
        "metadata": {"framework": "react", "component_name": "Input"},
    })
    r_react = rpc("artifact.list", {
        "session_id": "s_meta_filter",
        "metadata_filter": {"framework": "react"},
    })
    react_arts = r_react["result"]["artifacts"]
    assert len(react_arts) >= 2
    assert all(a["metadata"]["framework"] == "react" for a in react_arts)
    r_vue = rpc("artifact.list", {
        "session_id": "s_meta_filter",
        "metadata_filter": {"framework": "vue"},
    })
    vue_arts = r_vue["result"]["artifacts"]
    assert len(vue_arts) >= 1
    assert all(a["metadata"]["framework"] == "vue" for a in vue_arts)


def test_list_combined_project_id_and_metadata_filter(rpc_server):
    rpc("artifact.create", {
        "session_id": "s_combined",
        "name": "combined.tsx",
        "type": "react",
        "content": "c",
        "project_id": "proj-combined",
        "metadata": {"framework": "react"},
    })
    rpc("artifact.create", {
        "session_id": "s_combined",
        "name": "combined2.vue",
        "type": "html",
        "content": "c2",
        "project_id": "proj-combined",
        "metadata": {"framework": "vue"},
    })
    r = rpc("artifact.list", {
        "session_id": "s_combined",
        "project_id": "proj-combined",
        "metadata_filter": {"framework": "react"},
    })
    arts = r["result"]["artifacts"]
    assert len(arts) >= 1
    assert all(a["project_id"] == "proj-combined" for a in arts)
    assert all(a["metadata"]["framework"] == "react" for a in arts)


def test_import_artifact_with_metadata(rpc_server):
    r = rpc("artifact.import", {
        "session_id": "s_import_meta",
        "data": {
            "artifact": {
                "name": "imported_meta.py",
                "type": "code",
                "metadata": {"component_name": "ImportedTool", "framework": "python"},
            },
            "content": "def tool(): pass",
        },
    })
    assert "result" in r
    meta = r["result"]["artifact"]["metadata"]
    assert meta["component_name"] == "ImportedTool"
