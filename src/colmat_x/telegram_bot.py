from __future__ import annotations

import hashlib
import hmac
import json
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
_POST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")
_SNAPSHOT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PLATFORM_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,35}$")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_TELEGRAM_INTEGER = 9_223_372_036_854_775_807
TELEGRAM_DELEGATABLE_ROLES = frozenset({"editor", "reviewer", "publisher", "scheduler", "auditor"})
RESEARCH_PATTERN_STEPS = (
    "Formular la pregunta o tesis de trabajo.",
    "Usar hechos y cifras solo de fuentes aportadas o del registro canónico.",
    "Separar explícitamente hechos e inferencias.",
    "Incluir un contraste o una posible refutación.",
    "Cerrar con una síntesis apta para una futura pieza en X.",
    "No fabricar citas, referencias, cifras ni fuentes.",
)


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
    MANAGE_TEAM = "telegram.team.manage.owner"
    VIEW_EDITORIAL_LINE = "editorial.line.view"
    MANAGE_EDITORIAL_LINE = "editorial.line.manage.owner"
    RESEARCH = "research.request"
    VIEW_CALENDAR = "calendar.view"
    MANAGE_MODE = "automation.mode.manage"
    GENERATE = "content.generate"
    REQUEST_PUBLISH = "content.publish.request"
    APPROVE = "content.approve"
    REJECT = "content.reject"


class BotAutomationMode(StrEnum):
    HUMAN_REVIEW = "human_review"
    DIRECT = "direct"


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
class TelegramUpdateClaim:
    """Lease cercada para procesar o redistribuir un update persistido.

    ``prepared_actions`` distingue ``None`` (aún no existe un resultado durable)
    de una tupla vacía (resultado durable sin acciones salientes).
    """

    acquired: bool
    claim_token: str | None = field(default=None, repr=False)
    claim_fence: int | None = None
    prepared_actions: tuple[Mapping[str, object], ...] | None = None
    business_result: Mapping[str, object] | None = None
    retryable: bool = False

    def __bool__(self) -> bool:
        return self.acquired


@dataclass(frozen=True, slots=True)
class DecisionResult:
    text: str
    accepted: bool = True


@dataclass(frozen=True, slots=True)
class CommandResult:
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
    ) -> bool | TelegramUpdateClaim:
        """Reclama el update o describe por qué no debe procesarse otra vez."""

    def prepare_telegram_actions(
        self,
        update_id: int,
        actions: Sequence[Mapping[str, object]],
        *,
        claim_token: str,
        claim_fence: int,
    ) -> object:
        """Persiste la respuesta antes de ejecutar efectos externos contra Telegram."""


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

    # PlatformStore expone además ``apply_callback_decision``. El procesador lo
    # detecta por capacidad para conservar compatibilidad con stores simples de tests.


class TelegramBotOperations(Protocol):
    def get_status(self, *, telegram_user_id: int, chat_id: int) -> str: ...

    def get_team(self, *, telegram_user_id: int, chat_id: int) -> str: ...

    def list_telegram_users(self, *, telegram_user_id: int, chat_id: int) -> str: ...

    def invite_telegram_user(
        self,
        target_telegram_user_id: int,
        role: str,
        email: str,
        display_name: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        """Crea una membresía con rol acotado y un vínculo privado por ID numérico."""

    def bind_telegram_user(
        self,
        target_telegram_user_id: int,
        user_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        """Vincula una membresía existente a un ID numérico en su chat privado."""

    def get_editorial_line(
        self,
        month: str | None,
        *,
        telegram_user_id: int,
        chat_id: int,
    ) -> str: ...

    def set_editorial_line(
        self,
        month: str,
        text: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        """Fija una línea mensual; no genera, aprueba ni publica contenido."""

    def research_topic(
        self,
        topic: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        """Encola una investigación para el worker; nunca llama IA o X en el webhook."""

    def get_research_patterns(self, *, telegram_user_id: int, chat_id: int) -> str: ...

    def get_calendar(
        self,
        *,
        days: int,
        telegram_user_id: int,
        chat_id: int,
    ) -> str: ...

    def get_mode(self, *, telegram_user_id: int, chat_id: int) -> str: ...

    def set_mode(
        self,
        mode: BotAutomationMode,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        """Solo configura modos que el procesador permite cambiar desde Telegram."""

    def generate_draft(
        self,
        brief: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        """Genera o solicita un borrador; nunca lo aprueba ni publica."""

    def request_publication(
        self,
        post_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        """Crea una solicitud idempotente de cola; no llama a X."""

    def approve_post(
        self,
        *,
        post_id: str,
        snapshot_hash: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> DecisionResult: ...

    def reject_post(
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
    claim_token: str | None = field(default=None, repr=False)
    claim_fence: int | None = None
    retryable: bool = False
    replayed: bool = False


class ClaimedTelegramUpdateError(RuntimeError):
    """Propaga credenciales de claim sin incluirlas en mensajes ni ``repr``."""

    def __init__(self, update_id: int, claim_token: str, claim_fence: int) -> None:
        super().__init__("Falló el procesamiento de un update reclamado")
        self.update_id = update_id
        self.claim_token = claim_token
        self.claim_fence = claim_fence

    def __repr__(self) -> str:
        return f"{type(self).__name__}(update_id={self.update_id!r})"


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
class _ParsedCommand:
    name: str
    argument: str


@dataclass(frozen=True, slots=True)
class _TelegramInvitation:
    telegram_user_id: int
    role: str
    email: str
    display_name: str


@dataclass(frozen=True, slots=True)
class _TelegramBindingRequest:
    telegram_user_id: int
    user_id: str


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
        raw_claim = self._update_store.claim_update(
            update_id,
            payload=_durable_update_payload(payload),
            telegram_user_id=actor.telegram_user_id if actor else None,
            chat_id=actor.chat_id if actor else None,
        )
        claim = _coerce_update_claim(raw_claim)
        if not claim.acquired:
            return WebhookResult(
                update_id=update_id,
                duplicate=True,
                actions=(),
                retryable=claim.retryable,
            )

        try:
            replayed = claim.prepared_actions is not None or claim.business_result is not None
            if claim.prepared_actions is not None:
                actions = deserialize_bot_actions(claim.prepared_actions)
            elif claim.business_result is not None:
                actions = self._restore_business_result(event, claim.business_result)
            elif isinstance(event, _MessageEvent):
                actions = self._process_message(event, update_id=update_id)
            elif isinstance(event, _CallbackEvent):
                actions = self._process_callback(event, update_id=update_id, claim=claim)
            else:
                actions = ()
            self._prepare_actions(update_id, actions, claim=claim)
        except Exception as exc:
            if claim.claim_token is None or claim.claim_fence is None:
                raise
            raise ClaimedTelegramUpdateError(
                update_id,
                claim.claim_token,
                claim.claim_fence,
            ) from exc
        return WebhookResult(
            update_id=update_id,
            duplicate=False,
            actions=actions,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            replayed=replayed,
        )

    # Alias breve para adaptadores web que ya nombran el cuerpo como update.
    process = process_update

    def _process_message(
        self,
        event: _MessageEvent,
        *,
        update_id: int,
    ) -> tuple[BotAction, ...]:
        parsed = _parse_command(event.text)
        if parsed is None:
            return ()
        command = parsed.name
        argument = parsed.argument
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
                "Colmat X está conectado. Usa /ayuda para consultar los controles disponibles. "
                "Las aprobaciones requieren un botón válido y permisos explícitos."
            )
        elif command in {"ayuda", "help"}:
            text = (
                "Comandos disponibles:\n"
                "/estado — estado editorial y operativo\n"
                "/equipo — integrantes y roles visibles\n"
                "/usuarios — IDs y vínculos de Telegram (solo owner, chat privado)\n"
                "/invitar <telegram_id> <rol> <correo> <nombre> — alta segura (solo owner)\n"
                "/vincular <telegram_id> <user_id> — vincular una cuenta existente "
                "(solo owner)\n"
                "/linea [AAAA-MM <texto>] — consultar o fijar la línea editorial mensual\n"
                "/patrones — mostrar el método de investigación y sus límites\n"
                "/investigar <tema> — encolar investigación según la línea mensual\n"
                "/calendario [días] — agenda de los próximos 1-31 días\n"
                "/modo [human_review|direct] — consultar o solicitar un modo operativo\n"
                "/generar <brief> — crear un borrador sujeto a revisión\n"
                "/publicar <id> — solicitar la cola de un borrador ya aprobado\n"
                "/ayuda — esta guía\n"
                "Las altas usan from.id numérico, un chat privado y roles separados; nunca se "
                "confía en @username. El modo direct exige autorización y doble compuerta; el "
                "webhook no llama a X."
            )
        elif command == "estado":
            if argument:
                return (self._usage_message(event, "/estado"),)
            if not self._allowed(event.actor, BotPermission.VIEW_STATUS):
                return (self._forbidden_message(event, "consultar el estado"),)
            text = self._operations.get_status(
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
        elif command == "equipo":
            if argument:
                return (self._usage_message(event, "/equipo"),)
            if not self._allowed(event.actor, BotPermission.VIEW_TEAM):
                return (self._forbidden_message(event, "consultar el equipo"),)
            text = self._operations.get_team(
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
        elif command == "usuarios":
            if argument:
                return (self._usage_message(event, "/usuarios"),)
            if not self._allowed(event.actor, BotPermission.MANAGE_TEAM):
                return (self._forbidden_message(event, "administrar usuarios de Telegram"),)
            if not _is_private_actor_chat(event.actor):
                return (self._private_management_message(event),)
            text = self._operations.list_telegram_users(
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
        elif command == "invitar":
            if not self._allowed(event.actor, BotPermission.MANAGE_TEAM):
                return (self._forbidden_message(event, "administrar usuarios de Telegram"),)
            if not _is_private_actor_chat(event.actor):
                return (self._private_management_message(event),)
            invitation = _telegram_invitation(argument)
            if invitation is None:
                return (
                    self._usage_message(
                        event,
                        "/invitar <telegram_id> "
                        "<editor|reviewer|publisher|scheduler|auditor> <correo> <nombre>",
                    ),
                )
            result = self._operations.invite_telegram_user(
                invitation.telegram_user_id,
                invitation.role,
                invitation.email,
                invitation.display_name,
                request_id=_request_id(update_id, command),
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
            text = result.text
        elif command == "vincular":
            if not self._allowed(event.actor, BotPermission.MANAGE_TEAM):
                return (self._forbidden_message(event, "administrar usuarios de Telegram"),)
            if not _is_private_actor_chat(event.actor):
                return (self._private_management_message(event),)
            binding = _telegram_binding_request(argument)
            if binding is None:
                return (self._usage_message(event, "/vincular <telegram_id> <user_id>"),)
            result = self._operations.bind_telegram_user(
                binding.telegram_user_id,
                binding.user_id,
                request_id=_request_id(update_id, command),
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
            text = result.text
        elif command == "linea":
            editorial_line = _editorial_line_argument(argument)
            if not argument:
                if not self._allowed(event.actor, BotPermission.VIEW_EDITORIAL_LINE):
                    return (self._forbidden_message(event, "consultar la línea editorial"),)
                text = self._operations.get_editorial_line(
                    None,
                    telegram_user_id=event.actor.telegram_user_id,
                    chat_id=event.actor.chat_id,
                )
            elif editorial_line is None:
                return (self._usage_message(event, "/linea [AAAA-MM <texto>]"),)
            else:
                if not self._allowed(event.actor, BotPermission.MANAGE_EDITORIAL_LINE):
                    return (self._forbidden_message(event, "fijar la línea editorial"),)
                if not _is_private_actor_chat(event.actor):
                    return (self._private_management_message(event),)
                month, line_text = editorial_line
                result = self._operations.set_editorial_line(
                    month,
                    line_text,
                    request_id=_request_id(update_id, command),
                    telegram_user_id=event.actor.telegram_user_id,
                    chat_id=event.actor.chat_id,
                )
                text = result.text
        elif command == "investigar":
            if not self._allowed(event.actor, BotPermission.RESEARCH):
                return (self._forbidden_message(event, "asignar investigaciones"),)
            topic = _research_topic(argument)
            if topic is None:
                return (self._usage_message(event, "/investigar <tema>"),)
            result = self._operations.research_topic(
                topic,
                request_id=_request_id(update_id, command),
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
            text = result.text
        elif command == "patrones":
            if argument:
                return (self._usage_message(event, "/patrones"),)
            if not self._allowed(event.actor, BotPermission.RESEARCH):
                return (self._forbidden_message(event, "consultar los patrones"),)
            text = self._operations.get_research_patterns(
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
        elif command == "calendario":
            if not self._allowed(event.actor, BotPermission.VIEW_CALENDAR):
                return (self._forbidden_message(event, "consultar el calendario"),)
            days = _calendar_days(argument)
            if days is None:
                return (self._usage_message(event, "/calendario [días entre 1 y 31]"),)
            text = self._operations.get_calendar(
                days=days,
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
        elif command == "modo":
            if not self._allowed(event.actor, BotPermission.MANAGE_MODE):
                return (self._forbidden_message(event, "consultar o cambiar el modo"),)
            mode = _automation_mode(argument)
            if not argument:
                text = self._operations.get_mode(
                    telegram_user_id=event.actor.telegram_user_id,
                    chat_id=event.actor.chat_id,
                )
            elif mode is not None:
                result = self._operations.set_mode(
                    mode,
                    request_id=_request_id(update_id, command),
                    telegram_user_id=event.actor.telegram_user_id,
                    chat_id=event.actor.chat_id,
                )
                if mode is BotAutomationMode.DIRECT:
                    text = (
                        "Advertencia: direct solo puede activarse con rol administrativo, "
                        "kill switch y doble compuerta. " + result.text
                    )
                else:
                    text = result.text
            else:
                return (self._usage_message(event, "/modo [human_review|direct]"),)
        elif command == "generar":
            if not self._allowed(event.actor, BotPermission.GENERATE):
                return (self._forbidden_message(event, "generar borradores"),)
            brief = _generation_brief(argument)
            if brief is None:
                return (self._usage_message(event, "/generar <brief de hasta 1000 caracteres>"),)
            result = self._operations.generate_draft(
                brief,
                request_id=_request_id(update_id, command),
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
            text = result.text
        elif command == "publicar":
            if not self._allowed(event.actor, BotPermission.REQUEST_PUBLISH):
                return (self._forbidden_message(event, "solicitar una publicación"),)
            post_id = _publication_post_id(argument)
            if post_id is None:
                return (self._usage_message(event, "/publicar <id de borrador aprobado>"),)
            result = self._operations.request_publication(
                post_id,
                request_id=_request_id(update_id, command),
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
            text = result.text
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

    def _process_callback(
        self,
        event: _CallbackEvent,
        *,
        update_id: int,
        claim: TelegramUpdateClaim,
    ) -> tuple[BotAction, ...]:
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

        atomic_decider = getattr(self._callback_nonces, "apply_callback_decision", None)
        uses_atomic_decision = (
            callable(atomic_decider)
            and claim.claim_token is not None
            and claim.claim_fence is not None
        )
        if uses_atomic_decision:
            intent = atomic_decider(
                nonce,
                decision=decision,
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
                update_id=update_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
            )
        else:
            intent = self._callback_nonces.consume_callback_nonce(
                nonce,
                decision=decision,
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
        if not _valid_intent(intent, decision=decision, actor=event.actor):
            return (self._callback_error(event, "Acción inválida, usada o vencida."),)
        assert intent is not None
        if uses_atomic_decision:
            result = _atomic_callback_result(decision, post_id=intent.post_id)
        elif decision is CallbackDecision.APPROVE:
            result = self._operations.approve_post(
                post_id=intent.post_id,
                snapshot_hash=intent.snapshot_hash,
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )
        else:
            result = self._operations.reject_post(
                post_id=intent.post_id,
                snapshot_hash=intent.snapshot_hash,
                telegram_user_id=event.actor.telegram_user_id,
                chat_id=event.actor.chat_id,
            )

        return self._callback_decision_actions(event, decision=decision, result=result)

    def _prepare_actions(
        self,
        update_id: int,
        actions: tuple[BotAction, ...],
        *,
        claim: TelegramUpdateClaim,
    ) -> None:
        if claim.claim_token is None or claim.claim_fence is None:
            return
        prepare = getattr(self._update_store, "prepare_telegram_actions", None)
        if not callable(prepare):
            return
        prepare(
            update_id,
            serialize_bot_actions(actions),
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
        )

    def _restore_business_result(
        self,
        event: _InboundEvent,
        business_result: Mapping[str, object],
    ) -> tuple[BotAction, ...]:
        if not isinstance(event, _CallbackEvent):
            raise MalformedTelegramUpdate("El resultado durable no corresponde al tipo de update")
        if business_result.get("kind") != "callback_decision":
            raise MalformedTelegramUpdate("El resultado durable de Telegram no es reconocido")
        try:
            decision = CallbackDecision(business_result.get("decision"))
        except (TypeError, ValueError) as exc:
            raise MalformedTelegramUpdate("La decisión durable no es válida") from exc
        post_id = business_result.get("post_id")
        snapshot_hash = business_result.get("snapshot_hash")
        telegram_user_id = business_result.get("telegram_user_id")
        chat_id = business_result.get("chat_id")
        if (
            not isinstance(post_id, str)
            or _POST_ID_PATTERN.fullmatch(post_id) is None
            or not isinstance(snapshot_hash, str)
            or _SNAPSHOT_PATTERN.fullmatch(snapshot_hash) is None
            or telegram_user_id != event.actor.telegram_user_id
            or chat_id != event.actor.chat_id
        ):
            raise MalformedTelegramUpdate("El resultado durable no coincide con el callback")
        parsed = _parse_callback_data(event.data)
        if parsed is None or parsed[0] is not decision:
            raise MalformedTelegramUpdate("El callback no coincide con la decisión durable")
        return self._callback_decision_actions(
            event,
            decision=decision,
            result=_atomic_callback_result(decision, post_id=post_id),
        )

    @staticmethod
    def _callback_decision_actions(
        event: _CallbackEvent,
        *,
        decision: CallbackDecision,
        result: DecisionResult,
    ) -> tuple[BotAction, ...]:
        if decision is CallbackDecision.APPROVE:
            notice = (
                "Aprobación registrada. La publicación seguirá los controles de la cola."
                if result.accepted
                else "No se registró la aprobación."
            )
        else:
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
    def _usage_message(event: _MessageEvent, usage: str) -> SendMessage:
        return SendMessage(
            telegram_user_id=event.actor.telegram_user_id,
            chat_id=event.actor.chat_id,
            text=f"Uso: {usage}",
            reply_to_message_id=event.message_id,
        )

    @staticmethod
    def _private_management_message(event: _MessageEvent) -> SendMessage:
        return SendMessage(
            telegram_user_id=event.actor.telegram_user_id,
            chat_id=event.actor.chat_id,
            text="Por seguridad, administra usuarios únicamente en el chat privado con el bot.",
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
    *,
    replay: bool = False,
) -> tuple[object, ...]:
    """Ejecuta acciones; en replay deja el ack efímero para el final.

    Un ``answerCallbackQuery`` puede haber expirado cuando se recupera un update y
    no debe impedir el ``editMessageText`` que retira los botones. Los mensajes
    nuevos siguen siendo al-menos-una-vez: Telegram no ofrece idempotency keys.
    """

    normalized = tuple(actions)
    if any(
        not isinstance(action, (SendMessage, EditMessageText, AnswerCallbackQuery))
        for action in normalized
    ):
        raise TypeError("La secuencia contiene una acción de Telegram desconocida")

    ordered = (
        tuple(action for action in normalized if not isinstance(action, AnswerCallbackQuery))
        + tuple(action for action in normalized if isinstance(action, AnswerCallbackQuery))
        if replay
        else normalized
    )
    results: list[object] = []
    for action in ordered:
        try:
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
        except Exception as exc:
            if replay and isinstance(action, AnswerCallbackQuery):
                results.append(None)
                continue
            if replay and isinstance(action, EditMessageText) and _edit_already_applied(exc):
                results.append(None)
                continue
            raise
        results.append(result)
    return tuple(results)


def serialize_bot_actions(actions: Sequence[BotAction]) -> tuple[dict[str, object], ...]:
    """Convierte acciones validadas a JSON cerrado para la bandeja durable.

    La bandeja evita repetir mutaciones internas. La entrega HTTP de Telegram sigue
    siendo al-menos-una-vez porque Bot API no ofrece una clave de idempotencia.
    """

    serialized: list[dict[str, object]] = []
    for action in actions:
        if isinstance(action, SendMessage):
            serialized.append(
                {
                    "kind": "send_message",
                    "telegram_user_id": action.telegram_user_id,
                    "chat_id": action.chat_id,
                    "text": action.text,
                    "reply_to_message_id": action.reply_to_message_id,
                }
            )
        elif isinstance(action, EditMessageText):
            serialized.append(
                {
                    "kind": "edit_message_text",
                    "telegram_user_id": action.telegram_user_id,
                    "chat_id": action.chat_id,
                    "message_id": action.message_id,
                    "text": action.text,
                    "remove_inline_keyboard": action.remove_inline_keyboard,
                }
            )
        elif isinstance(action, AnswerCallbackQuery):
            serialized.append(
                {
                    "kind": "answer_callback_query",
                    "telegram_user_id": action.telegram_user_id,
                    "chat_id": action.chat_id,
                    "callback_query_id": action.callback_query_id,
                    "text": action.text,
                    "show_alert": action.show_alert,
                }
            )
        else:
            raise TypeError("La secuencia contiene una acción de Telegram desconocida")
    return tuple(serialized)


def deserialize_bot_actions(
    payload: Sequence[Mapping[str, object]],
) -> tuple[BotAction, ...]:
    """Reconstruye solo el esquema cerrado emitido por :func:`serialize_bot_actions`."""

    actions: list[BotAction] = []
    if len(payload) > 20:
        raise ValueError("El resultado durable contiene demasiadas acciones")
    for item in payload:
        if not isinstance(item, Mapping):
            raise ValueError("La acción durable debe ser un objeto")
        kind = item.get("kind")
        telegram_user_id = _stored_integer(item.get("telegram_user_id"), "telegram_user_id")
        chat_id = _stored_integer(item.get("chat_id"), "chat_id", positive=False)
        text = item.get("text")
        if not isinstance(text, str):
            raise ValueError("La acción durable requiere texto")
        if kind == "send_message" and set(item) == {
            "kind",
            "telegram_user_id",
            "chat_id",
            "text",
            "reply_to_message_id",
        }:
            reply = item.get("reply_to_message_id")
            actions.append(
                SendMessage(
                    telegram_user_id,
                    chat_id,
                    _operation_text(text),
                    reply_to_message_id=(
                        None if reply is None else _stored_integer(reply, "reply_to_message_id")
                    ),
                )
            )
        elif kind == "edit_message_text" and set(item) == {
            "kind",
            "telegram_user_id",
            "chat_id",
            "message_id",
            "text",
            "remove_inline_keyboard",
        }:
            remove_keyboard = item.get("remove_inline_keyboard")
            if not isinstance(remove_keyboard, bool):
                raise ValueError("remove_inline_keyboard durable debe ser booleano")
            actions.append(
                EditMessageText(
                    telegram_user_id,
                    chat_id,
                    _stored_integer(item.get("message_id"), "message_id"),
                    _operation_text(text),
                    remove_inline_keyboard=remove_keyboard,
                )
            )
        elif kind == "answer_callback_query" and set(item) == {
            "kind",
            "telegram_user_id",
            "chat_id",
            "callback_query_id",
            "text",
            "show_alert",
        }:
            callback_query_id = item.get("callback_query_id")
            show_alert = item.get("show_alert")
            if not isinstance(callback_query_id, str) or not callback_query_id:
                raise ValueError("callback_query_id durable no es válido")
            if not isinstance(show_alert, bool):
                raise ValueError("show_alert durable debe ser booleano")
            actions.append(
                AnswerCallbackQuery(
                    telegram_user_id,
                    chat_id,
                    callback_query_id,
                    _operation_text(text),
                    show_alert=show_alert,
                )
            )
        else:
            raise ValueError("El esquema de la acción durable no es válido")
    return tuple(actions)


def _coerce_update_claim(value: bool | TelegramUpdateClaim) -> TelegramUpdateClaim:
    if isinstance(value, bool):
        return TelegramUpdateClaim(acquired=value)
    if not isinstance(value, TelegramUpdateClaim):
        raise TypeError("claim_update devolvió un resultado desconocido")
    if value.acquired and (value.claim_token is None) != (value.claim_fence is None):
        raise ValueError("El claim de Telegram está incompleto")
    return value


def _atomic_callback_result(decision: CallbackDecision, *, post_id: str) -> DecisionResult:
    if decision is CallbackDecision.APPROVE:
        return DecisionResult(f"El borrador {post_id} fue aprobado; todavía no se ha publicado.")
    return DecisionResult(f"El borrador {post_id} fue rechazado.")


def _stored_integer(value: object, field_name: str, *, positive: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} durable debe ser un entero")
    if positive and value <= 0:
        raise ValueError(f"{field_name} durable debe ser positivo")
    if not positive and value == 0:
        raise ValueError(f"{field_name} durable no puede ser cero")
    if abs(value) > MAX_TELEGRAM_INTEGER:
        raise ValueError(f"{field_name} durable supera el rango permitido")
    return value


def _edit_already_applied(exc: Exception) -> bool:
    return "message is not modified" in str(exc).casefold()


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


def _durable_update_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Project an update for durable storage without retaining callback secrets.

    The raw callback remains available in memory through ``_CallbackEvent`` for the
    one-time decision.  Its stable digest lets the store compare retries byte for
    byte without making the approve/reject nonce recoverable from
    ``telegram_updates``.
    """

    projected_value = _redact_inline_callback_data(payload)
    if not isinstance(projected_value, Mapping):  # pragma: no cover - raíz validada
        raise MalformedTelegramUpdate("La proyección durable perdió el objeto raíz")
    projected_payload = dict(projected_value)
    callback = payload.get("callback_query")
    if not isinstance(callback, Mapping) or "data" not in callback:
        return projected_payload
    projected_callback_value = projected_payload.get("callback_query")
    if not isinstance(projected_callback_value, Mapping):  # pragma: no cover - misma topología
        raise MalformedTelegramUpdate("La proyección durable perdió callback_query")
    projected_callback = dict(projected_callback_value)
    projected_callback["data"] = _callback_data_projection(
        callback.get("data"),
        field_name="callback_query.data",
    )
    projected_payload["callback_query"] = projected_callback
    return projected_payload


def _redact_inline_callback_data(value: object) -> object:
    """Copia el JSON y sustituye cada callback de teclados, incluso los anidados."""

    if isinstance(value, Mapping):
        return {
            key: (
                _callback_data_projection(item, field_name="callback_data")
                if key == "callback_data"
                else _redact_inline_callback_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_inline_callback_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_inline_callback_data(item) for item in value)
    return value


def _callback_data_projection(raw_data: object, *, field_name: str) -> dict[str, object]:
    if isinstance(raw_data, str):
        encoded = raw_data.encode("utf-8", errors="surrogatepass")
        parsed = _parse_callback_data(raw_data)
        decision = parsed[0].value if parsed is not None else None
        data_kind = "text"
    else:
        try:
            serialized = json.dumps(
                raw_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            serialized = type(raw_data).__qualname__
        encoded = serialized.encode("utf-8", errors="surrogatepass")
        decision = None
        data_kind = "non_text"
    return {
        "_redacted": field_name,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "byte_length": len(encoded),
        "kind": data_kind,
        "decision": decision,
    }


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


def _parse_command(text: str | None) -> _ParsedCommand | None:
    if not isinstance(text, str) or len(text) > 4096:
        return None
    normalized = text.strip()
    if not normalized:
        return None
    parts = normalized.split(maxsplit=1)
    first_token = parts[0]
    match = _COMMAND_PATTERN.fullmatch(first_token)
    if match is None:
        return None
    argument = parts[1].strip() if len(parts) == 2 else ""
    return _ParsedCommand(name=match.group(1).casefold(), argument=argument)


def _calendar_days(argument: str) -> int | None:
    if not argument:
        return 7
    if re.fullmatch(r"[1-9][0-9]?", argument) is None:
        return None
    days = int(argument)
    return days if days <= 31 else None


def _automation_mode(argument: str) -> BotAutomationMode | None:
    if not argument:
        return None
    try:
        return BotAutomationMode(argument.casefold())
    except ValueError:
        return None


def _generation_brief(argument: str) -> str | None:
    if not argument or len(argument) > 1000 or "\x00" in argument:
        return None
    normalized = " ".join(argument.split())
    if not normalized or len(normalized) > 1000:
        return None
    if any(ord(character) < 32 for character in normalized):
        return None
    return normalized


def _telegram_invitation(argument: str) -> _TelegramInvitation | None:
    parts = argument.split(maxsplit=3)
    if len(parts) != 4:
        return None
    telegram_user_id = _telegram_user_id_argument(parts[0])
    role = parts[1].casefold()
    email = parts[2].casefold()
    display_name = " ".join(parts[3].split())
    if (
        telegram_user_id is None
        or role not in TELEGRAM_DELEGATABLE_ROLES
        or len(email) > 320
        or _EMAIL_PATTERN.fullmatch(email) is None
        or not display_name
        or len(display_name) > 120
        or any(ord(character) < 32 for character in display_name)
    ):
        return None
    return _TelegramInvitation(telegram_user_id, role, email, display_name)


def _telegram_binding_request(argument: str) -> _TelegramBindingRequest | None:
    parts = argument.split()
    if len(parts) != 2:
        return None
    telegram_user_id = _telegram_user_id_argument(parts[0])
    user_id = parts[1]
    if (
        telegram_user_id is None
        or len(user_id) > 36
        or _PLATFORM_USER_ID_PATTERN.fullmatch(user_id) is None
    ):
        return None
    return _TelegramBindingRequest(telegram_user_id, user_id)


def _telegram_user_id_argument(value: str) -> int | None:
    if re.fullmatch(r"[1-9][0-9]{0,18}", value) is None:
        return None
    parsed = int(value)
    return parsed if parsed <= MAX_TELEGRAM_INTEGER else None


def _editorial_line_argument(argument: str) -> tuple[str, str] | None:
    parts = argument.split(maxsplit=1)
    if len(parts) != 2 or re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", parts[0]) is None:
        return None
    line_text = " ".join(parts[1].split())
    if not line_text or len(line_text) > 600 or any(ord(character) < 32 for character in line_text):
        return None
    return parts[0], line_text


def _research_topic(argument: str) -> str | None:
    topic = " ".join(argument.split())
    if not topic or len(topic) > 300 or any(ord(character) < 32 for character in topic):
        return None
    return topic


def _is_private_actor_chat(actor: _Actor) -> bool:
    # Telegram usa el mismo ID numérico para from.id y el chat privado con el bot.
    return actor.chat_id == actor.telegram_user_id


def _publication_post_id(argument: str) -> str | None:
    if not argument or _POST_ID_PATTERN.fullmatch(argument) is None:
        return None
    return argument


def _request_id(update_id: int, command: str) -> str:
    return f"telegram:{update_id}:{command}"


def _operation_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("La operación del bot debe devolver texto no vacío")
    # sendMessage/editMessageText admiten como máximo 4096 caracteres.
    return value if len(value) <= 4096 else value[:4093] + "..."
