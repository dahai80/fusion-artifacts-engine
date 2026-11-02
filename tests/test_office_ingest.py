import json

import pytest

pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("pptx")

from docx import Document
from openpyxl import Workbook, load_workbook
from pptx import Presentation

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.ipc_consumer import OfficeIPCConsumer
from fusion_artifacts_engine.office_ingest import (
    markdown_to_docx,
    markdown_to_xlsx,
    maybe_write_back,
    parse_office,
)


def _make_docx(path, heading_style="Heading 1"):
    doc = Document()
    doc.add_heading("Report", level=0 if heading_style == "Title" else 1)
    doc.add_paragraph("intro paragraph")
    doc.add_heading("Findings", level=2)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "metric"
    table.cell(0, 1).text = "value"
    table.cell(1, 0).text = "mrr"
    table.cell(1, 1).text = "42"
    doc.add_paragraph("closing note")
    doc.save(str(path))


def _make_xlsx(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["region", "q1"])
    ws.append(["east", 10])
    ws.append(["west", 20])
    wb.create_sheet("Empty")
    wb.save(str(path))


def _make_pptx(path):
    prs = Presentation()
    layout = prs.slide_layouts[1]
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = "Title A"
    slide.placeholders[1].text = "bullet one"
    prs.slides.add_slide(layout)
    prs.save(str(path))


def test_parse_docx_sections(tmp_path):
    p = tmp_path / "a.docx"
    _make_docx(p)
    content, fmt, stats = parse_office(p)
    assert fmt == "docx"
    assert "## Report" in content
    assert "### Findings" in content
    assert "intro paragraph" in content
    assert "| metric | value |" in content
    assert "| mrr | 42 |" in content
    assert stats["headings"] >= 2


def test_parse_xlsx_sheets(tmp_path):
    p = tmp_path / "b.xlsx"
    _make_xlsx(p)
    content, fmt, _stats = parse_office(p)
    assert fmt == "xlsx"
    assert "## Sales" in content
    assert "| region | q1 |" in content
    assert "| east | 10 |" in content
    assert "## Empty" in content
    assert "(empty sheet)" in content


def test_parse_pptx_slides(tmp_path):
    p = tmp_path / "c.pptx"
    _make_pptx(p)
    content, fmt, _stats = parse_office(p)
    assert fmt == "pptx"
    assert "## Slide 1" in content
    assert "Title A" in content
    assert "## Slide 2" in content


def test_parse_unsupported_raises(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_office(p)


def _office_message(path, session="off-1", version=1, file=""):
    return {
        "target": {"Custom": "fusion-artifacts-engine"},
        "action": "save_artifact",
        "payload": {
            "source": "fusion-office",
            "title": "Q3 Report",
            "session": session,
            "version": version,
            "file_path": file or str(path),
        },
    }


def _consumer(tmp_path, engine):
    directory = tmp_path / "office-ipc"
    directory.mkdir(parents=True, exist_ok=True)
    return OfficeIPCConsumer(engine, directory, poll_interval=0.5), directory


def test_office_consumer_creates_markdown_artifact(tmp_path):
    src = tmp_path / "report.docx"
    _make_docx(src)
    engine = ArtifactEngine(
        ArtifactEngineConfig(storage_root=tmp_path / "artifacts", ipc_enabled=False)
    )
    consumer, directory = _consumer(tmp_path, engine)
    (directory / "m1.json").write_text(json.dumps(_office_message(src)), encoding="utf-8")
    try:
        assert consumer.process_once() == 1
        artifacts, total = engine.storage.list_all_artifacts()
        assert total == 1
        art = artifacts[0]
        assert art.source_module == "fusion-office"
        assert art.type == "markdown"
        assert art.metadata["office_format"] == "docx"
        assert art.metadata["office_session"] == "off-1"
        content = engine.storage.get_version(art.id, 1).content
        assert "## Report" in content
        assert not list(directory.glob("*.json"))
        assert len(list((directory / ".processed").glob("*.json"))) == 1
    finally:
        engine.close()


def test_office_consumer_version_bump_and_skip(tmp_path):
    src = tmp_path / "report.docx"
    _make_docx(src)
    src2 = tmp_path / "report2.docx"
    _make_docx(src2)
    engine = ArtifactEngine(
        ArtifactEngineConfig(storage_root=tmp_path / "artifacts", ipc_enabled=False)
    )
    consumer, directory = _consumer(tmp_path, engine)
    try:
        (directory / "a.json").write_text(json.dumps(_office_message(src)), encoding="utf-8")
        assert consumer.process_once() == 1
        (directory / "dup.json").write_text(
            json.dumps(_office_message(src, version=1)), encoding="utf-8"
        )
        assert consumer.process_once() == 0
        (directory / "b.json").write_text(
            json.dumps(_office_message(src2, version=2)), encoding="utf-8"
        )
        assert consumer.process_once() == 1
        artifacts, _total = engine.storage.list_all_artifacts()
        assert len(artifacts) == 1
        assert artifacts[0].metadata["office_version"] == 2
        assert len(engine.storage.list_versions(artifacts[0].id)) == 2
    finally:
        engine.close()


def test_office_consumer_missing_file_fails(tmp_path):
    engine = ArtifactEngine(
        ArtifactEngineConfig(storage_root=tmp_path / "artifacts", ipc_enabled=False)
    )
    consumer, directory = _consumer(tmp_path, engine)
    try:
        (directory / "x.json").write_text(
            json.dumps(_office_message(tmp_path / "nope.docx")), encoding="utf-8"
        )
        assert consumer.process_once() == 0
        assert len(list((directory / ".failed").glob("*.json"))) == 1
        _artifacts, total = engine.storage.list_all_artifacts()
        assert total == 0
    finally:
        engine.close()


def test_markdown_to_docx_roundtrip(tmp_path):
    md = "# Report\n\nsome text\n\n## Findings\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    out = tmp_path / "out.docx"
    out.write_bytes(markdown_to_docx(md))
    doc = Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    assert "Report" in texts
    assert "some text" in texts
    assert "Findings" in texts
    headings = [p.text for p in doc.paragraphs if (p.style.name or "").startswith("Heading")]
    assert set(headings) == {"Report", "Findings"}
    assert len(doc.tables) == 1
    assert doc.tables[0].cell(1, 1).text == "2"


def test_markdown_to_xlsx_roundtrip(tmp_path):
    md = "## Sales\n\n| region | q1 |\n|---|---|\n| east | 10 |\n\n## Notes\n\nplain note\n"
    out = tmp_path / "out.xlsx"
    out.write_bytes(markdown_to_xlsx(md))
    wb = load_workbook(str(out))
    assert wb.sheetnames == ["Sales", "Notes"]
    ws = wb["Sales"]
    assert ws.cell(1, 1).value == "region"
    assert ws.cell(2, 2).value == 10
    assert wb["Notes"].cell(1, 1).value == "plain note"


async def test_patch_triggers_write_back(tmp_path):
    src = tmp_path / "report.docx"
    _make_docx(src)
    content, _fmt, _stats = parse_office(src)
    engine = ArtifactEngine(
        ArtifactEngineConfig(
            storage_root=tmp_path / "artifacts",
            ipc_enabled=False,
            ipc_office_dir=str(tmp_path / "office-ipc-out"),
        )
    )
    try:
        artifact, _v, _ref = await engine.create_external_artifact(
            "fusion-office",
            "wb-1",
            "Q3 Report",
            "markdown",
            content,
            metadata={"office_format": "docx", "office_session": "wb-1", "office_version": 1},
        )
        version, _info = await engine.patch_artifact(
            artifact_id=artifact.id,
            operation="replace_section",
            anchor="Findings",
            content="### Findings\n\npatched body",
        )
        out = maybe_write_back(
            engine, engine.storage.get_artifact(artifact.id), version.version_num
        )
        assert out is not None
        path = engine.config.ipc_office_outbox_path / f"{artifact.id}_v{version.version_num}.docx"
        assert path.is_file()
        doc = Document(str(path))
        assert any("patched body" in p.text for p in doc.paragraphs)
    finally:
        engine.close()


async def test_write_back_skips_non_office(tmp_path):
    engine = ArtifactEngine(
        ArtifactEngineConfig(
            storage_root=tmp_path / "artifacts",
            ipc_enabled=False,
            ipc_office_dir=str(tmp_path / "office-ipc-out"),
        )
    )
    try:
        artifact, _v, _ref = await engine.create_artifact("s-x", "Plain", "markdown", "# hi")
        assert maybe_write_back(engine, artifact, 1) is None
        assert not engine.config.ipc_office_outbox_path.exists()
    finally:
        engine.close()


def test_pptx_write_back_not_implemented():
    with pytest.raises(NotImplementedError):
        markdown_to_pptx("# s")


from fusion_artifacts_engine.office_ingest import markdown_to_pptx  # noqa: E402


def test_office_config_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_OFFICE_DATA_DIR", str(tmp_path / "off"))
    config = ArtifactEngineConfig(storage_root=tmp_path / "artifacts")
    assert config.ipc_office_path == tmp_path / "off" / "fusion-artifacts-engine"
    assert config.ipc_office_outbox_path == tmp_path / "off" / "fusion-artifacts-engine" / ".outbox"


def test_office_config_yaml_dir(tmp_path):
    config = ArtifactEngineConfig(
        storage_root=tmp_path / "artifacts", ipc_office_dir=str(tmp_path / "custom")
    )
    assert config.ipc_office_path == tmp_path / "custom"
