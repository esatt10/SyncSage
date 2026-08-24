"""Bounded, pruning filesystem traversal for filesystem-backed sources.

pheasant supports pointing a source at any path the operator can read —
including a home directory. That makes the *traversal* a resource question,
not just a correctness one, and the original implementation answered it
badly: ``root.rglob("*")`` materialized every path under the root into a
list and only then applied the exclude patterns. Excluding
``**/node_modules/**`` therefore cost more than not excluding it — the tree
was walked and held in memory either way.

This module replaces that with a traversal that:

* **prunes on the way down** — a directory matching an exclude pattern is
  never descended into, so ``node_modules``/``.git``/``.venv`` cost one
  ``scandir`` entry instead of tens of thousands;
* **stops at ``max_depth`` during traversal** rather than after it;
* **carries a budget** (file count, per-file size, total bytes) and reports
  the moment one is exceeded, so an accidental ``/`` or ``$HOME`` sync is
  refused with an actionable message instead of consuming memory until the
  process dies;
* **reports what it saw** (entries scanned, directories pruned, the biggest
  subtrees), which is what makes a pre-flight ``scan`` possible.

Determinism is preserved: results are sorted, so the same tree yields the
same list in the same order, and re-walking an unchanged tree is
byte-identical. Nothing here reads file *content*.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pheasant.ingestion.pipeline import _match_any, within_max_depth

#: Hard ceiling on directory entries a single walk will visit, independent of
#: any configured budget. This is the "the operator misconfigured everything"
#: backstop: without it, a symlink loop or a mount point with tens of millions
#: of entries has no upper bound at all. Deliberately large enough that no
#: realistic corpus reaches it.
ABSOLUTE_MAX_ENTRIES = 5_000_000


class SyncBudgetExceeded(RuntimeError):
    """A walk exceeded its configured budget.

    Raised rather than silently truncating: a partial index is
    non-deterministic (rule 1's determinism guarantee and the idempotency
    spine both assume a sync either indexes a source or does not), and
    silently indexing "the first 25,000 files of your home directory" is a
    worse outcome than a clear refusal the operator can act on.
    """

    def __init__(self, message: str, report: WalkReport) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class WalkBudget:
    """Limits applied while walking. ``None`` on a field disables that limit."""

    max_files: int | None = None
    max_file_size_bytes: int | None = None
    max_total_bytes: int | None = None

    @classmethod
    def unlimited(cls) -> WalkBudget:
        return cls(None, None, None)

    @classmethod
    def from_settings(cls, settings: object | None) -> WalkBudget:
        """Build from a ``SyncLimitsSettings``-shaped object or a plain dict.

        Accepting a mapping too is deliberate: a config that reaches here
        un-built would otherwise silently degrade to *unlimited*, turning
        every guardrail off exactly when it is most needed.
        """
        if settings is None:
            return cls.unlimited()
        if isinstance(settings, dict):
            get = settings.get
        else:

            def get(name, default=None):
                return getattr(settings, name, default)

        megabytes = get("max_file_size_mb")
        total_megabytes = get("max_total_mb")
        return cls(
            max_files=get("max_files"),
            max_file_size_bytes=int(megabytes * 1024 * 1024) if megabytes else None,
            max_total_bytes=int(total_megabytes * 1024 * 1024) if total_megabytes else None,
        )


@dataclass
class WalkReport:
    """What a walk found. Also the payload behind ``pheasant scan``."""

    files: list[Path] = field(default_factory=list)
    total_bytes: int = 0
    entries_scanned: int = 0
    directories_pruned: int = 0
    #: Files that matched but were skipped for exceeding max_file_size_bytes.
    oversized: list[tuple[str, int]] = field(default_factory=list)
    #: Bytes per top-level subdirectory — the "what is making this big" answer.
    bytes_by_subtree: dict[str, int] = field(default_factory=dict)
    #: Matched files per depth, so a depth cap can be chosen from evidence.
    files_by_depth: dict[int, int] = field(default_factory=dict)
    #: Which budget field stopped the walk, if any.
    limit_hit: str | None = None
    #: Set when the absolute entry ceiling stopped traversal.
    aborted: bool = False

    @property
    def file_count(self) -> int:
        return len(self.files)

    def as_dict(self) -> dict:
        return {
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "total_mb": round(self.total_bytes / (1024 * 1024), 2),
            "entries_scanned": self.entries_scanned,
            "directories_pruned": self.directories_pruned,
            "oversized": [{"path": p, "bytes": b} for p, b in self.oversized[:20]],
            "oversized_count": len(self.oversized),
            "bytes_by_subtree": dict(
                sorted(self.bytes_by_subtree.items(), key=lambda kv: -kv[1])[:20]
            ),
            "files_by_depth": dict(sorted(self.files_by_depth.items())),
            "limit_hit": self.limit_hit,
            "aborted": self.aborted,
        }


def should_prune_directory(relative: str, exclude: list[str]) -> bool:
    """Whether a directory can be skipped without descending into it.

    A pattern written for *files* under a directory (``**/node_modules/**``)
    is the normal way people exclude a subtree, so it has to prune the
    directory itself — otherwise the exclusion only filters results after
    paying the full traversal cost.
    """
    if _match_any(relative, exclude):
        return True
    for pattern in exclude:
        if pattern.endswith("/**") and _match_any(relative, [pattern[:-3]]):
            return True
    return False


def _real_path_under(candidate_real: str, root_real: str) -> bool:
    """Is ``candidate_real`` equal to, or a descendant of, ``root_real``?

    Both arguments must already be ``os.path.realpath`` output — this is a
    plain string containment check, not a filesystem call, so it is only as
    correct as its inputs are already resolved.
    """
    if candidate_real == root_real:
        return True
    return candidate_real.startswith(root_real.rstrip(os.sep) + os.sep)


def walk_source(
    root: Path,
    *,
    include: list[str],
    exclude: list[str],
    max_depth: int | None = None,
    budget: WalkBudget | None = None,
    follow_symlinks: bool = False,
    collect_stats: bool = True,
    allowed_roots: list[Path] | None = None,
) -> WalkReport:
    """Walk ``root``, pruning as it goes, and report what it found.

    Never raises on a budget breach — it records ``limit_hit`` and stops.
    Callers that must refuse (the sync path) check the field and raise;
    callers that want the numbers regardless (``scan``) simply read them.

    ``allowed_roots`` (security audit finding M5) additionally constrains
    where a followed symlink may point, beyond ``root`` itself — pass the
    operator's configured allow-list (``security.allow_workspace_roots`` and
    friends) when ``follow_symlinks`` is on and
    ``security.allow_user_selected_source_paths`` is off, so a symlink
    inside an allow-listed corpus cannot read content the allow-list itself
    would refuse as a source root. It has no effect when ``follow_symlinks``
    is off (the default), since no symlink is ever followed in that case.
    """
    budget = budget or WalkBudget.unlimited()
    report = WalkReport()

    if not root.exists():
        return report

    if root.is_file():
        # A single_file source: the "tree" is one entry, and the include /
        # exclude patterns are matched against the bare filename. `root`
        # itself was already resolved (and symlinks followed) by whatever
        # registered this source, so there is no second hop to re-check here.
        relative = root.name
        if not _match_any(relative, exclude) and (not include or _match_any(relative, include)):
            size = root.stat().st_size
            if budget.max_file_size_bytes and size > budget.max_file_size_bytes:
                report.oversized.append((relative, size))
            else:
                report.files.append(root)
                report.total_bytes += size
                report.files_by_depth[0] = 1
        report.entries_scanned = 1
        return report

    # Security audit finding M5: computed once, used at every symlink this
    # walk follows — `follow_symlinks`'s existing loop-detection realpath
    # was never compared against where the walk is *rooted*, so a symlink
    # anywhere inside an already-validated source root could point outside
    # it (and outside the configured allow-list) and its contents were
    # walked and indexed anyway, defeating the containment check that was
    # applied to the source root itself at registration time.
    symlink_containment_roots: list[str] = []
    if follow_symlinks:
        symlink_containment_roots.append(os.path.realpath(root))
        for extra in allowed_roots or []:
            symlink_containment_roots.append(os.path.realpath(extra))

    # Explicit stack rather than recursion: a deep tree must not be able to
    # exhaust the interpreter's stack, and an explicit frontier is what lets
    # the walk stop cleanly the instant a budget is hit.
    stack: list[tuple[Path, str, int]] = [(root, "", 0)]
    seen_real_dirs: set[str] = set()

    while stack:
        directory, rel_dir, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except (PermissionError, OSError):
            # An unreadable directory is normal when indexing a home dir.
            # Skip it rather than failing the whole sync.
            continue

        for entry in sorted(entries, key=lambda e: e.name):
            report.entries_scanned += 1
            if report.entries_scanned > ABSOLUTE_MAX_ENTRIES:
                report.aborted = True
                report.limit_hit = report.limit_hit or "absolute_max_entries"
                return _finalize(report)

            relative = f"{rel_dir}/{entry.name}" if rel_dir else entry.name

            try:
                is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
            except OSError:
                continue

            if is_dir:
                if should_prune_directory(relative, exclude):
                    report.directories_pruned += 1
                    continue
                # Files inside a directory sit one level deeper than the
                # directory itself, so a directory at depth d only needs
                # descending when d < max_depth allows its contents.
                if max_depth is not None and len(Path(relative).parts) > max_depth:
                    continue
                if follow_symlinks:
                    try:
                        real = os.path.realpath(entry.path)
                    except OSError:
                        continue
                    # Security audit finding M5: containment before loop
                    # detection — a symlink pointing outside every allowed
                    # root is refused outright, not merely deduplicated
                    # against other escapes already seen.
                    if not any(
                        _real_path_under(real, allowed) for allowed in symlink_containment_roots
                    ):
                        continue
                    # Guard against symlink loops when the operator opts in.
                    if real in seen_real_dirs:
                        continue
                    seen_real_dirs.add(real)
                stack.append((Path(entry.path), relative, depth + 1))
                continue

            try:
                if not entry.is_file(follow_symlinks=follow_symlinks):
                    continue
            except OSError:
                continue

            if follow_symlinks:
                # Security audit finding M5: a symlinked *file* is the other
                # half of the same escape — `entry.path` looks like it is
                # under `root`, but opening it later follows the link, so a
                # target outside every allowed root must be refused here,
                # not merely have its nominal path recorded as if safe.
                try:
                    real = os.path.realpath(entry.path)
                except OSError:
                    continue
                if not any(
                    _real_path_under(real, allowed) for allowed in symlink_containment_roots
                ):
                    continue

            if not within_max_depth(relative, max_depth):
                continue
            if _match_any(relative, exclude):
                continue
            if include and not _match_any(relative, include):
                continue

            try:
                size = entry.stat(follow_symlinks=follow_symlinks).st_size
            except OSError:
                continue

            if budget.max_file_size_bytes and size > budget.max_file_size_bytes:
                report.oversized.append((relative, size))
                continue

            if budget.max_files is not None and report.file_count >= budget.max_files:
                report.limit_hit = "max_files"
                return _finalize(report)
            if (
                budget.max_total_bytes is not None
                and report.total_bytes + size > budget.max_total_bytes
            ):
                report.limit_hit = "max_total_mb"
                return _finalize(report)

            report.files.append(Path(entry.path))
            report.total_bytes += size
            if collect_stats:
                depth_of_file = max(0, len(Path(relative).parts) - 1)
                report.files_by_depth[depth_of_file] = (
                    report.files_by_depth.get(depth_of_file, 0) + 1
                )
                top = relative.split("/", 1)[0] if "/" in relative else "."
                report.bytes_by_subtree[top] = report.bytes_by_subtree.get(top, 0) + size

    return _finalize(report)


def _finalize(report: WalkReport) -> WalkReport:
    # Sorted output keeps the walk deterministic regardless of the order the
    # filesystem handed directories back or the stack popped them.
    report.files.sort()
    return report


def budget_message(source_name: str, report: WalkReport, budget: WalkBudget) -> str:
    """An error an operator can act on, not just 'limit exceeded'."""
    biggest = sorted(report.bytes_by_subtree.items(), key=lambda kv: -kv[1])[:3]
    hint = ", ".join(f"{name} ({_mb(size)} MB)" for name, size in biggest)
    if report.limit_hit == "max_files":
        what = f"more than {budget.max_files} matching files"
    elif report.limit_hit == "max_total_mb":
        what = f"more than {_mb(budget.max_total_bytes or 0)} MB of matching content"
    else:
        what = f"more than {ABSOLUTE_MAX_ENTRIES} directory entries"
    return (
        f"source {source_name!r} would index {what} — refusing to sync. "
        f"Scanned {report.entries_scanned} entries"
        + (f"; largest subtrees: {hint}. " if hint else ". ")
        + "Narrow it with `max_depth`, a tighter `include`, or more `exclude` "
        "patterns; run `pheasant scan` to see the shape first; or raise "
        "`sync.limits` / pass full_scan to index it anyway."
    )


def _mb(value: int) -> int:
    return int(value / (1024 * 1024))


def aggregate_depth_counts(report: WalkReport) -> list[dict]:
    """Cumulative file counts per depth — 'depth N would index M files'."""
    cumulative: dict[int, int] = defaultdict(int)
    running = 0
    for depth in sorted(report.files_by_depth):
        running += report.files_by_depth[depth]
        cumulative[depth] = running
    return [{"max_depth": d, "files": cumulative[d]} for d in sorted(cumulative)]
