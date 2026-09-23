"""Comandos de control operativo del bot: rol mínimo, chat, duplicados y confirmaciones.

Estas pruebas cubren el procesador con dobles en memoria; la integración con el
store real vive en ``tests/test_telegram_operations.py``.
"""

from __future__ import annotations

import pytest

from colmat_x.telegram_bot import (
    CONTROL_ACTION_PERMISSIONS,
    AnswerCallbackQuery,
    BotPermission,
    CommandResult,
    ControlAction,
    EditMessageText,
    InlineButton,
    SafeguardGate,
    SendMessage,
    approval_callback_data,
    cancellation_callback_data,
    confirmation_callback_data,
    confirmation_keyboard,
    deserialize_bot_actions,
    execute_bot_actions,
    rejection_callback_data,
    serialize_bot_actions,
)
from tests.test_telegram_bot import (
    NONCE,
    SECRET,
    FakeActionClient,
    FakeOperations,
    callback_update,
    make_processor,
    message_update,
)

REQUEST_ID = "550e8400-e29b-41d4-a716-446655440000"


def private_processor(**kwargs):
    return make_processor(user_id=101, chat_id=101, **kwargs)


def private_update(update_id: int, text: str) -> dict[str, object]:
    return message_update(update_id, text, user_id=101, chat_id=101)


def test_help_lists_every_control_command() -> None:
    processor, _, _, _, operations = make_processor()

    result = processor.process_update(message_update(1, "/ayuda"), secret_token=SECRET)

    text = result.actions[0].text
    for command in ("/agenda", "/seguros", "/seguro", "/cola", "/revisar", "/retirar"):
        assert command in text
    for command in ("/baja", "/alta", "/rol", "/doctor", "/auditoria"):
        assert command in text
    assert operations.calls == []


@pytest.mark.parametrize(
    ("command", "permission", "operation"),
    [
        ("/agenda", BotPermission.VIEW_AUTOMATION, "automation_status"),
        ("/agenda runs 3", BotPermission.VIEW_AUTOMATION, "automation_runs"),
        ("/agenda validar", BotPermission.VIEW_AUTOMATION, "automation_validate"),
        ("/agenda pausar", BotPermission.MANAGE_SCHEDULE, "automation_disable"),
        ("/agenda sincronizar", BotPermission.MANAGE_SCHEDULE, "automation_sync"),
        ("/agenda activar", BotPermission.MANAGE_SCHEDULE, "automation_enable"),
        (f"/agenda reintentar {REQUEST_ID}", BotPermission.MANAGE_SCHEDULE, "automation_retry"),
        ("/seguros", BotPermission.VIEW_SAFEGUARDS, "safeguards"),
        ("/seguro live off", BotPermission.MANAGE_SAFEGUARDS, "safeguard"),
        ("/cola", BotPermission.VIEW_QUEUE, "queue"),
        (f"/cola conciliar {REQUEST_ID} no_publicado", BotPermission.RECONCILE, "reconcile"),
        ("/retirar draft-001", BotPermission.WITHDRAW, "withdraw"),
        ("/revisar draft-001", BotPermission.REQUEST_REVIEW, "review"),
        ("/baja user-001", BotPermission.MANAGE_TEAM, "deactivate"),
        ("/alta user-001", BotPermission.MANAGE_TEAM, "reactivate"),
        ("/rol user-001 editor", BotPermission.MANAGE_TEAM, "role"),
        ("/doctor", BotPermission.VIEW_DIAGNOSTICS, "doctor"),
        ("/auditoria", BotPermission.VIEW_AUDIT, "audit"),
        ("/modo direct", BotPermission.MANAGE_MODE, "direct_mode"),
    ],
)
def test_each_control_command_declares_and_enforces_its_minimum_permission(
    command: str,
    permission: BotPermission,
    operation: str,
) -> None:
    processor, _, authorizer, _, operations = private_processor(
        permissions={BotPermission.ACCESS},
    )

    result = processor.process_update(private_update(30, command), secret_token=SECRET)

    action = result.actions[0]
    assert isinstance(action, SendMessage)
    assert "no tienes permiso" in action.text.casefold()
    assert (101, 101, permission) in authorizer.calls
    assert all(call[0] != operation for call in operations.calls)


@pytest.mark.parametrize(
    "command",
    [
        "/agenda sincronizar",
        "/agenda activar",
        f"/agenda reintentar {REQUEST_ID}",
        "/seguro live on",
        "/seguro direct on",
        f"/cola conciliar {REQUEST_ID} publicado 190000000000000001",
        f"/cola conciliar {REQUEST_ID} no_publicado",
        "/baja user-001",
        "/alta user-001",
        "/rol user-001 reviewer",
        "/modo direct",
    ],
)
def test_sensitive_control_commands_are_refused_outside_private_chats(command: str) -> None:
    processor, _, _, _, operations = make_processor()

    result = processor.process_update(message_update(40, command), secret_token=SECRET)

    action = result.actions[0]
    assert isinstance(action, SendMessage)
    assert "chat privado" in action.text
    assert operations.calls == []


def test_safe_direction_commands_are_allowed_from_any_authorized_chat() -> None:
    processor, _, _, _, operations = make_processor()

    paused = processor.process_update(message_update(41, "/agenda pausar"), secret_token=SECRET)
    closed = processor.process_update(message_update(42, "/seguro live off"), secret_token=SECRET)

    assert paused.actions[0].text == "Agenda pausada"
    assert closed.actions[0].text == "Seguro cerrado"
    assert operations.calls == [
        ("automation_disable", "telegram:41:agenda:pausar", 101, -202),
        ("safeguard", SafeguardGate.LIVE, False, "telegram:42:seguro:live:off", 101, -202),
    ]


def test_agenda_subcommands_dispatch_with_bounded_arguments() -> None:
    processor, _, _, _, operations = private_processor()

    responses = [
        processor.process_update(private_update(50 + index, command), secret_token=SECRET)
        .actions[0]
        .text
        for index, command in enumerate(
            (
                "/agenda",
                "/agenda runs",
                "/agenda runs 20",
                "/agenda runs 21",
                "/agenda validar",
                "/agenda sincronizar",
                "/agenda activar",
                f"/agenda reintentar {REQUEST_ID}",
                "/agenda reintentar ../run",
                "/agenda desconocido",
            )
        )
    ]

    assert responses[0] == "Agenda persistida: pausada"
    assert responses[1] == "Runs: 5"
    assert responses[2] == "Runs: 20"
    assert responses[3].startswith("Uso: /agenda")
    assert responses[4] == "Agenda YAML válida"
    assert responses[5] == "Agenda sincronizada"
    assert responses[6] == "Confirma activar la agenda"
    assert responses[7] == "Reintento solicitado"
    assert responses[8].startswith("Uso: /agenda")
    assert responses[9].startswith("Uso: /agenda")
    assert operations.calls == [
        ("automation_status", 101, 101),
        ("automation_runs", 5, 101, 101),
        ("automation_runs", 20, 101, 101),
        ("automation_validate", 101, 101),
        ("automation_sync", "telegram:55:agenda:sincronizar", 101, 101),
        ("automation_enable", "telegram:56:agenda:activar", 101, 101),
        ("automation_retry", REQUEST_ID, "telegram:57:agenda:reintentar", 101, 101),
    ]


def test_enable_confirmation_keyboard_reaches_the_reply() -> None:
    processor, _, _, _, _ = private_processor()

    result = processor.process_update(private_update(60, "/agenda activar"), secret_token=SECRET)

    action = result.actions[0]
    assert isinstance(action, SendMessage)
    assert action.keyboard == confirmation_keyboard(ControlAction.AGENDA_ENABLE, NONCE)


def test_safeguard_open_returns_confirmation_and_close_applies_immediately() -> None:
    processor, _, _, _, operations = private_processor()

    opened = processor.process_update(private_update(70, "/seguro live on"), secret_token=SECRET)
    closed = processor.process_update(private_update(71, "/seguro direct off"), secret_token=SECRET)
    malformed = processor.process_update(
        private_update(72, "/seguro live quizás"), secret_token=SECRET
    )

    assert opened.actions[0].keyboard == confirmation_keyboard(
        ControlAction.SAFEGUARD_LIVE_ON, NONCE
    )
    assert closed.actions[0].keyboard is None
    assert malformed.actions[0].text == "Uso: /seguro <live|direct> <on|off>"
    assert operations.calls == [
        ("safeguard", SafeguardGate.LIVE, True, "telegram:70:seguro:live:on", 101, 101),
        ("safeguard", SafeguardGate.DIRECT, False, "telegram:71:seguro:direct:off", 101, 101),
    ]


def test_queue_listing_and_reconciliation_arguments_are_closed() -> None:
    processor, _, _, _, operations = private_processor()

    listing = processor.process_update(private_update(80, "/cola"), secret_token=SECRET)
    bounded = processor.process_update(private_update(81, "/cola 5"), secret_token=SECRET)
    too_many = processor.process_update(private_update(82, "/cola 21"), secret_token=SECRET)
    published = processor.process_update(
        private_update(83, f"/cola conciliar {REQUEST_ID} publicado 190000000000000001"),
        secret_token=SECRET,
    )
    not_published = processor.process_update(
        private_update(84, f"/cola conciliar {REQUEST_ID} no_publicado"),
        secret_token=SECRET,
    )
    bad_post_id = processor.process_update(
        private_update(85, f"/cola conciliar {REQUEST_ID} publicado 0123"),
        secret_token=SECRET,
    )
    missing_post_id = processor.process_update(
        private_update(86, f"/cola conciliar {REQUEST_ID} publicado"),
        secret_token=SECRET,
    )

    assert listing.actions[0].text == "Cola: 10"
    assert bounded.actions[0].text == "Cola: 5"
    assert too_many.actions[0].text.startswith("Uso: /cola")
    assert published.actions[0].keyboard == confirmation_keyboard(
        ControlAction.RECONCILE_PUBLISHED, NONCE
    )
    assert not_published.actions[0].keyboard == confirmation_keyboard(
        ControlAction.RECONCILE_NOT_PUBLISHED, NONCE
    )
    assert bad_post_id.actions[0].text.startswith("Uso: /cola conciliar")
    assert missing_post_id.actions[0].text.startswith("Uso: /cola conciliar")
    assert operations.calls == [
        ("queue", 10, 101, 101),
        ("queue", 5, 101, 101),
        (
            "reconcile",
            REQUEST_ID,
            True,
            "190000000000000001",
            "telegram:83:cola:conciliar",
            101,
            101,
        ),
        ("reconcile", REQUEST_ID, False, None, "telegram:84:cola:conciliar", 101, 101),
    ]


def test_team_commands_validate_ids_and_only_delegable_roles() -> None:
    processor, _, _, _, operations = private_processor()

    owner_role = processor.process_update(
        private_update(90, "/rol user-001 owner"), secret_token=SECRET
    )
    admin_role = processor.process_update(
        private_update(91, "/rol user-001 admin"), secret_token=SECRET
    )
    changed = processor.process_update(
        private_update(92, "/rol user-001 reviewer"), secret_token=SECRET
    )
    deactivate = processor.process_update(private_update(93, "/baja user-001"), secret_token=SECRET)
    reactivate = processor.process_update(private_update(94, "/alta user-001"), secret_token=SECRET)
    missing = processor.process_update(private_update(95, "/baja"), secret_token=SECRET)
    unsafe = processor.process_update(private_update(96, "/alta ../user"), secret_token=SECRET)

    assert owner_role.actions[0].text.startswith("Uso: /rol")
    assert admin_role.actions[0].text.startswith("Uso: /rol")
    assert changed.actions[0].text == "Rol cambiado"
    assert deactivate.actions[0].keyboard == confirmation_keyboard(
        ControlAction.USER_DEACTIVATE, NONCE
    )
    assert reactivate.actions[0].text == "Integrante reactivado"
    assert missing.actions[0].text == "Uso: /baja <user_id>"
    assert unsafe.actions[0].text == "Uso: /alta <user_id>"
    assert operations.calls == [
        ("role", "user-001", "reviewer", "telegram:92:rol", 101, 101),
        ("deactivate", "user-001", "telegram:93:baja", 101, 101),
        ("reactivate", "user-001", "telegram:94:alta", 101, 101),
    ]


def test_review_and_withdraw_commands_work_from_review_groups() -> None:
    processor, _, _, _, operations = make_processor()

    review = processor.process_update(
        message_update(100, "/revisar draft-001"), secret_token=SECRET
    )
    withdrawn = processor.process_update(
        message_update(101, "/retirar draft-001"), secret_token=SECRET
    )
    malformed = processor.process_update(
        message_update(102, "/revisar draft:1"), secret_token=SECRET
    )

    assert review.actions[0].keyboard == (
        (
            InlineButton("Aprobar", approval_callback_data(NONCE)),
            InlineButton("Rechazar", rejection_callback_data(NONCE)),
        ),
    )
    assert withdrawn.actions[0].text == "Aprobación retirada"
    assert malformed.actions[0].text.startswith("Uso: /revisar")
    assert operations.calls == [
        ("review", "draft-001", "telegram:100:revisar", 101, -202),
        ("withdraw", "draft-001", "telegram:101:retirar", 101, -202),
    ]


def test_doctor_and_audit_are_read_only_views() -> None:
    processor, _, _, _, operations = make_processor()

    doctor = processor.process_update(message_update(110, "/doctor"), secret_token=SECRET)
    audit = processor.process_update(message_update(111, "/auditoria 50"), secret_token=SECRET)
    too_many = processor.process_update(message_update(112, "/auditoria 51"), secret_token=SECRET)

    assert doctor.actions[0].text == "Diagnóstico ok"
    assert audit.actions[0].text == "Auditoría: 50"
    assert too_many.actions[0].text.startswith("Uso: /auditoria")
    assert operations.calls == [("doctor", 101, -202), ("audit", 50, 101, -202)]


def test_duplicate_control_update_has_no_second_side_effect() -> None:
    processor, updates, _, _, operations = private_processor()
    update = private_update(120, "/agenda sincronizar")

    first = processor.process_update(update, secret_token=SECRET)
    second = processor.process_update(update, secret_token=SECRET)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.actions == ()
    assert updates.calls == [120, 120]
    assert operations.calls == [("automation_sync", "telegram:120:agenda:sincronizar", 101, 101)]


@pytest.mark.parametrize("action", list(ControlAction))
def test_confirmation_callbacks_reuse_the_permission_of_their_order(
    action: ControlAction,
) -> None:
    permission = CONTROL_ACTION_PERMISSIONS[action]
    processor, _, authorizer, _, operations = private_processor(
        permissions={BotPermission.ACCESS},
    )

    result = processor.process_update(
        callback_update(130, confirmation_callback_data(action, NONCE), user_id=101, chat_id=101),
        secret_token=SECRET,
    )

    denial = result.actions[0]
    assert isinstance(denial, AnswerCallbackQuery)
    assert denial.show_alert is True
    assert "permiso" in denial.text.casefold()
    assert (101, 101, permission) in authorizer.calls
    assert operations.calls == []


def test_confirmation_callback_is_private_only_single_use_and_cancellable() -> None:
    group_processor, _, _, _, group_operations = make_processor()
    grouped = group_processor.process_update(
        callback_update(140, confirmation_callback_data(ControlAction.SAFEGUARD_LIVE_ON, NONCE)),
        secret_token=SECRET,
    )

    processor, _, _, _, operations = private_processor()
    update = callback_update(
        141,
        confirmation_callback_data(ControlAction.SAFEGUARD_LIVE_ON, NONCE),
        user_id=101,
        chat_id=101,
    )
    confirmed = processor.process_update(update, secret_token=SECRET)
    replayed = processor.process_update(update, secret_token=SECRET)
    cancelled = processor.process_update(
        callback_update(
            142,
            cancellation_callback_data(ControlAction.USER_DEACTIVATE, NONCE),
            user_id=101,
            chat_id=101,
        ),
        secret_token=SECRET,
    )

    assert isinstance(grouped.actions[0], AnswerCallbackQuery)
    assert "chat privado" in grouped.actions[0].text
    assert group_operations.calls == []
    assert confirmed.actions == (
        AnswerCallbackQuery(
            telegram_user_id=101,
            chat_id=101,
            callback_query_id="query-141",
            text="Orden confirmada y registrada.",
        ),
        EditMessageText(telegram_user_id=101, chat_id=101, message_id=17, text="Orden aplicada"),
    )
    assert replayed.duplicate is True
    assert cancelled.actions[0].text == "Orden cancelada; no se aplicó ningún cambio."
    assert operations.calls == [
        (
            "confirmation",
            NONCE,
            ControlAction.SAFEGUARD_LIVE_ON,
            True,
            "telegram:141:confirm:live_on",
            101,
            101,
        ),
        (
            "confirmation",
            NONCE,
            ControlAction.USER_DEACTIVATE,
            False,
            "telegram:142:cancel:user_off",
            101,
            101,
        ),
    ]


def test_rejected_confirmation_is_reported_as_an_alert_without_false_success() -> None:
    operations = FakeOperations(command_accepted=False)
    processor, _, _, _, _ = private_processor(operations=operations)

    result = processor.process_update(
        callback_update(
            150,
            confirmation_callback_data(ControlAction.MODE_DIRECT, NONCE),
            user_id=101,
            chat_id=101,
        ),
        secret_token=SECRET,
    )

    answer = result.actions[0]
    assert isinstance(answer, AnswerCallbackQuery)
    assert answer.show_alert is True
    assert answer.text == "No se aplicó la orden."


@pytest.mark.parametrize(
    "data",
    [
        "confirm:unknown_action:a_secure_nonce_1234567890",
        "confirm:live_on:short",
        "confirm:live_on:a_secure_nonce_1234567890:extra",
        "approve:live_on:a_secure_nonce_1234567890",
        "confirm::a_secure_nonce_1234567890",
    ],
)
def test_malformed_control_callback_data_is_inert(data: str) -> None:
    processor, _, _, nonces, operations = private_processor()

    result = processor.process_update(
        callback_update(160 + len(data), data, user_id=101, chat_id=101),
        secret_token=SECRET,
    )

    action = result.actions[0]
    assert isinstance(action, AnswerCallbackQuery)
    assert action.show_alert is True
    assert nonces.calls == []
    assert operations.calls == []


def test_keyboard_round_trips_through_durable_actions_and_the_client() -> None:
    keyboard = confirmation_keyboard(ControlAction.SAFEGUARD_DIRECT_ON, NONCE)
    action = SendMessage(101, 101, "Confirma", reply_to_message_id=9, keyboard=keyboard)
    plain = SendMessage(101, 101, "Estado", reply_to_message_id=7)

    serialized = serialize_bot_actions((action, plain))
    restored = deserialize_bot_actions(serialized)

    assert restored == (action, plain)
    assert "keyboard" not in serialized[1]

    class KeyboardClient(FakeActionClient):
        def send_message(self, chat_id, text, *, reply_to_message_id=None, reply_markup=None):
            self.calls.append(("send", chat_id, text, reply_to_message_id, reply_markup))
            return {"message_id": 1}

    client = KeyboardClient()
    execute_bot_actions(client, restored)
    assert client.calls[0] == (
        "send",
        101,
        "Confirma",
        9,
        {
            "inline_keyboard": [
                [
                    {
                        "text": "Confirmar",
                        "callback_data": f"confirm:direct_on:{NONCE}",
                    },
                    {
                        "text": "Cancelar",
                        "callback_data": f"cancel:direct_on:{NONCE}",
                    },
                ]
            ]
        },
    )
    assert client.calls[1] == ("send", 101, "Estado", 7, None)

    # Un cliente sin soporte de teclados sigue recibiendo los mensajes planos sin kwargs.
    legacy = FakeActionClient()
    execute_bot_actions(legacy, (plain,))
    assert legacy.calls == [("send", 101, "Estado", 7)]


def test_keyboards_only_accept_the_closed_callback_schema() -> None:
    with pytest.raises(ValueError, match="esquema cerrado"):
        InlineButton("Abrir", "https://example.test")
    with pytest.raises(ValueError, match="formato seguro"):
        confirmation_callback_data(ControlAction.MODE_DIRECT, "predictable")
    with pytest.raises(ValueError, match="máximo de botones"):
        CommandResult(
            "demasiados",
            keyboard=tuple(
                (InlineButton(f"B{index}", approval_callback_data(NONCE)),) for index in range(9)
            ),
        )
    with pytest.raises(ValueError, match="botón durable"):
        deserialize_bot_actions(
            [
                {
                    "kind": "send_message",
                    "telegram_user_id": 101,
                    "chat_id": 101,
                    "text": "x",
                    "reply_to_message_id": None,
                    "keyboard": [[{"text": "Abrir", "url": "https://example.test"}]],
                }
            ]
        )
