from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    start_line: int
    end_line: int
    heading_path: str | None = None

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.text) // 4)


def chunk_text(text: str, max_chars: int = 4000, overlap_chars: int = 400, heading_path: str | None = None) -> list[TextChunk]:
    if not text:
        return []
    lines = text.splitlines()
    line_offsets: list[tuple[int, int]] = []
    pos = 0
    for idx, line in enumerate(lines, start=1):
        line_offsets.append((pos, idx))
        pos += len(line) + 1
    chunks: list[TextChunk] = []
    start = 0
    index = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start + max_chars // 2:
                end = newline
        chunk = text[start:end].strip()
        if chunk:
            start_line = next((line for offset, line in reversed(line_offsets) if offset <= start), 1)
            end_line = next((line for offset, line in reversed(line_offsets) if offset <= end), len(lines) or 1)
            chunks.append(TextChunk(index=index, text=chunk, start_line=start_line, end_line=end_line, heading_path=heading_path))
            index += 1
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return chunks
