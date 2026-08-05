from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateProfile:
    name: str
    source_heading: str
    concept_heading: str
    file_heading: str
    related_heading: str
    chunk_heading: str


PROFILES = {
    "engineering": TemplateProfile(
        name="engineering",
        source_heading="Implementation Surface",
        concept_heading="Concept Index",
        file_heading="Implementation Notes",
        related_heading="Related Implementation Context",
        chunk_heading="Indexed Chunks",
    ),
    "research": TemplateProfile(
        name="research",
        source_heading="Corpus",
        concept_heading="Research Threads",
        file_heading="Evidence Notes",
        related_heading="Related Evidence",
        chunk_heading="Extracts",
    ),
    "project-ops": TemplateProfile(
        name="project-ops",
        source_heading="Workstream",
        concept_heading="Operating Themes",
        file_heading="Operational Notes",
        related_heading="Adjacent Work",
        chunk_heading="Referenced Sections",
    ),
}


def get_profile(name: str | None) -> TemplateProfile:
    return PROFILES.get(name or "engineering", PROFILES["engineering"])
