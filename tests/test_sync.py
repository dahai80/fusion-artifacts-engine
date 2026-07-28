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
async def test_export_code(engine):
    art, _, _ = await engine.create_artifact("s1", "app.py", "code", "print('hello')")
    result = engine.export_code(art.id, "python")
    assert result["code"] == "print('hello')"
    assert result["language"] == "python"
    assert result["ext"] == "py"


@pytest.mark.asyncio
async def test_export_code_auto_ext(engine):
    art, _, _ = await engine.create_artifact("s1", "style.css", "code", "body {}")
    result = engine.export_code(art.id)
    assert result["ext"] == "css"


@pytest.mark.asyncio
async def test_import_code(engine):
    art, ver, ref = await engine.import_code(
        "s1", "def foo(): pass", language="python", name="foo.py"
    )
    assert art.name == "foo.py"
    assert art.type == "code"


@pytest.mark.asyncio
async def test_import_code_html(engine):
    art, ver, ref = await engine.import_code(
        "s1", "<h1>Hi</h1>", language="html", name="page.html"
    )
    assert art.type == "html"


@pytest.mark.asyncio
async def test_watch_register_poll(engine):
    art, _, _ = await engine.create_artifact("s1", "test.py", "code", "v1")
    engine.register_watcher(art.id, "w1")
    await engine.create_version(art.id, "v2", "update")
    events = engine.get_watch_events(art.id, since_version=1)
    assert len(events) == 1
    assert events[0]["version"] == 2
    engine.unregister_watcher(art.id, "w1")


@pytest.mark.asyncio
async def test_sync_artifact_to_code(engine):
    art, _, _ = await engine.create_artifact("s1", "sync.py", "code", "content")
    code_path = str(Path(engine.config.storage_root) / "sync_out.py")
    result = await engine.sync_artifact_file(art.id, code_path, "artifact_to_code")
    assert result["direction"] == "artifact_to_code"
    assert Path(code_path).read_text() == "content"


@pytest.mark.asyncio
async def test_sync_code_to_artifact(engine):
    art, _, _ = await engine.create_artifact("s1", "sync.py", "code", "v1")
    code_path = Path(engine.config.storage_root) / "sync_in.py"
    code_path.write_text("updated code", encoding="utf-8")
    result = await engine.sync_artifact_file(art.id, str(code_path), "code_to_artifact")
    assert result["direction"] == "code_to_artifact"
    assert result["version"] == 2
