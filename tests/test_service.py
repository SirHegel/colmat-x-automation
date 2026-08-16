from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from colmat_x.domain import PostStatus
from colmat_x.service import Outcome, run_due_posts
from colmat_x.state import StateStore
from colmat_x.x_api import AmbiguousPublishError, XApiError, XPostResponse
from tests.factories import make_post

NOW = datetime(2026, 8, 15, 15, 0, tzinfo=UTC)


class FakePublisher:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def create_post(self, text: str) -> XPostResponse:
        self.calls.append(text)
        if self.error:
            raise self.error
        return XPostResponse(id="190000000000000010", text=text)


def seeded_store(project_settings) -> StateStore:
    store = StateStore(project_settings.paths.state_db)
    post = make_post()
    store.sync_posts([post], now=NOW)
    store.approve(post.id, "Equipo", post.approval_snapshot_hash, now=NOW)
    return store


def test_dry_run_never_calls_publisher_or_changes_state(project_settings) -> None:
    store = seeded_store(project_settings)
    publisher = FakePublisher()

    results = run_due_posts(store, project_settings, live=False, publisher=publisher, now=NOW)

    assert results[0].outcome == Outcome.DRY_RUN
    assert publisher.calls == []
    assert store.list_posts()[0].status == PostStatus.SCHEDULED


def test_live_run_publishes_and_records_x_id(project_settings) -> None:
    store = seeded_store(project_settings)
    publisher = FakePublisher()

    results = run_due_posts(store, project_settings, live=True, publisher=publisher, now=NOW)

    assert results[0].outcome == Outcome.PUBLISHED
    assert len(publisher.calls) == 1
    assert store.list_posts()[0].x_post_id == "190000000000000010"


def test_ambiguous_result_is_never_retried_automatically(project_settings) -> None:
    store = seeded_store(project_settings)
    publisher = FakePublisher(AmbiguousPublishError("timeout ambiguo"))

    results = run_due_posts(store, project_settings, live=True, publisher=publisher, now=NOW)

    assert results[0].outcome == Outcome.UNKNOWN
    assert store.list_posts()[0].status == PostStatus.UNKNOWN
    again = run_due_posts(
        store,
        project_settings,
        live=True,
        publisher=publisher,
        now=NOW + timedelta(minutes=1),
    )
    assert again[0].outcome == Outcome.RECONCILIATION_REQUIRED
    assert len(publisher.calls) == 1


def test_definite_api_error_is_recorded_as_failed(project_settings) -> None:
    store = seeded_store(project_settings)
    publisher = FakePublisher(XApiError("401: credenciales inválidas"))

    results = run_due_posts(store, project_settings, live=True, publisher=publisher, now=NOW)

    assert results[0].outcome == Outcome.FAILED
    assert store.list_posts()[0].status == PostStatus.FAILED


def test_daily_limit_is_reserved_atomically_before_a_second_publish(project_settings) -> None:
    limited_settings = replace(
        project_settings,
        safety=replace(
            project_settings.safety,
            max_posts_per_run=2,
            max_posts_per_day=1,
        ),
    )
    store = StateStore(project_settings.paths.state_db)
    first = make_post()
    second = make_post(post_id="colmat-test-002", text="Segunda publicación del día")
    store.sync_posts([first, second], now=NOW)
    store.approve(first.id, "Equipo", first.approval_snapshot_hash, now=NOW)
    store.approve(second.id, "Equipo", second.approval_snapshot_hash, now=NOW)
    publisher = FakePublisher()

    results = run_due_posts(
        store,
        limited_settings,
        live=True,
        publisher=publisher,
        now=NOW,
    )

    assert [result.outcome for result in results] == [Outcome.PUBLISHED, Outcome.DAILY_LIMIT]
    assert len(publisher.calls) == 1
    assert len(store.list_posts(PostStatus.PUBLISHED)) == 1
    assert len(store.list_posts(PostStatus.SCHEDULED)) == 1
