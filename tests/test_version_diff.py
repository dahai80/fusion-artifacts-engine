import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine


@pytest.fixture
def engine_with_tmp(tmp_path):
    cfg = ArtifactEngineConfig(storage_root=str(tmp_path))
    return ArtifactEngine(config=cfg)


async def test_version_diff_basic(engine_with_tmp):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content="line1\nline2\nline3\n",
    )
    await engine.create_version(artifact.id, "line1\nline2_modified\nline3\n")
    result = engine.version_diff(artifact.id, 1, 2)
    assert "diff" in result
    assert result["lines_added"] > 0
    assert result["lines_removed"] > 0
    assert "---" in result["diff"]
    assert "+++" in result["diff"]


async def test_version_diff_same_version(engine_with_tmp):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content="hello\n",
    )
    result = engine.version_diff(artifact.id, 1, 1)
    assert result["diff"] == ""
    assert result["lines_added"] == 0
    assert result["lines_removed"] == 0


async def test_version_diff_artifact_not_found(engine_with_tmp):
    engine = engine_with_tmp
    with pytest.raises(ValueError, match="Artifact not found"):
        engine.version_diff("nonexistent", 1, 2)


async def test_version_diff_version_not_found(engine_with_tmp):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content="hello\n",
    )
    with pytest.raises(ValueError, match="Version 99 not found"):
        engine.version_diff(artifact.id, 1, 99)


async def test_version_diff_from_version_not_found(engine_with_tmp):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content="hello\n",
    )
    with pytest.raises(ValueError, match="Version 99 not found"):
        engine.version_diff(artifact.id, 99, 1)


async def test_version_diff_additions_only(engine_with_tmp):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content="line1\n",
    )
    await engine.create_version(artifact.id, "line1\nline2\nline3\n")
    result = engine.version_diff(artifact.id, 1, 2)
    assert result["lines_added"] >= 2
    assert result["lines_removed"] == 0


async def test_version_diff_deletions_only(engine_with_tmp):
    engine = engine_with_tmp
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content="line1\nline2\nline3\n",
    )
    await engine.create_version(artifact.id, "line1\n")
    result = engine.version_diff(artifact.id, 1, 2)
    assert result["lines_added"] == 0
    assert result["lines_removed"] >= 2


async def test_version_diff_rpc_method(engine_with_tmp):
    from fusion_artifacts_engine.rpc.methods import RPCHandler

    engine = engine_with_tmp
    handler = RPCHandler(engine)
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content="original\n",
    )
    await engine.create_version(artifact.id, "modified\n")
    result = await handler.dispatch(
        "artifact.version_diff",
        {"artifact_id": artifact.id, "from_version": 1, "to_version": 2},
    )
    assert "diff" in result
    assert result["lines_added"] >= 1
