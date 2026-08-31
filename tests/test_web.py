from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from colmat_x.platform_store import Base, ConflictError, PlatformStore
from colmat_x.rbac import Role
from colmat_x.research_registry import RESEARCH_ONLY_BRIEF_PREFIX, ResearchRegistryError
from colmat_x.telegram_bot import (
    BotAutomationMode,
    SendMessage,
    WebhookAuthenticationError,
    WebhookResult,
)
from colmat_x.web import (
    REQUIRED_DATABASE_COLUMNS,
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
    retryable: bool = False
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
            retryable=self.retryable,
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


def test_readiness_requires_every_declared_model_column(tmp_path) -> None:
    expected = {
        table_name: frozenset(column.name for column in table.columns)
        for table_name, table in Base.metadata.tables.items()
    }
    assert expected == REQUIRED_DATABASE_COLUMNS
    assert "id" in REQUIRED_DATABASE_COLUMNS["users"]

    store = PlatformStore(
        f"sqlite+pysqlite:///{tmp_path / 'partial-readiness.db'}",
        create_schema=False,
    )
    try:
        with store.engine.begin() as connection:
            for table_name, columns in REQUIRED_DATABASE_COLUMNS.items():
                selected = sorted(columns - {"id"}) if table_name == "users" else sorted(columns)
                declarations = ", ".join(f'"{column}" TEXT' for column in selected)
                connection.exec_driver_sql(f'CREATE TABLE "{table_name}" ({declarations})')
        provider = RuntimeProvider(
            store=store,
            processor=FakeProcessor(),
            telegram_client=FakeTelegramClient(),
        )

        ready, checks = provider.readiness()

        assert ready is False
        assert checks["database"] == "error"
    finally:
        store.close()


def test_ready_is_503_when_vercel_configuration_is_missing() -> None:
    app = create_app(environ={"VERCEL": "1"})

    response = TestClient(app).get("/api/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"] == {
        "worker_secrets": "ok",
        "web_auth_pepper": "missing",
        "telegram_webhook_secret": "missing",
        "telegram_bot_token": "missing",
        "database": "missing",
    }


@pytest.mark.parametrize(
    "variable_name",
    (
        "MINIMAX_API_KEY",
        "X_CONSUMER_KEY",
        "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
        "EXPECTED_X_USER_ID",
        "EXPECTED_X_USERNAME",
    ),
)
def test_ready_rejects_privileged_worker_environment_on_vercel(
    variable_name: str,
) -> None:
    secret = f"sensitive-{variable_name.lower()}"
    app = create_app(
        environ={"VERCEL": "1", "WEB_AUTH_PEPPER": "p" * 32, variable_name: secret},
        store=FakeStore(),
        processor=FakeProcessor(),
        telegram_client=FakeTelegramClient(),
    )

    response = TestClient(app).get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {
            "worker_secrets": "error",
            "web_auth_pepper": "ok",
            "telegram_webhook_secret": "ok",
            "telegram_bot_token": "ok",
            "database": "ok",
        },
    }
    assert secret not in response.text


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


def test_configured_webhook_rejects_bad_secret_before_reading_body() -> None:
    store = FakeStore()
    processor = FakeProcessor()
    app = create_app(
        environ={"TELEGRAM_WEBHOOK_SECRET": SECRET},
        store=store,
        processor=processor,
        telegram_client=FakeTelegramClient(),
        max_update_bytes=8,
    )

    response = TestClient(app).post(
        "/api/telegram/webhook",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": "wrong",
            "Content-Type": "application/json",
        },
        content=b'{"update_id":123456789}',
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    assert processor.calls == []
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


def test_active_duplicate_claim_returns_503_so_telegram_retries_later() -> None:
    client, store, _, telegram = make_client(
        processor=FakeProcessor(duplicate=True, retryable=True)
    )

    response = client.post(
        "/api/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={"update_id": 78},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "service unavailable"}
    assert store.finished == []
    assert telegram.sent == []


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


class FakeAutomationTelegramStore(FakeEditorialStore):
    def __init__(self) -> None:
        super().__init__()
        self.settings = SimpleNamespace(
            enabled=True,
            mode="human_review",
            timezone="America/Bogota",
            slots=[{"id": "manana", "at": "08:30", "mode": "human_review"}],
            version=3,
        )
        self.mode_changes: list[tuple[str, int, object, list[dict[str, object]]]] = []
        self.generation_requests: list[dict[str, object]] = []
        self.generation_error = False
        self.publication_requests: list[dict[str, object]] = []
        self.editorial_line = SimpleNamespace(
            month="2026-08",
            line_text="Analizar Colombia desde el materialismo filosófico.",
            version=3,
        )

    def get_automation_settings(self, *, actor_id: str):
        assert actor_id == "reviewer-1"
        return self.settings

    def update_automation_settings(
        self,
        *,
        actor_id: str,
        expected_version: int,
        mode: object,
        slots: list[dict[str, object]],
    ):
        self.mode_changes.append((actor_id, expected_version, mode, slots))
        self.settings = SimpleNamespace(
            **{
                **vars(self.settings),
                "mode": getattr(mode, "value", str(mode)),
                "slots": slots,
                "version": expected_version + 1,
            }
        )
        return self.settings

    def enqueue_generation_request(self, brief: str, **kwargs):
        if self.generation_error:
            raise ValueError("secret should never be returned")
        self.generation_requests.append({"brief": brief, **kwargs})
        return SimpleNamespace(id="generation-request-1", generate_image=kwargs["generate_image"])

    def get_editorial_line(self, month: str, *, actor_id: str):
        assert actor_id == "reviewer-1"
        assert month == "2026-08"
        return self.editorial_line

    def get_current_revision(self, post_id: str, *, actor_id: str):
        assert (post_id, actor_id) == ("draft-approved", "reviewer-1")
        return SimpleNamespace(snapshot_hash="b" * 64)

    def enqueue_publication_request(self, draft_id: str, **kwargs):
        self.publication_requests.append({"draft_id": draft_id, **kwargs})
        return SimpleNamespace(id="publication-request-1")


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


def test_platform_telegram_owner_can_provision_list_and_bind_real_ids() -> None:
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, _membership = store.bootstrap_owner(
            email="owner-telegram@web.test",
            display_name="Owner Telegram",
        )
        editor, _editor_membership = store.create_team_member(
            actor_id=owner.id,
            email="editor-existing@web.test",
            display_name="Editor existente",
            role=Role.EDITOR,
        )
        store.bind_telegram_chat(
            101,
            telegram_user_id=101,
            actor_id=owner.id,
            user_id=owner.id,
        )
        operations = PlatformTelegramOperations(store)

        invited = operations.invite_telegram_user(
            202,
            "reviewer",
            "reviewer-new@web.test",
            "Reviewer nuevo",
            request_id="telegram:301:invitar",
            telegram_user_id=101,
            chat_id=101,
        )
        bound = operations.bind_telegram_user(
            203,
            editor.id,
            request_id="telegram:302:vincular",
            telegram_user_id=101,
            chat_id=101,
        )
        users = operations.list_telegram_users(telegram_user_id=101, chat_id=101)

        assert invited.accepted is True
        assert bound.accepted is True
        assert store.resolve_telegram_actor(telegram_user_id=202, chat_id=202).display_name == (
            "Reviewer nuevo"
        )
        assert store.resolve_telegram_actor(telegram_user_id=203, chat_id=203).id == editor.id
        assert "Telegram=202 (activo, privado)" in users
        assert "Telegram=203 (activo, privado)" in users
        assert "@username" in users


def test_platform_telegram_user_listing_stays_within_telegram_limit() -> None:
    memberships = [SimpleNamespace(user_id=f"user-{index}", role="editor") for index in range(150)]

    class LargeTeamStore:
        def resolve_telegram_actor(self, **_kwargs):
            return SimpleNamespace(id="owner")

        def list_memberships(self, *, actor_id: str):
            assert actor_id == "owner"
            return memberships

        def list_telegram_bindings(self, *, actor_id: str):
            assert actor_id == "owner"
            return []

        def get_user(self, user_id: str):
            return SimpleNamespace(
                id=user_id,
                display_name=f"Integrante {user_id} " + "x" * 70,
                is_active=True,
            )

    text = PlatformTelegramOperations(LargeTeamStore()).list_telegram_users(
        telegram_user_id=101,
        chat_id=101,
    )

    assert len(text) <= 4_096
    assert "usuario(s) omitidos" in text


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


def test_platform_telegram_operations_expose_calendar_and_cas_mode() -> None:
    store = FakeAutomationTelegramStore()
    store.settings.slots = [
        {
            "id": "manana",
            "at": "08:30",
            "weekdays": ["lunes"],
            "mode": "human_review",
            "category": "dato_semana",
            "evidence": {"verified": False, "reference": None},
        },
        {
            "id": "tarde",
            "at": "16:30",
            "mode": "human_review",
            "generate_image": True,
        },
    ]
    operations = PlatformTelegramOperations(
        store,
        now=lambda: datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    calendar = operations.get_calendar(days=2, telegram_user_id=7, chat_id=9)
    current = operations.get_mode(telegram_user_id=7, chat_id=9)
    changed = operations.set_mode(
        BotAutomationMode.DIRECT,
        request_id="telegram:10:modo",
        telegram_user_id=7,
        chat_id=9,
    )

    assert "2026-08-30 08:30 — manana" not in calendar
    assert "2026-08-31 08:30 — manana" in calendar
    assert "2026-08-30 16:30 — tarde" in calendar
    assert "modo human_review" in calendar
    assert "versión 3" in current
    assert changed.accepted is True
    assert "versión 4" in changed.text
    assert store.mode_changes[0][0:2] == ("reviewer-1", 3)
    assert getattr(store.mode_changes[0][2], "value", None) == "direct"
    assert store.mode_changes[0][3] == [
        {
            "id": "manana",
            "at": "08:30",
            "weekdays": ["lunes"],
            "mode": "direct",
            "category": "dato_semana",
            "evidence": {"verified": False, "reference": None},
        },
        {
            "id": "tarde",
            "at": "16:30",
            "mode": "direct",
            "generate_image": True,
        },
    ]
    assert store.settings.slots == store.mode_changes[0][3]


def test_platform_telegram_mode_change_rejects_malformed_persisted_slots() -> None:
    store = FakeAutomationTelegramStore()
    store.settings.slots = [{"id": "manana", "mode": "human_review"}, "invalid"]
    operations = PlatformTelegramOperations(store)

    changed = operations.set_mode(
        BotAutomationMode.DIRECT,
        request_id="telegram:10:modo",
        telegram_user_id=7,
        chat_id=9,
    )

    assert changed.accepted is False
    assert store.mode_changes == []
    assert store.settings.mode == "human_review"


def test_platform_telegram_mode_change_uses_real_cas_and_rbac(monkeypatch) -> None:
    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, _membership = store.bootstrap_owner(
            email="owner@web.test",
            display_name="Owner",
        )
        reviewer = store.create_user(
            actor_id=owner.id,
            email="reviewer@web.test",
            display_name="Reviewer",
        )
        store.grant_membership(reviewer.id, Role.REVIEWER, actor_id=owner.id)
        store.bind_telegram_chat(
            -202,
            telegram_user_id=101,
            actor_id=owner.id,
            user_id=owner.id,
        )
        store.bind_telegram_chat(
            -303,
            telegram_user_id=102,
            actor_id=owner.id,
            user_id=reviewer.id,
        )
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            slots=[
                {
                    "id": "manana",
                    "at": "08:30",
                    "mode": "human_review",
                    "category": "dato_semana",
                    "institution": "colmat",
                    "brief": "Explica una cifra territorial con fuente primaria.",
                    "generate_image": True,
                    "evidence": {
                        "verified": False,
                        "reference": None,
                        "expected_figure": None,
                        "expected_source": None,
                    },
                }
            ],
        )
        operations = PlatformTelegramOperations(store)

        changed = operations.set_mode(
            BotAutomationMode.DIRECT,
            request_id="telegram:20:modo",
            telegram_user_id=101,
            chat_id=-202,
        )
        denied = operations.set_mode(
            BotAutomationMode.HUMAN_REVIEW,
            request_id="telegram:21:modo",
            telegram_user_id=102,
            chat_id=-303,
        )
        settings = store.get_automation_settings(actor_id=owner.id)

    assert changed.accepted is True
    assert denied.accepted is False
    assert settings.mode == "direct"
    assert settings.version == 3
    assert settings.slots == [
        {
            "at": "08:30",
            "brief": "Explica una cifra territorial con fuente primaria.",
            "category": "dato_semana",
            "evidence": {
                "expected_figure": None,
                "expected_source": None,
                "reference": None,
                "verified": False,
            },
            "generate_image": True,
            "id": "manana",
            "institution": "colmat",
            "mode": "direct",
        }
    ]


def test_platform_telegram_generation_only_enqueues_for_openclaw() -> None:
    store = FakeAutomationTelegramStore()
    operations = PlatformTelegramOperations(
        store,
        generate_images=True,
        now=lambda: datetime(2026, 8, 30, tzinfo=UTC),
    )

    result = operations.generate_draft(
        "Una cifra verificable",
        request_id="telegram:11:generar",
        telegram_user_id=7,
        chat_id=9,
    )

    assert result.accepted is True
    assert "OpenClaw" in result.text
    assert "revisión humana" in result.text
    assert store.generation_requests == [
        {
            "brief": (
                "Línea editorial 2026-08 v3: Analizar Colombia desde el materialismo "
                "filosófico. Encargo: Una cifra verificable"
            ),
            "actor_id": "reviewer-1",
            "telegram_user_id": 7,
            "chat_id": 9,
            "idempotency_key": "telegram:11:generar",
            "generate_image": True,
        }
    ]


def test_platform_telegram_generation_cannot_skip_missing_monthly_line() -> None:
    store = FakeAutomationTelegramStore()
    store.editorial_line = None
    operations = PlatformTelegramOperations(
        store,
        now=lambda: datetime(2026, 8, 30, tzinfo=UTC),
    )

    result = operations.generate_draft(
        "Intenta generar sin directriz",
        request_id="telegram:14:generar",
        telegram_user_id=7,
        chat_id=9,
    )

    assert result.accepted is False
    assert "línea editorial" in result.text
    assert store.generation_requests == []


def test_platform_telegram_research_enqueues_explicit_pattern_without_image_or_x() -> None:
    store = FakeAutomationTelegramStore()
    operations = PlatformTelegramOperations(
        store,
        now=lambda: datetime(2026, 8, 30, tzinfo=UTC),
    )

    result = operations.research_topic(
        "Guerra de los Supremos y formación estatal",
        request_id="telegram:15:investigar",
        telegram_user_id=7,
        chat_id=9,
    )
    patterns = operations.get_research_patterns(telegram_user_id=7, chat_id=9)

    assert result.accepted is True
    assert "OpenClaw" in result.text
    assert "MiniMax" in result.text
    assert "revisión humana" in result.text
    assert len(store.generation_requests) == 1
    request = store.generation_requests[0]
    assert request["generate_image"] is False
    assert request["category"] == "dato_semana"
    assert request["institution"] == "colmat"
    assert request["idempotency_key"] == "telegram:15:investigar"
    brief = str(request["brief"])
    assert len(brief) <= 1_000
    assert brief.startswith(RESEARCH_ONLY_BRIEF_PREFIX)
    for required in (
        "pregunta o tesis",
        "fuentes aportadas o del registro canónico",
        "hechos e inferencias",
        "posible refutación",
        "síntesis apta",
        "No fabricar citas",
        "POR VERIFICAR",
        "URLs maestras autorizadas",
        "recuperará solo esas fuentes",
    ):
        assert required in brief
    assert "verificación externa sigue siendo humana" in patterns
    assert "nunca pasa a publicación automática" in patterns
    assert not hasattr(store, "publish_post")


def test_platform_telegram_research_injects_selected_catalog_entry() -> None:
    store = FakeAutomationTelegramStore()
    operations = PlatformTelegramOperations(
        store,
        now=lambda: datetime(2026, 8, 30, tzinfo=UTC),
    )

    result = operations.research_topic(
        "Ensayos materialistas y materialismo filosófico",
        request_id="telegram:catalog:investigar",
        telegram_user_id=7,
        chat_id=9,
    )

    assert result.accepted is True
    brief = str(store.generation_requests[0]["brief"])
    assert "Ensayos materialistas (1972)" in brief
    assert "https://www.fgbueno.es/gbm/gb1972em.htm" in brief
    assert "Catálogo por título, no cita" in brief
    assert len(brief) <= 1_000


def test_platform_telegram_research_labels_explicit_disputed_attribution() -> None:
    store = FakeAutomationTelegramStore()
    operations = PlatformTelegramOperations(
        store,
        now=lambda: datetime(2026, 8, 30, tzinfo=UTC),
    )

    result = operations.research_topic(
        "Curso elemental de filosofía 1962",
        request_id="telegram:dispute:investigar",
        telegram_user_id=7,
        chat_id=9,
    )

    assert result.accepted is True
    brief = str(store.generation_requests[0]["brief"])
    assert "atribución editorial disputada; Bueno negó participación" in brief
    assert "https://nodulo.org/ec/2010/n099p02.htm" in brief


def test_platform_telegram_research_rejects_line_and_topic_over_1000() -> None:
    store = FakeAutomationTelegramStore()
    store.editorial_line.line_text = "x" * 600
    operations = PlatformTelegramOperations(
        store,
        now=lambda: datetime(2026, 8, 30, tzinfo=UTC),
    )

    result = operations.research_topic(
        "y" * 300,
        request_id="telegram:16:investigar",
        telegram_user_id=7,
        chat_id=9,
    )

    assert result.accepted is False
    assert store.generation_requests == []


def test_platform_telegram_publication_only_enqueues_durable_request() -> None:
    store = FakeAutomationTelegramStore()
    operations = PlatformTelegramOperations(store)

    result = operations.request_publication(
        "draft-approved",
        request_id="telegram:12:publicar",
        telegram_user_id=7,
        chat_id=9,
    )

    assert result.accepted is True
    assert "publication-request-1" in result.text
    assert "no se publicó durante el webhook" in result.text
    assert store.publication_requests == [
        {
            "draft_id": "draft-approved",
            "actor_id": "reviewer-1",
            "expected_snapshot_hash": "b" * 64,
            "idempotency_key": "telegram:12:publicar",
        }
    ]


def test_platform_telegram_generation_failure_is_generic_and_never_publishes() -> None:
    store = FakeAutomationTelegramStore()
    store.generation_error = True
    operations = PlatformTelegramOperations(
        store,
        now=lambda: datetime(2026, 8, 30, tzinfo=UTC),
    )

    result = operations.generate_draft(
        "Una cifra verificable",
        request_id="telegram:13:generar",
        telegram_user_id=7,
        chat_id=9,
    )

    assert result.accepted is False
    assert "secret should never be returned" not in result.text
    assert store.generation_requests == []
    assert not hasattr(store, "publish_post")


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


def test_runtime_provider_fails_closed_when_canonical_registry_is_missing(monkeypatch) -> None:
    def missing_registry():
        raise ResearchRegistryError("missing")

    monkeypatch.setattr("colmat_x.web.load_gustavo_bueno_registry", missing_registry)
    provider = RuntimeProvider(
        environ={},
        store=object(),
        processor=object(),
        telegram_client=object(),
    )

    with pytest.raises(ResearchRegistryError, match="missing"):
        provider.get()


def test_vercel_runtime_never_creates_schema_and_fails_readiness_when_unmigrated(
    tmp_path,
) -> None:
    database_path = tmp_path / "unmigrated.db"
    provider = RuntimeProvider(
        environ={
            "VERCEL": "1",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "TELEGRAM_WEBHOOK_SECRET": SECRET,
            "TELEGRAM_BOT_TOKEN": "123456:valid-local-test-token-without-spaces",
        }
    )

    runtime = provider.get()
    ready, checks = provider.readiness()

    assert ready is False
    assert checks["database"] == "error"
    assert inspect(runtime.store.engine).get_table_names() == []
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
