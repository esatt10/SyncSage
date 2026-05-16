from __future__ import annotations

from pathlib import Path

TEXT_EXTENSIONS = {".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".html", ".xml", ".css", ".js", ".ts"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx"}


def artifact_type(path: Path) -> str:
    if path.suffix.lower() == ".md":
        return "markdown_note"
    if path.suffix.lower() in DOCUMENT_EXTENSIONS:
        return "document"
    return "file"
