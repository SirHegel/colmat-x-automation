"""Adaptador de Telegram sobre el store real: seguros, confirmaciones, agenda, cola y equipo."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from colmat_x.cli import app
from colmat_x.platform_store import (
    AutomationMode,
    AutomationRunStatus,
    ConflictError,
    DraftStatus,
    PlatformStore,
    PublicationRequestStatus,
    PublishStatus,
)
from colmat_x.rbac import AuthorizationError, Role
from colmat_x.telegram_bot import (
    BotAutomationMode,
    CommandResult,
    ControlAction,
    SafeguardGate,
    TelegramWebhookProcessor,
    TelegramWebhookSecret,
    approval_callback_data,
)
from colmat_x.telegram_operations import PlatformTelegramOperations
from tests.test_cli import POLICY_PATH, persisted_test_slot, platform_database_url
from tests.test_platform_store import automation_slot

NOW = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
OWNER_TG = 101
EDITOR_TG = 102
REVIEWER_TG = 103
PUBLISHER_TG = 104
SCHEDULER_TG = 105
AUDITOR_TG = 106
GROUP_CHAT = -202
AUTOMATION_CONFIG = Path("config/automation.yaml").resolve()


class Team:
    def __init__(self, store: PlatformStore) -> None:
        self.store = store
        self.owner, _ = store.bootstrap_owner(email="owner@ops.test", display_name="Owner", now=NOW)
        self.editor = self._member(Role.EDITOR, EDITOR_TG)
        self.reviewer = self._member(Role.REVIEWER, REVIEWER_TG)
        self.publisher = self._member(Role.PUBLISHER, PUBLISHER_TG)
        self.scheduler = self._member(Role.SCHEDULER, SCHEDULER_TG)
        self.auditor = self._member(Role.AUDITOR, AUDITOR_TG)
        self._bind(self.owner.id, OWNER_TG)
        # El owner también participa en el grupo de revisión.
        store.bind_telegram_chat(
            GROUP_CHAT,
            telegram_user_id=OWNER_TG,
            actor_id=self.owner.id,
            user_id=self.owner.id,
            now=NOW,
        )

    def _member(self, role: Role, telegram_id: int):
        user, _membership = self.store.create_team_member(
            actor_id=self.owner.id,
            email=f"{role.value}@ops.test",
            display_name=role.value.title(),
            role=role,
            now=NOW,
        )
        self._bind(user.id, telegram_id)
        return user

    def _bind(self, user_id: str, telegram_id: int) -> None:
        self.store.bind_telegram_chat(
            telegram_id,
            telegram_user_id=telegram_id,
            actor_id=self.owner.id,
            user_id=user_id,
            now=NOW,
        )


@pytest.fixture
def store() -> PlatformStore:
    selected = PlatformStore("sqlite+pysqlite:///:memory:")
    try:
        yield selected
    finally:
        selected.close()


@pytest.fixture
def clock() -> dict[str, datetime]:
    return {"now": NOW}


@pytest.fixture
def team(store: PlatformStore) -> Team:
    return Team(store)


@pytest.fixture
def operations(store: PlatformStore, clock: dict[str, datetime]) -> PlatformTelegramOperations:
    return PlatformTelegramOperations(
        store,
        now=lambda: clock["now"],
        automation_config_path=AUTOMATION_CONFIG,
        policy_path=POLICY_PATH,
    )


def nonce_of(result: CommandResult) -> str:
    assert result.keyboard is not None, result.text
    return result.keyboard[0][0].callback_data.split(":")[2]


def confirm(
    operations: PlatformTelegramOperations,
    result: CommandResult,
    action: ControlAction,
    *,
    telegram_id: int,
    accept: bool = True,
) -> CommandResult:
    return operations.resolve_confirmation(
        nonce_of(result),
        action=action,
        confirm=accept,
        request_id=f"telegram:{telegram_id}:confirm",
        telegram_user_id=telegram_id,
        chat_id=telegram_id,
    )


def approved_draft(store: PlatformStore, team: Team, *, publish_at: datetime = NOW):
    draft, revision = store.create_draft(
        actor_id=team.editor.id,
        text="Bogotá aporta 25,2 % del PIB nacional. Fuente: DANE 2024.",
        category="dato_semana",
        publish_at=publish_at,
        evidence={"manual": "capitulo-2", "fuente": "DANE"},
        now=NOW,
    )
    store.submit_for_review(
        draft.id,
        actor_id=team.editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    store.approve_draft(
        draft.id,
        actor_id=team.reviewer.id,
        expected_snapshot_hash=revision.snapshot_hash,
        now=NOW,
    )
    return draft, revision


def unknown_request(store: PlatformStore, team: Team, *, with_attempt: bool):
    draft, revision = approved_draft(store, team)
    request = store.enqueue_publication_request(
        draft.id,
        actor_id=team.publisher.id,
        expected_snapshot_hash=revision.snapshot_hash,
        idempotency_key=f"telegram:{draft.id}:publicar",
        now=NOW,
    )
    claim = store.claim_publication_request(
        actor_id=team.publisher.id,
        lease_seconds=1,
        now=NOW,
    )
    assert claim is not None and claim.request.id == request.id
    if with_attempt:
        store.create_publish_attempt(
            draft.id,
            actor_id=team.publisher.id,
            expected_snapshot_hash=revision.snapshot_hash,
            idempotency_key=claim.publish_attempt_idempotency_key,
            now=NOW,
        )
    # La lease vence sin resultado durable: la solicitud queda unknown.
    store.has_queued_publication_request(actor_id=team.publisher.id, now=NOW + timedelta(seconds=2))
    expired = store.get_publication_request(request.id, actor_id=team.publisher.id)
    assert expired.status_value is PublicationRequestStatus.UNKNOWN
    return draft, revision, expired


# ----------------------------------------------------------------------------
# Seguros persistidos
# ----------------------------------------------------------------------------


def test_safeguards_start_closed_and_change_only_with_rbac_cas_and_audit(
    store: PlatformStore, team: Team
) -> None:
    initial = store.get_publication_safeguards(actor_id=team.auditor.id)
    assert initial.version == 0
    assert initial.live_enabled is False and initial.direct_publish_enabled is False

    with pytest.raises(AuthorizationError):
        store.update_publication_safeguards(
            actor_id=team.scheduler.id, expected_version=0, live_enabled=True, now=NOW
        )
    with pytest.raises(ConflictError, match="versión actual 0"):
        store.update_publication_safeguards(
            actor_id=team.owner.id, expected_version=1, live_enabled=True, now=NOW
        )
    with pytest.raises(ValueError, match="al menos un cambio"):
        store.update_publication_safeguards(actor_id=team.owner.id, expected_version=0, now=NOW)

    opened = store.update_publication_safeguards(
        actor_id=team.owner.id, expected_version=0, live_enabled=True, now=NOW
    )
    assert opened.version == 1
    assert opened.live_enabled is True
    assert opened.live_enabled_by == team.owner.id
    assert opened.live_enabled_at == NOW

    closed = store.update_publication_safeguards(
        actor_id=team.owner.id,
        expected_version=1,
        live_enabled=False,
        now=NOW + timedelta(minutes=1),
    )
    assert closed.version == 2
    assert closed.live_enabled is False and closed.live_enabled_by is None

    with pytest.raises(ConflictError, match="versión actual 2"):
        store.update_publication_safeguards(
            actor_id=team.owner.id, expected_version=1, direct_publish_enabled=True, now=NOW
        )
    events = [
        event
        for event in store.list_audit_events(actor_id=team.owner.id)
        if event.action == "publication_safeguards.updated"
    ]
    assert [event.detail["current"]["version"] for event in events] == [1, 2]
    assert events[0].detail["previous"]["live_enabled"] is False
    assert events[0].detail["current"]["live_enabled_by"] == team.owner.id


def test_direct_mode_is_armed_by_the_persisted_safeguard_when_the_process_lacks_the_env(
    store: PlatformStore, team: Team, monkeypatch
) -> None:
    monkeypatch.delenv("COLMAT_DIRECT_PUBLISH_ENABLED", raising=False)
    store.update_automation_settings(
        actor_id=team.owner.id,
        expected_version=1,
        slots=[automation_slot("directo", mode="human_review")],
        now=NOW,
    )

    with pytest.raises(ConflictError, match="COLMAT_DIRECT_PUBLISH_ENABLED"):
        store.update_automation_settings(
            actor_id=team.owner.id,
            expected_version=2,
            mode=AutomationMode.DIRECT,
            slots=[automation_slot("directo", mode="direct")],
            now=NOW,
        )
    store.update_publication_safeguards(
        actor_id=team.owner.id, expected_version=0, direct_publish_enabled=True, now=NOW
    )
    settings = store.update_automation_settings(
        actor_id=team.owner.id,
        expected_version=2,
        mode=AutomationMode.DIRECT,
        slots=[automation_slot("directo", mode="direct")],
        now=NOW,
    )
    assert settings.mode_value is AutomationMode.DIRECT
    assert settings.direct_authorized_by == team.owner.id


def test_safeguard_open_is_two_step_single_use_and_close_is_immediate(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations
) -> None:
    prompt = operations.request_safeguard_change(
        SafeguardGate.LIVE,
        True,
        request_id="telegram:1:seguro",
        telegram_user_id=OWNER_TG,
        chat_id=OWNER_TG,
    )
    assert prompt.accepted is True and prompt.keyboard is not None
    assert store.get_publication_safeguards(actor_id=team.owner.id).live_enabled is False

    applied = confirm(operations, prompt, ControlAction.SAFEGUARD_LIVE_ON, telegram_id=OWNER_TG)
    assert applied.accepted is True
    assert "abierto por Owner" in applied.text
    assert store.get_publication_safeguards(actor_id=team.owner.id).live_enabled is True

    replayed = confirm(operations, prompt, ControlAction.SAFEGUARD_LIVE_ON, telegram_id=OWNER_TG)
    assert replayed.accepted is False
    assert "ya se usó" in replayed.text

    closed = operations.request_safeguard_change(
        SafeguardGate.LIVE,
        False,
        request_id="telegram:2:seguro",
        telegram_user_id=OWNER_TG,
        chat_id=GROUP_CHAT,
    )
    assert closed.accepted is True and closed.keyboard is None
    assert store.get_publication_safeguards(actor_id=team.owner.id).live_enabled is False

    denied = operations.request_safeguard_change(
        SafeguardGate.DIRECT,
        False,
        request_id="telegram:3:seguro",
        telegram_user_id=PUBLISHER_TG,
        chat_id=PUBLISHER_TG,
    )
    assert denied.accepted is True and "ya está cerrado" in denied.text
    store.update_publication_safeguards(
        actor_id=team.owner.id, expected_version=2, direct_publish_enabled=True, now=NOW
    )
    denied_close = operations.request_safeguard_change(
        SafeguardGate.DIRECT,
        False,
        request_id="telegram:4:seguro",
        telegram_user_id=PUBLISHER_TG,
        chat_id=PUBLISHER_TG,
    )
    assert denied_close.accepted is False
    assert store.get_publication_safeguards(actor_id=team.owner.id).direct_publish_enabled

    status = operations.get_safeguards(telegram_user_id=AUDITOR_TG, chat_id=AUDITOR_TG)
    assert "direct: ABIERTO" in status and "live: cerrado" in status
    assert "COLMAT_LIVE_ENABLED" in status


def test_stale_safeguard_version_makes_the_confirmation_fail_closed(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations
) -> None:
    prompt = operations.request_safeguard_change(
        SafeguardGate.DIRECT,
        True,
        request_id="telegram:1:seguro",
        telegram_user_id=OWNER_TG,
        chat_id=OWNER_TG,
    )
    # Alguien cambia los seguros por otro canal antes de la confirmación.
    store.update_publication_safeguards(
        actor_id=team.owner.id, expected_version=0, live_enabled=True, now=NOW
    )

    applied = confirm(operations, prompt, ControlAction.SAFEGUARD_DIRECT_ON, telegram_id=OWNER_TG)

    assert applied.accepted is False
    assert "no se aplicó" in applied.text
    assert store.get_publication_safeguards(actor_id=team.owner.id).direct_publish_enabled is False


# ----------------------------------------------------------------------------
# Confirmaciones
# ----------------------------------------------------------------------------


def test_confirmation_nonce_is_bound_to_actor_chat_action_expiry_and_outcome(
    store: PlatformStore, team: Team, clock: dict[str, datetime]
) -> None:
    with pytest.raises(ValueError, match="chat privado"):
        store.issue_control_confirmation(
            actor_id=team.owner.id,
            action="live_on",
            parameters={"expected_version": 0},
            telegram_user_id=OWNER_TG,
            chat_id=GROUP_CHAT,
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )
    with pytest.raises(ValueError, match="15 minutos"):
        store.issue_control_confirmation(
            actor_id=team.owner.id,
            action="live_on",
            parameters={},
            telegram_user_id=OWNER_TG,
            chat_id=OWNER_TG,
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )
    with pytest.raises(AuthorizationError, match="no corresponde"):
        store.issue_control_confirmation(
            actor_id=team.editor.id,
            action="live_on",
            parameters={},
            telegram_user_id=OWNER_TG,
            chat_id=OWNER_TG,
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )

    issued = store.issue_control_confirmation(
        actor_id=team.owner.id,
        action="live_on",
        parameters={"expected_version": 0},
        telegram_user_id=OWNER_TG,
        chat_id=OWNER_TG,
        expires_at=NOW + timedelta(minutes=10),
        now=NOW,
    )
    assert issued.confirmation.nonce_hash != issued.nonce
    for kwargs in (
        {"action": "direct_on", "telegram_user_id": OWNER_TG, "chat_id": OWNER_TG},
        {"action": "live_on", "telegram_user_id": EDITOR_TG, "chat_id": EDITOR_TG},
        {"action": "live_on", "telegram_user_id": OWNER_TG, "chat_id": GROUP_CHAT},
    ):
        with pytest.raises(ConflictError, match="inválida"):
            store.consume_control_confirmation(issued.nonce, confirm=True, now=NOW, **kwargs)
    with pytest.raises(ConflictError, match="inválida"):
        store.consume_control_confirmation(
            issued.nonce,
            action="live_on",
            telegram_user_id=OWNER_TG,
            chat_id=OWNER_TG,
            confirm=True,
            now=NOW + timedelta(minutes=11),
        )

    cancelled = store.consume_control_confirmation(
        issued.nonce,
        action="live_on",
        telegram_user_id=OWNER_TG,
        chat_id=OWNER_TG,
        confirm=False,
        now=NOW + timedelta(minutes=1),
    )
    assert cancelled.outcome == "cancelled"
    with pytest.raises(ConflictError, match="ya fue utilizada"):
        store.consume_control_confirmation(
            issued.nonce,
            action="live_on",
            telegram_user_id=OWNER_TG,
            chat_id=OWNER_TG,
            confirm=True,
            now=NOW + timedelta(minutes=1),
        )

    for _ in range(5):
        store.issue_control_confirmation(
            actor_id=team.owner.id,
            action="live_on",
            parameters={},
            telegram_user_id=OWNER_TG,
            chat_id=OWNER_TG,
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )
    with pytest.raises(ConflictError, match="demasiadas confirmaciones"):
        store.issue_control_confirmation(
            actor_id=team.owner.id,
            action="live_on",
            parameters={},
            telegram_user_id=OWNER_TG,
            chat_id=OWNER_TG,
            expires_at=NOW + timedelta(minutes=10),
            now=NOW,
        )
    actions = [event.action for event in store.list_audit_events(actor_id=team.owner.id)]
    assert "telegram.control_confirmation_issued" in actions
    assert "telegram.control_confirmation_cancelled" in actions


def test_expired_confirmation_from_the_adapter_never_applies(
    store: PlatformStore,
    team: Team,
    operations: PlatformTelegramOperations,
    clock: dict[str, datetime],
) -> None:
    prompt = operations.request_safeguard_change(
        SafeguardGate.LIVE,
        True,
        request_id="telegram:1:seguro",
        telegram_user_id=OWNER_TG,
        chat_id=OWNER_TG,
    )
    clock["now"] = NOW + timedelta(minutes=11)

    result = confirm(operations, prompt, ControlAction.SAFEGUARD_LIVE_ON, telegram_id=OWNER_TG)

    assert result.accepted is False
    assert store.get_publication_safeguards(actor_id=team.owner.id).live_enabled is False


# ----------------------------------------------------------------------------
# Modo direct y agenda
# ----------------------------------------------------------------------------


def test_direct_mode_needs_the_persisted_gate_and_a_confirmation(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations, monkeypatch
) -> None:
    monkeypatch.delenv("COLMAT_DIRECT_PUBLISH_ENABLED", raising=False)
    store.update_automation_settings(
        actor_id=team.owner.id,
        expected_version=1,
        slots=[automation_slot("directo")],
        now=NOW,
    )
    single_step = operations.set_mode(
        BotAutomationMode.DIRECT,
        request_id="telegram:1:modo",
        telegram_user_id=OWNER_TG,
        chat_id=OWNER_TG,
    )
    blocked = operations.request_direct_mode(
        request_id="telegram:2:modo", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    assert single_step.accepted is False and "dos pasos" in single_step.text
    assert blocked.accepted is False and "/seguro direct on" in blocked.text

    gate = operations.request_safeguard_change(
        SafeguardGate.DIRECT,
        True,
        request_id="telegram:3:seguro",
        telegram_user_id=OWNER_TG,
        chat_id=OWNER_TG,
    )
    confirm(operations, gate, ControlAction.SAFEGUARD_DIRECT_ON, telegram_id=OWNER_TG)
    prompt = operations.request_direct_mode(
        request_id="telegram:4:modo", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    assert prompt.keyboard is not None and "modo direct" in prompt.text
    assert store.get_automation_settings(actor_id=team.owner.id).mode == "human_review"

    cancelled = confirm(
        operations, prompt, ControlAction.MODE_DIRECT, telegram_id=OWNER_TG, accept=False
    )
    assert "cancelada" in cancelled.text
    assert store.get_automation_settings(actor_id=team.owner.id).mode == "human_review"

    prompt = operations.request_direct_mode(
        request_id="telegram:5:modo", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    applied = confirm(operations, prompt, ControlAction.MODE_DIRECT, telegram_id=OWNER_TG)
    settings = store.get_automation_settings(actor_id=team.owner.id)
    assert applied.accepted is True
    assert settings.mode == "direct"
    assert settings.direct_authorized_by == team.owner.id
    assert settings.slots[0]["mode"] == "direct"

    # Volver a human_review sigue siendo un solo paso y no exige seguros.
    back = operations.set_mode(
        BotAutomationMode.HUMAN_REVIEW,
        request_id="telegram:6:modo",
        telegram_user_id=OWNER_TG,
        chat_id=GROUP_CHAT,
    )
    assert back.accepted is True
    assert store.get_automation_settings(actor_id=team.owner.id).mode == "human_review"


def test_agenda_validate_sync_enable_disable_and_retry_respect_roles(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations
) -> None:
    validated = operations.validate_automation_config(
        telegram_user_id=AUDITOR_TG, chat_id=AUDITOR_TG
    )
    assert validated.startswith("Agenda YAML válida")
    assert "slots=2" in validated

    denied = operations.sync_automation_config(
        request_id="telegram:1:agenda", telegram_user_id=AUDITOR_TG, chat_id=AUDITOR_TG
    )
    assert denied.accepted is False
    synced = operations.sync_automation_config(
        request_id="telegram:2:agenda", telegram_user_id=SCHEDULER_TG, chat_id=SCHEDULER_TG
    )
    assert synced.accepted is True and "versión 2" in synced.text
    settings = store.get_automation_settings(actor_id=team.owner.id)
    assert len(settings.slots) == 2 and settings.enabled is False

    enabled = operations.enable_automation(
        request_id="telegram:3:agenda", telegram_user_id=SCHEDULER_TG, chat_id=SCHEDULER_TG
    )
    assert enabled.accepted is True and enabled.keyboard is None
    assert store.get_automation_settings(actor_id=team.owner.id).enabled is True
    again = operations.enable_automation(
        request_id="telegram:4:agenda", telegram_user_id=SCHEDULER_TG, chat_id=SCHEDULER_TG
    )
    assert "ya está activa" in again.text

    status = operations.get_automation_status(telegram_user_id=EDITOR_TG, chat_id=EDITOR_TG)
    assert "activa; modo human_review; versión 3" in status
    assert "Seguros persistidos: live cerrado; direct cerrado" in status

    slot = settings.slots[1]
    scheduled_for = datetime(2026, 8, 29, 16, 30, tzinfo=ZoneInfo(settings.timezone))
    run = store.claim_automation_run(
        actor_id=team.scheduler.id,
        idempotency_key="colmat:auto:v1:2026-08-29:territorio-tarde",
        slot_id=slot["id"],
        scheduled_for=scheduled_for,
        slot_snapshot=slot,
        now=scheduled_for,
    )
    store.finish_automation_run(
        run.id,
        AutomationRunStatus.FAILED,
        actor_id=team.scheduler.id,
        error="Fallo antes del borrador",
        now=NOW,
    )
    retried = operations.retry_automation_run(
        run.id, request_id="telegram:6:agenda", telegram_user_id=SCHEDULER_TG, chat_id=SCHEDULER_TG
    )
    assert retried.accepted is True and run.id in retried.text
    missing = operations.retry_automation_run(
        "no-existe",
        request_id="telegram:7:agenda",
        telegram_user_id=SCHEDULER_TG,
        chat_id=SCHEDULER_TG,
    )
    assert missing.accepted is False
    runs = operations.list_automation_runs(limit=5, telegram_user_id=AUDITOR_TG, chat_id=AUDITOR_TG)
    assert "failed" in runs and "reintento solicitado" in runs

    paused = operations.disable_automation(
        request_id="telegram:5:agenda", telegram_user_id=OWNER_TG, chat_id=GROUP_CHAT
    )
    assert paused.accepted is True
    assert store.get_automation_settings(actor_id=team.owner.id).enabled is False


def test_enabling_the_agenda_in_direct_mode_requires_a_confirmation(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations, monkeypatch
) -> None:
    monkeypatch.delenv("COLMAT_DIRECT_PUBLISH_ENABLED", raising=False)
    store.update_publication_safeguards(
        actor_id=team.owner.id, expected_version=0, direct_publish_enabled=True, now=NOW
    )
    store.update_automation_settings(
        actor_id=team.owner.id,
        expected_version=1,
        mode=AutomationMode.DIRECT,
        slots=[automation_slot("directo", mode="direct")],
        now=NOW,
    )

    prompt = operations.enable_automation(
        request_id="telegram:1:agenda", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    assert prompt.keyboard is not None and "modo direct" in prompt.text
    assert store.get_automation_settings(actor_id=team.owner.id).enabled is False

    applied = confirm(operations, prompt, ControlAction.AGENDA_ENABLE, telegram_id=OWNER_TG)
    assert applied.accepted is True
    assert store.get_automation_settings(actor_id=team.owner.id).enabled is True


# ----------------------------------------------------------------------------
# Revisión, retiro y conciliación
# ----------------------------------------------------------------------------


def test_review_buttons_and_withdrawal_respect_separation_of_duties(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations
) -> None:
    draft, revision = store.create_draft(
        actor_id=team.editor.id,
        text="Bogotá aporta 25,2 % del PIB nacional. Fuente: DANE 2024.",
        category="dato_semana",
        publish_at=NOW,
        evidence={"manual": "capitulo-2", "fuente": "DANE"},
        now=NOW,
    )
    store.submit_for_review(
        draft.id, actor_id=team.editor.id, expected_snapshot_hash=revision.snapshot_hash, now=NOW
    )

    # Los nonces expiran en tiempo real; este tramo usa el reloj del sistema.
    live_operations = PlatformTelegramOperations(
        store, automation_config_path=AUTOMATION_CONFIG, policy_path=POLICY_PATH
    )
    author_buttons = live_operations.request_review(
        draft.id, request_id="telegram:1:revisar", telegram_user_id=EDITOR_TG, chat_id=EDITOR_TG
    )
    assert author_buttons.accepted is False and author_buttons.keyboard is None
    buttons = live_operations.request_review(
        draft.id,
        request_id="telegram:2:revisar",
        telegram_user_id=REVIEWER_TG,
        chat_id=REVIEWER_TG,
    )
    assert buttons.keyboard is not None
    assert revision.snapshot_hash[:12] in buttons.text
    approve_data = buttons.keyboard[0][0].callback_data
    assert approve_data.startswith("approve:")

    # El botón reemitido sigue el camino atómico existente del store.
    processor = TelegramWebhookProcessor(
        webhook_secret=TelegramWebhookSecret("secret"),
        update_store=store,
        authorizer=store,
        operations=live_operations,
        callback_nonces=store,
    )
    result = processor.process_update(
        {
            "update_id": 900,
            "callback_query": {
                "id": "q-900",
                "from": {"id": REVIEWER_TG},
                "message": {"message_id": 5, "chat": {"id": REVIEWER_TG, "type": "private"}},
                "data": approve_data,
            },
        },
        secret_token="secret",
    )
    assert "aprobado" in result.actions[1].text
    assert store.get_draft(draft.id, actor_id=team.owner.id).status_value is DraftStatus.APPROVED

    by_author = operations.withdraw_approval(
        draft.id, request_id="telegram:3:retirar", telegram_user_id=EDITOR_TG, chat_id=EDITOR_TG
    )
    assert by_author.accepted is False
    withdrawn = operations.withdraw_approval(
        draft.id,
        request_id="telegram:4:retirar",
        telegram_user_id=REVIEWER_TG,
        chat_id=REVIEWER_TG,
    )
    assert withdrawn.accepted is True
    refreshed = store.get_draft(draft.id, actor_id=team.owner.id)
    assert refreshed.status_value is DraftStatus.DRAFT
    assert refreshed.approved_revision_id is None
    assert any(
        event.action == "draft.approval_withdrawn"
        for event in store.list_audit_events(actor_id=team.owner.id)
    )

    # Con una solicitud en cola, retirar ya no es posible: la parada es el seguro live.
    queued_draft, queued_revision = approved_draft(store, team)
    store.enqueue_publication_request(
        queued_draft.id,
        actor_id=team.publisher.id,
        expected_snapshot_hash=queued_revision.snapshot_hash,
        idempotency_key="telegram:9:publicar",
        now=NOW,
    )
    blocked = operations.withdraw_approval(
        queued_draft.id,
        request_id="telegram:5:retirar",
        telegram_user_id=REVIEWER_TG,
        chat_id=REVIEWER_TG,
    )
    assert blocked.accepted is False and "seguro live" in blocked.text
    assert (
        store.get_draft(queued_draft.id, actor_id=team.owner.id).status_value
        is DraftStatus.APPROVED
    )


def test_reconciliation_as_published_needs_confirmation_and_marks_the_draft_published(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations
) -> None:
    draft, _revision, request = unknown_request(store, team, with_attempt=False)

    wrong_state = operations.request_reconciliation(
        "no-existe",
        published=True,
        provider_post_id="190000000000000001",
        request_id="telegram:1:cola",
        telegram_user_id=PUBLISHER_TG,
        chat_id=PUBLISHER_TG,
    )
    assert wrong_state.accepted is False
    prompt = operations.request_reconciliation(
        request.id,
        published=True,
        provider_post_id="190000000000000001",
        request_id="telegram:2:cola",
        telegram_user_id=PUBLISHER_TG,
        chat_id=PUBLISHER_TG,
    )
    assert prompt.keyboard is not None and "PUBLICADA" in prompt.text
    assert (
        store.get_publication_request(request.id, actor_id=team.owner.id).status_value
        is PublicationRequestStatus.UNKNOWN
    )

    applied = confirm(
        operations, prompt, ControlAction.RECONCILE_PUBLISHED, telegram_id=PUBLISHER_TG
    )
    assert applied.accepted is True
    reconciled = store.get_publication_request(request.id, actor_id=team.owner.id)
    assert reconciled.status_value is PublicationRequestStatus.SUCCEEDED
    attempt = store.get_publish_attempt(reconciled.publish_attempt_id, actor_id=team.owner.id)
    assert attempt.status_value is PublishStatus.SUCCEEDED
    assert attempt.provider_post_id == "190000000000000001"
    assert store.get_draft(draft.id, actor_id=team.owner.id).status_value is DraftStatus.PUBLISHED

    already = operations.request_reconciliation(
        request.id,
        published=False,
        provider_post_id=None,
        request_id="telegram:3:cola",
        telegram_user_id=PUBLISHER_TG,
        chat_id=PUBLISHER_TG,
    )
    assert already.accepted is False and "succeeded" in already.text
    queue = operations.list_queue(limit=5, telegram_user_id=AUDITOR_TG, chat_id=AUDITOR_TG)
    assert "succeeded — draft" in queue


def test_reconciliation_as_not_published_closes_the_pending_attempt_without_requeueing(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations
) -> None:
    draft, revision, request = unknown_request(store, team, with_attempt=True)

    prompt = operations.request_reconciliation(
        request.id,
        published=False,
        provider_post_id=None,
        request_id="telegram:1:cola",
        telegram_user_id=PUBLISHER_TG,
        chat_id=PUBLISHER_TG,
    )
    applied = confirm(
        operations, prompt, ControlAction.RECONCILE_NOT_PUBLISHED, telegram_id=PUBLISHER_TG
    )

    assert applied.accepted is True
    reconciled = store.get_publication_request(request.id, actor_id=team.owner.id)
    assert reconciled.status_value is PublicationRequestStatus.FAILED
    assert "no aparece en X" in reconciled.error
    attempt = store.get_publish_attempt(reconciled.publish_attempt_id, actor_id=team.owner.id)
    assert attempt.status_value is PublishStatus.FAILED
    assert store.get_draft(draft.id, actor_id=team.owner.id).status_value is DraftStatus.APPROVED
    with pytest.raises(ConflictError, match="otra clave de idempotencia"):
        store.enqueue_publication_request(
            draft.id,
            actor_id=team.publisher.id,
            expected_snapshot_hash=revision.snapshot_hash,
            idempotency_key="telegram:otra:publicar",
            now=NOW,
        )
    with pytest.raises(ConflictError, match="Solo se concilia"):
        store.reconcile_publication_request(
            request.id,
            actor_id=team.publisher.id,
            published=False,
            note="repetida",
            now=NOW,
        )
    with pytest.raises(ValueError, match="ID decimal"):
        store.reconcile_publication_request(
            request.id,
            actor_id=team.publisher.id,
            published=True,
            provider_post_id="abc",
            note="inválida",
            now=NOW,
        )


# ----------------------------------------------------------------------------
# Equipo
# ----------------------------------------------------------------------------


def test_team_commands_only_touch_delegable_roles_and_need_confirmation_to_deactivate(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations
) -> None:
    admin, _membership = store.create_team_member(
        actor_id=team.owner.id,
        email="admin@ops.test",
        display_name="Admin",
        role=Role.ADMIN,
        now=NOW,
    )

    self_deactivation = operations.request_user_deactivation(
        team.owner.id, request_id="telegram:1:baja", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    admin_deactivation = operations.request_user_deactivation(
        admin.id, request_id="telegram:2:baja", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    unknown = operations.request_user_deactivation(
        "no-existe", request_id="telegram:3:baja", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    assert self_deactivation.accepted is False
    assert admin_deactivation.accepted is False and "host" in admin_deactivation.text
    assert unknown.accepted is False

    prompt = operations.request_user_deactivation(
        team.editor.id, request_id="telegram:4:baja", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    assert prompt.keyboard is not None
    assert store.get_user(team.editor.id).is_active is True
    applied = confirm(operations, prompt, ControlAction.USER_DEACTIVATE, telegram_id=OWNER_TG)
    assert applied.accepted is True
    assert store.get_user(team.editor.id).is_active is False
    with pytest.raises(AuthorizationError):
        store.resolve_telegram_actor(telegram_user_id=EDITOR_TG, chat_id=EDITOR_TG)

    reactivated = operations.reactivate_user(
        team.editor.id, request_id="telegram:5:alta", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    assert reactivated.accepted is True
    assert store.resolve_telegram_actor(telegram_user_id=EDITOR_TG, chat_id=EDITOR_TG).is_active

    promoted = operations.change_user_role(
        team.editor.id,
        "reviewer",
        request_id="telegram:6:rol",
        telegram_user_id=OWNER_TG,
        chat_id=OWNER_TG,
    )
    assert promoted.accepted is True
    escalated = operations.change_user_role(
        team.editor.id,
        "admin",
        request_id="telegram:7:rol",
        telegram_user_id=OWNER_TG,
        chat_id=OWNER_TG,
    )
    assert escalated.accepted is False
    demoted_admin = operations.change_user_role(
        admin.id, "editor", request_id="telegram:8:rol", telegram_user_id=OWNER_TG, chat_id=OWNER_TG
    )
    assert demoted_admin.accepted is False
    roles = {
        membership.user_id: membership.role
        for membership in store.list_memberships(actor_id=team.owner.id)
    }
    assert roles[team.editor.id] == "reviewer"
    assert roles[admin.id] == "admin"


# ----------------------------------------------------------------------------
# Diagnóstico y auditoría
# ----------------------------------------------------------------------------


def test_doctor_and_audit_views_stay_read_only_and_role_bound(
    store: PlatformStore, team: Team, operations: PlatformTelegramOperations
) -> None:
    store.set_editorial_line("2026-08", "Materialismo y territorio.", actor_id=team.owner.id)
    unknown_request(store, team, with_attempt=False)

    report = operations.run_diagnostics(telegram_user_id=OWNER_TG, chat_id=OWNER_TG)
    assert "Base de datos: ok" in report
    assert "Seguros persistidos: live cerrado, direct cerrado" in report
    assert "Línea editorial 2026-08: fijada" in report
    assert "Agenda YAML desplegada: válida" in report
    assert "unknown 1" in report
    assert "x-whoami" in report

    editor_report = operations.run_diagnostics(telegram_user_id=EDITOR_TG, chat_id=EDITOR_TG)
    assert "Línea editorial 2026-08: fijada" in editor_report
    reviewer_report = operations.run_diagnostics(telegram_user_id=REVIEWER_TG, chat_id=REVIEWER_TG)
    assert "sin permiso para consultarla" in reviewer_report

    audit = operations.list_audit(limit=5, telegram_user_id=AUDITOR_TG, chat_id=AUDITOR_TG)
    assert audit.startswith("Últimos 5 evento(s)")
    denied = operations.list_audit(limit=5, telegram_user_id=EDITOR_TG, chat_id=EDITOR_TG)
    assert "No tienes permiso" in denied
    assert store.list_audit_events(actor_id=team.owner.id)[-1].action != "diagnostics"


# ----------------------------------------------------------------------------
# CLI del host: la misma capa persistida y el preflight del worker
# ----------------------------------------------------------------------------


def test_cli_safeguards_commands_share_the_persisted_layer(tmp_path: Path) -> None:
    runner = CliRunner()
    url = platform_database_url(tmp_path)
    with PlatformStore(url) as store:
        store.bootstrap_owner(email="owner@ops.test", display_name="Owner", user_id="owner-1")

    initial = runner.invoke(
        app, ["safeguards-status", "--actor-id", "owner-1", "--database-url", url]
    )
    opened = runner.invoke(
        app,
        [
            "safeguards-set",
            "live",
            "on",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "0",
            "--database-url",
            url,
        ],
    )
    stale = runner.invoke(
        app,
        [
            "safeguards-set",
            "direct",
            "on",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "0",
            "--database-url",
            url,
        ],
    )
    bogus = runner.invoke(
        app,
        [
            "safeguards-set",
            "fuego",
            "on",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "1",
            "--database-url",
            url,
        ],
    )

    assert initial.exit_code == 0, initial.output
    assert json.loads(initial.output)["version"] == 0
    assert opened.exit_code == 0, opened.output
    assert "live=sí" in opened.output and "version=1" in opened.output
    assert stale.exit_code == 1 and "versión actual 1" in stale.output
    assert bogus.exit_code == 1 and "live o direct" in bogus.output
    with PlatformStore(url) as store:
        safeguards = store.get_publication_safeguards(actor_id="owner-1")
        assert safeguards.live_enabled is True
        assert safeguards.direct_publish_enabled is False


def test_automation_run_direct_stops_before_claiming_when_persisted_safeguards_are_closed(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    url = platform_database_url(tmp_path)
    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("COLMAT_LIVE_ENABLED", "true")
    with PlatformStore(url) as store:
        owner, _membership = store.bootstrap_owner(
            email="owner@ops.test", display_name="Owner", user_id="owner-1"
        )
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            mode="direct",
            enabled=True,
            slots=[persisted_test_slot(mode="direct")],
            min_engagement_score=80,
        )
    monkeypatch.setenv("COLMAT_AUTOMATION_SCHEDULER_ID", "owner-1")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    result = runner.invoke(
        app,
        ["automation-run", "--live", "--database-url", url, "--policy", str(POLICY_PATH)],
    )

    assert result.exit_code == 1
    assert "seguro persistido live" in result.output
    with PlatformStore(url) as store:
        assert store.list_automation_runs(actor_id="owner-1") == []
        store.update_publication_safeguards(
            actor_id="owner-1", expected_version=0, live_enabled=True
        )
    direct_closed = runner.invoke(
        app,
        ["automation-run", "--live", "--database-url", url, "--policy", str(POLICY_PATH)],
    )
    assert direct_closed.exit_code == 1
    assert "seguro persistido direct" in direct_closed.output


def test_review_callback_data_helper_matches_reissued_buttons() -> None:
    assert approval_callback_data("a_secure_nonce_1234567890").startswith("approve:")
