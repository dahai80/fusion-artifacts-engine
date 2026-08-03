from fusion_artifacts_engine.auto_identifier import (
    detect_artifact_type,
    extract_name_hint,
    should_create_artifact,
)


def test_should_create_code():
    assert should_create_artifact("line\n" * 30, "code", threshold_lines=30) is True
    assert should_create_artifact("line\n" * 28, "code", threshold_lines=30) is False


def test_should_create_text():
    long_text = "x" * 1500
    assert should_create_artifact(long_text, "text", threshold_chars=1500) is True
    assert should_create_artifact("short", "text", threshold_chars=1500) is False


def test_detect_code():
    assert detect_artifact_type("main.py") == "code"
    assert detect_artifact_type("app.js") == "code"


def test_detect_react():
    assert detect_artifact_type("component.jsx") == "react"
    assert detect_artifact_type("app.tsx") == "react"


def test_detect_html():
    assert detect_artifact_type("page.html") == "html"
    assert detect_artifact_type("page.htm") == "html"


def test_detect_markdown():
    assert detect_artifact_type("readme.md") == "markdown"


def test_detect_data():
    assert detect_artifact_type("data.json") == "data"
    assert detect_artifact_type("data.csv") == "data"


def test_detect_from_content():
    assert detect_artifact_type("unknown", "<!DOCTYPE html><html>") == "html"
    assert detect_artifact_type("unknown", "# Heading\nContent") == "markdown"


def test_extract_name_hint():
    assert extract_name_hint("# filename: test.py") == "test.py"
    assert extract_name_hint("regular code", "python") == "artifact.py"
    assert extract_name_hint("regular code", "javascript") == "artifact.js"
