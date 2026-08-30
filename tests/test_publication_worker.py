from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import select  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from colmat_x.automation import AutomationError  # noqa: E402
from colmat_x.cli import app  # noqa: E402
from colmat_x.platform_store import (  # noqa: E402
    AutomationRunStatus,
    DraftStatus,
    PlatformStore,
    PublicationRequestStatus,
    PublishAttempt,
    PublishStatus,
)
from colmat_x.publication_worker import (  # noqa: E402
    EXPECTED_X_USER_ID_ENV,
    EXPECTED_X_USERNAME_ENV,
    LIVE_PUBLISH_ENV,
    QueuedPublicationWorker,
    QueuePublicationStatus,
)
from colmat_x.rbac import Role  # noqa: E402
from colmat_x.x_api import AmbiguousPublishError  # noqa: E402

NOW = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
PUBLISH_AT = NOW
POST_ID = "190000000000000001"


@pytest.fixture
def store() -> PlatformStore:
    selected = PlatformStore("sqlite+pysqlite:///:memory:")
    try:
        yield selected
    finally:
        selected.close()


@dataclass(frozen=True)
class Team:
    owner_id: str
    scheduler_id: str
    editor_id: str
    reviewer_id: str
    publisher_id: str


class FakeX:
    def __init__(self, *, post_error: Exception | None = None, after_upload=None) -> None:
        self.post_error = post_error
        self.after_upload = after_upload
        self.identity_calls: list[dict[str, object]] = []
        self.upload_calls: list[tuple[bytes, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []
        self.events: list[str] = []

    def verify_identity(self, **kwargs):
        self.events.append("identity")
        self.identity_calls.append(kwargs)
        return SimpleNamespace(id="123456789", username="colmat", name="Colmat")

    def upload_image(self, content: bytes, **kwargs):
        self.events.append("upload")
        self.upload_calls.append((content, kwargs))
        if self.after_upload is not None:
            self.after_upload()
        return SimpleNamespace(id="123456789")

    def create_post(self, text: str, **kwargs):
        self.events.append("post")
        self.post_calls.append((text, kwargs))
        if self.post_error is not None:
            raise self.post_error
        return SimpleNamespace(id=POST_ID, text=text)


def _add_member(store: PlatformStore, owner_id: str, role: Role, ordinal: int):
    user = store.create_user(
        actor_id=owner_id,
        email=f"{role.value}-{ordinal}@worker.test",
        display_name=f"{role.value.title()} {ordinal}",
        now=NOW,
    )
    store.grant_membership(user.id, role, actor_id=owner_id, now=NOW)
    return user


def _bootstrap_team(store: PlatformStore) -> Team:
    owner, _membership = store.bootstrap_owner(
        email="owner@worker.test",
        display_name="Owner",
        now=NOW,
    )
    scheduler = _add_member(store, owner.id, Role.SCHEDULER, 1)
    editor = _add_member(store, owner.id, Role.EDITOR, 1)
    reviewer = _add_member(store, owner.id, Role.REVIEWER, 1)
    publisher = _add_member(store, owner.id, Role.PUBLISHER, 1)
    return Team(owner.id, scheduler.id, editor.id, reviewer.id, publisher.id)


def _slot(*, generate_image: bool) -> dict[str, object]:
    return {
        "id": "worker-slot",
        "at": "10:00",
        "mode": "human_review",
        "category": "dato_semana",
        "institution": "colmat",
        "brief": "Explica una cifra territorial usando una fuente primaria verificable.",
        "generate_image": generate_image,
        "evidence": {
            "verified": False,
            "reference": None,
            "expected_figure": None,
            "expected_source": None,
        },
    }


def _prepare_request(
    store: PlatformStore,
    team: Team,
    *,
    image_sha256: str | None = None,
    linked_run: bool = False,
    enqueue_now: datetime = NOW,
    publish_at: datetime = PUBLISH_AT,
):
    slot = _slot(generate_image=image_sha256 is not None)
    if linked_run:
        store.update_automation_settings(
            actor_id=team.owner_id,
            expected_version=1,
            enabled=True,
            slots=[slot],
            max_posts_per_day=2,
            now=NOW,
        )
    draft, revision = store.create_draft(
        actor_id=team.editor_id,
        text="Bogotá transforma datos públicos en mejores decisiones territoriales.",
        category="dato_semana",
        publish_at=publish_at,
        evidence={"manual": "capitulo-2", "fuente": "DANE"},
        image_sha256=image_sha256,
        now=NOW,
    )
    store.submit_for_review(
        draft.id,
        actor_id=team.editor_id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    run_id = None
    if linked_run:
        run = store.claim_automation_run(
            actor_id=team.scheduler_id,
            idempotency_key="colmat:auto:v1:2026-08-29:worker-slot",
            slot_id="worker-slot",
            scheduled_for=NOW,
            slot_snapshot=slot,
            draft_id=draft.id,
            now=NOW,
        )
        store.finish_automation_run(
            run.id,
            AutomationRunStatus.AWAITING_REVIEW,
            actor_id=team.scheduler_id,
            draft_id=draft.id,
            now=NOW,
        )
        run_id = run.id
    store.approve_draft(
        draft.id,
        actor_id=team.reviewer_id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    request = store.enqueue_publication_request(
        draft.id,
        actor_id=team.publisher_id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key=f"telegram:{draft.id}:publicar",
        now=enqueue_now,
    )
    return draft, revision, request, run_id


def _register_media(
    store: PlatformStore,
    team: Team,
    *,
    digest: str,
    path: Path,
    byte_size: int,
    filename: str = "colmat-card.png",
) -> None:
    store.register_media_asset(
        actor_id=team.editor_id,
        kind="image",
        url=path.as_uri(),
        sha256=digest,
        mime_type="image/png",
        byte_size=byte_size,
        metadata={"alt_text": "Dato territorial de Colmat con fuente DANE.", "filename": filename},
        now=NOW,
    )


def _worker(
    store: PlatformStore,
    team: Team,
    x_client: FakeX,
    media_root: Path,
    environ: dict[str, str] | None = None,
    now: datetime = NOW,
) -> QueuedPublicationWorker:
    worker_environ = {} if environ is None else environ
    worker_environ.setdefault(LIVE_PUBLISH_ENV, "true")
    worker_environ.setdefault(EXPECTED_X_USER_ID_ENV, "123456789")
    worker_environ.setdefault(EXPECTED_X_USERNAME_ENV, "colmat")
    return QueuedPublicationWorker(
        store=store,
        x_client=x_client,
        publisher_actor_id=team.publisher_id,
        scheduler_actor_id=team.scheduler_id,
        media_root=media_root,
        environ=worker_environ,
        clock=lambda: now,
    )


def _only_attempt(store: PlatformStore) -> PublishAttempt:
    with store.session() as session:
        attempts = list(session.scalars(select(PublishAttempt)))
    assert len(attempts) == 1
    return attempts[0]


def test_worker_requires_distinct_scheduler_and_publisher(store, tmp_path: Path) -> None:
    team = _bootstrap_team(store)

    with pytest.raises(ValueError, match="deben ser distintos"):
        QueuedPublicationWorker(
            store=store,
            x_client=FakeX(),
            publisher_actor_id=team.publisher_id,
            scheduler_actor_id=team.publisher_id,
            media_root=tmp_path,
        )


def test_live_gate_is_checked_before_claiming_any_request(store, tmp_path: Path) -> None:
    team = _bootstrap_team(store)
    _draft, _revision, request, _run_id = _prepare_request(store, team)
    x_client = FakeX()

    with pytest.raises(AutomationError, match=LIVE_PUBLISH_ENV):
        _worker(
            store,
            team,
            x_client,
            tmp_path,
            environ={LIVE_PUBLISH_ENV: "false"},
        ).run()

    persisted = store.get_publication_request(request.id, actor_id=team.publisher_id)
    assert persisted.status_value is PublicationRequestStatus.QUEUED
    assert x_client.upload_calls == []
    assert x_client.post_calls == []
    with store.session() as session:
        assert list(session.scalars(select(PublishAttempt))) == []


def test_empty_queue_returns_before_verifying_x_identity(store, tmp_path: Path) -> None:
    team = _bootstrap_team(store)
    x_client = FakeX()

    assert _worker(store, team, x_client, tmp_path).run() == ()

    assert x_client.identity_calls == []
    assert x_client.upload_calls == []
    assert x_client.post_calls == []


def test_future_request_stays_queued_without_verifying_x_identity(store, tmp_path: Path) -> None:
    team = _bootstrap_team(store)
    publish_at = NOW + timedelta(hours=2)
    _draft, _revision, request, _run_id = _prepare_request(
        store,
        team,
        publish_at=publish_at,
    )
    x_client = FakeX()

    assert _worker(store, team, x_client, tmp_path, now=NOW).run() == ()

    persisted = store.get_publication_request(request.id, actor_id=team.publisher_id)
    assert persisted.status_value is PublicationRequestStatus.QUEUED
    assert persisted.claim_fence == 0
    assert x_client.identity_calls == []
    assert x_client.post_calls == []

    due = _worker(store, team, x_client, tmp_path, now=publish_at).run()
    assert due[0].status is QueuePublicationStatus.SUCCEEDED
    assert len(x_client.identity_calls) == 1
    assert len(x_client.post_calls) == 1


def test_worker_publishes_approved_media_once_and_closes_every_receipt(
    store, tmp_path: Path
) -> None:
    team = _bootstrap_team(store)
    media_root = tmp_path / "media"
    media_root.mkdir()
    content = b"verified-colmat-png"
    digest = hashlib.sha256(content).hexdigest()
    image_path = media_root / f"{digest}.png"
    image_path.write_bytes(content)
    draft, revision, request, _run_id = _prepare_request(
        store,
        team,
        image_sha256=digest,
    )
    _register_media(
        store,
        team,
        digest=digest,
        path=image_path,
        byte_size=len(content),
    )
    x_client = FakeX()
    worker = _worker(store, team, x_client, media_root)

    results = worker.run()

    assert len(results) == 1
    assert results[0].status is QueuePublicationStatus.SUCCEEDED
    assert results[0].provider_post_id == POST_ID
    assert x_client.upload_calls == [
        (
            content,
            {
                "filename": "colmat-card.png",
                "mime_type": "image/png",
                "alt_text": "Dato territorial de Colmat con fuente DANE.",
            },
        )
    ]
    assert x_client.post_calls == [
        (revision.text, {"media_ids": ["123456789"], "made_with_ai": True})
    ]
    assert x_client.identity_calls == [
        {
            "expected_user_id": "123456789",
            "expected_username": "colmat",
        }
    ]
    assert x_client.events == ["identity", "upload", "post"]
    assert store.get_draft(draft.id, actor_id=team.owner_id).status_value is DraftStatus.PUBLISHED
    persisted = store.get_publication_request(request.id, actor_id=team.publisher_id)
    assert persisted.status_value is PublicationRequestStatus.SUCCEEDED
    attempt = _only_attempt(store)
    assert attempt.status_value is PublishStatus.SUCCEEDED
    assert attempt.provider_post_id == POST_ID
    assert persisted.publish_attempt_id == attempt.id

    assert worker.run() == ()
    assert len(x_client.post_calls) == 1


def test_worker_rechecks_gate_after_upload_and_never_contacts_post_endpoint(
    store, tmp_path: Path
) -> None:
    team = _bootstrap_team(store)
    media_root = tmp_path / "media"
    media_root.mkdir()
    content = b"gate-revocation-image"
    digest = hashlib.sha256(content).hexdigest()
    image_path = media_root / f"{digest}.png"
    image_path.write_bytes(content)
    _draft, _revision, request, run_id = _prepare_request(
        store,
        team,
        image_sha256=digest,
        linked_run=True,
    )
    _register_media(
        store,
        team,
        digest=digest,
        path=image_path,
        byte_size=len(content),
    )
    environ = {LIVE_PUBLISH_ENV: "true"}
    x_client = FakeX(after_upload=lambda: environ.pop(LIVE_PUBLISH_ENV))

    results = _worker(store, team, x_client, media_root, environ=environ).run()

    assert results[0].status is QueuePublicationStatus.FAILED
    assert len(x_client.upload_calls) == 1
    assert x_client.post_calls == []
    assert _only_attempt(store).status_value is PublishStatus.FAILED
    assert (
        store.get_publication_request(request.id, actor_id=team.publisher_id).status_value
        is PublicationRequestStatus.FAILED
    )
    assert run_id is not None
    linked = next(
        run for run in store.list_automation_runs(actor_id=team.scheduler_id) if run.id == run_id
    )
    assert linked.status_value is AutomationRunStatus.FAILED


def test_gate_revocation_does_not_claim_the_next_queued_request(store, tmp_path: Path) -> None:
    team = _bootstrap_team(store)
    media_root = tmp_path / "media"
    media_root.mkdir()
    content = b"first-request-media"
    digest = hashlib.sha256(content).hexdigest()
    image_path = media_root / f"{digest}.png"
    image_path.write_bytes(content)
    _first_draft, _revision, first_request, _run_id = _prepare_request(
        store,
        team,
        image_sha256=digest,
    )
    _register_media(
        store,
        team,
        digest=digest,
        path=image_path,
        byte_size=len(content),
    )
    _second_draft, _revision, second_request, _run_id = _prepare_request(
        store,
        team,
        enqueue_now=NOW + timedelta(seconds=1),
    )
    environ = {LIVE_PUBLISH_ENV: "true"}
    x_client = FakeX(after_upload=lambda: environ.pop(LIVE_PUBLISH_ENV))

    results = _worker(
        store,
        team,
        x_client,
        media_root,
        environ=environ,
        now=NOW + timedelta(seconds=2),
    ).run()

    assert [result.request_id for result in results] == [first_request.id]
    assert results[0].status is QueuePublicationStatus.FAILED
    assert (
        store.get_publication_request(second_request.id, actor_id=team.publisher_id).status_value
        is PublicationRequestStatus.QUEUED
    )
    assert len(x_client.upload_calls) == 1
    assert x_client.post_calls == []


def test_ambiguous_x_response_becomes_unknown_and_is_never_retried(store, tmp_path: Path) -> None:
    team = _bootstrap_team(store)
    _draft, _revision, request, run_id = _prepare_request(store, team, linked_run=True)
    x_client = FakeX(post_error=AmbiguousPublishError("token=must-not-leak"))
    worker = _worker(store, team, x_client, tmp_path)

    first = worker.run()
    second = worker.run()

    assert first[0].status is QueuePublicationStatus.UNKNOWN
    assert second == ()
    assert len(x_client.post_calls) == 1
    attempt = _only_attempt(store)
    assert attempt.status_value is PublishStatus.UNKNOWN
    assert "must-not-leak" not in (attempt.error or "")
    persisted = store.get_publication_request(request.id, actor_id=team.publisher_id)
    assert persisted.status_value is PublicationRequestStatus.UNKNOWN
    assert persisted.publish_attempt_id == attempt.id
    assert run_id is not None
    linked = next(
        run for run in store.list_automation_runs(actor_id=team.scheduler_id) if run.id == run_id
    )
    assert linked.status_value is AutomationRunStatus.UNKNOWN


@pytest.mark.parametrize("unsafe_case", ["outside-root", "hash-mismatch", "symlink", "filename"])
def test_worker_rejects_untrusted_media_without_uploading_or_posting(
    store,
    tmp_path: Path,
    unsafe_case: str,
) -> None:
    team = _bootstrap_team(store)
    media_root = tmp_path / "media"
    media_root.mkdir()
    approved_content = b"approved-content"
    digest = hashlib.sha256(approved_content).hexdigest()
    filename = "colmat-card.png"
    if unsafe_case == "outside-root":
        image_path = tmp_path / "outside.png"
        image_path.write_bytes(approved_content)
    elif unsafe_case == "hash-mismatch":
        image_path = media_root / "tampered.png"
        image_path.write_bytes(b"different-content")
    elif unsafe_case == "symlink":
        target = media_root / "target.png"
        target.write_bytes(approved_content)
        image_path = media_root / "linked.png"
        image_path.symlink_to(target)
    else:
        image_path = media_root / "safe.png"
        image_path.write_bytes(approved_content)
        filename = "../unsafe.png"
    _draft, _revision, request, _run_id = _prepare_request(
        store,
        team,
        image_sha256=digest,
    )
    _register_media(
        store,
        team,
        digest=digest,
        path=image_path,
        byte_size=(
            len(b"different-content") if unsafe_case == "hash-mismatch" else len(approved_content)
        ),
        filename=filename,
    )
    x_client = FakeX()

    result = _worker(store, team, x_client, media_root).run()

    assert result[0].status is QueuePublicationStatus.FAILED
    assert x_client.upload_calls == []
    assert x_client.post_calls == []
    assert _only_attempt(store).status_value is PublishStatus.FAILED
    assert (
        store.get_publication_request(request.id, actor_id=team.publisher_id).status_value
        is PublicationRequestStatus.FAILED
    )


def test_successful_request_advances_linked_automation_run_to_succeeded(
    store, tmp_path: Path
) -> None:
    team = _bootstrap_team(store)
    _draft, _revision, _request, run_id = _prepare_request(store, team, linked_run=True)
    assert run_id is not None
    before = next(
        run for run in store.list_automation_runs(actor_id=team.scheduler_id) if run.id == run_id
    )
    assert before.status_value is AutomationRunStatus.READY

    result = _worker(store, team, FakeX(), tmp_path).run()

    assert result[0].status is QueuePublicationStatus.SUCCEEDED
    after = next(
        run for run in store.list_automation_runs(actor_id=team.scheduler_id) if run.id == run_id
    )
    assert after.status_value is AutomationRunStatus.SUCCEEDED
    assert after.finished_by == team.scheduler_id


def test_publication_run_cli_requires_both_explicit_live_controls(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setenv(LIVE_PUBLISH_ENV, "false")

    missing_option = runner.invoke(app, ["publication-run"])
    missing_environment = runner.invoke(app, ["publication-run", "--live"])

    assert missing_option.exit_code == 1
    assert "exige la opción --live" in missing_option.output
    assert missing_environment.exit_code == 1
    assert f"{LIVE_PUBLISH_ENV}=true" in missing_environment.output


def test_publication_run_cli_defers_identity_verification_to_worker(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    database_url = f"sqlite+pysqlite:///{tmp_path / 'cli-worker.db'}"
    required = {
        LIVE_PUBLISH_ENV: "true",
        "COLMAT_AUTOMATION_SCHEDULER_ID": "scheduler-service",
        "COLMAT_AUTOMATION_PUBLISHER_ID": "publisher-service",
        "EXPECTED_X_USER_ID": "123456789",
        "EXPECTED_X_USERNAME": "colmat",
        "X_CONSUMER_KEY": "consumer-key",
        "X_CONSUMER_SECRET": "consumer-secret",
        "X_ACCESS_TOKEN": "access-token",
        "X_ACCESS_TOKEN_SECRET": "access-token-secret",
    }
    for name, value in required.items():
        monkeypatch.setenv(name, value)
    captured: dict[str, object] = {}

    class FakeIdentityClient:
        def __init__(self, credentials) -> None:
            captured["credentials"] = credentials

        def verify_identity(self, **kwargs) -> None:
            captured["identity"] = kwargs

    class FakeWorker:
        def __init__(self, **kwargs) -> None:
            captured["worker"] = kwargs

        def run(self, *, limit: int):
            captured["limit"] = limit
            return ()

    monkeypatch.setattr("colmat_x.cli.XApiClient", FakeIdentityClient)
    monkeypatch.setattr("colmat_x.cli.QueuedPublicationWorker", FakeWorker)

    result = runner.invoke(
        app,
        [
            "publication-run",
            "--live",
            "--limit",
            "7",
            "--database-url",
            database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"processed": 0, "results": []}
    assert "identity" not in captured
    assert captured["limit"] == 7
    worker_kwargs = captured["worker"]
    assert worker_kwargs["x_client"] is not None
    assert worker_kwargs["scheduler_actor_id"] == "scheduler-service"
    assert worker_kwargs["publisher_actor_id"] == "publisher-service"
