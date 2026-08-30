from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from colmat_x.platform_store import ConflictError
from colmat_x.telegram_bot import (
    SendMessage,
    WebhookAuthenticationError,
    WebhookResult,
)
from colmat_x.web import (
    PlatformTelegramOperations,
    RuntimeProvider,
    WebConfigurationError,
    create_app,
)

SECRET = "webhook-secret_2026"


@dataclass
class FakeStore:
    finished: list[tuple[int, str | None]] = field(default_factory=list)

    def finish_telegram_update(self, update_id: int, *, error: str | None = None) -> object:
        self.finished.append((update_id, error))
        return object()


@dataclass
class FakeProcessor:
    duplicate: bool = False
    actions: tuple[object, ...] = ()
    error: Exception | None = None
    calls: list[tuple[object, str | None]] = field(default_factory=list)

    def process_update(self, payload: object, *, secret_token: str | None) -> WebhookResult:
        self.calls.append((payload, secret_token))
        if secret_token != SECRET:
            raise WebhookAuthenticationError("bad token")
        if self.error is not None:
            raise self.error
        assert isinstance(payload, dict)
        return WebhookResult(
            update_id=payload["update_id"],
            duplicate=self.duplicate,
            actions=self.actions,
        )


@dataclass
class FakeTelegramClient:
    sent: list[tuple[int, str, int | None]] = field(default_factory=list)
    error: Exception | None = None

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> object:
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text, reply_to_message_id))
        return {"ok": True}


def make_client(
    *,
    store: FakeStore | None = None,
    processor: FakeProcessor | None = None,
    telegram_client: FakeTelegramClient | None = None,
    maximum: int = 1024,
) -> tuple[TestClient, FakeStore, FakeProcessor, FakeTelegramClient]:
    selected_store = store or FakeStore()
    selected_processor = processor or FakeProcessor()
    selected_client = telegram_client or FakeTelegramClient()
    app = create_app(
        store=selected_store,
        processor=selected_processor,
        telegram_client=selected_client,
        max_update_bytes=maximum,
    )
    return TestClient(app), selected_store, selected_processor, selected_client


def test_health_does_not_initialize_integrations_or_return_secrets() -> None:
    app = create_app(environ={"TELEGRAM_BOT_TOKEN": "sensitive-value"})

    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "colmat-x-automation"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "sensitive-value" not in response.text
    assert app.state.runtime_provider._runtime is None


def test_ready_reports_only_check_states() -> None:
    client, _, _, _ = make_client()

    response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {
            "telegram_webhook_secret": "ok",
            "telegram_bot_token": "ok",
            "database": "ok",
        },
    }


def test_ready_is_503_when_vercel_configuration_is_missing() -> None:
    app = create_app(environ={"VERCEL": "1"})

    response = TestClient(app).get("/api/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"] == {
        "telegram_webhook_secret": "missing",
        "telegram_bot_token": "missing",
        "database": "missing",
    }


def test_webhook_processes_executes_and_finalizes_update() -> None:
    action = SendMessage(telegram_user_id=7, chat_id=9, text="Listo", reply_to_message_id=3)
    processor = FakeProcessor(actions=(action,))
    client, store, _, telegram = make_client(processor=processor)

    response = client.post(
        "/api/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={"update_id": 42},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "update_id": 42, "duplicate": False}
    assert telegram.sent == [(9, "Listo", 3)]
    assert store.finished == [(42, None)]


def test_duplicate_does_not_execute_or_finalize_again() -> None:
    action = SendMessage(telegram_user_id=7, chat_id=9, text="No enviar")
    processor = FakeProcessor(duplicate=True, actions=(action,))
    client, store, _, telegram = make_client(processor=processor)

    response = client.post(
        "/api/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={"update_id": 42},
    )

    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert telegram.sent == []
    assert store.finished == []


def test_webhook_rejects_bad_secret_without_finalizing() -> None:
    client, store, _, _ = make_client()

    response = client.post(
        "/api/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        json={"update_id": 42},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    assert store.finished == []


def test_webhook_rejects_malformed_json_and_non_object() -> None:
    client, store, processor, _ = make_client()

    invalid_json = client.post(
        "/api/telegram/webhook",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": SECRET,
            "Content-Type": "application/json",
        },
        content=b"{",
    )
    non_object = client.post(
        "/api/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json=[{"update_id": 42}],
    )

    assert invalid_json.status_code == 400
    assert non_object.status_code == 400
    assert processor.calls == []
    assert store.finished == []


def test_webhook_rejects_wrong_content_type_and_oversized_body() -> None:
    client, _, processor, _ = make_client(maximum=20)

    wrong_type = client.post(
        "/api/telegram/webhook",
        headers={"Content-Type": "text/plain"},
        content=b'{"update_id":1}',
    )
    oversized = client.post(
        "/api/telegram/webhook",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": SECRET,
            "Content-Type": "application/json",
        },
        content=b'{"update_id":123456789}',
    )

    assert wrong_type.status_code == 400
    assert oversized.status_code == 413
    assert processor.calls == []


def test_delivery_failure_returns_503_and_marks_claimed_update_failed() -> None:
    action = SendMessage(telegram_user_id=7, chat_id=9, text="Listo")
    telegram = FakeTelegramClient(error=RuntimeError("transport included secret"))
    client, store, _, _ = make_client(
        processor=FakeProcessor(actions=(action,)),
        telegram_client=telegram,
    )

    response = client.post(
        "/api/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={"update_id": 77},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "service unavailable"}
    assert store.finished == [(77, "webhook_failed:RuntimeError")]
    assert "transport included secret" not in response.text


def test_processing_failure_after_claim_is_finalized_when_record_exists() -> None:
    processor = FakeProcessor(error=RuntimeError("database failed"))
    client, store, _, _ = make_client(processor=processor)

    response = client.post(
        "/api/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={"update_id": 88},
    )

    assert response.status_code == 503
    assert store.finished == [(88, "webhook_failed:RuntimeError")]


def test_factory_rejects_invalid_size_limit() -> None:
    for value in (0, -1, True, 1.5):
        try:
            create_app(max_update_bytes=value)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError(f"create_app accepted invalid value {value!r}")


def test_ready_never_returns_environment_values() -> None:
    secret = "do-not-leak-this-webhook-secret"
    token = "123456:do-not-leak-this-bot-token"
    app = create_app(
        environ={
            "VERCEL": "1",
            "TELEGRAM_WEBHOOK_SECRET": secret,
            "TELEGRAM_BOT_TOKEN": token,
        }
    )

    response = TestClient(app).get("/api/ready")

    assert response.status_code == 503
    assert secret not in response.text
    assert token not in response.text
    assert all(value in {"ok", "missing", "error"} for value in response.json()["checks"].values())


class FakeEditorialStore:
    def __init__(self) -> None:
        self.review_error = False
        self.approved: list[tuple[str, str, str]] = []
        self.rejected: list[tuple[str, str, str, str]] = []

    def resolve_telegram_actor(self, **kwargs):
        assert kwargs == {"telegram_user_id": 7, "chat_id": 9}
        return SimpleNamespace(id="reviewer-1")

    def list_drafts(self, *, actor_id: str):
        assert actor_id == "reviewer-1"
        return [
            SimpleNamespace(status="draft"),
            SimpleNamespace(status="in_review"),
            SimpleNamespace(status="approved"),
        ]

    def list_memberships(self, *, actor_id: str):
        assert actor_id == "reviewer-1"
        return [
            SimpleNamespace(user_id="reviewer-1", role="reviewer"),
            SimpleNamespace(user_id="editor-1", role="editor"),
        ]

    def get_user(self, user_id: str):
        return SimpleNamespace(
            display_name="Revisión" if user_id == "reviewer-1" else "Edición",
            is_active=user_id == "reviewer-1",
        )

    def approve_draft(self, post_id: str, *, actor_id: str, expected_snapshot_hash: str):
        if self.review_error:
            raise ConflictError("cambió")
        self.approved.append((post_id, actor_id, expected_snapshot_hash))

    def reject_draft(
        self,
        post_id: str,
        *,
        actor_id: str,
        expected_snapshot_hash: str,
        reason: str,
    ):
        if self.review_error:
            raise ConflictError("cambió")
        self.rejected.append((post_id, actor_id, expected_snapshot_hash, reason))


def test_platform_telegram_operations_report_status_and_team() -> None:
    operations = PlatformTelegramOperations(FakeEditorialStore())

    status_text = operations.get_status(telegram_user_id=7, chat_id=9)
    team_text = operations.get_team(telegram_user_id=7, chat_id=9)

    assert "1 borradores" in status_text
    assert "1 en revisión" in status_text
    assert "1 aprobados" in status_text
    assert "Total: 3" in status_text
    assert "Revisión — reviewer (activo)" in team_text
    assert "Edición — editor (inactivo)" in team_text


def test_platform_telegram_operations_review_without_publishing() -> None:
    store = FakeEditorialStore()
    operations = PlatformTelegramOperations(store)

    approved = operations.approve_post(
        post_id="draft-1",
        snapshot_hash="a" * 64,
        telegram_user_id=7,
        chat_id=9,
    )
    rejected = operations.reject_post(
        post_id="draft-2",
        snapshot_hash="b" * 64,
        telegram_user_id=7,
        chat_id=9,
    )

    assert approved.accepted is True
    assert "todavía no se ha publicado" in approved.text
    assert rejected.accepted is True
    assert store.approved == [("draft-1", "reviewer-1", "a" * 64)]
    assert store.rejected[0][:3] == ("draft-2", "reviewer-1", "b" * 64)

    store.review_error = True
    conflict = operations.approve_post(
        post_id="draft-1",
        snapshot_hash="a" * 64,
        telegram_user_id=7,
        chat_id=9,
    )
    rejected_conflict = operations.reject_post(
        post_id="draft-2",
        snapshot_hash="b" * 64,
        telegram_user_id=7,
        chat_id=9,
    )
    assert conflict.accepted is False
    assert rejected_conflict.accepted is False


def test_runtime_provider_builds_real_local_runtime_and_reuses_it() -> None:
    provider = RuntimeProvider(
        environ={
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "TELEGRAM_WEBHOOK_SECRET": SECRET,
            "TELEGRAM_BOT_TOKEN": "123456:valid-local-test-token-without-spaces",
        }
    )

    runtime = provider.get()
    ready, checks = provider.readiness()

    assert provider.get() is runtime
    assert ready is True
    assert checks == {
        "telegram_webhook_secret": "ok",
        "telegram_bot_token": "ok",
        "database": "ok",
    }
    runtime.store.close()


def test_runtime_provider_rejects_vercel_without_database() -> None:
    provider = RuntimeProvider(
        environ={
            "VERCEL": "1",
            "TELEGRAM_WEBHOOK_SECRET": SECRET,
            "TELEGRAM_BOT_TOKEN": "123456:valid-local-test-token-without-spaces",
        }
    )

    with pytest.raises(WebConfigurationError, match="DATABASE_URL"):
        provider.get()
