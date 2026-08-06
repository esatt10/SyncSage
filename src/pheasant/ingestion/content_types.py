from __future__ import annotations

from pathlib import Path

TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".html",
    ".xml",
    ".css",
    ".js",
    ".ts",
}
# Formats whose text has to be *extracted* rather than decoded — see
# pheasant.ingestion.extractor (PDF/DOCX/HTML),
# pheasant.ingestion.office (PPTX/XLSX/EPUB/RTF) and
# pheasant.ingestion.msdoc (legacy binary DOC).
#
# Membership here means two things: `artifact_type` labels the file
# "document", and `parse_file`/`parse_connector_payload` will accept it. It
# does NOT mean every source indexes them — a source only sees these files if
# its own `include` globs admit the extension (the default list is
# code/markdown/config only), and `extractor_from_config` only builds an
# extractor when they do.
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".rtf", ".epub"}
# Synapse 25.4 (session A): images are ingested by captioning them into text
# (see pheasant.ingestion.captioner). The caption flows through the normal
# chunk -> embed -> graph path; the artifact type is "image".
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Synapse 25.4 (session B): audio is ingested by transcribing it into text
# (see pheasant.ingestion.transcriber). The transcript flows through the normal
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
