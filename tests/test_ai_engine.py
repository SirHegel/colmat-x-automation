from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from colmat_x.ai_engine import AICredentialError, AIEngineService, normalize_api_key
from colmat_x.credential_cipher import (
    PRIVATE_KEY_ENV,
    PUBLIC_KEY_ENV,
    CredentialCipher,
    CredentialCipherUnavailable,
)
from colmat_x.editorial import DRAFT_TOOL_NAME, load_editorial_policy
from colmat_x.platform_store import (
    AIProviderCredential,
    AuditEvent,
    ConflictError,
    PlatformStore,
)
from colmat_x.providers import OPENAI_MODELS_ENDPOINT, ProviderConfigurationError
from colmat_x.rbac import AuthorizationError, Role

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
SECRET = "sk-test-openai-secret-value-0001"
POLICY = load_editorial_policy("config/editorial-policy.yaml")


class FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self):
        return self._payload


class FakeHttp:
    """Transporte simulado: registra cabeceras y responde con un estado fijo."""

    def __init__(self, status_code: int = 200, payload=None) -> None:
        self.status_code = status_code
        self.payload = payload
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return FakeResponse(self.status_code, self.payload)

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return FakeResponse(self.status_code, self.payload)


@pytest.fixture
def store() -> PlatformStore:
    selected = PlatformStore("sqlite+pysqlite:///:memory:")
    try:
        yield selected
    finally:
        selected.close()


@pytest.fixture
def team(store: PlatformStore):
    owner, _membership = store.bootstrap_owner(
        email="owner@colmat.test", display_name="Owner", username="owner", now=NOW
    )
    members = {}
    for role in (Role.ADMIN, Role.EDITOR, Role.SCHEDULER):
        user = store.create_user(
            actor_id=owner.id,
            email=f"{role.value}@colmat.test",
            display_name=role.value.title(),
            now=NOW,
        )
        store.grant_membership(user.id, role, actor_id=owner.id, now=NOW)
        members[role] = user
    return owner, members


@pytest.fixture
def keypair() -> tuple[str, str]:
    return CredentialCipher.generate_keypair()


def sealer_service(
    store: PlatformStore, keypair, *, http: FakeHttp | None = None
) -> AIEngineService:
    """Proceso web: solo clave pública, nunca puede abrir sobres."""

    _private, public = keypair
    return AIEngineService(
        store,
        cipher=CredentialCipher.from_environment({PUBLIC_KEY_ENV: public}),
        environ={},
        http_session=http or FakeHttp(200, {"data": []}),
        clock=lambda: NOW,
    )


def worker_service(
    store: PlatformStore,
    keypair,
    *,
    environ: dict[str, str] | None = None,
    http: FakeHttp | None = None,
) -> AIEngineService:
    private, _public = keypair
    return AIEngineService(
        store,
        cipher=CredentialCipher.from_environment({PRIVATE_KEY_ENV: private}),
        environ=environ or {},
        http_session=http or FakeHttp(200, {"data": []}),
        clock=lambda: NOW + timedelta(minutes=5),
    )


def audit_actions(store: PlatformStore, actor_id: str) -> list[str]:
    return [event.action for event in store.list_audit_events(actor_id=actor_id, limit=200)]


def all_persisted_text(store: PlatformStore) -> str:
    with store.session() as session:
        rows = session.scalars(select(AIProviderCredential)).all()
        events = session.scalars(select(AuditEvent)).all()
    return json.dumps(
        [
            *(
                {
                    column.name: getattr(row, column.name)
                    for column in AIProviderCredential.__table__.columns
                }
                for row in rows
            ),
            *({"action": event.action, "detail": event.detail} for event in events),
        ],
        default=str,
    )


# --- selección de motor ------------------------------------------------------------------


def test_engine_defaults_to_minimax_with_cas_rbac_and_audit(store, team) -> None:
    owner, members = team
    defaults = store.get_ai_engine_settings(actor_id=members[Role.EDITOR].id)
    assert defaults.to_mapping() | {"updated_at": None} == {
        "text_provider": "minimax",
        "text_model": "MiniMax-M2.7",
        "image_provider": "minimax",
        "image_model": "image-01",
        "version": 1,
        "updated_by": owner.id,
        "updated_at": None,
    }
    assert "ai_engine.settings_initialized" in audit_actions(store, owner.id)

    for role in (Role.EDITOR, Role.SCHEDULER):
        with pytest.raises(AuthorizationError, match="no tiene el permiso"):
            store.update_ai_engine_settings(
                actor_id=members[role].id, expected_version=1, text_provider="anthropic"
            )

    updated = store.update_ai_engine_settings(
        actor_id=members[Role.ADMIN].id,
        expected_version=1,
        text_provider="anthropic",
        image_provider="openai",
        now=NOW,
    )
    assert (updated.text_provider, updated.text_model) == ("anthropic", "claude-opus-5")
    assert (updated.image_provider, updated.image_model) == ("openai", "gpt-image-1")
    assert updated.version == 2

    with pytest.raises(ConflictError, match="versión actual 2"):
        store.update_ai_engine_settings(
            actor_id=owner.id, expected_version=1, text_model="claude-sonnet-5"
        )
    with pytest.raises(ValueError, match="modelo de texto"):
        store.update_ai_engine_settings(actor_id=owner.id, expected_version=2, text_model="gpt-5")
    with pytest.raises(ValueError, match="uno de"):
        store.update_ai_engine_settings(
            actor_id=owner.id, expected_version=2, image_provider="anthropic"
        )
    with pytest.raises(ValueError, match="uno de"):
        store.update_ai_engine_settings(
            actor_id=owner.id, expected_version=2, text_provider="https://evil.invalid"
        )

    events = store.list_audit_events(actor_id=owner.id, limit=200)
    change = [event for event in events if event.action == "ai_engine.settings_updated"][-1]
    assert change.actor_id == members[Role.ADMIN].id
    assert change.detail["previous"]["text_provider"] == "minimax"
    assert change.detail["current"] == {
        "text_provider": "anthropic",
        "text_model": "claude-opus-5",
        "image_provider": "openai",
        "image_model": "gpt-image-1",
        "version": 2,
    }


# --- credenciales -------------------------------------------------------------------------


def test_credentials_are_sealed_owner_only_and_never_persisted_in_clear(
    store, team, keypair
) -> None:
    owner, members = team
    http = FakeHttp(200, {"data": []})
    service = sealer_service(store, keypair, http=http)

    result = service.store_credential(owner.id, "openai", SECRET)

    assert result.replaced is False
    assert result.check is not None and result.check.status == "valid"
    assert http.calls[0]["url"] == OPENAI_MODELS_ENDPOINT
    assert http.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    view = result.view
    assert view["last_four"] == SECRET[-4:]
    assert len(view["fingerprint_prefix"]) == 12
    assert view["last_verified_status"] == "valid"
    assert SECRET not in json.dumps(view)
    assert SECRET not in all_persisted_text(store)
    assert "ai_credential.stored" in audit_actions(store, owner.id)

    with pytest.raises(AuthorizationError, match="no tiene el permiso"):
        service.store_credential(members[Role.ADMIN].id, "openai", SECRET + "x")
    with pytest.raises(AuthorizationError, match="no tiene el permiso"):
        store.list_ai_credentials(actor_id=members[Role.EDITOR].id)
    assert store.ai_credentials_configured(actor_id=members[Role.EDITOR].id)["openai"] is True

    rotated = service.store_credential(owner.id, "openai", "sk-rotated-secret-value-0002")
    assert rotated.replaced is True
    rotation = [
        event
        for event in store.list_audit_events(actor_id=owner.id, limit=200)
        if event.action == "ai_credential.rotated"
    ][-1]
    assert rotation.detail["previous_fingerprint_prefix"] == view["fingerprint_prefix"]
    assert rotation.detail["last_four"] == "0002"
    assert "sk-rotated" not in json.dumps(rotation.detail)

    assert service.delete_credential(owner.id, "openai") is True
    assert service.delete_credential(owner.id, "openai") is False
    assert "ai_credential.deleted" in audit_actions(store, owner.id)
    with pytest.raises(AuthorizationError):
        service.delete_credential(members[Role.ADMIN].id, "openai")


def test_degraded_cipher_refuses_to_store_and_says_so(store, team) -> None:
    owner, _members = team
    degraded = AIEngineService(
        store, cipher=CredentialCipher.degraded(), environ={}, http_session=FakeHttp()
    )
    assert degraded.encryption_status()["can_encrypt"] is False
    with pytest.raises(CredentialCipherUnavailable, match=PUBLIC_KEY_ENV):
        degraded.store_credential(owner.id, "gemini", "gemini-secret-value-0003", verify=False)
    assert store.ai_credentials_configured(actor_id=owner.id)["gemini"] is False


def test_api_key_format_is_validated_without_echoing_the_value() -> None:
    for value in ("short", "has space in it", "contains\ttab", "ñ" * 20, 42):
        with pytest.raises(AICredentialError) as captured:
            normalize_api_key(value)
        assert str(value) not in str(captured.value)
    assert normalize_api_key("  sk-valid-key-value  ") == "sk-valid-key-value"


def test_verification_is_requested_by_the_panel_and_executed_by_the_worker(
    store, team, keypair
) -> None:
    owner, members = team
    panel = sealer_service(store, keypair)
    panel.store_credential(owner.id, "openai", SECRET, verify=False)

    requested = panel.verify_credential(members[Role.ADMIN].id, "openai")
    assert requested.outcome == "requested"
    assert requested.view["verification_pending"] is True
    with pytest.raises(AuthorizationError):
        panel.verify_credential(members[Role.EDITOR].id, "openai")

    worker_http = FakeHttp(401)
    worker = worker_service(store, keypair, http=worker_http)
    results = worker.run_pending_verifications(members[Role.SCHEDULER].id)

    assert [(provider, check.status) for provider, check in results] == [("openai", "invalid")]
    assert worker_http.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    stored = store.list_ai_credentials(actor_id=owner.id)[0].public_view()
    assert stored["last_verified_status"] == "invalid"
    assert stored["verification_pending"] is False
    assert stored["last_verified_by"] == members[Role.SCHEDULER].id
    actions = audit_actions(store, owner.id)
    assert "ai_credential.verification_requested" in actions
    assert "ai_credential.verified" in actions
    assert worker.run_pending_verifications(members[Role.SCHEDULER].id) == []

    direct = worker.verify_credential(owner.id, "openai")
    assert direct.outcome == "verified" and direct.check is not None


# --- precedencia base vs. entorno ----------------------------------------------------------


def test_stored_credential_wins_over_environment_and_env_is_the_fallback(
    store, team, keypair
) -> None:
    owner, members = team
    scheduler = members[Role.SCHEDULER].id
    sealer_service(store, keypair).store_credential(owner.id, "openai", SECRET, verify=False)
    worker = worker_service(
        store,
        keypair,
        environ={"OPENAI_API_KEY": "environment-key", "GEMINI_API_KEY": "gemini-env-key"},
    )

    assert worker.resolve_api_key("openai", actor_id=scheduler) == SECRET
    assert worker.resolve_api_key("gemini", actor_id=scheduler) is None
    with pytest.raises(AuthorizationError):
        worker.resolve_api_key("openai", actor_id=members[Role.EDITOR].id)

    store.update_ai_engine_settings(
        actor_id=owner.id, expected_version=1, text_provider="openai", image_provider="gemini"
    )
    http = FakeHttp(
        200,
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": DRAFT_TOOL_NAME,
                                    "arguments": json.dumps(
                                        {
                                            "categoria": "dato_semana",
                                            "institucion": "colmat",
                                            "texto": "El dato es 10 %. Fuente: DANE 2026.",
                                            "cifra": "10 %",
                                            "fuente": "DANE 2026",
                                            "visual": {
                                                "tipo": "ninguna",
                                                "descripcion": "Sin imagen.",
                                                "colores": [],
                                                "tipografia": "Arial",
                                                "incluye_retrato_persona_viva": False,
                                                "usa_simbolos": False,
                                                "serie_completa": False,
                                                "eje_truncado": False,
                                            },
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                }
            ]
        },
    )
    engine = worker.build_engine(scheduler, session=http)
    assert engine.describe() == {
        "text": {"provider": "openai", "model": "gpt-5"},
        "image": {"provider": "gemini", "model": "gemini-2.5-flash-image"},
    }
    draft = engine.generate_draft("Dato", POLICY)
    assert draft.figure == "10 %"
    # La base gana: el worker ignora OPENAI_API_KEY del entorno.
    assert http.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_worker_without_private_key_fails_closed_instead_of_falling_back(
    store, team, keypair, monkeypatch
) -> None:
    owner, members = team
    scheduler = members[Role.SCHEDULER].id
    sealer_service(store, keypair).store_credential(owner.id, "openai", SECRET, verify=False)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    store.update_ai_engine_settings(actor_id=owner.id, expected_version=1, text_provider="openai")
    blind = AIEngineService(
        store, cipher=CredentialCipher.degraded(), environ={}, http_session=FakeHttp()
    )

    with pytest.raises(ProviderConfigurationError, match=PRIVATE_KEY_ENV):
        blind.resolve_api_key("openai", actor_id=scheduler)
    engine = blind.build_engine(scheduler, session=FakeHttp())
    with pytest.raises(ProviderConfigurationError, match="no puede abrirla"):
        engine.generate_draft("Dato", POLICY)

    _other_private, _other_public = CredentialCipher.generate_keypair()
    stranger = worker_service(store, (_other_private, _other_public))
    with pytest.raises(ProviderConfigurationError, match="no se pudo abrir"):
        stranger.resolve_api_key("openai", actor_id=scheduler)


def test_worker_status_report_audits_only_changes(store, team, keypair) -> None:
    owner, members = team
    scheduler = members[Role.SCHEDULER].id
    worker = worker_service(store, keypair, environ={"MINIMAX_API_KEY": "present"})

    first = worker.report_worker_status(scheduler)
    again = worker.report_worker_status(scheduler)
    assert {row.provider: row.credential_in_environment for row in first} == {
        "minimax": True,
        "anthropic": False,
        "openai": False,
        "gemini": False,
    }
    assert all(row.can_decrypt_credentials for row in again)
    assert audit_actions(store, owner.id).count("ai_engine.provider_status_changed") == 1

    changed = worker_service(
        store, keypair, environ={"MINIMAX_API_KEY": "present", "OPENAI_API_KEY": "new"}
    ).report_worker_status(scheduler)
    assert next(row for row in changed if row.provider == "openai").credential_in_environment
    assert audit_actions(store, owner.id).count("ai_engine.provider_status_changed") == 2
    detail = [
        event
        for event in store.list_audit_events(actor_id=owner.id, limit=200)
        if event.action == "ai_engine.provider_status_changed"
    ][-1].detail
    assert detail["changes"] == {"openai": {"previous": False, "current": True}}
    assert "new" not in json.dumps(detail)
    with pytest.raises(AuthorizationError):
        AIEngineService(store, cipher=CredentialCipher.degraded(), environ={}).report_worker_status(
            members[Role.EDITOR].id
        )

    overview = worker.overview(owner.id, role=Role.OWNER)
    assert overview.can_manage_credentials is True
    assert overview.provider("minimax").worker_status["credential_in_environment"] is True
    assert overview.provider("openai").stored is None
    limited = worker.overview(members[Role.EDITOR].id, role=Role.EDITOR)
    assert limited.can_manage_engine is False
    assert limited.can_manage_credentials is False
