"""Acceptance tests for idempotent source synchronization."""

from __future__ import annotations

from tests.conftest import run_sync, sync_result_counts


def test_full_sync_is_idempotent_for_sample_repository(sync_engine: object) -> None:
    """Running a full sync twice should not duplicate graph/search artifacts."""

    first_result = run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    first_counts = sync_result_counts(first_result, sync_engine)

    second_result = run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    second_counts = sync_result_counts(second_result, sync_engine)

    assert second_counts == first_counts
    assert all(value > 0 for value in second_counts.values())
