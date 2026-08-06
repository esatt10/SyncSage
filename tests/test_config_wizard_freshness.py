"""PR gate: agent/config_wizard_prompt.md and docs/configuration.md must
mention every configurable section of ``PheasantConfig``.

This is the mechanical half of the freshness contract described in
``agent/config_wizard_prompt.md`` and ``.claude/skills/config-surface-sync``:
a human (or an agent) can forget to update prose docs when they add a field
to ``src/pheasant/config/schema.py``, but they cannot make this test pass
without doing so. It runs as part of the ordinary ``pytest -q`` job in
``.github/workflows/ci.yml`` on every PR — no separate workflow needed.

The check walks every dataclass reachable from ``PheasantConfig`` and, for
each field whose type is itself a dataclass (i.e. every "section" a
contributor might add, like ``search.embeddings`` or ``ingestion.captioner``
today), asserts the field name appears in both reference documents. Plain
scalar fields (``enabled: bool``, ``model: str``, ...) are deliberately not
checked one-by-one — that would be brittle busywork with little signal;
section names are the meaningful, low-churn unit worth enforcing.
"""

from __future__ import annotations

import dataclasses
import typing

from pheasant.config.schema import PheasantConfig

from tests.conftest import REPO_ROOT

CONFIG_DOCS = REPO_ROOT / "docs" / "configuration.md"
WIZARD_PROMPT = REPO_ROOT / "agent" / "config_wizard_prompt.md"
EXAMPLE_YAML = REPO_ROOT / "pheasant.example.yaml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SCHEMA_FILE = REPO_ROOT / "src" / "pheasant" / "config" / "schema.py"

# Fields whose name collides with generic prose ("path", "type", "name", ...)
# or that are genuinely internal plumbing rather than a section a user is
# ever walked through. Keep this list short and justify additions in review.
_IGNORE_FIELD_NAMES = frozenset({"limits"})  # SourceConfig.limits: an override
# of sync.limits, already covered under the `sync` walk; not a distinct
# section of its own.


def _flatten_type(annotation: object) -> list[object]:
    """Unwrap Optional/Union/list/... down to the concrete member types."""
    origin = typing.get_origin(annotation)
    if origin is None:
        return [annotation]
    flattened: list[object] = []
    for arg in typing.get_args(annotation):
        if arg is type(None):
            continue
        flattened.extend(_flatten_type(arg))
    return flattened


def _iter_section_field_names(root: type) -> set[str]:
    """Every field name, at any depth, whose type is itself a dataclass.

    Walks the whole PheasantConfig tree once (sources[].* included, via
    ``list[SourceConfig]`` unwrapping), memoizing visited dataclasses so a
    cyclic or repeated reference can't loop forever.
    """
    names: set[str] = set()
    seen_classes: set[type] = set()

    def visit(cls: type) -> None:
        if cls in seen_classes or not dataclasses.is_dataclass(cls):
            return
        seen_classes.add(cls)
        hints = typing.get_type_hints(cls)
        for f in dataclasses.fields(cls):
            annotation = hints.get(f.name, f.type)
            for member in _flatten_type(annotation):
                if dataclasses.is_dataclass(member):
                    if f.name not in _IGNORE_FIELD_NAMES:
                        names.add(f.name)
                    visit(member)

    visit(root)
    return names


def test_source_of_truth_files_exist() -> None:
    """The wizard cites these paths as authoritative; a silent rename here
    would make its very first instruction ("read these files") false."""
    for path in (CONFIG_DOCS, WIZARD_PROMPT, EXAMPLE_YAML, ENV_EXAMPLE, SCHEMA_FILE):
        assert path.is_file(), f"expected source-of-truth file missing: {path}"


def test_every_config_section_is_documented_in_configuration_reference() -> None:
    doc_text = CONFIG_DOCS.read_text(encoding="utf-8")
    sections = _iter_section_field_names(PheasantConfig)
    missing = sorted(name for name in sections if name not in doc_text)
    assert not missing, (
        "docs/configuration.md is missing a mention of these config "
        f"section(s) found in PheasantConfig: {missing}. Add a `## "
        "`<section>`` (or a row in an existing table) documenting what it "
        "controls — see docs/configuration.md's existing sections for the "
        "expected shape."
    )


def test_every_config_section_is_walked_by_the_wizard() -> None:
    wizard_text = WIZARD_PROMPT.read_text(encoding="utf-8")
    sections = _iter_section_field_names(PheasantConfig)
    missing = sorted(name for name in sections if name not in wizard_text)
    assert not missing, (
        "agent/config_wizard_prompt.md does not mention these config "
        f"section(s) found in PheasantConfig: {missing}. Add them to the "
        "walk order in its §5 (or a dedicated note, the way `search` "
        "and `sources` get one) so the wizard explains and asks about "
        "them. See docs/how-to/config-wizard.md and "
        ".claude/skills/config-surface-sync/SKILL.md."
    )


def test_wizard_prompt_names_its_source_of_truth_files() -> None:
    """The wizard's #1 rule is to re-read these files rather than recite
    memorized options — make sure it still names all four by path."""
    wizard_text = WIZARD_PROMPT.read_text(encoding="utf-8")
    for reference in (
        "docs/configuration.md",
        "pheasant.example.yaml",
        ".env.example",
        "src/pheasant/config/schema.py",
    ):
        assert reference in wizard_text, (
            f"agent/config_wizard_prompt.md no longer references {reference} "
            "as a source-of-truth file"
        )


def test_tool_adapters_point_at_the_canonical_wizard_file() -> None:
    """Every per-tool entry point must be a thin pointer at the one
    canonical spec, never a second copy that could drift from it."""
    adapters = [
        REPO_ROOT / ".claude" / "commands" / "config-wizard.md",
        REPO_ROOT / ".github" / "prompts" / "pheasant-config-wizard.prompt.md",
        REPO_ROOT / ".gemini" / "commands" / "config-wizard.toml",
        REPO_ROOT / "agent" / "codex_prompts" / "pheasant-config-wizard.md",
        REPO_ROOT / "AGENTS.md",
    ]
    for adapter in adapters:
        assert adapter.is_file(), f"expected wizard adapter file missing: {adapter}"
        text = adapter.read_text(encoding="utf-8")
        assert "agent/config_wizard_prompt.md" in text, (
            f"{adapter} no longer points at agent/config_wizard_prompt.md — "
            "adapters must stay thin pointers, not duplicate its content"
        )


def test_wizard_lists_the_pheasantconfig_field_walk_order() -> None:
    """The wizard's §5 must spell out its top-level walk order as one
    explicit ``a`` -> ``b`` -> ``c`` sequence, in ``PheasantConfig``'s own
    field-declaration order (the wizard's ``pheasant.yaml`` draft is meant
    to come out shaped like ``pheasant.example.yaml``).

    Deliberately not a "find each name anywhere and check positions" check:
    individual section names are legitimately mentioned earlier in the file
    for unrelated reasons (e.g. "the `pheasant` section" in the bootstrap
    questions), which isn't drift. What must exist is the one authoritative
    order statement itself.
    """
    top_level_fields = [f.name for f in dataclasses.fields(PheasantConfig)]
    expected_order_statement = " → ".join(f"`{name}`" for name in top_level_fields)
    wizard_text = WIZARD_PROMPT.read_text(encoding="utf-8")
    assert expected_order_statement in wizard_text, (
        "agent/config_wizard_prompt.md's §5 no longer states the exact walk "
        f"order matching PheasantConfig's field declaration order:\n\n"
        f"{expected_order_statement}\n\n"
        "Update its explicit `a` → `b` → `c` order line to match schema.py."
    )
