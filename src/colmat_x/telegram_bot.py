from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeAlias

DEFAULT_WEBHOOK_SECRET_ENVIRONMENT_VARIABLE = "TELEGRAM_WEBHOOK_SECRET"
TELEGRAM_WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
_WEBHOOK_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_CALLBACK_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,48}$")
_COMMAND_PATTERN = re.compile(r"^/([a-zA-Z0-9_]+)(?:@[A-Za-z0-9_]{5,32})?$")
_POST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,79}$")
_SNAPSHOT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_TELEGRAM_INTEGER = 9_223_372_036_854_775_807


class WebhookAuthenticationError(PermissionError):
    """La solicitud no contiene el secreto configurado para el webhook."""


class MalformedTelegramUpdate(ValueError):
    """El update de Telegram no tiene la estructura mínima esperada."""


@dataclass(frozen=True, slots=True)
class TelegramWebhookSecret:
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or _WEBHOOK_SECRET_PATTERN.fullmatch(self.value) is None:
            raise ValueError(
                "El secreto del webhook debe tener 1-256 letras, números, guiones o guiones bajos"
            )

    @classmethod
    def from_environment(
        cls,
        variable: str = DEFAULT_WEBHOOK_SECRET_ENVIRONMENT_VARIABLE,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> TelegramWebhookSecret:
        source = os.environ if environ is None else environ
        raw_value = source.get(variable, "")
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        if not value:
            raise ValueError(f"Falta la credencial requerida: {variable}")
        return cls(value)

    def matches(self, candidate: object) -> bool:
        supplied = candidate if isinstance(candidate, str) and len(candidate) <= 256 else ""
        return hmac.compare_digest(
            supplied.encode("utf-8", errors="surrogatepass"),
            self.value.encode("utf-8"),
        )


class BotPermission(StrEnum):
    ACCESS = "telegram.access"
    VIEW_STATUS = "telegram.status.view"
    VIEW_TEAM = "telegram.team.view"
    APPROVE = "content.approve"
    REJECT = "content.reject"


class CallbackDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class CallbackIntent:
    """Contexto ligado en servidor a un nonce opaco y de un solo uso."""

    decision: CallbackDecision
    post_id: str
    snapshot_hash: str
    telegram_user_id: int
    chat_id: int


@dataclass(frozen=True, slots=True)
class DecisionResult:
    text: str
    accepted: bool = True


class TelegramUpdateStore(Protocol):
    def claim_update(
        self,
        update_id: int,
        *,
        payload: Mapping[str, object],
        telegram_user_id: int | None,
        chat_id: int | None,
    ) -> bool:
        """Registra el update atómicamente y devuelve False si ya existía."""


class TelegramAuthorizer(Protocol):
    def is_allowed(
        self,
        *,
        telegram_user_id: int,
        chat_id: int,
        permission: BotPermission,
    ) -> bool: ...


class CallbackNonceStore(Protocol):
    def consume_callback_nonce(
        self,
        nonce: str,
        *,
        decision: CallbackDecision,
        telegram_user_id: int,
        chat_id: int,
    ) -> CallbackIntent | None:
        """Consume de forma atómica un nonce ligado al actor, chat y decisión."""


class TelegramBotOperations(Protocol):
    def get_status(self, *, telegram_user_id: int, chat_id: int) -> str: ...

    def get_team(self, *, telegram_user_id: int, chat_id: int) -> str: ...

    def approve_post(
        self,
        *,
        post_id: str,
        snapshot_hash: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> DecisionResult: ...


class TelegramActionClient(Protocol):
    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> object: ...

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Mapping[str, object] | None = None,
    ) -> object: ...

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> object: ...

    def reject_post(
        self,
        *,
        post_id: str,
        snapshot_hash: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> DecisionResult: ...


@dataclass(frozen=True, slots=True)
class SendMessage:
    telegram_user_id: int
    chat_id: int
    text: str
    reply_to_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class EditMessageText:
    telegram_user_id: int
    chat_id: int
    message_id: int
    text: str
    remove_inline_keyboard: bool = True


@dataclass(frozen=True, slots=True)
class AnswerCallbackQuery:
    telegram_user_id: int
    chat_id: int
    callback_query_id: str
    text: str
    show_alert: bool = False


BotAction: TypeAlias = SendMessage | EditMessageText | AnswerCallbackQuery


@dataclass(frozen=True, slots=True)
class WebhookResult:
    update_id: int
    duplicate: bool
    actions: tuple[BotAction, ...]


@dataclass(frozen=True, slots=True)
class _Actor:
    telegram_user_id: int
    chat_id: int


@dataclass(frozen=True, slots=True)
class _MessageEvent:
    actor: _Actor
    message_id: int
    text: str | None


@dataclass(frozen=True, slots=True)
class _CallbackEvent:
    actor: _Actor
    callback_query_id: str
    message_id: int
    data: str | None


_InboundEvent: TypeAlias = _MessageEvent | _CallbackEvent | None


class TelegramWebhookProcessor:
    """Convierte updates autenticados en acciones, sin depender de HTTP ni FastAPI."""

    def __init__(
        self,
        *,
        webhook_secret: TelegramWebhookSecret,
        update_store: TelegramUpdateStore,
        authorizer: TelegramAuthorizer,
        operations: TelegramBotOperations,
        callback_nonces: CallbackNonceStore,
    ) -> None:
        if not isinstance(webhook_secret, TelegramWebhookSecret):
            raise TypeError("webhook_secret debe ser TelegramWebhookSecret")
        self._webhook_secret = webhook_secret
        self._update_store = update_store
        self._authorizer = authorizer
        self._operations = operations
        self._callback_nonces = callback_nonces

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def process_update(
        self,
        payload: Mapping[str, object],
        *,
        secret_token: str | None,
    ) -> WebhookResult:
        if not self._webhook_secret.matches(secret_token):
            raise WebhookAuthenticationError("El secreto del webhook de Telegram no coincide")
        if not isinstance(payload, Mapping):
            raise MalformedTelegramUpdate("El update debe ser un objeto JSON")
        update_id = _required_integer(payload.get("update_id"), "update_id", positive=False)
        event = _parse_event(payload)
        actor = event.actor if isinstance(event, (_MessageEvent, _CallbackEvent)) else None
        if not self._update_store.claim_update(
            update_id,
            payload=payload,
            telegram_user_id=actor.telegram_user_id if actor else None,
            chat_id=actor.chat_id if actor else None,
        ):
            return WebhookResult(update_id=update_id, duplicate=True, actions=())
        if isinstance(event, _MessageEvent):
            actions = self._process_message(event)
        elif isinstance(event, _CallbackEvent):
            actions = self._process_callback(event)
        else:
            actions = ()
        return WebhookResult(update_id=update_id, duplicate=False, actions=actions)

    # Alias breve para adaptadores web que ya nombran el cuerpo como update.
    process = process_update

    def _process_message(self, event: _MessageEvent) -> tuple[BotAction, ...]:
        command = _command_name(event.text)
        if command is None:
            return ()
        if not self._allowed(event.actor, BotPermission.ACCESS):
            return (
                SendMessage(
                    telegram_user_id=event.actor.telegram_user_id,
                    chat_id=event.actor.chat_id,
                    text=(
                        "Acceso no autorizado. Comparte tu ID de Telegram "
                        f"({event.actor.telegram_user_id}) con un administrador; el bot no crea "
                        "cuentas ni asigna permisos."
                    ),
                    reply_to_message_id=event.message_id,
                ),
            )
        if command == "start":
            text = (
                "Colmat X está conectado. Puedes consultar /estado, /equipo y /ayuda. "
                "Las aprobaciones requieren un botón válido y permisos explícitos."
            )
        elif command in {"ayuda", "help"}:
            text = (
                "Comandos disponibles:\n"
                "/estado — estado editorial y operativo\n"
                "/equipo — integrantes y roles visibles\n"
                "/ayuda — esta guía\n"
                "El bot no crea usuarios ni publica contenido automáticamente."
            )
        elif command == "estado":
            if not self._allowed(event.actor, BotPermission.VIEW_STATUS):
                return (self._forbidden_message(event, "consultar el estado"),)
            text = self._operations.get_status(
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
        elif command == "equipo":
            if not self._allowed(event.actor, BotPermission.VIEW_TEAM):
                return (self._forbidden_message(event, "consultar el equipo"),)
            text = self._operations.get_team(
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
        else:
            text = "Comando desconocido. Usa /ayuda para ver las opciones disponibles."
        return (
            SendMessage(
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
                text=_operation_text(text),
                reply_to_message_id=event.message_id,
            ),
        )

    def _process_callback(self, event: _CallbackEvent) -> tuple[BotAction, ...]:
        parsed = _parse_callback_data(event.data)
        if parsed is None:
            return (self._callback_error(event, "Acción inválida o vencida."),)
        decision, nonce = parsed
        permission = (
            BotPermission.APPROVE if decision is CallbackDecision.APPROVE else BotPermission.REJECT
        )
        if not self._allowed(event.actor, BotPermission.ACCESS) or not self._allowed(
            event.actor, permission
        ):
            return (self._callback_error(event, "No tienes permiso para esta acción."),)

        intent = self._callback_nonces.consume_callback_nonce(
            nonce,
            decision=decision,
            telegram_user_id=event.actor.telegram_user_id,
            chat_id=event.actor.chat_id,
        )
        if not _valid_intent(intent, decision=decision, actor=event.actor):
            return (self._callback_error(event, "Acción inválida, usada o vencida."),)
        assert intent is not None
        if decision is CallbackDecision.APPROVE:
            result = self._operations.approve_post(
                post_id=intent.post_id,
                snapshot_hash=intent.snapshot_hash,
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
            notice = (
                "Aprobación registrada. La publicación seguirá los controles de la cola."
                if result.accepted
                else "No se registró la aprobación."
            )
        else:
            result = self._operations.reject_post(
                post_id=intent.post_id,
                snapshot_hash=intent.snapshot_hash,
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
            notice = "Rechazo registrado." if result.accepted else "No se registró el rechazo."

        result_text = _operation_text(result.text)
        return (
            AnswerCallbackQuery(
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
                callback_query_id=event.callback_query_id,
                text=notice,
                show_alert=not result.accepted,
            ),
            EditMessageText(
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
                message_id=event.message_id,
                text=result_text,
            ),
        )

    def _allowed(self, actor: _Actor, permission: BotPermission) -> bool:
        return self._authorizer.is_allowed(
            telegram_user_id=actor.telegram_user_id,
            chat_id=actor.chat_id,
            permission=permission,
        )

    @staticmethod
    def _forbidden_message(event: _MessageEvent, operation: str) -> SendMessage:
        return SendMessage(
            telegram_user_id=event.actor.telegram_user_id,
            chat_id=event.actor.chat_id,
            text=f"No tienes permiso para {operation}.",
            reply_to_message_id=event.message_id,
        )

    @staticmethod
    def _callback_error(event: _CallbackEvent, text: str) -> AnswerCallbackQuery:
        return AnswerCallbackQuery(
            telegram_user_id=event.actor.telegram_user_id,
            chat_id=event.actor.chat_id,
            callback_query_id=event.callback_query_id,
            text=text,
            show_alert=True,
        )


def execute_bot_actions(
    client: TelegramActionClient,
    actions: Sequence[BotAction],
) -> tuple[object, ...]:
    """Ejecuta únicamente las acciones salientes producidas por el procesador."""

    normalized = tuple(actions)
    if any(
        not isinstance(action, (SendMessage, EditMessageText, AnswerCallbackQuery))
        for action in normalized
    ):
        raise TypeError("La secuencia contiene una acción de Telegram desconocida")

    results: list[object] = []
    for action in normalized:
        if isinstance(action, SendMessage):
            result = client.send_message(
                action.chat_id,
                action.text,
                reply_to_message_id=action.reply_to_message_id,
            )
        elif isinstance(action, EditMessageText):
            reply_markup = {"inline_keyboard": []} if action.remove_inline_keyboard else None
            result = client.edit_message_text(
                action.chat_id,
                action.message_id,
                action.text,
                reply_markup=reply_markup,
            )
        else:
            result = client.answer_callback_query(
                action.callback_query_id,
                text=action.text,
                show_alert=action.show_alert,
            )
        results.append(result)
    return tuple(results)


def approval_callback_data(nonce: str) -> str:
    return _callback_data(CallbackDecision.APPROVE, nonce)


def rejection_callback_data(nonce: str) -> str:
    return _callback_data(CallbackDecision.REJECT, nonce)


def _callback_data(decision: CallbackDecision, nonce: str) -> str:
    if not isinstance(nonce, str) or _CALLBACK_NONCE_PATTERN.fullmatch(nonce) is None:
        raise ValueError("El nonce del callback no tiene un formato seguro")
    value = f"{decision.value}:{nonce}"
    if len(value.encode("utf-8")) > 64:
        raise ValueError("callback_data supera el límite de 64 bytes de Telegram")
    return value


def _parse_callback_data(value: object) -> tuple[CallbackDecision, str] | None:
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > 64:
        return None
    raw_decision, separator, nonce = value.partition(":")
    if not separator or ":" in nonce or _CALLBACK_NONCE_PATTERN.fullmatch(nonce) is None:
        return None
    try:
        decision = CallbackDecision(raw_decision)
    except ValueError:
        return None
    return decision, nonce


def _valid_intent(
    intent: CallbackIntent | None,
    *,
    decision: CallbackDecision,
    actor: _Actor,
) -> bool:
    return bool(
        intent is not None
        and intent.decision is decision
        and intent.telegram_user_id == actor.telegram_user_id
        and intent.chat_id == actor.chat_id
        and isinstance(intent.post_id, str)
        and _POST_ID_PATTERN.fullmatch(intent.post_id) is not None
        and isinstance(intent.snapshot_hash, str)
        and _SNAPSHOT_PATTERN.fullmatch(intent.snapshot_hash) is not None
    )


def _parse_event(payload: Mapping[str, object]) -> _InboundEvent:
    message = payload.get("message")
    callback = payload.get("callback_query")
    if isinstance(callback, Mapping):
        return _parse_callback(callback)
    if callback is not None:
        raise MalformedTelegramUpdate("callback_query debe ser un objeto")
    if isinstance(message, Mapping):
        return _parse_message(message)
    if message is not None:
        raise MalformedTelegramUpdate("message debe ser un objeto")
    return None


def _parse_message(message: Mapping[str, object]) -> _MessageEvent:
    actor = _actor_from_message(message)
    message_id = _required_integer(message.get("message_id"), "message_id")
    raw_text = message.get("text")
    text = raw_text if isinstance(raw_text, str) else None
    return _MessageEvent(actor=actor, message_id=message_id, text=text)


def _parse_callback(callback: Mapping[str, object]) -> _CallbackEvent:
    raw_user = callback.get("from")
    message = callback.get("message")
    callback_query_id = callback.get("id")
    if not isinstance(raw_user, Mapping) or not isinstance(message, Mapping):
        raise MalformedTelegramUpdate("El callback debe incluir from y message")
    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        raise MalformedTelegramUpdate("El callback debe incluir message.chat")
    actor = _Actor(
        telegram_user_id=_required_integer(raw_user.get("id"), "from.id"),
        chat_id=_required_integer(chat.get("id"), "chat.id", positive=False, nonzero=True),
    )
    if (
        not isinstance(callback_query_id, str)
        or not callback_query_id
        or len(callback_query_id) > 256
    ):
        raise MalformedTelegramUpdate("callback_query.id debe ser texto no vacío")
    raw_data = callback.get("data")
    data = raw_data if isinstance(raw_data, str) else None
    return _CallbackEvent(
        actor=actor,
        callback_query_id=callback_query_id,
        message_id=_required_integer(message.get("message_id"), "message_id"),
        data=data,
    )


def _actor_from_message(message: Mapping[str, object]) -> _Actor:
    raw_user = message.get("from")
    chat = message.get("chat")
    if not isinstance(raw_user, Mapping) or not isinstance(chat, Mapping):
        raise MalformedTelegramUpdate("El mensaje debe incluir from y chat")
    return _Actor(
        telegram_user_id=_required_integer(raw_user.get("id"), "from.id"),
        chat_id=_required_integer(chat.get("id"), "chat.id", positive=False, nonzero=True),
    )


def _required_integer(
    value: object,
    field_name: str,
    *,
    positive: bool = True,
    nonzero: bool = False,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedTelegramUpdate(f"{field_name} debe ser un entero")
    if abs(value) > MAX_TELEGRAM_INTEGER:
        raise MalformedTelegramUpdate(f"{field_name} está fuera del rango admitido")
    if positive and value <= 0:
        raise MalformedTelegramUpdate(f"{field_name} debe ser positivo")
    if not positive and nonzero and value == 0:
        raise MalformedTelegramUpdate(f"{field_name} no puede ser cero")
    if not positive and not nonzero and value < 0:
        raise MalformedTelegramUpdate(f"{field_name} no puede ser negativo")
    return value


def _command_name(text: str | None) -> str | None:
    if not isinstance(text, str) or len(text) > 4096:
        return None
    first_token = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    match = _COMMAND_PATTERN.fullmatch(first_token)
    return match.group(1).casefold() if match else None


def _operation_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("La operación del bot debe devolver texto no vacío")
    # sendMessage/editMessageText admiten como máximo 4096 caracteres.
    return value if len(value) <= 4096 else value[:4093] + "..."
