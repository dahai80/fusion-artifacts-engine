import logging
from fusion_artifacts_engine.models import ArtifactType

logger = logging.getLogger(__name__)

_CODE_LANGS = {
    "python", "py", "javascript", "js", "typescript", "ts", "rust", "go",
    "java", "c", "cpp", "c++", "csharp", "cs", "ruby", "rb", "php",
    "swift", "kotlin", "shell", "bash", "sh", "sql", "yaml", "yml",
    "toml", "json", "xml", "html", "css", "scss", "dockerfile",
}

_DATA_EXTS = {".json", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".xml"}

_HTML_EXTS = {".html", ".htm"}

_REACT_EXTS = {".jsx", ".tsx"}


def should_create_artifact(
    content: str,
    content_type: str = "text",
    threshold_lines: int = 30,
    threshold_chars: int = 1500,
) -> bool:
    if content_type == "code":
        line_count = content.count("\n") + 1
        return line_count >= threshold_lines
    return len(content) >= threshold_chars


def detect_artifact_type(name: str, content: str = "") -> ArtifactType:
    name_lower = name.lower()
    for ext in _REACT_EXTS:
        if name_lower.endswith(ext):
            return "react"
    for ext in _HTML_EXTS:
        if name_lower.endswith(ext):
            return "html"
    for ext in _DATA_EXTS:
        if name_lower.endswith(ext):
            return "data"
    if name_lower.endswith((".md", ".markdown")):
        return "markdown"
    _, _, ext = name_lower.rpartition(".")
    if ext in _CODE_LANGS:
        return "code"
    if content.strip().startswith("<!DOCTYPE") or content.strip().startswith("<html"):
        return "html"
    if content.strip().startswith("# ") or content.strip().startswith("## "):
        return "markdown"
    return "code"


def extract_name_hint(content: str, lang_hint: str = "") -> str:
    lines = content.strip().split("\n")
    for line in lines[:5]:
        line = line.strip()
        if line.startswith("# filename:") or line.startswith("// filename:"):
            return line.split(":", 1)[1].strip()
        if line.startswith("# file:") or line.startswith("// file:"):
            return line.split(":", 1)[1].strip()
    ext_map = {
        "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts", "rust": ".rs", "go": ".go",
        "html": ".html", "css": ".css", "shell": ".sh", "bash": ".sh",
        "sql": ".sql", "markdown": ".md", "md": ".md",
    }
    ext = ext_map.get(lang_hint.lower(), ".txt")
    return f"artifact{ext}"
