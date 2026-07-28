import tempfile
import shutil
from pathlib import Path
import pytest
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d)


@pytest.fixture
def engine(tmp_dir):
    cfg = ArtifactEngineConfig(storage_root=tmp_dir / "artifacts")
    eng = ArtifactEngine(cfg)
    yield eng
    eng.close()


@pytest.mark.asyncio
async def test_create_artifact(engine):
    art, ver, ref = await engine.create_artifact("s1", "test.py", "code", "print('hi')", summary="test")
    assert art.id.startswith("art_")
    assert art.name == "test.py"
    assert ver.version_num == 1
    assert "test.py" in ref


@pytest.mark.asyncio
async def test_get_artifact(engine):
    art, _, _ = await engine.create_artifact("s1", "app.py", "code", "code")
    got = engine.get_artifact(art.id)
    assert got is not None
    assert got.name == "app.py"


@pytest.mark.asyncio
async def test_list_artifacts(engine):
    await engine.create_artifact("s1", "a.py", "code", "a")
    await engine.create_artifact("s1", "b.py", "code", "b")
    listed = engine.list_artifacts("s1")
    assert len(listed) == 2


@pytest.mark.asyncio
async def test_delete_artifact_soft(engine):
    art, _, _ = await engine.create_artifact("s1", "x.py", "code", "x")
    ok = engine.delete_artifact(art.id, soft_delete=True)
    assert ok
    listed = engine.list_artifacts("s1")
    assert len(listed) == 0
    got = engine.get_artifact(art.id)
    assert got.is_deleted


@pytest.mark.asyncio
async def test_create_version(engine):
    art, v1, _ = await engine.create_artifact("s1", "main.py", "code", "v1 code")
    v2, ref2 = await engine.create_version(art.id, "v2 code", "updated")
    assert v2.version_num == 2
    content = engine.get_version_content(art.id, 1)
    assert content is not None
    assert "v1 code" in content.content


@pytest.mark.asyncio
async def test_rollback_version(engine):
    art, _, _ = await engine.create_artifact("s1", "main.py", "code", "original")
    await engine.create_version(art.id, "changed", "v2")
    v3, _ = await engine.rollback_version(art.id, 1)
    assert v3.version_num == 3
    assert v3.content == "original"


@pytest.mark.asyncio
async def test_check_safety(engine):
    safe, total, remaining = await engine.check_safety([{"role": "user", "content": "hello"}])
    assert safe is True
    assert total > 0


@pytest.mark.asyncio
async def test_should_create_artifact(engine):
    assert engine.should_create_artifact("line\n" * 35, "code") is True
    assert engine.should_create_artifact("short", "text") is False


@pytest.mark.asyncio
async def test_inject(engine):
    art, _, ref = await engine.create_artifact("s1", "test.py", "code", "print('hello')", summary="test")
    messages = [{"role": "assistant", "content": ref}]
    injected, total, safe = await engine.inject(messages)
    assert "print('hello')" in injected[0]["content"]
