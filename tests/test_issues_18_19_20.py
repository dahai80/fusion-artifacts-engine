import time
import pytest
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.rpc.event_bus import EventBus


@pytest.fixture
def engine(tmp_path):
    config = ArtifactEngineConfig(storage_root=str(tmp_path), allow_no_auth=True)
    eng = ArtifactEngine(config)
    yield eng
    eng.close()


@pytest.fixture
def event_bus():
    return EventBus()


class TestExternalCreate:
    @pytest.mark.asyncio
    async def test_create_external_artifact(self, engine):
        artifact, version, ref_text = await engine.create_external_artifact(
            source_module="fusion-mlx",
            workspace_id="ws-001",
            name="test_code",
            artifact_type="code",
            content="print('hello')",
            workflow_run_id="run-001",
        )
        assert artifact.source_module == "fusion-mlx"
        assert artifact.workspace_id == "ws-001"
        assert artifact.workflow_run_id == "run-001"
        assert artifact.session_id == "ext_fusion-mlx_ws-001"
        assert version.content == "print('hello')"
        assert ref_text

    @pytest.mark.asyncio
    async def test_list_by_source(self, engine):
        await engine.create_external_artifact(
            source_module="fusion-mlx",
            workspace_id="ws-001",
            name="a1",
            artifact_type="code",
            content="c1",
        )
        await engine.create_external_artifact(
            source_module="fusion-mlx",
            workspace_id="ws-002",
            name="a2",
            artifact_type="markdown",
            content="c2",
        )
        await engine.create_external_artifact(
            source_module="fusion-studio",
            workspace_id="ws-001",
            name="a3",
            artifact_type="html",
            content="c3",
        )
        all_mlx = engine.list_by_source("fusion-mlx")
        assert len(all_mlx) == 2
        ws1_mlx = engine.list_by_source("fusion-mlx", workspace_id="ws-001")
        assert len(ws1_mlx) == 1
        assert ws1_mlx[0].name == "a1"
        run_mlx = engine.list_by_source("fusion-mlx", workflow_run_id="run-999")
        assert len(run_mlx) == 0

    @pytest.mark.asyncio
    async def test_create_external_with_project_kb(self, engine):
        artifact, version, ref_text = await engine.create_external_artifact(
            source_module="fusion-mlx",
            workspace_id="ws-001",
            name="archived",
            artifact_type="code",
            content="archived content",
            project_id="proj-001",
        )
        assert artifact.project_id == "proj-001"
        fetched = engine.get_artifact(artifact.id)
        assert fetched is not None


class TestEventBus:
    def test_subscribe_and_publish(self, event_bus):
        q = event_bus.subscribe()
        event_bus.publish("artifact.created", {"artifact_id": "a1"})
        event = q.get(timeout=1)
        assert event["event_type"] == "artifact.created"
        assert event["artifact_id"] == "a1"
        event_bus.unsubscribe(q)

    def test_multiple_subscribers(self, event_bus):
        q1 = event_bus.subscribe()
        q2 = event_bus.subscribe()
        event_bus.publish("test", {"key": "val"})
        e1 = q1.get(timeout=1)
        e2 = q2.get(timeout=1)
        assert e1["key"] == "val"
        assert e2["key"] == "val"
        event_bus.unsubscribe(q1)
        event_bus.unsubscribe(q2)

    def test_full_subscriber_dropped(self, event_bus):
        q = event_bus.subscribe()
        for i in range(q.maxsize):
            q.put_nowait({"fill": i})
        event_bus.publish("test", {"should_drop": True})
        assert len(event_bus._subscribers) == 0

    def test_unsubscribe(self, event_bus):
        q = event_bus.subscribe()
        assert len(event_bus._subscribers) == 1
        event_bus.unsubscribe(q)
        assert len(event_bus._subscribers) == 0


class TestBatchQueryFilters:
    @pytest.mark.asyncio
    async def test_list_all_with_owner_filter(self, engine):
        await engine.create_artifact(
            session_id="s1",
            name="a1",
            artifact_type="code",
            content="c1",
        )
        await engine.create_artifact(
            session_id="s2",
            name="a2",
            artifact_type="code",
            content="c2",
        )
        _, total = engine.list_all_artifacts()
        assert total >= 2
        _, total_owned = engine.list_all_artifacts(
            filters={"owner_user_id": "nonexistent"},
        )
        assert total_owned == 0

    @pytest.mark.asyncio
    async def test_list_all_with_since_until(self, engine):
        before = time.time() - 1
        await engine.create_artifact(
            session_id="s1",
            name="a1",
            artifact_type="code",
            content="c1",
        )
        after = time.time() + 1
        _, total = engine.list_all_artifacts(
            filters={"since": before, "until": after},
        )
        assert total >= 1

    @pytest.mark.asyncio
    async def test_list_all_with_kind_filter(self, engine):
        await engine.create_artifact(
            session_id="s1",
            name="a1",
            artifact_type="code",
            content="c1",
            kind="app",
        )
        await engine.create_artifact(
            session_id="s1",
            name="a2",
            artifact_type="markdown",
            content="c2",
            kind="document",
        )
        artifacts, total = engine.list_all_artifacts(
            filters={"kind": "app"},
        )
        assert total >= 1
        assert all(a.kind == "app" for a in artifacts)
