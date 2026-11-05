import shutil
import tempfile
from pathlib import Path

import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.errors import ConflictError
from fusion_artifacts_engine.rpc.methods import RPCHandler
from fusion_artifacts_engine.utils import deep_link_url


@pytest.fixture
def handler():
    tmp = Path(tempfile.mkdtemp())
    cfg = ArtifactEngineConfig(storage_root=tmp / "artifacts", allow_no_auth=True)
    eng = ArtifactEngine(cfg)
    h = RPCHandler(eng)
    yield h, eng
    eng.close()
    shutil.rmtree(tmp)


async def test_workflow_create_stamps_tags_and_url(handler):
    # #119: one-call contract for pipeline nodes — create + project/stage tags + deep link
    h, _eng = handler
    r = await h.dispatch(
        "artifact.workflow.create",
        {
            "workspace_id": "run-1",
            "name": "PRD draft",
            "content": "# PRD\n\nbody",
            "type": "markdown",
            "project_slug": "demo-app",
            "stage": "prd",
            "workflow_run_id": "wf-42",
        },
    )
    assert r["artifact_id"].startswith("art_")
    assert r["version"] == 1
    assert "/shell/fusion-artifacts/#/a/" in r["url"]
    assert "?v=1" in r["url"]
    tags = await h.dispatch("artifact.list_artifact_tags", {"artifact_id": r["artifact_id"]})
    names = {t["name"] for t in tags["tags"]}
    assert "project:demo-app" in names
    assert "stage:prd" in names
    art = r["artifact"]
    assert art["source_module"] == "cowork"
    assert art["workflow_run_id"] == "wf-42"


async def test_workflow_create_default_source_and_type(handler):
    h, _eng = handler
    r = await h.dispatch(
        "artifact.workflow.create",
        {"workspace_id": "w1", "name": "n", "content": "x"},
    )
    assert r["artifact"]["source_module"] == "cowork"
    assert r["artifact"]["type"] == "markdown"


async def test_workflow_create_rejects_bad_type(handler):
    h, _eng = handler
    with pytest.raises(ValueError):
        await h.dispatch(
            "artifact.workflow.create",
            {"workspace_id": "w1", "name": "n", "content": "x", "type": "exe"},
        )


async def test_workflow_revision_creates_next_version(handler):
    # #119: gate A/B rejection loop lands the revised content as v2/v3 with the reason
    h, _eng = handler
    r1 = await h.dispatch(
        "artifact.workflow.create",
        {"workspace_id": "w1", "name": "plan", "content": "v1"},
    )
    aid = r1["artifact_id"]
    r2 = await h.dispatch(
        "artifact.workflow.revision",
        {"artifact_id": aid, "content": "v2 after gate A", "reason": "gate A findings"},
    )
    assert r2["version"] == 2
    assert "?v=2" in r2["url"]
    r3 = await h.dispatch(
        "artifact.workflow.revision",
        {"artifact_id": aid, "content": "v3 after gate B", "reason": "gate B findings"},
    )
    assert r3["version"] == 3
    c = await h.dispatch("artifact.get_content", {"artifact_id": aid, "version": 3})
    assert c["content"] == "v3 after gate B"


async def test_workflow_revision_conflict_on_hash_mismatch(handler):
    h, _eng = handler
    r1 = await h.dispatch(
        "artifact.workflow.create",
        {"workspace_id": "w1", "name": "plan", "content": "v1"},
    )
    aid = r1["artifact_id"]
    with pytest.raises(ConflictError) as ei:
        await h.dispatch(
            "artifact.workflow.revision",
            {
                "artifact_id": aid,
                "content": "v2",
                "expected_content_hash": "deadbeef",
            },
        )
    assert "deadbeef" in str(ei.value)


async def test_comment_roundtrip_and_version_filter(handler):
    # #119: review comments attach per version; list filters by version
    h, _eng = handler
    r1 = await h.dispatch(
        "artifact.workflow.create",
        {"workspace_id": "w1", "name": "doc", "content": "v1"},
    )
    aid = r1["artifact_id"]
    await h.dispatch(
        "artifact.workflow.revision",
        {"artifact_id": aid, "content": "v2", "reason": "rev"},
    )
    await h.dispatch(
        "artifact.comment",
        {"artifact_id": aid, "version": 1, "comment": "gate A: intro weak", "author": "reviewer1"},
    )
    await h.dispatch(
        "artifact.comment",
        {"artifact_id": aid, "version": 2, "comment": "gate B: looks good", "author": "reviewer2"},
    )
    all_c = await h.dispatch("artifact.list_comments", {"artifact_id": aid})
    assert len(all_c["comments"]) == 2
    assert all_c["comments"][0]["version"] == 1
    assert all_c["comments"][0]["author"] == "reviewer1"
    v1 = await h.dispatch("artifact.list_comments", {"artifact_id": aid, "version": 1})
    assert len(v1["comments"]) == 1
    assert v1["comments"][0]["comment"] == "gate A: intro weak"
    assert v1["comments"][0]["version"] == 1


async def test_deep_link_relative_without_gateway_url(handler):
    _, eng = handler
    assert eng.config.gateway_url == "http://127.0.0.1:11432"
    url = deep_link_url(eng.config, "art_x", 2)
    assert url == "http://127.0.0.1:11432/shell/fusion-artifacts/#/a/art_x?v=2"
    eng.config.gateway_url = ""
    assert deep_link_url(eng.config, "art_x") == "/shell/fusion-artifacts/#/a/art_x"
    eng.config.gateway_module_id = "custom-mod"
    assert deep_link_url(eng.config, "art_x", 3) == "/shell/custom-mod/#/a/art_x?v=3"


async def test_sse_publishes_review_comment(handler):
    # review_comment events must publish to the EventBus with kind so SSE delivers them
    h, eng = handler
    r1 = await h.dispatch(
        "artifact.workflow.create",
        {"workspace_id": "w1", "name": "doc", "content": "v1"},
    )
    aid = r1["artifact_id"]
    bus = eng.event_bus
    sub = bus.subscribe()
    try:
        await h.dispatch(
            "artifact.comment",
            {"artifact_id": aid, "version": 1, "comment": "note"},
        )
        got = [sub.get_nowait() for _ in range(sub.qsize())]
        kinds = [e.get("event_type") for e in got]
        assert "review_comment" in kinds
        ev = next(e for e in got if e.get("event_type") == "review_comment")
        assert ev.get("kind") == "document"
    finally:
        bus.unsubscribe(sub)
