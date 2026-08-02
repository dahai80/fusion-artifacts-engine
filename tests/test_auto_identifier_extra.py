from fusion_artifacts_engine.auto_identifier import (
    should_create_artifact,
    detect_artifact_type,
    detect_renderable_type,
    extract_name_hint,
)


def test_detect_artifact_type_react():
    assert detect_artifact_type("App.jsx") == "react"
    assert detect_artifact_type("Widget.tsx") == "react"


def test_detect_artifact_type_html():
    assert detect_artifact_type("page.html") == "html"
    assert detect_artifact_type("old.htm") == "html"


def test_detect_artifact_type_data():
    assert detect_artifact_type("data.json") == "data"
    assert detect_artifact_type("config.yaml") == "data"
    assert detect_artifact_type("table.csv") == "data"


def test_detect_artifact_type_markdown():
    assert detect_artifact_type("README.md") == "markdown"
    assert detect_artifact_type("doc.markdown") == "markdown"


def test_detect_artifact_type_code_by_ext():
    assert detect_artifact_type("main.py") == "code"
    assert detect_artifact_type("app.js") == "code"
    assert detect_artifact_type("main.rs") == "code"


def test_detect_artifact_type_by_content_html():
    assert detect_artifact_type("unknown", "<!DOCTYPE html><html></html>") == "html"
    assert detect_artifact_type("unknown", "<html><body></body></html>") == "html"


def test_detect_artifact_type_by_content_markdown():
    assert detect_artifact_type("unknown", "# Heading") == "markdown"
    assert detect_artifact_type("unknown", "## Sub heading") == "markdown"


def test_detect_artifact_type_default_code():
    assert detect_artifact_type("unknown") == "code"


def test_detect_renderable_type_svg_content():
    assert detect_renderable_type('<svg xmlns="http://www.w3.org/2000/svg"></svg>') == "svg"


def test_detect_renderable_type_mermaid_content():
    assert detect_renderable_type("graph TD\n  A --> B") == "mermaid"
    assert detect_renderable_type("sequenceDiagram\n  A->>B: hello") == "mermaid"


def test_detect_renderable_type_html_content():
    assert detect_renderable_type("<!DOCTYPE html><html></html>") == "html"


def test_detect_renderable_type_react_content():
    assert detect_renderable_type("import React from 'react'; export default function App() {}") == "react"


def test_detect_renderable_type_by_name_svg():
    assert detect_renderable_type("", "icon.svg") == "svg"


def test_detect_renderable_type_by_name_mermaid():
    assert detect_renderable_type("", "diagram.mermaid") == "mermaid"
    assert detect_renderable_type("", "flow.mmd") == "mermaid"


def test_detect_renderable_type_by_name_react():
    assert detect_renderable_type("", "App.jsx") == "react"
    assert detect_renderable_type("", "Widget.tsx") == "react"


def test_detect_renderable_type_by_name_html():
    assert detect_renderable_type("", "page.html") == "html"


def test_detect_renderable_type_none():
    assert detect_renderable_type("just some text", "file.txt") is None


def test_extract_name_hint_filename_directive():
    assert extract_name_hint("# filename: my_app.py") == "my_app.py"
    assert extract_name_hint("// filename: util.js") == "util.js"
    assert extract_name_hint("# file: config.yaml") == "config.yaml"
    assert extract_name_hint("// file: helper.ts") == "helper.ts"


def test_extract_name_hint_by_lang():
    assert extract_name_hint("x = 1", "python") == "artifact.py"
    assert extract_name_hint("const x = 1", "javascript") == "artifact.js"
    assert extract_name_hint("let x: number", "typescript") == "artifact.ts"


def test_extract_name_hint_default():
    assert extract_name_hint("some content") == "artifact.txt"
