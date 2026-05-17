from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


@dataclass(frozen=True)
class Replacement:
    path: Path
    pattern: re.Pattern[str]
    replacement: str
    label: str


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(data["project"]["version"])
    validate_semver(version)
    return version


def validate_semver(version: str) -> None:
    if not SEMVER_RE.fullmatch(version):
        raise SystemExit(f"pyproject.toml project.version is not valid semver: {version}")


def bump(version: str, part: str) -> str:
    match = SEMVER_RE.fullmatch(version)
    if not match or "-" in version or "+" in version:
        raise SystemExit(
            "Automatic major/minor/patch bumps require a stable MAJOR.MINOR.PATCH version"
        )
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"Unknown bump part: {part}")


def replace_once(text: str, replacement: Replacement) -> tuple[str, bool]:
    updated, count = replacement.pattern.subn(replacement.replacement, text, count=1)
    if count != 1:
        raise SystemExit(f"Could not find {replacement.label} in {replacement.path}")
    return updated, updated != text


def set_pyproject_version(version: str) -> None:
    validate_semver(version)
    path = ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    updated, changed = replace_once(
        text,
        Replacement(
            path=path.relative_to(ROOT),
            pattern=re.compile(r'(?m)^(version\s*=\s*)"[^"]+"'),
            replacement=rf'\g<1>"{version}"',
            label="project.version",
        ),
    )
    if changed:
        path.write_text(updated, encoding="utf-8")


def replacements(version: str) -> list[Replacement]:
    image = f"ghcr.io/esatt10/syncsage:{version}"
    return [
        Replacement(
            path=Path("deploy/helm/Chart.yaml"),
            pattern=re.compile(r"(?m)^(version:\s*)[^\r\n]+"),
            replacement=rf"\g<1>{version}",
            label="Helm chart version",
        ),
        Replacement(
            path=Path("deploy/helm/Chart.yaml"),
            pattern=re.compile(r'(?m)^(appVersion:\s*)"[^\r\n"]+"'),
            replacement=rf'\g<1>"{version}"',
            label="Helm appVersion",
        ),
        Replacement(
            path=Path("deploy/helm/values.yaml"),
            pattern=re.compile(
                r"(?ms)^(image:\r?\n(?:[ \t]+[^\r\n]*\r?\n)*?[ \t]+tag:\s*)[^\r\n#]+"
            ),
            replacement=rf"\g<1>{version}",
            label="Helm image tag",
        ),
        Replacement(
            path=Path("deploy/kubernetes/deployment.yaml"),
            pattern=re.compile(r"ghcr\.io/esatt10/syncsage:[0-9A-Za-z._+-]+"),
            replacement=image,
            label="Kubernetes deployment image",
        ),
        Replacement(
            path=Path("docker-compose.yml"),
            pattern=re.compile(r"ghcr\.io/esatt10/syncsage:[0-9A-Za-z._+-]+"),
            replacement=image,
            label="Docker Compose default image",
        ),
        Replacement(
            path=Path("syncsage.example.yaml"),
            pattern=re.compile(
                r"(?ms)^(deployment:\r?\n(?:[ \t]+[^\r\n]*\r?\n)*?[ \t]+image_tag:\s*)[^\r\n#]+"
            ),
            replacement=rf"\g<1>{version}",
            label="example compose image tag",
        ),
    ]


def sync_generated_versions(version: str, write: bool) -> list[str]:
    validate_semver(version)
    mismatches: list[str] = []
    writes: dict[Path, str] = {}
    for replacement in replacements(version):
        path = ROOT / replacement.path
        original = writes.get(path, path.read_text(encoding="utf-8"))
        updated, changed = replace_once(original, replacement)
        writes[path] = updated
        if changed:
            mismatches.append(str(replacement.path))

    if write:
        for path, text in writes.items():
            if path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
        return []
    return sorted(set(mismatches))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep generated SyncSage version references aligned."
    )
    parser.add_argument(
        "--print", dest="print_version", action="store_true", help="Print pyproject version."
    )
    parser.add_argument("--check", action="store_true", help="Check generated version references.")
    parser.add_argument(
        "--write", action="store_true", help="Refresh generated version references."
    )
    parser.add_argument(
        "--bump",
        choices=("major", "minor", "patch"),
        help="Bump pyproject version and refresh generated references.",
    )
    parser.add_argument(
        "--set", dest="set_version", help="Set pyproject version and refresh generated references."
    )
    args = parser.parse_args(argv)

    version = project_version()
    if args.bump and args.set_version:
        raise SystemExit("Use either --bump or --set, not both")
    if args.bump:
        version = bump(version, args.bump)
        set_pyproject_version(version)
    if args.set_version:
        validate_semver(args.set_version)
        version = args.set_version
        set_pyproject_version(version)

    if args.print_version:
        print(version)

    should_write = args.write or bool(args.bump or args.set_version)
    mismatches = sync_generated_versions(version, write=should_write)
    if mismatches:
        print("Version references are not aligned with pyproject.toml:", file=sys.stderr)
        for path in mismatches:
            print(f"  - {path}", file=sys.stderr)
        print("Run: python scripts/sync_version.py --write", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
