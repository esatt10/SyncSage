"""Host paths ↔ container paths, and how to make one visible in the other.

The single most common way a first setup fails is this: pheasant runs in a
container, the user types a path they can see on their own machine
(``/Users/me/notes``, ``C:/work/docs``), and the sync reports
``path_missing``. The path is real. It is simply not *in* the container.

Everything here exists to turn that dead end into an instruction:

* :func:`read_mount_table` parses the container's own mount table, so pheasant
  can tell whether a host path is already mounted and, if so, where it landed.
* :func:`resolve_host_path` answers the user's actual question — "can you see
  this?" — with either the container path to use or a precise remedy.
* :func:`mount_suggestion` renders that remedy as copy-pasteable Compose and
  ``docker run`` fragments, plus the ``security.allow_workspace_roots`` entry
  the path also needs.
* :func:`render_compose_override` writes the Compose half for the user
  (``pheasant mount``), because a bind mount is exactly the kind of edit that
  is easy to describe and easy to get subtly wrong by hand.

Nothing here is Docker-specific beyond the rendering: outside a container
every host path is trivially visible, and :func:`resolve_host_path` says so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: Where a mount suggested by pheasant lands inside the container. One
#: directory per mount, named after the host directory, under a root the
#: default config already allows.
CONTAINER_MOUNT_ROOT = "/data"

MOUNTINFO_PATH = "/proc/self/mountinfo"


@dataclass(frozen=True)
class Mount:
    """One entry from the container's mount table.

    ``source`` is the path *on the host side of the mount* as the kernel
    reports it — for a bind mount of a host directory this is the host path
    when the host root filesystem and the bind source share a device, which is
    the ordinary Docker Desktop / Linux case. When it is not (a separate
    device, a named volume), the mapping is still correct in the direction
    that matters here: container path → what it was mounted from.
    """

    mount_point: str
    source: str
    fstype: str
    read_only: bool


def in_container() -> bool:
    """Whether this process is running inside a container.

    Three signals, cheapest first. ``PHEASANT_IN_CONTAINER`` is honored so a
    deployment that containerises differently (Podman, Kubernetes, a
    devcontainer) can state the answer rather than hope the heuristics work.
    """
    override = os.environ.get("PHEASANT_IN_CONTAINER")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "kubepods", "containerd", "libpod"))


def _unescape(value: str) -> str:
    """mountinfo octal-escapes space, tab, newline and backslash."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 3 < len(value) and value[index + 1 : index + 4].isdigit():
            out.append(chr(int(value[index + 1 : index + 4], 8)))
            index += 4
            continue
        out.append(char)
        index += 1
    return "".join(out)


def read_mount_table(path: str | Path = MOUNTINFO_PATH) -> list[Mount]:
    """Parse ``/proc/self/mountinfo``. Empty list on any platform without it.

    Format (kernel docs, fields are positional up to the ``-`` separator)::

        36 35 98:0 /host/dir /container/dir rw,noatime - ext4 /dev/sda1 rw

    Field 4 is the root *within* the mounted filesystem, field 5 the mount
    point in this namespace. Everything after the ``-`` is fstype, source
    device and super options.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    mounts: list[Mount] = []
    for line in text.splitlines():
        fields = line.split(" - ", 1)
        if len(fields) != 2:
            continue
        left, right = fields
        left_parts = left.split()
        right_parts = right.split()
        if len(left_parts) < 6 or len(right_parts) < 2:
            continue
        root = _unescape(left_parts[3])
        mount_point = _unescape(left_parts[4])
        options = left_parts[5]
        mounts.append(
            Mount(
                mount_point=mount_point,
                source=root,
                fstype=right_parts[0],
                read_only="ro" in options.split(","),
            )
        )
    return mounts


#: Prefixes Docker Desktop has used to expose a Windows drive inside its Linux
#: VM. Which one appears depends on the Docker version, so we try them all
#: rather than pick one and be wrong on half the installations.
_WINDOWS_VM_PREFIXES = ("", "/host_mnt", "/run/desktop/mnt/host", "/mnt")


def _clean(path: str) -> str:
    """Collapse separators and strip the trailing slash. No drive handling."""
    text = str(path).replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/") or "/"


def _candidates(path: str) -> list[str]:
    """Every POSIX form a host path might plausibly appear as in the container.

    A POSIX host path has exactly one form. A **Windows** path does not: the
    user types ``C:\\Users\\me\\notes``, and Docker Desktop has, across
    versions, surfaced that in its Linux VM as ``/c/Users/me/notes``,
    ``/host_mnt/c/Users/me/notes``, ``/run/desktop/mnt/host/c/Users/me/notes``,
    and — when the bind is declared with a POSIX-style path — plain
    ``/Users/me/notes``.

    Picking one of those and comparing against it works on some machines and
    silently fails on the rest, which for this feature means telling a Windows
    user their mounted directory is not mounted. So we generate the set and let
    :func:`host_to_container` match any of them.
    """
    text = _clean(path)
    if len(text) >= 2 and text[1] == ":":
        drive = text[0].lower()
        rest = text[2:] or "/"
        if not rest.startswith("/"):
            rest = "/" + rest
        forms = [f"{prefix}/{drive}{rest}" for prefix in _WINDOWS_VM_PREFIXES]
        # The drive-less form, for a bind declared with a POSIX-shaped path.
        forms.append(rest)
        return [_clean(form) for form in forms]
    return [text]


def host_to_container(host_path: str, mounts: list[Mount] | None = None) -> str | None:
    """The container path for a host path, or None when it is not mounted.

    The longest matching mount wins: with both ``/`` and ``/work`` mounted, a
    path under ``/work`` must resolve through ``/work``, not through the root
    mount that also technically contains it.
    """
    mounts = read_mount_table() if mounts is None else mounts
    wanted_forms = _candidates(host_path)
    best: tuple[int, str] | None = None
    for mount in mounts:
        source = _clean(mount.source)
        if source == "/" and mount.fstype in {"overlay", "tmpfs", "proc", "sysfs"}:
            # The container's own root filesystem is not the host's.
            continue
        for wanted in wanted_forms:
            if wanted == source:
                relative = ""
            elif wanted.startswith(source + "/"):
                relative = wanted[len(source) + 1 :]
            else:
                continue
            candidate = (
                str(PurePosixPath(mount.mount_point) / relative) if relative else mount.mount_point
            )
            if best is None or len(source) > best[0]:
                best = (len(source), candidate)
            break
    return best[1] if best else None


def suggested_container_path(host_path: str, root: str = CONTAINER_MOUNT_ROOT) -> str:
    """Where pheasant would mount ``host_path``, if asked to."""
    name = PurePosixPath(_clean(host_path)).name or "mount"
    return str(PurePosixPath(root) / name)


def mount_suggestion(host_path: str, container_path: str | None = None) -> dict:
    """A copy-pasteable remedy for a host path that is not mounted.

    Returns the Compose fragment, the ``docker run`` flag and the
    ``allow_workspace_roots`` entry together, because supplying only the mount
    is a half-fix: the path becomes visible and the API still refuses to
    register a source under it.
    """
    target = container_path or suggested_container_path(host_path)
    host = str(host_path).replace("\\", "/")
    return {
        "host_path": host,
        "container_path": target,
        "compose_volume": f"{host}:{target}:ro",
        "docker_run_flag": f'-v "{host}:{target}:ro"',
        "config_patch": {"security": {"allow_workspace_roots": [target]}},
        "cli": f"pheasant mount {host}",
    }


def resolve_host_path(host_path: str, mounts: list[Mount] | None = None) -> dict:
    """Answer "can pheasant see this path?" with a next step either way.

    Four outcomes, and the caller can act on all of them:

    * ``native`` — not in a container; the path is used as typed.
    * ``visible`` — mounted; ``container_path`` is what to configure.
    * ``not_mounted`` — a real host path that needs a bind mount (``remedy``).
    * ``unknown`` — in a container with no readable mount table. Reported as
      unknown rather than guessed at: claiming "not mounted" when we cannot
      tell would send the user to fix something that may not be broken.
    """
    if not in_container():
        resolved = Path(host_path).expanduser()
        return {
            "status": "native",
            "host_path": str(host_path),
            "container_path": str(resolved),
            "exists": resolved.exists(),
            "remedy": None,
        }
    table = read_mount_table() if mounts is None else mounts
    container_path = host_to_container(host_path, table)
    if container_path is not None:
        return {
            "status": "visible",
            "host_path": str(host_path),
            "container_path": container_path,
            "exists": Path(container_path).exists(),
            "remedy": None,
        }
    if not table:
        return {
            "status": "unknown",
            "host_path": str(host_path),
            "container_path": None,
            "exists": False,
            "remedy": mount_suggestion(host_path),
        }
    return {
        "status": "not_mounted",
        "host_path": str(host_path),
        "container_path": None,
        "exists": False,
        "remedy": mount_suggestion(host_path),
    }


def render_compose_override(
    mounts: dict[str, str],
    *,
    service: str = "pheasant",
    existing: str | None = None,
) -> str:
    """A ``docker-compose.override.yml`` adding ``{host: container}`` mounts.

    Merged into any override file already present rather than replacing it —
    an override file is a place people keep their own edits, and a helper that
    silently discards them is worse than no helper. Existing volume entries are
    preserved and de-duplicated by their container-side target, so re-running
    ``pheasant mount`` for the same path updates rather than appends.
    """
    import yaml

    data: dict = {}
    if existing:
        loaded = yaml.safe_load(existing)
        if isinstance(loaded, dict):
            data = loaded
    services = data.setdefault("services", {})
    if not isinstance(services, dict):
        raise ValueError("compose override's `services` is not a mapping")
    entry = services.setdefault(service, {})
    if not isinstance(entry, dict):
        raise ValueError(f"compose override's `services.{service}` is not a mapping")
    volumes = [v for v in (entry.get("volumes") or []) if isinstance(v, str)]

    by_target = {}
    order: list[str] = []
    for volume in volumes:
        parts = volume.split(":")
        target = parts[1] if len(parts) > 1 else volume
        if target not in by_target:
            order.append(target)
        by_target[target] = volume
    for host, container in mounts.items():
        rendered = f"{str(host).replace(chr(92), '/')}:{container}:ro"
        if container not in by_target:
            order.append(container)
        by_target[container] = rendered

    entry["volumes"] = [by_target[target] for target in order]
    return yaml.safe_dump(data, sort_keys=False)
