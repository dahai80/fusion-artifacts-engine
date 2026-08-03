import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine


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
    assert result["total_tokens"] > 0
    assert result["title"] == "doc.md"
    assert "sections" in result
    assert isinstance(result["sections"], list)
    for sec in result["sections"]:
        assert "anchor" in sec
        assert "tokens" in sec


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
    assert result["total_tokens"] > 0


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
    assert isinstance(result["section"], dict)
    assert result["section"]["anchor"] == "Section A"
    assert result["section"]["tokens"] > 0


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
    assert isinstance(result["section"], dict)
    assert result["section"]["anchor"] == "Section B"


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
    result = engine.context_budget(session_id="s1")
    assert result["total_artifact_tokens"] > 0
    assert result["artifact_count"] == 2
    assert len(result["artifacts"]) == 2
    assert result["artifacts"][0]["artifact_id"] is not None
    assert result["artifacts"][0]["name"] is not None
    assert result["artifacts"][0]["tokens"] >= 0
    assert result["context_window"] == 200000
    assert result["utilization_percent"] >= 0
    assert result["warning"] is False
    assert result["recommendation"] is None


async def test_context_budget_empty_session(engine_with_tmp):
    engine = engine_with_tmp
    result = engine.context_budget(session_id="empty_session")
    assert result["total_artifact_tokens"] == 0
    assert result["artifact_count"] == 0
    assert result["artifacts"] == []
    assert result["context_window"] == 200000
    assert result["utilization_percent"] == 0.0
    assert result["warning"] is False
    assert result["recommendation"] is None


async def test_context_budget_with_context_window(engine_with_tmp):
    engine = engine_with_tmp
    await engine.create_artifact(
        session_id="s2",
        name="big.md",
        artifact_type="markdown",
        content="x" * 5000,
    )
    result = engine.context_budget(
        session_id="s2",
        context_window=131072,
    )
    assert result["context_window"] == 131072
    assert result["utilization_percent"] > 0
    assert result["warning"] is False
    assert result["recommendation"] is None


async def test_context_budget_warning_above_70(engine_with_tmp):
    engine = engine_with_tmp
    big_content = "word " * 8000
    await engine.create_artifact(
        session_id="s3",
        name="huge.md",
        artifact_type="markdown",
        content=big_content,
    )
    result = engine.context_budget(
        session_id="s3",
        context_window=1000,
    )
    assert result["context_window"] == 1000
    assert result["utilization_percent"] > 70
    assert result["warning"] is True
    assert (
        result["recommendation"]
        == "Consider using preview_only mode for artifact injection."
    )


async def test_context_budget_no_session_id(engine_with_tmp):
    engine = engine_with_tmp
    await engine.create_artifact(
        session_id="sa",
        name="a.md",
        artifact_type="markdown",
        content="hello",
    )
    await engine.create_artifact(
        session_id="sb",
        name="b.md",
        artifact_type="markdown",
        content="world",
    )
    result = engine.context_budget()
    assert result["artifact_count"] >= 2
    assert result["total_artifact_tokens"] > 0


async def test_load_sections_with_token_counts(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\nIntro\n## Section A\nContent A line 1\nContent A line 2\n## Section B\nContent B"
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    result = engine.load_artifact(artifact.id, preview_only=True)
    assert "sections" in result
    assert len(result["sections"]) == 3
    assert result["sections"][0]["anchor"] == "Title"
    assert result["sections"][0]["tokens"] > 0
    assert result["sections"][1]["anchor"] == "Section A"
    assert result["sections"][1]["tokens"] > 0
    assert result["sections"][2]["anchor"] == "Section B"


async def test_load_sections_code_artifact(engine_with_tmp):
    engine = engine_with_tmp
    content = (
        "class Foo:\n    pass\n\ndef bar():\n    return 1\n\nasync def baz():\n    pass"
    )
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="test.py",
        artifact_type="code",
        content=content,
    )
    result = engine.load_artifact(artifact.id, preview_only=False)
    assert len(result["sections"]) == 3
    assert result["sections"][0]["anchor"] == "Foo"
    assert result["sections"][0]["tokens"] > 0
    assert result["content"] is not None
