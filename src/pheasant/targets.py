"""Turn anything a user can name into a source entry.

``pheasant up ~/notes https://github.com/me/proj https://docs.example.com``
should just work, so this module answers one question: given an arbitrary
string, what source does the user mean? It classifies by shape — scheme,
git-ness, whether the path exists on disk and what it contains — and hands
back a ``ResolvedTarget`` the config renderer can write out verbatim.

Two extras make the "folder collection" case a one-liner:

* a glob (``~/clients/*``) expands to one source per matching directory;
* ``--split`` over a parent directory does the same without a glob,

so a directory of directories becomes a directory of *sources*, each
independently syncable, rather than one undifferentiated blob.

Remote git repositories are cloned once into ``<state>/sources/<slug>``
and then indexed as an ordinary local repository — the connector layer
never learns a new trick.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from pheasant.config.schema import SourceType

GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht")

# Explicit override prefixes, so an unusual target is still a one-liner:
# `pheasant up notion:workspace` or `pheasant up web:https://…`.
TYPE_PREFIXES = {
    "repo": SourceType.repository,
    "repository": SourceType.repository,
    "git": SourceType.repository,
    "vault": SourceType.obsidian_vault,
    "obsidian": SourceType.obsidian_vault,
    "folder": SourceType.document_folder,
    "docs": SourceType.document_folder,
    "markdown": SourceType.markdown_folder,
    "notes": SourceType.markdown_folder,
    "file": SourceType.single_file,
    "web": SourceType.web_collection,
    "api": SourceType.api,
    "s3": SourceType.s3,
    "memory": SourceType.memory,
}

MARKDOWN_INCLUDES = ["**/*.md", "**/*.markdown"]
WEB_INCLUDES = ["**/*.html", "**/*.htm", "**/*.md", "**/*.txt"]


class TargetError(ValueError):
    """A target string could not be resolved to a source."""


@dataclass
class ResolvedTarget:
    """One source entry, ready to render into a config."""

    name: str
    type: str
    path: str
    description: str
    include: list[str] | None = None
    urls: list[str] = field(default_factory=list)
    connector: dict | None = None
    # Remote repos: clone this before the first sync. None for local targets.
    clone_url: str | None = None
    # True when `path` is a real local directory/file that must exist.
    local: bool = True

    def to_source_dict(self) -> dict:
        payload: dict = {
            "name": self.name,
            "type": self.type,
            "path": self.path,
            "description": self.description,
        }
        if self.include:
            payload["include"] = list(self.include)
        if self.urls:
            payload["urls"] = list(self.urls)
        if self.connector:
            payload["connector"] = dict(self.connector)
        return payload


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "source"


def is_git_url(spec: str) -> bool:
    """Remote git remotes, in all the shapes people actually paste."""
    if spec.startswith(("git@", "ssh://", "git://")):
        return True
    if spec.endswith(".git"):
        return True
    parsed = urlparse(spec)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        host = parsed.netloc.split("@")[-1].split(":")[0].lower()
        if host in GIT_HOSTS:
            # A repo root is /owner/name; anything deeper is a page, not a clone.
            parts = [p for p in parsed.path.split("/") if p]
            return len(parts) == 2
    return False


#: Transports a clone URL may name. Everything else — notably git's
#: ``ext::`` helper, which runs an arbitrary shell command — is refused
#: before it reaches ``git clone``.
CLONE_SCHEMES = frozenset({"http", "https", "ssh", "git", "git+ssh"})

#: ``user@host:path``, the scp-like remote form git accepts without a scheme.
SCP_LIKE = re.compile(r"^[A-Za-z0-9._~+-]+@[A-Za-z0-9.-]+:(?!//).+$")


def validate_clone_url(url: str) -> str:
    """Refuse a clone URL that ``git clone`` must not be handed.

    ``clone_url`` reaches ``git clone`` as an argv element, and the string
    comes from whatever the caller typed (``pheasant up <x>``, or the
    unauthenticated ``POST /sources/quick-add``). Two shapes are dangerous
    regardless of how git happens to be configured on the host:

    * a leading ``-`` is parsed by git as an *option*, not a URL, which turns
      "clone this" into "run git with flags I chose";
    * a transport helper such as ``ext::`` names a command for git to run.
      Current git refuses ``ext`` by default, but that is git's defense, not
      ours, and it is a configuration flag away from being off.

    So the allowlist is positive: a known remote scheme, or the scp-like
    ``user@host:path`` form.
    """
    candidate = url.strip()
    if not candidate:
        raise TargetError("empty clone URL")
    if candidate.startswith("-"):
        raise TargetError(
            f"refusing to clone {url!r}: a URL starting with '-' would be read by git as an option"
        )
    scheme = urlparse(candidate).scheme.lower()
    if scheme:
        if scheme not in CLONE_SCHEMES:
            raise TargetError(
                f"refusing to clone {url!r}: unsupported transport {scheme!r} "
                f"(allowed: {', '.join(sorted(CLONE_SCHEMES))})"
            )
        return candidate
    if SCP_LIKE.match(candidate):
        return candidate
    raise TargetError(
        f"refusing to clone {url!r}: expected a "
        f"{'/'.join(sorted(CLONE_SCHEMES))} URL or a user@host:path remote"
    )


def repo_name_from_url(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return slugify(tail)


def detect_local_type(path: Path) -> SourceType:
    """Classify a local path by what it actually holds."""
    if path.is_file():
        return SourceType.single_file
    if (path / ".obsidian").is_dir():
        return SourceType.obsidian_vault
    if (path / ".git").is_dir():
        return SourceType.repository
    if _is_mostly_markdown(path):
        return SourceType.markdown_folder
    return SourceType.document_folder


def _is_mostly_markdown(path: Path) -> bool:
    """A notes folder is markdown-dominant; a mixed folder is not.

    Bounded scan — a shallow sample is enough to pick an include list, and
    walking a huge tree just to choose a default is not worth the latency.
    """
    markdown = 0
    other = 0
    try:
        for entry in list(path.rglob("*"))[:400]:
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if entry.suffix.lower() in (".md", ".markdown"):
                markdown += 1
            else:
                other += 1
    except OSError:
        return False
    return markdown > 0 and markdown >= other


def _describe(source_type: SourceType, origin: str) -> str:
    return f"Auto-detected {source_type.value} from {origin}"


def _local_target(path: Path, *, name: str | None, forced: SourceType | None) -> ResolvedTarget:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise TargetError(f"path does not exist: {resolved}")
    source_type = forced or detect_local_type(resolved)
    include = None
    if source_type is SourceType.markdown_folder:
        include = list(MARKDOWN_INCLUDES)
    elif source_type is SourceType.document_folder:
        # A user who points setup/up at a mixed folder is explicitly asking
        # pheasant to inspect that folder.  The schema's conservative default
        # is intentionally code/Markdown-only, so carry an explicit broad
        # include here; the extractor/captioner/transcriber gates recognize it
        # and secret-file exclusions still apply at sync time.
        include = ["**/*"]
    elif source_type is SourceType.single_file:
        include = [resolved.name]
    return ResolvedTarget(
        name=name or slugify(resolved.stem if resolved.is_file() else resolved.name),
        type=source_type.value,
        path=str(resolved.parent if resolved.is_file() else resolved),
        description=_describe(source_type, str(resolved)),
        include=include,
    )


def _git_target(url: str, clone_root: Path, *, name: str | None) -> ResolvedTarget:
    url = validate_clone_url(url)
    repo = name or repo_name_from_url(url)
    destination = (clone_root / repo).resolve()
    return ResolvedTarget(
        name=repo,
        type=SourceType.repository.value,
        path=str(destination),
        description=f"Git repository cloned from {url}",
        clone_url=url,
    )


def _web_target(url: str, workspace: Path, *, name: str | None) -> ResolvedTarget:
    parsed = urlparse(url)
    label = name or slugify(parsed.netloc or "web")
    return ResolvedTarget(
        name=label,
        type=SourceType.web_collection.value,
        # Web sources still carry a path (the cache/anchor dir); the URLs
        # are what actually gets fetched.
        path=str((workspace / "web" / label).resolve()),
        description=f"Web collection seeded from {url}",
        include=list(WEB_INCLUDES),
        urls=[url],
        local=False,
    )


def _s3_target(url: str, workspace: Path, *, name: str | None) -> ResolvedTarget:
    parsed = urlparse(url)
    label = name or slugify(parsed.netloc or "s3")
    return ResolvedTarget(
        name=label,
        type=SourceType.s3.value,
        path=str((workspace / "s3" / label).resolve()),
        description=f"S3 bucket {url}",
        urls=[url],
        local=False,
    )


def _plugin_target(kind: str, value: str, workspace: Path, *, name: str | None) -> ResolvedTarget:
    """A connector-plugin source (notion, slack, gdrive, …) by name.

    Step 31.1 resolves unknown type strings through the entry-point
    registry at dispatch time, so `up notion:my-workspace` needs no
    special-casing here beyond carrying the type through.
    """
    label = name or slugify(value or kind)
    return ResolvedTarget(
        name=label,
        type=kind,
        path=str((workspace / kind / label).resolve()),
        description=f"{kind} connector source ({value})" if value else f"{kind} connector source",
        local=False,
    )


def expand_specs(specs: list[str], *, split: bool = False) -> list[str]:
    """Expand globs and, with ``--split``, a parent directory's children."""
    expanded: list[str] = []
    for spec in specs:
        if any(ch in spec for ch in "*?[") and "://" not in spec:
            root = Path(spec).expanduser()
            base = root.parent if root.parent != root else Path(".")
            matches = sorted(p for p in base.glob(root.name) if p.is_dir() or p.is_file())
            if not matches:
                raise TargetError(f"no paths matched: {spec}")
            expanded.extend(str(p) for p in matches)
            continue
        if split and "://" not in spec:
            candidate = Path(spec).expanduser()
            if candidate.is_dir():
                children = sorted(
                    p for p in candidate.iterdir() if p.is_dir() and not p.name.startswith(".")
                )
                if children:
                    expanded.extend(str(p) for p in children)
                    continue
        expanded.append(spec)
    return expanded


def resolve_target(
    spec: str,
    *,
    clone_root: Path,
    workspace: Path,
    name: str | None = None,
) -> ResolvedTarget:
    """Classify one target string into a source entry."""
    spec = spec.strip()
    if not spec:
        raise TargetError("empty target")

    forced: SourceType | None = None
    prefix, separator, remainder = spec.partition(":")
    if separator and prefix.lower() in TYPE_PREFIXES and not remainder.startswith("//"):
        forced = TYPE_PREFIXES[prefix.lower()]
        spec = remainder or spec
    elif separator and prefix.lower() not in TYPE_PREFIXES and "//" not in remainder:
        # `notion:workspace`-style plugin target; a bare Windows drive
        # letter (`C:\…`) is one character and never a plugin name.
        if len(prefix) > 1 and re.fullmatch(r"[a-z][a-z0-9_-]*", prefix.lower()):
            local_candidate = Path(spec).expanduser()
            if not local_candidate.exists():
                return _plugin_target(prefix.lower(), remainder, workspace, name=name)

    if spec.startswith("s3://") or forced is SourceType.s3:
        return _s3_target(spec, workspace, name=name)
    if forced is SourceType.repository and "://" in spec or is_git_url(spec):
        return _git_target(spec, clone_root, name=name)
    parsed = urlparse(spec)
    if parsed.scheme in ("http", "https"):
        if forced is SourceType.api:
            target = _web_target(spec, workspace, name=name)
            return ResolvedTarget(
                name=target.name,
                type=SourceType.api.value,
                path=target.path,
                description=f"API collection at {spec}",
                urls=[spec],
                local=False,
            )
        return _web_target(spec, workspace, name=name)

    return _local_target(Path(spec), name=name, forced=forced)


def resolve_targets(
    specs: list[str],
    *,
    clone_root: Path,
    workspace: Path,
    split: bool = False,
    name: str | None = None,
) -> list[ResolvedTarget]:
    """Resolve every spec, de-duplicating the source names they produce."""
    expanded = expand_specs(specs, split=split)
    single = len(expanded) == 1
    targets: list[ResolvedTarget] = []
    used: set[str] = set()
    for spec in expanded:
        target = resolve_target(
            spec,
            clone_root=clone_root,
            workspace=workspace,
            name=name if single else None,
        )
        base = target.name
        suffix = 2
        while target.name in used:
            target.name = f"{base}-{suffix}"
            suffix += 1
        used.add(target.name)
        targets.append(target)
    return targets


#: Checked in this order because GITHUB_TOKEN is the name most users already
#: have set (GitHub Actions' own ambient token, and the name this project's
#: own .env.example documents); GH_TOKEN is the ``gh`` CLI's name, checked
#: second so an environment with both prefers the more explicit one.
GITHUB_TOKEN_ENV_CANDIDATES = ("GITHUB_TOKEN", "GH_TOKEN")


def _github_token() -> str | None:
    for name in GITHUB_TOKEN_ENV_CANDIDATES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _is_github_https_url(url: str) -> bool:
    """True for an HTTP(S) github.com remote — never for SSH/scp-like forms.

    SSH already carries its own auth (a deploy key or agent), so a token
    would be both useless and, if ever wired to the wrong transport, a way
    to leak it somewhere unintended. Only HTTP(S) is subject to
    ``http.extraHeader``.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    return host == "github.com"


def _git_env(clone_url: str | None = None) -> dict[str, str]:
    """Environment for the git subprocesses this module runs.

    Two things a clone must not do unattended: block on a credential prompt
    (an interactive password prompt in a server process hangs the sync), and
    speak a transport helper. ``protocol.allow=never`` plus explicit
    per-protocol allowances pins the set to the ones ``validate_clone_url``
    already accepts, so a hostile URL that slipped past parsing still has no
    helper to reach.

    When ``clone_url`` is an HTTPS github.com remote and a ``GITHUB_TOKEN``/
    ``GH_TOKEN`` is set, a Basic-auth header is injected via
    ``http.https://github.com/.extraheader`` — the same mechanism GitHub
    Actions' own checkout action uses — so a private repository can be
    cloned/fetched without a browser or a stored git credential. The token
    reaches git only through ``GIT_CONFIG_KEY_N``/``GIT_CONFIG_VALUE_N`` env
    vars, never through argv (invisible to a `ps`/Task Manager listing that
    the plain URL-embedded ``https://<token>@github.com/...`` form is not)
    and never through the URL string itself (git's own failure messages
    quote the URL back verbatim, which would otherwise leak the token into
    a raised ``TargetError`` — and, via the quick-add API, into an
    unauthenticated caller's error response). Scoped to github.com
    specifically, not a blanket credential helper, so the token is never
    sent to an unrelated remote even if one is cloned in the same process.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "")
    configs = [
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        ("protocol.http.allow", "always"),
        ("protocol.ssh.allow", "always"),
        ("protocol.git.allow", "always"),
    ]
    token = _github_token() if clone_url and _is_github_https_url(clone_url) else None
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        configs.append(("http.https://github.com/.extraheader", f"AUTHORIZATION: basic {basic}"))
    env["GIT_CONFIG_COUNT"] = str(len(configs))
    for index, (key, value) in enumerate(configs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def fetch_target(target: ResolvedTarget) -> str | None:
    """Materialize a remote target locally. Returns a status line, or None.

    Idempotent: an existing clone is fetched and fast-forwarded rather than
    re-cloned, so re-running ``up`` neither duplicates work nor discards
    local commits.
    """
    if not target.clone_url:
        return None
    # Re-check at the boundary: a ResolvedTarget can also be built by hand or
    # rehydrated from config, so the validation must not live only in the
    # classifier that usually produces one.
    clone_url = validate_clone_url(target.clone_url)
    if shutil.which("git") is None:
        raise TargetError("git is required to clone a remote repository but was not found on PATH")
    destination = Path(target.path)
    if (destination / ".git").is_dir():
        subprocess.run(
            ["git", "-C", str(destination), "fetch", "--all", "--quiet"],
            check=False,
            capture_output=True,
            env=_git_env(clone_url),
        )
        return f"{target.name}: reusing existing clone at {destination}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        # `--` ends option parsing, so nothing after it can be read as a flag.
        ["git", "clone", "--quiet", "--", clone_url, str(destination)],
        capture_output=True,
        text=True,
        env=_git_env(clone_url),
    )
    if result.returncode != 0:
        raise TargetError(
            f"could not clone {target.clone_url}: {result.stderr.strip() or 'git clone failed'}"
        )
    return f"{target.name}: cloned {target.clone_url} → {destination}"
