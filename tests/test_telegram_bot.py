from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from colmat_x.telegram_bot import (
    AnswerCallbackQuery,
    BotPermission,
    CallbackDecision,
    CallbackIntent,
    DecisionResult,
    EditMessageText,
    MalformedTelegramUpdate,
    SendMessage,
    TelegramWebhookProcessor,
    TelegramWebhookSecret,
    WebhookAuthenticationError,
    approval_callback_data,
    execute_bot_actions,
    rejection_callback_data,
)

SECRET = "telegram_webhook_secret-2026"
NONCE = "a_secure_nonce_1234567890"
SNAPSHOT = "a" * 64


@dataclass
class MemoryUpdates:
    seen: set[int] = field(default_factory=set)
    calls: list[int] = field(default_factory=list)
    contexts: list[tuple[object, int | None, int | None]] = field(default_factory=list)

    def claim_update(
        self,
        update_id: int,
        *,
        payload: object,
        telegram_user_id: int | None,
        chat_id: int | None,
    ) -> bool:
        self.calls.append(update_id)
        self.contexts.append((payload, telegram_user_id, chat_id))
        if update_id in self.seen:
            return False
        self.seen.add(update_id)
        return True


@dataclass
class MemoryAuthorizer:
    allowed: set[tuple[int, int, BotPermission]]
    calls: list[tuple[int, int, BotPermission]] = field(default_factory=list)

    def is_allowed(
        self,
        *,
        telegram_user_id: int,
        chat_id: int,
        permission: BotPermission,
    ) -> bool:
        key = (telegram_user_id, chat_id, permission)
        self.calls.append(key)
        return key in self.allowed


@dataclass
class MemoryNonces:
    intents: dict[str, CallbackIntent] = field(default_factory=dict)
    calls: list[tuple[str, CallbackDecision, int, int]] = field(default_factory=list)

    def consume_callback_nonce(
        self,
        nonce: str,
        *,
        decision: CallbackDecision,
        telegram_user_id: int,
        chat_id: int,
    ) -> CallbackIntent | None:
        self.calls.append((nonce, decision, telegram_user_id, chat_id))
        intent = self.intents.get(nonce)
        if intent is None or intent.decision is not decision:
            return None
        return self.intents.pop(nonce)


@dataclass
class FakeOperations:
    calls: list[tuple[object, ...]] = field(default_factory=list)
    decision_accepted: bool = True

    def get_status(self, *, telegram_user_id: int, chat_id: int) -> str:
        self.calls.append(("status", telegram_user_id, chat_id))
        return "2 borradores; 1 pendiente de aprobación"

    def get_team(self, *, telegram_user_id: int, chat_id: int) -> str:
        self.calls.append(("team", telegram_user_id, chat_id))
        return "Administración: 1; Editores: 2"

    def approve_post(
        self,
        *,
        post_id: str,
        snapshot_hash: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> DecisionResult:
        self.calls.append(("approve", post_id, snapshot_hash, telegram_user_id, chat_id))
        return DecisionResult(
            f"{post_id} fue aprobado; aún no se ha publicado.",
            accepted=self.decision_accepted,
        )

    def reject_post(
        self,
        *,
        post_id: str,
        snapshot_hash: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> DecisionResult:
        self.calls.append(("reject", post_id, snapshot_hash, telegram_user_id, chat_id))
        return DecisionResult(
            f"{post_id} fue rechazado.",
            accepted=self.decision_accepted,
        )


@dataclass
class FakeActionClient:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> object:
        self.calls.append(("send", chat_id, text, reply_to_message_id))
        return {"message_id": 1}

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: object = None,
    ) -> object:
        self.calls.append(("edit", chat_id, message_id, text, reply_markup))
        return {"message_id": message_id}

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> object:
        self.calls.append(("answer", callback_query_id, text, show_alert))
        return True


def message_update(
    update_id: int,
    text: str,
    *,
    user_id: int = 101,
    chat_id: int = -202,
    username: str = "mutable_name",
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 9,
            "from": {"id": user_id, "username": username},
            "chat": {"id": chat_id, "type": "supergroup"},
            "text": text,
        },
    }


def callback_update(
    update_id: int,
    data: str,
    *,
    user_id: int = 101,
    chat_id: int = -202,
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"query-{update_id}",
            "from": {"id": user_id, "username": "mutable_name"},
            "message": {
                "message_id": 17,
                "chat": {"id": chat_id, "type": "supergroup"},
            },
            "data": data,
        },
    }


def make_processor(
    *,
    permissions: set[BotPermission] | None = None,
    user_id: int = 101,
    chat_id: int = -202,
    updates: MemoryUpdates | None = None,
    nonces: MemoryNonces | None = None,
    operations: FakeOperations | None = None,
) -> tuple[
    TelegramWebhookProcessor,
    MemoryUpdates,
    MemoryAuthorizer,
    MemoryNonces,
    FakeOperations,
]:
    update_store = updates or MemoryUpdates()
    authorizer = MemoryAuthorizer(
        {
            (user_id, chat_id, permission)
            for permission in (permissions if permissions is not None else set(BotPermission))
        }
    )
    nonce_store = nonces or MemoryNonces()
    bot_operations = operations or FakeOperations()
    return (
        TelegramWebhookProcessor(
            webhook_secret=TelegramWebhookSecret(SECRET),
            update_store=update_store,
            authorizer=authorizer,
            operations=bot_operations,
            callback_nonces=nonce_store,
        ),
        update_store,
        authorizer,
        nonce_store,
        bot_operations,
    )


def test_webhook_secret_is_repr_safe_and_checked_before_store_access() -> None:
    processor, updates, _, _, _ = make_processor()

    assert SECRET not in repr(TelegramWebhookSecret(SECRET))
    assert SECRET not in repr(processor)
    with pytest.raises(WebhookAuthenticationError, match="no coincide"):
        processor.process_update(message_update(1, "/start"), secret_token="incorrect")

    assert updates.calls == []


def test_duplicate_update_is_idempotent_and_has_no_second_side_effect() -> None:
    processor, updates, _, _, operations = make_processor()
    update = message_update(5, "/estado")

    first = processor.process_update(update, secret_token=SECRET)
    second = processor.process_update(update, secret_token=SECRET)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.actions == ()
    assert updates.calls == [5, 5]
    assert operations.calls == [("status", 101, -202)]
    assert updates.contexts[0] == (update, 101, -202)


def test_status_uses_numeric_user_and_chat_identity_not_username() -> None:
    processor, _, authorizer, _, operations = make_processor()

    result = processor.process_update(
        message_update(10, "/estado@ColmatBot", username="same_for_everyone"),
        secret_token=SECRET,
    )

    assert result.actions == (
        SendMessage(
            telegram_user_id=101,
            chat_id=-202,
            text="2 borradores; 1 pendiente de aprobación",
            reply_to_message_id=9,
        ),
    )
    assert operations.calls == [("status", 101, -202)]
    assert authorizer.calls == [
        (101, -202, BotPermission.ACCESS),
        (101, -202, BotPermission.VIEW_STATUS),
    ]


def test_same_username_does_not_grant_a_different_user_access() -> None:
    processor, _, _, _, operations = make_processor(user_id=101)

    result = processor.process_update(
        message_update(11, "/estado", user_id=999, username="mutable_name"),
        secret_token=SECRET,
    )

    action = result.actions[0]
    assert isinstance(action, SendMessage)
    assert action.telegram_user_id == 999
    assert "no autorizado" in action.text.casefold()
    assert operations.calls == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/start", "está conectado"),
        ("/ayuda", "no crea usuarios"),
        ("/help", "no crea usuarios"),
        ("/desconocido", "comando desconocido"),
    ],
)
def test_safe_informational_commands(command: str, expected: str) -> None:
    processor, _, _, _, operations = make_processor()

    update_id = abs(hash(command))
    result = processor.process_update(message_update(update_id, command), secret_token=SECRET)

    action = result.actions[0]
    assert isinstance(action, SendMessage)
    assert expected in action.text.casefold()
    assert operations.calls == []


def test_team_requires_its_own_rbac_permission() -> None:
    processor, _, _, _, operations = make_processor(permissions={BotPermission.ACCESS})

    result = processor.process_update(message_update(20, "/equipo"), secret_token=SECRET)

    action = result.actions[0]
    assert isinstance(action, SendMessage)
    assert "no tienes permiso" in action.text.casefold()
    assert operations.calls == []


def test_approval_callback_consumes_bound_nonce_and_only_approves() -> None:
    intent = CallbackIntent(
        CallbackDecision.APPROVE,
        "colmat-post-001",
        SNAPSHOT,
        telegram_user_id=101,
        chat_id=-202,
    )
    nonces = MemoryNonces({NONCE: intent})
    processor, _, _, nonce_store, operations = make_processor(nonces=nonces)

    result = processor.process_update(
        callback_update(30, approval_callback_data(NONCE)),
        secret_token=SECRET,
    )

    assert nonce_store.calls == [(NONCE, CallbackDecision.APPROVE, 101, -202)]
    assert operations.calls == [("approve", "colmat-post-001", SNAPSHOT, 101, -202)]
    assert result.actions == (
        AnswerCallbackQuery(
            telegram_user_id=101,
            chat_id=-202,
            callback_query_id="query-30",
            text="Aprobación registrada. La publicación seguirá los controles de la cola.",
        ),
        EditMessageText(
            telegram_user_id=101,
            chat_id=-202,
            message_id=17,
            text="colmat-post-001 fue aprobado; aún no se ha publicado.",
        ),
    )
    assert not hasattr(operations, "publish_post")


def test_reject_callback_uses_separate_permission_and_operation() -> None:
    intent = CallbackIntent(
        CallbackDecision.REJECT,
        "colmat-post-002",
        SNAPSHOT,
        telegram_user_id=101,
        chat_id=-202,
    )
    nonces = MemoryNonces({NONCE: intent})
    processor, _, _, _, operations = make_processor(
        permissions={BotPermission.ACCESS, BotPermission.REJECT},
        nonces=nonces,
    )

    result = processor.process_update(
        callback_update(31, rejection_callback_data(NONCE)),
        secret_token=SECRET,
    )

    assert operations.calls == [("reject", "colmat-post-002", SNAPSHOT, 101, -202)]
    assert isinstance(result.actions[0], AnswerCallbackQuery)
    assert isinstance(result.actions[1], EditMessageText)


def test_expected_approval_conflict_is_reported_without_a_false_success() -> None:
    intent = CallbackIntent(
        CallbackDecision.APPROVE,
        "colmat-post-002",
        SNAPSHOT,
        telegram_user_id=101,
        chat_id=-202,
    )
    nonces = MemoryNonces({NONCE: intent})
    operations = FakeOperations(decision_accepted=False)
    processor, _, _, _, _ = make_processor(nonces=nonces, operations=operations)

    result = processor.process_update(
        callback_update(36, approval_callback_data(NONCE)),
        secret_token=SECRET,
    )

    answer = result.actions[0]
    assert isinstance(answer, AnswerCallbackQuery)
    assert answer.show_alert is True
    assert answer.text == "No se registró la aprobación."


def test_unauthorized_callback_cannot_burn_a_nonce() -> None:
    intent = CallbackIntent(
        CallbackDecision.APPROVE,
        "colmat-post-003",
        SNAPSHOT,
        telegram_user_id=101,
        chat_id=-202,
    )
    nonces = MemoryNonces({NONCE: intent})
    processor, _, _, nonce_store, operations = make_processor(
        permissions={BotPermission.ACCESS},
        nonces=nonces,
    )

    result = processor.process_update(
        callback_update(32, approval_callback_data(NONCE)),
        secret_token=SECRET,
    )

    action = result.actions[0]
    assert isinstance(action, AnswerCallbackQuery)
    assert action.show_alert is True
    assert "permiso" in action.text.casefold()
    assert nonce_store.calls == []
    assert NONCE in nonce_store.intents
    assert operations.calls == []


def test_expired_or_wrong_decision_nonce_never_reaches_operations() -> None:
    intent = CallbackIntent(
        CallbackDecision.REJECT,
        "colmat-post-004",
        SNAPSHOT,
        telegram_user_id=101,
        chat_id=-202,
    )
    nonces = MemoryNonces({NONCE: intent})
    processor, _, _, _, operations = make_processor(nonces=nonces)

    result = processor.process_update(
        callback_update(33, approval_callback_data(NONCE)),
        secret_token=SECRET,
    )

    action = result.actions[0]
    assert isinstance(action, AnswerCallbackQuery)
    assert action.show_alert is True
    assert "vencida" in action.text.casefold()
    assert operations.calls == []


def test_nonce_intent_must_be_bound_to_numeric_user_and_chat() -> None:
    intent = CallbackIntent(
        CallbackDecision.APPROVE,
        "colmat-post-004",
        SNAPSHOT,
        telegram_user_id=999,
        chat_id=-202,
    )
    nonces = MemoryNonces({NONCE: intent})
    processor, _, _, _, operations = make_processor(nonces=nonces)

    result = processor.process_update(
        callback_update(35, approval_callback_data(NONCE), user_id=101),
        secret_token=SECRET,
    )

    action = result.actions[0]
    assert isinstance(action, AnswerCallbackQuery)
    assert action.show_alert is True
    assert operations.calls == []


def test_replayed_callback_update_does_not_consume_nonce_twice() -> None:
    intent = CallbackIntent(
        CallbackDecision.APPROVE,
        "colmat-post-005",
        SNAPSHOT,
        telegram_user_id=101,
        chat_id=-202,
    )
    nonces = MemoryNonces({NONCE: intent})
    processor, _, _, nonce_store, operations = make_processor(nonces=nonces)
    update = callback_update(34, approval_callback_data(NONCE))

    processor.process_update(update, secret_token=SECRET)
    duplicate = processor.process_update(update, secret_token=SECRET)

    assert duplicate.duplicate is True
    assert len(nonce_store.calls) == 1
    assert len(operations.calls) == 1


@pytest.mark.parametrize(
    "data",
    [
        "approve:short",
        "publish:a_secure_nonce_1234567890",
        "approve:a_secure_nonce_1234567890:extra",
        "approve:" + "a" * 60,
    ],
)
def test_malformed_callback_data_is_inert(data: str) -> None:
    processor, _, _, nonces, operations = make_processor()

    update_id = abs(hash(data))
    result = processor.process_update(callback_update(update_id, data), secret_token=SECRET)

    action = result.actions[0]
    assert isinstance(action, AnswerCallbackQuery)
    assert action.show_alert is True
    assert nonces.calls == []
    assert operations.calls == []


def test_noncommand_text_is_claimed_but_has_no_action() -> None:
    processor, updates, _, _, operations = make_processor()

    result = processor.process_update(message_update(50, "hola"), secret_token=SECRET)

    assert result.actions == ()
    assert updates.seen == {50}
    assert operations.calls == []


def test_malformed_update_never_reaches_idempotency_store() -> None:
    processor, updates, _, _, _ = make_processor()

    with pytest.raises(MalformedTelegramUpdate, match="from y chat"):
        processor.process_update(
            {"update_id": 60, "message": {"message_id": 1, "text": "/start"}},
            secret_token=SECRET,
        )

    assert updates.calls == []


def test_callback_helpers_enforce_telegram_size_and_nonce_format() -> None:
    assert approval_callback_data(NONCE) == f"approve:{NONCE}"
    assert rejection_callback_data(NONCE) == f"reject:{NONCE}"
    assert len(approval_callback_data(NONCE).encode()) <= 64

    with pytest.raises(ValueError, match="formato seguro"):
        approval_callback_data("predictable")


def test_execute_bot_actions_dispatches_without_network_and_removes_buttons() -> None:
    client = FakeActionClient()
    actions = (
        SendMessage(101, -202, "Estado", reply_to_message_id=7),
        AnswerCallbackQuery(101, -202, "query-70", "Registrado", show_alert=False),
        EditMessageText(101, -202, 17, "Aprobado"),
    )

    results = execute_bot_actions(client, actions)

    assert results == ({"message_id": 1}, True, {"message_id": 17})
    assert client.calls == [
        ("send", -202, "Estado", 7),
        ("answer", "query-70", "Registrado", False),
        ("edit", -202, 17, "Aprobado", {"inline_keyboard": []}),
    ]


def test_execute_bot_actions_rejects_unknown_type_before_any_side_effect() -> None:
    client = FakeActionClient()

    with pytest.raises(TypeError, match="desconocida"):
        execute_bot_actions(
            client,
            [SendMessage(101, -202, "No debe enviarse"), object()],  # type: ignore[list-item]
        )

    assert client.calls == []
