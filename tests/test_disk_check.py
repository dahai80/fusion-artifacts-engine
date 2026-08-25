import asyncio
import time

import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.errors import ResourceLimitError
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage


def test_disk_check_disabled_by_default(tmp_path):
    st = SQLiteStorage(
        db_path=tmp_path / "meta.db",
        content_dir=tmp_path / "content",
        disk_space_warning_pct=0,
    )
    assert st.check_disk_space() is None
    st.ensure_disk_available()  # should not raise
    st.close()


def test_disk_check_returns_usage(tmp_path):
    st = SQLiteStorage(
        db_path=tmp_path / "meta.db",
        content_dir=tmp_path / "content",
        disk_space_warning_pct=90,
    )
    info = st.check_disk_space()
    assert info is not None
    used_pct, free, total = info
    assert 0 <= used_pct <= 100
    assert free >= 0
    assert total > 0
    st.close()


def test_disk_check_rejects_write_at_threshold(tmp_path):
    # 设阈值 1%——本机磁盘几乎必然超 1%，触发拒绝写
    st = SQLiteStorage(
        db_path=tmp_path / "meta.db",
        content_dir=tmp_path / "content",
        disk_space_warning_pct=1,
    )
    from fusion_artifacts_engine.models import Artifact
    art = Artifact(
        id="art_disk", session_id="s", name="n", type="code", kind="code",
        current_version=1, summary="", created_at=time.time(), updated_at=time.time(),
    )
    with pytest.raises(ResourceLimitError):
        st.save_artifact(art)
    st.close()


def test_disk_check_config_passed_to_storage(tmp_path):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=0,
    )
    config.disk_space_warning_pct = 50
    eng = ArtifactEngine(config)
    try:
        assert eng.storage.disk_space_warning_pct == 50
    finally:
        eng.close()


def test_engine_publishes_disk_alarm_on_full(tmp_path):
    # 设阈值 1% 触发磁盘预检拒绝，engine 应发 disk_full_alarm 事件
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts",
        server_host="127.0.0.1",
        server_port=0,
    )
    config.disk_space_warning_pct = 1
    eng = ArtifactEngine(config)
    sub = eng.event_bus.subscribe()
    try:
        with pytest.raises(ResourceLimitError):
            asyncio.run(
                eng.create_artifact(
                    session_id="disk", name="full", artifact_type="code", content="x",
                )
            )
        # 应收到 disk_full_alarm 事件
        event = sub.get(timeout=1)
        assert event["event_type"] == "disk_full_alarm"
        assert event["kind"] == "system.alarm"
    finally:
        eng.close()
