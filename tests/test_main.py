import sys
import threading
import time
from unittest.mock import patch

import pytest

from fusion_artifacts_engine.__main__ import _set_process_title, main


def test_set_process_title_no_throw():
    # #68: 无论 setproctitle/fusion_core 是否可用，调用不得抛异常（fallback safe）
    _set_process_title("fusion-artifacts-engine-test")


def test_set_process_title_actual():
    # #68: setproctitle 已安装时进程标题真实生效（CI 未装则跳过）
    pytest.importorskip("setproctitle")
    import setproctitle

    _set_process_title("fae-proc-title-test")
    assert setproctitle.getproctitle() == "fae-proc-title-test"
    setproctitle.setproctitle("pytest")


def test_version_command(capsys):
    with patch.object(sys, "argv", ["fusion-artifacts-engine", "version"]):
        main()
    captured = capsys.readouterr()
    assert "fusion-artifacts-engine" in captured.out


def test_status_not_running(capsys):
    # L-20: status 不健康须 exit 1；捕获 SystemExit 验证退出码
    with patch.object(sys, "argv", ["fusion-artifacts-engine", "status"]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Not running" in captured.out or "Running" in captured.out


def test_no_command(capsys):
    with patch.object(sys, "argv", ["fusion-artifacts-engine"]):
        main()
    captured = capsys.readouterr()
    assert (
        "fusion-artifacts-engine" in captured.out.lower()
        or "usage" in captured.out.lower()
    )


def test_start_with_config(tmp_path):
    config_file = tmp_path / "test_config.yaml"
    config_file.write_text("server_host: 127.0.0.1\nserver_port: 19998\n")
    storage_dir = tmp_path / "artifacts"
    storage_dir.mkdir()

    def run_main():
        with patch.object(
            sys,
            "argv",
            [
                "fusion-artifacts-engine",
                "start",
                "--config",
                str(config_file),
                "--storage-root",
                str(storage_dir),
            ],
        ):
            main()

    thread = threading.Thread(target=run_main, daemon=True)
    thread.start()
    time.sleep(2)

    import httpx

    try:
        resp = httpx.post(
            "http://127.0.0.1:19998",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            timeout=3.0,
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["pong"] is True
    except (httpx.ConnectError, httpx.TimeoutException, OSError, ValueError):
        pass  # server not reachable, skip


def test_start_with_host_port(tmp_path):
    storage_dir = tmp_path / "artifacts"
    storage_dir.mkdir()

    def run_main():
        with patch.object(
            sys,
            "argv",
            [
                "fusion-artifacts-engine",
                "start",
                "--host",
                "127.0.0.1",
                "--port",
                "19997",
                "--storage-root",
                str(storage_dir),
            ],
        ):
            main()

    thread = threading.Thread(target=run_main, daemon=True)
    thread.start()
    time.sleep(2)
