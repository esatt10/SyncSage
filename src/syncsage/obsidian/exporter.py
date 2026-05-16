from __future__ import annotations

from pathlib import Path

from syncsage.config.schema import SyncSageConfig
from syncsage.obsidian.frontmatter import render_frontmatter
from syncsage.persistence.state_store import StateStore


def safe_note_name(name: str) -> str:
    return name.replace("/", "__").replace("\\", "__").replace(":", "-")


class ObsidianExporter:
    def __init__(self, config: SyncSageConfig, state: StateStore):
        self.config = config
        self.state = state
        self.root = config.syncsage.vault_path / config.obsidian.note_root

    def export(self, source_name: str | None = None) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        if self.config.obsidian.create_index_notes:
            path = self.root / "Index.md"
            sources = self.state.rows("SELECT name,type,last_status,last_indexed_at FROM sources ORDER BY name")
            body = render_frontmatter({"syncsage": True, "knowledge_base": self.config.knowledge_base_id, "tags": ["syncsage"]})
            body += f"# {self.config.knowledge_base_id}\n\n## Sources\n"
            body += "\n".join(f"- [[Sources/{row['name']}|{row['name']}]] ({row['type']}, {row['last_status'] or 'registered'})" for row in sources)
            path.write_text(body + "\n", encoding="utf-8")
            written.append(str(path))
        written.extend(self._export_sources(source_name))
        return {"written": written, "count": len(written)}

    def _export_sources(self, source_name: str | None) -> list[str]:
        rows = self.state.rows("SELECT * FROM sources WHERE (? IS NULL OR name=?) ORDER BY name", (source_name, source_name))
        out: list[str] = []
        sources_dir = self.root / "Sources"
        files_dir = self.root / "Files"
        sources_dir.mkdir(parents=True, exist_ok=True)
        files_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            source_path = sources_dir / f"{safe_note_name(row['name'])}.md"
            artifacts = self.state.rows("SELECT * FROM artifacts WHERE source_id=? ORDER BY relative_path LIMIT 200", (row["name"],))
            text = render_frontmatter({"syncsage": True, "source_id": row["name"], "source_type": row["type"], "tags": ["syncsage", f"syncsage/{row['type']}"]})
            text += f"# {row['name']}\n\nPath: `{row['path']}`\n\n## Files\n"
            text += "\n".join(f"- [[../Files/{safe_note_name(a['relative_path'])}|{a['relative_path']}]]" for a in artifacts)
            source_path.write_text(text + "\n", encoding="utf-8")
            out.append(str(source_path))
            for artifact in artifacts:
                file_path = files_dir / f"{safe_note_name(artifact['relative_path'])}.md"
                chunks = self.state.rows("SELECT summary,start_line,end_line FROM chunks WHERE artifact_id=? ORDER BY chunk_index LIMIT 5", (artifact["id"],))
                body = render_frontmatter({"syncsage": True, "node_id": artifact["id"], "source_id": row["name"], "relative_path": artifact["relative_path"], "content_hash": f"sha256:{artifact['sha256']}", "tags": ["syncsage", "syncsage/file"]})
                body += f"# {artifact['relative_path']}\n\nSource: [[../Sources/{row['name']}|{row['name']}]]\n\n## Summary\n"
                body += "\n".join(f"- Lines {c['start_line']}-{c['end_line']}: {c['summary']}" for c in chunks)
                file_path.write_text(body + "\n", encoding="utf-8")
                out.append(str(file_path))
        return out
