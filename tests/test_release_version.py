from __future__ import annotations

from scripts.release_version import normalize_bump_selection, release_options, selected_release_bump


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


def test_release_options_are_incremented_from_highest_existing_tag() -> None:
    assert release_options({"1.2.3", "latest", "sha-deadbeef"}) == {
        "major": "2.0.0",
        "minor": "1.3.0",
        "patch": "1.2.4",
    }
