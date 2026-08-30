from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    case,
    create_engine,
    event,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import DateTime, TypeDecorator

from colmat_x.editorial import EditorialCategory, Institution
from colmat_x.rbac import (
    AuthorizationError,
    Permission,
    Role,
    require_distinct_approver,
    require_permission,
    require_role_assignment,
    roles_with,
)

DEFAULT_WORKSPACE_ID = "colmat"
DEFAULT_DATABASE_PATH = Path(".state/colmat-platform.db")
SYSTEM_ACTOR = "system:bootstrap"
TELEGRAM_ACTOR = "service:telegram"
CALLBACK_MAX_LIFETIME = timedelta(hours=24)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")
DEFAULT_AUTOMATION_TIMEZONE = "America/Bogota"
DEFAULT_MAX_POSTS_PER_DAY = 2
CANONICAL_AUTOMATION_WEEKDAYS = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)
DIRECT_PUBLISH_ENV = "COLMAT_DIRECT_PUBLISH_ENABLED"
MAX_AUTOMATION_SLOTS = 100
MAX_AUTOMATION_ERROR_LENGTH = 1000
DEFAULT_PUBLICATION_LEASE_SECONDS = 300
MAX_PUBLICATION_LEASE_SECONDS = 3600
DEFAULT_GENERATION_LEASE_SECONDS = 900
MAX_GENERATION_LEASE_SECONDS = 3600
DEFAULT_GENERATION_NOTIFICATION_LEASE_SECONDS = 120
MAX_GENERATION_NOTIFICATION_LEASE_SECONDS = 600
DEFAULT_AUTOMATION_REVIEW_NOTIFICATION_LEASE_SECONDS = 120
MAX_AUTOMATION_REVIEW_NOTIFICATION_LEASE_SECONDS = 600
DEFAULT_TELEGRAM_UPDATE_LEASE_SECONDS = 30
MAX_TELEGRAM_UPDATE_LEASE_SECONDS = 300
_UNSET = object()
_RBAC_LOCKS_GUARD = threading.Lock()
_RBAC_LOCKS: dict[str, threading.RLock] = {}
_RBAC_USER_LOCKS: dict[str, threading.RLock] = {}


class PlatformStoreError(RuntimeError):
    """Error base de persistencia de la plataforma."""


class NotFoundError(PlatformStoreError):
    """La entidad solicitada no existe."""


class ConflictError(PlatformStoreError):
    """La operación contradice el estado persistido."""


class StaleSnapshotError(ConflictError):
    """El contenido ya no coincide con el snapshot que se revisó."""


class DraftStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class UpdateStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"


class PublishStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PublicationRequestStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class GenerationRequestStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class GenerationNotificationStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AutomationReviewNotificationStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AutomationMode(StrEnum):
    HUMAN_REVIEW = "human_review"
    DIRECT = "direct"


class AutomationRunStatus(StrEnum):
    CLAIMED = "claimed"
    AWAITING_REVIEW = "awaiting_review"
    READY = "ready"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


_AUTOMATION_RUN_TRANSITIONS: Mapping[AutomationRunStatus, frozenset[AutomationRunStatus]] = {
    AutomationRunStatus.CLAIMED: frozenset(
        {
            AutomationRunStatus.AWAITING_REVIEW,
            AutomationRunStatus.PUBLISHING,
            AutomationRunStatus.FAILED,
        }
    ),
    AutomationRunStatus.AWAITING_REVIEW: frozenset(
        {AutomationRunStatus.READY, AutomationRunStatus.FAILED}
    ),
    AutomationRunStatus.READY: frozenset(
        {AutomationRunStatus.PUBLISHING, AutomationRunStatus.FAILED}
    ),
    AutomationRunStatus.PUBLISHING: frozenset(
        {
            AutomationRunStatus.SUCCEEDED,
            AutomationRunStatus.FAILED,
            AutomationRunStatus.UNKNOWN,
        }
    ),
    AutomationRunStatus.SUCCEEDED: frozenset(),
    AutomationRunStatus.FAILED: frozenset(),
    AutomationRunStatus.UNKNOWN: frozenset(),
}


class CallbackAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    PUBLISH = "publish"


@dataclass(frozen=True)
class IssuedCallbackIntent:
    """Nonce entregable a Telegram y registro persistido que lo limita."""

    nonce: str = field(repr=False)
    intent: CallbackIntent


class UTCDateTime(TypeDecorator[datetime]):
    """TIMESTAMPTZ en PostgreSQL y datetimes UTC conscientes también en SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Las fechas persistidas deben incluir zona horaria")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


def _new_id() -> str:
    return str(uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _workspace_rbac_lock(workspace_id: str) -> Iterator[None]:
    """Serializa mutaciones RBAC del mismo workspace dentro del proceso."""

    with _RBAC_LOCKS_GUARD:
        lock = _RBAC_LOCKS.setdefault(workspace_id, threading.RLock())
    with lock:
        yield


@contextmanager
def _user_rbac_lock(user_id: str) -> Iterator[None]:
    """Serializa cambios globales y memberships de una misma identidad."""

    with _RBAC_LOCKS_GUARD:
        lock = _RBAC_USER_LOCKS.setdefault(user_id, threading.RLock())
    with lock:
        yield


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        CheckConstraint(
            "username IS NULL OR username = lower(username)",
            name="ck_users_username_lowercase",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utc_now, onupdate=_utc_now
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_membership_workspace_user"),
        CheckConstraint(
            "role IN ('owner', 'admin', 'editor', 'reviewer', 'publisher', 'scheduler', 'auditor')",
            name="ck_memberships_role",
        ),
        Index("ix_memberships_workspace_role", "workspace_id", "role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    @property
    def role_value(self) -> Role:
        return Role(self.role)


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'in_review', 'approved', 'rejected', 'published')",
            name="ck_drafts_status",
        ),
        Index("ix_drafts_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=DraftStatus.DRAFT.value)
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    current_revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approved_revision_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    @property
    def status_value(self) -> DraftStatus:
        return DraftStatus(self.status)


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (
        UniqueConstraint("draft_id", "revision_number", name="uq_revision_draft_number"),
        Index("ix_revisions_draft_created", "draft_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    publish_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    evidence: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    image_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("decision IN ('approved', 'rejected')", name="ck_approvals_decision"),
        Index("ix_approvals_draft_created", "draft_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("revisions.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)

    @property
    def decision_value(self) -> ApprovalDecision:
        return ApprovalDecision(self.decision)


class TelegramBinding(Base):
    __tablename__ = "telegram_bindings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "telegram_user_id",
            "chat_id",
            name="uq_telegram_binding_identity_chat",
        ),
        Index(
            "ix_telegram_binding_identity",
            "workspace_id",
            "telegram_user_id",
            "is_active",
        ),
        CheckConstraint(
            "purpose IN ('control', 'review', 'alerts')",
            name="ck_telegram_bindings_purpose",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    telegram_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, default="control")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utc_now, onupdate=_utc_now
    )


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('received', 'processed', 'failed')",
            name="ck_telegram_updates_status",
        ),
        Index("ix_telegram_updates_status_received", "status", "received_at"),
        Index("ix_telegram_updates_lease", "status", "lease_expires_at"),
        CheckConstraint("claim_fence >= 0", name="ck_telegram_updates_claim_fence"),
        CheckConstraint("attempt_count >= 0", name="ck_telegram_updates_attempt_count"),
        CheckConstraint(
            "claim_token_hash IS NULL OR length(claim_token_hash) = 64",
            name="ck_telegram_updates_claim_token_hash_length",
        ),
    )

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(String(32))
    telegram_user_id: Mapped[str | None] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=UpdateStatus.RECEIVED.value
    )
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    claim_token_hash: Mapped[str | None] = mapped_column(String(64))
    claim_fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    prepared_actions: Mapped[Any | None] = mapped_column(JSON)
    business_result: Mapped[Any | None] = mapped_column(JSON)


class CallbackIntent(Base):
    __tablename__ = "callback_intents"
    __table_args__ = (
        CheckConstraint(
            "action IN ('approve', 'reject', 'publish')",
            name="ck_callback_intents_action",
        ),
        UniqueConstraint("nonce_hash", name="uq_callback_intents_nonce_hash"),
        Index("ix_callback_intents_expiry", "expires_at", "consumed_at"),
        Index("ix_callback_intents_draft", "draft_id", "revision_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("revisions.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    telegram_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consumed_by: Mapped[str | None] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)

    @property
    def action_value(self) -> CallbackAction:
        return CallbackAction(self.action)


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "sha256", name="uq_media_workspace_sha256"),
        Index("ix_media_assets_draft", "draft_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    draft_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    asset_metadata: Mapped[Any] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "channel",
            "idempotency_key",
            name="uq_publish_workspace_channel_key",
        ),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'unknown')",
            name="ck_publish_attempts_status",
        ),
        Index("ix_publish_attempts_draft_started", "draft_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("revisions.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PublishStatus.PENDING.value
    )
    provider_post_id: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def status_value(self) -> PublishStatus:
        return PublishStatus(self.status)


class PublicationRequest(Base):
    __tablename__ = "publication_requests"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "channel",
            "idempotency_key",
            name="uq_publication_request_workspace_channel_key",
        ),
        UniqueConstraint(
            "workspace_id",
            "channel",
            "draft_id",
            "revision_id",
            name="uq_publication_request_snapshot",
        ),
        UniqueConstraint(
            "publish_attempt_id",
            name="uq_publication_request_publish_attempt",
        ),
        UniqueConstraint(
            "claim_token_hash",
            name="uq_publication_request_claim_token_hash",
        ),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'succeeded', 'failed', 'unknown')",
            name="ck_publication_requests_status",
        ),
        CheckConstraint(
            "length(snapshot_hash) = 64",
            name="ck_publication_requests_snapshot_hash_length",
        ),
        CheckConstraint(
            "claim_token_hash IS NULL OR length(claim_token_hash) = 64",
            name="ck_publication_requests_claim_token_hash_length",
        ),
        CheckConstraint(
            "claim_fence >= 0",
            name="ck_publication_requests_claim_fence",
        ),
        CheckConstraint(
            "(status = 'queued' AND claim_fence = 0 AND claim_token_hash IS NULL "
            "AND claimed_by IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL) OR "
            "(status <> 'queued' AND claim_fence >= 1 AND claim_token_hash IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_publication_requests_claim_state",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > claimed_at",
            name="ck_publication_requests_lease",
        ),
        CheckConstraint(
            "(status IN ('queued', 'claimed') AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'unknown') AND finished_at IS NOT NULL)",
            name="ck_publication_requests_finished",
        ),
        CheckConstraint(
            "(status IN ('queued', 'claimed') AND publish_attempt_id IS NULL) OR "
            "status IN ('succeeded', 'failed', 'unknown')",
            name="ck_publication_requests_attempt_state",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed') OR publish_attempt_id IS NOT NULL",
            name="ck_publication_requests_final_attempt",
        ),
        CheckConstraint(
            "error IS NULL OR length(error) <= 1000",
            name="ck_publication_requests_error_length",
        ),
        Index(
            "ix_publication_requests_queue",
            "workspace_id",
            "channel",
            "status",
            "created_at",
        ),
        Index(
            "ix_publication_requests_lease",
            "workspace_id",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("revisions.id", ondelete="RESTRICT"), nullable=False
    )
    approval_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PublicationRequestStatus.QUEUED.value
    )
    claim_token_hash: Mapped[str | None] = mapped_column(String(64))
    claim_fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    publish_attempt_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("publish_attempts.id", ondelete="RESTRICT")
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def status_value(self) -> PublicationRequestStatus:
        return PublicationRequestStatus(self.status)


@dataclass(frozen=True, slots=True)
class PublicationClaim:
    request: PublicationRequest
    claim_token: str = field(repr=False)
    claim_fence: int
    lease_expires_at: datetime
    publish_attempt_idempotency_key: str


class GenerationRequest(Base):
    """Solicitud durable; Vercel solo escribe esta fila y nunca invoca MiniMax."""

    __tablename__ = "generation_requests"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_generation_request_workspace_key",
        ),
        UniqueConstraint("claim_token_hash", name="uq_generation_request_claim_token_hash"),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'succeeded', 'failed', 'unknown')",
            name="ck_generation_requests_status",
        ),
        CheckConstraint(
            "length(brief) BETWEEN 10 AND 1000",
            name="ck_generation_requests_brief_length",
        ),
        CheckConstraint(
            "category IS NULL OR category IN "
            "('dato_semana', 'ficha_territorio', 'lamina', 'correccion_publica')",
            name="ck_generation_requests_category",
        ),
        CheckConstraint(
            "institution IS NULL OR institution IN "
            "('colmat', 'escuela_colombiana_de_filosofia', 'tierra_firme')",
            name="ck_generation_requests_institution",
        ),
        CheckConstraint(
            "claim_token_hash IS NULL OR length(claim_token_hash) = 64",
            name="ck_generation_requests_claim_token_hash_length",
        ),
        CheckConstraint("claim_fence >= 0", name="ck_generation_requests_claim_fence"),
        CheckConstraint(
            "(status = 'queued' AND claim_fence = 0 AND claim_token_hash IS NULL "
            "AND claimed_by IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL) OR "
            "(status <> 'queued' AND claim_fence >= 1 AND claim_token_hash IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_generation_requests_claim_state",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > claimed_at",
            name="ck_generation_requests_lease",
        ),
        CheckConstraint(
            "(status IN ('queued', 'claimed') AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'unknown') AND finished_at IS NOT NULL)",
            name="ck_generation_requests_finished",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND draft_id IS NOT NULL AND revision_id IS NOT NULL) OR "
            "(status <> 'succeeded' AND draft_id IS NULL AND revision_id IS NULL)",
            name="ck_generation_requests_result",
        ),
        CheckConstraint(
            "error IS NULL OR length(error) <= 1000",
            name="ck_generation_requests_error_length",
        ),
        Index(
            "ix_generation_requests_queue",
            "workspace_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_generation_requests_lease",
            "workspace_id",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))
    institution: Mapped[str | None] = mapped_column(String(80))
    generate_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requested_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    telegram_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=GenerationRequestStatus.QUEUED.value
    )
    claim_token_hash: Mapped[str | None] = mapped_column(String(64))
    claim_fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    draft_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="RESTRICT")
    )
    revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("revisions.id", ondelete="RESTRICT")
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def status_value(self) -> GenerationRequestStatus:
        return GenerationRequestStatus(self.status)


@dataclass(frozen=True, slots=True)
class GenerationClaim:
    request: GenerationRequest
    claim_token: str = field(repr=False)
    claim_fence: int
    lease_expires_at: datetime


class GenerationNotification(Base):
    """Outbox durable de la revisión generada y sus callbacks de un solo uso."""

    __tablename__ = "generation_notifications"
    __table_args__ = (
        UniqueConstraint(
            "generation_request_id",
            name="uq_generation_notification_request",
        ),
        UniqueConstraint(
            "claim_token_hash",
            name="uq_generation_notification_claim_token_hash",
        ),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'sent', 'failed', 'unknown')",
            name="ck_generation_notifications_status",
        ),
        CheckConstraint(
            "length(snapshot_hash) = 64",
            name="ck_generation_notifications_snapshot_hash_length",
        ),
        CheckConstraint(
            "media_sha256 IS NULL OR length(media_sha256) = 64",
            name="ck_generation_notifications_media_sha256_length",
        ),
        CheckConstraint(
            "engagement_score BETWEEN 0 AND 100",
            name="ck_generation_notifications_engagement",
        ),
        CheckConstraint(
            "claim_token_hash IS NULL OR length(claim_token_hash) = 64",
            name="ck_generation_notifications_claim_token_hash_length",
        ),
        CheckConstraint("claim_fence >= 0", name="ck_generation_notifications_claim_fence"),
        CheckConstraint(
            "(status = 'queued' AND claim_fence = 0 AND claim_token_hash IS NULL "
            "AND claimed_by IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL) OR "
            "(status <> 'queued' AND claim_fence >= 1 AND claim_token_hash IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_generation_notifications_claim_state",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > claimed_at",
            name="ck_generation_notifications_lease",
        ),
        CheckConstraint(
            "(status IN ('queued', 'claimed') AND finished_at IS NULL) OR "
            "(status IN ('sent', 'failed', 'unknown') AND finished_at IS NOT NULL)",
            name="ck_generation_notifications_finished",
        ),
        CheckConstraint(
            "(approve_intent_id IS NULL AND reject_intent_id IS NULL) OR "
            "(approve_intent_id IS NOT NULL AND reject_intent_id IS NOT NULL)",
            name="ck_generation_notifications_callback_pair",
        ),
        CheckConstraint(
            "status <> 'sent' OR (review_message_id IS NOT NULL AND sent_at IS NOT NULL)",
            name="ck_generation_notifications_sent_result",
        ),
        CheckConstraint(
            "error IS NULL OR length(error) <= 1000",
            name="ck_generation_notifications_error_length",
        ),
        Index(
            "ix_generation_notifications_queue",
            "workspace_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_generation_notifications_lease",
            "workspace_id",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    generation_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("generation_requests.id", ondelete="CASCADE"), nullable=False
    )
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("revisions.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    engagement_score: Mapped[int] = mapped_column(Integer, nullable=False)
    media_sha256: Mapped[str | None] = mapped_column(String(64))
    approve_intent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("callback_intents.id", ondelete="RESTRICT")
    )
    reject_intent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("callback_intents.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=GenerationNotificationStatus.QUEUED.value
    )
    claim_token_hash: Mapped[str | None] = mapped_column(String(64))
    claim_fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    photo_message_id: Mapped[int | None] = mapped_column(BigInteger)
    review_message_id: Mapped[int | None] = mapped_column(BigInteger)
    photo_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def status_value(self) -> GenerationNotificationStatus:
        return GenerationNotificationStatus(self.status)


@dataclass(frozen=True, slots=True)
class GenerationNotificationClaim:
    notification: GenerationNotification
    claim_token: str = field(repr=False)
    claim_fence: int
    lease_expires_at: datetime


class AutomationSettings(Base):
    __tablename__ = "automation_settings"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('human_review', 'direct')",
            name="ck_automation_settings_mode",
        ),
        CheckConstraint(
            "jsonb_typeof(slots) = 'array'",
            name="ck_automation_settings_slots",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "min_engagement_score BETWEEN 0 AND 100",
            name="ck_automation_settings_engagement",
        ),
        CheckConstraint(
            "max_posts_per_day BETWEEN 1 AND 100",
            name="ck_automation_settings_daily_limit",
        ),
        CheckConstraint("version >= 1", name="ck_automation_settings_version"),
        CheckConstraint(
            "(mode = 'direct' AND direct_authorized_by IS NOT NULL "
            "AND direct_authorized_at IS NOT NULL) OR "
            "(mode = 'human_review' AND direct_authorized_by IS NULL "
            "AND direct_authorized_at IS NULL)",
            name="ck_automation_settings_direct_authorization",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AutomationMode.HUMAN_REVIEW.value
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_AUTOMATION_TIMEZONE
    )
    slots: Mapped[Any] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list
    )
    generate_images: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    min_engagement_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_posts_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_POSTS_PER_DAY
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    direct_authorized_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT")
    )
    direct_authorized_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)

    @property
    def mode_value(self) -> AutomationMode:
        return AutomationMode(self.mode)


class AutomationRun(Base):
    __tablename__ = "automation_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_automation_run_workspace_key"),
        CheckConstraint(
            "mode IN ('human_review', 'direct')",
            name="ck_automation_runs_mode",
        ),
        CheckConstraint(
            "status IN ('claimed', 'awaiting_review', 'ready', 'publishing', "
            "'succeeded', 'failed', 'unknown')",
            name="ck_automation_runs_status",
        ),
        CheckConstraint("settings_version >= 1", name="ck_automation_runs_settings_version"),
        CheckConstraint(
            "length(slot_hash) = 64",
            name="ck_automation_runs_slot_hash_length",
        ),
        CheckConstraint(
            "error IS NULL OR length(error) <= 1000",
            name="ck_automation_runs_error_length",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed', 'unknown') AND finished_at IS NOT NULL) "
            "OR status NOT IN ('succeeded', 'failed', 'unknown')",
            name="ck_automation_runs_finished",
        ),
        Index(
            "ix_automation_runs_workspace_schedule",
            "workspace_id",
            "scheduled_for",
            "status",
        ),
        Index("ix_automation_runs_draft", "draft_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    slot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AutomationRunStatus.CLAIMED.value
    )
    draft_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="SET NULL")
    )
    error: Mapped[str | None] = mapped_column(Text)
    claimed_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    finished_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT")
    )
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def mode_value(self) -> AutomationMode:
        return AutomationMode(self.mode)

    @property
    def status_value(self) -> AutomationRunStatus:
        return AutomationRunStatus(self.status)


class AutomationReviewNotification(Base):
    """Outbox durable, uno a uno, para la revisión humana de un run diario."""

    __tablename__ = "automation_review_notifications"
    __table_args__ = (
        UniqueConstraint(
            "automation_run_id",
            name="uq_automation_review_notification_run",
        ),
        UniqueConstraint(
            "claim_token_hash",
            name="uq_automation_review_notification_claim_token_hash",
        ),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'sent', 'failed', 'unknown')",
            name="ck_automation_review_notifications_status",
        ),
        CheckConstraint(
            "length(snapshot_hash) = 64",
            name="ck_automation_review_notifications_snapshot_hash_length",
        ),
        CheckConstraint(
            "media_sha256 IS NULL OR length(media_sha256) = 64",
            name="ck_automation_review_notifications_media_sha256_length",
        ),
        CheckConstraint(
            "engagement_score BETWEEN 0 AND 100",
            name="ck_automation_review_notifications_engagement",
        ),
        CheckConstraint(
            "claim_token_hash IS NULL OR length(claim_token_hash) = 64",
            name="ck_automation_review_notifications_claim_token_hash_length",
        ),
        CheckConstraint(
            "claim_fence >= 0",
            name="ck_automation_review_notifications_claim_fence",
        ),
        CheckConstraint(
            "(status = 'queued' AND claim_fence = 0 AND claim_token_hash IS NULL "
            "AND claimed_by IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL) OR "
            "(status <> 'queued' AND claim_fence >= 1 AND claim_token_hash IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_automation_review_notifications_claim_state",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > claimed_at",
            name="ck_automation_review_notifications_lease",
        ),
        CheckConstraint(
            "(status IN ('queued', 'claimed') AND finished_at IS NULL) OR "
            "(status IN ('sent', 'failed', 'unknown') AND finished_at IS NOT NULL)",
            name="ck_automation_review_notifications_finished",
        ),
        CheckConstraint(
            "(approve_intent_id IS NULL AND reject_intent_id IS NULL) OR "
            "(approve_intent_id IS NOT NULL AND reject_intent_id IS NOT NULL)",
            name="ck_automation_review_notifications_callback_pair",
        ),
        CheckConstraint(
            "(photo_message_id IS NULL AND photo_sent_at IS NULL) OR "
            "(photo_message_id IS NOT NULL AND photo_sent_at IS NOT NULL)",
            name="ck_automation_review_notifications_photo_pair",
        ),
        CheckConstraint(
            "status <> 'sent' OR (review_message_id IS NOT NULL AND sent_at IS NOT NULL "
            "AND approve_intent_id IS NOT NULL AND reject_intent_id IS NOT NULL)",
            name="ck_automation_review_notifications_sent_result",
        ),
        CheckConstraint(
            "(status IN ('failed', 'unknown') AND error IS NOT NULL) OR "
            "(status NOT IN ('failed', 'unknown') AND error IS NULL)",
            name="ck_automation_review_notifications_error_state",
        ),
        CheckConstraint(
            "error IS NULL OR length(error) <= 1000",
            name="ck_automation_review_notifications_error_length",
        ),
        CheckConstraint(
            "length(detail) BETWEEN 1 AND 1000",
            name="ck_automation_review_notifications_detail_length",
        ),
        Index(
            "ix_automation_review_notifications_queue",
            "workspace_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_automation_review_notifications_lease",
            "workspace_id",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    automation_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("revisions.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    engagement_score: Mapped[int] = mapped_column(Integer, nullable=False)
    media_sha256: Mapped[str | None] = mapped_column(String(64))
    approve_intent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("callback_intents.id", ondelete="RESTRICT")
    )
    reject_intent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("callback_intents.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AutomationReviewNotificationStatus.QUEUED.value
    )
    claim_token_hash: Mapped[str | None] = mapped_column(String(64))
    claim_fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    photo_message_id: Mapped[int | None] = mapped_column(BigInteger)
    review_message_id: Mapped[int | None] = mapped_column(BigInteger)
    photo_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def status_value(self) -> AutomationReviewNotificationStatus:
        return AutomationReviewNotificationStatus(self.status)


@dataclass(frozen=True, slots=True)
class AutomationReviewNotificationClaim:
    notification: AutomationReviewNotification
    claim_token: str = field(repr=False)
    claim_fence: int
    lease_expires_at: datetime


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_workspace_sequence", "workspace_id", "sequence"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    # INTEGER, no BIGINT, mantiene autoincremento real también en SQLite.
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utc_now)


def approval_snapshot_hash(
    *,
    text: str,
    category: str,
    publish_at: datetime | date | str,
    evidence: Any,
    image_sha256: str | None,
) -> str:
    """Hash canónico de todos los campos materiales que recibe el aprobador."""

    normalized_text = _normalize_text(text)
    normalized_category = _normalize_category(category)
    normalized_publish_at = _normalize_publish_at(publish_at)
    normalized_evidence = _normalize_json(evidence, field_name="evidence")
    normalized_image_hash = _normalize_sha256(image_sha256, required=False)
    payload = {
        "category": normalized_category,
        "evidence": normalized_evidence,
        "image_sha256": normalized_image_hash,
        "publish_at": _format_time(normalized_publish_at),
        "text": normalized_text,
        "version": 1,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Un alias descriptivo para consumidores que prefieran un verbo.
compute_approval_hash = approval_snapshot_hash


class PlatformStore:
    """Persistencia transaccional síncrona para SQLite local y PostgreSQL."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        create_schema: bool = True,
        echo: bool = False,
    ) -> None:
        self.database_url = resolve_database_url(database_url)
        engine_options: dict[str, Any] = {"future": True, "echo": echo}
        if self.database_url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False}
            if _is_sqlite_memory_url(self.database_url):
                engine_options["poolclass"] = StaticPool
        self.engine = create_engine(self.database_url, **engine_options)
        if self.database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)
        self._sessions = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=True,
        )
        if create_schema:
            if self.database_url.startswith("sqlite"):
                _migrate_legacy_sqlite_schema(self.engine)
            Base.metadata.create_all(self.engine)
            if self.database_url.startswith("sqlite"):
                _backfill_sqlite_automation_settings(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def __enter__(self) -> PlatformStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Sesión de solo lectura/uso avanzado; el llamador controla el commit."""

        with self._sessions() as session:
            yield session

    def bootstrap_owner(
        self,
        *,
        email: str,
        display_name: str,
        username: str | None = None,
        password_hash: str | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        actor_id: str = SYSTEM_ACTOR,
        user_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[User, Membership]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        with _workspace_rbac_lock(workspace), self._sessions.begin() as session:
            self._serialize_workspace_rbac(session, workspace)
            existing = session.scalar(
                select(func.count(Membership.id)).where(Membership.workspace_id == workspace)
            )
            if existing:
                raise ConflictError(f"El espacio '{workspace}' ya tiene un owner")
            user = self._new_user(
                session,
                email=email,
                display_name=display_name,
                username=username,
                password_hash=password_hash,
                user_id=user_id,
                now=timestamp,
            )
            membership = Membership(
                workspace_id=workspace,
                user_id=user.id,
                role=Role.OWNER.value,
                created_by=actor,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(membership)
            session.flush()
            settings = self._new_automation_settings(
                workspace_id=workspace,
                actor_id=user.id,
                now=timestamp,
            )
            session.add(settings)
            session.flush()
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="workspace.owner_bootstrapped",
                entity_type="membership",
                entity_id=membership.id,
                detail={"user_id": user.id, "role": Role.OWNER.value},
                now=timestamp,
            )
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="automation.settings_initialized",
                entity_type="automation_settings",
                entity_id=workspace,
                detail={"enabled": False, "mode": AutomationMode.HUMAN_REVIEW.value},
                now=timestamp,
            )
            return user, membership

    def create_user(
        self,
        *,
        actor_id: str,
        email: str,
        display_name: str,
        username: str | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        password_hash: str | None = None,
        user_id: str | None = None,
        now: datetime | None = None,
    ) -> User:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_USERS)
            user = self._new_user(
                session,
                email=email,
                display_name=display_name,
                username=username,
                password_hash=password_hash,
                user_id=user_id,
                now=timestamp,
            )
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="user.created",
                entity_type="user",
                entity_id=user.id,
                detail={"email": user.email},
                now=timestamp,
            )
            return user

    def set_user_active(
        self,
        user_id: str,
        *,
        active: bool,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> User:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        target_id = _normalize_entity_id(user_id)
        timestamp = _normalize_now(now)
        with (
            _workspace_rbac_lock(workspace),
            _user_rbac_lock(target_id),
            self._sessions.begin() as session,
        ):
            self._serialize_workspace_rbac(session, workspace)
            self._serialize_user_rbac(session, target_id)
            actor_role = self._authorize(session, actor, workspace, Permission.MANAGE_USERS)
            user = self._get_user(session, target_id)
            if user.id == actor and not active:
                raise ConflictError("Un administrador no puede desactivar su propia cuenta")
            target_membership = self._required_membership(session, target_id, workspace)
            self._require_can_manage_existing_role(actor_role, Role(target_membership.role))
            other_workspace = session.scalar(
                select(Membership.workspace_id).where(
                    Membership.user_id == target_id,
                    Membership.workspace_id != workspace,
                )
            )
            if other_workspace is not None:
                raise ConflictError(
                    "La cuenta pertenece a otros espacios; requiere administración global"
                )
            if not active and target_membership.role == Role.OWNER.value:
                self._require_another_active_owner(session, workspace, excluding_user_id=target_id)
            user.is_active = bool(active)
            user.updated_at = timestamp
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="user.activated" if active else "user.deactivated",
                entity_type="user",
                entity_id=user.id,
                detail={},
                now=timestamp,
            )
            return user

    def grant_membership(
        self,
        user_id: str,
        role: Role | str,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> Membership:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        target_id = _normalize_entity_id(user_id)
        target_role = _normalize_role(role)
        timestamp = _normalize_now(now)
        with (
            _workspace_rbac_lock(workspace),
            _user_rbac_lock(target_id),
            self._sessions.begin() as session,
        ):
            self._serialize_workspace_rbac(session, workspace)
            self._serialize_user_rbac(session, target_id)
            actor_role = self._authorize(session, actor, workspace, Permission.MANAGE_MEMBERSHIPS)
            require_role_assignment(actor_role, target_role)
            target_user = self._get_user(session, target_id)
            if not target_user.is_active:
                raise ConflictError("No se puede incorporar una cuenta globalmente inactiva")
            existing = self._membership(session, target_id, workspace)
            if existing is not None:
                raise ConflictError("El usuario ya pertenece al espacio de trabajo")
            membership = Membership(
                workspace_id=workspace,
                user_id=target_id,
                role=target_role.value,
                created_by=actor,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(membership)
            session.flush()
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="membership.granted",
                entity_type="membership",
                entity_id=membership.id,
                detail={"user_id": target_id, "role": target_role.value},
                now=timestamp,
            )
            return membership

    def change_membership_role(
        self,
        user_id: str,
        role: Role | str,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> Membership:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        target_id = _normalize_entity_id(user_id)
        target_role = _normalize_role(role)
        timestamp = _normalize_now(now)
        with (
            _workspace_rbac_lock(workspace),
            _user_rbac_lock(target_id),
            self._sessions.begin() as session,
        ):
            self._serialize_workspace_rbac(session, workspace)
            self._serialize_user_rbac(session, target_id)
            actor_role = self._authorize(session, actor, workspace, Permission.MANAGE_MEMBERSHIPS)
            require_role_assignment(actor_role, target_role)
            membership = self._required_membership(session, target_id, workspace)
            self._require_can_manage_existing_role(actor_role, Role(membership.role))
            if membership.role == Role.OWNER.value and target_role is not Role.OWNER:
                self._require_another_active_owner(
                    session,
                    workspace,
                    excluding_user_id=target_id,
                )
            previous = membership.role
            membership.role = target_role.value
            membership.updated_at = timestamp
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="membership.role_changed",
                entity_type="membership",
                entity_id=membership.id,
                detail={
                    "user_id": target_id,
                    "previous_role": previous,
                    "role": target_role.value,
                },
                now=timestamp,
            )
            return membership

    def revoke_membership(
        self,
        user_id: str,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> None:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        target_id = _normalize_entity_id(user_id)
        timestamp = _normalize_now(now)
        with (
            _workspace_rbac_lock(workspace),
            _user_rbac_lock(target_id),
            self._sessions.begin() as session,
        ):
            self._serialize_workspace_rbac(session, workspace)
            self._serialize_user_rbac(session, target_id)
            actor_role = self._authorize(session, actor, workspace, Permission.MANAGE_MEMBERSHIPS)
            membership = self._required_membership(session, target_id, workspace)
            self._require_can_manage_existing_role(actor_role, Role(membership.role))
            if membership.role == Role.OWNER.value:
                self._require_another_active_owner(
                    session,
                    workspace,
                    excluding_user_id=target_id,
                )
            detail = {"user_id": target_id, "role": membership.role}
            membership_id = membership.id
            session.delete(membership)
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="membership.revoked",
                entity_type="membership",
                entity_id=membership_id,
                detail=detail,
                now=timestamp,
            )

    def get_user(self, user_id: str) -> User:
        with self._sessions() as session:
            return self._get_user(session, user_id)

    def set_username(
        self,
        user_id: str,
        username: str | None,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> User:
        """Asigna un identificador global, canónico y opcional sin cambiar el email."""

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        target_id = _normalize_entity_id(user_id)
        normalized_username = _normalize_username(username)
        timestamp = _normalize_now(now)
        with (
            _workspace_rbac_lock(workspace),
            _user_rbac_lock(target_id),
            self._sessions.begin() as session,
        ):
            self._serialize_workspace_rbac(session, workspace)
            self._serialize_user_rbac(session, target_id)
            self._authorize_profile_change(session, actor, target_id, workspace)
            user = self._get_user(session, target_id)
            existing = (
                session.scalar(
                    select(User.id).where(
                        User.username == normalized_username,
                        User.id != target_id,
                    )
                )
                if normalized_username is not None
                else None
            )
            if existing is not None:
                raise ConflictError(f"Ya existe el username '{normalized_username}'")
            previous = user.username
            try:
                with session.begin_nested():
                    user.username = normalized_username
                    user.updated_at = timestamp
                    session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"Ya existe el username '{normalized_username}'") from exc
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="user.username_changed",
                entity_type="user",
                entity_id=user.id,
                detail={"previous_username": previous, "username": normalized_username},
                now=timestamp,
            )
            return user

    def update_display_name(
        self,
        user_id: str,
        display_name: str,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> User:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        target_id = _normalize_entity_id(user_id)
        normalized_name = _normalize_short_text(display_name, "display_name", 120)
        timestamp = _normalize_now(now)
        with (
            _workspace_rbac_lock(workspace),
            _user_rbac_lock(target_id),
            self._sessions.begin() as session,
        ):
            self._serialize_workspace_rbac(session, workspace)
            self._serialize_user_rbac(session, target_id)
            self._authorize_profile_change(session, actor, target_id, workspace)
            user = self._get_user(session, target_id)
            previous = user.display_name
            user.display_name = normalized_name
            user.updated_at = timestamp
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="user.display_name_changed",
                entity_type="user",
                entity_id=user.id,
                detail={"previous_display_name": previous, "display_name": normalized_name},
                now=timestamp,
            )
            return user

    def list_memberships(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> list[Membership]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            self._authorize(session, actor, workspace, Permission.VIEW_WORKSPACE)
            return list(
                session.scalars(
                    select(Membership)
                    .where(Membership.workspace_id == workspace)
                    .order_by(Membership.created_at, Membership.id)
                )
            )

    def create_draft(
        self,
        *,
        actor_id: str,
        text: str,
        category: str,
        publish_at: datetime | date | str,
        evidence: Any,
        image_sha256: str | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        draft_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[Draft, Revision]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        material = _normalize_revision_material(
            text=text,
            category=category,
            publish_at=publish_at,
            evidence=evidence,
            image_sha256=image_sha256,
        )
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.CREATE_DRAFTS)
            revision_id = _new_id()
            draft = Draft(
                id=_normalize_entity_id(draft_id) if draft_id else _new_id(),
                workspace_id=workspace,
                status=DraftStatus.DRAFT.value,
                created_by=actor,
                current_revision_id=revision_id,
                created_at=timestamp,
                updated_at=timestamp,
            )
            revision = Revision(
                id=revision_id,
                draft_id=draft.id,
                revision_number=1,
                created_by=actor,
                created_at=timestamp,
                **material,
            )
            session.add_all((draft, revision))
            session.flush()
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="draft.created",
                entity_type="draft",
                entity_id=draft.id,
                detail={
                    "revision_id": revision.id,
                    "snapshot_hash": revision.snapshot_hash,
                },
                now=timestamp,
            )
            return draft, revision

    def revise_draft(
        self,
        draft_id: str,
        *,
        actor_id: str,
        text: str,
        category: str,
        publish_at: datetime | date | str,
        evidence: Any,
        image_sha256: str | None = None,
        now: datetime | None = None,
    ) -> Revision:
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        material = _normalize_revision_material(
            text=text,
            category=category,
            publish_at=publish_at,
            evidence=evidence,
            image_sha256=image_sha256,
        )
        with self._sessions.begin() as session:
            draft = self._get_draft_for_update(session, draft_id)
            self._authorize(session, actor, draft.workspace_id, Permission.EDIT_DRAFTS)
            if DraftStatus(draft.status) in {DraftStatus.IN_REVIEW, DraftStatus.PUBLISHED}:
                raise ConflictError(f"No se puede editar un draft en estado '{draft.status}'")
            pending = session.scalar(
                select(PublishAttempt.id).where(
                    PublishAttempt.draft_id == draft.id,
                    PublishAttempt.status.in_(
                        (PublishStatus.PENDING.value, PublishStatus.UNKNOWN.value)
                    ),
                )
            )
            if pending is not None:
                raise ConflictError("Hay una publicación pendiente o de resultado desconocido")
            queued_request = session.scalar(
                select(PublicationRequest.id).where(
                    PublicationRequest.draft_id == draft.id,
                    PublicationRequest.status.in_(
                        (
                            PublicationRequestStatus.QUEUED.value,
                            PublicationRequestStatus.CLAIMED.value,
                            PublicationRequestStatus.UNKNOWN.value,
                        )
                    ),
                )
            )
            if queued_request is not None:
                raise ConflictError("Hay una solicitud de publicación en cola, reclamada o ambigua")
            latest_number = session.scalar(
                select(func.max(Revision.revision_number)).where(Revision.draft_id == draft.id)
            )
            revision = Revision(
                id=_new_id(),
                draft_id=draft.id,
                revision_number=int(latest_number or 0) + 1,
                created_by=actor,
                created_at=timestamp,
                **material,
            )
            previous_approval = draft.approved_revision_id
            draft.current_revision_id = revision.id
            draft.approved_revision_id = None
            draft.status = DraftStatus.DRAFT.value
            draft.updated_at = timestamp
            session.add(revision)
            session.flush()
            self._audit(
                session,
                workspace_id=draft.workspace_id,
                actor_id=actor,
                action="draft.revised",
                entity_type="draft",
                entity_id=draft.id,
                detail={
                    "revision_id": revision.id,
                    "revision_number": revision.revision_number,
                    "invalidated_approval_revision_id": previous_approval,
                    "snapshot_hash": revision.snapshot_hash,
                },
                now=timestamp,
            )
            return revision

    def submit_for_review(
        self,
        draft_id: str,
        *,
        actor_id: str,
        expected_snapshot_hash: str,
        now: datetime | None = None,
    ) -> Draft:
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        expected = _normalize_sha256(expected_snapshot_hash, required=True)
        with self._sessions.begin() as session:
            draft = self._get_draft(session, draft_id)
            self._authorize(session, actor, draft.workspace_id, Permission.SUBMIT_DRAFTS)
            if DraftStatus(draft.status) is not DraftStatus.DRAFT:
                raise ConflictError("Solo un draft editable se puede enviar a revisión")
            revision = self._current_revision(session, draft)
            if revision.snapshot_hash != expected:
                raise StaleSnapshotError("El draft cambió desde la vista previa; vuelve a cargarlo")
            draft.status = DraftStatus.IN_REVIEW.value
            draft.updated_at = timestamp
            self._audit(
                session,
                workspace_id=draft.workspace_id,
                actor_id=actor,
                action="draft.submitted",
                entity_type="draft",
                entity_id=draft.id,
                detail={
                    "revision_id": revision.id,
                    "snapshot_hash": revision.snapshot_hash,
                },
                now=timestamp,
            )
            return draft

    def approve_draft(
        self,
        draft_id: str,
        *,
        actor_id: str,
        expected_snapshot_hash: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Approval:
        return self._review_draft(
            draft_id,
            actor_id=actor_id,
            expected_snapshot_hash=expected_snapshot_hash,
            decision=ApprovalDecision.APPROVED,
            reason=reason,
            now=now,
        )

    def reject_draft(
        self,
        draft_id: str,
        *,
        actor_id: str,
        expected_snapshot_hash: str,
        reason: str,
        now: datetime | None = None,
    ) -> Approval:
        normalized_reason = _normalize_note(reason, required=True)
        return self._review_draft(
            draft_id,
            actor_id=actor_id,
            expected_snapshot_hash=expected_snapshot_hash,
            decision=ApprovalDecision.REJECTED,
            reason=normalized_reason,
            now=now,
        )

    def get_draft(self, draft_id: str, *, actor_id: str) -> Draft:
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            draft = self._get_draft(session, draft_id)
            self._authorize(session, actor, draft.workspace_id, Permission.VIEW_DRAFTS)
            return draft

    def get_current_revision(self, draft_id: str, *, actor_id: str) -> Revision:
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            draft = self._get_draft(session, draft_id)
            self._authorize(session, actor, draft.workspace_id, Permission.VIEW_DRAFTS)
            return self._current_revision(session, draft)

    def list_drafts(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        status: DraftStatus | str | None = None,
    ) -> list[Draft]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        normalized_status = DraftStatus(status) if status is not None else None
        with self._sessions() as session:
            self._authorize(session, actor, workspace, Permission.VIEW_DRAFTS)
            statement = select(Draft).where(Draft.workspace_id == workspace)
            if normalized_status is not None:
                statement = statement.where(Draft.status == normalized_status.value)
            return list(session.scalars(statement.order_by(Draft.created_at, Draft.id)))

    def register_media_asset(
        self,
        *,
        actor_id: str,
        kind: str,
        url: str,
        sha256: str,
        mime_type: str,
        byte_size: int | None = None,
        metadata: Any = None,
        draft_id: str | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> MediaAsset:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        digest = _normalize_sha256(sha256, required=True)
        normalized_kind = _normalize_short_text(kind, "kind", 40)
        normalized_url = _normalize_short_text(url, "url", 4096)
        normalized_mime = _normalize_short_text(mime_type, "mime_type", 120)
        normalized_metadata = _normalize_json(metadata or {}, field_name="metadata")
        if byte_size is not None and (not isinstance(byte_size, int) or byte_size < 0):
            raise ValueError("byte_size debe ser un entero no negativo")
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_MEDIA)
            if draft_id is not None:
                draft = self._get_draft(session, draft_id)
                if draft.workspace_id != workspace:
                    raise ConflictError("El asset y el draft deben pertenecer al mismo espacio")
            existing = session.scalar(
                select(MediaAsset).where(
                    MediaAsset.workspace_id == workspace, MediaAsset.sha256 == digest
                )
            )
            if existing is not None:
                return existing
            asset = MediaAsset(
                workspace_id=workspace,
                draft_id=draft_id,
                kind=normalized_kind,
                url=normalized_url,
                sha256=digest,
                mime_type=normalized_mime,
                byte_size=byte_size,
                asset_metadata=normalized_metadata,
                created_by=actor,
                created_at=timestamp,
            )
            session.add(asset)
            session.flush()
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="media.registered",
                entity_type="media_asset",
                entity_id=asset.id,
                detail={"draft_id": draft_id, "sha256": digest, "kind": normalized_kind},
                now=timestamp,
            )
            return asset

    def get_media_asset_by_sha256(
        self,
        sha256: str,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> MediaAsset | None:
        """Obtiene un asset del workspace sin exponer una sesión privada al consumidor."""

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        digest = _normalize_sha256(sha256, required=True)
        with self._sessions() as session:
            self._authorize(session, actor, workspace, Permission.VIEW_DRAFTS)
            return session.scalar(
                select(MediaAsset).where(
                    MediaAsset.workspace_id == workspace,
                    MediaAsset.sha256 == digest,
                )
            )

    def bind_telegram_chat(
        self,
        chat_id: int | str,
        *,
        telegram_user_id: int | str,
        actor_id: str,
        user_id: str,
        purpose: str = "control",
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        active: bool = True,
        now: datetime | None = None,
    ) -> TelegramBinding:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        normalized_chat = _normalize_chat_id(chat_id)
        normalized_telegram_user = _normalize_telegram_user_id(telegram_user_id)
        if purpose not in {"control", "review", "alerts"}:
            raise ValueError("purpose debe ser control, review o alerts")
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_TELEGRAM)
            self._get_user(session, user_id)
            self._required_membership(session, user_id, workspace)
            binding = session.scalar(
                select(TelegramBinding).where(
                    TelegramBinding.workspace_id == workspace,
                    TelegramBinding.telegram_user_id == normalized_telegram_user,
                    TelegramBinding.chat_id == normalized_chat,
                )
            )
            action = "telegram.binding_updated"
            if binding is None:
                binding = TelegramBinding(
                    workspace_id=workspace,
                    telegram_user_id=normalized_telegram_user,
                    chat_id=normalized_chat,
                    created_by=actor,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(binding)
                action = "telegram.binding_created"
            binding.user_id = user_id
            binding.purpose = purpose
            binding.is_active = bool(active)
            binding.updated_at = timestamp
            session.flush()
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action=action,
                entity_type="telegram_binding",
                entity_id=binding.id,
                detail={
                    "chat_id": normalized_chat,
                    "telegram_user_id": normalized_telegram_user,
                    "user_id": user_id,
                    "purpose": purpose,
                    "active": bool(active),
                },
                now=timestamp,
            )
            return binding

    def resolve_telegram_actor(
        self,
        *,
        telegram_user_id: int | str,
        chat_id: int | str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> User:
        """Resuelve `from.id`, nunca el chat, a una cuenta activa de la plataforma."""

        workspace = _normalize_workspace(workspace_id)
        telegram_identity = _normalize_telegram_user_id(telegram_user_id)
        normalized_chat = _normalize_chat_id(chat_id)
        with self._sessions() as session:
            user, _membership_row = self._resolve_telegram_identity(
                session,
                workspace_id=workspace,
                telegram_user_id=telegram_identity,
                chat_id=normalized_chat,
            )
            return user

    def issue_callback_intent(
        self,
        draft_id: str,
        action: CallbackAction | str,
        *,
        expected_snapshot_hash: str,
        telegram_user_id: int | str,
        chat_id: int | str,
        expires_at: datetime,
        actor_id: str = TELEGRAM_ACTOR,
        now: datetime | None = None,
        _session: Session | None = None,
    ) -> IssuedCallbackIntent:
        """Crea un nonce opaco ligado al actor, chat y snapshot actuales."""

        normalized_action = _normalize_callback_action(action)
        expected = _normalize_sha256(expected_snapshot_hash, required=True)
        telegram_identity = _normalize_telegram_user_id(telegram_user_id)
        normalized_chat = _normalize_chat_id(chat_id)
        creator = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        expiration = _normalize_now(expires_at)
        if expiration <= timestamp:
            raise ValueError("expires_at debe estar en el futuro")
        if expiration > timestamp + CALLBACK_MAX_LIFETIME:
            raise ValueError("El callback no puede durar más de 24 horas")
        transaction = self._sessions.begin() if _session is None else nullcontext(_session)
        with transaction as session:
            draft = self._get_draft(session, draft_id)
            user, membership = self._resolve_telegram_identity(
                session,
                workspace_id=draft.workspace_id,
                telegram_user_id=telegram_identity,
                chat_id=normalized_chat,
            )
            require_permission(Role(membership.role), _permission_for_callback(normalized_action))
            revision = self._current_revision(session, draft)
            if revision.snapshot_hash != expected:
                raise StaleSnapshotError(
                    "El snapshot solicitado ya no coincide con la revisión actual"
                )
            self._validate_callback_state(
                draft=draft,
                revision=revision,
                action=normalized_action,
                actor_id=user.id,
            )
            nonce = secrets.token_urlsafe(32)
            nonce_hash = _hash_nonce(nonce)
            intent = CallbackIntent(
                nonce_hash=nonce_hash,
                workspace_id=draft.workspace_id,
                action=normalized_action.value,
                draft_id=draft.id,
                revision_id=revision.id,
                snapshot_hash=revision.snapshot_hash,
                user_id=user.id,
                telegram_user_id=telegram_identity,
                chat_id=normalized_chat,
                expires_at=expiration,
                created_by=creator,
                created_at=timestamp,
            )
            session.add(intent)
            session.flush()
            self._audit(
                session,
                workspace_id=draft.workspace_id,
                actor_id=creator,
                action="telegram.callback_issued",
                entity_type="callback_intent",
                entity_id=intent.id,
                detail={
                    "action": normalized_action.value,
                    "draft_id": draft.id,
                    "revision_id": revision.id,
                    "snapshot_hash": revision.snapshot_hash,
                    "user_id": user.id,
                    "telegram_user_id": telegram_identity,
                    "chat_id": normalized_chat,
                    "expires_at": _format_time(expiration),
                },
                now=timestamp,
            )
            return IssuedCallbackIntent(nonce=nonce, intent=intent)

    def issue_review_callback_intents(
        self,
        draft_id: str,
        *,
        expected_snapshot_hash: str,
        telegram_user_id: int | str,
        chat_id: int | str,
        expires_at: datetime,
        actor_id: str = TELEGRAM_ACTOR,
        now: datetime | None = None,
        _session: Session | None = None,
    ) -> tuple[IssuedCallbackIntent, IssuedCallbackIntent]:
        """Emite approve/reject juntos; si uno falla, ninguno queda utilizable."""

        timestamp = _normalize_now(now)
        transaction = self._sessions.begin() if _session is None else nullcontext(_session)
        with transaction as session:
            approve = self.issue_callback_intent(
                draft_id,
                CallbackAction.APPROVE,
                expected_snapshot_hash=expected_snapshot_hash,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                expires_at=expires_at,
                actor_id=actor_id,
                now=timestamp,
                _session=session,
            )
            reject = self.issue_callback_intent(
                draft_id,
                CallbackAction.REJECT,
                expected_snapshot_hash=expected_snapshot_hash,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                expires_at=expires_at,
                actor_id=actor_id,
                now=timestamp,
                _session=session,
            )
            return approve, reject

    def consume_callback_intent(
        self,
        nonce: str,
        action: CallbackAction | str,
        *,
        telegram_user_id: int | str,
        chat_id: int | str,
        now: datetime | None = None,
        _session: Session | None = None,
    ) -> CallbackIntent:
        """Valida y consume el nonce con compare-and-swap dentro de una transacción."""

        nonce_hash = _hash_nonce(nonce)
        normalized_action = _normalize_callback_action(action)
        telegram_identity = _normalize_telegram_user_id(telegram_user_id)
        normalized_chat = _normalize_chat_id(chat_id)
        timestamp = _normalize_now(now)
        transaction = self._sessions.begin() if _session is None else nullcontext(_session)
        with transaction as session:
            intent = session.scalar(
                select(CallbackIntent).where(CallbackIntent.nonce_hash == nonce_hash)
            )
            generic_error = "El callback es inválido, expiró o ya fue utilizado"
            if (
                intent is None
                or intent.action != normalized_action.value
                or intent.telegram_user_id != telegram_identity
                or intent.chat_id != normalized_chat
                or intent.consumed_at is not None
                or intent.expires_at <= timestamp
            ):
                raise ConflictError(generic_error)
            user, membership = self._resolve_telegram_identity(
                session,
                workspace_id=intent.workspace_id,
                telegram_user_id=telegram_identity,
                chat_id=normalized_chat,
            )
            if user.id != intent.user_id:
                raise ConflictError(generic_error)
            require_permission(Role(membership.role), _permission_for_callback(normalized_action))
            draft = self._get_draft(session, intent.draft_id)
            revision = self._current_revision(session, draft)
            if revision.id != intent.revision_id or revision.snapshot_hash != intent.snapshot_hash:
                raise StaleSnapshotError("El callback pertenece a una revisión anterior")
            self._validate_callback_state(
                draft=draft,
                revision=revision,
                action=normalized_action,
                actor_id=user.id,
            )
            result = session.execute(
                update(CallbackIntent)
                .where(
                    CallbackIntent.id == intent.id,
                    CallbackIntent.consumed_at.is_(None),
                    CallbackIntent.expires_at > timestamp,
                )
                .values(consumed_at=timestamp, consumed_by=user.id)
            )
            if result.rowcount != 1:
                raise ConflictError(generic_error)
            session.flush()
            intent.consumed_at = timestamp
            intent.consumed_by = user.id
            self._audit(
                session,
                workspace_id=intent.workspace_id,
                actor_id=user.id,
                action="telegram.callback_consumed",
                entity_type="callback_intent",
                entity_id=intent.id,
                detail={
                    "action": normalized_action.value,
                    "draft_id": intent.draft_id,
                    "revision_id": intent.revision_id,
                    "snapshot_hash": intent.snapshot_hash,
                    "telegram_user_id": telegram_identity,
                    "chat_id": normalized_chat,
                },
                now=timestamp,
            )
            return intent

    def apply_callback_decision(
        self,
        nonce: str,
        *,
        decision: object,
        telegram_user_id: int,
        chat_id: int,
        update_id: int,
        claim_token: str,
        claim_fence: int,
        now: datetime | None = None,
    ) -> object | None:
        """Consume el nonce, decide el draft y deja resultado recuperable en un commit."""

        decision_value = getattr(decision, "value", decision)
        try:
            callback_action = _normalize_callback_action(decision_value)
        except ValueError:
            return None
        if callback_action not in {CallbackAction.APPROVE, CallbackAction.REJECT}:
            return None
        timestamp = _normalize_now(now)
        telegram_identity = _normalize_telegram_user_id(telegram_user_id)
        normalized_chat = _normalize_chat_id(chat_id)
        try:
            with self._sessions.begin() as session:
                stored_update = session.scalar(
                    select(TelegramUpdate)
                    .where(TelegramUpdate.update_id == update_id)
                    .with_for_update()
                )
                if stored_update is None:
                    raise NotFoundError(f"No existe el update de Telegram {update_id}")
                _require_telegram_update_claim(
                    stored_update,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                )
                if UpdateStatus(stored_update.status) is not UpdateStatus.RECEIVED:
                    raise ConflictError("El update de Telegram ya no está en procesamiento")
                if (
                    stored_update.lease_expires_at is None
                    or stored_update.lease_expires_at <= timestamp
                ):
                    raise ConflictError("La lease del update de Telegram venció")
                if (
                    stored_update.telegram_user_id != telegram_identity
                    or stored_update.chat_id != normalized_chat
                    or stored_update.business_result is not None
                ):
                    raise ConflictError("El update no coincide con el callback reclamado")

                intent = self.consume_callback_intent(
                    nonce,
                    callback_action,
                    telegram_user_id=telegram_identity,
                    chat_id=normalized_chat,
                    now=timestamp,
                    _session=session,
                )
                approval_decision = (
                    ApprovalDecision.APPROVED
                    if callback_action is CallbackAction.APPROVE
                    else ApprovalDecision.REJECTED
                )
                self._review_draft(
                    intent.draft_id,
                    actor_id=intent.user_id,
                    expected_snapshot_hash=intent.snapshot_hash,
                    decision=approval_decision,
                    reason=(
                        None
                        if approval_decision is ApprovalDecision.APPROVED
                        else "Rechazado mediante el control editorial de Telegram."
                    ),
                    now=timestamp,
                    _session=session,
                )
                stored_update.business_result = {
                    "kind": "callback_decision",
                    "decision": callback_action.value,
                    "post_id": intent.draft_id,
                    "snapshot_hash": intent.snapshot_hash,
                    "telegram_user_id": int(telegram_identity),
                    "chat_id": int(normalized_chat),
                }
                self._audit(
                    session,
                    workspace_id=intent.workspace_id,
                    actor_id=intent.user_id,
                    action="telegram.callback_decision_applied",
                    entity_type="telegram_update",
                    entity_id=str(update_id),
                    detail={
                        "callback_intent_id": intent.id,
                        "decision": callback_action.value,
                        "draft_id": intent.draft_id,
                        "snapshot_hash": intent.snapshot_hash,
                        "claim_fence": claim_fence,
                    },
                    now=timestamp,
                )
        except (AuthorizationError, PlatformStoreError, ValueError):
            return None

        from colmat_x.telegram_bot import CallbackDecision as TelegramCallbackDecision
        from colmat_x.telegram_bot import CallbackIntent as TelegramCallbackIntent

        return TelegramCallbackIntent(
            decision=TelegramCallbackDecision(intent.action),
            post_id=intent.draft_id,
            snapshot_hash=intent.snapshot_hash,
            telegram_user_id=int(intent.telegram_user_id),
            chat_id=int(intent.chat_id),
        )

    def record_telegram_update(
        self,
        update_id: int,
        payload: Any,
        *,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        chat_id: int | str | None = None,
        telegram_user_id: int | str | None = None,
        actor_id: str = TELEGRAM_ACTOR,
        now: datetime | None = None,
    ) -> bool:
        """Registra una actualización exactamente una vez; False indica duplicado."""

        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            raise ValueError("update_id debe ser un entero no negativo")
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        normalized_payload = _normalize_json(payload, field_name="payload")
        normalized_chat = _normalize_chat_id(chat_id) if chat_id is not None else None
        normalized_telegram_user = (
            _normalize_telegram_user_id(telegram_user_id) if telegram_user_id is not None else None
        )
        with self._sessions.begin() as session:
            if session.get(TelegramUpdate, update_id) is not None:
                return False
            update = TelegramUpdate(
                update_id=update_id,
                workspace_id=workspace,
                chat_id=normalized_chat,
                telegram_user_id=normalized_telegram_user,
                actor_id=actor,
                payload=normalized_payload,
                status=UpdateStatus.RECEIVED.value,
                received_at=timestamp,
            )
            try:
                # El savepoint hace que una carrera por el mismo update_id no aborte
                # el resto de la transacción exterior.
                with session.begin_nested():
                    session.add(update)
                    session.flush()
            except IntegrityError:
                return False
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="telegram.update_received",
                entity_type="telegram_update",
                entity_id=str(update_id),
                detail={
                    "chat_id": normalized_chat,
                    "telegram_user_id": normalized_telegram_user,
                    "actor_id": actor,
                },
                now=timestamp,
            )
            return True

    def claim_update(
        self,
        update_id: int,
        *,
        payload: Mapping[str, object],
        telegram_user_id: int | None,
        chat_id: int | None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        lease_seconds: int = DEFAULT_TELEGRAM_UPDATE_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> object:
        """Reclama con lease/fence sin volver a ejecutar una mutación incierta.

        Un update vencido o fallido solo se vuelve a reclamar cuando ya contiene
        acciones preparadas o un resultado de negocio recuperable. Sin esa evidencia
        durable se cierra como ambiguo: se prefiere perder la respuesta antes que
        duplicar una aprobación, un borrador o un cambio de configuración.
        """

        from colmat_x.telegram_bot import TelegramUpdateClaim

        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            raise ValueError("update_id debe ser un entero no negativo")
        workspace = _normalize_workspace(workspace_id)
        normalized_payload = _normalize_json(payload, field_name="payload")
        normalized_chat = _normalize_chat_id(chat_id) if chat_id is not None else None
        normalized_telegram_user = (
            _normalize_telegram_user_id(telegram_user_id) if telegram_user_id is not None else None
        )
        actor = (
            f"telegram:{normalized_telegram_user}"
            if normalized_telegram_user is not None
            else TELEGRAM_ACTOR
        )
        normalized_lease = _normalize_bounded_int(
            lease_seconds,
            "lease_seconds",
            minimum=1,
            maximum=MAX_TELEGRAM_UPDATE_LEASE_SECONDS,
        )
        timestamp = _normalize_now(now)
        lease_expires_at = timestamp + timedelta(seconds=normalized_lease)
        claim_token = secrets.token_urlsafe(32)
        claim_token_hash = _hash_telegram_update_claim_token(claim_token)

        with self._sessions.begin() as session:
            stored = session.scalar(
                select(TelegramUpdate)
                .where(TelegramUpdate.update_id == update_id)
                .with_for_update()
            )
            if stored is None:
                stored = TelegramUpdate(
                    update_id=update_id,
                    workspace_id=workspace,
                    chat_id=normalized_chat,
                    telegram_user_id=normalized_telegram_user,
                    actor_id=actor,
                    payload=normalized_payload,
                    status=UpdateStatus.RECEIVED.value,
                    received_at=timestamp,
                    claim_token_hash=claim_token_hash,
                    claim_fence=1,
                    attempt_count=1,
                    claimed_at=timestamp,
                    lease_expires_at=lease_expires_at,
                )
                try:
                    with session.begin_nested():
                        session.add(stored)
                        session.flush()
                except IntegrityError:
                    stored = session.scalar(
                        select(TelegramUpdate)
                        .where(TelegramUpdate.update_id == update_id)
                        .with_for_update()
                    )
                else:
                    self._audit(
                        session,
                        workspace_id=workspace,
                        actor_id=actor,
                        action="telegram.update_claimed",
                        entity_type="telegram_update",
                        entity_id=str(update_id),
                        detail={"claim_fence": 1, "attempt_count": 1, "replay": False},
                        now=timestamp,
                    )
                    return TelegramUpdateClaim(
                        acquired=True,
                        claim_token=claim_token,
                        claim_fence=1,
                    )

            if stored is None:  # pragma: no cover - una PK concurrente debe ser visible
                return TelegramUpdateClaim(acquired=False, retryable=True)
            if (
                stored.workspace_id != workspace
                or stored.chat_id != normalized_chat
                or stored.telegram_user_id != normalized_telegram_user
                or stored.payload != normalized_payload
            ):
                # Nunca se procesa un payload distinto bajo una PK ya observada. Se
                # responde como duplicado terminal para no crear un oráculo remoto.
                return TelegramUpdateClaim(acquired=False)
            current_status = UpdateStatus(stored.status)
            if current_status is UpdateStatus.PROCESSED:
                return TelegramUpdateClaim(acquired=False)
            if (
                current_status is UpdateStatus.RECEIVED
                and stored.claim_token_hash is not None
                and stored.lease_expires_at is not None
                and stored.lease_expires_at > timestamp
            ):
                return TelegramUpdateClaim(acquired=False, retryable=True)

            replayable = stored.prepared_actions is not None or stored.business_result is not None
            if not replayable:
                stored.status = UpdateStatus.FAILED.value
                stored.processed_at = timestamp
                stored.error = stored.error or "ambiguous_processing_state:no_durable_result"
                stored.lease_expires_at = timestamp
                self._audit(
                    session,
                    workspace_id=stored.workspace_id,
                    actor_id=actor,
                    action="telegram.update_ambiguous",
                    entity_type="telegram_update",
                    entity_id=str(update_id),
                    detail={
                        "claim_fence": stored.claim_fence,
                        "attempt_count": stored.attempt_count,
                    },
                    now=timestamp,
                )
                return TelegramUpdateClaim(acquired=False)

            previous_fence = stored.claim_fence
            next_fence = previous_fence + 1
            next_attempt = stored.attempt_count + 1
            claimed = session.execute(
                update(TelegramUpdate)
                .where(
                    TelegramUpdate.update_id == update_id,
                    TelegramUpdate.claim_fence == previous_fence,
                    TelegramUpdate.status.in_(
                        (UpdateStatus.RECEIVED.value, UpdateStatus.FAILED.value)
                    ),
                )
                .values(
                    status=UpdateStatus.RECEIVED.value,
                    error=None,
                    processed_at=None,
                    claim_token_hash=claim_token_hash,
                    claim_fence=next_fence,
                    attempt_count=next_attempt,
                    claimed_at=timestamp,
                    lease_expires_at=lease_expires_at,
                )
            )
            if claimed.rowcount != 1:
                return TelegramUpdateClaim(acquired=False, retryable=True)
            stored.status = UpdateStatus.RECEIVED.value
            stored.error = None
            stored.processed_at = None
            stored.claim_token_hash = claim_token_hash
            stored.claim_fence = next_fence
            stored.attempt_count = next_attempt
            stored.claimed_at = timestamp
            stored.lease_expires_at = lease_expires_at
            self._audit(
                session,
                workspace_id=stored.workspace_id,
                actor_id=actor,
                action="telegram.update_reclaimed",
                entity_type="telegram_update",
                entity_id=str(update_id),
                detail={
                    "claim_fence": next_fence,
                    "attempt_count": next_attempt,
                    "replay": True,
                },
                now=timestamp,
            )
            prepared_actions = _normalize_prepared_telegram_actions(stored.prepared_actions)
            business_result = (
                _normalize_json(stored.business_result, field_name="business_result")
                if stored.business_result is not None
                else None
            )
            return TelegramUpdateClaim(
                acquired=True,
                claim_token=claim_token,
                claim_fence=next_fence,
                prepared_actions=prepared_actions,
                business_result=business_result,
            )

    def prepare_telegram_actions(
        self,
        update_id: int,
        actions: Sequence[Mapping[str, object]],
        *,
        claim_token: str,
        claim_fence: int,
        actor_id: str = TELEGRAM_ACTOR,
        now: datetime | None = None,
    ) -> TelegramUpdate:
        """Guarda la respuesta antes de cualquier llamada externa a Telegram."""

        normalized_actions = _normalize_prepared_telegram_actions(actions)
        assert normalized_actions is not None
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            stored = session.scalar(
                select(TelegramUpdate)
                .where(TelegramUpdate.update_id == update_id)
                .with_for_update()
            )
            if stored is None:
                raise NotFoundError(f"No existe el update de Telegram {update_id}")
            _require_telegram_update_claim(
                stored,
                claim_token=claim_token,
                claim_fence=claim_fence,
            )
            if UpdateStatus(stored.status) is not UpdateStatus.RECEIVED:
                raise ConflictError("El update de Telegram ya no está en procesamiento")
            if stored.lease_expires_at is None or stored.lease_expires_at <= timestamp:
                raise ConflictError("La lease del update de Telegram venció")
            if stored.prepared_actions is not None:
                if stored.prepared_actions != list(normalized_actions):
                    raise ConflictError("El update ya tiene otra respuesta preparada")
                return stored
            stored.prepared_actions = list(normalized_actions)
            self._audit(
                session,
                workspace_id=stored.workspace_id,
                actor_id=actor,
                action="telegram.actions_prepared",
                entity_type="telegram_update",
                entity_id=str(update_id),
                detail={
                    "claim_fence": stored.claim_fence,
                    "action_count": len(normalized_actions),
                },
                now=timestamp,
            )
            return stored

    def is_allowed(
        self,
        *,
        telegram_user_id: int,
        chat_id: int,
        permission: object,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> bool:
        """Implementa TelegramAuthorizer usando siempre `from.id`, no `chat.id`."""

        permission_value = getattr(permission, "value", permission)
        permission_map = {
            "telegram.access": Permission.VIEW_WORKSPACE,
            "telegram.status.view": Permission.VIEW_DRAFTS,
            "telegram.team.view": Permission.VIEW_WORKSPACE,
            "calendar.view": Permission.VIEW_AUTOMATION,
            "automation.mode.manage": Permission.MANAGE_AUTOMATION_MODE,
            "content.generate": Permission.CREATE_DRAFTS,
            "content.publish.request": Permission.PUBLISH_DRAFTS,
            "content.approve": Permission.REVIEW_DRAFTS,
            "content.reject": Permission.REVIEW_DRAFTS,
        }
        required_permission = permission_map.get(permission_value)
        if required_permission is None:
            return False
        try:
            workspace = _normalize_workspace(workspace_id)
            telegram_identity = _normalize_telegram_user_id(telegram_user_id)
            normalized_chat = _normalize_chat_id(chat_id)
            with self._sessions() as session:
                _user, membership = self._resolve_telegram_identity(
                    session,
                    workspace_id=workspace,
                    telegram_user_id=telegram_identity,
                    chat_id=normalized_chat,
                )
                require_permission(Role(membership.role), required_permission)
        except (AuthorizationError, ValueError):
            return False
        return True

    def consume_callback_nonce(
        self,
        nonce: str,
        *,
        decision: object,
        telegram_user_id: int,
        chat_id: int,
    ) -> object | None:
        """Adapta el registro persistido al protocolo CallbackNonceStore del bot."""

        decision_value = getattr(decision, "value", decision)
        try:
            intent = self.consume_callback_intent(
                nonce,
                decision_value,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
            )
        except (AuthorizationError, PlatformStoreError, ValueError):
            return None
        # Importación local: platform_store permanece utilizable sin arrancar el bot.
        from colmat_x.telegram_bot import CallbackDecision as TelegramCallbackDecision
        from colmat_x.telegram_bot import CallbackIntent as TelegramCallbackIntent

        return TelegramCallbackIntent(
            decision=TelegramCallbackDecision(intent.action),
            post_id=intent.draft_id,
            snapshot_hash=intent.snapshot_hash,
            telegram_user_id=int(intent.telegram_user_id),
            chat_id=int(intent.chat_id),
        )

    def finish_telegram_update(
        self,
        update_id: int,
        *,
        actor_id: str = TELEGRAM_ACTOR,
        error: str | None = None,
        claim_token: str | None = None,
        claim_fence: int | None = None,
        now: datetime | None = None,
    ) -> TelegramUpdate:
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        normalized_error = _normalize_note(error, required=False)
        with self._sessions.begin() as session:
            update = session.get(TelegramUpdate, update_id)
            if update is None:
                raise NotFoundError(f"No existe el update de Telegram {update_id}")
            if update.claim_token_hash is not None:
                if claim_token is None or claim_fence is None:
                    raise AuthorizationError("El cierre exige las credenciales del claim")
                _require_telegram_update_claim(
                    update,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                )
            elif claim_token is not None or claim_fence is not None:
                raise ConflictError("El update legado no tiene un claim cercado")
            current_status = UpdateStatus(update.status)
            if current_status is UpdateStatus.PROCESSED and normalized_error is None:
                return update
            if current_status is not UpdateStatus.RECEIVED:
                raise ConflictError("La actualización de Telegram ya fue finalizada")
            update.status = (
                UpdateStatus.FAILED.value if normalized_error else UpdateStatus.PROCESSED.value
            )
            update.error = normalized_error
            update.processed_at = timestamp
            if normalized_error:
                update.lease_expires_at = timestamp
            self._audit(
                session,
                workspace_id=update.workspace_id,
                actor_id=actor,
                action=(
                    "telegram.update_failed" if normalized_error else "telegram.update_processed"
                ),
                entity_type="telegram_update",
                entity_id=str(update_id),
                detail={"error": normalized_error, "claim_fence": update.claim_fence},
                now=timestamp,
            )
            return update

    def enqueue_generation_request(
        self,
        brief: str,
        *,
        actor_id: str,
        telegram_user_id: int | str,
        chat_id: int | str,
        idempotency_key: str,
        generate_image: bool = True,
        category: EditorialCategory | str | None = None,
        institution: Institution | str | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> GenerationRequest:
        """Encola generación humana sin construir clientes de IA ni hacer red."""

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        normalized_brief = _normalize_short_text(brief, "brief", 1_000)
        if len(normalized_brief) < 10:
            raise ValueError("brief debe tener al menos 10 caracteres")
        normalized_key = _normalize_short_text(idempotency_key, "idempotency_key", 120)
        telegram_identity = _normalize_telegram_user_id(telegram_user_id)
        normalized_chat = _normalize_chat_id(chat_id)
        normalized_image = _normalize_bool(generate_image, "generate_image")
        normalized_category = EditorialCategory(category).value if category is not None else None
        normalized_institution = Institution(institution).value if institution is not None else None
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            resolved_user, membership = self._resolve_telegram_identity(
                session,
                workspace_id=workspace,
                telegram_user_id=telegram_identity,
                chat_id=normalized_chat,
            )
            if resolved_user.id != actor:
                raise AuthorizationError("La identidad Telegram no coincide con el actor")
            require_permission(Role(membership.role), Permission.CREATE_DRAFTS)
            existing = session.scalar(
                select(GenerationRequest).where(
                    GenerationRequest.workspace_id == workspace,
                    GenerationRequest.idempotency_key == normalized_key,
                )
            )
            if existing is not None:
                _require_matching_generation_request(
                    existing,
                    actor_id=actor,
                    brief=normalized_brief,
                    telegram_user_id=telegram_identity,
                    chat_id=normalized_chat,
                    generate_image=normalized_image,
                    category=normalized_category,
                    institution=normalized_institution,
                )
                return existing
            request = GenerationRequest(
                workspace_id=workspace,
                idempotency_key=normalized_key,
                brief=normalized_brief,
                category=normalized_category,
                institution=normalized_institution,
                generate_image=normalized_image,
                requested_by=actor,
                telegram_user_id=telegram_identity,
                chat_id=normalized_chat,
                status=GenerationRequestStatus.QUEUED.value,
                claim_fence=0,
                created_at=timestamp,
                updated_at=timestamp,
            )
            try:
                with session.begin_nested():
                    session.add(request)
                    session.flush()
            except IntegrityError as exc:
                raced = session.scalar(
                    select(GenerationRequest).where(
                        GenerationRequest.workspace_id == workspace,
                        GenerationRequest.idempotency_key == normalized_key,
                    )
                )
                if raced is None:
                    raise ConflictError("No se pudo encolar la generación") from exc
                _require_matching_generation_request(
                    raced,
                    actor_id=actor,
                    brief=normalized_brief,
                    telegram_user_id=telegram_identity,
                    chat_id=normalized_chat,
                    generate_image=normalized_image,
                    category=normalized_category,
                    institution=normalized_institution,
                )
                return raced
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="generation_request.queued",
                entity_type="generation_request",
                entity_id=request.id,
                detail={
                    "idempotency_key": normalized_key,
                    "generate_image": normalized_image,
                    "category": normalized_category,
                    "institution": normalized_institution,
                    "telegram_user_id": telegram_identity,
                    "chat_id": normalized_chat,
                },
                now=timestamp,
            )
            return request

    def claim_generation_request(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        lease_seconds: int = DEFAULT_GENERATION_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> GenerationClaim | None:
        """Entrega una solicitud a un worker OpenClaw con lease y fence opacos."""

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        normalized_lease = _normalize_bounded_int(
            lease_seconds,
            "lease_seconds",
            minimum=1,
            maximum=MAX_GENERATION_LEASE_SECONDS,
        )
        timestamp = _normalize_now(now)
        lease_expires_at = timestamp + timedelta(seconds=normalized_lease)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_SCHEDULE)
            self._expire_generation_claims(
                session,
                workspace_id=workspace,
                actor_id=actor,
                now=timestamp,
            )
            candidate_id = (
                select(GenerationRequest)
                .with_only_columns(GenerationRequest.id)
                .where(
                    GenerationRequest.workspace_id == workspace,
                    GenerationRequest.status == GenerationRequestStatus.QUEUED.value,
                )
                .order_by(GenerationRequest.created_at, GenerationRequest.id)
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            claim_token = secrets.token_urlsafe(32)
            token_hash = _hash_generation_claim_token(claim_token)
            claimed_id = session.scalar(
                update(GenerationRequest)
                .where(
                    GenerationRequest.id == candidate_id,
                    GenerationRequest.status == GenerationRequestStatus.QUEUED.value,
                )
                .values(
                    status=GenerationRequestStatus.CLAIMED.value,
                    claim_token_hash=token_hash,
                    claim_fence=GenerationRequest.claim_fence + 1,
                    claimed_by=actor,
                    claimed_at=timestamp,
                    lease_expires_at=lease_expires_at,
                    updated_at=timestamp,
                )
                .returning(GenerationRequest.id)
            )
            if claimed_id is None:
                return None
            request = session.get(GenerationRequest, claimed_id)
            if request is None:  # pragma: no cover - protegido por RETURNING
                raise NotFoundError("La solicitud desapareció durante el claim")
            session.refresh(request)
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="generation_request.claimed",
                entity_type="generation_request",
                entity_id=request.id,
                detail={
                    "claim_fence": request.claim_fence,
                    "lease_expires_at": _format_time(lease_expires_at),
                },
                now=timestamp,
            )
            return GenerationClaim(
                request=request,
                claim_token=claim_token,
                claim_fence=request.claim_fence,
                lease_expires_at=lease_expires_at,
            )

    def validate_generation_claim(
        self,
        request_id: str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        now: datetime | None = None,
    ) -> GenerationRequest:
        normalized_id = _normalize_entity_id(request_id)
        actor = _normalize_actor(actor_id)
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        timestamp = _normalize_now(now)
        expired = False
        with self._sessions.begin() as session:
            request = session.scalar(
                select(GenerationRequest)
                .where(GenerationRequest.id == normalized_id)
                .with_for_update()
            )
            if request is None:
                raise NotFoundError(f"No existe la solicitud de generación '{normalized_id}'")
            self._authorize(session, actor, request.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_generation_claim_credentials(
                request,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            if GenerationRequestStatus(request.status) is not GenerationRequestStatus.CLAIMED:
                raise ConflictError("La solicitud de generación ya no está reclamada")
            if request.lease_expires_at is None or request.lease_expires_at <= timestamp:
                self._expire_generation_request(
                    session,
                    request=request,
                    actor_id=actor,
                    now=timestamp,
                )
                expired = True
        if expired:
            raise ConflictError(
                "La lease de generación venció; la solicitud quedó UNKNOWN y no se reencolará"
            )
        return request

    def complete_generation_request(
        self,
        request_id: str,
        *,
        actor_id: str,
        author_actor_id: str,
        claim_token: str,
        claim_fence: int,
        text: str,
        category: str,
        publish_at: datetime | date | str,
        evidence: Any,
        engagement_score: int,
        image: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[GenerationRequest, Draft, Revision, GenerationNotification]:
        """Crea draft, revisión y outbox destinada a un revisor en una transacción."""

        normalized_id = _normalize_entity_id(request_id)
        worker = _normalize_actor(actor_id)
        author = _normalize_actor(author_actor_id)
        if worker == author:
            raise AuthorizationError("El worker que reclama no puede ser el autor editorial")
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        score = _normalize_bounded_int(
            engagement_score,
            "engagement_score",
            minimum=0,
            maximum=100,
        )
        timestamp = _normalize_now(now)
        normalized_image = _normalize_generation_image(image)
        material = _normalize_revision_material(
            text=text,
            category=category,
            publish_at=publish_at,
            evidence=evidence,
            image_sha256=(normalized_image["sha256"] if normalized_image is not None else None),
        )
        expired = False
        completed: tuple[GenerationRequest, Draft, Revision, GenerationNotification] | None = None
        with self._sessions.begin() as session:
            request = session.scalar(
                select(GenerationRequest)
                .where(GenerationRequest.id == normalized_id)
                .with_for_update()
            )
            if request is None:
                raise NotFoundError(f"No existe la solicitud de generación '{normalized_id}'")
            self._authorize(session, worker, request.workspace_id, Permission.MANAGE_SCHEDULE)
            self._authorize(session, author, request.workspace_id, Permission.CREATE_DRAFTS)
            self._authorize(session, author, request.workspace_id, Permission.SUBMIT_DRAFTS)
            if normalized_image is not None:
                self._authorize(session, author, request.workspace_id, Permission.MANAGE_MEDIA)
            if author == request.requested_by:
                raise AuthorizationError("La identidad solicitante no puede ser el autor IA")
            _require_generation_claim_credentials(
                request,
                actor_id=worker,
                claim_token=token,
                claim_fence=fence,
            )
            current = GenerationRequestStatus(request.status)
            if current is GenerationRequestStatus.SUCCEEDED:
                if request.draft_id is None or request.revision_id is None:
                    raise ConflictError("La generación exitosa no tiene resultado íntegro")
                draft = self._get_draft(session, request.draft_id)
                revision = session.get(Revision, request.revision_id)
                notification = session.scalar(
                    select(GenerationNotification).where(
                        GenerationNotification.generation_request_id == request.id
                    )
                )
                if revision is None or notification is None:
                    raise ConflictError("La generación exitosa no tiene outbox íntegro")
                return request, draft, revision, notification
            if current is not GenerationRequestStatus.CLAIMED:
                raise ConflictError("La solicitud de generación no está reclamada")
            if request.lease_expires_at is None or request.lease_expires_at <= timestamp:
                self._expire_generation_request(
                    session,
                    request=request,
                    actor_id=worker,
                    now=timestamp,
                )
                expired = True
            else:
                revision_id = _new_id()
                draft = Draft(
                    id=_new_id(),
                    workspace_id=request.workspace_id,
                    status=DraftStatus.IN_REVIEW.value,
                    created_by=author,
                    current_revision_id=revision_id,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                revision = Revision(
                    id=revision_id,
                    draft_id=draft.id,
                    revision_number=1,
                    created_by=author,
                    created_at=timestamp,
                    **material,
                )
                session.add_all((draft, revision))
                session.flush()
                asset: MediaAsset | None = None
                if normalized_image is not None:
                    asset = session.scalar(
                        select(MediaAsset).where(
                            MediaAsset.workspace_id == request.workspace_id,
                            MediaAsset.sha256 == normalized_image["sha256"],
                        )
                    )
                    if asset is None:
                        asset = MediaAsset(
                            workspace_id=request.workspace_id,
                            draft_id=draft.id,
                            kind="generated_image",
                            url=normalized_image["url"],
                            sha256=normalized_image["sha256"],
                            mime_type=normalized_image["mime_type"],
                            byte_size=normalized_image["byte_size"],
                            asset_metadata=normalized_image["metadata"],
                            created_by=author,
                            created_at=timestamp,
                        )
                        session.add(asset)
                        session.flush()
                    elif (
                        asset.mime_type != normalized_image["mime_type"]
                        or asset.byte_size != normalized_image["byte_size"]
                    ):
                        raise ConflictError("El hash de imagen ya existe con otros metadatos")
                self._audit(
                    session,
                    workspace_id=request.workspace_id,
                    actor_id=author,
                    action="draft.created",
                    entity_type="draft",
                    entity_id=draft.id,
                    detail={
                        "revision_id": revision.id,
                        "snapshot_hash": revision.snapshot_hash,
                        "generation_request_id": request.id,
                    },
                    now=timestamp,
                )
                if asset is not None:
                    self._audit(
                        session,
                        workspace_id=request.workspace_id,
                        actor_id=author,
                        action="media.registered",
                        entity_type="media_asset",
                        entity_id=asset.id,
                        detail={
                            "draft_id": draft.id,
                            "sha256": asset.sha256,
                            "kind": asset.kind,
                        },
                        now=timestamp,
                    )
                self._audit(
                    session,
                    workspace_id=request.workspace_id,
                    actor_id=author,
                    action="draft.submitted",
                    entity_type="draft",
                    entity_id=draft.id,
                    detail={
                        "revision_id": revision.id,
                        "snapshot_hash": revision.snapshot_hash,
                        "generation_request_id": request.id,
                    },
                    now=timestamp,
                )
                reviewer_binding = self._select_generation_reviewer_binding(
                    session,
                    request=request,
                    author_actor_id=author,
                )
                notification = GenerationNotification(
                    workspace_id=request.workspace_id,
                    generation_request_id=request.id,
                    draft_id=draft.id,
                    revision_id=revision.id,
                    snapshot_hash=revision.snapshot_hash,
                    telegram_user_id=reviewer_binding.telegram_user_id,
                    chat_id=reviewer_binding.chat_id,
                    text=revision.text,
                    engagement_score=score,
                    media_sha256=revision.image_sha256,
                    status=GenerationNotificationStatus.QUEUED.value,
                    claim_fence=0,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(notification)
                session.flush()
                request.status = GenerationRequestStatus.SUCCEEDED.value
                request.draft_id = draft.id
                request.revision_id = revision.id
                request.error = None
                request.updated_at = timestamp
                request.finished_at = timestamp
                self._audit(
                    session,
                    workspace_id=request.workspace_id,
                    actor_id=worker,
                    action="generation_request.succeeded",
                    entity_type="generation_request",
                    entity_id=request.id,
                    detail={
                        "claim_fence": request.claim_fence,
                        "draft_id": draft.id,
                        "revision_id": revision.id,
                        "snapshot_hash": revision.snapshot_hash,
                        "notification_id": notification.id,
                        "human_review": True,
                    },
                    now=timestamp,
                )
                self._audit(
                    session,
                    workspace_id=request.workspace_id,
                    actor_id=worker,
                    action="generation_notification.queued",
                    entity_type="generation_notification",
                    entity_id=notification.id,
                    detail={
                        "generation_request_id": request.id,
                        "draft_id": draft.id,
                        "snapshot_hash": revision.snapshot_hash,
                        "media_sha256": revision.image_sha256,
                        "reviewer_user_id": reviewer_binding.user_id,
                        "telegram_user_id": reviewer_binding.telegram_user_id,
                        "chat_id": reviewer_binding.chat_id,
                    },
                    now=timestamp,
                )
                completed = request, draft, revision, notification
        if expired:
            raise ConflictError(
                "La lease de generación venció; la solicitud quedó UNKNOWN y no se reencolará"
            )
        if completed is None:  # pragma: no cover - todas las ramas anteriores terminan
            raise ConflictError("No fue posible completar la generación")
        return completed

    def finish_generation_request(
        self,
        request_id: str,
        status: GenerationRequestStatus | str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        error: str,
        now: datetime | None = None,
    ) -> GenerationRequest:
        """Cierra un fallo previo al draft; SUCCESS solo lo emite el commit compuesto."""

        normalized_id = _normalize_entity_id(request_id)
        actor = _normalize_actor(actor_id)
        target = _normalize_generation_request_status(status)
        if target not in {GenerationRequestStatus.FAILED, GenerationRequestStatus.UNKNOWN}:
            raise ValueError("finish_generation_request solo admite failed o unknown")
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        normalized_error = _sanitize_automation_error(error)
        if normalized_error is None:
            raise ValueError("El cierre fallido exige un detalle seguro")
        timestamp = _normalize_now(now)
        expired = False
        with self._sessions.begin() as session:
            request = session.scalar(
                select(GenerationRequest)
                .where(GenerationRequest.id == normalized_id)
                .with_for_update()
            )
            if request is None:
                raise NotFoundError(f"No existe la solicitud de generación '{normalized_id}'")
            self._authorize(session, actor, request.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_generation_claim_credentials(
                request,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            current = GenerationRequestStatus(request.status)
            if current in {
                GenerationRequestStatus.SUCCEEDED,
                GenerationRequestStatus.FAILED,
                GenerationRequestStatus.UNKNOWN,
            }:
                if current is not target:
                    raise ConflictError("La solicitud ya tiene otro resultado terminal")
                return request
            if current is not GenerationRequestStatus.CLAIMED:
                raise ConflictError("La solicitud de generación no está reclamada")
            if request.lease_expires_at is None or request.lease_expires_at <= timestamp:
                self._expire_generation_request(
                    session,
                    request=request,
                    actor_id=actor,
                    now=timestamp,
                )
                expired = True
            else:
                request.status = target.value
                request.error = normalized_error
                request.updated_at = timestamp
                request.finished_at = timestamp
                self._audit(
                    session,
                    workspace_id=request.workspace_id,
                    actor_id=actor,
                    action=f"generation_request.{target.value}",
                    entity_type="generation_request",
                    entity_id=request.id,
                    detail={
                        "claim_fence": request.claim_fence,
                        "error": normalized_error,
                    },
                    now=timestamp,
                )
        if expired:
            raise ConflictError(
                "La lease de generación venció; la solicitud quedó UNKNOWN y no se reencolará"
            )
        return request

    def expire_generation_claims(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> list[GenerationRequest]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_SCHEDULE)
            return self._expire_generation_claims(
                session,
                workspace_id=workspace,
                actor_id=actor,
                now=timestamp,
            )

    def get_generation_request(
        self,
        request_id: str,
        *,
        actor_id: str,
    ) -> GenerationRequest:
        normalized_id = _normalize_entity_id(request_id)
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            request = session.get(GenerationRequest, normalized_id)
            if request is None:
                raise NotFoundError(f"No existe la solicitud de generación '{normalized_id}'")
            self._authorize(session, actor, request.workspace_id, Permission.VIEW_DRAFTS)
            return request

    def list_generation_requests(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        status: GenerationRequestStatus | str | None = None,
        limit: int = 100,
    ) -> list[GenerationRequest]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        normalized_status = (
            _normalize_generation_request_status(status) if status is not None else None
        )
        normalized_limit = _normalize_bounded_int(limit, "limit", minimum=1, maximum=1000)
        with self._sessions() as session:
            self._authorize(session, actor, workspace, Permission.VIEW_DRAFTS)
            statement = select(GenerationRequest).where(GenerationRequest.workspace_id == workspace)
            if normalized_status is not None:
                statement = statement.where(GenerationRequest.status == normalized_status.value)
            return list(
                session.scalars(
                    statement.order_by(
                        GenerationRequest.created_at.desc(), GenerationRequest.id.desc()
                    ).limit(normalized_limit)
                )
            )

    def claim_generation_notification(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        lease_seconds: int = DEFAULT_GENERATION_NOTIFICATION_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> GenerationNotificationClaim | None:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        lease = _normalize_bounded_int(
            lease_seconds,
            "lease_seconds",
            minimum=1,
            maximum=MAX_GENERATION_NOTIFICATION_LEASE_SECONDS,
        )
        timestamp = _normalize_now(now)
        lease_expires_at = timestamp + timedelta(seconds=lease)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_SCHEDULE)
            self._expire_generation_notification_claims(
                session,
                workspace_id=workspace,
                actor_id=actor,
                now=timestamp,
            )
            candidate_id = (
                select(GenerationNotification)
                .with_only_columns(GenerationNotification.id)
                .where(
                    GenerationNotification.workspace_id == workspace,
                    GenerationNotification.status == GenerationNotificationStatus.QUEUED.value,
                )
                .order_by(GenerationNotification.created_at, GenerationNotification.id)
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            claim_token = secrets.token_urlsafe(32)
            token_hash = _hash_generation_claim_token(claim_token)
            claimed_id = session.scalar(
                update(GenerationNotification)
                .where(
                    GenerationNotification.id == candidate_id,
                    GenerationNotification.status == GenerationNotificationStatus.QUEUED.value,
                )
                .values(
                    status=GenerationNotificationStatus.CLAIMED.value,
                    claim_token_hash=token_hash,
                    claim_fence=GenerationNotification.claim_fence + 1,
                    claimed_by=actor,
                    claimed_at=timestamp,
                    lease_expires_at=lease_expires_at,
                    updated_at=timestamp,
                )
                .returning(GenerationNotification.id)
            )
            if claimed_id is None:
                return None
            notification = session.get(GenerationNotification, claimed_id)
            if notification is None:  # pragma: no cover
                raise NotFoundError("La notificación desapareció durante el claim")
            session.refresh(notification)
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="generation_notification.claimed",
                entity_type="generation_notification",
                entity_id=notification.id,
                detail={
                    "claim_fence": notification.claim_fence,
                    "lease_expires_at": _format_time(lease_expires_at),
                },
                now=timestamp,
            )
            return GenerationNotificationClaim(
                notification=notification,
                claim_token=claim_token,
                claim_fence=notification.claim_fence,
                lease_expires_at=lease_expires_at,
            )

    def validate_generation_notification_claim(
        self,
        notification_id: str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        now: datetime | None = None,
    ) -> GenerationNotification:
        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        timestamp = _normalize_now(now)
        expired = False
        with self._sessions.begin() as session:
            notification = session.scalar(
                select(GenerationNotification)
                .where(GenerationNotification.id == normalized_id)
                .with_for_update()
            )
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_generation_notification_claim_credentials(
                notification,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            if (
                GenerationNotificationStatus(notification.status)
                is not GenerationNotificationStatus.CLAIMED
            ):
                raise ConflictError("La notificación ya no está reclamada")
            if notification.lease_expires_at is None or notification.lease_expires_at <= timestamp:
                self._expire_generation_notification(
                    session,
                    notification=notification,
                    actor_id=actor,
                    now=timestamp,
                )
                expired = True
        if expired:
            raise ConflictError("La lease de notificación venció; quedó UNKNOWN y no se reenviará")
        return notification

    def record_generation_notification_photo(
        self,
        notification_id: str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        telegram_message_id: int,
        now: datetime | None = None,
    ) -> GenerationNotification:
        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        message_id = _normalize_positive_int(
            telegram_message_id,
            "telegram_message_id",
            maximum=9_223_372_036_854_775_807,
        )
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            notification = session.scalar(
                select(GenerationNotification)
                .where(GenerationNotification.id == normalized_id)
                .with_for_update()
            )
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_generation_notification_claim_credentials(
                notification,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            if (
                GenerationNotificationStatus(notification.status)
                is not GenerationNotificationStatus.CLAIMED
                or notification.lease_expires_at is None
                or notification.lease_expires_at <= timestamp
            ):
                raise ConflictError("La notificación ya no tiene una lease vigente")
            if notification.photo_message_id is not None:
                if notification.photo_message_id != message_id:
                    raise ConflictError("La foto ya tiene otro identificador de Telegram")
                return notification
            notification.photo_message_id = message_id
            notification.photo_sent_at = timestamp
            notification.updated_at = timestamp
            self._audit(
                session,
                workspace_id=notification.workspace_id,
                actor_id=actor,
                action="generation_notification.photo_recorded",
                entity_type="generation_notification",
                entity_id=notification.id,
                detail={"claim_fence": fence, "telegram_message_id": message_id},
                now=timestamp,
            )
        return notification

    def prepare_generation_notification_callbacks(
        self,
        notification_id: str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> tuple[IssuedCallbackIntent, IssuedCallbackIntent]:
        """Emite nonces solo en memoria y conserva únicamente sus IDs hasheados."""

        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        timestamp = _normalize_now(now)
        expiration = _normalize_now(expires_at)
        with self._sessions.begin() as session:
            notification = session.scalar(
                select(GenerationNotification)
                .where(GenerationNotification.id == normalized_id)
                .with_for_update()
            )
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_generation_notification_claim_credentials(
                notification,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            if (
                GenerationNotificationStatus(notification.status)
                is not GenerationNotificationStatus.CLAIMED
                or notification.lease_expires_at is None
                or notification.lease_expires_at <= timestamp
            ):
                raise ConflictError("La notificación ya no tiene una lease vigente")
            if (
                notification.approve_intent_id is not None
                or notification.reject_intent_id is not None
            ):
                raise ConflictError(
                    "Los callbacks ya se prepararon; su entrega requiere conciliación"
                )
            approve, reject = self.issue_review_callback_intents(
                notification.draft_id,
                expected_snapshot_hash=notification.snapshot_hash,
                telegram_user_id=notification.telegram_user_id,
                chat_id=notification.chat_id,
                expires_at=expiration,
                actor_id=actor,
                now=timestamp,
                _session=session,
            )
            notification.approve_intent_id = approve.intent.id
            notification.reject_intent_id = reject.intent.id
            notification.updated_at = timestamp
            self._audit(
                session,
                workspace_id=notification.workspace_id,
                actor_id=actor,
                action="generation_notification.callbacks_prepared",
                entity_type="generation_notification",
                entity_id=notification.id,
                detail={
                    "approve_intent_id": approve.intent.id,
                    "reject_intent_id": reject.intent.id,
                    "claim_fence": fence,
                    "expires_at": _format_time(expiration),
                },
                now=timestamp,
            )
            return approve, reject

    def finish_generation_notification(
        self,
        notification_id: str,
        status: GenerationNotificationStatus | str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        review_message_id: int | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> GenerationNotification:
        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        target = _normalize_generation_notification_status(status)
        if target in {
            GenerationNotificationStatus.QUEUED,
            GenerationNotificationStatus.CLAIMED,
        }:
            raise ValueError("finish_generation_notification exige un estado terminal")
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        normalized_message_id = (
            _normalize_positive_int(
                review_message_id,
                "review_message_id",
                maximum=9_223_372_036_854_775_807,
            )
            if review_message_id is not None
            else None
        )
        if target is GenerationNotificationStatus.SENT and normalized_message_id is None:
            raise ValueError("Una notificación enviada requiere review_message_id")
        normalized_error = _sanitize_automation_error(error)
        if target is not GenerationNotificationStatus.SENT and normalized_error is None:
            raise ValueError("Una notificación fallida exige error seguro")
        timestamp = _normalize_now(now)
        expired = False
        with self._sessions.begin() as session:
            notification = session.scalar(
                select(GenerationNotification)
                .where(GenerationNotification.id == normalized_id)
                .with_for_update()
            )
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_generation_notification_claim_credentials(
                notification,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            current = GenerationNotificationStatus(notification.status)
            if current in {
                GenerationNotificationStatus.SENT,
                GenerationNotificationStatus.FAILED,
                GenerationNotificationStatus.UNKNOWN,
            }:
                if current is not target:
                    raise ConflictError("La notificación ya tiene otro estado terminal")
                return notification
            if current is not GenerationNotificationStatus.CLAIMED:
                raise ConflictError("La notificación no está reclamada")
            if notification.lease_expires_at is None or notification.lease_expires_at <= timestamp:
                self._expire_generation_notification(
                    session,
                    notification=notification,
                    actor_id=actor,
                    now=timestamp,
                )
                expired = True
            else:
                notification.status = target.value
                notification.review_message_id = normalized_message_id
                notification.sent_at = (
                    timestamp if target is GenerationNotificationStatus.SENT else None
                )
                notification.error = normalized_error
                notification.updated_at = timestamp
                notification.finished_at = timestamp
                self._audit(
                    session,
                    workspace_id=notification.workspace_id,
                    actor_id=actor,
                    action=f"generation_notification.{target.value}",
                    entity_type="generation_notification",
                    entity_id=notification.id,
                    detail={
                        "claim_fence": fence,
                        "generation_request_id": notification.generation_request_id,
                        "draft_id": notification.draft_id,
                        "review_message_id": normalized_message_id,
                        "error": normalized_error,
                    },
                    now=timestamp,
                )
        if expired:
            raise ConflictError("La lease de notificación venció; quedó UNKNOWN y no se reenviará")
        return notification

    def get_generation_notification(
        self,
        notification_id: str,
        *,
        actor_id: str,
    ) -> GenerationNotification:
        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            notification = session.get(GenerationNotification, normalized_id)
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.VIEW_DRAFTS)
            return notification

    def enqueue_publication_request(
        self,
        draft_id: str,
        *,
        actor_id: str,
        expected_snapshot_hash: str,
        idempotency_key: str,
        channel: str = "x",
        now: datetime | None = None,
    ) -> PublicationRequest:
        """Encola un snapshot aprobado sin iniciar ninguna llamada al proveedor."""

        actor = _normalize_actor(actor_id)
        expected = _normalize_sha256(expected_snapshot_hash, required=True)
        normalized_channel = _normalize_short_text(channel, "channel", 30).lower()
        normalized_key = _normalize_short_text(idempotency_key, "idempotency_key", 120)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            draft = self._get_draft_for_update(session, draft_id)
            self._authorize(session, actor, draft.workspace_id, Permission.PUBLISH_DRAFTS)
            existing = session.scalar(
                select(PublicationRequest).where(
                    PublicationRequest.workspace_id == draft.workspace_id,
                    PublicationRequest.channel == normalized_channel,
                    PublicationRequest.idempotency_key == normalized_key,
                )
            )
            if existing is not None:
                _require_matching_publication_request(
                    existing,
                    draft_id=draft.id,
                    snapshot_hash=expected,
                )
                return existing
            prior_snapshot = session.scalar(
                select(PublicationRequest).where(
                    PublicationRequest.workspace_id == draft.workspace_id,
                    PublicationRequest.channel == normalized_channel,
                    PublicationRequest.draft_id == draft.id,
                    PublicationRequest.revision_id == draft.current_revision_id,
                )
            )
            if prior_snapshot is not None:
                raise ConflictError(
                    "El snapshot ya tiene una solicitud con otra clave de idempotencia"
                )
            revision, approval = self._approved_snapshot_for_publication(
                session,
                draft=draft,
                expected_snapshot_hash=expected,
            )
            request = PublicationRequest(
                workspace_id=draft.workspace_id,
                draft_id=draft.id,
                revision_id=revision.id,
                approval_id=approval.id,
                requested_by=actor,
                channel=normalized_channel,
                idempotency_key=normalized_key,
                snapshot_hash=revision.snapshot_hash,
                status=PublicationRequestStatus.QUEUED.value,
                claim_fence=0,
                created_at=timestamp,
                updated_at=timestamp,
            )
            try:
                with session.begin_nested():
                    session.add(request)
                    session.flush()
            except IntegrityError as exc:
                raced = session.scalar(
                    select(PublicationRequest).where(
                        PublicationRequest.workspace_id == draft.workspace_id,
                        PublicationRequest.channel == normalized_channel,
                        PublicationRequest.idempotency_key == normalized_key,
                    )
                )
                if raced is not None:
                    _require_matching_publication_request(
                        raced,
                        draft_id=draft.id,
                        snapshot_hash=expected,
                    )
                    return raced
                if (
                    session.scalar(
                        select(PublicationRequest.id).where(
                            PublicationRequest.workspace_id == draft.workspace_id,
                            PublicationRequest.channel == normalized_channel,
                            PublicationRequest.draft_id == draft.id,
                            PublicationRequest.revision_id == revision.id,
                        )
                    )
                    is not None
                ):
                    raise ConflictError(
                        "El snapshot ya tiene una solicitud con otra clave de idempotencia"
                    ) from exc
                raise ConflictError("No se pudo encolar la solicitud de publicación") from exc
            self._audit(
                session,
                workspace_id=draft.workspace_id,
                actor_id=actor,
                action="publication_request.queued",
                entity_type="publication_request",
                entity_id=request.id,
                detail={
                    "draft_id": draft.id,
                    "revision_id": revision.id,
                    "approval_id": approval.id,
                    "snapshot_hash": revision.snapshot_hash,
                    "channel": normalized_channel,
                    "idempotency_key": normalized_key,
                },
                now=timestamp,
            )
            return request

    def has_queued_publication_request(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        channel: str = "x",
        now: datetime | None = None,
    ) -> bool:
        """Comprueba trabajo publicable sin contactar al proveedor.

        El mismo cierre transaccional reconcilia leases vencidas antes del peek,
        de modo que un claim huérfano no quede pendiente solo porque no existen
        filas ``QUEUED``.
        """

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        normalized_channel = _normalize_short_text(channel, "channel", 30).lower()
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.PUBLISH_DRAFTS)
            self._expire_publication_claims(
                session,
                workspace_id=workspace,
                actor_id=actor,
                now=timestamp,
            )
            return (
                session.scalar(
                    select(PublicationRequest.id)
                    .join(Revision, Revision.id == PublicationRequest.revision_id)
                    .where(
                        PublicationRequest.workspace_id == workspace,
                        PublicationRequest.channel == normalized_channel,
                        PublicationRequest.status == PublicationRequestStatus.QUEUED.value,
                        Revision.publish_at <= timestamp,
                    )
                    .order_by(
                        Revision.publish_at,
                        PublicationRequest.created_at,
                        PublicationRequest.id,
                    )
                    .limit(1)
                )
                is not None
            )

    def claim_publication_request(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        channel: str = "x",
        lease_seconds: int = DEFAULT_PUBLICATION_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> PublicationClaim | None:
        """Reclama una solicitud una sola vez y entrega el token opaco de fencing."""

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        normalized_channel = _normalize_short_text(channel, "channel", 30).lower()
        normalized_lease = _normalize_bounded_int(
            lease_seconds,
            "lease_seconds",
            minimum=1,
            maximum=MAX_PUBLICATION_LEASE_SECONDS,
        )
        timestamp = _normalize_now(now)
        lease_expires_at = timestamp + timedelta(seconds=normalized_lease)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.PUBLISH_DRAFTS)
            self._expire_publication_claims(
                session,
                workspace_id=workspace,
                actor_id=actor,
                now=timestamp,
            )
            candidate_id = (
                select(PublicationRequest)
                .with_only_columns(PublicationRequest.id)
                .join(Revision, Revision.id == PublicationRequest.revision_id)
                .where(
                    PublicationRequest.workspace_id == workspace,
                    PublicationRequest.channel == normalized_channel,
                    PublicationRequest.status == PublicationRequestStatus.QUEUED.value,
                    Revision.publish_at <= timestamp,
                )
                .order_by(
                    Revision.publish_at,
                    PublicationRequest.created_at,
                    PublicationRequest.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True, of=PublicationRequest)
                .scalar_subquery()
            )
            claim_token = secrets.token_urlsafe(32)
            claim_token_hash = _hash_publication_claim_token(claim_token)
            claimed_id = session.scalar(
                update(PublicationRequest)
                .where(
                    PublicationRequest.id == candidate_id,
                    PublicationRequest.status == PublicationRequestStatus.QUEUED.value,
                )
                .values(
                    status=PublicationRequestStatus.CLAIMED.value,
                    claim_token_hash=claim_token_hash,
                    claim_fence=PublicationRequest.claim_fence + 1,
                    claimed_by=actor,
                    claimed_at=timestamp,
                    lease_expires_at=lease_expires_at,
                    updated_at=timestamp,
                )
                .returning(PublicationRequest.id)
            )
            if claimed_id is None:
                return None
            request = session.get(PublicationRequest, claimed_id)
            if request is None:  # pragma: no cover - protegido por RETURNING
                raise NotFoundError("La solicitud desapareció durante el claim")
            draft = self._get_draft(session, request.draft_id)
            revision, approval = self._approved_snapshot_for_publication(
                session,
                draft=draft,
                expected_snapshot_hash=request.snapshot_hash,
            )
            if revision.id != request.revision_id or approval.id != request.approval_id:
                raise StaleSnapshotError("La solicitud ya no coincide con la aprobación persistida")
            session.refresh(request)
            claim_fence = request.claim_fence
            attempt_key = _publication_attempt_idempotency_key(request.id, claim_fence)
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="publication_request.claimed",
                entity_type="publication_request",
                entity_id=request.id,
                detail={
                    "draft_id": request.draft_id,
                    "revision_id": request.revision_id,
                    "channel": request.channel,
                    "claim_fence": claim_fence,
                    "lease_expires_at": _format_time(lease_expires_at),
                    "publish_attempt_idempotency_key": attempt_key,
                },
                now=timestamp,
            )
            return PublicationClaim(
                request=request,
                claim_token=claim_token,
                claim_fence=claim_fence,
                lease_expires_at=lease_expires_at,
                publish_attempt_idempotency_key=attempt_key,
            )

    def validate_publication_claim(
        self,
        request_id: str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        now: datetime | None = None,
    ) -> PublicationRequest:
        """Revalida el fence, la lease y el snapshot justo antes del efecto externo."""

        normalized_request_id = _normalize_entity_id(request_id)
        actor = _normalize_actor(actor_id)
        normalized_token = _normalize_publication_claim_token(claim_token)
        normalized_fence = _normalize_positive_int(
            claim_fence,
            "claim_fence",
            maximum=None,
        )
        timestamp = _normalize_now(now)
        expired_status: PublicationRequestStatus | None = None
        with self._sessions.begin() as session:
            request = session.scalar(
                select(PublicationRequest)
                .where(PublicationRequest.id == normalized_request_id)
                .with_for_update()
            )
            if request is None:
                raise NotFoundError(
                    f"No existe la solicitud de publicación '{normalized_request_id}'"
                )
            self._authorize(session, actor, request.workspace_id, Permission.PUBLISH_DRAFTS)
            _require_publication_claim_credentials(
                request,
                actor_id=actor,
                claim_token=normalized_token,
                claim_fence=normalized_fence,
            )
            if PublicationRequestStatus(request.status) is not PublicationRequestStatus.CLAIMED:
                raise ConflictError("La solicitud ya no está reclamada")
            if request.lease_expires_at is None:  # pragma: no cover - protegido por el check
                raise ConflictError("La solicitud reclamada no tiene lease")
            if request.lease_expires_at <= timestamp:
                expired_status = self._expire_publication_request(
                    session,
                    request=request,
                    actor_id=actor,
                    now=timestamp,
                )
            else:
                draft = self._get_draft(session, request.draft_id)
                revision, approval = self._approved_snapshot_for_publication(
                    session,
                    draft=draft,
                    expected_snapshot_hash=request.snapshot_hash,
                )
                if revision.id != request.revision_id or approval.id != request.approval_id:
                    raise StaleSnapshotError(
                        "La solicitud ya no coincide con la aprobación persistida"
                    )
        if expired_status is not None:
            raise ConflictError(_expired_publication_claim_message(expired_status))
        return request

    def expire_publication_claims(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> list[PublicationRequest]:
        """Cierra leases vencidas como UNKNOWN; nunca las devuelve a la cola."""

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.PUBLISH_DRAFTS)
            return self._expire_publication_claims(
                session,
                workspace_id=workspace,
                actor_id=actor,
                now=timestamp,
            )

    def get_publication_request(
        self,
        request_id: str,
        *,
        actor_id: str,
    ) -> PublicationRequest:
        normalized_request_id = _normalize_entity_id(request_id)
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            request = session.get(PublicationRequest, normalized_request_id)
            if request is None:
                raise NotFoundError(
                    f"No existe la solicitud de publicación '{normalized_request_id}'"
                )
            self._authorize(session, actor, request.workspace_id, Permission.VIEW_DRAFTS)
            return request

    def list_publication_requests(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        status: PublicationRequestStatus | str | None = None,
        limit: int = 100,
    ) -> list[PublicationRequest]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        normalized_status = (
            _normalize_publication_request_status(status) if status is not None else None
        )
        normalized_limit = _normalize_bounded_int(limit, "limit", minimum=1, maximum=1000)
        with self._sessions() as session:
            self._authorize(session, actor, workspace, Permission.VIEW_DRAFTS)
            statement = select(PublicationRequest).where(
                PublicationRequest.workspace_id == workspace
            )
            if normalized_status is not None:
                statement = statement.where(PublicationRequest.status == normalized_status.value)
            return list(
                session.scalars(
                    statement.order_by(
                        PublicationRequest.created_at.desc(), PublicationRequest.id.desc()
                    ).limit(normalized_limit)
                )
            )

    def create_publish_attempt(
        self,
        draft_id: str,
        *,
        actor_id: str,
        expected_snapshot_hash: str,
        idempotency_key: str,
        channel: str = "x",
        now: datetime | None = None,
    ) -> PublishAttempt:
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        expected = _normalize_sha256(expected_snapshot_hash, required=True)
        normalized_channel = _normalize_short_text(channel, "channel", 30).lower()
        normalized_key = _normalize_short_text(idempotency_key, "idempotency_key", 120)
        with self._sessions.begin() as session:
            draft = self._get_draft(session, draft_id)
            self._authorize(session, actor, draft.workspace_id, Permission.PUBLISH_DRAFTS)
            existing = session.scalar(
                select(PublishAttempt).where(
                    PublishAttempt.workspace_id == draft.workspace_id,
                    PublishAttempt.channel == normalized_channel,
                    PublishAttempt.idempotency_key == normalized_key,
                )
            )
            if existing is not None:
                if existing.draft_id != draft.id or existing.snapshot_hash != expected:
                    raise ConflictError("La clave de idempotencia pertenece a otro snapshot")
                return existing
            if DraftStatus(draft.status) is not DraftStatus.APPROVED:
                raise ConflictError("Solo se puede publicar un draft aprobado")
            revision = self._current_revision(session, draft)
            if draft.approved_revision_id != revision.id:
                raise StaleSnapshotError("La aprobación no pertenece a la revisión actual")
            approval = session.scalar(
                select(Approval)
                .where(
                    Approval.draft_id == draft.id,
                    Approval.revision_id == revision.id,
                    Approval.decision == ApprovalDecision.APPROVED.value,
                )
                .order_by(Approval.created_at.desc(), Approval.id.desc())
                .limit(1)
            )
            if (
                approval is None
                or approval.snapshot_hash != revision.snapshot_hash
                or revision.snapshot_hash != expected
            ):
                raise StaleSnapshotError("El snapshot aprobado ya no coincide con el contenido")
            attempt = PublishAttempt(
                workspace_id=draft.workspace_id,
                draft_id=draft.id,
                revision_id=revision.id,
                requested_by=actor,
                channel=normalized_channel,
                idempotency_key=normalized_key,
                snapshot_hash=revision.snapshot_hash,
                status=PublishStatus.PENDING.value,
                started_at=timestamp,
            )
            session.add(attempt)
            session.flush()
            self._audit(
                session,
                workspace_id=draft.workspace_id,
                actor_id=actor,
                action="publish.started",
                entity_type="publish_attempt",
                entity_id=attempt.id,
                detail={
                    "draft_id": draft.id,
                    "revision_id": revision.id,
                    "channel": normalized_channel,
                    "idempotency_key": normalized_key,
                },
                now=timestamp,
            )
            return attempt

    def finish_publish_attempt(
        self,
        attempt_id: str,
        status: PublishStatus | str,
        *,
        actor_id: str,
        provider_post_id: str | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> PublishAttempt:
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        try:
            target_status = status if isinstance(status, PublishStatus) else PublishStatus(status)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Estado de publicación desconocido: {status!r}") from exc
        if target_status is PublishStatus.PENDING:
            raise ValueError("finish_publish_attempt requiere un estado terminal")
        normalized_post_id = _normalize_note(provider_post_id, required=False, max_length=100)
        normalized_error = _normalize_note(error, required=False)
        if target_status is PublishStatus.SUCCEEDED and normalized_post_id is None:
            raise ValueError("Una publicación exitosa requiere provider_post_id")
        with self._sessions.begin() as session:
            attempt = session.get(PublishAttempt, attempt_id)
            if attempt is None:
                raise NotFoundError(f"No existe el intento de publicación '{attempt_id}'")
            self._authorize(session, actor, attempt.workspace_id, Permission.PUBLISH_DRAFTS)
            if PublishStatus(attempt.status) is not PublishStatus.PENDING:
                if PublishStatus(attempt.status) is target_status:
                    return attempt
                raise ConflictError("El intento de publicación ya tiene un resultado")
            attempt.status = target_status.value
            attempt.provider_post_id = normalized_post_id
            attempt.error = normalized_error
            attempt.finished_at = timestamp
            draft = self._get_draft(session, attempt.draft_id)
            if target_status is PublishStatus.SUCCEEDED:
                if (
                    draft.current_revision_id != attempt.revision_id
                    or draft.approved_revision_id != attempt.revision_id
                ):
                    raise StaleSnapshotError(
                        "El draft cambió mientras se publicaba; requiere conciliación"
                    )
                draft.status = DraftStatus.PUBLISHED.value
                draft.updated_at = timestamp
            self._audit(
                session,
                workspace_id=attempt.workspace_id,
                actor_id=actor,
                action=f"publish.{target_status.value}",
                entity_type="publish_attempt",
                entity_id=attempt.id,
                detail={
                    "draft_id": attempt.draft_id,
                    "provider_post_id": normalized_post_id,
                    "error": normalized_error,
                },
                now=timestamp,
            )
            return attempt

    def finish_publication_request(
        self,
        request_id: str,
        status: PublicationRequestStatus | str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        publish_attempt_id: str | None = None,
        now: datetime | None = None,
    ) -> PublicationRequest:
        """Finaliza un claim vigente solo contra su PublishAttempt terminal y cercado."""

        normalized_request_id = _normalize_entity_id(request_id)
        actor = _normalize_actor(actor_id)
        target = _normalize_publication_request_status(status)
        if target in {
            PublicationRequestStatus.QUEUED,
            PublicationRequestStatus.CLAIMED,
        }:
            raise ValueError("finish_publication_request requiere un estado terminal")
        normalized_token = _normalize_publication_claim_token(claim_token)
        normalized_fence = _normalize_positive_int(
            claim_fence,
            "claim_fence",
            maximum=None,
        )
        normalized_attempt_id = (
            _normalize_entity_id(publish_attempt_id) if publish_attempt_id is not None else None
        )
        timestamp = _normalize_now(now)
        expired_status: PublicationRequestStatus | None = None
        with self._sessions.begin() as session:
            request = session.scalar(
                select(PublicationRequest)
                .where(PublicationRequest.id == normalized_request_id)
                .with_for_update()
            )
            if request is None:
                raise NotFoundError(
                    f"No existe la solicitud de publicación '{normalized_request_id}'"
                )
            self._authorize(session, actor, request.workspace_id, Permission.PUBLISH_DRAFTS)
            _require_publication_claim_credentials(
                request,
                actor_id=actor,
                claim_token=normalized_token,
                claim_fence=normalized_fence,
            )

            current = PublicationRequestStatus(request.status)
            if current in {
                PublicationRequestStatus.SUCCEEDED,
                PublicationRequestStatus.FAILED,
                PublicationRequestStatus.UNKNOWN,
            }:
                if current is not target or request.publish_attempt_id != normalized_attempt_id:
                    raise ConflictError("La solicitud ya tiene otro resultado terminal")
                return request
            if current is not PublicationRequestStatus.CLAIMED:
                raise ConflictError("La solicitud no está reclamada")
            if request.lease_expires_at is None:  # pragma: no cover - protegido por el check
                raise ConflictError("La solicitud reclamada no tiene lease")
            if request.lease_expires_at <= timestamp:
                expired_status = self._expire_publication_request(
                    session,
                    request=request,
                    actor_id=actor,
                    now=timestamp,
                )
            else:
                if normalized_attempt_id is None:
                    raise ConflictError("El cierre exige un PublishAttempt terminal")
                attempt = session.get(PublishAttempt, normalized_attempt_id)
                if attempt is None:
                    raise NotFoundError(
                        f"No existe el intento de publicación '{normalized_attempt_id}'"
                    )
                expected_attempt_key = _publication_attempt_idempotency_key(
                    request.id,
                    request.claim_fence,
                )
                _require_matching_publication_attempt(
                    request,
                    attempt,
                    actor_id=actor,
                    expected_idempotency_key=expected_attempt_key,
                    target_status=target,
                )
                request.status = target.value
                request.publish_attempt_id = attempt.id
                request.error = _sanitize_automation_error(attempt.error)
                request.updated_at = timestamp
                request.finished_at = timestamp
                self._audit(
                    session,
                    workspace_id=request.workspace_id,
                    actor_id=actor,
                    action=f"publication_request.{target.value}",
                    entity_type="publication_request",
                    entity_id=request.id,
                    detail={
                        "draft_id": request.draft_id,
                        "revision_id": request.revision_id,
                        "publish_attempt_id": attempt.id,
                        "claim_fence": request.claim_fence,
                        "provider_post_id": attempt.provider_post_id,
                        "error": request.error,
                    },
                    now=timestamp,
                )
        if expired_status is not None:
            raise ConflictError(_expired_publication_claim_message(expired_status))
        return request

    def get_automation_settings(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> AutomationSettings:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            self._authorize(session, actor, workspace, Permission.VIEW_AUTOMATION)
            settings = session.get(AutomationSettings, workspace)
            if settings is None:
                raise NotFoundError(
                    f"El espacio '{workspace}' no tiene configuración de automatización"
                )
            return settings

    def update_automation_settings(
        self,
        *,
        actor_id: str,
        expected_version: int,
        enabled: object = _UNSET,
        mode: AutomationMode | str | object = _UNSET,
        timezone: object = _UNSET,
        slots: object = _UNSET,
        generate_images: object = _UNSET,
        min_engagement_score: object = _UNSET,
        max_posts_per_day: object = _UNSET,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> AutomationSettings:
        """Actualiza settings con CAS y separa programación de autorización directa."""

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        version = _normalize_positive_int(expected_version, "expected_version", maximum=None)
        timestamp = _normalize_now(now)
        supplied = {
            "enabled": enabled,
            "mode": mode,
            "timezone": timezone,
            "slots": slots,
            "generate_images": generate_images,
            "min_engagement_score": min_engagement_score,
            "max_posts_per_day": max_posts_per_day,
        }
        if all(value is _UNSET for value in supplied.values()):
            raise ValueError("Se requiere al menos un cambio de automatización")

        normalized: dict[str, Any] = {}
        if enabled is not _UNSET:
            normalized["enabled"] = _normalize_bool(enabled, "enabled")
        if mode is not _UNSET:
            normalized["mode"] = _normalize_automation_mode(mode).value
        if timezone is not _UNSET:
            normalized["timezone"] = _normalize_timezone(timezone)
        if slots is not _UNSET:
            normalized["slots"] = _normalize_automation_slots(slots)
        if generate_images is not _UNSET:
            normalized["generate_images"] = _normalize_bool(generate_images, "generate_images")
        if min_engagement_score is not _UNSET:
            normalized["min_engagement_score"] = _normalize_bounded_int(
                min_engagement_score,
                "min_engagement_score",
                minimum=0,
                maximum=100,
            )
        if max_posts_per_day is not _UNSET:
            normalized["max_posts_per_day"] = _normalize_bounded_int(
                max_posts_per_day,
                "max_posts_per_day",
                minimum=1,
                maximum=100,
            )

        with self._sessions.begin() as session:
            current = session.scalar(
                select(AutomationSettings)
                .where(AutomationSettings.workspace_id == workspace)
                .with_for_update()
            )
            if current is None:
                raise NotFoundError(
                    f"El espacio '{workspace}' no tiene configuración de automatización"
                )
            self._authorize(session, actor, workspace, Permission.VIEW_AUTOMATION)
            if current.version != version:
                raise ConflictError(f"La configuración cambió (versión actual {current.version})")

            schedule_fields = {
                "enabled",
                "timezone",
                "slots",
                "generate_images",
                "min_engagement_score",
                "max_posts_per_day",
            }
            if schedule_fields.intersection(normalized):
                self._authorize(session, actor, workspace, Permission.MANAGE_SCHEDULE)
            requested_mode = AutomationMode(normalized.get("mode", current.mode))
            if "mode" in normalized:
                self._authorize(session, actor, workspace, Permission.MANAGE_AUTOMATION_MODE)
            if normalized.get("enabled") is True and requested_mode is AutomationMode.DIRECT:
                self._authorize(session, actor, workspace, Permission.MANAGE_AUTOMATION_MODE)
            direct_material_fields = {
                "timezone",
                "slots",
                "generate_images",
                "min_engagement_score",
                "max_posts_per_day",
            }
            direct_material_changed = bool(direct_material_fields.intersection(normalized))
            if requested_mode is AutomationMode.DIRECT and direct_material_changed:
                # La autorización directa se liga también al contenido y límites de
                # esta versión; un scheduler no puede cambiar material publicable.
                self._authorize(session, actor, workspace, Permission.MANAGE_AUTOMATION_MODE)
            if requested_mode is AutomationMode.DIRECT and (
                "mode" in normalized or normalized.get("enabled") is True or direct_material_changed
            ):
                _require_direct_publish_kill_switch()
            effective_enabled = normalized.get("enabled", current.enabled)
            if effective_enabled:
                effective_slots = normalized.get("slots")
                if effective_slots is None:
                    effective_slots = _normalize_automation_slots(current.slots)
                if not effective_slots:
                    raise ValueError(
                        "No se puede activar la automatización sin slots válidos en la agenda"
                    )

            previous = _automation_settings_audit_snapshot(current)
            values = {
                **normalized,
                "version": version + 1,
                "updated_by": actor,
                "updated_at": timestamp,
            }
            if (
                "mode" in normalized
                or normalized.get("enabled") is True
                or (requested_mode is AutomationMode.DIRECT and direct_material_changed)
            ):
                if requested_mode is AutomationMode.DIRECT:
                    values["direct_authorized_by"] = actor
                    values["direct_authorized_at"] = timestamp
                else:
                    values["direct_authorized_by"] = None
                    values["direct_authorized_at"] = None

            changed = session.execute(
                update(AutomationSettings)
                .where(
                    AutomationSettings.workspace_id == workspace,
                    AutomationSettings.version == version,
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise ConflictError("Otra operación actualizó la configuración")
            session.flush()
            updated_settings = session.get(AutomationSettings, workspace)
            if updated_settings is None:  # pragma: no cover - protegido por la PK
                raise NotFoundError("La configuración desapareció durante la actualización")
            session.refresh(updated_settings)
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="automation.settings_updated",
                entity_type="automation_settings",
                entity_id=workspace,
                detail={
                    "previous": previous,
                    "current": _automation_settings_audit_snapshot(updated_settings),
                },
                now=timestamp,
            )
            return updated_settings

    def claim_automation_run(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        slot_id: str,
        scheduled_for: datetime | date | str,
        slot_snapshot: Mapping[str, Any],
        mode: AutomationMode | str | None = None,
        draft_id: str | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> AutomationRun:
        """Reserva un slot una sola vez y consume su cupo diario de forma transaccional."""

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        key = _normalize_short_text(idempotency_key, "idempotency_key", 120)
        normalized_slot = _normalize_slot_id(slot_id)
        schedule = _normalize_publish_at(scheduled_for)
        requested_mode = _normalize_automation_mode(mode) if mode is not None else None
        requested_slot = _normalize_single_automation_slot(slot_snapshot)
        requested_slot_hash = _automation_slot_hash(requested_slot)
        normalized_draft_id = _normalize_entity_id(draft_id) if draft_id else None
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_SCHEDULE)
            settings = session.scalar(
                select(AutomationSettings)
                .where(AutomationSettings.workspace_id == workspace)
                .with_for_update()
            )
            if settings is None:
                raise NotFoundError("No existe configuración de automatización")
            existing = session.scalar(
                select(AutomationRun).where(
                    AutomationRun.workspace_id == workspace,
                    AutomationRun.idempotency_key == key,
                )
            )
            effective_mode = AutomationMode(settings.mode)
            if existing is not None:
                _require_matching_automation_claim(
                    existing,
                    slot_id=normalized_slot,
                    scheduled_for=schedule,
                    mode=requested_mode,
                    draft_id=normalized_draft_id,
                    slot_hash=requested_slot_hash,
                )
                return existing
            if not settings.enabled:
                raise ConflictError("La automatización está desactivada")
            if requested_mode is not None and requested_mode is not effective_mode:
                raise ConflictError("El modo solicitado no coincide con la configuración activa")
            authorized_slot = _require_scheduled_automation_claim(
                settings,
                idempotency_key=key,
                slot_id=normalized_slot,
                scheduled_for=schedule,
                mode=effective_mode,
            )
            if requested_slot != authorized_slot:
                raise ConflictError("El snapshot del slot no coincide con la agenda autorizada")
            if effective_mode is AutomationMode.DIRECT:
                if settings.direct_authorized_by is None or settings.direct_authorized_at is None:
                    raise ConflictError("El modo directo no tiene autorización persistida")
                try:
                    self._authorize(
                        session,
                        settings.direct_authorized_by,
                        workspace,
                        Permission.MANAGE_AUTOMATION_MODE,
                    )
                except AuthorizationError as exc:
                    raise ConflictError(
                        "La autorización directa pertenece a una cuenta inactiva o sin privilegio"
                    ) from exc
                _require_direct_publish_kill_switch()
            if normalized_draft_id is not None:
                draft = self._get_draft(session, normalized_draft_id)
                if draft.workspace_id != workspace:
                    raise ConflictError("El draft pertenece a otro espacio de trabajo")

            day_start, day_end = _local_day_bounds(schedule, settings.timezone)
            claimed_today = session.scalar(
                select(func.count(AutomationRun.id)).where(
                    AutomationRun.workspace_id == workspace,
                    AutomationRun.scheduled_for >= day_start,
                    AutomationRun.scheduled_for < day_end,
                )
            )
            if int(claimed_today or 0) >= settings.max_posts_per_day:
                raise ConflictError("Se alcanzó max_posts_per_day para la fecha local")

            run = AutomationRun(
                workspace_id=workspace,
                idempotency_key=key,
                slot_id=normalized_slot,
                scheduled_for=schedule,
                mode=effective_mode.value,
                settings_version=settings.version,
                slot_hash=requested_slot_hash,
                status=AutomationRunStatus.CLAIMED.value,
                draft_id=normalized_draft_id,
                claimed_by=actor,
                claimed_at=timestamp,
                updated_at=timestamp,
            )
            try:
                with session.begin_nested():
                    session.add(run)
                    session.flush()
            except IntegrityError as exc:
                raced = session.scalar(
                    select(AutomationRun).where(
                        AutomationRun.workspace_id == workspace,
                        AutomationRun.idempotency_key == key,
                    )
                )
                if raced is not None:
                    _require_matching_automation_claim(
                        raced,
                        slot_id=normalized_slot,
                        scheduled_for=schedule,
                        mode=requested_mode,
                        draft_id=normalized_draft_id,
                        slot_hash=requested_slot_hash,
                    )
                    return raced
                raise ConflictError("No se pudo reservar el run de automatización") from exc
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="automation.run_claimed",
                entity_type="automation_run",
                entity_id=run.id,
                detail={
                    "idempotency_key": key,
                    "slot_id": normalized_slot,
                    "slot_hash": requested_slot_hash,
                    "settings_version": settings.version,
                    "scheduled_for": _format_time(schedule),
                    "mode": effective_mode.value,
                },
                now=timestamp,
            )
            return run

    def finish_automation_run(
        self,
        run_id: str,
        status: AutomationRunStatus | str,
        *,
        actor_id: str,
        draft_id: str | None | object = _UNSET,
        error: str | None | object = _UNSET,
        now: datetime | None = None,
    ) -> AutomationRun:
        actor = _normalize_actor(actor_id)
        normalized_run_id = _normalize_entity_id(run_id)
        target = _normalize_automation_run_status(status)
        if target is AutomationRunStatus.CLAIMED:
            raise ValueError("finish_automation_run requiere avanzar el estado")
        normalized_draft: str | None | object = draft_id
        if draft_id is not _UNSET:
            normalized_draft = _normalize_entity_id(draft_id) if draft_id else None
        normalized_error: str | None | object = error
        if error is not _UNSET:
            normalized_error = _sanitize_automation_error(error)
        if target not in {AutomationRunStatus.FAILED, AutomationRunStatus.UNKNOWN} and (
            normalized_error not in {_UNSET, None}
        ):
            raise ValueError("error solo se admite para runs failed o unknown")
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            run = session.scalar(
                select(AutomationRun).where(AutomationRun.id == normalized_run_id).with_for_update()
            )
            if run is None:
                raise NotFoundError(f"No existe el run de automatización '{normalized_run_id}'")
            self._authorize(session, actor, run.workspace_id, Permission.MANAGE_SCHEDULE)
            current = AutomationRunStatus(run.status)
            if current is target:
                if normalized_draft is not _UNSET and run.draft_id != normalized_draft:
                    raise ConflictError("El run ya terminó con otro draft")
                if normalized_error is not _UNSET and run.error != normalized_error:
                    raise ConflictError("El run ya terminó con otro resultado")
                if target is AutomationRunStatus.PUBLISHING:
                    effective_draft_id = (
                        run.draft_id if normalized_draft is _UNSET else normalized_draft
                    )
                    self._validate_automation_run_transition(
                        session,
                        run=run,
                        target=target,
                        draft_id=effective_draft_id,
                    )
                return run
            allowed = _AUTOMATION_RUN_TRANSITIONS[current]
            if target not in allowed:
                raise ConflictError(
                    f"No se puede cambiar un run de '{current.value}' a '{target.value}'"
                )
            if normalized_draft is not _UNSET and normalized_draft is not None:
                draft = self._get_draft(session, normalized_draft)
                if draft.workspace_id != run.workspace_id:
                    raise ConflictError("El draft pertenece a otro espacio de trabajo")

            effective_draft_id = run.draft_id if normalized_draft is _UNSET else normalized_draft
            self._validate_automation_run_transition(
                session,
                run=run,
                target=target,
                draft_id=effective_draft_id,
            )

            run.status = target.value
            if normalized_draft is not _UNSET:
                run.draft_id = normalized_draft
            if normalized_error is not _UNSET:
                run.error = normalized_error
            elif target not in {AutomationRunStatus.FAILED, AutomationRunStatus.UNKNOWN}:
                run.error = None
            run.updated_at = timestamp
            if target in {
                AutomationRunStatus.SUCCEEDED,
                AutomationRunStatus.FAILED,
                AutomationRunStatus.UNKNOWN,
            }:
                run.finished_by = actor
                run.finished_at = timestamp
            self._audit(
                session,
                workspace_id=run.workspace_id,
                actor_id=actor,
                action=f"automation.run_{target.value}",
                entity_type="automation_run",
                entity_id=run.id,
                detail={
                    "previous_status": current.value,
                    "status": target.value,
                    "draft_id": run.draft_id,
                    "error": run.error,
                },
                now=timestamp,
            )
            return run

    def persist_automation_prepared(
        self,
        run_id: str,
        *,
        actor_id: str,
        author_actor_id: str,
        text: str,
        category: str,
        publish_at: datetime | date | str,
        evidence: Any,
        image: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[AutomationRun, Draft, Revision]:
        """Persiste el resultado preparado y lo liga al run en un solo commit.

        El archivo de media puede existir antes de esta transacción, pero el draft,
        su revisión, el registro del asset y ``run.draft_id`` nunca quedan a medias.
        Un reintento exacto devuelve el snapshot ya ligado; uno distinto se rechaza.
        """

        normalized_run_id = _normalize_entity_id(run_id)
        worker = _normalize_actor(actor_id)
        author = _normalize_actor(author_actor_id)
        if worker == author:
            raise AuthorizationError("El scheduler no puede ser el autor editorial")
        timestamp = _normalize_now(now)
        normalized_image = _normalize_generation_image(image)
        material = _normalize_revision_material(
            text=text,
            category=category,
            publish_at=publish_at,
            evidence=evidence,
            image_sha256=(normalized_image["sha256"] if normalized_image is not None else None),
        )

        with self._sessions.begin() as session:
            run = session.scalar(
                select(AutomationRun).where(AutomationRun.id == normalized_run_id).with_for_update()
            )
            if run is None:
                raise NotFoundError(f"No existe el run de automatización '{normalized_run_id}'")
            self._authorize(session, worker, run.workspace_id, Permission.MANAGE_SCHEDULE)
            self._authorize(session, author, run.workspace_id, Permission.CREATE_DRAFTS)
            self._authorize(session, author, run.workspace_id, Permission.SUBMIT_DRAFTS)
            if normalized_image is not None:
                self._authorize(session, author, run.workspace_id, Permission.MANAGE_MEDIA)
            if run.claimed_by != worker:
                raise AuthorizationError("Solo el scheduler que reclamó el run puede prepararlo")

            if run.draft_id is not None:
                draft = self._get_draft(session, run.draft_id)
                revision = self._current_revision(session, draft)
                if (
                    draft.workspace_id != run.workspace_id
                    or draft.created_by != author
                    or revision.created_by != author
                    or DraftStatus(draft.status) is not DraftStatus.IN_REVIEW
                    or any(getattr(revision, field) != value for field, value in material.items())
                ):
                    raise ConflictError("El run ya está ligado a otro snapshot preparado")
                return run, draft, revision

            if AutomationRunStatus(run.status) is not AutomationRunStatus.CLAIMED:
                raise ConflictError("El run ya no admite un borrador preparado")

            revision_id = _new_id()
            draft = Draft(
                id=_new_id(),
                workspace_id=run.workspace_id,
                status=DraftStatus.IN_REVIEW.value,
                created_by=author,
                current_revision_id=revision_id,
                created_at=timestamp,
                updated_at=timestamp,
            )
            revision = Revision(
                id=revision_id,
                draft_id=draft.id,
                revision_number=1,
                created_by=author,
                created_at=timestamp,
                **material,
            )
            session.add_all((draft, revision))
            session.flush()

            asset: MediaAsset | None = None
            if normalized_image is not None:
                asset = session.scalar(
                    select(MediaAsset).where(
                        MediaAsset.workspace_id == run.workspace_id,
                        MediaAsset.sha256 == normalized_image["sha256"],
                    )
                )
                if asset is None:
                    asset = MediaAsset(
                        workspace_id=run.workspace_id,
                        draft_id=draft.id,
                        kind="generated_image",
                        url=normalized_image["url"],
                        sha256=normalized_image["sha256"],
                        mime_type=normalized_image["mime_type"],
                        byte_size=normalized_image["byte_size"],
                        asset_metadata=normalized_image["metadata"],
                        created_by=author,
                        created_at=timestamp,
                    )
                    session.add(asset)
                    session.flush()
                elif (
                    asset.mime_type != normalized_image["mime_type"]
                    or asset.byte_size != normalized_image["byte_size"]
                ):
                    raise ConflictError("El hash de imagen ya existe con otros metadatos")

            run.draft_id = draft.id
            run.updated_at = timestamp
            self._audit(
                session,
                workspace_id=run.workspace_id,
                actor_id=author,
                action="draft.created",
                entity_type="draft",
                entity_id=draft.id,
                detail={
                    "revision_id": revision.id,
                    "snapshot_hash": revision.snapshot_hash,
                    "automation_run_id": run.id,
                },
                now=timestamp,
            )
            if asset is not None:
                self._audit(
                    session,
                    workspace_id=run.workspace_id,
                    actor_id=author,
                    action="media.registered",
                    entity_type="media_asset",
                    entity_id=asset.id,
                    detail={
                        "draft_id": draft.id,
                        "sha256": asset.sha256,
                        "kind": asset.kind,
                    },
                    now=timestamp,
                )
            self._audit(
                session,
                workspace_id=run.workspace_id,
                actor_id=author,
                action="draft.submitted",
                entity_type="draft",
                entity_id=draft.id,
                detail={
                    "revision_id": revision.id,
                    "snapshot_hash": revision.snapshot_hash,
                    "automation_run_id": run.id,
                },
                now=timestamp,
            )
            self._audit(
                session,
                workspace_id=run.workspace_id,
                actor_id=worker,
                action="automation.run_prepared",
                entity_type="automation_run",
                entity_id=run.id,
                detail={
                    "draft_id": draft.id,
                    "revision_id": revision.id,
                    "snapshot_hash": revision.snapshot_hash,
                    "media_sha256": revision.image_sha256,
                },
                now=timestamp,
            )
            return run, draft, revision

    def hold_automation_run_for_review(
        self,
        run_id: str,
        *,
        actor_id: str,
        draft_id: str,
        expected_snapshot_hash: str,
        telegram_user_id: int | str,
        chat_id: int | str,
        detail: str,
        engagement_score: int,
        now: datetime | None = None,
    ) -> AutomationReviewNotification:
        """Liga AWAITING_REVIEW y su outbox Telegram en un único commit."""

        normalized_run_id = _normalize_entity_id(run_id)
        normalized_draft_id = _normalize_entity_id(draft_id)
        actor = _normalize_actor(actor_id)
        expected = _normalize_sha256(expected_snapshot_hash, required=True)
        telegram_identity = _normalize_telegram_user_id(telegram_user_id)
        normalized_chat = _normalize_chat_id(chat_id)
        normalized_detail = _sanitize_automation_error(detail)
        if normalized_detail is None:
            raise ValueError("detail debe ser texto no vacío")
        score = _normalize_bounded_int(
            engagement_score,
            "engagement_score",
            minimum=0,
            maximum=100,
        )
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            run = session.scalar(
                select(AutomationRun).where(AutomationRun.id == normalized_run_id).with_for_update()
            )
            if run is None:
                raise NotFoundError(f"No existe el run de automatización '{normalized_run_id}'")
            self._authorize(session, actor, run.workspace_id, Permission.MANAGE_SCHEDULE)
            draft = self._get_draft_for_update(session, normalized_draft_id)
            if draft.workspace_id != run.workspace_id:
                raise ConflictError("El draft pertenece a otro espacio de trabajo")
            if DraftStatus(draft.status) is not DraftStatus.IN_REVIEW:
                raise ConflictError("La notificación exige un draft pendiente de revisión")
            revision = self._current_revision(session, draft)
            if revision.snapshot_hash != expected:
                raise StaleSnapshotError(
                    "El snapshot solicitado ya no coincide con la revisión actual"
                )
            _reviewer, membership = self._resolve_telegram_identity(
                session,
                workspace_id=run.workspace_id,
                telegram_user_id=telegram_identity,
                chat_id=normalized_chat,
            )
            require_permission(Role(membership.role), Permission.REVIEW_DRAFTS)

            existing = session.scalar(
                select(AutomationReviewNotification)
                .where(AutomationReviewNotification.automation_run_id == run.id)
                .with_for_update()
            )
            if existing is not None:
                if (
                    existing.draft_id != draft.id
                    or existing.revision_id != revision.id
                    or existing.snapshot_hash != expected
                    or existing.telegram_user_id != telegram_identity
                    or existing.chat_id != normalized_chat
                    or existing.detail != normalized_detail
                    or existing.engagement_score != score
                    or existing.text != revision.text
                    or existing.media_sha256 != revision.image_sha256
                    or AutomationRunStatus(run.status) is not AutomationRunStatus.AWAITING_REVIEW
                ):
                    raise ConflictError("El run ya tiene otra notificación de revisión persistida")
                return existing

            if AutomationRunStatus(run.status) is not AutomationRunStatus.CLAIMED:
                if AutomationRunStatus(run.status) is AutomationRunStatus.AWAITING_REVIEW:
                    raise ConflictError(
                        "El run awaiting_review carece de recibo; requiere conciliación manual"
                    )
                raise ConflictError("El run ya no admite una revisión humana nueva")
            if run.draft_id is not None and run.draft_id != draft.id:
                raise ConflictError("El run ya está ligado a otro draft")
            self._validate_automation_run_transition(
                session,
                run=run,
                target=AutomationRunStatus.AWAITING_REVIEW,
                draft_id=draft.id,
            )

            notification = AutomationReviewNotification(
                workspace_id=run.workspace_id,
                automation_run_id=run.id,
                draft_id=draft.id,
                revision_id=revision.id,
                snapshot_hash=revision.snapshot_hash,
                telegram_user_id=telegram_identity,
                chat_id=normalized_chat,
                text=revision.text,
                detail=normalized_detail,
                engagement_score=score,
                media_sha256=revision.image_sha256,
                status=AutomationReviewNotificationStatus.QUEUED.value,
                claim_fence=0,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(notification)
            session.flush()

            previous_status = run.status
            run.status = AutomationRunStatus.AWAITING_REVIEW.value
            run.draft_id = draft.id
            run.error = None
            run.updated_at = timestamp
            self._audit(
                session,
                workspace_id=run.workspace_id,
                actor_id=actor,
                action="automation.run_awaiting_review",
                entity_type="automation_run",
                entity_id=run.id,
                detail={
                    "previous_status": previous_status,
                    "status": run.status,
                    "draft_id": draft.id,
                    "notification_id": notification.id,
                },
                now=timestamp,
            )
            self._audit(
                session,
                workspace_id=run.workspace_id,
                actor_id=actor,
                action="automation_review_notification.queued",
                entity_type="automation_review_notification",
                entity_id=notification.id,
                detail={
                    "automation_run_id": run.id,
                    "draft_id": draft.id,
                    "revision_id": revision.id,
                    "snapshot_hash": revision.snapshot_hash,
                    "media_sha256": revision.image_sha256,
                    "telegram_user_id": telegram_identity,
                    "chat_id": normalized_chat,
                },
                now=timestamp,
            )
            return notification

    def get_automation_review_notification(
        self,
        notification_id: str,
        *,
        actor_id: str,
    ) -> AutomationReviewNotification:
        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            notification = session.get(AutomationReviewNotification, normalized_id)
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.VIEW_DRAFTS)
            return notification

    def get_automation_review_notification_for_run(
        self,
        run_id: str,
        *,
        actor_id: str,
    ) -> AutomationReviewNotification:
        normalized_run_id = _normalize_entity_id(run_id)
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            notification = session.scalar(
                select(AutomationReviewNotification).where(
                    AutomationReviewNotification.automation_run_id == normalized_run_id
                )
            )
            if notification is None:
                raise NotFoundError("El run no tiene notificación de revisión")
            self._authorize(session, actor, notification.workspace_id, Permission.VIEW_DRAFTS)
            return notification

    def has_queued_automation_review_notifications(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> bool:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        with self._sessions() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_SCHEDULE)
            notification_id = session.scalar(
                select(AutomationReviewNotification.id)
                .where(
                    AutomationReviewNotification.workspace_id == workspace,
                    AutomationReviewNotification.status
                    == AutomationReviewNotificationStatus.QUEUED.value,
                )
                .limit(1)
            )
            return notification_id is not None

    def claim_automation_review_notification(
        self,
        *,
        actor_id: str,
        notification_id: str | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        lease_seconds: int = DEFAULT_AUTOMATION_REVIEW_NOTIFICATION_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> AutomationReviewNotificationClaim | None:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        requested_id = _normalize_entity_id(notification_id) if notification_id else None
        lease = _normalize_bounded_int(
            lease_seconds,
            "lease_seconds",
            minimum=1,
            maximum=MAX_AUTOMATION_REVIEW_NOTIFICATION_LEASE_SECONDS,
        )
        timestamp = _normalize_now(now)
        lease_expires_at = timestamp + timedelta(seconds=lease)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_SCHEDULE)
            self._expire_automation_review_notification_claims(
                session,
                workspace_id=workspace,
                actor_id=actor,
                now=timestamp,
            )
            filters = [
                AutomationReviewNotification.workspace_id == workspace,
                AutomationReviewNotification.status
                == AutomationReviewNotificationStatus.QUEUED.value,
            ]
            if requested_id is not None:
                filters.append(AutomationReviewNotification.id == requested_id)
            candidate_id = (
                select(AutomationReviewNotification)
                .with_only_columns(AutomationReviewNotification.id)
                .where(*filters)
                .order_by(
                    AutomationReviewNotification.created_at,
                    AutomationReviewNotification.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            claim_token = secrets.token_urlsafe(32)
            token_hash = _hash_generation_claim_token(claim_token)
            claimed_id = session.scalar(
                update(AutomationReviewNotification)
                .where(
                    AutomationReviewNotification.id == candidate_id,
                    AutomationReviewNotification.status
                    == AutomationReviewNotificationStatus.QUEUED.value,
                )
                .values(
                    status=AutomationReviewNotificationStatus.CLAIMED.value,
                    claim_token_hash=token_hash,
                    claim_fence=AutomationReviewNotification.claim_fence + 1,
                    claimed_by=actor,
                    claimed_at=timestamp,
                    lease_expires_at=lease_expires_at,
                    updated_at=timestamp,
                )
                .returning(AutomationReviewNotification.id)
            )
            if claimed_id is None:
                return None
            notification = session.get(AutomationReviewNotification, claimed_id)
            if notification is None:  # pragma: no cover
                raise NotFoundError("La notificación desapareció durante el claim")
            session.refresh(notification)
            self._audit(
                session,
                workspace_id=workspace,
                actor_id=actor,
                action="automation_review_notification.claimed",
                entity_type="automation_review_notification",
                entity_id=notification.id,
                detail={
                    "automation_run_id": notification.automation_run_id,
                    "claim_fence": notification.claim_fence,
                    "lease_expires_at": _format_time(lease_expires_at),
                },
                now=timestamp,
            )
            return AutomationReviewNotificationClaim(
                notification=notification,
                claim_token=claim_token,
                claim_fence=notification.claim_fence,
                lease_expires_at=lease_expires_at,
            )

    def validate_automation_review_notification_claim(
        self,
        notification_id: str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        now: datetime | None = None,
    ) -> AutomationReviewNotification:
        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        timestamp = _normalize_now(now)
        expired = False
        with self._sessions.begin() as session:
            notification = session.scalar(
                select(AutomationReviewNotification)
                .where(AutomationReviewNotification.id == normalized_id)
                .with_for_update()
            )
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_automation_review_notification_claim_credentials(
                notification,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            if (
                AutomationReviewNotificationStatus(notification.status)
                is not AutomationReviewNotificationStatus.CLAIMED
            ):
                raise ConflictError("La notificación ya no está reclamada")
            if notification.lease_expires_at is None or notification.lease_expires_at <= timestamp:
                self._expire_automation_review_notification(
                    session,
                    notification=notification,
                    actor_id=actor,
                    now=timestamp,
                )
                expired = True
        if expired:
            raise ConflictError("La lease de notificación venció; quedó UNKNOWN y no se reenviará")
        return notification

    def prepare_automation_review_notification_callbacks(
        self,
        notification_id: str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> tuple[IssuedCallbackIntent, IssuedCallbackIntent]:
        """Crea el par de callbacks y devuelve los nonces únicamente en memoria."""

        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        timestamp = _normalize_now(now)
        expiration = _normalize_now(expires_at)
        with self._sessions.begin() as session:
            notification = session.scalar(
                select(AutomationReviewNotification)
                .where(AutomationReviewNotification.id == normalized_id)
                .with_for_update()
            )
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_automation_review_notification_claim_credentials(
                notification,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            if (
                AutomationReviewNotificationStatus(notification.status)
                is not AutomationReviewNotificationStatus.CLAIMED
                or notification.lease_expires_at is None
                or notification.lease_expires_at <= timestamp
            ):
                raise ConflictError("La notificación ya no tiene una lease vigente")
            if (
                notification.approve_intent_id is not None
                or notification.reject_intent_id is not None
            ):
                raise ConflictError(
                    "Los callbacks ya se prepararon; su entrega requiere conciliación"
                )
            run = session.get(AutomationRun, notification.automation_run_id)
            draft = self._get_draft(session, notification.draft_id)
            revision = self._current_revision(session, draft)
            if (
                run is None
                or AutomationRunStatus(run.status) is not AutomationRunStatus.AWAITING_REVIEW
                or run.draft_id != draft.id
                or DraftStatus(draft.status) is not DraftStatus.IN_REVIEW
                or revision.id != notification.revision_id
                or revision.snapshot_hash != notification.snapshot_hash
                or revision.text != notification.text
                or revision.image_sha256 != notification.media_sha256
            ):
                raise ConflictError("La revisión cambió antes de preparar sus callbacks")
            approve, reject = self.issue_review_callback_intents(
                notification.draft_id,
                expected_snapshot_hash=notification.snapshot_hash,
                telegram_user_id=notification.telegram_user_id,
                chat_id=notification.chat_id,
                expires_at=expiration,
                actor_id=actor,
                now=timestamp,
                _session=session,
            )
            notification.approve_intent_id = approve.intent.id
            notification.reject_intent_id = reject.intent.id
            notification.updated_at = timestamp
            self._audit(
                session,
                workspace_id=notification.workspace_id,
                actor_id=actor,
                action="automation_review_notification.callbacks_prepared",
                entity_type="automation_review_notification",
                entity_id=notification.id,
                detail={
                    "approve_intent_id": approve.intent.id,
                    "reject_intent_id": reject.intent.id,
                    "claim_fence": fence,
                    "expires_at": _format_time(expiration),
                },
                now=timestamp,
            )
            return approve, reject

    def record_automation_review_notification_photo(
        self,
        notification_id: str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        telegram_message_id: int,
        now: datetime | None = None,
    ) -> AutomationReviewNotification:
        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        message_id = _normalize_positive_int(
            telegram_message_id,
            "telegram_message_id",
            maximum=9_223_372_036_854_775_807,
        )
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            notification = session.scalar(
                select(AutomationReviewNotification)
                .where(AutomationReviewNotification.id == normalized_id)
                .with_for_update()
            )
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_automation_review_notification_claim_credentials(
                notification,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            if (
                AutomationReviewNotificationStatus(notification.status)
                is not AutomationReviewNotificationStatus.CLAIMED
                or notification.lease_expires_at is None
                or notification.lease_expires_at <= timestamp
            ):
                raise ConflictError("La notificación ya no tiene una lease vigente")
            if notification.photo_message_id is not None:
                if notification.photo_message_id != message_id:
                    raise ConflictError("La foto ya tiene otro identificador de Telegram")
                return notification
            notification.photo_message_id = message_id
            notification.photo_sent_at = timestamp
            notification.updated_at = timestamp
            self._audit(
                session,
                workspace_id=notification.workspace_id,
                actor_id=actor,
                action="automation_review_notification.photo_recorded",
                entity_type="automation_review_notification",
                entity_id=notification.id,
                detail={"claim_fence": fence, "telegram_message_id": message_id},
                now=timestamp,
            )
        return notification

    def finish_automation_review_notification(
        self,
        notification_id: str,
        status: AutomationReviewNotificationStatus | str,
        *,
        actor_id: str,
        claim_token: str,
        claim_fence: int,
        review_message_id: int | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> AutomationReviewNotification:
        normalized_id = _normalize_entity_id(notification_id)
        actor = _normalize_actor(actor_id)
        target = _normalize_automation_review_notification_status(status)
        if target in {
            AutomationReviewNotificationStatus.QUEUED,
            AutomationReviewNotificationStatus.CLAIMED,
        }:
            raise ValueError("finish_automation_review_notification exige estado terminal")
        token = _normalize_generation_claim_token(claim_token)
        fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
        normalized_message_id = (
            _normalize_positive_int(
                review_message_id,
                "review_message_id",
                maximum=9_223_372_036_854_775_807,
            )
            if review_message_id is not None
            else None
        )
        if target is AutomationReviewNotificationStatus.SENT and normalized_message_id is None:
            raise ValueError("Una notificación enviada requiere review_message_id")
        normalized_error = _sanitize_automation_error(error)
        if target is not AutomationReviewNotificationStatus.SENT and normalized_error is None:
            raise ValueError("Una notificación fallida exige error seguro")
        timestamp = _normalize_now(now)
        expired = False
        with self._sessions.begin() as session:
            notification = session.scalar(
                select(AutomationReviewNotification)
                .where(AutomationReviewNotification.id == normalized_id)
                .with_for_update()
            )
            if notification is None:
                raise NotFoundError(f"No existe la notificación '{normalized_id}'")
            self._authorize(session, actor, notification.workspace_id, Permission.MANAGE_SCHEDULE)
            _require_automation_review_notification_claim_credentials(
                notification,
                actor_id=actor,
                claim_token=token,
                claim_fence=fence,
            )
            current = AutomationReviewNotificationStatus(notification.status)
            if current in {
                AutomationReviewNotificationStatus.SENT,
                AutomationReviewNotificationStatus.FAILED,
                AutomationReviewNotificationStatus.UNKNOWN,
            }:
                if current is not target:
                    raise ConflictError("La notificación ya tiene otro estado terminal")
                return notification
            if current is not AutomationReviewNotificationStatus.CLAIMED:
                raise ConflictError("La notificación no está reclamada")
            if notification.lease_expires_at is None or notification.lease_expires_at <= timestamp:
                self._expire_automation_review_notification(
                    session,
                    notification=notification,
                    actor_id=actor,
                    now=timestamp,
                )
                expired = True
            else:
                if target is AutomationReviewNotificationStatus.SENT and (
                    notification.approve_intent_id is None or notification.reject_intent_id is None
                ):
                    raise ConflictError("La entrega no tiene callbacks persistidos")
                notification.status = target.value
                notification.review_message_id = normalized_message_id
                notification.sent_at = (
                    timestamp if target is AutomationReviewNotificationStatus.SENT else None
                )
                notification.error = normalized_error
                notification.updated_at = timestamp
                notification.finished_at = timestamp
                self._audit(
                    session,
                    workspace_id=notification.workspace_id,
                    actor_id=actor,
                    action=f"automation_review_notification.{target.value}",
                    entity_type="automation_review_notification",
                    entity_id=notification.id,
                    detail={
                        "automation_run_id": notification.automation_run_id,
                        "claim_fence": fence,
                        "draft_id": notification.draft_id,
                        "review_message_id": normalized_message_id,
                        "error": normalized_error,
                    },
                    now=timestamp,
                )
        if expired:
            raise ConflictError("La lease de notificación venció; quedó UNKNOWN y no se reenviará")
        return notification

    def expire_automation_review_notification_claims(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
    ) -> list[AutomationReviewNotification]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_SCHEDULE)
            return self._expire_automation_review_notification_claims(
                session,
                workspace_id=workspace,
                actor_id=actor,
                now=timestamp,
            )

    def reconcile_stale_automation_runs(
        self,
        *,
        actor_id: str,
        stale_before: datetime | date | str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[AutomationRun]:
        """Concilia runs atascados usando solo evidencia local; nunca contacta a X.

        Un ``CLAIMED`` sin snapshot se considera un fallo previo. Si el snapshot
        completo ya quedó ligado atómicamente, se recupera hacia revisión humana
        con outbox durable. Un ``PUBLISHING`` vencido solo puede terminar con éxito
        ante un ``PublishAttempt`` exitoso para la revisión aprobada; cualquier
        resultado inconcluso queda desconocido. Ningún estado terminal se reencola.
        """

        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        cutoff = _normalize_publish_at(stale_before)
        timestamp = _normalize_now(now)
        normalized_limit = _normalize_bounded_int(limit, "limit", minimum=1, maximum=1000)
        if cutoff > timestamp:
            raise ValueError("stale_before no puede ser posterior a now")

        with self._sessions.begin() as session:
            self._authorize(session, actor, workspace, Permission.MANAGE_SCHEDULE)
            stale_runs = list(
                session.scalars(
                    select(AutomationRun)
                    .where(
                        AutomationRun.workspace_id == workspace,
                        AutomationRun.status.in_(
                            (
                                AutomationRunStatus.CLAIMED.value,
                                AutomationRunStatus.PUBLISHING.value,
                            )
                        ),
                        AutomationRun.updated_at <= cutoff,
                    )
                    .order_by(AutomationRun.updated_at, AutomationRun.id)
                    .limit(normalized_limit)
                    .with_for_update(skip_locked=True)
                )
            )
            reconciled: list[AutomationRun] = []
            for run in stale_runs:
                previous = AutomationRunStatus(run.status)
                attempt_ids: list[str] = []
                attempt_statuses: list[str] = []
                reason = "stale_claim_before_publish"
                target = AutomationRunStatus.FAILED

                if previous is AutomationRunStatus.CLAIMED and run.draft_id is not None:
                    notification, recovery_reason = self._recover_prepared_automation_run(
                        session,
                        run=run,
                        actor_id=actor,
                        now=timestamp,
                    )
                    if notification is not None:
                        self._audit(
                            session,
                            workspace_id=workspace,
                            actor_id=actor,
                            action="automation.run_reconciled",
                            entity_type="automation_run",
                            entity_id=run.id,
                            detail={
                                "previous_status": previous.value,
                                "status": run.status,
                                "reason": recovery_reason,
                                "notification_id": notification.id,
                                "attempt_ids": [],
                                "attempt_statuses": [],
                                "stale_before": _format_time(cutoff),
                            },
                            now=timestamp,
                        )
                        reconciled.append(run)
                        continue
                    reason = recovery_reason
                elif previous is AutomationRunStatus.PUBLISHING:
                    attempts = self._automation_run_publish_attempts(session, run=run)
                    attempt_ids = [attempt.id for attempt in attempts]
                    attempt_statuses = [attempt.status for attempt in attempts]
                    target, reason = self._reconciled_publishing_status(
                        session,
                        run=run,
                        attempts=attempts,
                    )

                run.status = target.value
                run.error = _automation_reconciliation_error(target, reason=reason)
                run.updated_at = timestamp
                run.finished_by = actor
                run.finished_at = timestamp
                self._audit(
                    session,
                    workspace_id=workspace,
                    actor_id=actor,
                    action="automation.run_reconciled",
                    entity_type="automation_run",
                    entity_id=run.id,
                    detail={
                        "previous_status": previous.value,
                        "status": target.value,
                        "reason": reason,
                        "attempt_ids": attempt_ids,
                        "attempt_statuses": attempt_statuses,
                        "stale_before": _format_time(cutoff),
                    },
                    now=timestamp,
                )
                reconciled.append(run)
            return reconciled

    def _recover_prepared_automation_run(
        self,
        session: Session,
        *,
        run: AutomationRun,
        actor_id: str,
        now: datetime,
    ) -> tuple[AutomationReviewNotification | None, str]:
        """Enruta un snapshot preparado a revisión; no repite ninguna llamada externa."""

        if run.draft_id is None:  # pragma: no cover - protegido por el llamador
            return None, "stale_claim_before_publish"
        draft = session.get(Draft, run.draft_id)
        if (
            draft is None
            or draft.workspace_id != run.workspace_id
            or DraftStatus(draft.status) is not DraftStatus.IN_REVIEW
            or draft.created_by == run.claimed_by
        ):
            return None, "stale_prepared_snapshot_inconsistent"
        revision = session.get(Revision, draft.current_revision_id)
        if revision is None or revision.draft_id != draft.id:
            return None, "stale_prepared_snapshot_inconsistent"
        if revision.image_sha256 is not None:
            asset_id = session.scalar(
                select(MediaAsset.id).where(
                    MediaAsset.workspace_id == run.workspace_id,
                    MediaAsset.sha256 == revision.image_sha256,
                )
            )
            if asset_id is None:
                return None, "stale_prepared_snapshot_inconsistent"

        existing = session.scalar(
            select(AutomationReviewNotification)
            .where(AutomationReviewNotification.automation_run_id == run.id)
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.draft_id != draft.id
                or existing.revision_id != revision.id
                or existing.snapshot_hash != revision.snapshot_hash
                or existing.text != revision.text
                or existing.media_sha256 != revision.image_sha256
            ):
                return None, "stale_prepared_snapshot_inconsistent"
            notification = existing
        else:
            try:
                binding = self._select_reviewer_binding(
                    session,
                    workspace_id=run.workspace_id,
                    author_actor_id=draft.created_by,
                )
            except AuthorizationError:
                return None, "stale_prepared_reviewer_missing"
            notification = AutomationReviewNotification(
                workspace_id=run.workspace_id,
                automation_run_id=run.id,
                draft_id=draft.id,
                revision_id=revision.id,
                snapshot_hash=revision.snapshot_hash,
                telegram_user_id=binding.telegram_user_id,
                chat_id=binding.chat_id,
                text=revision.text,
                detail=(
                    "El worker se interrumpió después de persistir el snapshot; "
                    "se exige revisión humana antes de publicar."
                ),
                engagement_score=_automation_revision_engagement_score(revision),
                media_sha256=revision.image_sha256,
                status=AutomationReviewNotificationStatus.QUEUED.value,
                claim_fence=0,
                created_at=now,
                updated_at=now,
            )
            session.add(notification)
            session.flush()
            self._audit(
                session,
                workspace_id=run.workspace_id,
                actor_id=actor_id,
                action="automation_review_notification.queued",
                entity_type="automation_review_notification",
                entity_id=notification.id,
                detail={
                    "automation_run_id": run.id,
                    "draft_id": draft.id,
                    "revision_id": revision.id,
                    "snapshot_hash": revision.snapshot_hash,
                    "media_sha256": revision.image_sha256,
                    "reviewer_user_id": binding.user_id,
                    "telegram_user_id": binding.telegram_user_id,
                    "chat_id": binding.chat_id,
                    "recovered": True,
                },
                now=now,
            )

        run.status = AutomationRunStatus.AWAITING_REVIEW.value
        run.error = None
        run.updated_at = now
        run.finished_by = None
        run.finished_at = None
        return notification, "stale_prepared_routed_to_review"

    def _automation_run_publish_attempts(
        self,
        session: Session,
        *,
        run: AutomationRun,
    ) -> list[PublishAttempt]:
        if run.draft_id is None:
            return []
        draft = session.get(Draft, run.draft_id)
        if draft is None or draft.workspace_id != run.workspace_id:
            return []
        revision_id = draft.approved_revision_id
        if revision_id is None:
            return []
        attempts = list(
            session.scalars(
                select(PublishAttempt)
                .where(
                    PublishAttempt.workspace_id == run.workspace_id,
                    PublishAttempt.draft_id == draft.id,
                    PublishAttempt.revision_id == revision_id,
                    PublishAttempt.channel == "x",
                )
                .order_by(PublishAttempt.started_at.desc(), PublishAttempt.id.desc())
                .with_for_update()
            )
        )
        # El cierre del intento actualiza el draft en la misma transacción. Si el
        # SELECT FOR UPDATE esperó a ese commit, refrescar evita validar contra la
        # copia anterior del draft que ya estaba en el identity map de la sesión.
        session.refresh(draft)
        return attempts

    def _reconciled_publishing_status(
        self,
        session: Session,
        *,
        run: AutomationRun,
        attempts: Sequence[PublishAttempt],
    ) -> tuple[AutomationRunStatus, str]:
        succeeded = next(
            (
                attempt
                for attempt in attempts
                if PublishStatus(attempt.status) is PublishStatus.SUCCEEDED
                and attempt.provider_post_id
            ),
            None,
        )
        if succeeded is not None:
            try:
                self._validate_automation_run_transition(
                    session,
                    run=run,
                    target=AutomationRunStatus.SUCCEEDED,
                    draft_id=run.draft_id,
                )
            except ConflictError:
                return AutomationRunStatus.UNKNOWN, "inconsistent_success_evidence"
            return AutomationRunStatus.SUCCEEDED, "publish_attempt_succeeded"
        if any(
            PublishStatus(attempt.status) in {PublishStatus.PENDING, PublishStatus.UNKNOWN}
            for attempt in attempts
        ):
            return AutomationRunStatus.UNKNOWN, "publish_attempt_inconclusive"
        if attempts and all(
            PublishStatus(attempt.status) is PublishStatus.FAILED for attempt in attempts
        ):
            return AutomationRunStatus.FAILED, "publish_attempt_failed"
        return AutomationRunStatus.UNKNOWN, "publish_attempt_missing"

    def _validate_automation_run_transition(
        self,
        session: Session,
        *,
        run: AutomationRun,
        target: AutomationRunStatus,
        draft_id: object,
    ) -> None:
        if target in {AutomationRunStatus.FAILED, AutomationRunStatus.UNKNOWN}:
            return
        if not isinstance(draft_id, str) or not draft_id:
            raise ConflictError(f"El estado '{target.value}' exige un draft persistido")
        draft = self._get_draft(session, draft_id)
        if draft.workspace_id != run.workspace_id:
            raise ConflictError("El draft pertenece a otro espacio de trabajo")
        draft_status = DraftStatus(draft.status)
        if target is AutomationRunStatus.AWAITING_REVIEW:
            if draft_status is not DraftStatus.IN_REVIEW:
                raise ConflictError("awaiting_review exige un draft enviado a revisión")
            return
        if target is AutomationRunStatus.READY:
            if draft_status is not DraftStatus.APPROVED:
                raise ConflictError("ready exige un draft aprobado")
            return
        if target is AutomationRunStatus.PUBLISHING:
            if run.mode == AutomationMode.DIRECT.value:
                settings = session.scalar(
                    select(AutomationSettings)
                    .where(AutomationSettings.workspace_id == run.workspace_id)
                    .with_for_update()
                )
                if (
                    settings is None
                    or not settings.enabled
                    or settings.mode != run.mode
                    or settings.version != run.settings_version
                ):
                    raise ConflictError(
                        "La configuración cambió después del claim; direct requiere reprogramación"
                    )
                try:
                    current_slots = _normalize_automation_slots(settings.slots)
                except ValueError as exc:
                    raise ConflictError("La agenda persistida ya no es válida") from exc
                current_slot = next(
                    (item for item in current_slots if item["id"] == run.slot_id),
                    None,
                )
                if current_slot is None or _automation_slot_hash(current_slot) != run.slot_hash:
                    raise ConflictError("El slot autorizado cambió después del claim")
                if settings.direct_authorized_by is None or settings.direct_authorized_at is None:
                    raise ConflictError("El modo directo no tiene autorización persistida")
                try:
                    self._authorize(
                        session,
                        settings.direct_authorized_by,
                        run.workspace_id,
                        Permission.MANAGE_AUTOMATION_MODE,
                    )
                except AuthorizationError as exc:
                    raise ConflictError(
                        "La autorización directa pertenece a una cuenta inactiva o sin privilegio"
                    ) from exc
                _require_direct_publish_kill_switch()
            if draft_status is not DraftStatus.APPROVED:
                raise ConflictError("publishing exige un draft aprobado")
            pending = session.scalar(
                select(PublishAttempt.id).where(
                    PublishAttempt.draft_id == draft.id,
                    PublishAttempt.revision_id == draft.approved_revision_id,
                    PublishAttempt.status == PublishStatus.PENDING.value,
                )
            )
            if pending is None:
                raise ConflictError("publishing exige un intento de publicación pendiente")
            return
        if target is AutomationRunStatus.SUCCEEDED:
            if draft_status is not DraftStatus.PUBLISHED:
                raise ConflictError("succeeded exige un draft publicado")
            succeeded = session.scalar(
                select(PublishAttempt.id).where(
                    PublishAttempt.draft_id == draft.id,
                    PublishAttempt.revision_id == draft.approved_revision_id,
                    PublishAttempt.status == PublishStatus.SUCCEEDED.value,
                )
            )
            if succeeded is None:
                raise ConflictError("succeeded exige una publicación confirmada")

    def list_automation_runs(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        status: AutomationRunStatus | str | None = None,
        scheduled_from: datetime | date | str | None = None,
        scheduled_to: datetime | date | str | None = None,
        limit: int = 100,
    ) -> list[AutomationRun]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        normalized_limit = _normalize_bounded_int(limit, "limit", minimum=1, maximum=1000)
        normalized_status = _normalize_automation_run_status(status) if status is not None else None
        start = _normalize_publish_at(scheduled_from) if scheduled_from is not None else None
        end = _normalize_publish_at(scheduled_to) if scheduled_to is not None else None
        if start is not None and end is not None and start >= end:
            raise ValueError("scheduled_from debe ser anterior a scheduled_to")
        with self._sessions() as session:
            self._authorize(session, actor, workspace, Permission.VIEW_AUTOMATION)
            statement = select(AutomationRun).where(AutomationRun.workspace_id == workspace)
            if normalized_status is not None:
                statement = statement.where(AutomationRun.status == normalized_status.value)
            if start is not None:
                statement = statement.where(AutomationRun.scheduled_for >= start)
            if end is not None:
                statement = statement.where(AutomationRun.scheduled_for < end)
            return list(
                session.scalars(
                    statement.order_by(
                        AutomationRun.scheduled_for.desc(), AutomationRun.id.desc()
                    ).limit(normalized_limit)
                )
            )

    def list_audit_events(
        self,
        *,
        actor_id: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        limit: int = 100,
        after_sequence: int | None = None,
    ) -> list[AuditEvent]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit debe estar entre 1 y 1000")
        with self._sessions() as session:
            self._authorize(session, actor, workspace, Permission.VIEW_AUDIT)
            statement = select(AuditEvent).where(AuditEvent.workspace_id == workspace)
            if after_sequence is not None:
                statement = statement.where(AuditEvent.sequence > after_sequence)
            return list(session.scalars(statement.order_by(AuditEvent.sequence).limit(limit)))

    def _review_draft(
        self,
        draft_id: str,
        *,
        actor_id: str,
        expected_snapshot_hash: str,
        decision: ApprovalDecision,
        reason: str | None,
        now: datetime | None,
        _session: Session | None = None,
    ) -> Approval:
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        expected = _normalize_sha256(expected_snapshot_hash, required=True)
        normalized_reason = _normalize_note(reason, required=False)
        transaction = self._sessions.begin() if _session is None else nullcontext(_session)
        with transaction as session:
            draft = self._get_draft(session, draft_id)
            self._authorize(session, actor, draft.workspace_id, Permission.REVIEW_DRAFTS)
            if DraftStatus(draft.status) is not DraftStatus.IN_REVIEW:
                raise ConflictError("Solo un draft enviado a revisión puede recibir decisión")
            revision = self._current_revision(session, draft)
            require_distinct_approver(author_id=revision.created_by, approver_id=actor)
            if revision.snapshot_hash != expected:
                raise StaleSnapshotError(
                    "El contenido cambió desde la vista previa; no se registró la decisión"
                )
            target_status = (
                DraftStatus.APPROVED
                if decision is ApprovalDecision.APPROVED
                else DraftStatus.REJECTED
            )
            approved_revision_id = revision.id if decision is ApprovalDecision.APPROVED else None
            claimed = session.execute(
                update(Draft)
                .where(
                    Draft.id == draft.id,
                    Draft.status == DraftStatus.IN_REVIEW.value,
                    Draft.current_revision_id == revision.id,
                )
                .values(
                    status=target_status.value,
                    approved_revision_id=approved_revision_id,
                    updated_at=timestamp,
                )
            )
            if claimed.rowcount != 1:
                raise ConflictError("Otro reviewer ya decidió sobre este draft")
            approval = Approval(
                workspace_id=draft.workspace_id,
                draft_id=draft.id,
                revision_id=revision.id,
                decision=decision.value,
                snapshot_hash=revision.snapshot_hash,
                actor_id=actor,
                reason=normalized_reason,
                created_at=timestamp,
            )
            session.add(approval)
            session.flush()
            self._audit(
                session,
                workspace_id=draft.workspace_id,
                actor_id=actor,
                action=f"draft.{decision.value}",
                entity_type="draft",
                entity_id=draft.id,
                detail={
                    "approval_id": approval.id,
                    "revision_id": revision.id,
                    "snapshot_hash": revision.snapshot_hash,
                    "reason": normalized_reason,
                },
                now=timestamp,
            )
            linked_runs = list(
                session.scalars(
                    select(AutomationRun)
                    .where(
                        AutomationRun.workspace_id == draft.workspace_id,
                        AutomationRun.draft_id == draft.id,
                        AutomationRun.status == AutomationRunStatus.AWAITING_REVIEW.value,
                    )
                    .with_for_update()
                )
            )
            for run in linked_runs:
                previous_status = run.status
                if decision is ApprovalDecision.APPROVED:
                    run.status = AutomationRunStatus.READY.value
                    run.error = None
                else:
                    run.status = AutomationRunStatus.FAILED.value
                    run.error = _sanitize_automation_error(
                        normalized_reason or "El borrador fue rechazado por revisión humana."
                    )
                    run.finished_by = actor
                    run.finished_at = timestamp
                run.updated_at = timestamp
                self._audit(
                    session,
                    workspace_id=run.workspace_id,
                    actor_id=actor,
                    action=f"automation.run_{run.status}",
                    entity_type="automation_run",
                    entity_id=run.id,
                    detail={
                        "previous_status": previous_status,
                        "status": run.status,
                        "draft_id": draft.id,
                        "trigger": f"draft.{decision.value}",
                    },
                    now=timestamp,
                )
            return approval

    def _new_user(
        self,
        session: Session,
        *,
        email: str,
        display_name: str,
        username: str | None,
        password_hash: str | None,
        user_id: str | None,
        now: datetime,
    ) -> User:
        normalized_email = _normalize_email(email)
        normalized_name = _normalize_short_text(display_name, "display_name", 120)
        normalized_username = _normalize_username(username)
        normalized_password = _normalize_note(password_hash, required=False, max_length=255)
        if session.scalar(select(User.id).where(User.email == normalized_email)) is not None:
            raise ConflictError(f"Ya existe un usuario con email '{normalized_email}'")
        if (
            normalized_username is not None
            and session.scalar(select(User.id).where(User.username == normalized_username))
            is not None
        ):
            raise ConflictError(f"Ya existe el username '{normalized_username}'")
        user = User(
            id=_normalize_entity_id(user_id) if user_id else _new_id(),
            email=normalized_email,
            username=normalized_username,
            display_name=normalized_name,
            password_hash=normalized_password,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        try:
            with session.begin_nested():
                session.add(user)
                session.flush()
        except IntegrityError as exc:
            if (
                normalized_username is not None
                and session.scalar(select(User.id).where(User.username == normalized_username))
                is not None
            ):
                raise ConflictError(f"Ya existe el username '{normalized_username}'") from exc
            if session.scalar(select(User.id).where(User.email == normalized_email)) is not None:
                raise ConflictError(f"Ya existe un usuario con email '{normalized_email}'") from exc
            raise ConflictError("No se pudo crear el usuario por un conflicto de unicidad") from exc
        return user

    @staticmethod
    def _new_automation_settings(
        *, workspace_id: str, actor_id: str, now: datetime
    ) -> AutomationSettings:
        return AutomationSettings(
            workspace_id=workspace_id,
            enabled=False,
            mode=AutomationMode.HUMAN_REVIEW.value,
            timezone=DEFAULT_AUTOMATION_TIMEZONE,
            slots=[],
            generate_images=False,
            min_engagement_score=0,
            max_posts_per_day=DEFAULT_MAX_POSTS_PER_DAY,
            version=1,
            direct_authorized_by=None,
            direct_authorized_at=None,
            updated_by=actor_id,
            updated_at=now,
        )

    def _approved_snapshot_for_publication(
        self,
        session: Session,
        *,
        draft: Draft,
        expected_snapshot_hash: str,
    ) -> tuple[Revision, Approval]:
        if DraftStatus(draft.status) is not DraftStatus.APPROVED:
            raise ConflictError("Solo se puede publicar un draft aprobado")
        revision = self._current_revision(session, draft)
        if draft.approved_revision_id != revision.id:
            raise StaleSnapshotError("La aprobación no pertenece a la revisión actual")
        approval = session.scalar(
            select(Approval)
            .where(
                Approval.draft_id == draft.id,
                Approval.revision_id == revision.id,
                Approval.decision == ApprovalDecision.APPROVED.value,
            )
            .order_by(Approval.created_at.desc(), Approval.id.desc())
            .limit(1)
        )
        if (
            approval is None
            or approval.snapshot_hash != revision.snapshot_hash
            or revision.snapshot_hash != expected_snapshot_hash
        ):
            raise StaleSnapshotError("El snapshot aprobado ya no coincide con el contenido")
        return revision, approval

    def _expire_generation_claims(
        self,
        session: Session,
        *,
        workspace_id: str,
        actor_id: str,
        now: datetime,
    ) -> list[GenerationRequest]:
        expired = list(
            session.scalars(
                select(GenerationRequest)
                .where(
                    GenerationRequest.workspace_id == workspace_id,
                    GenerationRequest.status == GenerationRequestStatus.CLAIMED.value,
                    GenerationRequest.lease_expires_at <= now,
                )
                .order_by(GenerationRequest.lease_expires_at, GenerationRequest.id)
                .with_for_update(skip_locked=True)
            )
        )
        for request in expired:
            self._expire_generation_request(
                session,
                request=request,
                actor_id=actor_id,
                now=now,
            )
        return expired

    def _expire_generation_request(
        self,
        session: Session,
        *,
        request: GenerationRequest,
        actor_id: str,
        now: datetime,
    ) -> None:
        if GenerationRequestStatus(request.status) is not GenerationRequestStatus.CLAIMED:
            return
        if request.lease_expires_at is None or request.lease_expires_at > now:
            return
        request.status = GenerationRequestStatus.UNKNOWN.value
        request.error = "La lease venció; el resultado de generación requiere conciliación."
        request.updated_at = now
        request.finished_at = now
        self._audit(
            session,
            workspace_id=request.workspace_id,
            actor_id=actor_id,
            action="generation_request.unknown",
            entity_type="generation_request",
            entity_id=request.id,
            detail={"claim_fence": request.claim_fence, "reason": "lease_expired"},
            now=now,
        )

    def _expire_generation_notification_claims(
        self,
        session: Session,
        *,
        workspace_id: str,
        actor_id: str,
        now: datetime,
    ) -> list[GenerationNotification]:
        expired = list(
            session.scalars(
                select(GenerationNotification)
                .where(
                    GenerationNotification.workspace_id == workspace_id,
                    GenerationNotification.status == GenerationNotificationStatus.CLAIMED.value,
                    GenerationNotification.lease_expires_at <= now,
                )
                .order_by(
                    GenerationNotification.lease_expires_at,
                    GenerationNotification.id,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for notification in expired:
            self._expire_generation_notification(
                session,
                notification=notification,
                actor_id=actor_id,
                now=now,
            )
        return expired

    def _expire_generation_notification(
        self,
        session: Session,
        *,
        notification: GenerationNotification,
        actor_id: str,
        now: datetime,
    ) -> None:
        if (
            GenerationNotificationStatus(notification.status)
            is not GenerationNotificationStatus.CLAIMED
        ):
            return
        if notification.lease_expires_at is None or notification.lease_expires_at > now:
            return
        notification.status = GenerationNotificationStatus.UNKNOWN.value
        notification.error = "La lease venció; la entrega Telegram se considera ambigua."
        notification.updated_at = now
        notification.finished_at = now
        self._audit(
            session,
            workspace_id=notification.workspace_id,
            actor_id=actor_id,
            action="generation_notification.unknown",
            entity_type="generation_notification",
            entity_id=notification.id,
            detail={
                "claim_fence": notification.claim_fence,
                "generation_request_id": notification.generation_request_id,
                "reason": "lease_expired",
            },
            now=now,
        )

    def _expire_automation_review_notification_claims(
        self,
        session: Session,
        *,
        workspace_id: str,
        actor_id: str,
        now: datetime,
    ) -> list[AutomationReviewNotification]:
        expired = list(
            session.scalars(
                select(AutomationReviewNotification)
                .where(
                    AutomationReviewNotification.workspace_id == workspace_id,
                    AutomationReviewNotification.status
                    == AutomationReviewNotificationStatus.CLAIMED.value,
                    AutomationReviewNotification.lease_expires_at <= now,
                )
                .order_by(
                    AutomationReviewNotification.lease_expires_at,
                    AutomationReviewNotification.id,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for notification in expired:
            self._expire_automation_review_notification(
                session,
                notification=notification,
                actor_id=actor_id,
                now=now,
            )
        return expired

    def _expire_automation_review_notification(
        self,
        session: Session,
        *,
        notification: AutomationReviewNotification,
        actor_id: str,
        now: datetime,
    ) -> None:
        if (
            AutomationReviewNotificationStatus(notification.status)
            is not AutomationReviewNotificationStatus.CLAIMED
        ):
            return
        if notification.lease_expires_at is None or notification.lease_expires_at > now:
            return
        notification.status = AutomationReviewNotificationStatus.UNKNOWN.value
        notification.error = "La lease venció; la entrega Telegram se considera ambigua."
        notification.updated_at = now
        notification.finished_at = now
        self._audit(
            session,
            workspace_id=notification.workspace_id,
            actor_id=actor_id,
            action="automation_review_notification.unknown",
            entity_type="automation_review_notification",
            entity_id=notification.id,
            detail={
                "automation_run_id": notification.automation_run_id,
                "claim_fence": notification.claim_fence,
                "reason": "lease_expired",
            },
            now=now,
        )

    def _expire_publication_claims(
        self,
        session: Session,
        *,
        workspace_id: str,
        actor_id: str,
        now: datetime,
    ) -> list[PublicationRequest]:
        expired = list(
            session.scalars(
                select(PublicationRequest)
                .where(
                    PublicationRequest.workspace_id == workspace_id,
                    PublicationRequest.status == PublicationRequestStatus.CLAIMED.value,
                    PublicationRequest.lease_expires_at <= now,
                )
                .order_by(PublicationRequest.lease_expires_at, PublicationRequest.id)
                .with_for_update(skip_locked=True)
            )
        )
        for request in expired:
            self._expire_publication_request(
                session,
                request=request,
                actor_id=actor_id,
                now=now,
            )
        return expired

    def _expire_publication_request(
        self,
        session: Session,
        *,
        request: PublicationRequest,
        actor_id: str,
        now: datetime,
    ) -> PublicationRequestStatus | None:
        if PublicationRequestStatus(request.status) is not PublicationRequestStatus.CLAIMED:
            return None
        if request.lease_expires_at is None or request.lease_expires_at > now:
            return None

        expected_attempt_key = _publication_attempt_idempotency_key(
            request.id,
            request.claim_fence,
        )
        attempt = session.scalar(
            select(PublishAttempt)
            .where(
                PublishAttempt.workspace_id == request.workspace_id,
                PublishAttempt.channel == request.channel,
                PublishAttempt.idempotency_key == expected_attempt_key,
            )
            .with_for_update()
        )
        target = PublicationRequestStatus.UNKNOWN
        reason = "lease_expired_attempt_missing"
        linked_attempt: PublishAttempt | None = None
        if attempt is not None:
            attempt_status = PublishStatus(attempt.status)
            reason = "lease_expired_attempt_pending"
            if attempt_status is not PublishStatus.PENDING:
                attempted_target = PublicationRequestStatus(attempt_status.value)
                try:
                    _require_matching_publication_attempt(
                        request,
                        attempt,
                        actor_id=request.claimed_by or "",
                        expected_idempotency_key=expected_attempt_key,
                        target_status=attempted_target,
                    )
                except ConflictError:
                    reason = "lease_expired_attempt_mismatch"
                else:
                    target = attempted_target
                    linked_attempt = attempt
                    reason = f"lease_expired_attempt_{attempt_status.value}"

        request.status = target.value
        request.publish_attempt_id = linked_attempt.id if linked_attempt is not None else None
        if linked_attempt is not None:
            request.error = _sanitize_automation_error(linked_attempt.error)
        else:
            request.error = "La lease venció; el resultado externo se considera desconocido."
        request.updated_at = now
        request.finished_at = now
        self._audit(
            session,
            workspace_id=request.workspace_id,
            actor_id=actor_id,
            action=f"publication_request.{target.value}",
            entity_type="publication_request",
            entity_id=request.id,
            detail={
                "draft_id": request.draft_id,
                "revision_id": request.revision_id,
                "claim_fence": request.claim_fence,
                "publish_attempt_id": (linked_attempt.id if linked_attempt is not None else None),
                "provider_post_id": (
                    linked_attempt.provider_post_id if linked_attempt is not None else None
                ),
                "error": request.error,
                "reason": reason,
            },
            now=now,
        )
        return target

    def _authorize_profile_change(
        self,
        session: Session,
        actor_id: str,
        target_id: str,
        workspace_id: str,
    ) -> None:
        actor_membership = self._required_membership(session, actor_id, workspace_id)
        actor_user = self._get_user(session, actor_id)
        if not actor_user.is_active:
            raise AuthorizationError("La cuenta del actor está desactivada")
        self._required_membership(session, target_id, workspace_id)
        if actor_id == target_id:
            return
        actor_role = Role(actor_membership.role)
        require_permission(actor_role, Permission.MANAGE_USERS)
        target_membership = self._required_membership(session, target_id, workspace_id)
        self._require_can_manage_existing_role(actor_role, Role(target_membership.role))
        other_workspace = session.scalar(
            select(Membership.workspace_id).where(
                Membership.user_id == target_id,
                Membership.workspace_id != workspace_id,
            )
        )
        if other_workspace is not None:
            raise ConflictError(
                "La cuenta pertenece a otros espacios; requiere administración global"
            )

    def _authorize(
        self,
        session: Session,
        actor_id: str,
        workspace_id: str,
        permission: Permission,
    ) -> Role:
        membership = self._membership(session, actor_id, workspace_id)
        user = session.get(User, actor_id)
        if membership is None or user is None or not user.is_active:
            raise AuthorizationError(
                f"El actor '{actor_id}' no es un miembro activo de '{workspace_id}'"
            )
        role = Role(membership.role)
        require_permission(role, permission)
        return role

    @staticmethod
    def _serialize_workspace_rbac(session: Session, workspace_id: str) -> None:
        """Extiende la serialización a todos los procesos en PostgreSQL.

        La clave estable solo coordina transacciones; no identifica usuarios ni
        concede permisos. En SQLite, el lock de proceso y el writer lock nativo
        cubren el entorno local soportado.
        """

        PlatformStore._serialize_rbac_scope(session, scope="workspace", identifier=workspace_id)

    @staticmethod
    def _serialize_user_rbac(session: Session, user_id: str) -> None:
        PlatformStore._serialize_rbac_scope(session, scope="user", identifier=user_id)

    @staticmethod
    def _serialize_rbac_scope(
        session: Session,
        *,
        scope: str,
        identifier: str,
    ) -> None:
        bind = session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        digest = hashlib.sha256(f"colmat-rbac:{scope}:{identifier}".encode()).digest()
        lock_key = int.from_bytes(digest[:8], "big", signed=True)
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    def _resolve_telegram_identity(
        self,
        session: Session,
        *,
        workspace_id: str,
        telegram_user_id: str,
        chat_id: str,
    ) -> tuple[User, Membership]:
        binding = session.scalar(
            select(TelegramBinding).where(
                TelegramBinding.workspace_id == workspace_id,
                TelegramBinding.telegram_user_id == telegram_user_id,
                TelegramBinding.chat_id == chat_id,
                TelegramBinding.is_active.is_(True),
            )
        )
        if binding is None:
            raise AuthorizationError(
                "La identidad de Telegram no está vinculada para este usuario y chat"
            )
        user = session.get(User, binding.user_id)
        membership = self._membership(session, binding.user_id, workspace_id)
        if user is None or not user.is_active or membership is None:
            raise AuthorizationError("La cuenta vinculada a Telegram no es un miembro activo")
        return user, membership

    @staticmethod
    def _select_generation_reviewer_binding(
        session: Session,
        *,
        request: GenerationRequest,
        author_actor_id: str,
    ) -> TelegramBinding:
        """Elige un revisor Telegram activo, nunca el autor IA.

        Un binding marcado ``review`` tiene prioridad. Si no existe, se conserva
        el chat solicitante cuando ese usuario sí puede revisar; finalmente se
        usa de forma determinista otro binding de control con permiso de revisión.
        """

        return PlatformStore._select_reviewer_binding(
            session,
            workspace_id=request.workspace_id,
            author_actor_id=author_actor_id,
            preferred_telegram_user_id=request.telegram_user_id,
            preferred_chat_id=request.chat_id,
        )

    @staticmethod
    def _select_reviewer_binding(
        session: Session,
        *,
        workspace_id: str,
        author_actor_id: str,
        preferred_telegram_user_id: str | None = None,
        preferred_chat_id: str | None = None,
    ) -> TelegramBinding:
        """Selecciona de forma determinista un revisor humano activo y distinto."""

        reviewer_roles = tuple(
            role.value for role in sorted(roles_with(Permission.REVIEW_DRAFTS), key=str)
        )
        preferred_match = (
            (TelegramBinding.telegram_user_id == preferred_telegram_user_id)
            & (TelegramBinding.chat_id == preferred_chat_id)
            if preferred_telegram_user_id is not None and preferred_chat_id is not None
            else False
        )
        binding = session.scalar(
            select(TelegramBinding)
            .join(
                Membership,
                (Membership.user_id == TelegramBinding.user_id)
                & (Membership.workspace_id == TelegramBinding.workspace_id),
            )
            .join(User, User.id == TelegramBinding.user_id)
            .where(
                TelegramBinding.workspace_id == workspace_id,
                TelegramBinding.is_active.is_(True),
                TelegramBinding.purpose.in_(("review", "control")),
                TelegramBinding.user_id != author_actor_id,
                Membership.role.in_(reviewer_roles),
                User.is_active.is_(True),
            )
            .order_by(
                case((TelegramBinding.purpose == "review", 0), else_=1),
                case((preferred_match, 0), else_=1),
                case(
                    (Membership.role == Role.REVIEWER.value, 0),
                    (Membership.role == Role.OWNER.value, 1),
                    (Membership.role == Role.ADMIN.value, 2),
                    else_=3,
                ),
                TelegramBinding.created_at,
                TelegramBinding.id,
            )
            .limit(1)
        )
        if binding is None:
            raise AuthorizationError("No existe un binding Telegram activo con permiso de revisión")
        return binding

    @staticmethod
    def _validate_callback_state(
        *,
        draft: Draft,
        revision: Revision,
        action: CallbackAction,
        actor_id: str,
    ) -> None:
        if action in {CallbackAction.APPROVE, CallbackAction.REJECT}:
            if DraftStatus(draft.status) is not DraftStatus.IN_REVIEW:
                raise ConflictError("El draft ya no está pendiente de revisión")
            require_distinct_approver(author_id=revision.created_by, approver_id=actor_id)
            return
        if (
            DraftStatus(draft.status) is not DraftStatus.APPROVED
            or draft.approved_revision_id != revision.id
        ):
            raise ConflictError("El draft ya no está listo para publicación")

    @staticmethod
    def _membership(session: Session, user_id: str, workspace_id: str) -> Membership | None:
        return session.scalar(
            select(Membership).where(
                Membership.user_id == user_id,
                Membership.workspace_id == workspace_id,
            )
        )

    def _required_membership(self, session: Session, user_id: str, workspace_id: str) -> Membership:
        membership = self._membership(session, user_id, workspace_id)
        if membership is None:
            raise NotFoundError("El usuario no pertenece al espacio de trabajo")
        return membership

    @staticmethod
    def _get_user(session: Session, user_id: str) -> User:
        user = session.get(User, user_id)
        if user is None:
            raise NotFoundError(f"No existe el usuario '{user_id}'")
        return user

    @staticmethod
    def _get_draft(session: Session, draft_id: str) -> Draft:
        draft = session.get(Draft, draft_id)
        if draft is None:
            raise NotFoundError(f"No existe el draft '{draft_id}'")
        return draft

    @staticmethod
    def _get_draft_for_update(session: Session, draft_id: str) -> Draft:
        draft = session.scalar(select(Draft).where(Draft.id == draft_id).with_for_update())
        if draft is None:
            raise NotFoundError(f"No existe el draft '{draft_id}'")
        return draft

    @staticmethod
    def _current_revision(session: Session, draft: Draft) -> Revision:
        revision = session.get(Revision, draft.current_revision_id)
        if revision is None or revision.draft_id != draft.id:
            raise ConflictError("El draft no tiene una revisión actual válida")
        calculated = approval_snapshot_hash(
            text=revision.text,
            category=revision.category,
            publish_at=revision.publish_at,
            evidence=revision.evidence,
            image_sha256=revision.image_sha256,
        )
        if calculated != revision.snapshot_hash:
            raise StaleSnapshotError("La revisión persistida no coincide con su snapshot")
        return revision

    @staticmethod
    def _require_can_manage_existing_role(actor_role: Role, existing_role: Role) -> None:
        if actor_role is Role.ADMIN and existing_role in {Role.OWNER, Role.ADMIN}:
            raise AuthorizationError("Un admin no puede modificar owners ni otros admins")

    @staticmethod
    def _require_another_active_owner(
        session: Session, workspace_id: str, *, excluding_user_id: str
    ) -> None:
        another = session.scalar(
            select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.workspace_id == workspace_id,
                Membership.role == Role.OWNER.value,
                Membership.user_id != excluding_user_id,
                User.is_active.is_(True),
            )
        )
        if another is None:
            raise ConflictError("El espacio de trabajo debe conservar al menos un owner activo")

    @staticmethod
    def _audit(
        session: Session,
        *,
        workspace_id: str,
        actor_id: str,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: Any,
        now: datetime,
    ) -> AuditEvent:
        event_row = AuditEvent(
            workspace_id=_normalize_workspace(workspace_id),
            actor_id=_normalize_actor(actor_id),
            action=_normalize_short_text(action, "action", 100),
            entity_type=_normalize_short_text(entity_type, "entity_type", 50),
            entity_id=_normalize_short_text(entity_id, "entity_id", 100),
            detail=_normalize_json(detail or {}, field_name="audit detail"),
            occurred_at=now,
        )
        session.add(event_row)
        session.flush()
        return event_row


def resolve_database_url(database_url: str | None = None) -> str:
    """Resuelve DATABASE_URL y corrige el alias postgres:// usado por algunos hosts."""

    candidate = database_url or os.getenv("DATABASE_URL")
    if candidate is None or not candidate.strip():
        path = DEFAULT_DATABASE_PATH.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+pysqlite:///{path}"
    normalized = candidate.strip()
    if normalized.startswith("postgres://"):
        return "postgresql+psycopg://" + normalized.removeprefix("postgres://")
    if normalized.startswith("postgresql://"):
        return "postgresql+psycopg://" + normalized.removeprefix("postgresql://")
    return normalized


def _migrate_legacy_sqlite_schema(engine: Any) -> None:
    """Actualiza la única revisión SQLite previa sin borrar cuentas ni membresías."""

    connection = engine.raw_connection()
    cursor = connection.cursor()
    legacy_memberships = "memberships_colmat_legacy_v1"
    try:
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]: row[1] or ""
            for row in cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
        }
        if "users" in tables:
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
            if "username" not in columns:
                cursor.execute("ALTER TABLE users ADD COLUMN username VARCHAR(64)")
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username "
                "ON users(username) WHERE username IS NOT NULL"
            )

        membership_sql = tables.get("memberships", "")
        if membership_sql and "scheduler" not in membership_sql.casefold():
            if legacy_memberships in tables:
                raise PlatformStoreError(
                    "La migración SQLite de memberships quedó incompleta; requiere revisión"
                )
            cursor.execute(f"ALTER TABLE memberships RENAME TO {legacy_memberships}")
            cursor.execute(
                """
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
                        role IN (
                            'owner', 'admin', 'editor', 'reviewer',
                            'publisher', 'scheduler', 'auditor'
                        )
                    )
                )
                """
            )
            cursor.execute(
                f"""
                INSERT INTO memberships (
                    id, workspace_id, user_id, role, created_by, created_at, updated_at
                )
                SELECT id, workspace_id, user_id, role, created_by, created_at, updated_at
                FROM {legacy_memberships}
                """
            )
            cursor.execute(f"DROP TABLE {legacy_memberships}")
            cursor.execute(
                "CREATE INDEX ix_memberships_workspace_role ON memberships(workspace_id, role)"
            )
        if "automation_runs" in tables:
            run_columns = {row[1] for row in cursor.execute("PRAGMA table_info(automation_runs)")}
            if "settings_version" not in run_columns:
                cursor.execute(
                    "ALTER TABLE automation_runs ADD COLUMN "
                    "settings_version INTEGER NOT NULL DEFAULT 1"
                )
            if "slot_hash" not in run_columns:
                cursor.execute(
                    "ALTER TABLE automation_runs ADD COLUMN slot_hash VARCHAR(64) "
                    f"NOT NULL DEFAULT '{'0' * 64}'"
                )
        if "telegram_updates" in tables:
            update_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(telegram_updates)")
            }
            telegram_columns = {
                "claim_token_hash": "VARCHAR(64)",
                "claim_fence": "INTEGER NOT NULL DEFAULT 0",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "claimed_at": "DATETIME",
                "lease_expires_at": "DATETIME",
                "prepared_actions": "JSON",
                "business_result": "JSON",
            }
            for column_name, declaration in telegram_columns.items():
                if column_name not in update_columns:
                    cursor.execute(
                        f"ALTER TABLE telegram_updates ADD COLUMN {column_name} {declaration}"
                    )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS ix_telegram_updates_lease "
                "ON telegram_updates(status, lease_expires_at)"
            )
        violations = list(cursor.execute("PRAGMA foreign_key_check"))
        if violations:
            raise PlatformStoreError("La migración SQLite detectó claves foráneas inválidas")
        connection.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:
        connection.rollback()
        raise
    finally:
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
            connection.close()


def _backfill_sqlite_automation_settings(engine: Any) -> None:
    """Crea settings pausados y completa evidencia de agendas SQLite anteriores."""

    with Session(engine) as session, session.begin():
        for settings in session.scalars(select(AutomationSettings)):
            if not isinstance(settings.slots, list):
                continue
            migrated_slots: list[Any] = []
            changed = False
            for slot in settings.slots:
                if not isinstance(slot, dict) or not isinstance(slot.get("evidence"), dict):
                    migrated_slots.append(slot)
                    continue
                evidence = dict(slot["evidence"])
                if "expected_figure" not in evidence:
                    evidence["expected_figure"] = None
                    changed = True
                if "expected_source" not in evidence:
                    evidence["expected_source"] = None
                    changed = True
                migrated_slots.append({**slot, "evidence": evidence})
            if changed:
                settings.slots = migrated_slots

        workspaces = list(session.scalars(select(Membership.workspace_id).distinct()))
        for workspace in workspaces:
            if session.get(AutomationSettings, workspace) is not None:
                continue
            actor_id = session.scalar(
                select(Membership.user_id)
                .where(Membership.workspace_id == workspace)
                .order_by(
                    # owner primero, luego admin y finalmente el registro más antiguo.
                    case(
                        (Membership.role == Role.OWNER.value, 0),
                        (Membership.role == Role.ADMIN.value, 1),
                        else_=2,
                    ),
                    Membership.created_at,
                    Membership.id,
                )
                .limit(1)
            )
            if actor_id is None:
                continue
            session.add(
                PlatformStore._new_automation_settings(
                    workspace_id=workspace,
                    actor_id=actor_id,
                    now=_utc_now(),
                )
            )


def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _is_sqlite_memory_url(database_url: str) -> bool:
    return database_url.endswith(":memory:") or "mode=memory" in database_url


def _normalize_revision_material(
    *,
    text: str,
    category: str,
    publish_at: datetime | date | str,
    evidence: Any,
    image_sha256: str | None,
) -> dict[str, Any]:
    normalized_text = _normalize_text(text)
    normalized_category = _normalize_category(category)
    normalized_publish_at = _normalize_publish_at(publish_at)
    normalized_evidence = _normalize_json(evidence, field_name="evidence")
    normalized_image = _normalize_sha256(image_sha256, required=False)
    return {
        "text": normalized_text,
        "category": normalized_category,
        "publish_at": normalized_publish_at,
        "evidence": normalized_evidence,
        "image_sha256": normalized_image,
        "snapshot_hash": approval_snapshot_hash(
            text=normalized_text,
            category=normalized_category,
            publish_at=normalized_publish_at,
            evidence=normalized_evidence,
            image_sha256=normalized_image,
        ),
    }


def _normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("text debe ser texto")
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n")
    if not normalized.strip():
        raise ValueError("text no puede estar vacío")
    if "\x00" in normalized:
        raise ValueError("text contiene un carácter nulo")
    if len(normalized) > 20_000:
        raise ValueError("text supera los 20000 caracteres")
    return normalized


def _normalize_category(value: str) -> str:
    return _normalize_short_text(value, "category", 80)


def _normalize_publish_at(value: datetime | date | str) -> datetime:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, str) and value.strip():
        normalized = value.strip().replace("Z", "+00:00")
        try:
            if "T" not in normalized and " " not in normalized:
                parsed_date = date.fromisoformat(normalized)
                parsed = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
            else:
                parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("publish_at debe ser una fecha ISO 8601 válida") from exc
    else:
        raise ValueError("publish_at debe ser date, datetime o texto ISO 8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("publish_at debe incluir zona horaria")
    return parsed.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_json(value: Any, *, field_name: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} debe ser JSON válido") from exc


def _normalize_sha256(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError("Se requiere un hash SHA-256")
        return None
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value.lower()):
        raise ValueError("El hash debe contener exactamente 64 dígitos hexadecimales")
    return value.lower()


def _normalize_email(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("email debe ser texto")
    normalized = value.strip().lower()
    if len(normalized) > 320 or EMAIL_PATTERN.fullmatch(normalized) is None:
        raise ValueError("email no tiene un formato válido")
    return normalized


def _normalize_username(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("username debe ser texto o null")
    normalized = value.strip().lower()
    if USERNAME_PATTERN.fullmatch(normalized) is None:
        raise ValueError("username debe tener 3-64 caracteres: minúsculas, números, ., _ o -")
    return normalized


def _normalize_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} debe ser booleano")
    return value


def _normalize_bounded_int(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} debe estar entre {minimum} y {maximum}")
    return value


def _normalize_positive_int(value: object, field_name: str, *, maximum: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} debe ser un entero positivo")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} no puede superar {maximum}")
    return value


def _normalize_timezone(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("timezone debe ser texto")
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise ValueError("timezone debe tener entre 1 y 64 caracteres")
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"timezone IANA desconocida: {normalized!r}") from exc
    return normalized


def _normalize_automation_slots(value: object) -> list[dict[str, Any]]:
    normalized = _normalize_json(value, field_name="slots")
    if not isinstance(normalized, list):
        raise ValueError("slots debe ser una lista JSON")
    if len(normalized) > MAX_AUTOMATION_SLOTS:
        raise ValueError(f"slots no puede contener más de {MAX_AUTOMATION_SLOTS} entradas")
    if len(json.dumps(normalized, ensure_ascii=False)) > 100_000:
        raise ValueError("slots supera el tamaño máximo permitido")
    seen_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    required_fields = {
        "id",
        "at",
        "mode",
        "category",
        "institution",
        "brief",
        "generate_image",
        "evidence",
    }
    for index, slot in enumerate(normalized):
        if not isinstance(slot, dict):
            raise ValueError(f"slots[{index}] debe ser un objeto JSON")
        keys = set(slot)
        if any(not isinstance(key, str) for key in keys):
            raise ValueError(f"slots[{index}] solo admite claves de texto")
        missing = sorted(required_fields - keys)
        unknown = sorted(keys - (required_fields | {"weekdays"}))
        if missing:
            raise ValueError(f"Faltan campos en slots[{index}]: {', '.join(missing)}")
        if unknown:
            raise ValueError(f"Campos desconocidos en slots[{index}]: {', '.join(unknown)}")

        slot_id = _normalize_slot_id(slot["id"])
        if slot_id in seen_ids:
            raise ValueError("slot.id no admite duplicados")
        seen_ids.add(slot_id)
        at = _normalize_short_text(slot["at"], f"slots[{index}].at", 5)
        if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", at) is None:
            raise ValueError(f"slots[{index}].at debe usar HH:MM en formato 24 horas")
        try:
            category = EditorialCategory(slot["category"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"slots[{index}].category no es canónica") from exc
        try:
            institution = Institution(slot["institution"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"slots[{index}].institution no es canónica") from exc
        brief = _normalize_short_text(slot["brief"], f"slots[{index}].brief", 1_000)
        if len(brief) < 10:
            raise ValueError(f"slots[{index}].brief debe tener al menos 10 caracteres")
        evidence = _normalize_automation_evidence(slot["evidence"], index=index)
        weekdays = _normalize_automation_weekdays(slot.get("weekdays", _UNSET), index=index)
        selected = {
            "id": slot_id,
            "at": at,
            "mode": _normalize_automation_mode(slot["mode"]).value,
            "category": category.value,
            "institution": institution.value,
            "brief": brief,
            "generate_image": _normalize_bool(
                slot["generate_image"], f"slots[{index}].generate_image"
            ),
            "evidence": evidence,
        }
        if weekdays is not None:
            selected["weekdays"] = weekdays
        result.append(selected)
    return result


def _normalize_automation_weekdays(value: object, *, index: int) -> list[str] | None:
    """Normaliza restricciones semanales; ``None`` representa los siete días."""

    field = f"slots[{index}].weekdays"
    if value is _UNSET:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} debe ser una lista no vacía")
    if len(value) > len(CANONICAL_AUTOMATION_WEEKDAYS):
        raise ValueError(f"{field} no admite más de siete días")
    if any(not isinstance(day, str) or day not in CANONICAL_AUTOMATION_WEEKDAYS for day in value):
        allowed = ", ".join(CANONICAL_AUTOMATION_WEEKDAYS)
        raise ValueError(f"{field} solo admite valores canónicos: {allowed}")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} no admite duplicados")
    canonical = [day for day in CANONICAL_AUTOMATION_WEEKDAYS if day in value]
    return canonical if len(canonical) < len(CANONICAL_AUTOMATION_WEEKDAYS) else None


def _normalize_automation_evidence(value: object, *, index: int) -> dict[str, Any]:
    field = f"slots[{index}].evidence"
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} debe ser un objeto JSON con claves de texto")
    keys = set(value)
    required = {"verified", "reference", "expected_figure", "expected_source"}
    if keys != required:
        missing = sorted(required - keys)
        unknown = sorted(keys - required)
        detail = []
        if missing:
            detail.append("faltan " + ", ".join(missing))
        if unknown:
            detail.append("sobran " + ", ".join(unknown))
        raise ValueError(f"{field} no tiene el esquema cerrado: {'; '.join(detail)}")
    verified = _normalize_bool(value["verified"], f"{field}.verified")
    reference_value = value["reference"]
    reference = None
    if reference_value is not None:
        reference = _normalize_short_text(reference_value, f"{field}.reference", 500)
        if len(reference) < 3:
            raise ValueError(f"{field}.reference debe tener al menos 3 caracteres")
    if verified and reference is None:
        raise ValueError(f"{field}.verified exige una referencia auditable")
    expected_figure_value = value["expected_figure"]
    expected_figure = None
    if expected_figure_value is not None:
        expected_figure = _normalize_short_text(
            expected_figure_value,
            f"{field}.expected_figure",
            80,
        )
    expected_source_value = value["expected_source"]
    expected_source = None
    if expected_source_value is not None:
        expected_source = _normalize_short_text(
            expected_source_value,
            f"{field}.expected_source",
            200,
        )
        if len(expected_source) < 3:
            raise ValueError(f"{field}.expected_source debe tener al menos 3 caracteres")
    if (expected_figure is None) != (expected_source is None):
        raise ValueError(f"{field} exige expected_figure y expected_source juntos")
    if verified and expected_figure is None:
        raise ValueError(f"{field}.verified exige cifra y fuente esperadas concretas")
    return {
        "verified": verified,
        "reference": reference,
        "expected_figure": expected_figure,
        "expected_source": expected_source,
    }


def _normalize_slot_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("slot_id debe ser texto")
    normalized = value.strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", normalized) is None:
        raise ValueError("slot_id debe usar 2-80 caracteres: minúsculas, números, _ o -")
    return normalized


def _normalize_publication_request_status(value: object) -> PublicationRequestStatus:
    try:
        return (
            value
            if isinstance(value, PublicationRequestStatus)
            else PublicationRequestStatus(value)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Estado de solicitud de publicación desconocido: {value!r}") from exc


def _normalize_generation_request_status(value: object) -> GenerationRequestStatus:
    try:
        return (
            value if isinstance(value, GenerationRequestStatus) else GenerationRequestStatus(value)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Estado de solicitud de generación desconocido: {value!r}") from exc


def _normalize_generation_notification_status(
    value: object,
) -> GenerationNotificationStatus:
    try:
        return (
            value
            if isinstance(value, GenerationNotificationStatus)
            else GenerationNotificationStatus(value)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Estado de notificación de generación desconocido: {value!r}") from exc


def _normalize_automation_review_notification_status(
    value: object,
) -> AutomationReviewNotificationStatus:
    try:
        return (
            value
            if isinstance(value, AutomationReviewNotificationStatus)
            else AutomationReviewNotificationStatus(value)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Estado de notificación de revisión desconocido: {value!r}") from exc


def _normalize_generation_claim_token(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,200}", value) is None:
        raise ValueError("El token del claim de generación no tiene un formato válido")
    return value


def _hash_generation_claim_token(value: str) -> str:
    normalized = _normalize_generation_claim_token(value)
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def _require_generation_claim_credentials(
    request: GenerationRequest,
    *,
    actor_id: str,
    claim_token: str,
    claim_fence: int,
) -> None:
    if request.claimed_by != actor_id:
        raise AuthorizationError("Solo el worker que reclamó la generación puede cerrarla")
    if (
        request.claim_fence != claim_fence
        or request.claim_token_hash is None
        or not secrets.compare_digest(
            request.claim_token_hash,
            _hash_generation_claim_token(claim_token),
        )
    ):
        raise ConflictError("El token o fence de generación no coincide")


def _require_generation_notification_claim_credentials(
    notification: GenerationNotification,
    *,
    actor_id: str,
    claim_token: str,
    claim_fence: int,
) -> None:
    if notification.claimed_by != actor_id:
        raise AuthorizationError("Solo el worker que reclamó la notificación puede cerrarla")
    if (
        notification.claim_fence != claim_fence
        or notification.claim_token_hash is None
        or not secrets.compare_digest(
            notification.claim_token_hash,
            _hash_generation_claim_token(claim_token),
        )
    ):
        raise ConflictError("El token o fence de notificación no coincide")


def _require_automation_review_notification_claim_credentials(
    notification: AutomationReviewNotification,
    *,
    actor_id: str,
    claim_token: str,
    claim_fence: int,
) -> None:
    if notification.claimed_by != actor_id:
        raise AuthorizationError("Solo el worker que reclamó la notificación puede cerrarla")
    if (
        notification.claim_fence != claim_fence
        or notification.claim_token_hash is None
        or not secrets.compare_digest(
            notification.claim_token_hash,
            _hash_generation_claim_token(claim_token),
        )
    ):
        raise ConflictError("El token o fence de notificación no coincide")


def _require_matching_generation_request(
    request: GenerationRequest,
    *,
    actor_id: str,
    brief: str,
    telegram_user_id: str,
    chat_id: str,
    generate_image: bool,
    category: str | None,
    institution: str | None,
) -> None:
    if (
        request.requested_by != actor_id
        or request.brief != brief
        or request.telegram_user_id != telegram_user_id
        or request.chat_id != chat_id
        or request.generate_image is not generate_image
        or request.category != category
        or request.institution != institution
    ):
        raise ConflictError("La clave de idempotencia pertenece a otra generación")


def _normalize_generation_image(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("image debe ser un objeto o None")
    expected = {"url", "sha256", "mime_type", "byte_size", "metadata"}
    if set(value) != expected:
        raise ValueError("image debe contener url, sha256, mime_type, byte_size y metadata")
    url = _normalize_short_text(value["url"], "image.url", 4096)
    sha256 = _normalize_sha256(value["sha256"], required=True)
    mime_type = _normalize_short_text(value["mime_type"], "image.mime_type", 120)
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("image.mime_type no está permitido")
    byte_size = _normalize_positive_int(
        value["byte_size"],
        "image.byte_size",
        maximum=20 * 1024 * 1024,
    )
    metadata = _normalize_json(value["metadata"], field_name="image.metadata")
    if not isinstance(metadata, dict):
        raise ValueError("image.metadata debe ser un objeto JSON")
    return {
        "url": url,
        "sha256": sha256,
        "mime_type": mime_type,
        "byte_size": byte_size,
        "metadata": metadata,
    }


def _normalize_publication_claim_token(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,200}", value) is None:
        raise ValueError("El token del claim no tiene un formato válido")
    return value


def _hash_publication_claim_token(value: str) -> str:
    normalized = _normalize_publication_claim_token(value)
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def _publication_attempt_idempotency_key(request_id: str, claim_fence: int) -> str:
    return f"publication-request:{request_id}:f{claim_fence}"


def _expired_publication_claim_message(status: PublicationRequestStatus) -> str:
    if status is PublicationRequestStatus.UNKNOWN:
        return "La lease del claim venció; la solicitud quedó UNKNOWN y no se reencolará"
    return (
        "La lease del claim venció; la solicitud se concilió como "
        f"{status.value.upper()} y no se reencolará"
    )


def _require_publication_claim_credentials(
    request: PublicationRequest,
    *,
    actor_id: str,
    claim_token: str,
    claim_fence: int,
) -> None:
    if request.claimed_by != actor_id:
        raise AuthorizationError("Solo el actor que reclamó la solicitud puede usar el claim")
    if (
        request.claim_fence != claim_fence
        or request.claim_token_hash is None
        or not secrets.compare_digest(
            request.claim_token_hash,
            _hash_publication_claim_token(claim_token),
        )
    ):
        raise ConflictError("El token o fence del claim no coincide")


def _require_matching_publication_request(
    request: PublicationRequest,
    *,
    draft_id: str,
    snapshot_hash: str,
) -> None:
    if request.draft_id != draft_id or request.snapshot_hash != snapshot_hash:
        raise ConflictError("La clave de idempotencia pertenece a otro snapshot aprobado")


def _require_matching_publication_attempt(
    request: PublicationRequest,
    attempt: PublishAttempt,
    *,
    actor_id: str,
    expected_idempotency_key: str,
    target_status: PublicationRequestStatus,
) -> None:
    if (
        attempt.workspace_id != request.workspace_id
        or attempt.draft_id != request.draft_id
        or attempt.revision_id != request.revision_id
        or attempt.snapshot_hash != request.snapshot_hash
        or attempt.channel != request.channel
        or attempt.requested_by != actor_id
        or attempt.idempotency_key != expected_idempotency_key
        or request.claimed_at is None
        or attempt.started_at < request.claimed_at
        or (attempt.finished_at is not None and attempt.finished_at < attempt.started_at)
    ):
        raise ConflictError("El PublishAttempt no coincide con el claim cercado")
    attempt_status = PublishStatus(attempt.status)
    if attempt_status is PublishStatus.PENDING or attempt.finished_at is None:
        raise ConflictError("El PublishAttempt todavía no tiene un resultado final")
    if attempt_status is PublishStatus.SUCCEEDED and attempt.provider_post_id is None:
        raise ConflictError("El PublishAttempt exitoso no tiene identificador del proveedor")
    if attempt_status.value != target_status.value:
        raise ConflictError("El resultado del PublishAttempt no coincide con la solicitud")


def _normalize_automation_mode(value: object) -> AutomationMode:
    try:
        return value if isinstance(value, AutomationMode) else AutomationMode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Modo de automatización desconocido: {value!r}") from exc


def _normalize_automation_run_status(value: object) -> AutomationRunStatus:
    try:
        return value if isinstance(value, AutomationRunStatus) else AutomationRunStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Estado de automatización desconocido: {value!r}") from exc


def _require_matching_automation_claim(
    run: AutomationRun,
    *,
    slot_id: str,
    scheduled_for: datetime,
    mode: AutomationMode | None,
    draft_id: str | None,
    slot_hash: str,
) -> None:
    if (
        run.slot_id != slot_id
        or run.scheduled_for != scheduled_for
        or (mode is not None and run.mode != mode.value)
        or (draft_id is not None and run.draft_id != draft_id)
        or run.slot_hash != slot_hash
    ):
        raise ConflictError("La clave de idempotencia pertenece a otro run de automatización")


def _require_scheduled_automation_claim(
    settings: AutomationSettings,
    *,
    idempotency_key: str,
    slot_id: str,
    scheduled_for: datetime,
    mode: AutomationMode,
) -> dict[str, Any]:
    try:
        slots = _normalize_automation_slots(settings.slots)
    except ValueError as exc:
        raise ConflictError("La agenda persistida no tiene un esquema válido") from exc
    selected = next((slot for slot in slots if slot["id"] == slot_id), None)
    if selected is None:
        raise ConflictError("El slot no pertenece a la agenda autorizada")
    if selected["mode"] != mode.value:
        raise ConflictError("El modo del slot no coincide con la agenda autorizada")
    local = scheduled_for.astimezone(ZoneInfo(settings.timezone))
    if local.second or local.microsecond or selected["at"] != local.strftime("%H:%M"):
        raise ConflictError("La hora del claim no coincide con la agenda autorizada")
    weekdays = selected.get("weekdays")
    if weekdays is not None and CANONICAL_AUTOMATION_WEEKDAYS[local.weekday()] not in weekdays:
        raise ConflictError("El día local del claim no coincide con la agenda autorizada")
    expected_key = f"colmat:auto:v1:{local.date().isoformat()}:{slot_id}"
    if idempotency_key != expected_key:
        raise ConflictError("La clave de idempotencia no coincide con fecha local y slot")
    return selected


def _normalize_single_automation_slot(value: object) -> dict[str, Any]:
    slots = _normalize_automation_slots([value])
    return slots[0]


def _automation_slot_hash(slot: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(slot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _automation_reconciliation_error(
    status: AutomationRunStatus,
    *,
    reason: str,
) -> str | None:
    if status is AutomationRunStatus.SUCCEEDED:
        return None
    messages = {
        "stale_claim_before_publish": (
            "El claim venció antes de iniciar la publicación; no se reintentará."
        ),
        "stale_prepared_snapshot_inconsistent": (
            "El snapshot preparado quedó inconsistente; requiere conciliación manual."
        ),
        "stale_prepared_reviewer_missing": (
            "No existe un revisor Telegram activo; el draft queda disponible para conciliación."
        ),
        "publish_attempt_failed": (
            "El intento de publicación terminó en fallo confirmado; no se reintentará."
        ),
        "publish_attempt_inconclusive": (
            "El intento de publicación quedó pendiente o desconocido; no se reintentará."
        ),
        "publish_attempt_missing": (
            "No existe evidencia persistida del resultado externo; no se reintentará."
        ),
        "inconsistent_success_evidence": (
            "La evidencia local de éxito es inconsistente; no se reintentará."
        ),
    }
    return messages[reason]


def _automation_revision_engagement_score(revision: Revision) -> int:
    evidence = revision.evidence
    if not isinstance(evidence, Mapping):
        return 0
    engagement = evidence.get("engagement")
    if not isinstance(engagement, Mapping):
        return 0
    score = engagement.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        return 0
    return min(100, max(0, score))


def _sanitize_automation_error(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("error debe ser texto o null")
    normalized = unicodedata.normalize("NFC", value)
    normalized = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in normalized
    ).strip()
    if not normalized:
        return None
    redactions = (
        (r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{6,}", "Bearer [REDACTED]"),
        (r"(?i)\bsk-[A-Za-z0-9_-]{6,}", "[REDACTED]"),
        (r"\b[0-9]{6,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]"),
        (r"(?i)(://)[^/@\s]+@", r"\1[REDACTED]@"),
        (
            r"(?i)\b(api[_ -]?key|access[_ -]?token|token|secret|password|authorization)"
            r"\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
        ),
    )
    for pattern, replacement in redactions:
        normalized = re.sub(pattern, replacement, normalized)
    normalized = " ".join(normalized.split())
    if len(normalized) > MAX_AUTOMATION_ERROR_LENGTH:
        normalized = normalized[: MAX_AUTOMATION_ERROR_LENGTH - 1].rstrip() + "…"
    return normalized


def _require_direct_publish_kill_switch() -> None:
    enabled = os.getenv(DIRECT_PUBLISH_ENV, "").strip().lower()
    if enabled != "true":
        raise ConflictError(
            f"El modo directo está bloqueado: {DIRECT_PUBLISH_ENV} debe valer exactamente true"
        )


def _local_day_bounds(value: datetime, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    local_day = value.astimezone(zone).date()
    start = datetime(local_day.year, local_day.month, local_day.day, tzinfo=zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _automation_settings_audit_snapshot(settings: AutomationSettings) -> dict[str, Any]:
    return {
        "enabled": settings.enabled,
        "mode": settings.mode,
        "timezone": settings.timezone,
        "slots": settings.slots,
        "generate_images": settings.generate_images,
        "min_engagement_score": settings.min_engagement_score,
        "max_posts_per_day": settings.max_posts_per_day,
        "version": settings.version,
        "direct_authorized_by": settings.direct_authorized_by,
        "direct_authorized_at": (
            _format_time(settings.direct_authorized_at)
            if settings.direct_authorized_at is not None
            else None
        ),
    }


def _normalize_workspace(value: str) -> str:
    normalized = _normalize_short_text(value, "workspace_id", 80)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", normalized):
        raise ValueError("workspace_id solo admite minúsculas, números, - y _")
    return normalized


def _normalize_entity_id(value: str) -> str:
    return _normalize_short_text(value, "id", 36)


def _normalize_actor(value: str) -> str:
    return _normalize_short_text(value, "actor_id", 80)


def _normalize_short_text(value: str, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} debe ser texto")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} debe tener entre 1 y {max_length} caracteres")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{field_name} contiene caracteres de control")
    return normalized


def _normalize_note(value: str | None, *, required: bool, max_length: int = 4000) -> str | None:
    if value is None:
        if required:
            raise ValueError("Se requiere una razón")
        return None
    normalized = _normalize_short_text(value, "nota", max_length)
    return normalized


def _normalize_chat_id(value: int | str) -> str:
    if isinstance(value, bool):
        raise ValueError("chat_id no es válido")
    normalized = str(value).strip()
    if not re.fullmatch(r"-?[0-9]{1,20}", normalized):
        raise ValueError("chat_id debe ser un identificador decimal de Telegram")
    return normalized


def _normalize_telegram_user_id(value: int | str) -> str:
    if isinstance(value, bool):
        raise ValueError("telegram_user_id no es válido")
    normalized = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,19}", normalized):
        raise ValueError("telegram_user_id debe ser el from.id decimal de Telegram")
    return normalized


def _normalize_callback_action(value: CallbackAction | str) -> CallbackAction:
    try:
        return value if isinstance(value, CallbackAction) else CallbackAction(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Acción de callback desconocida: {value!r}") from exc


def _permission_for_callback(action: CallbackAction) -> Permission:
    if action in {CallbackAction.APPROVE, CallbackAction.REJECT}:
        return Permission.REVIEW_DRAFTS
    return Permission.PUBLISH_DRAFTS


def _hash_nonce(value: str) -> str:
    if not isinstance(value, str) or not 32 <= len(value) <= 512:
        raise ValueError("El nonce del callback no tiene un formato válido")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_telegram_update_claim_token(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,200}", value) is None:
        raise ValueError("El token del claim de Telegram no tiene un formato válido")
    return value


def _hash_telegram_update_claim_token(value: object) -> str:
    normalized = _normalize_telegram_update_claim_token(value)
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def _require_telegram_update_claim(
    stored: TelegramUpdate,
    *,
    claim_token: object,
    claim_fence: object,
) -> None:
    normalized_token = _normalize_telegram_update_claim_token(claim_token)
    normalized_fence = _normalize_positive_int(claim_fence, "claim_fence", maximum=None)
    if (
        stored.claim_token_hash is None
        or stored.claim_fence != normalized_fence
        or not secrets.compare_digest(
            stored.claim_token_hash,
            _hash_telegram_update_claim_token(normalized_token),
        )
    ):
        raise ConflictError("El token o fence del claim de Telegram no coincide")


def _normalize_prepared_telegram_actions(
    value: object,
) -> tuple[dict[str, Any], ...] | None:
    if value is None:
        return None
    normalized = _normalize_json(value, field_name="prepared_actions")
    if not isinstance(normalized, list):
        raise ValueError("prepared_actions debe ser una lista JSON")
    if len(normalized) > 20:
        raise ValueError("prepared_actions no puede superar 20 acciones")
    if len(json.dumps(normalized, ensure_ascii=False)) > 100_000:
        raise ValueError("prepared_actions supera el tamaño máximo permitido")
    if any(
        not isinstance(item, dict) or any(not isinstance(key, str) for key in item)
        for item in normalized
    ):
        raise ValueError("Cada prepared_action debe ser un objeto con claves de texto")
    return tuple(normalized)


def _normalize_now(value: datetime | None) -> datetime:
    selected = value or _utc_now()
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("now debe incluir zona horaria")
    return selected.astimezone(UTC)


def _normalize_role(value: Role | str) -> Role:
    try:
        return value if isinstance(value, Role) else Role(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Rol desconocido: {value!r}") from exc


def table_names() -> Sequence[str]:
    """Lista estable para health checks y migraciones externas."""

    return tuple(sorted(Base.metadata.tables))
