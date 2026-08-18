from __future__ import annotations

from scripts.release_version import (
    effective_release_bump,
    normalize_bump_selection,
    release_base_version,
    release_options,
    render_prompt,
    selected_release_bump,
)
from scripts.sync_version import project_version


def test_normalize_bump_selection_accepts_words_and_numbers() -> None:
    assert normalize_bump_selection("major") == "major"
    assert normalize_bump_selection("2") == "minor"
    assert normalize_bump_selection("/release patch") == "patch"
    assert normalize_bump_selection("semver: 3") == "patch"


def test_normalize_bump_selection_rejects_incidental_text() -> None:
    assert normalize_bump_selection("This PR includes a patch for docs.") is None


def test_selected_release_bump_ignores_bot_prompt() -> None:
    comments = [
        {"user": {"type": "Bot"}, "body": "patch"},
        {"user": {"type": "User"}, "body": "minor"},
    ]

    assert selected_release_bump(comments)[0] == "minor"  # type: ignore[index]


def test_effective_release_bump_defaults_to_patch() -> None:
    selection, defaulted = effective_release_bump([], {"patch": "1.2.4", "minor": "1.3.0"})

    assert selection == "patch"
    assert defaulted is True


def test_effective_release_bump_comment_overrides_patch_default() -> None:
    comments = [{"user": {"type": "User"}, "body": "2"}]
    selection, defaulted = effective_release_bump(
        comments,
        {"patch": "1.2.4", "minor": "1.3.0"},
    )

    assert selection == "minor"
    assert defaulted is False


def test_render_prompt_discloses_patch_default() -> None:
    rendered = render_prompt({"patch": "1.2.4"}, "patch", defaulted=True)

    assert "Patch is selected by default" in rendered
    assert "`3` / `patch` (default) -> `1.2.4`" in rendered


def test_release_options_are_incremented_from_highest_existing_tag() -> None:
    assert release_options({"1.2.3", "latest", "sha-deadbeef"}) == {
        "major": "2.0.0",
        "minor": "1.3.0",
        "patch": "1.2.4",
    }


# --------------------------------------------------------------------------
# The increment is anchored to whichever is further ahead
# --------------------------------------------------------------------------


def test_the_base_version_follows_the_registry_when_it_is_ahead() -> None:
    """A published image main never recorded must not be handed out twice.

    This is the direction that happens when the release commit fails after the
    images are pushed — the order the publish workflow deliberately runs in.
    """

    ahead = _bumped_major(project_version())
    assert release_base_version({ahead, "latest"}) == ahead
    assert ahead not in set(release_options({ahead, "latest"}).values())


def test_the_base_version_follows_pyproject_when_it_is_ahead() -> None:
    """And this is the direction a partially-failed older release left behind.

    pyproject ahead of the registry means a version was recorded but never
    published; the next release must step past it rather than reissue it.
    """

    behind = "0.0.1"
    assert release_base_version({behind, "latest"}) == project_version()


def test_the_first_release_of_a_package_has_no_published_tags() -> None:
    assert release_base_version(set()) == project_version()
    assert release_options(set())["patch"] != project_version()


def _bumped_major(version: str) -> str:
    major, _, _ = version.split(".")
    return f"{int(major) + 1}.0.0"
