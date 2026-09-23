from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from colmat_x.ai_engine import AIEngineService
from colmat_x.credential_cipher import PRIVATE_KEY_ENV, PUBLIC_KEY_ENV, CredentialCipher
from colmat_x.platform_store import PlatformStore
from colmat_x.providers import OPENAI_MODELS_ENDPOINT
from colmat_x.rbac import Role
from colmat_x.web import CSRF_COOKIE, create_app
from tests.test_web_dashboard import ORIGIN, PEPPER, RecordingTelegramClient, _login

SECRET = "sk-panel-secret-value-000777"


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = ""

    def json(self):
        return {"data": []}


class FakeHttp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.status_code)

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.status_code)


class LeakGuardClient(TestClient):
    """Cliente que retiene cada respuesta para comprobar que nada filtra la clave."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.responses: list = []

    def request(self, *args, **kwargs):
        response = super().request(*args, **kwargs)
        self.responses.append(response)
        return response


def build_stack(*, cipher: CredentialCipher, http: FakeHttp | None = None):
    store = PlatformStore("sqlite+pysqlite:///:memory:")
    owner, _membership = store.bootstrap_owner(
        email="owner@colmat.test", username="sirhegel", display_name="SirHegel"
    )
    store.bind_telegram_chat(
        7084929277,
        telegram_user_id=7084929277,
        actor_id=owner.id,
        user_id=owner.id,
        purpose="control",
    )
    telegram = RecordingTelegramClient()
    service = AIEngineService(store, cipher=cipher, environ={}, http_session=http or FakeHttp())
    app = create_app(
        environ={"WEB_AUTH_PEPPER": PEPPER},
        store=store,
        processor=object(),
        telegram_client=telegram,
        ai_engine_service=service,
    )
    client = LeakGuardClient(app, base_url=ORIGIN, follow_redirects=False)
    return client, store, owner, telegram, service


@pytest.fixture
def sealed_stack():
    private_key, public_key = CredentialCipher.generate_keypair()
    http = FakeHttp(200)
    client, store, owner, telegram, service = build_stack(
        cipher=CredentialCipher.from_environment({PUBLIC_KEY_ENV: public_key}), http=http
    )
    try:
        yield client, store, owner, telegram, http, private_key
    finally:
        client.close()
        store.close()


def _csrf(client: TestClient) -> str:
    token = client.cookies.get(CSRF_COOKIE)
    assert token
    return token


def _assert_no_leak(client: LeakGuardClient) -> None:
    for response in client.responses:
        assert SECRET not in response.text
        assert SECRET not in str(response.headers)


def test_owner_stores_credential_from_panel_and_no_response_ever_contains_it(
    sealed_stack,
) -> None:
    client, store, owner, telegram, http, private_key = sealed_stack
    _login(client, telegram)
    csrf = _csrf(client)

    panel = client.get("/app")
    assert "Proveedores y credenciales" in panel.text
    assert "Encargar a MiniMax" in panel.text
    assert 'name="api_key"' in panel.text

    stored = client.post(
        "/app/ai-credentials",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf, "provider": "openai", "api_key": SECRET},
    )
    assert stored.status_code == 303
    assert stored.headers["location"] == "/app?notice=ai_credential_stored_valid"
    assert http.calls[0]["url"] == OPENAI_MODELS_ENDPOINT
    assert http.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"

    after = client.get("/app")
    assert "el proveedor la aceptó" in after.text
    assert f"…{SECRET[-4:]}" in after.text
    assert "Rotar" in after.text
    record = store.list_ai_credentials(actor_id=owner.id)[0]
    assert record.envelope != SECRET and SECRET not in record.envelope
    assert record.public_view()["fingerprint_prefix"] in after.text
    opener = CredentialCipher.from_environment({PRIVATE_KEY_ENV: private_key})
    assert opener.open(record.envelope, context="colmat:ai-credential:colmat:openai") == SECRET
    _assert_no_leak(client)

    rejected = client.post(
        "/app/ai-credentials",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf, "provider": "codex", "api_key": SECRET},
    )
    assert rejected.headers["location"] == "/app?error=request_rejected"
    malformed = client.post(
        "/app/ai-credentials",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf, "provider": "gemini", "api_key": "has spaces inside"},
    )
    assert malformed.headers["location"] == "/app?error=ai_credential_failed"
    _assert_no_leak(client)


def test_panel_selects_engine_with_cas_and_relabels_generation(sealed_stack) -> None:
    client, store, owner, telegram, _http, _private = sealed_stack
    _login(client, telegram)
    csrf = _csrf(client)

    updated = client.post(
        "/app/ai-engine",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf,
            "expected_version": "1",
            "text_provider": "anthropic",
            "text_model": "claude-sonnet-5",
            "image_provider": "openai",
            "image_model": "gpt-image-1",
        },
    )
    stale = client.post(
        "/app/ai-engine",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf,
            "expected_version": "1",
            "text_provider": "gemini",
            "text_model": "gemini-2.5-pro",
            "image_provider": "gemini",
            "image_model": "gemini-2.5-flash-image",
        },
    )
    impossible = client.post(
        "/app/ai-engine",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf,
            "expected_version": "2",
            "text_provider": "anthropic",
            "text_model": "claude-sonnet-5",
            "image_provider": "anthropic",
            "image_model": "none",
        },
    )

    assert updated.headers["location"] == "/app?notice=ai_engine_updated"
    assert stale.headers["location"] == "/app?error=ai_engine_failed"
    assert impossible.headers["location"] == "/app?error=ai_engine_failed"
    settings = store.get_ai_engine_settings(actor_id=owner.id)
    assert (settings.text_provider, settings.text_model) == ("anthropic", "claude-sonnet-5")
    assert (settings.image_provider, settings.image_model) == ("openai", "gpt-image-1")
    assert settings.version == 2
    panel = client.get("/app")
    assert "Encargar a Anthropic (Claude)" in panel.text
    assert "Anthropic (Claude) prepara" in panel.text


def test_verify_requests_worker_execution_and_delete_is_audited(sealed_stack) -> None:
    client, store, owner, telegram, _http, _private = sealed_stack
    _login(client, telegram)
    csrf = _csrf(client)
    client.post(
        "/app/ai-credentials",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf, "provider": "gemini", "api_key": SECRET, "verify": "false"},
    )

    verify = client.post(
        "/app/ai-credentials/gemini/verify",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf},
    )
    assert verify.headers["location"] == "/app?notice=ai_credential_verification_requested"
    pending = store.list_pending_ai_credential_verifications(actor_id=owner.id)
    assert [row.provider for row in pending] == ["gemini"]
    assert "pendiente del worker" in client.get("/app").text

    delete = client.post(
        "/app/ai-credentials/gemini/delete",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf},
    )
    assert delete.headers["location"] == "/app?notice=ai_credential_deleted"
    assert store.ai_credentials_configured(actor_id=owner.id)["gemini"] is False
    actions = [event.action for event in store.list_audit_events(actor_id=owner.id, limit=200)]
    assert "ai_credential.verification_requested" in actions
    assert "ai_credential.deleted" in actions
    unknown = client.post(
        "/app/ai-credentials/codex/delete", headers={"Origin": ORIGIN}, data={"csrf_token": csrf}
    )
    assert unknown.headers["location"] == "/app?error=request_rejected"
    _assert_no_leak(client)


def test_editor_sees_read_only_engine_and_cannot_manage_it(sealed_stack) -> None:
    client, store, owner, telegram, _http, _private = sealed_stack
    editor, _membership = store.create_team_member(
        actor_id=owner.id,
        email="editor@colmat.test",
        username="editor.colmat",
        display_name="Editora",
        role=Role.EDITOR,
    )
    store.bind_telegram_chat(
        5550001,
        telegram_user_id=5550001,
        actor_id=owner.id,
        user_id=editor.id,
        purpose="control",
    )
    _login(client, telegram, identifier="editor.colmat")
    csrf = _csrf(client)

    panel = client.get("/app")
    assert "Selección de solo lectura" in panel.text
    assert 'name="api_key"' not in panel.text
    engine = client.post(
        "/app/ai-engine",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf,
            "expected_version": "1",
            "text_provider": "openai",
            "text_model": "gpt-5",
            "image_provider": "openai",
            "image_model": "gpt-image-1",
        },
    )
    credential = client.post(
        "/app/ai-credentials",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf, "provider": "openai", "api_key": SECRET},
    )
    assert engine.headers["location"] == "/app?error=ai_engine_failed"
    assert credential.headers["location"] == "/app?error=ai_credential_failed"
    assert store.get_ai_engine_settings(actor_id=owner.id).version == 1
    assert store.ai_credentials_configured(actor_id=owner.id)["openai"] is False
    _assert_no_leak(client)


def test_admin_can_select_engine_but_not_store_credentials(sealed_stack) -> None:
    client, store, owner, telegram, _http, _private = sealed_stack
    admin, _membership = store.create_team_member(
        actor_id=owner.id,
        email="admin@colmat.test",
        username="admin.colmat",
        display_name="Admin",
        role=Role.ADMIN,
    )
    store.bind_telegram_chat(
        5550002, telegram_user_id=5550002, actor_id=owner.id, user_id=admin.id, purpose="control"
    )
    _login(client, telegram, identifier="admin.colmat")
    csrf = _csrf(client)

    panel = client.get("/app")
    assert "Guardar motor de IA" in panel.text
    assert 'name="api_key"' not in panel.text
    engine = client.post(
        "/app/ai-engine",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf,
            "expected_version": "1",
            "text_provider": "gemini",
            "text_model": "gemini-2.5-flash",
            "image_provider": "gemini",
            "image_model": "gemini-2.5-flash-image",
        },
    )
    credential = client.post(
        "/app/ai-credentials",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf, "provider": "gemini", "api_key": SECRET},
    )
    assert engine.headers["location"] == "/app?notice=ai_engine_updated"
    assert credential.headers["location"] == "/app?error=ai_credential_failed"
    _assert_no_leak(client)


def test_degraded_panel_declares_missing_public_key_and_refuses_storage() -> None:
    client, store, _owner, telegram, _service = build_stack(cipher=CredentialCipher.degraded())
    try:
        _login(client, telegram)
        csrf = _csrf(client)
        panel = client.get("/app")
        assert "Almacén cifrado no disponible" in panel.text
        assert 'name="api_key"' not in panel.text
        refused = client.post(
            "/app/ai-credentials",
            headers={"Origin": ORIGIN},
            data={"csrf_token": csrf, "provider": "openai", "api_key": SECRET},
        )
        assert refused.headers["location"] == "/app?error=ai_credential_unavailable"
        _assert_no_leak(client)
    finally:
        client.close()
        store.close()


def test_ready_rejects_the_private_envelope_key_on_vercel() -> None:
    app = create_app(
        environ={"VERCEL": "1", "WEB_AUTH_PEPPER": PEPPER, PRIVATE_KEY_ENV: "x" * 44},
        store=object(),
        processor=object(),
        telegram_client=object(),
    )
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/api/ready")
    assert response.json()["checks"]["worker_secrets"] == "error"
