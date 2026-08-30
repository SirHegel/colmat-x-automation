from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import event as sqlalchemy_event  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402

from colmat_x.platform_store import (  # noqa: E402
    Approval,
    AuditEvent,
    AutomationMode,
    AutomationReviewNotification,
    AutomationReviewNotificationStatus,
    AutomationRunStatus,
    Base,
    CallbackAction,
    CallbackIntent,
    ConflictError,
    Draft,
    DraftStatus,
    Membership,
    NotFoundError,
    PlatformStore,
    PublicationRequest,
    PublicationRequestStatus,
    PublishAttempt,
    PublishStatus,
    Revision,
    StaleSnapshotError,
    TelegramUpdate,
    User,
    _automation_revision_engagement_score,
    approval_snapshot_hash,
    resolve_database_url,
    table_names,
)
from colmat_x.rbac import (  # noqa: E402
    AuthorizationError,
    Role,
    SeparationOfDutiesError,
)
from colmat_x.telegram_bot import (  # noqa: E402
    BotPermission,
    CallbackDecision,
    TelegramWebhookProcessor,
    TelegramWebhookSecret,
    approval_callback_data,
)

NOW = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
PUBLISH_AT = NOW
IMAGE_HASH = "a" * 64


@pytest.fixture
def store() -> PlatformStore:
    selected = PlatformStore("sqlite+pysqlite:///:memory:")
    try:
        yield selected
    finally:
        selected.close()


def add_member(store: PlatformStore, owner_id: str, role: Role, ordinal: int):
    user = store.create_user(
        actor_id=owner_id,
        email=f"{role.value}-{ordinal}@colmat.test",
        display_name=f"{role.value.title()} {ordinal}",
        now=NOW,
    )
    store.grant_membership(user.id, role, actor_id=owner_id, now=NOW)
    return user


def bootstrap_team(store: PlatformStore):
    owner, _ = store.bootstrap_owner(
        email="owner@colmat.test",
        display_name="Owner Colmat",
        password_hash="$argon2id$hash",
        now=NOW,
    )
    editor = add_member(store, owner.id, Role.EDITOR, 1)
    reviewer = add_member(store, owner.id, Role.REVIEWER, 1)
    publisher = add_member(store, owner.id, Role.PUBLISHER, 1)
    auditor = add_member(store, owner.id, Role.AUDITOR, 1)
    return owner, editor, reviewer, publisher, auditor


def new_draft(
    store: PlatformStore,
    editor_id: str,
    *,
    publish_at: datetime = PUBLISH_AT,
):
    return store.create_draft(
        actor_id=editor_id,
        text="La educación transforma posibilidades en decisiones.",
        category="educacion",
        publish_at=publish_at,
        evidence={"manual": "capitulo-2", "fuentes": ["estudio-7"]},
        image_sha256=IMAGE_HASH,
        now=NOW,
    )


def approved_draft(
    store: PlatformStore,
    editor_id: str,
    reviewer_id: str,
    *,
    publish_at: datetime = PUBLISH_AT,
):
    draft, revision = new_draft(store, editor_id, publish_at=publish_at)
    store.submit_for_review(
        draft.id,
        actor_id=editor_id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    approval = store.approve_draft(
        draft.id,
        actor_id=reviewer_id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    return draft, revision, approval


def automation_slot(
    slot_id: str = "manana",
    *,
    at: str = "10:00",
    mode: str = "human_review",
    generate_image: bool = True,
) -> dict[str, object]:
    return {
        "id": slot_id,
        "at": at,
        "mode": mode,
        "category": "dato_semana",
        "institution": "colmat",
        "brief": "Explica una cifra territorial con su fuente primaria verificable.",
        "generate_image": generate_image,
        "evidence": {
            "verified": False,
            "reference": None,
            "expected_figure": None,
            "expected_source": None,
        },
    }


def test_schema_contains_all_platform_tables() -> None:
    assert set(table_names()) == {
        "approvals",
        "audit_events",
        "automation_review_notifications",
        "automation_runs",
        "automation_settings",
        "callback_intents",
        "drafts",
        "generation_notifications",
        "generation_requests",
        "media_assets",
        "memberships",
        "publish_attempts",
        "publication_requests",
        "revisions",
        "telegram_bindings",
        "telegram_updates",
        "users",
        "web_auth_challenges",
        "web_auth_sessions",
    }
    assert set(Base.metadata.tables) == set(table_names())


def test_database_url_uses_environment_and_normalizes_host_alias(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://user:secret@db.example/colmat")

    assert resolve_database_url() == "postgresql+psycopg://user:secret@db.example/colmat"
    assert resolve_database_url("sqlite+pysqlite:///:memory:") == ("sqlite+pysqlite:///:memory:")


def test_approval_hash_covers_every_material_field_and_canonicalizes_evidence() -> None:
    base = {
        "text": "Texto final",
        "category": "institucional",
        "publish_at": PUBLISH_AT,
        "evidence": {"b": 2, "a": 1},
        "image_sha256": IMAGE_HASH,
    }
    expected = approval_snapshot_hash(**base)

    reordered = {**base, "evidence": {"a": 1, "b": 2}}
    assert approval_snapshot_hash(**reordered) == expected
    for field, replacement in (
        ("text", "Texto final modificado"),
        ("category", "academia"),
        ("publish_at", PUBLISH_AT + timedelta(minutes=1)),
        ("evidence", {"a": 1, "b": 3}),
        ("image_sha256", "b" * 64),
    ):
        changed = {**base, field: replacement}
        assert approval_snapshot_hash(**changed) != expected


def test_editor_reviewer_publisher_workflow_is_authorized_and_audited(store) -> None:
    owner, editor, reviewer, publisher, _auditor = bootstrap_team(store)
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )

    with pytest.raises(AuthorizationError, match="no tiene el permiso"):
        store.approve_draft(
            draft.id,
            actor_id=editor.id,
            expected_snapshot_hash=revision.snapshot_hash,
            now=NOW,
        )

    approval = store.approve_draft(
        draft.id,
        actor_id=reviewer.id,
        expected_snapshot_hash=revision.snapshot_hash,
        reason="Cumple el manual editorial",
        now=NOW,
    )
    attempt = store.create_publish_attempt(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key=f"x:{draft.id}:{revision.snapshot_hash}",
        now=NOW,
    )
    repeated = store.create_publish_attempt(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key=f"x:{draft.id}:{revision.snapshot_hash}",
        now=NOW,
    )
    completed = store.finish_publish_attempt(
        attempt.id,
        PublishStatus.SUCCEEDED,
        actor_id=publisher.id,
        provider_post_id="190000000000000001",
        now=NOW,
    )

    assert approval.actor_id == reviewer.id
    assert repeated.id == attempt.id
    assert completed.status_value is PublishStatus.SUCCEEDED
    assert store.get_draft(draft.id, actor_id=owner.id).status_value is DraftStatus.PUBLISHED
    events = store.list_audit_events(actor_id=owner.id)
    assert {editor.id, reviewer.id, publisher.id}.issubset({event.actor_id for event in events})
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)


def test_publication_request_enqueue_is_snapshot_bound_idempotent_and_does_not_publish(
    store,
) -> None:
    owner, editor, reviewer, publisher, _auditor = bootstrap_team(store)
    draft, revision, approval = approved_draft(store, editor.id, reviewer.id)

    with pytest.raises(AuthorizationError, match="no tiene el permiso"):
        store.enqueue_publication_request(
            draft.id,
            actor_id=editor.id,
            expected_snapshot_hash=revision.snapshot_hash,
            idempotency_key="telegram:100:publicar",
            now=NOW,
        )

    request = store.enqueue_publication_request(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key="telegram:100:publicar",
        now=NOW,
    )
    repeated = store.enqueue_publication_request(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key="telegram:100:publicar",
        now=NOW + timedelta(seconds=1),
    )

    assert repeated.id == request.id
    assert request.status_value is PublicationRequestStatus.QUEUED
    assert request.revision_id == revision.id
    assert request.approval_id == approval.id
    assert request.snapshot_hash == revision.snapshot_hash
    assert request.claim_token_hash is None
    assert request.claim_fence == 0
    with store.session() as session:
        assert session.scalar(select(func.count(PublicationRequest.id))) == 1
        assert session.scalar(select(func.count(PublishAttempt.id))) == 0

    with pytest.raises(ConflictError, match="otra clave de idempotencia"):
        store.enqueue_publication_request(
            draft.id,
            actor_id=publisher.id,
            expected_snapshot_hash=revision.snapshot_hash,
            idempotency_key="telegram:101:publicar",
            now=NOW,
        )
    with pytest.raises(ConflictError, match="solicitud de publicación"):
        store.revise_draft(
            draft.id,
            actor_id=editor.id,
            text="No se puede sustituir el snapshot ya encolado.",
            category="educacion",
            publish_at=PUBLISH_AT,
            evidence={"manual": "capitulo-2"},
            image_sha256=IMAGE_HASH,
            now=NOW + timedelta(seconds=2),
        )
    assert store.get_draft(draft.id, actor_id=owner.id).status_value is DraftStatus.APPROVED
    events = store.list_audit_events(actor_id=owner.id)
    assert sum(event.action == "publication_request.queued" for event in events) == 1


def test_publication_queue_peek_is_authorized_and_has_no_claim_side_effect(store) -> None:
    _owner, editor, reviewer, publisher, _auditor = bootstrap_team(store)
    draft, revision, _approval = approved_draft(store, editor.id, reviewer.id)
    request = store.enqueue_publication_request(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key="telegram:peek:publicar",
        now=NOW,
    )

    with pytest.raises(AuthorizationError, match="no tiene el permiso"):
        store.has_queued_publication_request(actor_id=editor.id, now=NOW)
    assert store.has_queued_publication_request(actor_id=publisher.id, now=NOW)
    persisted = store.get_publication_request(request.id, actor_id=publisher.id)
    assert persisted.status_value is PublicationRequestStatus.QUEUED
    assert persisted.claim_fence == 0
    assert persisted.claim_token_hash is None

    claim = store.claim_publication_request(
        actor_id=publisher.id,
        now=NOW + timedelta(seconds=1),
    )
    assert claim is not None
    assert not store.has_queued_publication_request(
        actor_id=publisher.id,
        now=NOW + timedelta(seconds=2),
    )


def test_publication_queue_does_not_claim_before_immutable_publish_at(store) -> None:
    _owner, editor, reviewer, publisher, _auditor = bootstrap_team(store)
    publish_at = NOW + timedelta(hours=2)
    draft, revision, _approval = approved_draft(
        store,
        editor.id,
        reviewer.id,
        publish_at=publish_at,
    )
    request = store.enqueue_publication_request(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key="telegram:future:publicar",
        now=NOW,
    )

    assert not store.has_queued_publication_request(
        actor_id=publisher.id,
        now=publish_at - timedelta(microseconds=1),
    )
    assert (
        store.claim_publication_request(
            actor_id=publisher.id,
            now=publish_at - timedelta(microseconds=1),
        )
        is None
    )
    persisted = store.get_publication_request(request.id, actor_id=publisher.id)
    assert persisted.status_value is PublicationRequestStatus.QUEUED
    assert persisted.claim_fence == 0

    claim = store.claim_publication_request(actor_id=publisher.id, now=publish_at)
    assert claim is not None
    assert claim.request.id == request.id


def test_publication_claim_is_opaque_fenced_revalidated_and_expires_unknown(store) -> None:
    owner, editor, reviewer, publisher, _auditor = bootstrap_team(store)
    draft, revision, _approval = approved_draft(store, editor.id, reviewer.id)
    request = store.enqueue_publication_request(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key="telegram:102:publicar",
        now=NOW,
    )

    claim = store.claim_publication_request(
        actor_id=publisher.id,
        lease_seconds=60,
        now=NOW + timedelta(seconds=1),
    )
    assert claim is not None
    assert claim.request.id == request.id
    assert claim.request.status_value is PublicationRequestStatus.CLAIMED
    assert claim.claim_fence == 1
    assert claim.lease_expires_at == NOW + timedelta(seconds=61)
    assert claim.claim_token != claim.request.claim_token_hash
    assert claim.claim_token not in repr(claim)
    assert claim.claim_token not in json_text(claim.request.claim_token_hash)
    assert claim.publish_attempt_idempotency_key == (f"publication-request:{request.id}:f1")
    assert (
        store.claim_publication_request(
            actor_id=publisher.id,
            lease_seconds=60,
            now=NOW + timedelta(seconds=2),
        )
        is None
    )

    with pytest.raises(ConflictError, match="token o fence"):
        store.validate_publication_claim(
            request.id,
            actor_id=publisher.id,
            claim_token="z" * 43,
            claim_fence=claim.claim_fence,
            now=NOW + timedelta(seconds=2),
        )
    with pytest.raises(ConflictError, match="token o fence"):
        store.validate_publication_claim(
            request.id,
            actor_id=publisher.id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence + 1,
            now=NOW + timedelta(seconds=2),
        )
    valid = store.validate_publication_claim(
        request.id,
        actor_id=publisher.id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
        now=NOW + timedelta(seconds=60),
    )
    assert valid.status_value is PublicationRequestStatus.CLAIMED

    with pytest.raises(ConflictError, match="quedó UNKNOWN"):
        store.validate_publication_claim(
            request.id,
            actor_id=publisher.id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            now=NOW + timedelta(seconds=61),
        )
    expired = store.get_publication_request(request.id, actor_id=publisher.id)
    assert expired.status_value is PublicationRequestStatus.UNKNOWN
    assert expired.publish_attempt_id is None
    assert expired.finished_at == NOW + timedelta(seconds=61)
    assert (
        store.expire_publication_claims(
            actor_id=publisher.id,
            now=NOW + timedelta(hours=1),
        )
        == []
    )
    assert (
        store.claim_publication_request(
            actor_id=publisher.id,
            now=NOW + timedelta(hours=1),
        )
        is None
    )
    with pytest.raises(ConflictError, match="otro resultado terminal"):
        store.finish_publication_request(
            request.id,
            PublicationRequestStatus.SUCCEEDED,
            actor_id=publisher.id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            publish_attempt_id="attempt-that-never-ran",
            now=NOW + timedelta(hours=1),
        )
    events = store.list_audit_events(actor_id=owner.id)
    assert sum(event.action == "publication_request.claimed" for event in events) == 1
    assert sum(event.action == "publication_request.unknown" for event in events) == 1
    assert all(claim.claim_token not in json_text(event.detail) for event in events)


@pytest.mark.parametrize(
    ("attempt_status", "request_status", "provider_post_id"),
    [
        (PublishStatus.SUCCEEDED, PublicationRequestStatus.SUCCEEDED, "190000000000000101"),
        (PublishStatus.FAILED, PublicationRequestStatus.FAILED, None),
    ],
)
def test_expired_publication_claim_reconciles_exact_terminal_attempt(
    store,
    attempt_status: PublishStatus,
    request_status: PublicationRequestStatus,
    provider_post_id: str | None,
) -> None:
    owner, editor, reviewer, publisher, _auditor = bootstrap_team(store)
    draft, revision, _approval = approved_draft(store, editor.id, reviewer.id)
    request = store.enqueue_publication_request(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key=f"telegram:terminal:{attempt_status.value}",
        now=NOW,
    )
    claim = store.claim_publication_request(
        actor_id=publisher.id,
        lease_seconds=60,
        now=NOW + timedelta(seconds=1),
    )
    assert claim is not None
    attempt = store.create_publish_attempt(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key=claim.publish_attempt_idempotency_key,
        now=NOW + timedelta(seconds=2),
    )
    store.finish_publish_attempt(
        attempt.id,
        attempt_status,
        actor_id=publisher.id,
        provider_post_id=provider_post_id,
        error=("Fallo explícito antes de confirmar X" if provider_post_id is None else None),
        now=NOW + timedelta(seconds=3),
    )

    expired = store.expire_publication_claims(
        actor_id=publisher.id,
        now=NOW + timedelta(seconds=61),
    )

    assert [item.id for item in expired] == [request.id]
    persisted = store.get_publication_request(request.id, actor_id=publisher.id)
    assert persisted.status_value is request_status
    assert persisted.publish_attempt_id == attempt.id
    assert persisted.finished_at == NOW + timedelta(seconds=61)
    assert not store.has_queued_publication_request(
        actor_id=publisher.id,
        now=NOW + timedelta(hours=1),
    )
    events = store.list_audit_events(actor_id=owner.id)
    reconciled = [
        event for event in events if event.action == f"publication_request.{request_status.value}"
    ]
    assert len(reconciled) == 1
    assert reconciled[0].detail["publish_attempt_id"] == attempt.id
    assert reconciled[0].detail["reason"] == (f"lease_expired_attempt_{attempt_status.value}")


@pytest.mark.parametrize("attempt_case", ["pending", "mismatch"])
def test_expired_publication_claim_with_inconclusive_attempt_stays_unknown(
    store,
    attempt_case: str,
) -> None:
    owner, editor, reviewer, publisher, _auditor = bootstrap_team(store)
    draft, revision, _approval = approved_draft(store, editor.id, reviewer.id)
    request = store.enqueue_publication_request(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key=f"telegram:inconclusive:{attempt_case}",
        now=NOW,
    )
    claim = store.claim_publication_request(
        actor_id=publisher.id,
        lease_seconds=60,
        now=NOW + timedelta(seconds=1),
    )
    assert claim is not None
    attempt = store.create_publish_attempt(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key=claim.publish_attempt_idempotency_key,
        now=NOW + timedelta(seconds=2),
    )
    if attempt_case == "mismatch":
        store.finish_publish_attempt(
            attempt.id,
            PublishStatus.FAILED,
            actor_id=publisher.id,
            error="Fallo terminal que no pertenece temporalmente al claim",
            now=NOW + timedelta(seconds=3),
        )
        with store.session() as session:
            persisted_attempt = session.get(PublishAttempt, attempt.id)
            assert persisted_attempt is not None
            persisted_attempt.started_at = NOW
            session.commit()

    store.expire_publication_claims(
        actor_id=publisher.id,
        now=NOW + timedelta(seconds=61),
    )

    persisted = store.get_publication_request(request.id, actor_id=publisher.id)
    assert persisted.status_value is PublicationRequestStatus.UNKNOWN
    assert persisted.publish_attempt_id is None
    assert (
        store.claim_publication_request(
            actor_id=publisher.id,
            now=NOW + timedelta(hours=1),
        )
        is None
    )
    events = store.list_audit_events(actor_id=owner.id)
    unknown = [event for event in events if event.action == "publication_request.unknown"]
    assert len(unknown) == 1
    assert unknown[0].detail["reason"] == f"lease_expired_attempt_{attempt_case}"


def test_publication_request_finish_requires_matching_final_publish_attempt(store) -> None:
    owner, editor, reviewer, publisher, _auditor = bootstrap_team(store)
    draft, revision, _approval = approved_draft(store, editor.id, reviewer.id)
    request = store.enqueue_publication_request(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key="telegram:103:publicar",
        now=NOW,
    )
    claim = store.claim_publication_request(
        actor_id=publisher.id,
        lease_seconds=120,
        now=NOW + timedelta(seconds=1),
    )
    assert claim is not None
    with pytest.raises(AuthorizationError, match="actor que reclamó"):
        store.validate_publication_claim(
            request.id,
            actor_id=owner.id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            now=NOW + timedelta(seconds=2),
        )

    unrelated = store.create_publish_attempt(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key="unrelated-attempt",
        now=NOW + timedelta(seconds=2),
    )
    store.finish_publish_attempt(
        unrelated.id,
        PublishStatus.FAILED,
        actor_id=publisher.id,
        error="fallo explícito previo a X",
        now=NOW + timedelta(seconds=3),
    )
    with pytest.raises(ConflictError, match="no coincide con el claim cercado"):
        store.finish_publication_request(
            request.id,
            PublicationRequestStatus.FAILED,
            actor_id=publisher.id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            publish_attempt_id=unrelated.id,
            now=NOW + timedelta(seconds=4),
        )

    attempt = store.create_publish_attempt(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key=claim.publish_attempt_idempotency_key,
        now=NOW + timedelta(seconds=5),
    )
    with pytest.raises(ConflictError, match="todavía no tiene un resultado final"):
        store.finish_publication_request(
            request.id,
            PublicationRequestStatus.SUCCEEDED,
            actor_id=publisher.id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            publish_attempt_id=attempt.id,
            now=NOW + timedelta(seconds=6),
        )
    store.finish_publish_attempt(
        attempt.id,
        PublishStatus.SUCCEEDED,
        actor_id=publisher.id,
        provider_post_id="190000000000000099",
        now=NOW + timedelta(seconds=7),
    )
    with pytest.raises(ConflictError, match="token o fence"):
        store.finish_publication_request(
            request.id,
            PublicationRequestStatus.SUCCEEDED,
            actor_id=publisher.id,
            claim_token="z" * 43,
            claim_fence=claim.claim_fence,
            publish_attempt_id=attempt.id,
            now=NOW + timedelta(seconds=8),
        )
    finished = store.finish_publication_request(
        request.id,
        PublicationRequestStatus.SUCCEEDED,
        actor_id=publisher.id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
        publish_attempt_id=attempt.id,
        now=NOW + timedelta(seconds=8),
    )
    repeated = store.finish_publication_request(
        request.id,
        PublicationRequestStatus.SUCCEEDED,
        actor_id=publisher.id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
        publish_attempt_id=attempt.id,
        now=NOW + timedelta(seconds=9),
    )
    assert repeated.id == finished.id
    assert finished.status_value is PublicationRequestStatus.SUCCEEDED
    assert finished.publish_attempt_id == attempt.id
    assert finished.finished_at == NOW + timedelta(seconds=8)


def test_publication_claim_is_atomic_for_competing_workers(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'publication-claim-race.db'}"
    first = PlatformStore(database_url)
    second = PlatformStore(database_url)
    barrier = Barrier(2)

    def synchronize_claim_update(
        connection, cursor, statement, parameters, context, executemany
    ) -> None:
        del cursor, parameters, context, executemany
        normalized = " ".join(statement.casefold().split())
        if not normalized.startswith("update publication_requests set") or connection.info.get(
            "publication_claim_race_synchronized"
        ):
            return
        connection.info["publication_claim_race_synchronized"] = True
        barrier.wait(timeout=5)

    try:
        _owner, editor, reviewer, publisher, _auditor = bootstrap_team(first)
        draft, revision, _approval = approved_draft(first, editor.id, reviewer.id)
        first.enqueue_publication_request(
            draft.id,
            actor_id=publisher.id,
            expected_snapshot_hash=revision.snapshot_hash,
            idempotency_key="telegram:104:publicar",
            now=NOW,
        )
        sqlalchemy_event.listen(
            first.engine,
            "before_cursor_execute",
            synchronize_claim_update,
        )
        sqlalchemy_event.listen(
            second.engine,
            "before_cursor_execute",
            synchronize_claim_update,
        )

        def claim(selected: PlatformStore):
            return selected.claim_publication_request(
                actor_id=publisher.id,
                now=NOW + timedelta(seconds=1),
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                claims = list(executor.map(claim, (first, second)))
        finally:
            sqlalchemy_event.remove(
                first.engine,
                "before_cursor_execute",
                synchronize_claim_update,
            )
            sqlalchemy_event.remove(
                second.engine,
                "before_cursor_execute",
                synchronize_claim_update,
            )

        assert sum(item is not None for item in claims) == 1
        persisted = first.list_publication_requests(
            actor_id=publisher.id,
            status=PublicationRequestStatus.CLAIMED,
        )
        assert len(persisted) == 1
        assert persisted[0].claim_fence == 1
    finally:
        first.close()
        second.close()


def test_media_asset_lookup_by_sha256_is_workspace_scoped_and_read_authorized(store) -> None:
    _owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    assert (
        store.get_media_asset_by_sha256(
            IMAGE_HASH,
            actor_id=reviewer.id,
        )
        is None
    )
    asset = store.register_media_asset(
        actor_id=editor.id,
        kind="image",
        url="https://assets.colmat.test/card.png",
        sha256=IMAGE_HASH,
        mime_type="image/png",
        byte_size=512,
        now=NOW,
    )
    found = store.get_media_asset_by_sha256(
        IMAGE_HASH.upper(),
        actor_id=reviewer.id,
    )
    assert found is not None
    assert found.id == asset.id
    assert found.workspace_id == "colmat"


def test_even_owner_cannot_approve_a_revision_they_authored(store) -> None:
    owner, _ = store.bootstrap_owner(email="owner@colmat.test", display_name="Owner", now=NOW)
    draft, revision = new_draft(store, owner.id)
    store.submit_for_review(
        draft.id,
        actor_id=owner.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )

    with pytest.raises(SeparationOfDutiesError, match="propia revisión"):
        store.approve_draft(
            draft.id,
            actor_id=owner.id,
            expected_snapshot_hash=revision.snapshot_hash,
            now=NOW,
        )


def test_new_revision_invalidates_approval_and_requires_new_snapshot(store) -> None:
    _owner, editor, reviewer, publisher, _auditor = bootstrap_team(store)
    draft, original = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=original.snapshot_hash,
        now=NOW,
    )
    store.approve_draft(
        draft.id,
        actor_id=reviewer.id,
        expected_snapshot_hash=original.snapshot_hash,
        now=NOW,
    )

    revised = store.revise_draft(
        draft.id,
        actor_id=editor.id,
        text="Una versión materialmente distinta.",
        category="educacion",
        publish_at=PUBLISH_AT,
        evidence={"manual": "capitulo-2"},
        image_sha256=IMAGE_HASH,
        now=NOW + timedelta(minutes=1),
    )

    current = store.get_draft(draft.id, actor_id=editor.id)
    assert current.status_value is DraftStatus.DRAFT
    assert current.approved_revision_id is None
    assert revised.snapshot_hash != original.snapshot_hash
    with pytest.raises((ConflictError, StaleSnapshotError)):
        store.create_publish_attempt(
            draft.id,
            actor_id=publisher.id,
            expected_snapshot_hash=original.snapshot_hash,
            idempotency_key="stale-attempt",
            now=NOW,
        )


def test_telegram_updates_are_deduplicated_and_every_mutation_has_actor(store) -> None:
    owner, _editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    store.bind_telegram_chat(
        -1001234567890,
        telegram_user_id=700000001,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="control",
        now=NOW,
    )
    assert (
        store.resolve_telegram_actor(telegram_user_id=700000001, chat_id=-1001234567890).id
        == reviewer.id
    )

    assert store.record_telegram_update(
        4242,
        {"message": {"chat": {"id": -1001234567890}, "text": "/estado"}},
        chat_id=-1001234567890,
        telegram_user_id=700000001,
        actor_id=reviewer.id,
        now=NOW,
    )
    assert not store.record_telegram_update(
        4242,
        {"message": {"text": "reintento del mismo update"}},
        now=NOW,
    )
    processed = store.finish_telegram_update(4242, now=NOW + timedelta(seconds=1))

    assert processed.status == "processed"
    with store.session() as session:
        assert session.scalar(select(func.count(TelegramUpdate.update_id))) == 1
        stored_update = session.get(TelegramUpdate, 4242)
        assert stored_update is not None
        assert stored_update.actor_id == reviewer.id
        assert stored_update.chat_id == "-1001234567890"
        assert stored_update.telegram_user_id == "700000001"
        assert stored_update.payload["message"]["text"] == "/estado"
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.sequence)))
    assert events
    assert all(event.actor_id for event in events)
    assert sum(event.action == "telegram.update_received" for event in events) == 1


def test_callback_nonce_is_bound_to_user_chat_snapshot_and_consumed_once(store) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    store.bind_telegram_chat(
        -1001234567890,
        telegram_user_id=700000001,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )
    issued = store.issue_callback_intent(
        draft.id,
        CallbackAction.APPROVE,
        expected_snapshot_hash=revision.snapshot_hash,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        expires_at=NOW + timedelta(minutes=10),
        now=NOW,
    )

    assert issued.nonce not in issued.intent.nonce_hash
    with pytest.raises(ConflictError, match="inválido"):
        store.consume_callback_intent(
            issued.nonce,
            CallbackAction.APPROVE,
            telegram_user_id=700000001,
            chat_id=-1009999999999,
            now=NOW + timedelta(seconds=1),
        )

    consumed = store.consume_callback_intent(
        issued.nonce,
        CallbackAction.APPROVE,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        now=NOW + timedelta(seconds=2),
    )
    assert consumed.user_id == reviewer.id
    assert consumed.revision_id == revision.id
    assert consumed.snapshot_hash == revision.snapshot_hash
    assert consumed.consumed_by == reviewer.id

    with pytest.raises(ConflictError, match="ya fue utilizado"):
        store.consume_callback_intent(
            issued.nonce,
            CallbackAction.APPROVE,
            telegram_user_id=700000001,
            chat_id=-1001234567890,
            now=NOW + timedelta(seconds=3),
        )
    with store.session() as session:
        assert session.scalar(select(func.count(CallbackIntent.id))) == 1


def test_atomic_callback_decision_commits_nonce_review_and_recovery_result(store) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    store.bind_telegram_chat(
        -1001234567890,
        telegram_user_id=700000001,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )
    issued = store.issue_callback_intent(
        draft.id,
        CallbackAction.APPROVE,
        expected_snapshot_hash=revision.snapshot_hash,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        expires_at=NOW + timedelta(minutes=10),
        now=NOW,
    )
    claim = store.claim_update(
        4301,
        payload={"update_id": 4301, "callback_query": {"data": "approve:opaque"}},
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        now=NOW,
    )

    applied = store.apply_callback_decision(
        issued.nonce,
        decision=CallbackDecision.APPROVE,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        update_id=4301,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
        now=NOW + timedelta(seconds=1),
    )

    assert applied is not None
    assert store.get_draft(draft.id, actor_id=reviewer.id).status_value is DraftStatus.APPROVED
    with store.session() as session:
        intent = session.get(CallbackIntent, issued.intent.id)
        update_row = session.get(TelegramUpdate, 4301)
        assert intent is not None and intent.consumed_by == reviewer.id
        assert update_row is not None
        assert update_row.business_result == {
            "kind": "callback_decision",
            "decision": "approve",
            "post_id": draft.id,
            "snapshot_hash": revision.snapshot_hash,
            "telegram_user_id": 700000001,
            "chat_id": -1001234567890,
        }
        assert session.scalar(select(func.count(Approval.id))) == 1


def test_production_processor_uses_atomic_callback_path_without_operations(store) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    store.bind_telegram_chat(
        -1001234567890,
        telegram_user_id=700000001,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )
    wall_clock = datetime.now(UTC)
    issued = store.issue_callback_intent(
        draft.id,
        CallbackAction.APPROVE,
        expected_snapshot_hash=revision.snapshot_hash,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        expires_at=wall_clock + timedelta(minutes=10),
        now=wall_clock,
    )

    class OperationsMustNotReview:
        def approve_post(self, **kwargs):
            raise AssertionError(f"approve_post separado fue llamado: {kwargs}")

        def reject_post(self, **kwargs):
            raise AssertionError(f"reject_post separado fue llamado: {kwargs}")

    processor = TelegramWebhookProcessor(
        webhook_secret=TelegramWebhookSecret("atomic-secret"),
        update_store=store,
        authorizer=store,
        operations=OperationsMustNotReview(),  # type: ignore[arg-type]
        callback_nonces=store,
    )
    payload = {
        "update_id": 4305,
        "callback_query": {
            "id": "callback-4305",
            "from": {"id": 700000001},
            "message": {
                "message_id": 17,
                "chat": {"id": -1001234567890},
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": "Aprobar",
                                "callback_data": approval_callback_data(issued.nonce),
                            }
                        ]
                    ]
                },
            },
            "data": approval_callback_data(issued.nonce),
        },
    }

    result = processor.process_update(payload, secret_token="atomic-secret")

    assert not result.duplicate
    assert len(result.actions) == 2
    assert store.get_draft(draft.id, actor_id=reviewer.id).status_value is DraftStatus.APPROVED
    with store.session() as session:
        update_row = session.get(TelegramUpdate, 4305)
        assert update_row is not None
        durable_callback = update_row.payload["callback_query"]
        assert durable_callback["data"]["_redacted"] == "callback_query.data"
        assert durable_callback["data"]["decision"] == "approve"
        durable_button = durable_callback["message"]["reply_markup"]["inline_keyboard"][0][0]
        assert durable_button["callback_data"]["_redacted"] == "callback_data"
        assert durable_button["callback_data"]["decision"] == "approve"
        assert issued.nonce not in json.dumps(update_row.payload, sort_keys=True)
        assert update_row.business_result["decision"] == "approve"
        assert len(update_row.prepared_actions) == 2


def test_atomic_callback_rolls_back_consumed_nonce_when_review_step_crashes(
    store, monkeypatch
) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    store.bind_telegram_chat(
        -1001234567890,
        telegram_user_id=700000001,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )
    issued = store.issue_callback_intent(
        draft.id,
        CallbackAction.REJECT,
        expected_snapshot_hash=revision.snapshot_hash,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        expires_at=NOW + timedelta(minutes=10),
        now=NOW,
    )
    claim = store.claim_update(
        4302,
        payload={"update_id": 4302, "callback_query": {"data": "reject:opaque"}},
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        now=NOW,
    )

    def fail_review(*args, **kwargs):
        del args, kwargs
        raise ConflictError("fallo inyectado tras consumir")

    monkeypatch.setattr(store, "_review_draft", fail_review)
    assert (
        store.apply_callback_decision(
            issued.nonce,
            decision=CallbackDecision.REJECT,
            telegram_user_id=700000001,
            chat_id=-1001234567890,
            update_id=4302,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            now=NOW + timedelta(seconds=1),
        )
        is None
    )

    with store.session() as session:
        intent = session.get(CallbackIntent, issued.intent.id)
        update_row = session.get(TelegramUpdate, 4302)
        assert intent is not None and intent.consumed_at is None
        assert update_row is not None and update_row.business_result is None
        assert session.scalar(select(func.count(Approval.id))) == 0
    assert store.get_draft(draft.id, actor_id=reviewer.id).status_value is DraftStatus.IN_REVIEW


def test_competing_callback_updates_can_apply_nonce_only_once(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'callback-race.db'}"
    first = PlatformStore(database_url)
    second = PlatformStore(database_url)
    try:
        owner, editor, reviewer, _publisher, _auditor = bootstrap_team(first)
        draft, revision = new_draft(first, editor.id)
        first.submit_for_review(
            draft.id,
            actor_id=editor.id,
            expected_snapshot_hash=revision.snapshot_hash,
            now=NOW,
        )
        first.bind_telegram_chat(
            -1001234567890,
            telegram_user_id=700000001,
            actor_id=owner.id,
            user_id=reviewer.id,
            purpose="review",
            now=NOW,
        )
        issued = first.issue_callback_intent(
            draft.id,
            CallbackAction.APPROVE,
            expected_snapshot_hash=revision.snapshot_hash,
            telegram_user_id=700000001,
            chat_id=-1001234567890,
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )
        claims = (
            first.claim_update(
                4310,
                payload={"update_id": 4310, "callback_query": {"data": "approve:opaque"}},
                telegram_user_id=700000001,
                chat_id=-1001234567890,
                now=NOW,
            ),
            first.claim_update(
                4311,
                payload={"update_id": 4311, "callback_query": {"data": "approve:opaque"}},
                telegram_user_id=700000001,
                chat_id=-1001234567890,
                now=NOW,
            ),
        )

        def apply(candidate):
            selected, update_id, claim = candidate
            return selected.apply_callback_decision(
                issued.nonce,
                decision=CallbackDecision.APPROVE,
                telegram_user_id=700000001,
                chat_id=-1001234567890,
                update_id=update_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                now=NOW + timedelta(seconds=1),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    apply,
                    ((first, 4310, claims[0]), (second, 4311, claims[1])),
                )
            )

        assert sum(result is not None for result in results) == 1
        with first.session() as session:
            intent = session.get(CallbackIntent, issued.intent.id)
            assert intent is not None and intent.consumed_by == reviewer.id
            assert session.scalar(select(func.count(Approval.id))) == 1
    finally:
        first.close()
        second.close()


def test_review_callback_pair_rolls_back_both_intents_if_second_issue_fails(
    store, monkeypatch
) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    store.bind_telegram_chat(
        -1001234567890,
        telegram_user_id=700000001,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )
    original_issue = store.issue_callback_intent
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fallo inyectado")
        return original_issue(*args, **kwargs)

    monkeypatch.setattr(store, "issue_callback_intent", fail_second)
    with pytest.raises(RuntimeError, match="inyectado"):
        store.issue_review_callback_intents(
            draft.id,
            expected_snapshot_hash=revision.snapshot_hash,
            telegram_user_id=700000001,
            chat_id=-1001234567890,
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )
    with store.session() as session:
        assert session.scalar(select(func.count(CallbackIntent.id))) == 0


def test_failed_update_replays_only_prepared_actions_with_new_fence(store) -> None:
    payload = {"update_id": 4303, "message": {"text": "/estado"}}
    first = store.claim_update(
        4303,
        payload=payload,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        now=NOW,
    )
    actions = (
        {
            "kind": "send_message",
            "telegram_user_id": 700000001,
            "chat_id": -1001234567890,
            "text": "Estado",
            "reply_to_message_id": 9,
        },
    )
    store.prepare_telegram_actions(
        4303,
        actions,
        claim_token=first.claim_token,
        claim_fence=first.claim_fence,
        now=NOW + timedelta(seconds=1),
    )
    store.finish_telegram_update(
        4303,
        error="webhook_failed:transport",
        claim_token=first.claim_token,
        claim_fence=first.claim_fence,
        now=NOW + timedelta(seconds=2),
    )

    replay = store.claim_update(
        4303,
        payload=payload,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        now=NOW + timedelta(seconds=3),
    )

    assert replay.acquired
    assert replay.claim_fence == first.claim_fence + 1
    assert replay.claim_token != first.claim_token
    assert replay.prepared_actions == actions
    with pytest.raises(ConflictError, match="token o fence"):
        store.finish_telegram_update(
            4303,
            claim_token=first.claim_token,
            claim_fence=first.claim_fence,
            now=NOW + timedelta(seconds=4),
        )
    completed = store.finish_telegram_update(
        4303,
        claim_token=replay.claim_token,
        claim_fence=replay.claim_fence,
        now=NOW + timedelta(seconds=4),
    )
    assert completed.status == "processed"
    assert completed.attempt_count == 2


def test_expired_update_without_durable_result_is_not_reprocessed(store) -> None:
    payload = {"update_id": 4304, "message": {"text": "/generar algo"}}
    claim = store.claim_update(
        4304,
        payload=payload,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        lease_seconds=1,
        now=NOW,
    )
    assert claim.acquired

    retry = store.claim_update(
        4304,
        payload=payload,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        lease_seconds=1,
        now=NOW + timedelta(seconds=2),
    )

    assert not retry.acquired
    assert not retry.retryable
    with store.session() as session:
        stored = session.get(TelegramUpdate, 4304)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.error == "ambiguous_processing_state:no_durable_result"


def test_callback_intent_rejects_a_stale_expected_snapshot_before_issuing_nonce(store) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    store.bind_telegram_chat(
        -1001234567890,
        telegram_user_id=700000001,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )

    with pytest.raises(StaleSnapshotError, match="snapshot solicitado"):
        store.issue_callback_intent(
            draft.id,
            CallbackAction.APPROVE,
            expected_snapshot_hash="f" * 64,
            telegram_user_id=700000001,
            chat_id=-1001234567890,
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )
    with store.session() as session:
        assert session.scalar(select(func.count(CallbackIntent.id))) == 0


def test_platform_store_implements_telegram_bot_protocols(store) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    store.bind_telegram_chat(
        -1001234567890,
        telegram_user_id=700000001,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )

    assert store.is_allowed(
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        permission=BotPermission.APPROVE,
    )
    assert store.is_allowed(
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        permission=BotPermission.VIEW_CALENDAR,
    )
    assert not store.is_allowed(
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        permission=BotPermission.MANAGE_MODE,
    )
    assert not store.is_allowed(
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        permission=BotPermission.GENERATE,
    )
    assert not store.is_allowed(
        telegram_user_id=700000002,
        chat_id=-1001234567890,
        permission=BotPermission.APPROVE,
    )
    assert store.claim_update(
        9191,
        payload={"update_id": 9191},
        telegram_user_id=700000001,
        chat_id=-1001234567890,
    )
    assert not store.claim_update(
        9191,
        payload={"update_id": 9191, "duplicate": True},
        telegram_user_id=700000001,
        chat_id=-1001234567890,
    )

    wall_clock = datetime.now(UTC)
    issued = store.issue_callback_intent(
        draft.id,
        CallbackAction.APPROVE,
        expected_snapshot_hash=revision.snapshot_hash,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
        expires_at=wall_clock + timedelta(minutes=10),
        now=wall_clock,
    )
    bot_intent = store.consume_callback_nonce(
        issued.nonce,
        decision=CallbackDecision.APPROVE,
        telegram_user_id=700000001,
        chat_id=-1001234567890,
    )

    assert bot_intent is not None
    assert bot_intent.decision is CallbackDecision.APPROVE
    assert bot_intent.post_id == draft.id
    assert bot_intent.snapshot_hash == revision.snapshot_hash


def test_admin_cannot_promote_admin_or_remove_owner(store) -> None:
    owner, _ = store.bootstrap_owner(email="owner@colmat.test", display_name="Owner", now=NOW)
    admin = add_member(store, owner.id, Role.ADMIN, 1)
    candidate = store.create_user(
        actor_id=admin.id,
        email="candidate@colmat.test",
        display_name="Candidate",
        now=NOW,
    )

    with pytest.raises(AuthorizationError, match="no puede asignar"):
        store.grant_membership(candidate.id, Role.ADMIN, actor_id=admin.id, now=NOW)
    with pytest.raises(AuthorizationError, match="owners"):
        store.revoke_membership(owner.id, actor_id=admin.id, now=NOW)


def test_create_team_member_is_atomic_and_enforces_role_hierarchy(store) -> None:
    owner, _ = store.bootstrap_owner(email="owner@colmat.test", display_name="Owner", now=NOW)
    admin = add_member(store, owner.id, Role.ADMIN, 1)

    editor, membership = store.create_team_member(
        actor_id=admin.id,
        email="new-editor@colmat.test",
        username="new.editor",
        display_name="New Editor",
        role=Role.EDITOR,
        now=NOW,
    )

    assert editor.email == "new-editor@colmat.test"
    assert editor.username == "new.editor"
    assert membership.user_id == editor.id
    assert membership.role_value is Role.EDITOR

    with pytest.raises(AuthorizationError, match="no puede asignar"):
        store.create_team_member(
            actor_id=admin.id,
            email="forbidden-admin@colmat.test",
            display_name="Forbidden Admin",
            role=Role.ADMIN,
            now=NOW,
        )

    # La identidad tampoco queda huérfana cuando falla la asignación de rol.
    recovered = store.create_user(
        actor_id=owner.id,
        email="forbidden-admin@colmat.test",
        display_name="Recovered",
        now=NOW,
    )
    assert recovered.email == "forbidden-admin@colmat.test"


def test_telegram_binding_cannot_impersonate_higher_roles_or_transfer_identity(store) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    admin = add_member(store, owner.id, Role.ADMIN, 2)

    with pytest.raises(AuthorizationError, match="owners"):
        store.bind_telegram_chat(
            9001,
            telegram_user_id=9001,
            actor_id=admin.id,
            user_id=owner.id,
            now=NOW,
        )
    with pytest.raises(AuthorizationError, match="Solo el owner"):
        store.bind_telegram_chat(
            9002,
            telegram_user_id=9002,
            actor_id=admin.id,
            user_id=reviewer.id,
            now=NOW,
        )

    store.bind_telegram_chat(
        9003,
        telegram_user_id=9003,
        actor_id=owner.id,
        user_id=reviewer.id,
        now=NOW,
    )
    with pytest.raises(ConflictError, match="no se transfiere"):
        store.bind_telegram_chat(
            9003,
            telegram_user_id=9003,
            actor_id=owner.id,
            user_id=editor.id,
            now=NOW,
        )


def test_last_owner_is_protected(store) -> None:
    owner, _ = store.bootstrap_owner(email="owner@colmat.test", display_name="Owner", now=NOW)

    with pytest.raises(ConflictError, match="al menos un owner"):
        store.revoke_membership(owner.id, actor_id=owner.id, now=NOW)


@pytest.mark.parametrize("action", ["demote", "revoke"])
def test_inactive_owner_does_not_satisfy_last_active_owner_invariant(store, action: str) -> None:
    owner_a, _ = store.bootstrap_owner(
        email="active-owner@colmat.test",
        display_name="Active Owner",
        now=NOW,
    )
    owner_b = add_member(store, owner_a.id, Role.OWNER, 2)
    store.set_user_active(
        owner_b.id,
        active=False,
        actor_id=owner_a.id,
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ConflictError, match="owner activo"):
        if action == "demote":
            store.change_membership_role(
                owner_a.id,
                Role.ADMIN,
                actor_id=owner_a.id,
                now=NOW + timedelta(seconds=2),
            )
        else:
            store.revoke_membership(
                owner_a.id,
                actor_id=owner_a.id,
                now=NOW + timedelta(seconds=2),
            )

    membership = next(
        item for item in store.list_memberships(actor_id=owner_a.id) if item.user_id == owner_a.id
    )
    assert membership.role_value is Role.OWNER
    assert store.get_user(owner_a.id).is_active is True


def test_concurrent_owner_demotions_preserve_one_owner(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'owner-race.db'}"
    first = PlatformStore(database_url)
    second = PlatformStore(database_url)
    barrier = Barrier(2)
    try:
        owner_a, _ = first.bootstrap_owner(
            email="owner-a@colmat.test",
            display_name="Owner A",
            now=NOW,
        )
        owner_b = first.create_user(
            actor_id=owner_a.id,
            email="owner-b@colmat.test",
            display_name="Owner B",
            now=NOW,
        )
        first.grant_membership(owner_b.id, Role.OWNER, actor_id=owner_a.id, now=NOW)

        def demote(candidate: tuple[PlatformStore, str]) -> str:
            selected, owner_id = candidate
            barrier.wait(timeout=5)
            try:
                selected.change_membership_role(
                    owner_id,
                    Role.ADMIN,
                    actor_id=owner_id,
                    now=NOW + timedelta(seconds=1),
                )
            except (AuthorizationError, ConflictError):
                return "rejected"
            return "changed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    demote,
                    ((first, owner_a.id), (second, owner_b.id)),
                )
            )

        assert sorted(outcomes) == ["changed", "rejected"]
        with first.session() as session:
            owners = list(
                session.scalars(
                    select(Membership).where(
                        Membership.workspace_id == "colmat",
                        Membership.role == Role.OWNER.value,
                    )
                )
            )
            assert len(owners) == 1
            assert session.get(User, owners[0].user_id).is_active
    finally:
        first.close()
        second.close()


def test_postgres_rbac_serialization_uses_transaction_advisory_lock() -> None:
    calls: list[tuple[str, dict[str, int]]] = []
    fake_session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=lambda statement, parameters: calls.append((str(statement), parameters)),
    )

    PlatformStore._serialize_workspace_rbac(fake_session, "colmat")
    PlatformStore._serialize_user_rbac(fake_session, "user-1")

    assert len(calls) == 2
    assert all("pg_advisory_xact_lock" in statement for statement, _parameters in calls)
    assert all(isinstance(parameters["lock_key"], int) for _statement, parameters in calls)
    assert calls[0][1]["lock_key"] != calls[1][1]["lock_key"]


def test_workspace_admin_cannot_mutate_a_global_cross_workspace_account(store) -> None:
    owner_a, _ = store.bootstrap_owner(
        email="owner-a@colmat.test",
        display_name="Owner A",
        workspace_id="workspace-a",
        now=NOW,
    )
    owner_b, _ = store.bootstrap_owner(
        email="owner-b@colmat.test",
        display_name="Owner B",
        workspace_id="workspace-b",
        now=NOW,
    )

    with pytest.raises(NotFoundError, match="no pertenece"):
        store.set_user_active(
            owner_b.id,
            active=False,
            actor_id=owner_a.id,
            workspace_id="workspace-a",
            now=NOW,
        )

    store.grant_membership(
        owner_b.id,
        Role.EDITOR,
        actor_id=owner_a.id,
        workspace_id="workspace-a",
        now=NOW,
    )
    with pytest.raises(ConflictError, match="otros espacios"):
        store.set_user_active(
            owner_b.id,
            active=False,
            actor_id=owner_a.id,
            workspace_id="workspace-a",
            now=NOW,
        )
    with pytest.raises(ConflictError, match="otros espacios"):
        store.set_username(
            owner_b.id,
            "mutated-by-a",
            actor_id=owner_a.id,
            workspace_id="workspace-a",
            now=NOW,
        )

    updated = store.set_username(
        owner_b.id,
        "owner-b",
        actor_id=owner_b.id,
        workspace_id="workspace-b",
        now=NOW,
    )
    assert updated.username == "owner-b"
    assert updated.is_active is True


def test_inactive_account_cannot_be_granted_into_another_workspace(store) -> None:
    owner_a, _ = store.bootstrap_owner(
        email="owner-a@colmat.test",
        display_name="Owner A",
        workspace_id="workspace-a",
        now=NOW,
    )
    member = store.create_user(
        actor_id=owner_a.id,
        email="inactive@colmat.test",
        display_name="Inactive",
        workspace_id="workspace-a",
        now=NOW,
    )
    store.grant_membership(
        member.id,
        Role.EDITOR,
        actor_id=owner_a.id,
        workspace_id="workspace-a",
        now=NOW,
    )
    store.set_user_active(
        member.id,
        active=False,
        actor_id=owner_a.id,
        workspace_id="workspace-a",
        now=NOW,
    )
    owner_b, _ = store.bootstrap_owner(
        email="owner-b@colmat.test",
        display_name="Owner B",
        workspace_id="workspace-b",
        now=NOW,
    )

    with pytest.raises(ConflictError, match="globalmente inactiva"):
        store.grant_membership(
            member.id,
            Role.EDITOR,
            actor_id=owner_b.id,
            workspace_id="workspace-b",
            now=NOW,
        )


def test_raw_tampering_is_detected_before_approval(store) -> None:
    _owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    with store.session() as session, session.begin():
        persisted = session.get(Revision, revision.id)
        assert persisted is not None
        persisted.category = "categoria-alterada"

    with pytest.raises(StaleSnapshotError, match="revisión persistida"):
        store.approve_draft(
            draft.id,
            actor_id=reviewer.id,
            expected_snapshot_hash=revision.snapshot_hash,
            now=NOW,
        )


def test_failed_bootstrap_does_not_create_a_second_owner(store) -> None:
    store.bootstrap_owner(email="owner@colmat.test", display_name="Owner", now=NOW)

    with pytest.raises(ConflictError, match="ya tiene un owner"):
        store.bootstrap_owner(email="other@colmat.test", display_name="Other", now=NOW)

    with store.session() as session:
        assert session.scalar(select(func.count(Approval.id))) == 0


def test_automation_defaults_permissions_and_cas_are_safe(store) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    scheduler = add_member(store, owner.id, Role.SCHEDULER, 1)

    defaults = store.get_automation_settings(actor_id=editor.id)
    assert defaults.enabled is False
    assert defaults.mode_value is AutomationMode.HUMAN_REVIEW
    assert defaults.timezone == "America/Bogota"
    assert defaults.slots == []
    assert defaults.generate_images is False
    assert defaults.min_engagement_score == 0
    assert defaults.max_posts_per_day == 2
    assert defaults.version == 1
    assert defaults.direct_authorized_by is None
    assert defaults.direct_authorized_at is None

    with pytest.raises(AuthorizationError, match="no tiene el permiso"):
        store.update_automation_settings(
            actor_id=reviewer.id,
            expected_version=1,
            enabled=True,
            now=NOW,
        )

    updated = store.update_automation_settings(
        actor_id=scheduler.id,
        expected_version=1,
        enabled=True,
        slots=[automation_slot()],
        generate_images=True,
        min_engagement_score=75,
        max_posts_per_day=3,
        now=NOW + timedelta(seconds=1),
    )
    assert updated.version == 2
    assert updated.updated_by == scheduler.id
    assert updated.slots[0]["id"] == "manana"
    assert updated.slots[0]["evidence"] == {
        "verified": False,
        "reference": None,
        "expected_figure": None,
        "expected_source": None,
    }

    with pytest.raises(ConflictError, match="versión actual 2"):
        store.update_automation_settings(
            actor_id=scheduler.id,
            expected_version=1,
            enabled=False,
            now=NOW + timedelta(seconds=2),
        )


def test_automation_cannot_be_enabled_with_an_empty_effective_schedule(store) -> None:
    owner, _ = store.bootstrap_owner(
        email="owner@colmat.test",
        display_name="Owner",
        now=NOW,
    )
    with pytest.raises(ValueError, match="sin slots válidos"):
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            enabled=True,
            now=NOW,
        )
    assert store.get_automation_settings(actor_id=owner.id).version == 1

    configured = store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        slots=[automation_slot()],
        now=NOW + timedelta(seconds=1),
    )
    enabled = store.update_automation_settings(
        actor_id=owner.id,
        expected_version=configured.version,
        enabled=True,
        now=NOW + timedelta(seconds=2),
    )
    with pytest.raises(ValueError, match="sin slots válidos"):
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=enabled.version,
            slots=[],
            now=NOW + timedelta(seconds=3),
        )
    persisted = store.get_automation_settings(actor_id=owner.id)
    assert persisted.version == enabled.version
    assert persisted.enabled is True
    assert persisted.slots == [automation_slot()]


def test_persisted_evidence_requires_expected_figure_and_source_when_verified(store) -> None:
    owner, _ = store.bootstrap_owner(
        email="owner@colmat.test",
        display_name="Owner",
        now=NOW,
    )
    missing_expected = automation_slot()
    missing_expected["evidence"] = {
        "verified": True,
        "reference": "DANE, cuenta nacional 2024",
        "expected_figure": None,
        "expected_source": None,
    }
    with pytest.raises(ValueError, match="cifra y fuente esperadas concretas"):
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            slots=[missing_expected],
            now=NOW,
        )

    only_figure = automation_slot()
    only_figure["evidence"] = {
        "verified": False,
        "reference": None,
        "expected_figure": "25,2 %",
        "expected_source": None,
    }
    with pytest.raises(ValueError, match="expected_figure y expected_source juntos"):
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            slots=[only_figure],
            now=NOW,
        )


def test_sqlite_backfills_expected_evidence_fields_as_unverified_nulls(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'legacy-evidence.db'}"
    legacy_slot = automation_slot()
    legacy_slot["evidence"] = {"verified": False, "reference": None}
    first = PlatformStore(database_url)
    try:
        owner, _ = first.bootstrap_owner(
            email="owner@colmat.test",
            display_name="Owner",
            now=NOW,
        )
        with first.engine.begin() as connection:
            connection.execute(
                text("UPDATE automation_settings SET slots = :slots"),
                {"slots": json.dumps([legacy_slot])},
            )
    finally:
        first.close()

    with PlatformStore(database_url) as reopened:
        settings = reopened.get_automation_settings(actor_id=owner.id)

    assert settings.slots[0]["evidence"] == {
        "verified": False,
        "reference": None,
        "expected_figure": None,
        "expected_source": None,
    }


def test_direct_mode_requires_privilege_kill_switch_and_persists_authorizer(
    store, monkeypatch
) -> None:
    owner, _editor, _reviewer, _publisher, _auditor = bootstrap_team(store)
    scheduler = add_member(store, owner.id, Role.SCHEDULER, 1)

    with pytest.raises(AuthorizationError, match="no tiene el permiso"):
        store.update_automation_settings(
            actor_id=scheduler.id,
            expected_version=1,
            mode=AutomationMode.DIRECT,
            now=NOW,
        )
    with pytest.raises(ConflictError, match="COLMAT_DIRECT_PUBLISH_ENABLED"):
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            mode=AutomationMode.DIRECT,
            now=NOW,
        )

    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    direct = store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        mode=AutomationMode.DIRECT,
        slots=[automation_slot("directo", mode="direct")],
        now=NOW + timedelta(seconds=1),
    )
    assert direct.mode_value is AutomationMode.DIRECT
    assert direct.direct_authorized_by == owner.id
    assert direct.direct_authorized_at == NOW + timedelta(seconds=1)

    # El scheduler puede accionar el kill switch persistido, pero no reautorizarlo.
    disabled = store.update_automation_settings(
        actor_id=scheduler.id,
        expected_version=2,
        enabled=False,
        now=NOW + timedelta(seconds=2),
    )
    assert disabled.enabled is False
    human = store.update_automation_settings(
        actor_id=owner.id,
        expected_version=3,
        mode=AutomationMode.HUMAN_REVIEW,
        now=NOW + timedelta(seconds=3),
    )
    assert human.direct_authorized_by is None
    assert human.direct_authorized_at is None


@pytest.mark.parametrize("revocation", ["deactivate", "demote"])
def test_direct_claim_rejects_revoked_authorizer(store, monkeypatch, revocation: str) -> None:
    owner, _ = store.bootstrap_owner(
        email="owner@colmat.test",
        display_name="Owner",
        now=NOW,
    )
    replacement_owner = add_member(store, owner.id, Role.OWNER, 2)
    scheduler = add_member(store, owner.id, Role.SCHEDULER, 1)
    slot = automation_slot("directo", mode="direct")
    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        mode=AutomationMode.DIRECT,
        slots=[slot],
        now=NOW + timedelta(seconds=1),
    )

    if revocation == "deactivate":
        store.set_user_active(
            owner.id,
            active=False,
            actor_id=replacement_owner.id,
            now=NOW + timedelta(seconds=2),
        )
    else:
        store.change_membership_role(
            owner.id,
            Role.AUDITOR,
            actor_id=replacement_owner.id,
            now=NOW + timedelta(seconds=2),
        )

    with pytest.raises(ConflictError, match="inactiva o sin privilegio"):
        store.claim_automation_run(
            actor_id=scheduler.id,
            idempotency_key="colmat:auto:v1:2026-08-29:directo",
            slot_id="directo",
            scheduled_for=NOW,
            slot_snapshot=slot,
            mode=AutomationMode.DIRECT,
            now=NOW + timedelta(seconds=3),
        )
    assert store.list_automation_runs(actor_id=replacement_owner.id) == []


def test_scheduler_cannot_mutate_material_schedule_while_direct(store, monkeypatch) -> None:
    owner, _ = store.bootstrap_owner(
        email="owner@colmat.test",
        display_name="Owner",
        now=NOW,
    )
    scheduler = add_member(store, owner.id, Role.SCHEDULER, 1)
    authorized_slot = automation_slot("directo", mode="direct")
    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    direct = store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        mode=AutomationMode.DIRECT,
        slots=[authorized_slot],
        now=NOW + timedelta(seconds=1),
    )
    altered_slot = {
        **authorized_slot,
        "brief": "Contenido sustituido por el scheduler sin permiso.",
    }

    with pytest.raises(AuthorizationError, match="no tiene el permiso"):
        store.update_automation_settings(
            actor_id=scheduler.id,
            expected_version=direct.version,
            slots=[altered_slot],
            max_posts_per_day=100,
            now=NOW + timedelta(seconds=2),
        )

    persisted = store.get_automation_settings(actor_id=owner.id)
    assert persisted.version == direct.version
    assert persisted.slots == [authorized_slot]
    assert persisted.max_posts_per_day == direct.max_posts_per_day
    assert persisted.direct_authorized_by == owner.id


def test_reenabling_direct_renews_authorizer(store, monkeypatch) -> None:
    owner, _ = store.bootstrap_owner(
        email="owner@colmat.test",
        display_name="Owner",
        now=NOW,
    )
    replacement_owner = add_member(store, owner.id, Role.OWNER, 2)
    scheduler = add_member(store, owner.id, Role.SCHEDULER, 1)
    slot = automation_slot("directo", mode="direct")
    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    direct = store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        mode=AutomationMode.DIRECT,
        slots=[slot],
        now=NOW + timedelta(seconds=1),
    )
    paused = store.update_automation_settings(
        actor_id=scheduler.id,
        expected_version=direct.version,
        enabled=False,
        now=NOW + timedelta(seconds=2),
    )
    store.set_user_active(
        owner.id,
        active=False,
        actor_id=replacement_owner.id,
        now=NOW + timedelta(seconds=3),
    )

    reenabled = store.update_automation_settings(
        actor_id=replacement_owner.id,
        expected_version=paused.version,
        enabled=True,
        now=NOW + timedelta(seconds=4),
    )

    assert reenabled.direct_authorized_by == replacement_owner.id
    assert reenabled.direct_authorized_at == NOW + timedelta(seconds=4)
    claimed = store.claim_automation_run(
        actor_id=scheduler.id,
        idempotency_key="colmat:auto:v1:2026-08-29:directo",
        slot_id="directo",
        scheduled_for=NOW,
        slot_snapshot=slot,
        mode=AutomationMode.DIRECT,
        now=NOW + timedelta(seconds=5),
    )
    assert claimed.settings_version == reenabled.version


def test_claim_rejects_altered_slot_snapshot_with_same_identity_and_time(store) -> None:
    owner, _ = store.bootstrap_owner(
        email="owner@colmat.test",
        display_name="Owner",
        now=NOW,
    )
    authorized_slot = automation_slot()
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        slots=[authorized_slot],
        now=NOW,
    )
    altered_slot = {
        **authorized_slot,
        "brief": "Otro brief con el mismo identificador y exactamente la misma hora.",
    }

    with pytest.raises(ConflictError, match="snapshot del slot"):
        store.claim_automation_run(
            actor_id=owner.id,
            idempotency_key="colmat:auto:v1:2026-08-29:manana",
            slot_id="manana",
            scheduled_for=NOW,
            slot_snapshot=altered_slot,
            now=NOW,
        )
    altered_evidence = {
        **authorized_slot,
        "evidence": {
            **authorized_slot["evidence"],
            "expected_figure": "25,2 %",
            "expected_source": "DANE 2024",
        },
    }
    with pytest.raises(ConflictError, match="snapshot del slot"):
        store.claim_automation_run(
            actor_id=owner.id,
            idempotency_key="colmat:auto:v1:2026-08-29:manana",
            slot_id="manana",
            scheduled_for=NOW,
            slot_snapshot=altered_evidence,
            now=NOW,
        )
    assert store.list_automation_runs(actor_id=owner.id) == []


def test_concurrent_identical_claims_return_one_run(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'claim-race.db'}"
    first = PlatformStore(database_url)
    second = PlatformStore(database_url)
    slot = automation_slot()
    barrier = Barrier(2)

    def synchronize_existing_run_lookup(
        connection, cursor, statement, parameters, context, executemany
    ) -> None:
        del cursor, parameters, context, executemany
        normalized = " ".join(statement.casefold().split())
        if (
            "from automation_runs" not in normalized
            or "idempotency_key" not in normalized
            or connection.info.get("claim_race_synchronized")
        ):
            return
        connection.info["claim_race_synchronized"] = True
        barrier.wait(timeout=5)

    try:
        owner, _ = first.bootstrap_owner(
            email="owner@colmat.test",
            display_name="Owner",
            user_id="race-owner",
            now=NOW,
        )
        first.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            enabled=True,
            slots=[slot],
            now=NOW,
        )
        sqlalchemy_event.listen(
            first.engine,
            "after_cursor_execute",
            synchronize_existing_run_lookup,
        )
        sqlalchemy_event.listen(
            second.engine,
            "after_cursor_execute",
            synchronize_existing_run_lookup,
        )

        def claim(selected: PlatformStore) -> str:
            return selected.claim_automation_run(
                actor_id=owner.id,
                idempotency_key="colmat:auto:v1:2026-08-29:manana",
                slot_id="manana",
                scheduled_for=NOW,
                slot_snapshot=slot,
                now=NOW,
            ).id

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                run_ids = list(executor.map(claim, (first, second)))
        finally:
            sqlalchemy_event.remove(
                first.engine,
                "after_cursor_execute",
                synchronize_existing_run_lookup,
            )
            sqlalchemy_event.remove(
                second.engine,
                "after_cursor_execute",
                synchronize_existing_run_lookup,
            )

        assert run_ids[0] == run_ids[1]
        assert [item.id for item in first.list_automation_runs(actor_id=owner.id)] == [run_ids[0]]
    finally:
        first.close()
        second.close()


def test_usernames_are_optional_canonical_unique_and_profile_changes_are_audited(store) -> None:
    owner, _ = store.bootstrap_owner(
        email="owner@colmat.test",
        username="SirHegel",
        display_name="Owner",
        now=NOW,
    )
    assert owner.username == "sirhegel"
    editor = add_member(store, owner.id, Role.EDITOR, 1)
    changed = store.set_username(
        editor.id,
        "  Equipo.Colmat  ",
        actor_id=editor.id,
        now=NOW + timedelta(seconds=1),
    )
    assert changed.username == "equipo.colmat"
    renamed = store.update_display_name(
        editor.id,
        "Equipo Editorial",
        actor_id=editor.id,
        now=NOW + timedelta(seconds=2),
    )
    assert renamed.display_name == "Equipo Editorial"

    with pytest.raises(ConflictError, match="Ya existe el username"):
        store.create_user(
            actor_id=owner.id,
            email="duplicate@colmat.test",
            username="SIRHEGEL",
            display_name="Duplicate",
            now=NOW,
        )

    admin = add_member(store, owner.id, Role.ADMIN, 1)
    with pytest.raises(AuthorizationError, match="owners"):
        store.update_display_name(owner.id, "Other", actor_id=admin.id, now=NOW)
    with store.session() as session:
        persisted = session.get(User, editor.id)
        assert persisted is not None
        assert persisted.username == "equipo.colmat"


def test_automation_review_hold_commits_run_and_outbox_then_prepares_callbacks(
    store,
) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    telegram_user_id = 778899
    chat_id = -1001234567890
    store.bind_telegram_chat(
        chat_id,
        telegram_user_id=telegram_user_id,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )
    slot = automation_slot("review-outbox", generate_image=False)
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        slots=[slot],
        now=NOW,
    )
    run = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:review-outbox",
        slot_id="review-outbox",
        scheduled_for=NOW,
        slot_snapshot=slot,
        now=NOW,
    )
    draft, revision = store.create_draft(
        actor_id=editor.id,
        text="Texto territorial verificable para revisión humana.",
        category="educacion",
        publish_at=PUBLISH_AT,
        evidence={"source": "manual"},
        image_sha256=None,
        now=NOW,
    )
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )

    notification = store.hold_automation_run_for_review(
        run.id,
        actor_id=owner.id,
        draft_id=draft.id,
        expected_snapshot_hash=revision.snapshot_hash,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        detail="Revisión humana obligatoria.",
        engagement_score=81,
        now=NOW + timedelta(seconds=1),
    )
    repeated = store.hold_automation_run_for_review(
        run.id,
        actor_id=owner.id,
        draft_id=draft.id,
        expected_snapshot_hash=revision.snapshot_hash,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        detail="Revisión humana obligatoria.",
        engagement_score=81,
        now=NOW + timedelta(seconds=2),
    )

    assert repeated.id == notification.id
    persisted_run = store.list_automation_runs(actor_id=owner.id)[0]
    assert persisted_run.status_value is AutomationRunStatus.AWAITING_REVIEW
    assert persisted_run.draft_id == draft.id
    with store.session() as session:
        assert session.scalar(select(func.count(AutomationReviewNotification.id))) == 1

    claim = store.claim_automation_review_notification(
        actor_id=owner.id,
        now=NOW + timedelta(seconds=3),
    )
    assert claim is not None
    assert "claim_token" not in repr(claim)
    approve, reject = store.prepare_automation_review_notification_callbacks(
        notification.id,
        actor_id=owner.id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
        expires_at=NOW + timedelta(hours=1),
        now=NOW + timedelta(seconds=4),
    )
    persisted = store.get_automation_review_notification(
        notification.id,
        actor_id=owner.id,
    )
    assert persisted.approve_intent_id == approve.intent.id
    assert persisted.reject_intent_id == reject.intent.id
    assert not hasattr(persisted, "approve_nonce")
    assert not hasattr(persisted, "reject_nonce")

    sent = store.finish_automation_review_notification(
        notification.id,
        AutomationReviewNotificationStatus.SENT,
        actor_id=owner.id,
        claim_token=claim.claim_token,
        claim_fence=claim.claim_fence,
        review_message_id=1234,
        now=NOW + timedelta(seconds=5),
    )
    assert sent.status_value is AutomationReviewNotificationStatus.SENT


def test_automation_review_hold_rolls_back_and_expired_claim_is_unknown(store) -> None:
    owner, editor, reviewer, _publisher, _auditor = bootstrap_team(store)
    telegram_user_id = 778899
    chat_id = -1001234567890
    store.bind_telegram_chat(
        chat_id,
        telegram_user_id=telegram_user_id,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )
    slot = automation_slot("review-expiry", generate_image=False)
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        slots=[slot],
        now=NOW,
    )
    run = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:review-expiry",
        slot_id="review-expiry",
        scheduled_for=NOW,
        slot_snapshot=slot,
        now=NOW,
    )
    draft, revision = store.create_draft(
        actor_id=editor.id,
        text="Texto territorial verificable para revisión humana.",
        category="educacion",
        publish_at=PUBLISH_AT,
        evidence={"source": "manual"},
        image_sha256=None,
        now=NOW,
    )
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )

    with pytest.raises(StaleSnapshotError):
        store.hold_automation_run_for_review(
            run.id,
            actor_id=owner.id,
            draft_id=draft.id,
            expected_snapshot_hash="f" * 64,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            detail="Revisión humana obligatoria.",
            engagement_score=81,
            now=NOW + timedelta(seconds=1),
        )
    assert store.list_automation_runs(actor_id=owner.id)[0].status_value is (
        AutomationRunStatus.CLAIMED
    )
    assert store.has_queued_automation_review_notifications(actor_id=owner.id) is False

    notification = store.hold_automation_run_for_review(
        run.id,
        actor_id=owner.id,
        draft_id=draft.id,
        expected_snapshot_hash=revision.snapshot_hash,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        detail="Revisión humana obligatoria.",
        engagement_score=81,
        now=NOW + timedelta(seconds=2),
    )
    claim = store.claim_automation_review_notification(
        actor_id=owner.id,
        lease_seconds=1,
        now=NOW + timedelta(seconds=3),
    )
    assert claim is not None
    expired = store.expire_automation_review_notification_claims(
        actor_id=owner.id,
        now=NOW + timedelta(seconds=5),
    )
    assert [item.id for item in expired] == [notification.id]
    assert expired[0].status_value is AutomationReviewNotificationStatus.UNKNOWN
    assert (
        store.claim_automation_review_notification(
            actor_id=owner.id,
            now=NOW + timedelta(seconds=6),
        )
        is None
    )


def test_automation_claim_is_idempotent_enforces_daily_limit_and_redacts_errors(store) -> None:
    owner, _editor, _reviewer, _publisher, auditor = bootstrap_team(store)
    settings = store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        max_posts_per_day=1,
        slots=[automation_slot(), automation_slot("tarde", at="11:00")],
        now=NOW,
    )
    assert settings.version == 2
    key = "colmat:auto:v1:2026-08-29:manana"
    run = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key=key,
        slot_id="manana",
        scheduled_for=NOW,
        slot_snapshot=automation_slot(),
        now=NOW,
    )
    repeated = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key=key,
        slot_id="manana",
        scheduled_for=NOW,
        slot_snapshot=automation_slot(),
        now=NOW + timedelta(seconds=1),
    )
    assert repeated.id == run.id
    with pytest.raises(ConflictError, match="otro run"):
        store.claim_automation_run(
            actor_id=owner.id,
            idempotency_key=key,
            slot_id="tarde",
            scheduled_for=NOW,
            slot_snapshot=automation_slot("tarde", at="11:00"),
            now=NOW,
        )
    with pytest.raises(ConflictError, match="max_posts_per_day"):
        store.claim_automation_run(
            actor_id=owner.id,
            idempotency_key="colmat:auto:v1:2026-08-29:tarde",
            slot_id="tarde",
            scheduled_for=NOW + timedelta(hours=1),
            slot_snapshot=automation_slot("tarde", at="11:00"),
            now=NOW,
        )

    raw_secret = "provider failed Authorization: Bearer super-secret-token token=abc123456789"
    failed = store.finish_automation_run(
        run.id,
        AutomationRunStatus.FAILED,
        actor_id=owner.id,
        error=raw_secret,
        now=NOW + timedelta(minutes=1),
    )
    assert failed.status_value is AutomationRunStatus.FAILED
    assert failed.finished_by == owner.id
    assert failed.finished_at == NOW + timedelta(minutes=1)
    assert failed.error is not None
    assert "super-secret-token" not in failed.error
    assert "abc123456789" not in failed.error
    assert "[REDACTED]" in failed.error
    assert (
        store.finish_automation_run(
            run.id,
            AutomationRunStatus.FAILED,
            actor_id=owner.id,
            error=raw_secret,
            now=NOW + timedelta(minutes=2),
        ).id
        == run.id
    )
    assert store.list_automation_runs(actor_id=auditor.id)[0].id == run.id
    events = store.list_audit_events(actor_id=auditor.id)
    assert sum(event.action == "automation.run_claimed" for event in events) == 1
    assert sum(event.action == "automation.run_failed" for event in events) == 1
    assert all("super-secret-token" not in json_text(event.detail) for event in events)


def test_automation_run_workflow_attaches_same_workspace_draft(store) -> None:
    owner, editor, reviewer, publisher, auditor = bootstrap_team(store)
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        max_posts_per_day=3,
        slots=[automation_slot("workflow")],
        now=NOW,
    )
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    run = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:workflow",
        slot_id="workflow",
        scheduled_for=NOW,
        slot_snapshot=automation_slot("workflow"),
        draft_id=draft.id,
        now=NOW,
    )
    awaiting = store.finish_automation_run(
        run.id,
        AutomationRunStatus.AWAITING_REVIEW,
        actor_id=owner.id,
        draft_id=draft.id,
        now=NOW + timedelta(seconds=1),
    )
    assert awaiting.finished_at is None
    store.approve_draft(
        draft.id,
        actor_id=reviewer.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW + timedelta(seconds=2),
    )
    store.finish_automation_run(
        run.id,
        AutomationRunStatus.READY,
        actor_id=owner.id,
        now=NOW + timedelta(seconds=3),
    )
    attempt = store.create_publish_attempt(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key="workflow-x",
        now=NOW + timedelta(seconds=4),
    )
    store.finish_automation_run(
        run.id,
        AutomationRunStatus.PUBLISHING,
        actor_id=owner.id,
        now=NOW + timedelta(seconds=5),
    )
    store.finish_publish_attempt(
        attempt.id,
        PublishStatus.SUCCEEDED,
        actor_id=publisher.id,
        provider_post_id="123456789",
        now=NOW + timedelta(seconds=6),
    )
    succeeded = store.finish_automation_run(
        run.id,
        AutomationRunStatus.SUCCEEDED,
        actor_id=owner.id,
        now=NOW + timedelta(seconds=7),
    )
    assert succeeded.draft_id == draft.id
    assert succeeded.finished_at == NOW + timedelta(seconds=7)
    with pytest.raises(ConflictError, match="No se puede cambiar"):
        store.finish_automation_run(
            run.id,
            AutomationRunStatus.FAILED,
            actor_id=owner.id,
            error="late failure",
            now=NOW + timedelta(seconds=8),
        )
    listed = store.list_automation_runs(
        actor_id=auditor.id,
        status=AutomationRunStatus.SUCCEEDED,
        scheduled_from=NOW - timedelta(seconds=1),
        scheduled_to=NOW + timedelta(seconds=1),
    )
    assert [item.id for item in listed] == [run.id]


def test_reconcile_stale_claim_fails_closed_and_never_requeues(store) -> None:
    owner, _editor, _reviewer, _publisher, auditor = bootstrap_team(store)
    slot = automation_slot("stale-claim")
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        slots=[slot],
        now=NOW,
    )
    run = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:stale-claim",
        slot_id="stale-claim",
        scheduled_for=NOW,
        slot_snapshot=slot,
        now=NOW,
    )

    reconciled = store.reconcile_stale_automation_runs(
        actor_id=owner.id,
        stale_before=NOW + timedelta(minutes=30),
        now=NOW + timedelta(minutes=31),
    )

    assert [item.id for item in reconciled] == [run.id]
    assert reconciled[0].status_value is AutomationRunStatus.FAILED
    assert "antes de iniciar" in reconciled[0].error
    repeated = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:stale-claim",
        slot_id="stale-claim",
        scheduled_for=NOW,
        slot_snapshot=slot,
        now=NOW + timedelta(hours=1),
    )
    assert repeated.id == run.id
    assert repeated.status_value is AutomationRunStatus.FAILED
    assert (
        store.reconcile_stale_automation_runs(
            actor_id=owner.id,
            stale_before=NOW + timedelta(hours=2),
            now=NOW + timedelta(hours=2),
        )
        == []
    )
    events = store.list_audit_events(actor_id=auditor.id)
    reconciliation = [event for event in events if event.action == "automation.run_reconciled"]
    assert len(reconciliation) == 1
    assert reconciliation[0].detail["reason"] == "stale_claim_before_publish"
    assert reconciliation[0].detail["attempt_ids"] == []


def test_prepared_automation_snapshot_is_atomic_idempotent_and_recovers_to_review(store) -> None:
    owner, editor, reviewer, _publisher, auditor = bootstrap_team(store)
    slot = automation_slot("prepared-recovery")
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        slots=[slot],
        now=NOW,
    )
    store.bind_telegram_chat(
        -1001234567890,
        telegram_user_id=700000001,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )
    run = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:prepared-recovery",
        slot_id="prepared-recovery",
        scheduled_for=NOW,
        slot_snapshot=slot,
        now=NOW,
    )
    material = {
        "text": "Un dato territorial verificable abre mejores preguntas públicas.",
        "category": "dato_semana",
        "publish_at": NOW,
        "evidence": {"engagement": {"score": 87}, "verification": {"verified": False}},
    }

    persisted_run, draft, revision = store.persist_automation_prepared(
        run.id,
        actor_id=owner.id,
        author_actor_id=editor.id,
        **material,
        now=NOW + timedelta(seconds=1),
    )
    repeated_run, repeated_draft, repeated_revision = store.persist_automation_prepared(
        run.id,
        actor_id=owner.id,
        author_actor_id=editor.id,
        **material,
        now=NOW + timedelta(seconds=2),
    )

    assert persisted_run.draft_id == draft.id
    assert draft.status_value is DraftStatus.IN_REVIEW
    assert (repeated_run.id, repeated_draft.id, repeated_revision.id) == (
        run.id,
        draft.id,
        revision.id,
    )
    with pytest.raises(ConflictError, match="otro snapshot"):
        store.persist_automation_prepared(
            run.id,
            actor_id=owner.id,
            author_actor_id=editor.id,
            **{**material, "text": "Contenido distinto."},
            now=NOW + timedelta(seconds=3),
        )

    reconciled = store.reconcile_stale_automation_runs(
        actor_id=owner.id,
        stale_before=NOW + timedelta(minutes=30),
        now=NOW + timedelta(minutes=31),
    )

    assert [item.id for item in reconciled] == [run.id]
    assert reconciled[0].status_value is AutomationRunStatus.AWAITING_REVIEW
    notification = store.get_automation_review_notification_for_run(
        run.id,
        actor_id=owner.id,
    )
    assert notification.status_value is AutomationReviewNotificationStatus.QUEUED
    assert notification.draft_id == draft.id
    assert notification.revision_id == revision.id
    assert notification.engagement_score == 87
    assert notification.telegram_user_id == "700000001"
    events = store.list_audit_events(actor_id=auditor.id)
    recovery = [event for event in events if event.action == "automation.run_reconciled"]
    assert recovery[-1].detail["reason"] == "stale_prepared_routed_to_review"
    assert recovery[-1].detail["notification_id"] == notification.id


def test_prepared_automation_snapshot_rolls_back_draft_and_run_link(store, monkeypatch) -> None:
    owner, editor, _reviewer, _publisher, _auditor = bootstrap_team(store)
    slot = automation_slot("prepared-rollback")
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        slots=[slot],
        now=NOW,
    )
    run = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:prepared-rollback",
        slot_id="prepared-rollback",
        scheduled_for=NOW,
        slot_snapshot=slot,
        now=NOW,
    )

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("fallo transaccional inyectado")

    monkeypatch.setattr(store, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="inyectado"):
        store.persist_automation_prepared(
            run.id,
            actor_id=owner.id,
            author_actor_id=editor.id,
            text="Este borrador nunca debe quedar huérfano en la base de datos.",
            category="dato_semana",
            publish_at=NOW,
            evidence={"engagement": {"score": 70}},
            now=NOW + timedelta(seconds=1),
        )

    with store.session() as session:
        persisted_run = session.get(type(run), run.id)
        assert persisted_run is not None
        assert persisted_run.draft_id is None
        assert persisted_run.status_value is AutomationRunStatus.CLAIMED
        assert session.scalar(select(func.count(Draft.id))) == 0


def test_prepared_automation_recovery_fails_closed_without_reviewer_binding(store) -> None:
    owner, editor, _reviewer, _publisher, auditor = bootstrap_team(store)
    scheduler = add_member(store, owner.id, Role.SCHEDULER, 2)
    slot = automation_slot("prepared-no-reviewer")
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        slots=[slot],
        now=NOW,
    )
    run = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:prepared-no-reviewer",
        slot_id="prepared-no-reviewer",
        scheduled_for=NOW,
        slot_snapshot=slot,
        now=NOW,
    )
    with pytest.raises(AuthorizationError, match="scheduler no puede ser el autor"):
        store.persist_automation_prepared(
            run.id,
            actor_id=owner.id,
            author_actor_id=owner.id,
            text="Separación de funciones obligatoria para el contenido automático.",
            category="dato_semana",
            publish_at=NOW,
            evidence={},
            now=NOW,
        )
    with pytest.raises(AuthorizationError, match="reclamó el run"):
        store.persist_automation_prepared(
            run.id,
            actor_id=scheduler.id,
            author_actor_id=editor.id,
            text="Solo el scheduler reclamante puede ligar el contenido preparado.",
            category="dato_semana",
            publish_at=NOW,
            evidence={},
            now=NOW,
        )

    _persisted, draft, _revision = store.persist_automation_prepared(
        run.id,
        actor_id=owner.id,
        author_actor_id=editor.id,
        text="El borrador persiste, pero nunca se publica sin un revisor disponible.",
        category="dato_semana",
        publish_at=NOW,
        evidence={"engagement": {"score": "desconocido"}},
        now=NOW + timedelta(seconds=1),
    )
    reconciled = store.reconcile_stale_automation_runs(
        actor_id=owner.id,
        stale_before=NOW + timedelta(minutes=30),
        now=NOW + timedelta(minutes=31),
    )

    assert reconciled[0].status_value is AutomationRunStatus.FAILED
    assert "revisor Telegram activo" in (reconciled[0].error or "")
    assert store.get_draft(draft.id, actor_id=owner.id).status_value is DraftStatus.IN_REVIEW
    events = store.list_audit_events(actor_id=auditor.id)
    reconciliation = [event for event in events if event.action == "automation.run_reconciled"]
    assert reconciliation[-1].detail["reason"] == "stale_prepared_reviewer_missing"


def test_recovered_automation_engagement_score_fails_closed_on_untrusted_evidence() -> None:
    assert _automation_revision_engagement_score(SimpleNamespace(evidence=None)) == 0
    assert (
        _automation_revision_engagement_score(
            SimpleNamespace(evidence={"engagement": "sin estructura"})
        )
        == 0
    )
    assert (
        _automation_revision_engagement_score(
            SimpleNamespace(evidence={"engagement": {"score": True}})
        )
        == 0
    )
    assert (
        _automation_revision_engagement_score(
            SimpleNamespace(evidence={"engagement": {"score": 999}})
        )
        == 100
    )


@pytest.mark.parametrize(
    ("attempt_status", "expected_run_status", "expected_reason"),
    (
        (PublishStatus.SUCCEEDED, AutomationRunStatus.SUCCEEDED, "publish_attempt_succeeded"),
        (PublishStatus.FAILED, AutomationRunStatus.FAILED, "publish_attempt_failed"),
        (PublishStatus.UNKNOWN, AutomationRunStatus.UNKNOWN, "publish_attempt_inconclusive"),
        (PublishStatus.PENDING, AutomationRunStatus.UNKNOWN, "publish_attempt_inconclusive"),
        (None, AutomationRunStatus.UNKNOWN, "publish_attempt_missing"),
    ),
)
def test_reconcile_stale_publishing_uses_only_persisted_attempt_evidence(
    store,
    attempt_status,
    expected_run_status,
    expected_reason,
) -> None:
    owner, editor, reviewer, publisher, auditor = bootstrap_team(store)
    slot = automation_slot("stale-publishing")
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        slots=[slot],
        now=NOW,
    )
    draft, revision, _approval = approved_draft(store, editor.id, reviewer.id)
    run = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:stale-publishing",
        slot_id="stale-publishing",
        scheduled_for=NOW,
        slot_snapshot=slot,
        draft_id=draft.id,
        now=NOW,
    )
    attempt = store.create_publish_attempt(
        draft.id,
        actor_id=publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key="stale-publishing-x",
        now=NOW + timedelta(seconds=1),
    )
    store.finish_automation_run(
        run.id,
        AutomationRunStatus.PUBLISHING,
        actor_id=owner.id,
        draft_id=draft.id,
        now=NOW + timedelta(seconds=2),
    )
    if attempt_status is None:
        with store.session() as session:
            session.delete(session.get(PublishAttempt, attempt.id))
            session.commit()
    elif attempt_status is not PublishStatus.PENDING:
        store.finish_publish_attempt(
            attempt.id,
            attempt_status,
            actor_id=publisher.id,
            provider_post_id=(
                "190000000000000099" if attempt_status is PublishStatus.SUCCEEDED else None
            ),
            error=(None if attempt_status is PublishStatus.SUCCEEDED else "resultado local"),
            now=NOW + timedelta(seconds=3),
        )

    reconciled = store.reconcile_stale_automation_runs(
        actor_id=owner.id,
        stale_before=NOW + timedelta(minutes=30),
        now=NOW + timedelta(minutes=31),
    )

    assert len(reconciled) == 1
    assert reconciled[0].id == run.id
    assert reconciled[0].status_value is expected_run_status
    assert reconciled[0].finished_at == NOW + timedelta(minutes=31)
    assert (
        store.reconcile_stale_automation_runs(
            actor_id=owner.id,
            stale_before=NOW + timedelta(hours=1),
            now=NOW + timedelta(hours=1),
        )
        == []
    )
    event = next(
        event
        for event in reversed(store.list_audit_events(actor_id=auditor.id))
        if event.action == "automation.run_reconciled"
    )
    assert event.detail["reason"] == expected_reason
    assert event.detail["status"] == expected_run_status.value
    assert event.detail["attempt_ids"] == ([] if attempt_status is None else [attempt.id])


def test_reconcile_ignores_fresh_and_human_review_runs(store) -> None:
    owner, editor, _reviewer, _publisher, _auditor = bootstrap_team(store)
    slots = [automation_slot("fresh"), automation_slot("waiting", at="11:00")]
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        max_posts_per_day=2,
        slots=slots,
        now=NOW,
    )
    fresh = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:fresh",
        slot_id="fresh",
        scheduled_for=NOW,
        slot_snapshot=slots[0],
        now=NOW + timedelta(minutes=20),
    )
    draft, revision = new_draft(store, editor.id)
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    waiting = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:waiting",
        slot_id="waiting",
        scheduled_for=NOW + timedelta(hours=1),
        slot_snapshot=slots[1],
        draft_id=draft.id,
        now=NOW,
    )
    store.finish_automation_run(
        waiting.id,
        AutomationRunStatus.AWAITING_REVIEW,
        actor_id=owner.id,
        now=NOW,
    )

    assert (
        store.reconcile_stale_automation_runs(
            actor_id=owner.id,
            stale_before=NOW + timedelta(minutes=15),
            now=NOW + timedelta(minutes=30),
        )
        == []
    )
    runs = {run.id: run for run in store.list_automation_runs(actor_id=owner.id)}
    assert runs[fresh.id].status_value is AutomationRunStatus.CLAIMED
    assert runs[waiting.id].status_value is AutomationRunStatus.AWAITING_REVIEW


def test_direct_runs_recheck_runtime_kill_switch(store, monkeypatch) -> None:
    owner, _ = store.bootstrap_owner(email="owner@colmat.test", display_name="Owner", now=NOW)
    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        mode=AutomationMode.DIRECT,
        max_posts_per_day=2,
        slots=[
            automation_slot("directo", mode="direct"),
            automation_slot("directo-tarde", at="11:00", mode="direct"),
        ],
        now=NOW,
    )
    first = store.claim_automation_run(
        actor_id=owner.id,
        idempotency_key="colmat:auto:v1:2026-08-29:directo",
        slot_id="directo",
        scheduled_for=NOW,
        slot_snapshot=automation_slot("directo", mode="direct"),
        mode=AutomationMode.DIRECT,
        now=NOW,
    )
    assert first.mode_value is AutomationMode.DIRECT
    monkeypatch.delenv("COLMAT_DIRECT_PUBLISH_ENABLED")
    with pytest.raises(ConflictError, match="COLMAT_DIRECT_PUBLISH_ENABLED"):
        store.claim_automation_run(
            actor_id=owner.id,
            idempotency_key="colmat:auto:v1:2026-08-29:directo-tarde",
            slot_id="directo-tarde",
            scheduled_for=NOW + timedelta(hours=1),
            slot_snapshot=automation_slot("directo-tarde", at="11:00", mode="direct"),
            mode=AutomationMode.DIRECT,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("timezone", "Mars/Olympus"),
        ("slots", {"id": "not-a-list"}),
        ("min_engagement_score", 101),
        ("max_posts_per_day", 0),
        ("generate_images", 1),
    ),
)
def test_automation_settings_reject_invalid_values(store, field, value) -> None:
    owner, _ = store.bootstrap_owner(email="owner@colmat.test", display_name="Owner", now=NOW)
    with pytest.raises(ValueError):
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            **{field: value},
        )


def test_legacy_sqlite_migration_is_idempotent_and_preserves_data_and_fks(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-platform.db"
    timestamp = "2026-08-29 15:00:00.000000"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE users (
                id VARCHAR(36) PRIMARY KEY,
                email VARCHAR(320) NOT NULL UNIQUE,
                display_name VARCHAR(120) NOT NULL,
                password_hash VARCHAR(255),
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT ck_users_email_lowercase CHECK (email = lower(email))
            );
            CREATE TABLE memberships (
                id VARCHAR(36) PRIMARY KEY,
                workspace_id VARCHAR(80) NOT NULL,
                user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role VARCHAR(20) NOT NULL,
                created_by VARCHAR(80) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_membership_workspace_user UNIQUE (workspace_id, user_id),
                CONSTRAINT ck_memberships_role CHECK (
                    role IN ('owner', 'admin', 'editor', 'reviewer', 'publisher', 'auditor')
                )
            );
            CREATE INDEX ix_memberships_workspace_role
                ON memberships(workspace_id, role);
            CREATE TABLE telegram_updates (
                update_id BIGINT PRIMARY KEY,
                workspace_id VARCHAR(80) NOT NULL,
                chat_id VARCHAR(32),
                telegram_user_id VARCHAR(32),
                actor_id VARCHAR(80) NOT NULL,
                payload JSON NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'received',
                received_at DATETIME NOT NULL,
                processed_at DATETIME,
                error TEXT,
                CONSTRAINT ck_telegram_updates_status CHECK (
                    status IN ('received', 'processed', 'failed')
                )
            );
            """
        )
        connection.execute(
            """
            INSERT INTO users (
                id, email, display_name, password_hash, is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-owner",
                "legacy-owner@colmat.test",
                "Legacy Owner",
                "$argon2id$legacy",
                True,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO telegram_updates (
                update_id, workspace_id, actor_id, payload, status, received_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (77, "colmat", "service:telegram", '{"update_id":77}', "received", timestamp),
        )
        connection.execute(
            """
            INSERT INTO memberships (
                id, workspace_id, user_id, role, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-membership",
                "colmat",
                "legacy-owner",
                "owner",
                "system:bootstrap",
                timestamp,
                timestamp,
            ),
        )

    database_url = f"sqlite+pysqlite:///{database}"
    with PlatformStore(database_url) as migrated:
        owner = migrated.get_user("legacy-owner")
        assert owner.display_name == "Legacy Owner"
        assert owner.password_hash == "$argon2id$legacy"
        assert owner.username is None
        migrated.set_username(
            owner.id,
            "Legacy.Owner",
            actor_id=owner.id,
            now=NOW,
        )
        scheduler = migrated.create_user(
            actor_id=owner.id,
            email="scheduler@colmat.test",
            display_name="Scheduler",
            user_id="legacy-scheduler",
            now=NOW,
        )
        migrated.grant_membership(
            scheduler.id,
            Role.SCHEDULER,
            actor_id=owner.id,
            now=NOW,
        )
        settings = migrated.get_automation_settings(actor_id=owner.id)
        assert settings.enabled is False
        assert settings.mode_value is AutomationMode.HUMAN_REVIEW

    # Una segunda apertura ejecuta exactamente la misma ruta de migración.
    with PlatformStore(database_url) as reopened:
        assert reopened.get_user("legacy-owner").username == "legacy.owner"
        memberships = reopened.list_memberships(actor_id="legacy-owner")
        assert {(item.user_id, item.role) for item in memberships} == {
            ("legacy-owner", Role.OWNER.value),
            ("legacy-scheduler", Role.SCHEDULER.value),
        }
        settings = reopened.get_automation_settings(actor_id="legacy-owner")
        assert settings.workspace_id == "colmat"

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'publication_requests'"
            ).fetchone()[0]
            == 1
        )
        membership_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memberships'"
        ).fetchone()[0]
        assert "scheduler" in membership_sql
        assert connection.execute("SELECT count(*) FROM automation_settings").fetchone()[0] == 1
        telegram_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(telegram_updates)")
        }
        assert {
            "claim_token_hash",
            "claim_fence",
            "attempt_count",
            "claimed_at",
            "lease_expires_at",
            "prepared_actions",
            "business_result",
        } <= telegram_columns
        migrated_update = connection.execute(
            """
            SELECT claim_token_hash, claim_fence, attempt_count, claimed_at,
                   lease_expires_at, prepared_actions, business_result
            FROM telegram_updates WHERE update_id = 77
            """
        ).fetchone()
        assert migrated_update == (None, 0, 0, None, None, None, None)
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'index' AND name = 'ix_telegram_updates_lease'"
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO memberships (
                    id, workspace_id, user_id, role, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "invalid-membership",
                    "colmat",
                    "missing-user",
                    "scheduler",
                    "legacy-owner",
                    timestamp,
                    timestamp,
                ),
            )


def test_postgres_ddl_is_repeatable_and_migrates_existing_user_and_role_schema() -> None:
    ddl = Path("deploy/postgres.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE users ADD COLUMN IF NOT EXISTS username" in ddl
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username" in ddl
    assert "ALTER TABLE memberships DROP CONSTRAINT IF EXISTS ck_memberships_role" in ddl
    assert "'scheduler'" in ddl
    assert "CREATE TABLE IF NOT EXISTS automation_settings" in ddl
    assert "CREATE TABLE IF NOT EXISTS automation_runs" in ddl
    assert "CREATE TABLE IF NOT EXISTS publication_requests" in ddl
    for column in (
        "claim_token_hash",
        "claim_fence",
        "attempt_count",
        "claimed_at",
        "lease_expires_at",
        "prepared_actions",
        "business_result",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in ddl
    assert "ix_telegram_updates_lease" in ddl
    assert "ck_telegram_updates_claim_fence" in ddl
    assert "ck_telegram_updates_attempt_count" in ddl
    assert "ck_telegram_updates_claim_token_hash_length" in ddl
    assert "uq_publication_request_workspace_channel_key" in ddl
    assert "uq_publication_request_snapshot" in ddl
    assert "udt_name <> 'jsonb'" in ddl
    assert "ON CONFLICT (workspace_id) DO NOTHING" in ddl
    assert "'expected_figure', NULL" in ddl
    assert "'expected_source', NULL" in ddl
    assert "'verified', FALSE" in ddl
    assert "jsonb_object_length" not in ddl
    assert "FROM jsonb_object_keys(item -> 'evidence')" in ddl
    assert "FILTER (WHERE jsonb_typeof(item) = 'object')" in ddl
    assert "ELSE item" not in ddl
    assert "version = settings.version + 1" in ddl
    assert "direct_authorized_by = NULL" in ddl
    assert "enabled = FALSE" in ddl


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
