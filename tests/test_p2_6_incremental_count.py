import pytest

from fusion_artifacts_engine.compactor import compact_and_count, compact_content
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.token_counter import count_tokens, estimate_tokens


@pytest.fixture
def engine(tmp_path):
    return ArtifactEngine(config=ArtifactEngineConfig(storage_root=str(tmp_path)))


# ── estimate_tokens ──────────────────────────────────────


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_ascii_positive():
    assert estimate_tokens("hello world foo bar baz") >= 1


def test_estimate_tokens_cjk_uses_bytes_div3():
    # Pure CJK: byte_len far exceeds char_len, exercising the byte//3 path
    text = "你好世界" * 10
    est = estimate_tokens(text)
    assert est == len(text.encode("utf-8")) // 3


def test_estimate_tokens_is_cheap_upper_ish():
    # Estimate is of reasonable magnitude (not required to be exact), used by the compactor to skip clearly over-budget content
    long = "line of code here\n" * 200
    est = estimate_tokens(long)
    exact = count_tokens(long)
    # Same order of magnitude (estimate within 0.3x~3x of the exact value suffices)
    assert 0.3 * exact <= est <= 3 * exact


# ── compact_and_count ────────────────────────────────────


def test_compact_and_count_matches_separate():
    content = "# c\nx = 1\n\n\n\n# c2\ny = 2"
    budget = count_tokens(content) - 1
    compacted, n = compact_and_count(content, "code", token_budget=budget)
    # Token count returned by compact_and_count == an independent count_tokens(result)
    assert n == count_tokens(compacted)


def test_compact_and_count_already_within_budget():
    content = "short"
    compacted, n = compact_and_count(content, "code", token_budget=1000)
    assert compacted == content
    assert n == count_tokens(content)


# ── incremental truncation correctness ─────────────────────


def test_truncate_sections_no_budget_violation_many_sections():
    # Multi-section: the incremental prefix-sum path must not return a result over budget
    content = "# A\n" + "line\n" * 200 + "# B\n" + "line\n" * 200
    budget = count_tokens("# A\n" + "line\n" * 50)
    result = compact_content(content, "markdown", token_budget=budget)
    assert count_tokens(result) <= budget + 5


def test_truncate_sections_no_section_fallback_respects_budget():
    # No sections: fall back to ratio cut + exact retry loop, ending at ≤ budget
    content = "line\n" * 500
    budget = 50
    result = compact_content(content, "code", token_budget=budget)
    assert count_tokens(result) <= budget


def test_truncate_sections_preserves_prefix_up_to_budget():
    # Truncation keeps the head prefix. Most content after the head section is cut, but the head is preserved
    content = "# H\nkeepme\n\n# Tail\n" + "drop\n" * 300
    budget = count_tokens("# H\nkeepme\n")
    result = compact_content(content, "markdown", token_budget=budget)
    assert "keepme" in result
    assert "Tail" not in result


# ── patch reuse version.token_count ────────────────────────


async def test_patch_reuses_version_token_count(engine):
    art, v1, _ = await engine.create_artifact(
        session_id="s1", name="p.py", artifact_type="code", content="x = 1\n"
    )
    ver, info = await engine.patch_artifact(art.id, "append", content="y = 2\n")
    # tokens_net = new_tokens - old_tokens (append has no replaced)
    assert info["tokens_net"] == ver.token_count - v1.token_count
    assert info["tokens_added"] == ver.token_count - v1.token_count
    assert info["tokens_removed"] == 0


async def test_patch_replace_section_token_count_reused(engine):
    content = "# A\nold body line\n# B\nkeep\n"
    art, v1, _ = await engine.create_artifact(
        session_id="s1", name="r.md", artifact_type="markdown", content=content
    )
    ver, info = await engine.patch_artifact(
        art.id, "replace_section", anchor="A", content="# A\nnew body\n"
    )
    # new_tokens reuses ver.token_count, no second encoding
    assert ver.token_count > 0
    assert info["new_version"] == 2
    # tokens_net = (new - old + replaced) - replaced = new - old
    assert info["tokens_net"] == ver.token_count - v1.token_count


# ── auto_compact offloaded ─────────────────────────────────


async def test_auto_compact_large_content_offloaded(engine):
    # Large content goes through to_thread(compact_and_count) with a correct result
    content = "# H\n" + "# c\nx = 1\n" * 100
    art, v1, _ = await engine.create_artifact(
        session_id="s1", name="big.py", artifact_type="code", content=content
    )
    budget = max(1, v1.token_count // 2)
    result = await engine.auto_compact(art.id, token_budget=budget)
    assert result["compacted"] is True
    assert result["compacted_tokens"] < v1.token_count
    assert result["version"]["version_num"] == 2
    # compacted_tokens matches an independent count
    assert result["compacted_tokens"] == count_tokens(
        engine.storage.get_version(art.id, 2).content
    )
