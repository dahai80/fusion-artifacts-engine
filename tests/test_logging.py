import json
import logging

from fusion_artifacts_engine.utils import JsonFormatter, setup_logging


def test_json_formatter_emits_valid_json():
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    line = fmt.format(record)
    obj = json.loads(line)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "test"
    assert obj["msg"] == "hello world"
    assert "ts" in obj


def test_json_formatter_includes_extra_fields():
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="warn", args=(), exc_info=None,
    )
    record.artifact_id = "art_abc"
    record.method = "artifact.create"
    line = fmt.format(record)
    obj = json.loads(line)
    assert obj["artifact_id"] == "art_abc"
    assert obj["method"] == "artifact.create"


def test_json_formatter_includes_exception():
    fmt = JsonFormatter()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="err", args=(), exc_info=sys.exc_info(),
        )
    line = fmt.format(record)
    obj = json.loads(line)
    assert "exc" in obj
    assert "RuntimeError: boom" in obj["exc"]


def test_setup_logging_writes_rotating_file(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("FUSION_ARTIFACTS_LOG_DIR", str(log_dir))
    setup_logging(logging.INFO)
    log = logging.getLogger("fae.test.log")
    log.info("rotating log test message")
    for h in logging.getLogger().handlers:
        h.flush()
    log_file = log_dir / "artifacts-engine.log"
    assert log_file.exists()
    content = log_file.read_text()
    assert "rotating log test message" in content
    # 应为 JSON 行
    line = content.strip().splitlines()[-1]
    obj = json.loads(line)
    assert obj["msg"] == "rotating log test message"
    # 清理 root handlers，避免污染后续测试
    for h in list(logging.getLogger().handlers):
        h.close()
        logging.getLogger().removeHandler(h)


def test_setup_logging_console_and_file_both_present(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_ARTIFACTS_LOG_DIR", str(tmp_path / "logs"))
    setup_logging(logging.DEBUG)
    root = logging.getLogger()
    handler_types = {type(h).__name__ for h in root.handlers}
    assert "StreamHandler" in handler_types
    assert "RotatingFileHandler" in handler_types
    for h in list(root.handlers):
        h.close()
        logging.getLogger().removeHandler(h)
