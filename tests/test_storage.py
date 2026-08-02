import tempfile
import shutil
from pathlib import Path
import pytest
from fusion_artifacts_engine.models import Artifact, ArtifactVersion
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage


@pytest.fixture
def storage():
    tmp = Path(tempfile.mkdtemp())
    s = SQLiteStorage(tmp / "meta.db", tmp / "content")
    yield s
    s.close()
    shutil.rmtree(tmp)


def test_save_and_get_artifact(storage):
    a = Artifact(id="art_test1", session_id="s1", name="hello.py", type="code")
    storage.save_artifact(a)
    got = storage.get_artifact("art_test1")
    assert got is not None
    assert got.name == "hello.py"


def test_list_artifacts(storage):
    storage.save_artifact(Artifact(id="art_a", session_id="s1", name="a.py", type="code"))
    storage.save_artifact(Artifact(id="art_b", session_id="s1", name="b.py", type="code"))
    storage.save_artifact(Artifact(id="art_c", session_id="s2", name="c.py", type="code"))
    assert len(storage.list_artifacts("s1")) == 2
    assert len(storage.list_artifacts("s2")) == 1


def test_soft_delete(storage):
    storage.save_artifact(Artifact(id="art_d", session_id="s1", name="d.py", type="code"))
    ok = storage.delete_artifact("art_d", soft_delete=True)
    assert ok
    assert len(storage.list_artifacts("s1")) == 0
    got = storage.get_artifact("art_d")
    assert got.is_deleted


def test_hard_delete(storage):
    storage.save_artifact(Artifact(id="art_e", session_id="s1", name="e.py", type="code"))
    ok = storage.delete_artifact("art_e", soft_delete=False)
    assert ok
    assert storage.get_artifact("art_e") is None


def test_save_and_get_version(storage):
    storage.save_artifact(Artifact(id="art_v1", session_id="s1", name="v.py", type="code"))
    v = ArtifactVersion(artifact_id="art_v1", version_num=1, content="hello", size_bytes=5)
    storage.save_version(v)
    got = storage.get_version("art_v1", 1)
    assert got is not None
    assert got.content == "hello"


def test_large_content_to_file(tmp_path):
    s = SQLiteStorage(tmp_path / "meta.db", tmp_path / "content", small_content_limit=50)
    s.save_artifact(Artifact(id="art_big", session_id="s1", name="big.py", type="code"))
    big = "x" * 200
    s.save_version(ArtifactVersion(artifact_id="art_big", version_num=1, content=big, size_bytes=100))
    got = s.get_version("art_big", 1)
    assert got.content == big
    assert got.content_path is not None
    s.close()


def test_versions_survive_artifact_update(storage):
    storage.save_artifact(Artifact(id="art_casc", session_id="s1", name="casc.py", type="code", current_version=1))
    storage.save_version(ArtifactVersion(artifact_id="art_casc", version_num=1, content="v1", size_bytes=5))
    a = storage.get_artifact("art_casc")
    a.current_version = 2
    storage.save_artifact(a)
    storage.save_version(ArtifactVersion(artifact_id="art_casc", version_num=2, content="v2", size_bytes=5))
    v1 = storage.get_version("art_casc", 1)
    assert v1 is not None
    assert v1.content == "v1"
    v2 = storage.get_version("art_casc", 2)
    assert v2 is not None
    assert v2.content == "v2"


def test_list_versions(storage):
    storage.save_artifact(Artifact(id="art_lv", session_id="s1", name="lv.py", type="code"))
    for i in range(1, 4):
        storage.save_version(ArtifactVersion(artifact_id="art_lv", version_num=i, content=f"v{i}", size_bytes=5))
    versions = storage.list_versions("art_lv")
    assert len(versions) == 3
    assert versions[0].version_num == 3
