from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest

from colmat_x.domain import PostStatus
from colmat_x.state import (
    PublicationInProgress,
    ReconciliationRequired,
    StateError,
    StateStore,
)
from tests.factories import NOW, make_post


def sync_and_approve(store: StateStore, post=None) -> None:
    selected = post or make_post()
    store.sync_posts([selected], now=NOW)
    store.approve(
        selected.id,
        "Equipo editorial",
        selected.approval_snapshot_hash,
        now=NOW,
    )


def test_sync_creates_only_drafts_and_approve_schedules_snapshot(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    first = make_post()
    second = make_post(post_id="colmat-test-002", text="Otro borrador")

    summary = store.sync_posts([first, second], now=NOW)
    approval_hash = store.approve(
        first.id, "Equipo editorial", first.approval_snapshot_hash, now=NOW
    )

    assert summary.inserted == 2
    assert len(store.list_posts(PostStatus.SCHEDULED)) == 1
    assert len(store.list_posts(PostStatus.DRAFT)) == 1
    approved = store.list_posts(PostStatus.SCHEDULED)[0]
    assert approved.approved_by == "Equipo editorial"
    assert approved.approved_at == NOW
    assert approved.approval_hash == approval_hash


def test_material_change_invalidates_existing_approval(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    original = make_post()
    sync_and_approve(store, original)

    changed = make_post(text="Texto nuevo que necesita otra revisión")
    summary = store.sync_posts([changed], now=NOW)

    stored = store.list_posts()[0]
    assert summary.updated == 1
    assert stored.status == PostStatus.DRAFT
    assert stored.approved_by is None
    assert stored.approval_hash is None
    assert store.claim(changed.id, NOW, lease_minutes=10) is None


def test_approve_rejects_snapshot_changed_after_preview(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    previewed = make_post(text="Texto que vio el revisor")
    changed = make_post(text="Texto modificado después de preview")
    store.sync_posts([previewed], now=NOW)
    store.sync_posts([changed], now=NOW)

    with pytest.raises(StateError, match="snapshot cambió"):
        store.approve(
            changed.id,
            "Equipo",
            previewed.approval_snapshot_hash,
            now=NOW,
        )

    assert store.list_posts()[0].status == PostStatus.DRAFT


def test_claim_revalidates_approval_hash_inside_the_transaction(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    post = make_post()
    sync_and_approve(store, post)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            "UPDATE posts SET rendered_text = ? WHERE id = ?",
            ("Texto alterado fuera del flujo editorial", post.id),
        )

    assert store.claim(post.id, NOW, lease_minutes=10) is None

    stored = store.list_posts()[0]
    assert stored.status == PostStatus.DRAFT
    assert stored.approval_hash is None


def test_claim_is_atomic_and_publication_is_idempotent(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    post = make_post()
    sync_and_approve(store, post)

    first = store.claim(post.id, NOW, lease_minutes=10)

    assert first is not None
    assert first.status == PostStatus.PUBLISHING
    with pytest.raises(PublicationInProgress):
        store.claim(post.id, NOW, lease_minutes=10)
    store.mark_published(
        post.id,
        "190000000000000001",
        expected_attempt=first.attempts,
        now=NOW,
    )
    stored = store.list_posts()[0]
    assert stored.status == PostStatus.PUBLISHED
    assert stored.x_post_id == "190000000000000001"
    assert store.due_posts(NOW, 10) == []


def test_unknown_atomically_blocks_every_other_claim(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    first = make_post()
    second = make_post(post_id="colmat-test-002", text="Una segunda idea")
    store.sync_posts([first, second], now=NOW)
    store.approve(first.id, "Equipo", first.approval_snapshot_hash, now=NOW)
    store.approve(second.id, "Equipo", second.approval_snapshot_hash, now=NOW)
    first_claim = store.claim(first.id, NOW, lease_minutes=10)
    assert first_claim is not None
    store.mark_unknown(
        first.id,
        "timeout",
        expected_attempt=first_claim.attempts,
        now=NOW,
    )

    with pytest.raises(ReconciliationRequired):
        store.claim(second.id, NOW, lease_minutes=10)

    assert store.list_posts(PostStatus.SCHEDULED)[0].id == second.id


def test_expired_lease_becomes_unknown_not_automatic_retry(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    post = make_post()
    sync_and_approve(store, post)
    assert store.claim(post.id, NOW, lease_minutes=10)

    recovered = store.recover_expired_leases(NOW + timedelta(minutes=11))

    assert recovered == 1
    assert store.list_posts()[0].status == PostStatus.UNKNOWN
    with pytest.raises(StateError, match="Solo se puede reintentar"):
        store.retry(post.id)
    store.retry(post.id, confirm_not_published=True)
    assert store.list_posts()[0].status == PostStatus.SCHEDULED


def test_stale_attempt_cannot_overwrite_a_new_claim(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    post = make_post()
    sync_and_approve(store, post)
    first = store.claim(post.id, NOW, lease_minutes=10)
    assert first is not None
    store.recover_expired_leases(NOW + timedelta(minutes=11))
    store.retry(post.id, confirm_not_published=True)
    changed = make_post(text="Snapshot nuevo después del resultado ambiguo")
    store.sync_posts([changed], now=NOW + timedelta(minutes=11, seconds=30))
    store.approve(
        changed.id,
        "Equipo",
        changed.approval_snapshot_hash,
        now=NOW + timedelta(minutes=11, seconds=45),
    )
    second = store.claim(changed.id, NOW + timedelta(minutes=12), lease_minutes=10)
    assert second is not None
    assert second.attempts == first.attempts + 1

    with pytest.raises(StateError, match="ya no es la publicación activa"):
        store.mark_failed(
            post.id,
            "callback tardío",
            expected_attempt=first.attempts,
            now=NOW + timedelta(minutes=13),
        )

    active = store.list_posts()[0]
    assert active.status == PostStatus.PUBLISHING
    assert active.attempts == second.attempts


def test_published_post_cannot_be_silently_changed(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    post = make_post()
    sync_and_approve(store, post)
    claim = store.claim(post.id, NOW, lease_minutes=10)
    assert claim is not None
    store.mark_published(
        post.id,
        "190000000000000002",
        expected_attempt=claim.attempts,
        now=NOW,
    )

    changed = make_post(text="Texto cambiado")
    with pytest.raises(StateError, match="no se puede cambiar"):
        store.sync_posts([changed], now=NOW)


def test_reconcile_unknown_as_published(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    post = make_post()
    sync_and_approve(store, post)
    claim = store.claim(post.id, NOW, lease_minutes=10)
    assert claim is not None
    store.mark_unknown(post.id, "timeout", expected_attempt=claim.attempts, now=NOW)

    store.reconcile_as_published(post.id, "190000000000000003")

    stored = store.list_posts()[0]
    assert stored.status == PostStatus.PUBLISHED
    assert stored.x_post_id == "190000000000000003"


def test_sync_cancels_missing_source_and_restore_requires_new_approval(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    post = make_post()
    sync_and_approve(store, post)

    summary = store.sync_posts([], now=NOW)
    restored = make_post(
        text="Texto restaurado que requiere revisión",
        publish_at=NOW + timedelta(days=1),
    )
    store.restore_cancelled(restored, now=NOW)

    assert summary.cancelled == 1
    stored = store.list_posts()[0]
    assert stored.status == PostStatus.DRAFT
    assert stored.rendered_text == restored.text
    assert stored.scheduled_at_utc == restored.publish_at_utc
    assert stored.approval_hash is None
    assert store.due_posts(NOW, 10) == []


def test_two_connections_can_claim_only_one_post_at_a_time(project_settings) -> None:
    first_store = StateStore(project_settings.paths.state_db)
    second_store = StateStore(project_settings.paths.state_db)
    first = make_post()
    second = make_post(post_id="colmat-test-002", text="Otra pieza concurrente")
    first_store.sync_posts([first, second], now=NOW)
    first_store.approve(first.id, "Equipo", first.approval_snapshot_hash, now=NOW)
    first_store.approve(second.id, "Equipo", second.approval_snapshot_hash, now=NOW)
    barrier = Barrier(2)

    def attempt(store: StateStore, post_id: str) -> str:
        barrier.wait(timeout=5)
        try:
            claimed = store.claim(post_id, NOW, lease_minutes=10)
        except PublicationInProgress:
            return "busy"
        return "claimed" if claimed else "missed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: attempt(*pair),
                ((first_store, first.id), (second_store, second.id)),
            )
        )

    assert sorted(outcomes) == ["busy", "claimed"]
    assert len(first_store.list_posts(PostStatus.PUBLISHING)) == 1


def test_legacy_approval_is_invalidated_during_migration(tmp_path: Path) -> None:
    state_dir = tmp_path / "legacy-state"
    state_dir.mkdir(mode=0o700)
    state_dir.chmod(0o700)
    database = state_dir / "legacy.db"
    timestamp = NOW.isoformat()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """
                CREATE TABLE posts (
                    id TEXT PRIMARY KEY,
                    scheduled_at_utc TEXT NOT NULL,
                    rendered_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    source_path TEXT NOT NULL,
                    approved_by TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_until TEXT,
                    x_post_id TEXT UNIQUE,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT
                )
                """
        )
        connection.execute(
            """
                INSERT INTO posts (
                    id, scheduled_at_utc, rendered_text, content_hash, source_path,
                    approved_by, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
            (
                "colmat-legacy-001",
                timestamp,
                "Aprobación antigua",
                "legacy-hash",
                "legacy.yaml",
                "Revisor legado",
                PostStatus.SCHEDULED.value,
                timestamp,
                timestamp,
            ),
        )

    migrated = StateStore(database).list_posts()[0]

    assert migrated.status == PostStatus.DRAFT
    assert migrated.approved_by is None
    assert migrated.approval_hash is None


def test_store_never_changes_permissions_of_existing_parent(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)

    with pytest.raises(StateError, match="debe tener permisos 0700"):
        StateStore(shared / "colmat.db")

    assert shared.stat().st_mode & 0o777 == 0o755
    assert not (shared / "colmat.db").exists()


@pytest.mark.parametrize(
    "invalid_x_id",
    ["https://x.com/colmat/status/123", "00123", "１２３", "0", "1" * 21],
)
def test_reconciliation_requires_canonical_x_id(project_settings, invalid_x_id: str) -> None:
    store = StateStore(project_settings.paths.state_db)
    first = make_post()
    sync_and_approve(store, first)
    claim = store.claim(first.id, NOW, lease_minutes=10)
    assert claim is not None
    store.mark_unknown(first.id, "timeout", expected_attempt=claim.attempts, now=NOW)

    with pytest.raises(StateError, match="dígitos ASCII"):
        store.reconcile_as_published(first.id, invalid_x_id)


def test_reconciliation_requires_unique_x_id(project_settings) -> None:
    store = StateStore(project_settings.paths.state_db)
    first = make_post()
    second = make_post(post_id="colmat-test-002", text="Una segunda idea")
    store.sync_posts([first, second], now=NOW)
    store.approve(first.id, "Equipo", first.approval_snapshot_hash, now=NOW)
    first_claim = store.claim(first.id, NOW, lease_minutes=10)
    assert first_claim is not None
    store.mark_unknown(
        first.id,
        "timeout",
        expected_attempt=first_claim.attempts,
        now=NOW,
    )

    store.reconcile_as_published(first.id, "190000000000000003")
    store.approve(second.id, "Equipo", second.approval_snapshot_hash, now=NOW)
    second_claim = store.claim(second.id, NOW, lease_minutes=10)
    assert second_claim is not None
    store.mark_unknown(
        second.id,
        "timeout",
        expected_attempt=second_claim.attempts,
        now=NOW,
    )
    with pytest.raises(StateError, match="UNIQUE constraint failed"):
        store.reconcile_as_published(second.id, "190000000000000003")
