import pytest

from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.token_counter import count_tokens


@pytest.fixture
def engine_with_tmp(tmp_path):
    from fusion_artifacts_engine.config import ArtifactEngineConfig

    cfg = ArtifactEngineConfig(storage_root=str(tmp_path))
    return ArtifactEngine(config=cfg)


async def test_token_count_on_create(engine_with_tmp):
    engine = engine_with_tmp
    _artifact, version, _ = await engine.create_artifact(
        session_id="test-session",
        name="test.md",
        artifact_type="markdown",
        content="# Hello World\nThis is a test document.",
    )
    assert version.token_count > 0
    assert version.token_count == count_tokens(
        "# Hello World\nThis is a test document."
    )


async def test_token_count_on_update(engine_with_tmp):
    engine = engine_with_tmp
    _artifact, v1, _ = await engine.create_artifact(
        session_id="test-session",
        name="test.md",
        artifact_type="markdown",
        content="Short content.",
    )
    assert v1.token_count > 0
    v2, _ = await engine.create_version(
        _artifact.id,
        "This is much longer content with more tokens than the original version.",
    )
    assert v2.token_count > v1.token_count


async def test_section_index_markdown(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\n## Section A\nSome text\n## Section B\nMore text\n### Sub\nDeep"
    _artifact, version, _ = await engine.create_artifact(
        session_id="test-session",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    assert version.section_index is not None
    import json

    sections = (
        json.loads(version.section_index)
        if isinstance(version.section_index, str)
        else version.section_index
    )
    assert len(sections) == 4
    assert sections[0] == {"anchor": "Title", "level": 1}
    assert sections[1] == {"anchor": "Section A", "level": 2}
    assert sections[2] == {"anchor": "Section B", "level": 2}
    assert sections[3] == {"anchor": "Sub", "level": 3}


async def test_section_index_code(engine_with_tmp):
    engine = engine_with_tmp
    content = (
        "class Foo:\n    pass\n\ndef bar():\n    return 1\n\nasync def baz():\n    pass"
    )
    _artifact, version, _ = await engine.create_artifact(
        session_id="test-session",
        name="test.py",
        artifact_type="code",
        content=content,
    )
    assert version.section_index is not None
    import json

    sections = (
        json.loads(version.section_index)
        if isinstance(version.section_index, str)
        else version.section_index
    )
    assert len(sections) == 3
    assert sections[0] == {"anchor": "Foo", "level": 1}
    assert sections[1] == {"anchor": "bar", "level": 1}
    assert sections[2] == {"anchor": "baz", "level": 1}


async def test_patch_replace_section(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\n## Section A\nOld content A\n## Section B\nContent B"
    artifact, _, _ = await engine.create_artifact(
        session_id="test-session",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    version, info = await engine.patch_artifact(
        artifact_id=artifact.id,
        operation="replace_section",
        anchor="Section A",
        content="## Section A\nNew content A\n",
    )
    assert info["new_version"] == 2
    assert "New content A" in version.content
    assert "Old content A" not in version.content
    assert "Content B" in version.content


async def test_patch_append(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\nInitial content"
    artifact, _, _ = await engine.create_artifact(
        session_id="test-session",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    version, info = await engine.patch_artifact(
        artifact_id=artifact.id,
        operation="append",
        content="\n## Appended\nNew section",
    )
    assert info["new_version"] == 2
    assert "Initial content" in version.content
    assert "Appended" in version.content


async def test_patch_prepend(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\nOriginal"
    artifact, _, _ = await engine.create_artifact(
        session_id="test-session",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    version, info = await engine.patch_artifact(
        artifact_id=artifact.id,
        operation="prepend",
        content="# Header\nPrepended content\n\n",
    )
    assert info["new_version"] == 2
    assert version.content.startswith("# Header")
    assert "Original" in version.content


async def test_patch_delete_section(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\n## Section A\nContent A\n## Section B\nContent B"
    artifact, _, _ = await engine.create_artifact(
        session_id="test-session",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    version, info = await engine.patch_artifact(
        artifact_id=artifact.id,
        operation="delete_section",
        anchor="Section A",
    )
    assert info["new_version"] == 2
    assert "Section A" not in version.content
    assert "Content A" not in version.content
    assert "Section B" in version.content
    assert "Content B" in version.content


async def test_patch_anchor_not_found(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\n## Section A\nContent A"
    artifact, _, _ = await engine.create_artifact(
        session_id="test-session",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    with pytest.raises(ValueError, match="not found"):
        await engine.patch_artifact(
            artifact_id=artifact.id,
            operation="replace_section",
            anchor="NonExistent",
            content="## NonExistent\nNew",
        )


async def test_patch_multiple_match(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\n## Section A\nContent A\n## Section A\nContent A2"
    artifact, _, _ = await engine.create_artifact(
        session_id="test-session",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    with pytest.raises(ValueError, match="Multiple matches"):
        await engine.patch_artifact(
            artifact_id=artifact.id,
            operation="replace_section",
            anchor="Section A",
            content="## Section A\nReplaced\n",
        )


async def test_patch_optimistic_lock(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Title\n## Section A\nContent A"
    artifact, _, _ = await engine.create_artifact(
        session_id="test-session",
        name="doc.md",
        artifact_type="markdown",
        content=content,
    )
    with pytest.raises(ValueError, match="Optimistic lock failed"):
        await engine.patch_artifact(
            artifact_id=artifact.id,
            operation="append",
            content="more",
            expected_version=999,
        )
