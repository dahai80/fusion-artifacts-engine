import os
import tempfile
from pathlib import Path

import pytest
import yaml

from fusion_artifacts_engine.config import (
    ArtifactEngineConfig,
    _flatten_yaml_config,
    load_config,
)


def test_default_config():
    config = ArtifactEngineConfig()
    assert config.storage_root == Path.home() / ".fusion" / "artifacts"
    assert config.server_host == "127.0.0.1"
    assert config.server_port == 11451
    assert config.allow_no_auth is False
    assert config.artifact_id_prefix == "art_"


def test_extra_fields_forbidden():
    with pytest.raises(ValueError):
        ArtifactEngineConfig(storage_root="/tmp/test", unknown_field="bad")


def test_flatten_yaml_config():
    data = {
        "server": {"host": "0.0.0.0", "port": 9000},
        "storage": {
            "root": "/tmp/artifacts",
            "db_name": "test.db",
            "small_content_limit": 2048,
        },
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


def test_env_data_dir_override(monkeypatch, tmp_path):
    # #69: FUSION_ARTIFACTS_DATA_DIR 契约变量，覆盖 yaml 默认 storage root
    monkeypatch.setenv("FUSION_ARTIFACTS_DATA_DIR", str(tmp_path / "data"))
    config = load_config()
    assert config.storage_root == tmp_path / "data"


def test_env_data_dir_tilde_expansion(monkeypatch):
    # #69: DATA_DIR 含 ~ 时展开（与 STORAGE_ROOT 行为一致）
    monkeypatch.setenv("FUSION_ARTIFACTS_DATA_DIR", "~/artifacts_data_6869")
    config = load_config()
    assert str(config.storage_root).startswith(str(Path.home()))
    assert "~" not in str(config.storage_root)


def test_env_storage_root_legacy_alias(monkeypatch, tmp_path):
    # #69: legacy FUSION_ARTIFACTS_STORAGE_ROOT 仍生效（迁移期向后兼容）
    monkeypatch.setenv("FUSION_ARTIFACTS_STORAGE_ROOT", str(tmp_path / "legacy"))
    config = load_config()
    assert config.storage_root == tmp_path / "legacy"


def test_env_data_dir_wins_over_storage_root(monkeypatch, tmp_path):
    # #69: 两个都设时 DATA_DIR（契约名）优先于 STORAGE_ROOT（legacy 别名）
    monkeypatch.setenv("FUSION_ARTIFACTS_STORAGE_ROOT", str(tmp_path / "legacy"))
    monkeypatch.setenv("FUSION_ARTIFACTS_DATA_DIR", str(tmp_path / "contract"))
    config = load_config()
    assert config.storage_root == tmp_path / "contract"


def test_invalid_port_env_keeps_yaml_value(monkeypatch):
    # #69: 非整数 PORT 走 fusion_core.service_port 的 ValueError 路径，
    # 告警并保留 yaml 默认值（fail-open 到默认而非崩溃）
    monkeypatch.setenv("FUSION_ARTIFACTS_PORT", "not-a-port")
    config = load_config()
    assert config.server_port == 11451


def test_db_path_and_content_dir():
    config = ArtifactEngineConfig(storage_root=Path("/tmp/test_engine"))
    assert config.db_path == Path("/tmp/test_engine/meta.db")
    assert config.content_dir == Path("/tmp/test_engine/content")
