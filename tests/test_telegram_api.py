from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import requests

from colmat_x.telegram_api import (
    BotCommand,
    TelegramApiClient,
    TelegramApiError,
    TelegramConfigurationError,
    TelegramCredentials,
    TelegramProtocolError,
    TelegramTransportError,
)

TOKEN = "123456789:super_secret_bot_token"


@dataclass
class FakeResponse:
    status_code: int
    payload: object

    def json(self) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [FakeResponse(200, {"ok": True, "result": True})])
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def make_client(
    *,
    session: FakeSession | None = None,
    token: str = TOKEN,
) -> tuple[TelegramApiClient, FakeSession]:
    fake_session = session or FakeSession()
    return (
        TelegramApiClient(
            TelegramCredentials(token),
            timeout_seconds=3.5,
            session=fake_session,  # type: ignore[arg-type]
        ),
        fake_session,
    )


def test_credentials_are_loaded_from_environment_and_repr_safe() -> None:
    credentials = TelegramCredentials.from_environment(environ={"TELEGRAM_BOT_TOKEN": TOKEN})
    client, _ = make_client(token=credentials.token)

    assert credentials.token == TOKEN
    assert TOKEN not in repr(credentials)
    assert TOKEN not in repr(client)


def test_missing_environment_token_has_a_nonsecret_error() -> None:
    with pytest.raises(TelegramConfigurationError, match="TELEGRAM_BOT_TOKEN") as captured:
        TelegramCredentials.from_environment(environ={})

    assert TOKEN not in str(captured.value)


def test_client_requires_a_credential_container() -> None:
    with pytest.raises(TypeError, match="TelegramCredentials"):
        TelegramApiClient(TOKEN)  # type: ignore[arg-type]


def test_get_me_uses_bot_api_timeout_and_returns_result() -> None:
    response = FakeResponse(200, {"ok": True, "result": {"id": 42, "username": "colmat_bot"}})
    client, session = make_client(session=FakeSession([response]))

    result = client.get_me()

    assert result == {"id": 42, "username": "colmat_bot"}
    assert session.calls == [
        {
            "url": f"https://api.telegram.org/bot{TOKEN}/getMe",
            "json": {},
            "timeout": 3.5,
        }
    ]


def test_webhook_and_commands_use_their_canonical_bot_api_methods() -> None:
    client, session = make_client(
        session=FakeSession(
            [
                FakeResponse(200, {"ok": True, "result": True}),
                FakeResponse(200, {"ok": True, "result": {"url": "https://bot.example/hook"}}),
                FakeResponse(200, {"ok": True, "result": True}),
            ]
        )
    )

    assert (
        client.set_webhook(
            "https://bot.example/hook",
            secret_token="Webhook_secret-1",
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
            max_connections=20,
        )
        is True
    )
    assert client.get_webhook_info() == {"url": "https://bot.example/hook"}
    assert (
        client.set_my_commands(
            [
                BotCommand("estado", "Consultar estado"),
                {"command": "equipo", "description": "Equipo"},
            ],
            scope={"type": "all_private_chats"},
            language_code="es",
        )
        is True
    )

    assert [call["url"].rsplit("/", 1)[-1] for call in session.calls] == [
        "setWebhook",
        "getWebhookInfo",
        "setMyCommands",
    ]
    assert session.calls[0]["json"] == {
        "url": "https://bot.example/hook",
        "secret_token": "Webhook_secret-1",
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
        "max_connections": 20,
    }
    assert session.calls[2]["json"]["commands"] == [
        {"command": "estado", "description": "Consultar estado"},
        {"command": "equipo", "description": "Equipo"},
    ]


def test_message_photo_edit_and_callback_build_expected_payloads() -> None:
    client, session = make_client(
        session=FakeSession(
            [FakeResponse(200, {"ok": True, "result": {"message_id": index}}) for index in range(4)]
        )
    )

    client.send_message(-1001, "Hola", reply_to_message_id=9, reply_markup={"inline_keyboard": []})
    client.send_photo(-1001, "photo-file-id", caption="Vista previa")
    client.edit_message_text(-1001, 11, "Aprobado", reply_markup={"inline_keyboard": []})
    client.answer_callback_query("callback-1", text="Listo", show_alert=True, cache_time=2)

    assert [call["url"].rsplit("/", 1)[-1] for call in session.calls] == [
        "sendMessage",
        "sendPhoto",
        "editMessageText",
        "answerCallbackQuery",
    ]
    assert session.calls[0]["json"] == {
        "chat_id": -1001,
        "text": "Hola",
        "disable_notification": False,
        "reply_markup": {"inline_keyboard": []},
        "reply_parameters": {"message_id": 9},
    }
    assert session.calls[1]["json"]["photo"] == "photo-file-id"
    assert session.calls[2]["json"]["message_id"] == 11
    assert session.calls[3]["json"] == {
        "callback_query_id": "callback-1",
        "show_alert": True,
        "cache_time": 2,
        "text": "Listo",
    }


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("http://bot.example/hook", "valid_secret"),
        ("https://user:pass@bot.example/hook", "valid_secret"),
        ("https://bot.example/hook#secret", "valid_secret"),
        ("https://bot.example/hook", "invalid secret"),
    ],
)
def test_set_webhook_rejects_unsafe_configuration(url: str, secret: str) -> None:
    client, session = make_client()

    with pytest.raises(ValueError):
        client.set_webhook(url, secret_token=secret)

    assert session.calls == []


def test_transport_error_never_exposes_the_token() -> None:
    class FailingSession(FakeSession):
        def post(self, url: str, **kwargs: Any) -> FakeResponse:
            raise requests.Timeout(f"timeout requesting {url}")

    client, _ = make_client(session=FailingSession())

    with pytest.raises(TelegramTransportError) as captured:
        client.get_me()

    assert TOKEN not in str(captured.value)
    assert TOKEN not in repr(captured.value)
    assert captured.value.__context__ is None


def test_api_error_redacts_tokens_from_remote_description() -> None:
    description = f"Bad request to https://api.telegram.org/bot{TOKEN}/sendMessage using {TOKEN}"
    client, _ = make_client(
        session=FakeSession(
            [
                FakeResponse(
                    429,
                    {
                        "ok": False,
                        "error_code": 429,
                        "description": description,
                        "parameters": {"retry_after": 7},
                    },
                )
            ]
        )
    )

    with pytest.raises(TelegramApiError, match="retry_after=7") as captured:
        client.send_message(1, "Hola")

    assert TOKEN not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        ValueError(f"invalid body containing {TOKEN}"),
        ["not", "an", "object"],
        {"ok": True},
    ],
)
def test_malformed_success_response_does_not_echo_the_body(payload: object) -> None:
    client, _ = make_client(session=FakeSession([FakeResponse(200, payload)]))

    with pytest.raises(TelegramProtocolError) as captured:
        client.get_me()

    assert TOKEN not in str(captured.value)
    assert captured.value.__context__ is None
