from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)
STABLE_SEMVER_TAG_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)$"
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


def stable_semver_tuple(version: str) -> tuple[int, int, int]:
    match = STABLE_SEMVER_TAG_RE.fullmatch(version)
    if not match or version.startswith("v"):
        raise SystemExit(
            "Published container versions must use stable MAJOR.MINOR.PATCH semver "
            f"from pyproject.toml; got {version}"
        )
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))


def parse_stable_semver_tag(tag: str) -> tuple[int, int, int] | None:
    match = STABLE_SEMVER_TAG_RE.fullmatch(tag)
    if not match:
        return None
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))


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
    image = f"ghcr.io/esatt10/pheasant:{version}"
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
            pattern=re.compile(r"ghcr\.io/esatt10/pheasant:[0-9A-Za-z._+-]+"),
            replacement=image,
            label="Kubernetes deployment image",
        ),
        Replacement(
            path=Path("docker-compose.yml"),
            pattern=re.compile(r"ghcr\.io/esatt10/pheasant:[0-9A-Za-z._+-]+"),
            replacement=image,
            label="Docker Compose default image",
        ),
        # The UI sidecar is published from the same commit under the same
        # version tag, so it moves with the backend rather than drifting on a
        # hand-edited tag of its own.
        Replacement(
            path=Path("docker-compose.yml"),
            pattern=re.compile(r"ghcr\.io/esatt10/pheasant-ui:[0-9A-Za-z._+-]+"),
            replacement=f"ghcr.io/esatt10/pheasant-ui:{version}",
            label="Docker Compose UI image",
        ),
        Replacement(
            path=Path("pheasant.example.yaml"),
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


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        url_part, _, rel_part = part.partition(";")
        if 'rel="next"' in rel_part:
            return url_part.strip()[1:-1]
    return None


def _github_api_pages(url: str, token: str | None) -> list[dict]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    results: list[dict] = []
    next_url: str | None = url
    while next_url:
        request = Request(next_url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                page = json.loads(response.read().decode("utf-8"))
                if not isinstance(page, list):
                    raise SystemExit(f"GitHub API returned an unexpected response for {next_url}")
                results.extend(page)
                next_url = _next_link(response.headers.get("Link"))
        except HTTPError:
            raise
        except URLError as exc:
            raise SystemExit(f"Could not query GitHub Packages: {exc}") from exc
    return results


def fetch_container_versions(
    owner: str,
    package_name: str,
    token: str | None,
    api_url: str,
) -> list[dict]:
    encoded_owner = quote(owner, safe="")
    encoded_package = quote(package_name, safe="")
    base = api_url.rstrip("/")
    errors: list[str] = []
    auth_errors: list[str] = []

    for owner_scope in ("orgs", "users"):
        url = (
            f"{base}/{owner_scope}/{encoded_owner}/packages/container/"
            f"{encoded_package}/versions?per_page=100"
        )
        try:
            return _github_api_pages(url, token)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                errors.append(f"{owner_scope}: 404")
                continue
            if exc.code in {401, 403}:
                auth_errors.append(f"{owner_scope}: HTTP {exc.code} {detail}")
                continue
            raise SystemExit(
                f"GitHub Packages query failed for {owner_scope}/{owner}: HTTP {exc.code} {detail}"
            ) from exc

    if auth_errors:
        raise SystemExit(
            "GitHub Packages query requires a token with package read access:\n"
            + "\n".join(auth_errors)
        )

    print(
        f"No existing GHCR package found for {owner}/{package_name}; "
        f"treating {project_version()} as the first published version.",
        file=sys.stderr,
    )
    if errors:
        print("Checked package scopes: " + ", ".join(errors), file=sys.stderr)
    return []


def container_tags(package_versions: list[dict]) -> set[str]:
    tags: set[str] = set()
    for version in package_versions:
        metadata = version.get("metadata", {})
        container = metadata.get("container", {})
        for tag in container.get("tags", []) or []:
            tags.add(str(tag))
    return tags


def check_image_version_increment(version: str, existing_tags: set[str]) -> None:
    current = stable_semver_tuple(version)
    if version in existing_tags or f"v{version}" in existing_tags:
        raise SystemExit(f"Container image tag already exists for version {version}")

    semver_tags = {
        tag: parsed for tag in existing_tags if (parsed := parse_stable_semver_tag(tag)) is not None
    }
    if not semver_tags:
        print(f"No prior stable semver image tags found; accepting {version}.")
        return

    max_tag, max_version = max(semver_tags.items(), key=lambda item: item[1])
    if current <= max_version:
        raise SystemExit(
            f"pyproject.toml version {version} must be greater than the highest "
            f"published image semver tag {max_tag}"
        )

    print(f"Release image version accepted: {version} > {max_tag}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep generated pheasant version references aligned."
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
    parser.add_argument(
        "--check-ghcr-tags",
        action="store_true",
        help=(
            "Require pyproject version to be a new image tag greater than existing "
            "GHCR semver tags."
        ),
    )
    parser.add_argument("--owner", help="GitHub owner that owns the container package.")
    parser.add_argument("--package", default="pheasant", help="GitHub container package name.")
    parser.add_argument("--token", help="GitHub token for package tag lookup.")
    parser.add_argument("--api-url", default="https://api.github.com", help="GitHub API base URL.")
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

    if args.check_ghcr_tags:
        if not args.owner:
            raise SystemExit("--owner is required with --check-ghcr-tags")
        versions = fetch_container_versions(args.owner, args.package, args.token, args.api_url)
        check_image_version_increment(version, container_tags(versions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
