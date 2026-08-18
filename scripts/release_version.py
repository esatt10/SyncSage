from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from sync_version import (
        bump,
        container_tags,
        fetch_container_versions,
        parse_stable_semver_tag,
        project_version,
        stable_semver_tuple,
    )
except ModuleNotFoundError:
    from scripts.sync_version import (
        bump,
        container_tags,
        fetch_container_versions,
        parse_stable_semver_tag,
        project_version,
        stable_semver_tuple,
    )

BOT_COMMENT_MARKER = "<!-- pheasant-release-version-selection -->"
STATUS_CONTEXT = "Release version selection"
BUMP_ORDER = ("major", "minor", "patch")
DEFAULT_BUMP = "patch"
BUMP_ALIASES = {
    "1": "major",
    "2": "minor",
    "3": "patch",
    "major": "major",
    "minor": "minor",
    "patch": "patch",
}


def github_api(
    method: str,
    url: str,
    token: str,
    payload: Mapping[str, Any] | None = None,
) -> Any:
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"GitHub API request failed: {method} {url} HTTP {exc.code} {detail}"
        ) from exc
    except URLError as exc:
        raise SystemExit(f"GitHub API request failed: {method} {url}: {exc}") from exc


def next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        url_part, _, rel_part = part.partition(";")
        if 'rel="next"' in rel_part:
            return url_part.strip()[1:-1]
    return None


def github_api_pages(url: str, token: str) -> list[dict[str, Any]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        request = Request(next_url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                page = json.loads(response.read().decode("utf-8"))
                if not isinstance(page, list):
                    raise SystemExit(f"GitHub API returned an unexpected response for {next_url}")
                results.extend(page)
                next_url = next_link(response.headers.get("Link"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SystemExit(
                f"GitHub API request failed: GET {next_url} HTTP {exc.code} {detail}"
            ) from exc
        except URLError as exc:
            raise SystemExit(f"GitHub API request failed: GET {next_url}: {exc}") from exc
    return results


def normalize_bump_selection(body: str) -> str | None:
    lines = [line.strip().lower() for line in body.splitlines() if line.strip()]
    if not lines:
        return None
    candidates = [lines[0], body.strip().lower()]
    for candidate in candidates:
        cleaned = candidate.strip("`*_ \t\r\n.")
        if cleaned in BUMP_ALIASES:
            return BUMP_ALIASES[cleaned]
        match = re.fullmatch(
            r"(?:/)?(?:release|semver|bump)\s*(?::|=|\s)\s*(major|minor|patch|1|2|3)",
            cleaned,
        )
        if match:
            return BUMP_ALIASES[match.group(1)]
    return None


def latest_published_version(tags: set[str]) -> tuple[int, int, int] | None:
    versions = [parsed for tag in tags if (parsed := parse_stable_semver_tag(tag)) is not None]
    return max(versions) if versions else None


def tuple_to_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def release_base_version(tags: set[str]) -> str:
    current = stable_semver_tuple(project_version())
    published = latest_published_version(tags)
    base = max(current, published) if published else current
    return tuple_to_version(base)


def release_options(tags: set[str]) -> dict[str, str]:
    base = release_base_version(tags)
    options: dict[str, str] = {}
    for part in BUMP_ORDER:
        version = bump(base, part)
        if version not in tags and f"v{version}" not in tags:
            options[part] = version
    return options


def selected_release_bump(comments: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    for comment in reversed(comments):
        user = comment.get("user") or {}
        if user.get("type") == "Bot":
            continue
        body = str(comment.get("body") or "")
        selection = normalize_bump_selection(body)
        if selection:
            return selection, comment
    return None


def effective_release_bump(
    comments: list[dict[str, Any]],
    options: dict[str, str],
) -> tuple[str | None, bool]:
    selected = selected_release_bump(comments)
    if selected:
        return selected[0], False
    if DEFAULT_BUMP in options:
        return DEFAULT_BUMP, True
    return None, True


def pr_number_from_event(event: Mapping[str, Any]) -> int | None:
    if "pull_request" in event:
        return int(event["pull_request"]["number"])
    issue = event.get("issue")
    if isinstance(issue, Mapping) and issue.get("pull_request"):
        return int(issue["number"])
    return None


def get_pull_request(repo: str, number: int, token: str, api_url: str) -> dict[str, Any]:
    return github_api("GET", f"{api_url}/repos/{repo}/pulls/{number}", token)


def comments_for_pr(repo: str, number: int, token: str, api_url: str) -> list[dict[str, Any]]:
    return github_api_pages(f"{api_url}/repos/{repo}/issues/{number}/comments?per_page=100", token)


def update_pr_status(
    repo: str,
    sha: str,
    token: str,
    api_url: str,
    state: str,
    description: str,
) -> None:
    run_id = os.environ.get("GITHUB_RUN_ID")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", repo)
    target_url = f"{server_url}/{repository}/actions/runs/{run_id}" if run_id else None
    payload: dict[str, Any] = {
        "state": state,
        "context": STATUS_CONTEXT,
        "description": description[:140],
    }
    if target_url:
        payload["target_url"] = target_url
    github_api("POST", f"{api_url}/repos/{repo}/statuses/{sha}", token, payload)


def render_prompt(
    options: dict[str, str],
    selection: str | None = None,
    defaulted: bool = False,
) -> str:
    option_lines = "\n".join(
        f"- `{index}` or `{part}` -> `{version}`"
        for index, part in enumerate(BUMP_ORDER, start=1)
        if (version := options.get(part))
    )
    if selection and defaulted:
        selected_line = f"\nCurrent selection: `3` / `patch` (default) -> `{options[selection]}`\n"
    elif selection:
        selected_line = f"\nCurrent selection: `{selection}` -> `{options[selection]}`\n"
    else:
        selected_line = ""
    return (
        f"{BOT_COMMENT_MARKER}\n"
        "Review the release increment for this PR. Patch is selected by default; "
        "comment with one value to override it:\n\n"
        f"{option_lines}\n"
        f"{selected_line}\n"
        "Accepted forms include `patch`, `minor`, `major`, `3`, `2`, `1`, "
        "or `/release patch`. Each new comment rechecks this selection against "
        "the currently valid release options."
    )


def upsert_prompt_comment(
    repo: str,
    number: int,
    comments: list[dict[str, Any]],
    token: str,
    api_url: str,
    options: dict[str, str],
    selection: str | None,
    defaulted: bool = False,
) -> None:
    body = render_prompt(options, selection, defaulted)
    for comment in comments:
        user = comment.get("user") or {}
        if user.get("type") == "Bot" and BOT_COMMENT_MARKER in str(comment.get("body") or ""):
            github_api(
                "PATCH",
                f"{api_url}/repos/{repo}/issues/comments/{comment['id']}",
                token,
                {"body": body},
            )
            return
    github_api("POST", f"{api_url}/repos/{repo}/issues/{number}/comments", token, {"body": body})


def validate_pr_selection(args: argparse.Namespace) -> int:
    event = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
    number = pr_number_from_event(event)
    if number is None:
        print("No pull request found for this event; skipping release selection check.")
        return 0

    pr = get_pull_request(args.repo, number, args.token, args.api_url)
    sha = pr["head"]["sha"]
    comments = comments_for_pr(args.repo, number, args.token, args.api_url)
    tags = container_tags(
        fetch_container_versions(args.owner, args.package, args.token, args.api_url)
    )
    options = release_options(tags)
    if not options:
        update_pr_status(
            args.repo,
            sha,
            args.token,
            args.api_url,
            "failure",
            "No valid semver increment is available",
        )
        print("No valid semver increment is available.")
        return 0

    selection, defaulted = effective_release_bump(comments, options)
    upsert_prompt_comment(
        args.repo,
        number,
        comments,
        args.token,
        args.api_url,
        options,
        selection,
        defaulted,
    )
    if not selection:
        update_pr_status(
            args.repo,
            sha,
            args.token,
            args.api_url,
            "failure",
            "Default patch is unavailable; comment with major, minor, 1, or 2",
        )
        print("Default patch is unavailable and no valid release increment was selected.")
        return 0
    if selection not in options:
        update_pr_status(
            args.repo,
            sha,
            args.token,
            args.api_url,
            "failure",
            f"Selected {selection} increment is not valid",
        )
        print(f"Selected {selection} increment is not valid.")
        return 0

    description = (
        f"Release increment selected: {selection} -> {options[selection]}"
        if not defaulted
        else f"Default release increment: patch -> {options[selection]}"
    )
    update_pr_status(
        args.repo,
        sha,
        args.token,
        args.api_url,
        "success",
        description,
    )
    print(description)
    return 0


def merged_pr_for_commit(repo: str, sha: str, token: str, api_url: str) -> dict[str, Any] | None:
    pulls = github_api(
        "GET",
        f"{api_url}/repos/{repo}/commits/{sha}/pulls",
        token,
    )
    if not isinstance(pulls, list):
        return None
    merged = [pr for pr in pulls if pr.get("merged_at")]
    return merged[0] if merged else (pulls[0] if pulls else None)


def print_merged_pr_bump(args: argparse.Namespace) -> int:
    pr = merged_pr_for_commit(args.repo, args.sha, args.token, args.api_url)
    if pr is None:
        raise SystemExit(f"No pull request is associated with merge commit {args.sha}.")
    number = int(pr["number"])
    comments = comments_for_pr(args.repo, number, args.token, args.api_url)
    tags = container_tags(
        fetch_container_versions(args.owner, args.package, args.token, args.api_url)
    )
    options = release_options(tags)
    bump_part, _defaulted = effective_release_bump(comments, options)
    if not bump_part:
        raise SystemExit(f"PR #{number} does not have a valid release increment.")
    if bump_part not in options:
        raise SystemExit(f"Selected {bump_part} increment is no longer valid for PR #{number}.")
    version = options[bump_part]
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as output:
            output.write(f"bump={bump_part}\n")
            output.write(f"pr={number}\n")
            output.write(f"version={version}\n")
    print(f"Resolved release increment from PR #{number}: {bump_part} -> {version}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PR release increment workflow helpers.")
    sub = parser.add_subparsers(dest="command", required=True)

    pr_gate = sub.add_parser("pr-gate")
    pr_gate.add_argument("--event-path", required=True)
    pr_gate.add_argument("--repo", required=True)
    pr_gate.add_argument("--owner", required=True)
    pr_gate.add_argument("--package", default="pheasant")
    pr_gate.add_argument("--token", required=True)
    pr_gate.add_argument("--api-url", default="https://api.github.com")

    merged = sub.add_parser("merged-pr-bump")
    merged.add_argument("--repo", required=True)
    merged.add_argument("--sha", required=True)
    merged.add_argument("--owner", required=True)
    merged.add_argument("--package", default="pheasant")
    merged.add_argument("--token", required=True)
    merged.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    merged.add_argument("--api-url", default="https://api.github.com")

    args = parser.parse_args(argv)
    if args.command == "pr-gate":
        return validate_pr_selection(args)
    if args.command == "merged-pr-bump":
        return print_merged_pr_bump(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
