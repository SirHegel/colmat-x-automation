from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    create_engine,
    event,
    func,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import DateTime, TypeDecorator

from colmat_x.rbac import (
    AuthorizationError,
    Permission,
    Role,
    require_distinct_approver,
    require_permission,
    require_role_assignment,
)

DEFAULT_WORKSPACE_ID = "colmat"
DEFAULT_DATABASE_PATH = Path(".state/colmat-platform.db")
SYSTEM_ACTOR = "system:bootstrap"
TELEGRAM_ACTOR = "service:telegram"
CALLBACK_MAX_LIFETIME = timedelta(hours=24)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


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


class CallbackAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    PUBLISH = "publish"


@dataclass(frozen=True)
class IssuedCallbackIntent:
    """Nonce entregable a Telegram y registro persistido que lo limita."""

    nonce: str
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


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
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
            "role IN ('owner', 'admin', 'editor', 'reviewer', 'publisher', 'auditor')",
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
            Base.metadata.create_all(self.engine)

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
        password_hash: str | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        actor_id: str = SYSTEM_ACTOR,
        user_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[User, Membership]:
        workspace = _normalize_workspace(workspace_id)
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(func.count(Membership.id)).where(Membership.workspace_id == workspace)
            )
            if existing:
                raise ConflictError(f"El espacio '{workspace}' ya tiene un owner")
            user = self._new_user(
                session,
                email=email,
                display_name=display_name,
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
            return user, membership

    def create_user(
        self,
        *,
        actor_id: str,
        email: str,
        display_name: str,
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
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            actor_role = self._authorize(session, actor, workspace, Permission.MANAGE_USERS)
            user = self._get_user(session, user_id)
            if user.id == actor and not active:
                raise ConflictError("Un administrador no puede desactivar su propia cuenta")
            target_membership = self._membership(session, user_id, workspace)
            if target_membership is not None:
                self._require_can_manage_existing_role(actor_role, Role(target_membership.role))
                if not active and target_membership.role == Role.OWNER.value:
                    self._require_another_active_owner(
                        session, workspace, excluding_user_id=user_id
                    )
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
        target_role = _normalize_role(role)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            actor_role = self._authorize(session, actor, workspace, Permission.MANAGE_MEMBERSHIPS)
            require_role_assignment(actor_role, target_role)
            self._get_user(session, user_id)
            existing = self._membership(session, user_id, workspace)
            if existing is not None:
                raise ConflictError("El usuario ya pertenece al espacio de trabajo")
            membership = Membership(
                workspace_id=workspace,
                user_id=user_id,
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
                detail={"user_id": user_id, "role": target_role.value},
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
        target_role = _normalize_role(role)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            actor_role = self._authorize(session, actor, workspace, Permission.MANAGE_MEMBERSHIPS)
            require_role_assignment(actor_role, target_role)
            membership = self._required_membership(session, user_id, workspace)
            self._require_can_manage_existing_role(actor_role, Role(membership.role))
            if membership.role == Role.OWNER.value and target_role is not Role.OWNER:
                self._require_another_owner(session, workspace, excluding_user_id=user_id)
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
                    "user_id": user_id,
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
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
            actor_role = self._authorize(session, actor, workspace, Permission.MANAGE_MEMBERSHIPS)
            membership = self._required_membership(session, user_id, workspace)
            self._require_can_manage_existing_role(actor_role, Role(membership.role))
            if membership.role == Role.OWNER.value:
                self._require_another_owner(session, workspace, excluding_user_id=user_id)
            detail = {"user_id": user_id, "role": membership.role}
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
            draft = self._get_draft(session, draft_id)
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
        telegram_user_id: int | str,
        chat_id: int | str,
        expires_at: datetime,
        actor_id: str = TELEGRAM_ACTOR,
        now: datetime | None = None,
    ) -> IssuedCallbackIntent:
        """Crea un nonce opaco ligado al actor, chat y snapshot actuales."""

        normalized_action = _normalize_callback_action(action)
        telegram_identity = _normalize_telegram_user_id(telegram_user_id)
        normalized_chat = _normalize_chat_id(chat_id)
        creator = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        expiration = _normalize_now(expires_at)
        if expiration <= timestamp:
            raise ValueError("expires_at debe estar en el futuro")
        if expiration > timestamp + CALLBACK_MAX_LIFETIME:
            raise ValueError("El callback no puede durar más de 24 horas")
        nonce = secrets.token_urlsafe(32)
        nonce_hash = _hash_nonce(nonce)
        with self._sessions.begin() as session:
            draft = self._get_draft(session, draft_id)
            user, membership = self._resolve_telegram_identity(
                session,
                workspace_id=draft.workspace_id,
                telegram_user_id=telegram_identity,
                chat_id=normalized_chat,
            )
            require_permission(Role(membership.role), _permission_for_callback(normalized_action))
            revision = self._current_revision(session, draft)
            self._validate_callback_state(
                draft=draft,
                revision=revision,
                action=normalized_action,
                actor_id=user.id,
            )
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

    def consume_callback_intent(
        self,
        nonce: str,
        action: CallbackAction | str,
        *,
        telegram_user_id: int | str,
        chat_id: int | str,
        now: datetime | None = None,
    ) -> CallbackIntent:
        """Valida y consume el nonce con compare-and-swap dentro de una transacción."""

        nonce_hash = _hash_nonce(nonce)
        normalized_action = _normalize_callback_action(action)
        telegram_identity = _normalize_telegram_user_id(telegram_user_id)
        normalized_chat = _normalize_chat_id(chat_id)
        timestamp = _normalize_now(now)
        with self._sessions.begin() as session:
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
    ) -> bool:
        """Implementa directamente el protocolo TelegramUpdateStore."""

        actor_id = (
            f"telegram:{telegram_user_id}" if telegram_user_id is not None else TELEGRAM_ACTOR
        )
        return self.record_telegram_update(
            update_id,
            payload,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            actor_id=actor_id,
        )

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
        now: datetime | None = None,
    ) -> TelegramUpdate:
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        normalized_error = _normalize_note(error, required=False)
        with self._sessions.begin() as session:
            update = session.get(TelegramUpdate, update_id)
            if update is None:
                raise NotFoundError(f"No existe el update de Telegram {update_id}")
            if UpdateStatus(update.status) is not UpdateStatus.RECEIVED:
                raise ConflictError("La actualización de Telegram ya fue finalizada")
            update.status = (
                UpdateStatus.FAILED.value if normalized_error else UpdateStatus.PROCESSED.value
            )
            update.error = normalized_error
            update.processed_at = timestamp
            self._audit(
                session,
                workspace_id=update.workspace_id,
                actor_id=actor,
                action=(
                    "telegram.update_failed" if normalized_error else "telegram.update_processed"
                ),
                entity_type="telegram_update",
                entity_id=str(update_id),
                detail={"error": normalized_error},
                now=timestamp,
            )
            return update

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
    ) -> Approval:
        actor = _normalize_actor(actor_id)
        timestamp = _normalize_now(now)
        expected = _normalize_sha256(expected_snapshot_hash, required=True)
        normalized_reason = _normalize_note(reason, required=False)
        with self._sessions.begin() as session:
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
            return approval

    def _new_user(
        self,
        session: Session,
        *,
        email: str,
        display_name: str,
        password_hash: str | None,
        user_id: str | None,
        now: datetime,
    ) -> User:
        normalized_email = _normalize_email(email)
        normalized_name = _normalize_short_text(display_name, "display_name", 120)
        normalized_password = _normalize_note(password_hash, required=False, max_length=255)
        if session.scalar(select(User.id).where(User.email == normalized_email)) is not None:
            raise ConflictError(f"Ya existe un usuario con email '{normalized_email}'")
        user = User(
            id=_normalize_entity_id(user_id) if user_id else _new_id(),
            email=normalized_email,
            display_name=normalized_name,
            password_hash=normalized_password,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.flush()
        return user

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
    def _require_another_owner(
        session: Session, workspace_id: str, *, excluding_user_id: str
    ) -> None:
        another = session.scalar(
            select(Membership.id).where(
                Membership.workspace_id == workspace_id,
                Membership.role == Role.OWNER.value,
                Membership.user_id != excluding_user_id,
            )
        )
        if another is None:
            raise ConflictError("El espacio de trabajo debe conservar al menos un owner")

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
            raise ConflictError("El espacio de trabajo debe conservar un owner activo")

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
