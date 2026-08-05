from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pheasant.config.schema import PheasantConfig
from pheasant.obsidian.frontmatter import render_frontmatter
from pheasant.obsidian.note_templates import get_profile
from pheasant.persistence.state_store import StateStore


def safe_note_name(name: str) -> str:
    return name.replace("/", "__").replace("\\", "__").replace(":", "-")


@dataclass(frozen=True)
class PlannedNote:
    path: Path
    body: str


class ObsidianExporter:
    def __init__(self, config: PheasantConfig, state: StateStore):
        self.config = config
        self.state = state
        self.root = config.pheasant.vault_path / config.obsidian.note_root

    def export(
        self,
        source_name: str | None = None,
        preview: bool = False,
        template_profile: str | None = None,
    ) -> dict:
        profile = get_profile(template_profile or self.config.obsidian.template_profile)
        notes = self._plan_notes(source_name, profile.name)
        changed = [
            str(note.path)
            for note in notes
            if not note.path.exists() or note.path.read_text(encoding="utf-8") != note.body
        ]
        unchanged = [str(note.path) for note in notes if str(note.path) not in set(changed)]
        if not preview:
            for note in notes:
                note.path.parent.mkdir(parents=True, exist_ok=True)
                note.path.write_text(note.body, encoding="utf-8")
        return {
            "preview": preview,
            "template_profile": profile.name,
            "planned_count": len(notes),
            "changed_count": len(changed),
            "changed_files": changed,
            "unchanged_files": unchanged,
            "written": [] if preview else [str(note.path) for note in notes],
            "count": 0 if preview else len(notes),
        }

    def _plan_notes(self, source_name: str | None, profile_name: str) -> list[PlannedNote]:
        profile = get_profile(profile_name)
        notes: list[PlannedNote] = []
        sources = self.state.rows(
            "SELECT * FROM sources WHERE (? IS NULL OR name=?) ORDER BY name",
            (source_name, source_name),
        )
        if self.config.obsidian.create_index_notes:
            notes.append(self._index_note(sources))
        if self.config.obsidian.create_source_notes:
            for source in sources:
                notes.append(self._source_note(source, profile_name))
        if self.config.obsidian.create_file_notes:
            for source in sources:
                artifacts = self._artifacts(source["name"], limit=500)
                notes.extend(self._concept_notes(source["name"], profile_name))
                for artifact in artifacts:
                    notes.append(self._file_note(source, artifact, profile_name))
                    if self.config.obsidian.create_chunk_notes:
                        notes.extend(self._chunk_notes(source, artifact, profile.chunk_heading))
        return notes

    def _index_note(self, sources: list) -> PlannedNote:
        path = self.root / "Index.md"
        body = render_frontmatter(
            {
                "pheasant": True,
                "knowledge_base": self.config.knowledge_base_id,
                "tags": ["pheasant"],
            }
        )
        body += f"# {self.config.knowledge_base_id}\n\n## Sources\n"
        body += "\n".join(
            f"- [[Sources/{safe_note_name(row['name'])}|{row['name']}]] "
            f"({row['type']}, {row['last_status'] or 'registered'})"
            for row in sources
        )
        return PlannedNote(path, body + "\n")

    def _source_note(self, source, profile_name: str) -> PlannedNote:
        profile = get_profile(profile_name)
        artifacts = self._artifacts(source["name"], limit=500)
        concepts = self._source_concepts(source["name"], limit=100)
        source_path = self.root / "Sources" / f"{safe_note_name(source['name'])}.md"
        body = render_frontmatter(
            {
                "pheasant": True,
                "source_id": source["name"],
                "source_type": source["type"],
                "template_profile": profile.name,
                "tags": ["pheasant", f"pheasant/{source['type']}"],
            }
        )
        body += f"# {source['name']}\n\nPath: `{source['path']}`\n\n"
        body += f"## {profile.source_heading}\n"
        body += "\n".join(
            f"- [[../Files/{safe_note_name(a['relative_path'])}|{a['relative_path']}]]"
            for a in artifacts
        )
        if concepts:
            body += f"\n\n## {profile.concept_heading}\n"
            body += "\n".join(
                f"- [[../Concepts/{safe_note_name(c['normalized_term'])}|{c['term']}]]"
                for c in concepts
            )
        return PlannedNote(source_path, body + "\n")

    def _concept_notes(self, source_name: str, profile_name: str) -> list[PlannedNote]:
        profile = get_profile(profile_name)
        notes = []
        for concept in self._source_concepts(source_name, limit=250):
            artifacts = self._artifacts_for_term(source_name, concept["normalized_term"])
            path = self.root / "Concepts" / f"{safe_note_name(concept['normalized_term'])}.md"
            body = render_frontmatter(
                {
                    "pheasant": True,
                    "source_id": source_name,
                    "concept": concept["normalized_term"],
                    "node_id": concept["node_id"],
                    "template_profile": profile.name,
                    "tags": ["pheasant", "pheasant/concept"],
                }
            )
            body += f"# {concept['term']}\n\n## {profile.concept_heading}\n"
            body += "\n".join(
                f"- [[../Files/{safe_note_name(a['relative_path'])}|{a['relative_path']}]]"
                for a in artifacts
            )
            notes.append(PlannedNote(path, body + "\n"))
        return notes

    def _file_note(self, source, artifact, profile_name: str) -> PlannedNote:
        profile = get_profile(profile_name)
        file_path = self.root / "Files" / f"{safe_note_name(artifact['relative_path'])}.md"
        chunks = self.state.rows(
            """SELECT id,summary,start_line,end_line,chunk_index
               FROM chunks WHERE artifact_id=?
               ORDER BY chunk_index LIMIT 20""",
            (artifact["id"],),
        )
        concepts = self._artifact_terms(artifact["id"], "concept", limit=20)
        related = self._related_artifacts(artifact["id"], source["name"], limit=8)
        body = render_frontmatter(
            {
                "pheasant": True,
                "node_id": artifact["id"],
                "source_id": source["name"],
                "relative_path": artifact["relative_path"],
                "content_hash": f"sha256:{artifact['sha256']}",
                "template_profile": profile.name,
                "tags": ["pheasant", "pheasant/file"],
            }
        )
        body += (
            f"# {artifact['relative_path']}\n\n"
            f"Source: [[../Sources/{safe_note_name(source['name'])}|{source['name']}]]\n\n"
            f"## {profile.file_heading}\n"
        )
        if concepts:
            body += "\n### Concepts\n"
            body += "\n".join(
                f"- [[../Concepts/{safe_note_name(c['normalized_term'])}|{c['term']}]]"
                for c in concepts
            )
        if related:
            body += f"\n\n## {profile.related_heading}\n"
            body += "\n".join(
                f"- [[{safe_note_name(row['relative_path'])}|{row['relative_path']}]]"
                f" via {row['matched_terms']}"
                for row in related
            )
        body += f"\n\n## {profile.chunk_heading}\n"
        for chunk in chunks:
            if self.config.obsidian.create_chunk_notes:
                body += (
                    f"- [[../Chunks/{safe_note_name(chunk['id'])}|"
                    f"Lines {chunk['start_line']}-{chunk['end_line']}]]: "
                    f"{chunk['summary']}\n"
                )
            else:
                body += f"- Lines {chunk['start_line']}-{chunk['end_line']}: {chunk['summary']}\n"
        return PlannedNote(file_path, body)

    def _chunk_notes(self, source, artifact, heading: str) -> list[PlannedNote]:
        notes = []
        rows = self.state.rows(
            """SELECT id,text,summary,start_line,end_line,chunk_index
               FROM chunks WHERE artifact_id=? ORDER BY chunk_index""",
            (artifact["id"],),
        )
        for row in rows:
            path = self.root / "Chunks" / f"{safe_note_name(row['id'])}.md"
            body = render_frontmatter(
                {
                    "pheasant": True,
                    "node_id": row["id"],
                    "artifact_id": artifact["id"],
                    "source_id": source["name"],
                    "relative_path": artifact["relative_path"],
                    "tags": ["pheasant", "pheasant/chunk"],
                }
            )
            body += (
                f"# {artifact['relative_path']}#{row['chunk_index']}\n\n"
                f"File: [[../Files/{safe_note_name(artifact['relative_path'])}|"
                f"{artifact['relative_path']}]]\n\n"
                f"## {heading}\n\n"
                f"Lines {row['start_line']}-{row['end_line']}\n\n"
                f"{row['text']}\n"
            )
            notes.append(PlannedNote(path, body))
        return notes

    def _artifacts(self, source_name: str, limit: int) -> list:
        return self.state.rows(
            "SELECT * FROM artifacts WHERE source_id=? ORDER BY relative_path LIMIT ?",
            (source_name, limit),
        )

    def _source_concepts(self, source_name: str, limit: int) -> list:
        return self.state.rows(
            """SELECT normalized_term, MIN(term) AS term, MIN(node_id) AS node_id,
                      COUNT(DISTINCT artifact_id) AS artifact_count
               FROM artifact_terms
               WHERE source_id=? AND node_type='concept'
               GROUP BY normalized_term
               ORDER BY artifact_count DESC, normalized_term
               LIMIT ?""",
            (source_name, limit),
        )

    def _artifact_terms(self, artifact_id: str, node_type: str, limit: int) -> list:
        return self.state.rows(
            """SELECT DISTINCT normalized_term, term, node_id
               FROM artifact_terms
               WHERE artifact_id=? AND node_type=?
               ORDER BY weight DESC, normalized_term
               LIMIT ?""",
            (artifact_id, node_type, limit),
        )

    def _artifacts_for_term(self, source_name: str, normalized_term: str) -> list:
        return self.state.rows(
            """SELECT DISTINCT artifacts.relative_path, artifacts.id
               FROM artifact_terms
               JOIN artifacts ON artifacts.id=artifact_terms.artifact_id
               WHERE artifact_terms.source_id=? AND artifact_terms.normalized_term=?
               ORDER BY artifacts.relative_path""",
            (source_name, normalized_term),
        )

    def _related_artifacts(self, artifact_id: str, source_name: str, limit: int) -> list:
        return self.state.rows(
            """SELECT artifacts.relative_path,
                      GROUP_CONCAT(DISTINCT related.term) AS matched_terms,
                      COUNT(DISTINCT related.normalized_term) AS shared_terms
               FROM artifact_terms AS base
               JOIN artifact_terms AS related
                    ON related.normalized_term=base.normalized_term
                   AND related.artifact_id<>base.artifact_id
               JOIN artifacts ON artifacts.id=related.artifact_id
               WHERE base.artifact_id=?
                 AND related.source_id=?
                 AND related.node_type='concept'
               GROUP BY related.artifact_id
               ORDER BY shared_terms DESC, artifacts.relative_path
               LIMIT ?""",
            (artifact_id, source_name, limit),
        )
