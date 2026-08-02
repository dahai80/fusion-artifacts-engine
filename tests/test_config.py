import os
import tempfile
from pathlib import Path
import pytest
import yaml
from fusion_artifacts_engine.config import ArtifactEngineConfig, load_config, _flatten_yaml_config


def test_default_config():
    config = ArtifactEngineConfig()
    assert config.storage_root == Path.home() / ".fusion" / "artifacts"
    assert config.server_host == "127.0.0.1"
    assert config.server_port == 11451
    assert config.allow_no_auth is True
    assert config.artifact_id_prefix == "art_"


def test_extra_fields_forbidden():
    with pytest.raises(Exception):
        ArtifactEngineConfig(storage_root="/tmp/test", unknown_field="bad")


def test_flatten_yaml_config():
    data = {
        "server": {"host": "0.0.0.0", "port": 9000},
        "storage": {"root": "/tmp/artifacts", "db_name": "test.db", "small_content_limit": 2048},
        "mlx": {"url": "http://mlx:8890"},
        "thresholds": {"auto_create_lines": 50, "auto_create_chars": 3000},
        "artifact": {"id_prefix": "test_"},
    }
    flat = _flatten_yaml_config(data)
    assert flat["server_host"] == "0.0.0.0"
    assert flat["server_port"] == 9000
    assert flat["storage_root"] == Path("/tmp/artifacts")
    assert flat["db_name"] == "test.db"
    assert flat["artifact_id_prefix"] == "test_"


def test_flatten_yaml_tilde_expansion():
    data = {"storage": {"root": "~/test_artifacts"}}
    flat = _flatten_yaml_config(data)
    assert str(flat["storage_root"]).startswith(str(Path.home()))
    assert "~" not in str(flat["storage_root"])


def test_load_config_with_user_yaml():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump({"server": {"port": 7777}, "storage": {"root": "/tmp/cfg_test"}}, f)
        f.flush()
        config = load_config(user_config_path=Path(f.name))
    os.unlink(f.name)
    assert config.server_port == 7777
    assert config.storage_root == Path("/tmp/cfg_test")


def test_load_config_env_override(monkeypatch):
    monkeypatch.setenv("FUSION_ARTIFACTS_PORT", "9999")
    monkeypatch.setenv("FUSION_ARTIFACTS_HOST", "0.0.0.0")
    config = load_config()
    assert config.server_port == 9999
    assert config.server_host == "0.0.0.0"


def test_db_path_and_content_dir():
    config = ArtifactEngineConfig(storage_root=Path("/tmp/test_engine"))
    assert config.db_path == Path("/tmp/test_engine/meta.db")
    assert config.content_dir == Path("/tmp/test_engine/content")
