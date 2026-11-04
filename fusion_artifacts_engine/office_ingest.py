import io
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# v0.7.0: fusion-office long-context ingest. Office files (docx/xlsx/pptx) are parsed into
# markdown with `#` headers — directly addressable by section_index (docx=heading levels,
# xlsx=worksheets, pptx=slides); patch/budget/references all go through the existing pipeline.
# The write-back direction regenerates patched markdown into the same-format binary (pptx
# write-back unsupported, explicit not_implemented) and exports to .outbox/ in the office IPC
# dir for fusion-office to pick up. The engine never overwrites the user's original file.

_MAX_SHEET_ROWS = 500
_MAX_SHEET_COLS = 64
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")


def _md_escape_cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def parse_office(path: Path) -> tuple[str, str, dict]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        content = _parse_docx(path)
    elif suffix == ".xlsx":
        content = _parse_xlsx(path)
    elif suffix == ".pptx":
        content = _parse_pptx(path)
    else:
        raise ValueError(f"unsupported office format: {suffix or '(none)'}")
    stats = {
        "format": suffix.lstrip("."),
        "chars": len(content),
        "headings": len(_HEADING_RE.findall(content)),
    }
    logger.info(
        "office parsed %s (%s, %d chars, %d headings)", path.name, suffix, len(content), stats["headings"]
    )
    return content, suffix.lstrip("."), stats


def _parse_docx(path: Path) -> str:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as e:
        raise RuntimeError("python-docx not installed") from e
    document = Document(str(path))
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, document)
            text = para.text.strip()
            if not text:
                lines.append("")
                continue
            style = ""
            try:
                style = para.style.name or ""
            except Exception:  # noqa: BLE001
                style = ""
            m = re.match(r"^Heading (\d)$", style)
            if m:
                level = min(6, int(m.group(1)) + 1)
                lines.append(f"{'#' * level} {text}")
            else:
                lines.append(text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            for row in table.rows:
                cells = [_md_escape_cell(c.text) for c in row.cells]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _parse_xlsx(path: Path) -> str:
    try:
        import openpyxl
    except ImportError as e:
        raise RuntimeError("openpyxl not installed") from e
    workbook = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    lines: list[str] = []
    for sheet in workbook.worksheets:
        lines.append(f"## {sheet.title}")
        lines.append("")
        rows_written = 0
        for row in sheet.iter_rows(max_row=_MAX_SHEET_ROWS, max_col=_MAX_SHEET_COLS, values_only=True):
            cells = [_md_escape_cell(v) for v in row]
            lines.append("| " + " | ".join(cells) + " |")
            if rows_written == 0:
                lines.append("|" + "---|" * len(cells))
            rows_written += 1
        if rows_written == 0:
            lines.append("(empty sheet)")
        elif rows_written >= _MAX_SHEET_ROWS:
            lines.append(f"(truncated at {_MAX_SHEET_ROWS} rows)")
        lines.append("")
    workbook.close()
    return "\n".join(lines).strip() + "\n"


def _parse_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError as e:
        raise RuntimeError("python-pptx not installed") from e
    presentation = Presentation(str(path))
    lines: list[str] = []
    for idx, slide in enumerate(presentation.slides, start=1):
        lines.append(f"## Slide {idx}")
        lines.append("")
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for para in shape.text_frame.paragraphs:
                text = "".join(run.text for run in para.runs).strip()
                if text:
                    lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _blocks(content: str) -> list[dict]:
    blocks: list[dict] = []
    paragraph: list[str] = []
    table: list[list[str]] = []

    def flush_paragraph():
        if paragraph:
            blocks.append({"kind": "para", "text": " ".join(paragraph)})
            paragraph.clear()

    def flush_table():
        if table:
            blocks.append({"kind": "table", "rows": [r[:] for r in table]})
            table.clear()

    for line in content.split("\n"):
        stripped = line.strip()
        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            flush_table()
            blocks.append({"kind": "heading", "level": len(heading.group(1)), "text": heading.group(2)})
        elif _TABLE_ROW_RE.match(stripped) and "---" not in stripped:
            flush_paragraph()
            cells = [c.replace("\\|", "|").strip() for c in stripped.strip("|").split("|")]
            table.append(cells)
        elif _TABLE_ROW_RE.match(stripped) and "---" in stripped:
            flush_paragraph()
        elif stripped:
            flush_table()
            paragraph.append(stripped)
        else:
            flush_paragraph()
            flush_table()
    flush_paragraph()
    flush_table()
    return blocks


def _safe_sheet_name(name: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", name).strip() or "Sheet"
    cleaned = cleaned[:31]
    base = cleaned
    n = 2
    while cleaned.lower() in used:
        suffix = f"~{n}"
        cleaned = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(cleaned.lower())
    return cleaned


def markdown_to_docx(content: str) -> bytes:
    try:
        from docx import Document
    except ImportError as e:
        raise RuntimeError("python-docx not installed") from e
    document = Document()
    for block in _blocks(content):
        if block["kind"] == "heading":
            document.add_heading(block["text"], level=max(1, block["level"] - 1))
        elif block["kind"] == "para":
            document.add_paragraph(block["text"])
        else:
            rows = block["rows"]
            cols = max(len(r) for r in rows)
            table = document.add_table(rows=len(rows), cols=cols)
            for r, row in enumerate(rows):
                for c in range(cols):
                    table.cell(r, c).text = row[c] if c < len(row) else ""
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _coerce_cell(text: str):
    # markdown table cells are strings; coerce back to numbers on xlsx write-back to keep types round-trippable
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return text


def markdown_to_xlsx(content: str) -> bytes:
    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise RuntimeError("openpyxl not installed") from e
    workbook = Workbook()
    workbook.remove(workbook.active)
    used: set[str] = set()
    current = None
    for block in _blocks(content):
        if block["kind"] == "heading":
            current = workbook.create_sheet(_safe_sheet_name(block["text"], used))
        elif current is None:
            current = workbook.create_sheet(_safe_sheet_name("Sheet1", used))
        elif block["kind"] == "para":
            current.append([block["text"]])
        else:
            for row in block["rows"]:
                current.append([_coerce_cell(c) for c in row])
    if not workbook.worksheets:
        workbook.create_sheet("Sheet1")
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


def markdown_to_pptx(content: str) -> bytes:
    raise NotImplementedError("pptx write-back is not supported; export docx/xlsx only")


def maybe_write_back(engine, artifact, version_num: int) -> str | None:
    # After patch/update, regenerate the office file per metadata.office_format into
    # .outbox/. Failures only log and do not affect the patch result itself.
    meta = (artifact.metadata or {}) if artifact is not None else {}
    fmt = meta.get("office_format")
    if fmt not in ("docx", "xlsx"):
        return None
    version = engine.storage.get_version(artifact.id, version_num)
    if version is None:
        logger.warning("office write-back: version %s v%s not found", artifact.id, version_num)
        return None
    try:
        if fmt == "docx":
            data = markdown_to_docx(version.content)
        else:
            data = markdown_to_xlsx(version.content)
    except Exception:  # noqa: BLE001
        logger.exception("office write-back failed for %s v%d", artifact.id, version_num)
        return None
    outbox = engine.config.ipc_office_outbox_path
    try:
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"{artifact.id}_v{version_num}.{fmt}"
        path.write_bytes(data)
    except OSError as e:
        logger.error("office write-back export failed %s v%d: %s", artifact.id, version_num, e)
        return None
    logger.info("office write-back exported %s", path)
    return str(path)
