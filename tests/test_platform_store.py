from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import func, select  # noqa: E402

from colmat_x.platform_store import (  # noqa: E402
    Approval,
    AuditEvent,
    Base,
    CallbackAction,
    CallbackIntent,
    ConflictError,
    DraftStatus,
    PlatformStore,
    PublishStatus,
    Revision,
    StaleSnapshotError,
    TelegramUpdate,
    approval_snapshot_hash,
    resolve_database_url,
    table_names,
)
from colmat_x.rbac import (  # noqa: E402
    AuthorizationError,
    Role,
    SeparationOfDutiesError,
)
from colmat_x.telegram_bot import BotPermission, CallbackDecision  # noqa: E402

NOW = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
PUBLISH_AT = NOW + timedelta(hours=2)
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


def new_draft(store: PlatformStore, editor_id: str):
    return store.create_draft(
        actor_id=editor_id,
        text="La educación transforma posibilidades en decisiones.",
        category="educacion",
        publish_at=PUBLISH_AT,
        evidence={"manual": "capitulo-2", "fuentes": ["estudio-7"]},
        image_sha256=IMAGE_HASH,
        now=NOW,
    )


def test_schema_contains_all_platform_tables() -> None:
    assert set(table_names()) == {
        "approvals",
        "audit_events",
        "callback_intents",
        "drafts",
        "media_assets",
        "memberships",
        "publish_attempts",
        "revisions",
        "telegram_bindings",
        "telegram_updates",
        "users",
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


def test_last_owner_is_protected(store) -> None:
    owner, _ = store.bootstrap_owner(email="owner@colmat.test", display_name="Owner", now=NOW)

    with pytest.raises(ConflictError, match="al menos un owner"):
        store.revoke_membership(owner.id, actor_id=owner.id, now=NOW)


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
