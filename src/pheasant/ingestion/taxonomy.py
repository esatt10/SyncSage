"""Structural taxonomy extraction for books, procedures and legal documents.

Highly structured documents carry their own outline — Part / Chapter /
Article / Section / § / 1.2.3 / (a) — and that outline is the most useful
retrieval signal they have. "What does § 12.3 say about termination?" is a
different question from "find the word termination", and answering the first
needs the document's hierarchy, not just its text.

Three things already existed for this and none of them were connected:

1. :class:`pheasant.ingestion.chunking.TextChunk` has a ``heading_path``
   field, ``chunks`` has a ``heading_path`` column, and ``chunks_fts``
   indexes it **with BM25 weight 2.0 — double the body text**. It was
   ``NULL`` for every chunk ever indexed, because ``chunks_for_source``
   never passed one.
2. ``docs/graph_model.md`` documents a ``heading`` node type and a
   ``has_heading`` edge type. Neither was ever emitted anywhere in the code.
3. ``graph/enrichment.py`` regexes Markdown headings and hands them to
   ``_add_concept`` — which has been a no-op since concept extraction was
   retired (2026-08-03). The regex still runs and the result is discarded.

So this module fills in a contract the rest of the system already describes,
rather than adding a new one.

Deterministic and offline
-------------------------
Rule-based only: a set of ordered line patterns plus a stack walk to nest
them. No model, no network, no LLM (rule 1 holds by construction). The same
bytes always yield the same taxonomy, so a re-sync of unchanged content
reproduces identical ``heading_path`` values and identical graph nodes.

Opt-in, per source, for a reason
--------------------------------
``sources[].taxonomy.enabled`` defaults to **false**. Two reasons, both real:

- Populating ``heading_path`` changes what the FTS index contains for a
  source, and a weight-2.0 column going from empty to full changes ranking.
  That is an improvement on structured corpora and an unwanted surprise on
  arbitrary ones, so it is an explicit choice per source.
- The numbering rules are genuinely ambiguous on prose. ``1. Introduction``
  in a standards document is a section; ``1. Buy milk`` in a note is a list
  item, and nothing in the line distinguishes them. Heuristics below reduce
  the confusion but cannot remove it — which is exactly why this is enabled
  per source, on the sources where the structure is real.

Level assignment
----------------
Each rule carries its own natural depth, and nesting is then a stack walk,
so a document may mix conventions (``ARTICLE IV`` then ``4.1`` then ``(a)``)
and still nest correctly:

===========================  =====
Pattern                      Level
===========================  =====
Markdown ``#``…``######``    1-6
PART / TITLE / BOOK / DIVISION / SCHEDULE / APPENDIX / ANNEX / EXHIBIT  1
CHAPTER / SUBPART            2
ALL-CAPS heading line        2
ARTICLE / SECTION / RULE / § 3
``N``                        3
``N.M``                      4
CLAUSE / STEP / PARAGRAPH    4
``N.M.P`` and deeper         5+
``(a)`` / ``(iv)``           6
===========================  =====

Skipped levels are fine: the stack nests a level-4 heading under a level-2
one when no level-3 heading intervenes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Separator between breadcrumb components in ``heading_path``. Punctuation
#: the FTS5 tokenizer discards, so a path contributes its words to the index
#: without contributing a junk term of its own.
PATH_SEPARATOR = " > "

#: Bounds. A pathological or misdetected document must not be able to add
#: unbounded nodes to the graph or an unbounded string to every chunk row.
MAX_HEADINGS_PER_DOCUMENT = 2_000
MAX_TITLE_CHARS = 200
MAX_PATH_CHARS = 500

#: Longest a line may be and still be considered a heading. Real headings are
#: short; this is the cheapest single filter against prose false positives.
MAX_HEADING_LINE_CHARS = 120
#: Most words a heading's title may carry. "1. Introduction" passes;
#: "1. Take the lid off and then pour the contents into the mixing bowl" does
#: not. Ten is chosen against real headings rather than guessed: "Limitation
#: of Liability and Exclusion of Consequential Damages" is eight words, and
#: legal/procedural headings longer than ten are rare while numbered list
#: items longer than ten are common.
MAX_TITLE_WORDS = 10

_KEYWORD_LEVELS: dict[str, int] = {
    "book": 1,
    "part": 1,
    "title": 1,
    "division": 1,
    "schedule": 1,
    "appendix": 1,
    "annex": 1,
    "exhibit": 1,
    "chapter": 2,
    "subpart": 2,
    "article": 3,
    "section": 3,
    "rule": 3,
    "regulation": 3,
    "clause": 4,
    "step": 4,
    "paragraph": 4,
    "subsection": 5,
}

_ORDINAL = r"[0-9]+(?:\.[0-9]+)*[A-Za-z]?|[IVXLCDM]+|[A-Z]"

_RE_MARKDOWN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_RE_KEYWORD = re.compile(
    rf"^\s*({'|'.join(_KEYWORD_LEVELS)})\s+({_ORDINAL})\s*[-–—:.)]?\s*(.*)$",
    re.IGNORECASE,
)
_RE_SECTION_SYMBOL = re.compile(r"^\s*§{1,2}\s*([0-9]+(?:\.[0-9]+)*[A-Za-z]?)\s*[-–—:.)]?\s*(.*)$")
_RE_NUMBERED = re.compile(r"^\s*([0-9]+(?:\.[0-9]+){0,5})\.?\s+(\S.*)$")
_RE_LETTERED = re.compile(r"^\s*\(([a-z]{1,2}|[ivxlcdm]{1,5})\)\s*(\S.*)$")
_RE_CAPS = re.compile(r"^\s*([A-Z][A-Z0-9][A-Z0-9 ,'&/()\[\].:;–—-]{2,78})\s*$")

#: Rule names, for ``TaxonomySettings.detect``. Ordered by how specific the
#: rule is: the first that matches a line wins.
RULE_NAMES = ("markdown", "keyword", "code", "numbered", "lettered", "caps")
DEFAULT_RULES: tuple[str, ...] = RULE_NAMES


@dataclass(frozen=True)
class SectionHeading:
    """One detected structural heading.

    ``number`` is the document's own code for the section (``"12.3"``,
    ``"IV"``, ``"(a)"``) and is kept separate from ``title`` so a caller can
    look a section up by its citation rather than by its wording — the
    question "what does § 12.3 say" is answerable from ``number`` alone.
    """

    line: int
    level: int
    number: str | None
    title: str
    kind: str
    path: str

    @property
    def label(self) -> str:
        if self.number and self.title:
            return f"{self.number} {self.title}"
        return self.number or self.title

    def as_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "level": self.level,
            "number": self.number,
            "title": self.title,
            "kind": self.kind,
            "path": self.path,
        }


def _plausible_heading(title: str, line_text: str) -> bool:
    """Whether a numbered/lettered/caps line reads like a heading, not prose.

    Cheap structural filters only — deliberately not a classifier. Anything
    smarter would be a judgement call that varies by corpus, and this feature
    is enabled per source precisely so the operator makes that call.
    """
    if len(line_text) > MAX_HEADING_LINE_CHARS:
        return False
    if len(title.split()) > MAX_TITLE_WORDS:
        return False
    # A line ending in a clause separator is mid-sentence, not a heading.
    if title.rstrip().endswith((",", ";", ":", "and", "or")):
        return False
    # A heading rarely contains sentence-ending punctuation followed by more
    # words ("1. Do this. Then do that." is an instruction, not a heading).
    if re.search(r"[.!?]\s+\S", title):
        return False
    return True


def _classify(raw_line: str) -> tuple[int, str | None, str, str] | None:
    """``(level, number, title, kind)`` for a heading line, else ``None``."""
    line = raw_line.rstrip()
    if not line.strip():
        return None

    match = _RE_MARKDOWN.match(line)
    if match:
        return len(match.group(1)), None, match.group(2).strip()[:MAX_TITLE_CHARS], "markdown"

    match = _RE_KEYWORD.match(line)
    if match and len(line) <= MAX_HEADING_LINE_CHARS:
        keyword, ordinal, title = match.group(1), match.group(2), match.group(3).strip()
        level = _KEYWORD_LEVELS[keyword.lower()]
        number = f"{keyword.title()} {ordinal}"
        return level, number, title[:MAX_TITLE_CHARS], "keyword"

    match = _RE_SECTION_SYMBOL.match(line)
    if match:
        ordinal, title = match.group(1), match.group(2).strip()
        # A § citation is a heading regardless of what follows it on the line;
        # legal drafting routinely runs the text straight on from the number.
        return 3 + ordinal.count("."), f"§ {ordinal}", title[:MAX_TITLE_CHARS], "code"

    match = _RE_NUMBERED.match(line)
    if match:
        ordinal, title = match.group(1), match.group(2).strip()
        if _plausible_heading(title, line):
            return 3 + ordinal.count("."), ordinal, title[:MAX_TITLE_CHARS], "numbered"

    match = _RE_LETTERED.match(line)
    if match:
        ordinal, title = match.group(1), match.group(2).strip()
        if _plausible_heading(title, line):
            return 6, f"({ordinal})", title[:MAX_TITLE_CHARS], "lettered"

    match = _RE_CAPS.match(line)
    if match:
        title = match.group(1).strip()
        # Require two words so a stray acronym or a shouted single word does
        # not become a section.
        if len(title.split()) >= 2 and _plausible_heading(title, line):
            return 2, None, title[:MAX_TITLE_CHARS], "caps"

    return None


def _continuation_title(lines: list[str], heading_line: int) -> str:
    """Title carried on the line *after* a bare ``ARTICLE IV`` / ``§ 5``.

    Legal drafting routinely puts the citation and its caption on separate
    lines:

    .. code-block:: text

        ARTICLE IV
        Term and Termination

    Without this, that Article's path reads "Article IV" and its actual
    subject — the words someone would search for — is dropped on the floor.
    Only a short, non-heading, non-sentence next line is taken, so body text
    immediately following a bare citation is not mistaken for a caption.
    """
    for offset in range(heading_line, min(heading_line + 2, len(lines))):
        candidate = lines[offset].strip()
        if not candidate:
            continue
        if len(candidate) > MAX_HEADING_LINE_CHARS or len(candidate.split()) > MAX_TITLE_WORDS:
            return ""
        # A line that is itself a heading belongs to the outline, not to this
        # heading's caption.
        if _classify(lines[offset]) is not None:
            return ""
        if candidate.endswith((".", ";", ",")):
            return ""
        return candidate[:MAX_TITLE_CHARS]
    return ""


def detect_headings(
    text: str,
    *,
    rules: tuple[str, ...] | list[str] = DEFAULT_RULES,
    max_depth: int = 6,
    max_headings: int = MAX_HEADINGS_PER_DOCUMENT,
) -> list[SectionHeading]:
    """Detect the structural outline of ``text``, deepest-first nested.

    Returns headings in document order, each carrying the full breadcrumb
    ``path`` of its ancestors. Deterministic: same text, same rules, same
    result.
    """
    if not text:
        return []
    enabled = {name for name in rules if name in RULE_NAMES}
    if not enabled:
        return []

    lines = text.splitlines()
    headings: list[SectionHeading] = []
    stack: list[SectionHeading] = []
    for index, raw_line in enumerate(lines, start=1):
        classified = _classify(raw_line)
        if classified is None:
            continue
        level, number, title, kind = classified
        if kind not in enabled:
            continue
        if not title and kind in ("keyword", "code"):
            title = _continuation_title(lines, index)
        if level > max_depth:
            continue
        if not (number or title):
            continue

        while stack and stack[-1].level >= level:
            stack.pop()
        heading = SectionHeading(
            line=index,
            level=level,
            number=number,
            title=title,
            kind=kind,
            path="",  # filled below, once the label is known
        )
        crumbs = [ancestor.label for ancestor in stack] + [heading.label]
        path = PATH_SEPARATOR.join(c for c in crumbs if c)[:MAX_PATH_CHARS]
        heading = SectionHeading(
            line=index,
            level=level,
            number=number,
            title=title,
            kind=kind,
            path=path,
        )
        stack.append(heading)
        headings.append(heading)
        if len(headings) >= max_headings:
            logger.warning("taxonomy detection hit the %d-heading cap; truncating", max_headings)
            break
    return headings


def heading_path_for_line(headings: list[SectionHeading], line: int) -> str | None:
    """Breadcrumb of the innermost heading at or above ``line``.

    ``headings`` must be in document order (as :func:`detect_headings`
    returns them). Used to label a chunk with the section it falls inside
    **without moving the chunk's boundaries**, so enabling the feature adds a
    column value rather than re-cutting the corpus.
    """
    found: str | None = None
    for heading in headings:
        if heading.line > line:
            break
        found = heading.path
    return found


def taxonomy_tree(headings: list[SectionHeading]) -> list[dict[str, Any]]:
    """Nest a flat heading list into a tree of ``{...,"children":[...]}``.

    The shape the taxonomy is actually *read* in — one node per section with
    its children under it — as opposed to the flat document-order list the
    detector produces and the chunk labelling needs.
    """
    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    for heading in headings:
        node = heading.as_dict()
        node["children"] = []
        while stack and stack[-1][0] >= heading.level:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            roots.append(node)
        stack.append((heading.level, node))
    return roots


def source_taxonomy_enabled(source: Any) -> bool:
    """Whether a source has taxonomy extraction switched on.

    Tolerant of a source object without the block (a config written before
    this feature, or a duck-typed test double), which reads as disabled.
    """
    settings = getattr(source, "taxonomy", None)
    return bool(getattr(settings, "enabled", False))


def rules_for_source(source: Any) -> tuple[str, ...]:
    settings = getattr(source, "taxonomy", None)
    configured = list(getattr(settings, "detect", None) or ())
    valid = tuple(name for name in configured if name in RULE_NAMES)
    if configured and not valid:
        logger.warning(
            "taxonomy.detect for source %r names no known rule (%s); using defaults",
            getattr(source, "name", "?"),
            ", ".join(RULE_NAMES),
        )
    return valid or DEFAULT_RULES


def max_depth_for_source(source: Any) -> int:
    settings = getattr(source, "taxonomy", None)
    depth = getattr(settings, "max_depth", 6)
    try:
        return max(1, min(6, int(depth)))
    except (TypeError, ValueError):
        return 6


def headings_for_source(source: Any, text: str) -> list[SectionHeading]:
    """Detect headings for ``source``, honouring its config; ``[]`` when off."""
    if not source_taxonomy_enabled(source):
        return []
    return detect_headings(
        text,
        rules=rules_for_source(source),
        max_depth=max_depth_for_source(source),
    )
