from __future__ import annotations

from pathlib import Path

TEXT_EXTENSIONS = {".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".html", ".xml", ".css", ".js", ".ts"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx"}
# Synapse 25.4 (session A): images are ingested by captioning them into text
# (see syncsage.ingestion.captioner). The caption flows through the normal
# chunk -> embed -> graph path; the artifact type is "image".
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Synapse 25.4 (session B): audio is ingested by transcribing it into text
# (see syncsage.ingestion.transcriber). The transcript flows through the normal
# chunk -> embed -> graph path; the artifact type is "audio".
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}


def artifact_type(path: Path) -> str:
    if path.suffix.lower() == ".md":
        return "markdown_note"
    if path.suffix.lower() in DOCUMENT_EXTENSIONS:
        return "document"
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return "image"
    if path.suffix.lower() in AUDIO_EXTENSIONS:
        return "audio"
    return "file"
