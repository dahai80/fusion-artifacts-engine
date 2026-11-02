import json

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.ipc_consumer import DesignIPCConsumer


def _engine(tmp_path):
    return ArtifactEngine(
        ArtifactEngineConfig(storage_root=tmp_path / "artifacts", ipc_enabled=False)
    )


def _message(session="s-abc", version=1, title="Design Demo", document='{"pages": []}'):
    return {
        "target": {"Custom": "fusion-artifacts-engine"},
        "action": "save_artifact",
        "payload": {
            "source": "fusion-design",
            "title": title,
            "session": session,
            "version": version,
            "document": document,
            "design_system": None,
        },
    }


def _write(directory, name, data):
    path = directory / name
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


def _consumer(tmp_path, engine):
    directory = tmp_path / "ipc"
    directory.mkdir(parents=True, exist_ok=True)
    return DesignIPCConsumer(engine, directory, poll_interval=0.5), directory


def test_save_artifact_creates_artifact(tmp_path):
    engine = _engine(tmp_path)
    consumer, directory = _consumer(tmp_path, engine)
    _write(directory, "1_2_0.json", _message())
    try:
        assert consumer.process_once() == 1
        artifacts, total = engine.storage.list_all_artifacts()
        assert total == 1
        art = artifacts[0]
        assert art.source_module == "fusion-design"
        assert art.type == "data"
        assert art.name == "Design Demo"
        assert art.metadata["design_session"] == "s-abc"
        assert art.metadata["design_version"] == 1
        version = engine.storage.get_version(art.id, 1)
        assert version.content == '{"pages": []}'
        assert not list(directory.glob("*.json"))
        assert len(list((directory / ".processed").glob("*.json"))) == 1
    finally:
        engine.close()


def test_duplicate_version_not_duplicated(tmp_path):
    engine = _engine(tmp_path)
    consumer, directory = _consumer(tmp_path, engine)
    try:
        _write(directory, "a.json", _message(version=1))
        assert consumer.process_once() == 1
        _write(directory, "b.json", _message(version=1))
        assert consumer.process_once() == 0
        artifacts, total = engine.storage.list_all_artifacts()
        assert total == 1
        versions = engine.storage.list_versions(artifacts[0].id)
        assert len(versions) == 1
    finally:
        engine.close()


def test_version_bump_appends_version(tmp_path):
    engine = _engine(tmp_path)
    consumer, directory = _consumer(tmp_path, engine)
    try:
        _write(directory, "a.json", _message(version=1))
        consumer.process_once()
        _write(directory, "b.json", _message(version=2, document='{"pages": [1]}'))
        assert consumer.process_once() == 1
        artifacts, total = engine.storage.list_all_artifacts()
        assert total == 1
        art = engine.storage.get_artifact(artifacts[0].id)
        assert art.metadata["design_version"] == 2
        versions = engine.storage.list_versions(art.id)
        assert len(versions) == 2
        assert engine.storage.get_version(art.id, 2).content == '{"pages": [1]}'
    finally:
        engine.close()


def test_distinct_sessions_get_distinct_artifacts(tmp_path):
    engine = _engine(tmp_path)
    consumer, directory = _consumer(tmp_path, engine)
    try:
        _write(directory, "a.json", _message(session="s-1"))
        _write(directory, "b.json", _message(session="s-2"))
        assert consumer.process_once() == 2
        _artifacts, total = engine.storage.list_all_artifacts()
        assert total == 2
    finally:
        engine.close()


def test_malformed_message_fails_after_attempts(tmp_path):
    engine = _engine(tmp_path)
    consumer, directory = _consumer(tmp_path, engine)
    try:
        _write(directory, "bad.json", "{not json")
        assert consumer.process_once() == 0
        assert consumer.process_once() == 0
        assert (directory / "bad.json").exists()
        assert consumer.process_once() == 0
        assert not (directory / "bad.json").exists()
        assert len(list((directory / ".failed").glob("*.json"))) == 1
    finally:
        engine.close()


def test_unsupported_action_archived(tmp_path):
    engine = _engine(tmp_path)
    consumer, directory = _consumer(tmp_path, engine)
    try:
        _write(directory, "other.json", {"action": "open_document", "payload": {}})
        assert consumer.process_once() == 0
        _artifacts, total = engine.storage.list_all_artifacts()
        assert total == 0
        assert len(list((directory / ".processed").glob("*.json"))) == 1
    finally:
        engine.close()
