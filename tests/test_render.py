import time
import tempfile
import shutil
from pathlib import Path
import pytest
import httpx
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.auto_identifier import detect_renderable_type
from fusion_artifacts_engine.rpc.server import ArtifactRPCServer

PORT = 19898


@pytest.fixture(scope="module")
def rpc_server():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts")
    engine = ArtifactEngine(cfg)
    server = ArtifactRPCServer(engine, host="127.0.0.1", port=PORT)
    server.start_async()
    time.sleep(0.5)
    yield server, engine
    server.stop()
    engine.close()
    shutil.rmtree(tmp)


def rest_post(path, data):
    return httpx.post(f"http://127.0.0.1:{PORT}{path}", json=data, timeout=5.0)


def rest_get(path):
    return httpx.get(f"http://127.0.0.1:{PORT}{path}", timeout=5.0)


# --- type detection tests ---

def test_detect_html():
    assert detect_renderable_type("<!DOCTYPE html><html><body>hi</body></html>") == "html"


def test_detect_html_no_doctype():
    assert detect_renderable_type("<html><body>hi</body></html>") == "html"


def test_detect_svg():
    assert detect_renderable_type('<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>') == "svg"


def test_detect_mermaid_graph():
    assert detect_renderable_type("graph TD\n    A-->B\n    B-->C") == "mermaid"


def test_detect_mermaid_sequence():
    assert detect_renderable_type("sequenceDiagram\n    Alice->>Bob: Hello") == "mermaid"


def test_detect_react():
    assert detect_renderable_type('import React from "react";\nexport default function App() { return <div/> }') == "react"


def test_detect_non_renderable():
    assert detect_renderable_type("x = 1\ny = 2\nprint(x + y)") is None


def test_detect_via_name_svg():
    assert detect_renderable_type("anything", name="diagram.svg") == "svg"


def test_detect_via_name_react():
    assert detect_renderable_type("anything", name="App.tsx") == "react"


def test_detect_via_name_mermaid():
    assert detect_renderable_type("anything", name="flow.mermaid") == "mermaid"


# --- REST render endpoint tests ---

def test_render_html_auto(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "<!DOCTYPE html><html><body><h1>Hello</h1></body></html>",
        "session_id": "s_render",
        "type": "auto",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["renderable"] is True
    assert data["artifact_type"] == "html"
    assert data["artifact_id"] is not None
    assert data["render_url"].startswith("/api/artifact/content/")


def test_render_svg_auto(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": '<svg xmlns="http://www.w3.org/2000/svg"><rect width="100" height="100"/></svg>',
        "session_id": "s_render",
        "type": "auto",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["renderable"] is True
    assert data["artifact_type"] == "svg"


def test_render_mermaid_auto(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "graph TD\n    A-->B",
        "session_id": "s_render",
        "type": "auto",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["renderable"] is True
    assert data["artifact_type"] == "mermaid"


def test_render_non_renderable(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "x = 1\ny = 2",
        "session_id": "s_render",
        "type": "auto",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["renderable"] is False


def test_render_explicit_type(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "<html><body>forced</body></html>",
        "session_id": "s_render",
        "type": "html",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["renderable"] is True
    assert data["artifact_type"] == "html"


def test_render_invalid_type(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "some code",
        "session_id": "s_render",
        "type": "python",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["renderable"] is False


def test_render_with_viewport(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "<!DOCTYPE html><html><body>vptest</body></html>",
        "session_id": "s_render",
        "type": "html",
        "viewport": {"width": 1024, "height": 768},
    })
    assert r.status_code == 200
    data = r.json()
    assert data["viewport"] == {"width": 1024, "height": 768}


# --- GET content endpoint tests ---

def test_get_artifact_content(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "<!DOCTYPE html><html><body>content-test</body></html>",
        "session_id": "s_render_content",
        "type": "html",
    })
    artifact_id = r.json()["artifact_id"]
    r2 = rest_get(f"/api/artifact/content/{artifact_id}")
    assert r2.status_code == 200
    assert "content-test" in r2.text
    assert "text/html" in r2.headers.get("content-type", "")


def test_get_artifact_content_not_found(rpc_server):
    r = rest_get("/api/artifact/content/nonexistent_id")
    assert r.status_code == 404


# --- REST interact endpoint tests ---

def test_interact_state_change(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "<!DOCTYPE html><html><body>interact-test</body></html>",
        "session_id": "s_interact",
        "type": "html",
    })
    artifact_id = r.json()["artifact_id"]
    r2 = rest_post("/api/artifact/interact", {
        "artifact_id": artifact_id,
        "action": "state_change",
        "payload": {"content": "<!DOCTYPE html><html><body>updated via interact</body></html>"},
        "session_id": "s_interact",
    })
    assert r2.status_code == 200
    data = r2.json()
    assert data["ok"] is True
    assert data["version"] >= 2


def test_interact_user_edit(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "<!DOCTYPE html><html><body>edit-test</body></html>",
        "session_id": "s_interact_edit",
        "type": "html",
    })
    artifact_id = r.json()["artifact_id"]
    r2 = rest_post("/api/artifact/interact", {
        "artifact_id": artifact_id,
        "action": "user_edit",
        "payload": {"content": "<!DOCTYPE html><html><body>edited content</body></html>"},
    })
    assert r2.status_code == 200
    assert r2.json()["ok"] is True


def test_interact_invalid_action(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "<!DOCTYPE html><html><body>invalid-action</body></html>",
        "session_id": "s_interact_inv",
        "type": "html",
    })
    artifact_id = r.json()["artifact_id"]
    r2 = rest_post("/api/artifact/interact", {
        "artifact_id": artifact_id,
        "action": "hack",
        "payload": {},
    })
    assert r2.status_code == 400


def test_interact_not_found(rpc_server):
    r2 = rest_post("/api/artifact/interact", {
        "artifact_id": "nonexistent",
        "action": "state_change",
        "payload": {"content": "x"},
    })
    assert r2.status_code in (400, 500)


def test_interact_no_content_no_version_bump(rpc_server):
    r = rest_post("/api/artifact/render", {
        "content": "<!DOCTYPE html><html><body>no-bump</body></html>",
        "session_id": "s_interact_nobump",
        "type": "html",
    })
    artifact_id = r.json()["artifact_id"]
    r2 = rest_post("/api/artifact/interact", {
        "artifact_id": artifact_id,
        "action": "user_click",
        "payload": {},
    })
    assert r2.status_code == 200
    assert r2.json()["version"] == 1
