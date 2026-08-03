import pytest

from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.config import ArtifactEngineConfig


@pytest.fixture
def engine_with_tmp(tmp_path):
    cfg = ArtifactEngineConfig(storage_root=str(tmp_path))
    return ArtifactEngine(config=cfg)


async def test_load_preview_only(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\n## Section A\nContent A\n## Section B\nContent B"
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    result = engine.load_artifact(artifact.id, preview_only=True)
    assert result["content"] is None
    assert result["token_count"] > 0
    assert result["name"] == "doc.md"
    assert result["section_index"] is not None


async def test_load_full_content(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\nHello world"
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    result = engine.load_artifact(artifact.id, preview_only=False)
    assert result["content"] == content
    assert result["token_count"] > 0


async def test_load_by_section(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\n## Section A\nContent A\n## Section B\nContent B"
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    result = engine.load_artifact(artifact.id, section="Section A")
    assert "Content A" in result["content"]
    assert "Content B" not in result["content"]
    assert result["section"] == "Section A"


async def test_load_by_section_with_hash_prefix(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\n## Section A\nContent A\n## Section B\nContent B"
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    result = engine.load_artifact(artifact.id, section="# Section B")
    assert "Content B" in result["content"]
    assert "Content A" not in result["content"]


async def test_load_section_not_found(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\nHello"
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    with pytest.raises(ValueError, match="not found"):
        engine.load_artifact(artifact.id, section="NonExistent")


async def test_load_artifact_not_found(engine_with_tmp):
    engine = engine_with_tmp
    with pytest.raises(ValueError, match="not found"):
        engine.load_artifact("art_nonexistent", preview_only=True)


async def test_context_budget(engine_with_tmp):
    engine = engine_with_tmp
    await engine.create_artifact(
        session_id="s1",
        name="a.md",
        artifact_type="markdown",
        content="# Hello\nWorld",
    )
    await engine.create_artifact(
        session_id="s1",
        name="b.py",
        artifact_type="code",
        content="def foo(): pass",
    )
    result = engine.context_budget("s1")
    assert result["used_tokens"] > 0
    assert result["total_budget"] == 200000
    assert result["available_tokens"] == 200000 - result["used_tokens"]
    assert result["utilization_pct"] >= 0
    assert len(result["artifacts"]) == 2


async def test_context_budget_empty_session(engine_with_tmp):
    engine = engine_with_tmp
    result = engine.context_budget("empty_session")
    assert result["used_tokens"] == 0
    assert result["available_tokens"] == 200000
    assert result["utilization_pct"] == 0.0
    assert result["artifacts"] == []
