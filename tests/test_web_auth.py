from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from colmat_x.platform_store import PlatformStore, TelegramBinding, User
from colmat_x.rbac import Role
from colmat_x.web_auth import (
    CHALLENGE_IDENTIFIER_RATE_LIMIT,
    CHALLENGE_IP_RATE_LIMIT,
    CHALLENGE_LIFETIME,
    CHALLENGE_RATE_WINDOW,
    SESSION_ABSOLUTE_LIFETIME,
    SESSION_IDLE_LIFETIME,
    CsrfError,
    InvalidChallengeError,
    InvalidSessionError,
    RateLimitError,
    WebAuthChallenge,
    WebAuthConfigurationError,
    WebAuthService,
    WebAuthSession,
)

NOW = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
PEPPER = "web-auth-test-pepper-with-at-least-32-characters"


@pytest.fixture
def store() -> PlatformStore:
    selected = PlatformStore("sqlite+pysqlite:///:memory:")
    try:
        yield selected
    finally:
        selected.close()


def bootstrap_eligible_user(
    store: PlatformStore,
    *,
    email: str = "owner@colmat.test",
    username: str = "sirhegel",
    purpose: str = "control",
):
    owner, membership = store.bootstrap_owner(
        email=email,
        username=username,
        display_name="SirHegel",
        now=NOW,
    )
    binding = store.bind_telegram_chat(
        7084929277,
        telegram_user_id=7084929277,
        actor_id=owner.id,
        user_id=owner.id,
        purpose=purpose,
        now=NOW,
    )
    return owner, membership, binding


def issued_session(
    store: PlatformStore,
    *,
    issued_at: datetime = NOW,
    identifier: str = "SirHegel",
):
    service = WebAuthService(store, pepper=PEPPER)
    challenge = service.request_challenge(identifier, "203.0.113.9", now=issued_at)
    result = service.verify_challenge(
        challenge.challenge_id,
        challenge.code,
        "203.0.113.9",
        now=issued_at + timedelta(seconds=1),
    )
    return service, challenge, result


def test_pepper_is_required_and_must_have_32_characters(store, monkeypatch) -> None:
    monkeypatch.delenv("WEB_AUTH_PEPPER", raising=False)

    with pytest.raises(WebAuthConfigurationError):
        WebAuthService(store)
    with pytest.raises(WebAuthConfigurationError):
        WebAuthService(store, pepper="too-short")

    monkeypatch.setenv("WEB_AUTH_PEPPER", PEPPER)
    assert WebAuthService(store).workspace_id == "colmat"


def test_known_and_unknown_challenges_have_generic_public_shape_and_only_hashes(store) -> None:
    bootstrap_eligible_user(store)
    service = WebAuthService(store, pepper=PEPPER)

    known = service.request_challenge(" @SirHegel ", "2001:0DB8::1", now=NOW)
    unknown = service.request_challenge("nobody@colmat.test", "2001:db8::1", now=NOW)

    assert known.expires_at == unknown.expires_at == NOW + CHALLENGE_LIFETIME
    assert re.fullmatch(r"\d{8}", known.code)
    assert re.fullmatch(r"\d{8}", unknown.code)
    assert known.deliverable is True
    assert (known.telegram_user_id, known.chat_id) == ("7084929277", "7084929277")
    assert unknown.deliverable is False
    assert (unknown.telegram_user_id, unknown.chat_id) == (None, None)
    assert known.code not in repr(known)
    assert unknown.code not in repr(unknown)

    with store.session() as session:
        rows = list(session.scalars(select(WebAuthChallenge).order_by(WebAuthChallenge.id)))
    assert len(rows) == 2
    for row in rows:
        assert len(row.identifier_hash) == len(row.ip_hash) == len(row.code_hash) == 64
        persisted = " ".join(
            (row.identifier_hash, row.ip_hash, row.code_hash, row.id, row.workspace_id)
        )
        assert "sirhegel" not in persisted
        assert "nobody" not in persisted
        assert known.code not in persisted
        assert unknown.code not in persisted


def test_unknown_internal_inactive_or_non_control_accounts_are_not_deliverable(store) -> None:
    owner, _, binding = bootstrap_eligible_user(store, email="service@colmat.internal")
    service = WebAuthService(store, pepper=PEPPER)

    internal = service.request_challenge("sirhegel", "198.51.100.1", now=NOW)
    assert internal.deliverable is False
    with pytest.raises(InvalidChallengeError):
        service.verify_challenge(
            internal.challenge_id,
            internal.code,
            "198.51.100.1",
            now=NOW + timedelta(seconds=1),
        )

    with store.session() as session, session.begin():
        session.get(User, owner.id).email = "owner@colmat.test"
        session.get(User, owner.id).is_active = False
    inactive = service.request_challenge("sirhegel", "198.51.100.2", now=NOW)
    assert inactive.deliverable is False

    with store.session() as session, session.begin():
        session.get(User, owner.id).is_active = True
        session.get(TelegramBinding, binding.id).purpose = "review"
    non_control = service.request_challenge("sirhegel", "198.51.100.3", now=NOW)
    assert non_control.deliverable is False


def test_group_control_binding_cannot_receive_web_login_otp(store) -> None:
    owner, _, _binding = bootstrap_eligible_user(store)
    with store.session() as session, session.begin():
        binding = session.scalar(select(TelegramBinding).where(TelegramBinding.user_id == owner.id))
        binding.chat_id = "-1001234567890"
    service = WebAuthService(store, pepper=PEPPER)

    challenge = service.request_challenge("sirhegel", "198.51.100.4", now=NOW)

    assert challenge.deliverable is False
    with pytest.raises(InvalidChallengeError):
        service.verify_challenge(
            challenge.challenge_id,
            challenge.code,
            "198.51.100.4",
            now=NOW + timedelta(seconds=1),
        )


def test_challenge_is_bound_to_ip_expires_and_can_be_cancelled(store) -> None:
    bootstrap_eligible_user(store)
    service = WebAuthService(store, pepper=PEPPER)
    wrong_ip = service.request_challenge("sirhegel", "203.0.113.10", now=NOW)

    with pytest.raises(InvalidChallengeError):
        service.verify_challenge(
            wrong_ip.challenge_id,
            wrong_ip.code,
            "203.0.113.11",
            now=NOW + timedelta(seconds=1),
        )

    expired = service.request_challenge("sirhegel", "203.0.113.12", now=NOW)
    with pytest.raises(InvalidChallengeError):
        service.verify_challenge(
            expired.challenge_id,
            expired.code,
            "203.0.113.12",
            now=NOW + CHALLENGE_LIFETIME,
        )

    cancelled = service.request_challenge("sirhegel", "203.0.113.13", now=NOW)
    assert service.cancel_challenge(cancelled.challenge_id, now=NOW + timedelta(seconds=1))
    assert service.cancel_challenge(cancelled.challenge_id, now=NOW + timedelta(seconds=2))
    with pytest.raises(InvalidChallengeError):
        service.verify_challenge(
            cancelled.challenge_id,
            cancelled.code,
            "203.0.113.13",
            now=NOW + timedelta(seconds=3),
        )


def test_browser_challenge_state_is_authenticated_before_database_lookup(store) -> None:
    bootstrap_eligible_user(store)
    service = WebAuthService(store, pepper=PEPPER)
    challenge = service.request_challenge("sirhegel", "203.0.113.14", now=NOW)
    state = service.seal_challenge_id(challenge.challenge_id)

    assert service.open_challenge_state(state) == challenge.challenge_id
    with pytest.raises(InvalidChallengeError):
        service.open_challenge_state(f"{challenge.challenge_id}.{'0' * 64}")
    with pytest.raises(InvalidChallengeError):
        service.open_challenge_state("inventado")


def test_five_wrong_attempts_are_persisted_and_lock_the_challenge(store) -> None:
    bootstrap_eligible_user(store)
    service = WebAuthService(store, pepper=PEPPER)
    challenge = service.request_challenge("sirhegel", "203.0.113.20", now=NOW)
    wrong_code = "00000000" if challenge.code != "00000000" else "00000001"

    for attempt in range(5):
        with pytest.raises(InvalidChallengeError):
            service.verify_challenge(
                challenge.challenge_id,
                wrong_code,
                "203.0.113.20",
                now=NOW + timedelta(seconds=attempt + 1),
            )

    with store.session() as session:
        assert session.get(WebAuthChallenge, challenge.challenge_id).attempt_count == 5
    with pytest.raises(InvalidChallengeError):
        service.verify_challenge(
            challenge.challenge_id,
            challenge.code,
            "203.0.113.20",
            now=NOW + timedelta(seconds=10),
        )


def test_rate_limit_is_persistent_per_identifier_and_ip_pair(store) -> None:
    bootstrap_eligible_user(store)
    service = WebAuthService(store, pepper=PEPPER)

    for offset in range(5):
        service.request_challenge(
            "sirhegel",
            "203.0.113.30",
            now=NOW + timedelta(seconds=offset),
        )

    recreated_service = WebAuthService(store, pepper=PEPPER)
    with pytest.raises(RateLimitError) as error:
        recreated_service.request_challenge(
            "@SIRHEGEL",
            "203.0.113.30",
            now=NOW + timedelta(minutes=1),
        )
    assert error.value.retry_at == NOW + CHALLENGE_RATE_WINDOW

    assert recreated_service.request_challenge(
        "sirhegel", "203.0.113.31", now=NOW + timedelta(minutes=1)
    )
    assert recreated_service.request_challenge(
        "sirhegel",
        "203.0.113.30",
        now=NOW + CHALLENGE_RATE_WINDOW,
    )


def test_rate_limits_random_identifiers_per_ip_and_one_identifier_across_ips(store) -> None:
    bootstrap_eligible_user(store)
    service = WebAuthService(store, pepper=PEPPER)

    for offset in range(CHALLENGE_IP_RATE_LIMIT):
        service.request_challenge(
            f"random-{offset}@colmat.test",
            "203.0.113.60",
            now=NOW + timedelta(seconds=offset),
        )
    with pytest.raises(RateLimitError):
        service.request_challenge(
            "one-more@colmat.test",
            "203.0.113.60",
            now=NOW + timedelta(minutes=1),
        )

    second_store = PlatformStore("sqlite+pysqlite:///:memory:")
    try:
        bootstrap_eligible_user(second_store)
        second_service = WebAuthService(second_store, pepper=PEPPER)
        for offset in range(CHALLENGE_IDENTIFIER_RATE_LIMIT):
            second_service.request_challenge(
                "sirhegel",
                f"198.51.100.{offset + 1}",
                now=NOW + timedelta(seconds=offset),
            )
        with pytest.raises(RateLimitError):
            second_service.request_challenge(
                "@SIRHEGEL",
                "198.51.100.200",
                now=NOW + timedelta(minutes=1),
            )
    finally:
        second_store.close()


def test_expired_auth_records_are_garbage_collected(store) -> None:
    bootstrap_eligible_user(store)
    service = WebAuthService(store, pepper=PEPPER)
    expired = service.request_challenge("sirhegel", "203.0.113.70", now=NOW)

    service.request_challenge(
        "sirhegel",
        "203.0.113.71",
        now=NOW + timedelta(days=2),
    )

    with store.session() as session:
        assert session.get(WebAuthChallenge, expired.challenge_id) is None


def test_success_consumes_once_and_persists_only_token_and_csrf_hashes(store) -> None:
    owner, membership, _ = bootstrap_eligible_user(store)
    service, challenge, issued = issued_session(store)

    assert issued.user.user_id == owner.id
    assert issued.user.membership_id == membership.id
    assert issued.user.role is Role.OWNER
    assert issued.user.telegram_user_id == issued.user.chat_id == "7084929277"
    assert issued.absolute_expires_at == NOW + timedelta(seconds=1) + SESSION_ABSOLUTE_LIFETIME
    assert issued.idle_expires_at == NOW + timedelta(seconds=1) + SESSION_IDLE_LIFETIME
    assert issued.token not in repr(issued)
    assert issued.csrf_token not in repr(issued)

    with store.session() as session:
        challenge_row = session.get(WebAuthChallenge, challenge.challenge_id)
        session_row = session.scalar(select(WebAuthSession))
    assert challenge_row.consumed_at == NOW + timedelta(seconds=1)
    assert len(session_row.token_hash) == len(session_row.csrf_hash) == 64
    assert issued.token != session_row.token_hash
    assert issued.csrf_token != session_row.csrf_hash

    with pytest.raises(InvalidChallengeError):
        service.verify_challenge(
            challenge.challenge_id,
            challenge.code,
            "203.0.113.9",
            now=NOW + timedelta(seconds=2),
        )


def test_concurrent_verification_issues_exactly_one_session(tmp_path: Path) -> None:
    database = tmp_path / "web-auth.sqlite3"
    store = PlatformStore(f"sqlite+pysqlite:///{database}")
    try:
        bootstrap_eligible_user(store)
        service = WebAuthService(store, pepper=PEPPER)
        challenge = service.request_challenge("sirhegel", "203.0.113.40", now=NOW)

        def verify():
            try:
                return service.verify_challenge(
                    challenge.challenge_id,
                    challenge.code,
                    "203.0.113.40",
                    now=NOW + timedelta(seconds=1),
                )
            except InvalidChallengeError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: verify(), range(2)))

        assert sum(result is not None for result in results) == 1
        with store.session() as session:
            assert len(list(session.scalars(select(WebAuthSession)))) == 1
    finally:
        store.close()


def test_authentication_checks_csrf_idle_absolute_expiry_and_logout(store) -> None:
    bootstrap_eligible_user(store)
    service, _, issued = issued_session(store)
    started = NOW + timedelta(seconds=1)

    authenticated = service.authenticate(issued.token, now=started + timedelta(hours=1))
    assert authenticated.user_id == issued.user.user_id
    assert (
        service.verify_csrf(
            issued.token,
            issued.csrf_token,
            now=started + timedelta(hours=1, minutes=30),
        )
        == authenticated
    )
    with pytest.raises(CsrfError):
        service.verify_csrf(
            issued.token,
            "wrong-csrf",
            now=started + timedelta(hours=1, minutes=31),
        )

    assert service.logout(issued.token, now=started + timedelta(hours=1, minutes=32))
    assert service.logout(issued.token, now=started + timedelta(hours=1, minutes=33))
    assert service.logout("unknown-token", now=started) is False
    with pytest.raises(InvalidSessionError):
        service.authenticate(issued.token, now=started + timedelta(hours=1, minutes=34))

    _, _, idle = issued_session(store, issued_at=NOW + timedelta(hours=2))
    with pytest.raises(InvalidSessionError):
        service.authenticate(
            idle.token,
            now=NOW + timedelta(hours=2, seconds=1) + SESSION_IDLE_LIFETIME,
        )

    _, _, absolute = issued_session(store, issued_at=NOW + timedelta(hours=5))
    with pytest.raises(InvalidSessionError):
        service.authenticate(
            absolute.token,
            now=NOW + timedelta(hours=5, seconds=1) + SESSION_ABSOLUTE_LIFETIME,
        )


def test_each_authentication_uses_current_role_user_membership_and_control_binding(store) -> None:
    owner, _, binding = bootstrap_eligible_user(store)
    editor = store.create_user(
        actor_id=owner.id,
        email="editor@colmat.test",
        username="editora",
        display_name="Editora",
        now=NOW,
    )
    store.grant_membership(editor.id, Role.EDITOR, actor_id=owner.id, now=NOW)
    editor_binding = store.bind_telegram_chat(
        111,
        telegram_user_id=111,
        actor_id=owner.id,
        user_id=editor.id,
        purpose="control",
        now=NOW,
    )
    service, _, issued = issued_session(store, identifier="editora")

    store.change_membership_role(
        editor.id,
        Role.REVIEWER,
        actor_id=owner.id,
        now=NOW + timedelta(minutes=1),
    )
    current = service.authenticate(issued.token, now=NOW + timedelta(minutes=2))
    assert current.role is Role.REVIEWER
    assert (current.telegram_user_id, current.chat_id) == ("111", "111")

    with store.session() as session, session.begin():
        session.get(TelegramBinding, editor_binding.id).is_active = False
    with pytest.raises(InvalidSessionError):
        service.authenticate(issued.token, now=NOW + timedelta(minutes=3))

    # La vinculación del owner no autoriza por accidente la sesión de otro usuario.
    with store.session() as session:
        assert session.get(TelegramBinding, binding.id).is_active is True


def test_membership_revocation_and_user_deactivation_invalidate_sessions(store) -> None:
    owner, _, _ = bootstrap_eligible_user(store)
    editor = store.create_user(
        actor_id=owner.id,
        email="writer@colmat.test",
        username="writer",
        display_name="Writer",
        now=NOW,
    )
    store.grant_membership(editor.id, Role.EDITOR, actor_id=owner.id, now=NOW)
    store.bind_telegram_chat(
        333,
        telegram_user_id=333,
        actor_id=owner.id,
        user_id=editor.id,
        now=NOW,
    )
    service, _, revoked = issued_session(store, identifier="writer")
    store.revoke_membership(editor.id, actor_id=owner.id, now=NOW + timedelta(minutes=1))
    with pytest.raises(InvalidSessionError):
        service.authenticate(revoked.token, now=NOW + timedelta(minutes=2))

    second = store.create_user(
        actor_id=owner.id,
        email="second@colmat.test",
        username="second",
        display_name="Second",
        now=NOW,
    )
    store.grant_membership(second.id, Role.EDITOR, actor_id=owner.id, now=NOW)
    store.bind_telegram_chat(
        444,
        telegram_user_id=444,
        actor_id=owner.id,
        user_id=second.id,
        now=NOW,
    )
    _, _, deactivated = issued_session(store, identifier="second")
    store.set_user_active(
        second.id,
        active=False,
        actor_id=owner.id,
        now=NOW + timedelta(minutes=3),
    )
    with pytest.raises(InvalidSessionError):
        service.authenticate(deactivated.token, now=NOW + timedelta(minutes=4))


def test_postgres_ddl_declares_idempotent_tables_constraints_fks_and_indexes() -> None:
    ddl = (Path(__file__).parents[1] / "deploy" / "postgres.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS web_auth_challenges" in ddl
    assert "CREATE TABLE IF NOT EXISTS web_auth_sessions" in ddl
    assert "fk_web_auth_challenges_telegram_binding" in ddl
    assert "fk_web_auth_sessions_membership" in ddl
    assert "uq_web_auth_sessions_token_hash" in ddl
    assert "ix_web_auth_challenges_rate_limit" in ddl
    assert "ix_web_auth_sessions_user_active" in ddl
    assert "WHERE conname = 'ck_web_auth_challenges_attempt_count'" in ddl


def test_schema_rejects_plain_or_malformed_hashes(store) -> None:
    owner, membership, binding = bootstrap_eligible_user(store)
    with pytest.raises(IntegrityError), store.session() as session, session.begin():
        session.add(
            WebAuthChallenge(
                id="bad-challenge",
                workspace_id="colmat",
                identifier_hash="raw-identifier",
                ip_hash="raw-ip",
                code_hash="12345678",
                user_id=owner.id,
                membership_id=membership.id,
                telegram_binding_id=binding.id,
                attempt_count=0,
                created_at=NOW,
                expires_at=NOW + CHALLENGE_LIFETIME,
            )
        )
