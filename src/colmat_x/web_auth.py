from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    delete,
    or_,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from colmat_x.platform_store import (
    DEFAULT_WORKSPACE_ID,
    Base,
    Membership,
    PlatformStore,
    TelegramBinding,
    User,
    UTCDateTime,
)
from colmat_x.rbac import Role

WEB_AUTH_PEPPER_ENV: Final = "WEB_AUTH_PEPPER"
CHALLENGE_LIFETIME: Final = timedelta(minutes=5)
CHALLENGE_ATTEMPT_LIMIT: Final = 5
CHALLENGE_RATE_LIMIT: Final = 5
CHALLENGE_IDENTIFIER_RATE_LIMIT: Final = 10
CHALLENGE_IP_RATE_LIMIT: Final = 25
CHALLENGE_RATE_WINDOW: Final = timedelta(minutes=15)
SESSION_ABSOLUTE_LIFETIME: Final = timedelta(hours=12)
SESSION_IDLE_LIFETIME: Final = timedelta(hours=2)
_LOCK_SHARDS: Final = 64
_RATE_LOCKS: Final = tuple(threading.Lock() for _ in range(_LOCK_SHARDS))
_CHALLENGE_LOCKS: Final = tuple(threading.Lock() for _ in range(_LOCK_SHARDS))
_ROLE_VALUES: Final = frozenset(role.value for role in Role)


class WebAuthError(RuntimeError):
    """Base de errores seguros de autenticación web."""


class WebAuthConfigurationError(WebAuthError):
    """La autenticación web no tiene una configuración segura."""


class InvalidChallengeError(WebAuthError):
    """El reto no existe, ya se usó, expiró o no coincide."""


class RateLimitError(WebAuthError):
    """Se alcanzó el límite persistente de retos para el origen."""

    def __init__(self, *, retry_at: datetime) -> None:
        super().__init__("Demasiados intentos; vuelve a probar más tarde")
        self.retry_at = retry_at


class InvalidSessionError(WebAuthError):
    """La sesión no existe, expiró, fue revocada o perdió autorización."""


class CsrfError(WebAuthError):
    """El token CSRF no corresponde a la sesión."""


class WebAuthChallenge(Base):
    """Reto passwordless; nunca persiste identificador, IP ni código en claro."""

    __tablename__ = "web_auth_challenges"
    __table_args__ = (
        CheckConstraint(
            "length(identifier_hash) = 64",
            name="ck_web_auth_challenges_identifier_hash_length",
        ),
        CheckConstraint(
            "length(ip_hash) = 64",
            name="ck_web_auth_challenges_ip_hash_length",
        ),
        CheckConstraint(
            "length(code_hash) = 64",
            name="ck_web_auth_challenges_code_hash_length",
        ),
        CheckConstraint(
            f"attempt_count BETWEEN 0 AND {CHALLENGE_ATTEMPT_LIMIT}",
            name="ck_web_auth_challenges_attempt_count",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_web_auth_challenges_expiry",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="ck_web_auth_challenges_consumed_at",
        ),
        CheckConstraint(
            "((user_id IS NULL AND membership_id IS NULL AND telegram_binding_id IS NULL) OR "
            "(user_id IS NOT NULL AND membership_id IS NOT NULL "
            "AND telegram_binding_id IS NOT NULL))",
            name="ck_web_auth_challenges_identity_state",
        ),
        Index(
            "ix_web_auth_challenges_rate_limit",
            "workspace_id",
            "identifier_hash",
            "ip_hash",
            "created_at",
        ),
        Index(
            "ix_web_auth_challenges_identifier_rate",
            "workspace_id",
            "identifier_hash",
            "created_at",
        ),
        Index(
            "ix_web_auth_challenges_ip_rate",
            "workspace_id",
            "ip_hash",
            "created_at",
        ),
        Index("ix_web_auth_challenges_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    identifier_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_web_auth_challenges_user"),
    )
    membership_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "memberships.id",
            ondelete="CASCADE",
            name="fk_web_auth_challenges_membership",
        ),
    )
    telegram_binding_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "telegram_bindings.id",
            ondelete="CASCADE",
            name="fk_web_auth_challenges_telegram_binding",
        ),
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class WebAuthSession(Base):
    """Sesión web durable; solo conserva hashes HMAC de bearer y CSRF."""

    __tablename__ = "web_auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_web_auth_sessions_token_hash"),
        CheckConstraint(
            "length(token_hash) = 64",
            name="ck_web_auth_sessions_token_hash_length",
        ),
        CheckConstraint(
            "length(csrf_hash) = 64",
            name="ck_web_auth_sessions_csrf_hash_length",
        ),
        CheckConstraint(
            "absolute_expires_at > created_at",
            name="ck_web_auth_sessions_absolute_expiry",
        ),
        CheckConstraint(
            "last_seen_at >= created_at AND last_seen_at <= absolute_expires_at",
            name="ck_web_auth_sessions_last_seen",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_web_auth_sessions_revoked_at",
        ),
        Index("ix_web_auth_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_web_auth_sessions_expiry", "absolute_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_web_auth_sessions_user"),
        nullable=False,
    )
    membership_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "memberships.id",
            ondelete="CASCADE",
            name="fk_web_auth_sessions_membership",
        ),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


@dataclass(frozen=True, slots=True)
class IssuedWebAuthChallenge:
    """Resultado interno: la respuesta HTTP debe exponer solo id y expiración."""

    challenge_id: str
    expires_at: datetime
    code: str = field(repr=False)
    telegram_user_id: str | None = field(default=None, repr=False)
    chat_id: str | None = field(default=None, repr=False)

    @property
    def deliverable(self) -> bool:
        return self.telegram_user_id is not None and self.chat_id is not None


@dataclass(frozen=True, slots=True)
class AuthenticatedWebUser:
    user_id: str
    membership_id: str
    workspace_id: str
    email: str
    username: str | None
    display_name: str
    role: Role
    telegram_user_id: str
    chat_id: str


@dataclass(frozen=True, slots=True)
class IssuedWebSession:
    user: AuthenticatedWebUser
    absolute_expires_at: datetime
    idle_expires_at: datetime
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)

    @property
    def session_token(self) -> str:
        return self.token

    @property
    def expires_at(self) -> datetime:
        return self.absolute_expires_at


class WebAuthService:
    """Autenticación passwordless por Telegram con estado durable y fail-closed."""

    def __init__(
        self,
        store: PlatformStore,
        *,
        pepper: str | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        selected_pepper = os.environ.get(WEB_AUTH_PEPPER_ENV) if pepper is None else pepper
        if not isinstance(selected_pepper, str) or len(selected_pepper) < 32:
            raise WebAuthConfigurationError(
                f"{WEB_AUTH_PEPPER_ENV} debe contener al menos 32 caracteres"
            )
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise WebAuthConfigurationError("workspace_id no puede estar vacío")
        normalized_workspace = workspace_id.strip()
        if len(normalized_workspace) > 80:
            raise WebAuthConfigurationError("workspace_id supera 80 caracteres")
        self.store = store
        self.workspace_id = normalized_workspace
        self._pepper = selected_pepper.encode("utf-8")
        self._now = now or (lambda: datetime.now(UTC))

    def request_challenge(
        self,
        identifier: str,
        ip_address: str,
        *,
        now: datetime | None = None,
    ) -> IssuedWebAuthChallenge:
        timestamp = self._normalize_now(now)
        normalized_identifier, can_resolve = _normalize_identifier(identifier)
        normalized_ip = _normalize_ip(ip_address)
        identifier_hash = self._digest("identifier", normalized_identifier)
        ip_hash = self._digest("ip", normalized_ip)
        shard = _RATE_LOCKS[_lock_shard(f"{self.workspace_id}\0{ip_hash}")]

        with shard, self.store.session() as session, session.begin():
            _database_advisory_lock(session, "web-auth-rate-ip", ip_hash)
            _database_advisory_lock(session, "web-auth-rate-identifier", identifier_hash)
            self._cleanup_expired_records(session, timestamp)
            cutoff = timestamp - CHALLENGE_RATE_WINDOW
            recent_pair = list(
                session.scalars(
                    select(WebAuthChallenge)
                    .where(
                        WebAuthChallenge.workspace_id == self.workspace_id,
                        WebAuthChallenge.identifier_hash == identifier_hash,
                        WebAuthChallenge.ip_hash == ip_hash,
                        WebAuthChallenge.created_at > cutoff,
                    )
                    .order_by(WebAuthChallenge.created_at)
                    .with_for_update()
                )
            )
            recent_identifier = list(
                session.scalars(
                    select(WebAuthChallenge)
                    .where(
                        WebAuthChallenge.workspace_id == self.workspace_id,
                        WebAuthChallenge.identifier_hash == identifier_hash,
                        WebAuthChallenge.created_at > cutoff,
                    )
                    .order_by(WebAuthChallenge.created_at)
                    .with_for_update()
                )
            )
            recent_ip = list(
                session.scalars(
                    select(WebAuthChallenge)
                    .where(
                        WebAuthChallenge.workspace_id == self.workspace_id,
                        WebAuthChallenge.ip_hash == ip_hash,
                        WebAuthChallenge.created_at > cutoff,
                    )
                    .order_by(WebAuthChallenge.created_at)
                    .with_for_update()
                )
            )
            _enforce_rate_limit(recent_pair, CHALLENGE_RATE_LIMIT)
            _enforce_rate_limit(recent_identifier, CHALLENGE_IDENTIFIER_RATE_LIMIT)
            _enforce_rate_limit(recent_ip, CHALLENGE_IP_RATE_LIMIT)

            identity = (
                self._eligible_identity(session, normalized_identifier) if can_resolve else None
            )
            challenge_id = _new_id()
            code = f"{secrets.randbelow(100_000_000):08d}"
            expires_at = timestamp + CHALLENGE_LIFETIME
            challenge = WebAuthChallenge(
                id=challenge_id,
                workspace_id=self.workspace_id,
                identifier_hash=identifier_hash,
                ip_hash=ip_hash,
                code_hash=self._digest("challenge-code", f"{challenge_id}\0{code}"),
                user_id=identity.user.id if identity is not None else None,
                membership_id=identity.membership.id if identity is not None else None,
                telegram_binding_id=identity.binding.id if identity is not None else None,
                attempt_count=0,
                created_at=timestamp,
                expires_at=expires_at,
            )
            session.add(challenge)
            session.flush()
            return IssuedWebAuthChallenge(
                challenge_id=challenge_id,
                expires_at=expires_at,
                code=code,
                telegram_user_id=(
                    identity.binding.telegram_user_id if identity is not None else None
                ),
                chat_id=identity.binding.chat_id if identity is not None else None,
            )

    issue_challenge = request_challenge

    def seal_challenge_id(self, challenge_id: str) -> str:
        """Autentica el identificador que se devolverá al navegador."""

        normalized_id = _normalize_challenge_id(challenge_id)
        signature = self._digest("challenge-state", normalized_id)
        return f"{normalized_id}.{signature}"

    def open_challenge_state(self, state: str) -> str:
        """Valida un estado web sin consultar la base de datos."""

        if not isinstance(state, str) or len(state) > 140 or "." not in state:
            raise InvalidChallengeError("El código no es válido")
        challenge_id, signature = state.rsplit(".", 1)
        normalized_id = _normalize_challenge_id(challenge_id)
        expected = self._digest("challenge-state", normalized_id)
        if len(signature) != 64 or not hmac.compare_digest(signature, expected):
            raise InvalidChallengeError("El código no es válido")
        return normalized_id

    def verify_challenge(
        self,
        challenge_id: str,
        code: str,
        ip_address: str,
        *,
        now: datetime | None = None,
    ) -> IssuedWebSession:
        timestamp = self._normalize_now(now)
        normalized_id = _normalize_challenge_id(challenge_id)
        normalized_ip = _normalize_ip(ip_address)
        ip_hash = self._digest("ip", normalized_ip)
        normalized_code = code if isinstance(code, str) else ""
        failure: InvalidChallengeError | None = None
        issued: IssuedWebSession | None = None
        shard = _CHALLENGE_LOCKS[_lock_shard(normalized_id)]

        with shard, self.store.session() as session, session.begin():
            challenge = session.scalar(
                select(WebAuthChallenge)
                .where(
                    WebAuthChallenge.id == normalized_id,
                    WebAuthChallenge.workspace_id == self.workspace_id,
                )
                .with_for_update()
            )
            if challenge is None:
                failure = InvalidChallengeError("El código no es válido")
            else:
                supplied_hash = self._digest("challenge-code", f"{challenge.id}\0{normalized_code}")
                digest_matches = hmac.compare_digest(supplied_hash, challenge.code_hash)
                code_matches = bool(
                    len(normalized_code) == 8
                    and normalized_code.isascii()
                    and normalized_code.isdigit()
                    and digest_matches
                )
                ip_matches = hmac.compare_digest(ip_hash, challenge.ip_hash)
                usable = bool(
                    challenge.consumed_at is None
                    and challenge.created_at <= timestamp
                    and challenge.expires_at > timestamp
                    and challenge.attempt_count < CHALLENGE_ATTEMPT_LIMIT
                    and ip_matches
                )
                if not usable:
                    failure = InvalidChallengeError("El código no es válido")
                elif not code_matches:
                    challenge.attempt_count += 1
                    session.flush()
                    failure = InvalidChallengeError("El código no es válido")
                else:
                    challenge.consumed_at = timestamp
                    identity = self._challenge_identity(session, challenge)
                    if identity is None:
                        failure = InvalidChallengeError("El código no es válido")
                    else:
                        token, token_hash = self._new_unique_session_token(session)
                        csrf_token = secrets.token_urlsafe(32)
                        absolute_expires_at = timestamp + SESSION_ABSOLUTE_LIFETIME
                        web_session = WebAuthSession(
                            id=_new_id(),
                            workspace_id=self.workspace_id,
                            user_id=identity.user.id,
                            membership_id=identity.membership.id,
                            token_hash=token_hash,
                            csrf_hash=self._digest("csrf", csrf_token),
                            created_at=timestamp,
                            last_seen_at=timestamp,
                            absolute_expires_at=absolute_expires_at,
                        )
                        session.add(web_session)
                        session.flush()
                        user = _authenticated_user(
                            identity.user,
                            identity.membership,
                            identity.binding,
                            self.workspace_id,
                        )
                        issued = IssuedWebSession(
                            user=user,
                            absolute_expires_at=absolute_expires_at,
                            idle_expires_at=timestamp + SESSION_IDLE_LIFETIME,
                            token=token,
                            csrf_token=csrf_token,
                        )

        if failure is not None:
            raise failure
        if issued is None:  # pragma: no cover - defensa ante cambios futuros
            raise InvalidChallengeError("El código no es válido")
        return issued

    verify_code = verify_challenge

    def cancel_challenge(
        self,
        challenge_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Invalida un OTP cuya entrega Telegram falló o quedó ambigua."""

        timestamp = self._normalize_now(now)
        normalized_id = _normalize_challenge_id(challenge_id)
        shard = _CHALLENGE_LOCKS[_lock_shard(normalized_id)]
        with shard, self.store.session() as session, session.begin():
            challenge = session.scalar(
                select(WebAuthChallenge)
                .where(
                    WebAuthChallenge.id == normalized_id,
                    WebAuthChallenge.workspace_id == self.workspace_id,
                )
                .with_for_update()
            )
            if challenge is None:
                return False
            if challenge.consumed_at is None:
                challenge.consumed_at = max(timestamp, challenge.created_at)
                session.flush()
            return True

    mark_delivery_failed = cancel_challenge

    def authenticate(
        self,
        session_token: str,
        *,
        now: datetime | None = None,
    ) -> AuthenticatedWebUser:
        return self._authenticate(session_token, csrf_token=None, require_csrf=False, now=now)

    authenticate_session = authenticate

    def verify_csrf(
        self,
        session_token: str,
        csrf_token: str,
        *,
        now: datetime | None = None,
    ) -> AuthenticatedWebUser:
        return self._authenticate(
            session_token,
            csrf_token=csrf_token,
            require_csrf=True,
            now=now,
        )

    validate_csrf = verify_csrf

    def logout(
        self,
        session_token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        timestamp = self._normalize_now(now)
        token_hash = self._digest("session", _normalize_secret(session_token))
        with self.store.session() as session, session.begin():
            web_session = session.scalar(
                select(WebAuthSession)
                .where(
                    WebAuthSession.workspace_id == self.workspace_id,
                    WebAuthSession.token_hash == token_hash,
                )
                .with_for_update()
            )
            if web_session is None:
                return False
            if web_session.revoked_at is None:
                web_session.revoked_at = max(timestamp, web_session.created_at)
                session.flush()
            return True

    revoke_session = logout

    def _authenticate(
        self,
        session_token: str,
        *,
        csrf_token: str | None,
        require_csrf: bool,
        now: datetime | None,
    ) -> AuthenticatedWebUser:
        timestamp = self._normalize_now(now)
        token_hash = self._digest("session", _normalize_secret(session_token))
        failure: WebAuthError | None = None
        authenticated: AuthenticatedWebUser | None = None

        with self.store.session() as session, session.begin():
            web_session = session.scalar(
                select(WebAuthSession)
                .where(
                    WebAuthSession.workspace_id == self.workspace_id,
                    WebAuthSession.token_hash == token_hash,
                )
                .with_for_update()
            )
            if web_session is None or web_session.revoked_at is not None:
                failure = InvalidSessionError("La sesión no es válida")
            elif (
                timestamp < web_session.created_at
                or web_session.absolute_expires_at <= timestamp
                or web_session.last_seen_at + SESSION_IDLE_LIFETIME <= timestamp
            ):
                web_session.revoked_at = max(timestamp, web_session.created_at)
                session.flush()
                failure = InvalidSessionError("La sesión no es válida")
            elif require_csrf and not self._csrf_matches(web_session, csrf_token):
                failure = CsrfError("La solicitud no tiene un token CSRF válido")
            else:
                identity = self._session_identity(session, web_session)
                if identity is None:
                    web_session.revoked_at = max(timestamp, web_session.created_at)
                    session.flush()
                    failure = InvalidSessionError("La sesión no es válida")
                else:
                    web_session.last_seen_at = max(web_session.last_seen_at, timestamp)
                    session.flush()
                    authenticated = _authenticated_user(
                        identity.user,
                        identity.membership,
                        identity.binding,
                        self.workspace_id,
                    )

        if failure is not None:
            raise failure
        if authenticated is None:  # pragma: no cover - defensa ante cambios futuros
            raise InvalidSessionError("La sesión no es válida")
        return authenticated

    def _eligible_identity(self, session: Session, identifier: str) -> _Identity | None:
        row = session.execute(
            select(User, Membership, TelegramBinding)
            .join(
                Membership,
                (Membership.user_id == User.id) & (Membership.workspace_id == self.workspace_id),
            )
            .join(
                TelegramBinding,
                (TelegramBinding.user_id == User.id)
                & (TelegramBinding.workspace_id == self.workspace_id),
            )
            .where(
                or_(User.email == identifier, User.username == identifier),
                User.is_active.is_(True),
                ~User.email.like("%.internal"),
                Membership.role.in_(_ROLE_VALUES),
                TelegramBinding.is_active.is_(True),
                TelegramBinding.purpose == "control",
                TelegramBinding.chat_id == TelegramBinding.telegram_user_id,
                ~TelegramBinding.chat_id.like("-%"),
            )
            .order_by(TelegramBinding.created_at, TelegramBinding.id)
            .limit(1)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return _Identity(user=row[0], membership=row[1], binding=row[2])

    def _challenge_identity(
        self,
        session: Session,
        challenge: WebAuthChallenge,
    ) -> _Identity | None:
        if (
            not challenge.user_id
            or not challenge.membership_id
            or not challenge.telegram_binding_id
        ):
            return None
        row = session.execute(
            select(User, Membership, TelegramBinding)
            .where(
                User.id == challenge.user_id,
                User.is_active.is_(True),
                ~User.email.like("%.internal"),
                Membership.id == challenge.membership_id,
                Membership.user_id == User.id,
                Membership.workspace_id == self.workspace_id,
                Membership.role.in_(_ROLE_VALUES),
                TelegramBinding.id == challenge.telegram_binding_id,
                TelegramBinding.user_id == User.id,
                TelegramBinding.workspace_id == self.workspace_id,
                TelegramBinding.is_active.is_(True),
                TelegramBinding.purpose == "control",
                TelegramBinding.chat_id == TelegramBinding.telegram_user_id,
                ~TelegramBinding.chat_id.like("-%"),
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return _Identity(user=row[0], membership=row[1], binding=row[2])

    def _session_identity(
        self,
        session: Session,
        web_session: WebAuthSession,
    ) -> _Identity | None:
        row = session.execute(
            select(User, Membership)
            .where(
                User.id == web_session.user_id,
                User.is_active.is_(True),
                ~User.email.like("%.internal"),
                Membership.id == web_session.membership_id,
                Membership.user_id == User.id,
                Membership.workspace_id == web_session.workspace_id,
                Membership.role.in_(_ROLE_VALUES),
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        binding = session.scalar(
            select(TelegramBinding)
            .where(
                TelegramBinding.user_id == row[0].id,
                TelegramBinding.workspace_id == web_session.workspace_id,
                TelegramBinding.is_active.is_(True),
                TelegramBinding.purpose == "control",
                TelegramBinding.chat_id == TelegramBinding.telegram_user_id,
                ~TelegramBinding.chat_id.like("-%"),
            )
            .order_by(TelegramBinding.created_at, TelegramBinding.id)
            .limit(1)
            .with_for_update()
        )
        if binding is None:
            return None
        return _Identity(user=row[0], membership=row[1], binding=binding)

    def _new_unique_session_token(self, session: Session) -> tuple[str, str]:
        for _ in range(4):
            token = secrets.token_urlsafe(32)
            token_hash = self._digest("session", token)
            existing = session.scalar(
                select(WebAuthSession.id).where(WebAuthSession.token_hash == token_hash)
            )
            if existing is None:
                return token, token_hash
        raise WebAuthError("No fue posible crear una sesión única")

    def _csrf_matches(self, web_session: WebAuthSession, csrf_token: str | None) -> bool:
        supplied_hash = self._digest("csrf", _normalize_secret(csrf_token))
        return hmac.compare_digest(supplied_hash, web_session.csrf_hash)

    @staticmethod
    def _cleanup_expired_records(session: Session, timestamp: datetime) -> None:
        """Mantiene acotadas las tablas efímeras sin tocar estado todavía útil."""

        expired_challenges = (
            select(WebAuthChallenge.id)
            .where(WebAuthChallenge.expires_at < timestamp - timedelta(days=1))
            .order_by(WebAuthChallenge.expires_at, WebAuthChallenge.id)
            .limit(500)
        )
        session.execute(
            delete(WebAuthChallenge)
            .where(WebAuthChallenge.id.in_(expired_challenges))
            .execution_options(synchronize_session=False)
        )
        expired_sessions = (
            select(WebAuthSession.id)
            .where(WebAuthSession.absolute_expires_at < timestamp - timedelta(days=7))
            .order_by(WebAuthSession.absolute_expires_at, WebAuthSession.id)
            .limit(500)
        )
        session.execute(
            delete(WebAuthSession)
            .where(WebAuthSession.id.in_(expired_sessions))
            .execution_options(synchronize_session=False)
        )

    def _digest(self, domain: str, value: str) -> str:
        payload = f"colmat-web-auth-v1\0{domain}\0{value}".encode()
        return hmac.new(self._pepper, payload, hashlib.sha256).hexdigest()

    def _normalize_now(self, value: datetime | None) -> datetime:
        selected = self._now() if value is None else value
        if selected.tzinfo is None or selected.utcoffset() is None:
            raise ValueError("now debe incluir zona horaria")
        return selected.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _Identity:
    user: User
    membership: Membership
    binding: TelegramBinding


def _enforce_rate_limit(rows: list[WebAuthChallenge], limit: int) -> None:
    if len(rows) >= limit:
        raise RateLimitError(retry_at=rows[0].created_at + CHALLENGE_RATE_WINDOW)


def _authenticated_user(
    user: User,
    membership: Membership,
    binding: TelegramBinding,
    workspace_id: str,
) -> AuthenticatedWebUser:
    return AuthenticatedWebUser(
        user_id=user.id,
        membership_id=membership.id,
        workspace_id=workspace_id,
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        role=Role(membership.role),
        telegram_user_id=binding.telegram_user_id,
        chat_id=binding.chat_id,
    )


def _normalize_identifier(value: str) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "<invalid>", False
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    if not normalized:
        return "<empty>", False
    if len(normalized) > 320 or "\x00" in normalized:
        return normalized, False
    return normalized, True


def _normalize_ip(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ip_address no puede estar vacío")
    normalized = value.strip().casefold()
    if len(normalized) > 255 or "\x00" in normalized:
        raise ValueError("ip_address no es válido")
    try:
        return ipaddress.ip_address(normalized).compressed
    except ValueError:
        # Permite identificadores de origen de proxies confiables en pruebas/desarrollo,
        # pero los normaliza de manera estable para que no eludan la cuota persistente.
        return normalized


def _normalize_challenge_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        return "<invalid>"
    return value.strip()


def _normalize_secret(value: str | None) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        return "<invalid>"
    return value


def _new_id() -> str:
    # UUID4 no es una credencial; los secretos de sesión usan 32 bytes aleatorios aparte.
    return str(uuid4())


def _lock_shard(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % _LOCK_SHARDS


def _database_advisory_lock(session: Session, domain: str, value: str) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"{domain}\0{value}".encode()).digest()
    lock_key = int.from_bytes(digest[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


__all__ = [
    "AuthenticatedWebUser",
    "CHALLENGE_ATTEMPT_LIMIT",
    "CHALLENGE_IDENTIFIER_RATE_LIMIT",
    "CHALLENGE_IP_RATE_LIMIT",
    "CHALLENGE_LIFETIME",
    "CHALLENGE_RATE_LIMIT",
    "CHALLENGE_RATE_WINDOW",
    "CsrfError",
    "InvalidChallengeError",
    "InvalidSessionError",
    "IssuedWebAuthChallenge",
    "IssuedWebSession",
    "RateLimitError",
    "SESSION_ABSOLUTE_LIFETIME",
    "SESSION_IDLE_LIFETIME",
    "WEB_AUTH_PEPPER_ENV",
    "WebAuthChallenge",
    "WebAuthConfigurationError",
    "WebAuthError",
    "WebAuthService",
    "WebAuthSession",
]
