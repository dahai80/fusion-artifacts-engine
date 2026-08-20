import pytest

from fusion_artifacts_engine.compactor import (
    _collapse_blank_lines,
    _remove_comments,
    _remove_decorators,
    compact_content,
)
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.token_counter import count_tokens


@pytest.fixture
def engine_with_tmp(tmp_path):
    cfg = ArtifactEngineConfig(storage_root=str(tmp_path))
    return ArtifactEngine(config=cfg)


def test_remove_comments_code():
    content = "# comment\nx = 1\n# another\ny = 2"
    result = _remove_comments(content, "code")
    assert "# comment" not in result
    assert "x = 1" in result
    assert "y = 2" in result


def test_remove_comments_markdown():
    content = "text\n<!-- html comment -->\nmore text"
    result = _remove_comments(content, "markdown")
    assert "html comment" not in result
    assert "text" in result


def test_collapse_blank_lines():
    content = "a\n\n\n\nb\n\n\n\nc"
    result = _collapse_blank_lines(content)
    assert "\n\n\n" not in result
    assert "a" in result
    assert "b" in result


def test_remove_decorators():
    content = "# Title\n---\n## Section\n***\nContent"
    result = _remove_decorators(content, "markdown")
    assert "---" not in result
    assert "***" not in result
    assert "# Title" in result


def test_compact_removes_comments_and_collapses():
    content = "# comment\nx = 1\n\n\n\n# another\ny = 2"
    original_tokens = count_tokens(content)
    result = compact_content(content, "code", token_budget=original_tokens - 1)
    assert "# comment" not in result
    assert "x = 1" in result


def test_compact_truncates_to_budget():
    content = "# A\n" + "line\n" * 200 + "# B\n" + "line\n" * 200
    budget = count_tokens("# A\n" + "line\n" * 50)
    result = compact_content(content, "markdown", token_budget=budget)
    assert count_tokens(result) <= budget + 5


def test_compact_already_within_budget():
    content = "short content"
    result = compact_content(content, "code", token_budget=1000)
    assert result == content


async def test_auto_compact_creates_version(engine_with_tmp):
    engine = engine_with_tmp
    content = "# Header\n" + "# comment\nx = 1\n" * 50
    artifact, v1, _ = await engine.create_artifact(
        session_id="s1",
        name="big.py",
        artifact_type="code",
        content=content,
    )
    original_tokens = v1.token_count
    budget = max(1, original_tokens // 2)
    result = await engine.auto_compact(artifact.id, token_budget=budget)
    assert result["compacted"] is True
    assert result["compacted_tokens"] < original_tokens
    assert result["savings_pct"] > 0
    assert result["version"]["version_num"] == 2


async def test_auto_compact_already_within_budget(engine_with_tmp):
    engine = engine_with_tmp
    content = "short"
    artifact, _, _ = await engine.create_artifact(
        session_id="s1",
        name="small.py",
        artifact_type="code",
        content=content,
    )
    result = await engine.auto_compact(artifact.id, token_budget=1000)
    assert result["compacted"] is False
    assert result["savings_pct"] == 0.0


async def test_auto_compact_not_found(engine_with_tmp):
    engine = engine_with_tmp
    with pytest.raises(ValueError, match="not found"):
        await engine.auto_compact("art_nonexistent", token_budget=100)
