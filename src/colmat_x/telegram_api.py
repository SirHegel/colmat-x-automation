from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import requests

TELEGRAM_API_ROOT = "https://api.telegram.org"
DEFAULT_TOKEN_ENVIRONMENT_VARIABLE = "TELEGRAM_BOT_TOKEN"
_COMMAND_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")
_WEBHOOK_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_PHOTO_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_PHOTO_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_PHOTO_BYTES = 10 * 1024 * 1024


class TelegramConfigurationError(ValueError):
    """La configuración local de Telegram no es válida."""


class TelegramApiError(RuntimeError):
    """Telegram rechazó o no pudo interpretar una solicitud del bot."""


class TelegramTransportError(TelegramApiError):
    """No fue posible obtener una respuesta concluyente de Telegram."""


class TelegramProtocolError(TelegramApiError):
    """Telegram respondió con un cuerpo que no cumple el protocolo Bot API."""


@dataclass(frozen=True, slots=True)
class TelegramCredentials:
    """Credencial explícita; su representación nunca contiene el token."""

    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token:
            raise TelegramConfigurationError("El token del bot de Telegram está vacío")
        if len(self.token) > 256 or any(character.isspace() for character in self.token):
            raise TelegramConfigurationError(
                "El token del bot de Telegram no tiene un formato válido"
            )
        if any(ord(character) < 33 or ord(character) > 126 for character in self.token):
            raise TelegramConfigurationError(
                "El token del bot de Telegram no tiene un formato válido"
            )

    @classmethod
    def from_environment(
        cls,
        variable: str = DEFAULT_TOKEN_ENVIRONMENT_VARIABLE,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> TelegramCredentials:
        if not isinstance(variable, str) or not variable:
            raise TelegramConfigurationError("El nombre de la variable del token no es válido")
        source = os.environ if environ is None else environ
        raw_token = source.get(variable, "")
        token = raw_token.strip() if isinstance(raw_token, str) else ""
        if not token:
            raise TelegramConfigurationError(f"Falta la credencial requerida: {variable}")
        return cls(token=token)


@dataclass(frozen=True, slots=True)
class BotCommand:
    command: str
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or _COMMAND_PATTERN.fullmatch(self.command) is None:
            raise ValueError(
                "El comando debe contener 1-32 letras minúsculas, números o guiones bajos"
            )
        _required_text(self.description, field_name="description", maximum=256)

    def as_payload(self) -> dict[str, str]:
        return {"command": self.command, "description": self.description}


class TelegramApiClient:
    """Cliente síncrono y acotado para las operaciones Bot API usadas por Colmat."""

    def __init__(
        self,
        credentials: TelegramCredentials,
        *,
        timeout_seconds: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        if not isinstance(credentials, TelegramCredentials):
            raise TypeError("credentials debe ser TelegramCredentials")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds debe ser un número positivo")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser un número positivo")
        self._credentials = credentials
        self.timeout_seconds = float(timeout_seconds)
        self.session = session or requests.Session()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(timeout_seconds={self.timeout_seconds!r})"

    def get_me(self) -> Any:
        return self._call("getMe")

    def set_webhook(
        self,
        url: str,
        *,
        secret_token: str,
        allowed_updates: Sequence[str] | None = None,
        drop_pending_updates: bool = False,
        max_connections: int | None = None,
    ) -> Any:
        _validate_webhook_url(url)
        if (
            not isinstance(secret_token, str)
            or _WEBHOOK_SECRET_PATTERN.fullmatch(secret_token) is None
        ):
            raise ValueError(
                "secret_token debe tener 1-256 caracteres: letras, números, guion o guion bajo"
            )
        if not isinstance(drop_pending_updates, bool):
            raise ValueError("drop_pending_updates debe ser booleano")
        payload: dict[str, Any] = {
            "url": url,
            "secret_token": secret_token,
            "drop_pending_updates": drop_pending_updates,
        }
        if allowed_updates is not None:
            payload["allowed_updates"] = _string_sequence(
                allowed_updates,
                field_name="allowed_updates",
            )
        if max_connections is not None:
            if (
                isinstance(max_connections, bool)
                or not isinstance(max_connections, int)
                or not 1 <= max_connections <= 100
            ):
                raise ValueError("max_connections debe estar entre 1 y 100")
            payload["max_connections"] = max_connections
        return self._call("setWebhook", payload)

    def get_webhook_info(self) -> Any:
        return self._call("getWebhookInfo")

    def set_my_commands(
        self,
        commands: Sequence[BotCommand | Mapping[str, str]],
        *,
        scope: Mapping[str, Any] | None = None,
        language_code: str | None = None,
    ) -> Any:
        if isinstance(commands, (str, bytes)):
            raise ValueError("commands debe ser una secuencia de comandos")
        normalized: list[dict[str, str]] = []
        for command in commands:
            if isinstance(command, BotCommand):
                normalized.append(command.as_payload())
                continue
            if not isinstance(command, Mapping):
                raise ValueError("Cada comando debe contener command y description")
            item = BotCommand(
                command=command.get("command", ""),
                description=command.get("description", ""),
            )
            normalized.append(item.as_payload())
        if not normalized or len(normalized) > 100:
            raise ValueError("commands debe contener entre 1 y 100 comandos")
        payload: dict[str, Any] = {"commands": normalized}
        if scope is not None:
            payload["scope"] = dict(scope)
        if language_code is not None:
            _required_text(language_code, field_name="language_code", maximum=2)
            payload["language_code"] = language_code
        return self._call("setMyCommands", payload)

    def get_my_commands(
        self,
        *,
        scope: Mapping[str, Any] | None = None,
        language_code: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {}
        if scope is not None:
            payload["scope"] = dict(scope)
        if language_code is not None:
            _required_text(language_code, field_name="language_code", maximum=2)
            payload["language_code"] = language_code
        return self._call("getMyCommands", payload)

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_markup: Mapping[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool = False,
    ) -> Any:
        _validate_chat_id(chat_id)
        _required_text(text, field_name="text", maximum=4096)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": _positive_int(reply_to_message_id)}
        return self._call("sendMessage", payload)

    def send_photo(
        self,
        chat_id: int | str,
        photo: str,
        *,
        caption: str | None = None,
        reply_markup: Mapping[str, Any] | None = None,
        disable_notification: bool = False,
    ) -> Any:
        _validate_chat_id(chat_id)
        _required_text(photo, field_name="photo", maximum=2048)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo,
            "disable_notification": disable_notification,
        }
        if caption is not None:
            _required_text(caption, field_name="caption", maximum=1024)
            payload["caption"] = caption
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        return self._call("sendPhoto", payload)

    def send_photo_bytes(
        self,
        chat_id: int | str,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
        caption: str | None = None,
        reply_markup: Mapping[str, Any] | None = None,
        disable_notification: bool = False,
    ) -> Any:
        """Envía una imagen local sin publicarla antes en una URL accesible."""

        _validate_chat_id(chat_id)
        if not isinstance(content, bytes) or not content:
            raise ValueError("content debe ser una imagen no vacía")
        if len(content) > MAX_PHOTO_BYTES:
            raise ValueError("La foto supera el máximo local de 10 MB")
        if not isinstance(filename, str) or _PHOTO_FILENAME_PATTERN.fullmatch(filename) is None:
            raise ValueError("filename no tiene un formato seguro")
        if mime_type not in _PHOTO_MIME_TYPES:
            raise ValueError("mime_type debe ser image/jpeg, image/png o image/webp")
        data: dict[str, Any] = {
            "chat_id": str(chat_id),
            "disable_notification": "true" if disable_notification else "false",
        }
        if caption is not None:
            _required_text(caption, field_name="caption", maximum=1024)
            data["caption"] = caption
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(
                dict(reply_markup),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return self._call_multipart(
            "sendPhoto",
            data=data,
            files={"photo": (filename, content, mime_type)},
        )

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Any:
        _validate_chat_id(chat_id)
        _required_text(text, field_name="text", maximum=4096)
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": _positive_int(message_id),
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        return self._call("editMessageText", payload)

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
        cache_time: int = 0,
    ) -> Any:
        _required_text(callback_query_id, field_name="callback_query_id", maximum=256)
        if not isinstance(show_alert, bool):
            raise ValueError("show_alert debe ser booleano")
        if isinstance(cache_time, bool) or not isinstance(cache_time, int) or cache_time < 0:
            raise ValueError("cache_time debe ser un entero no negativo")
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
            "cache_time": cache_time,
        }
        if text is not None:
            _required_text(text, field_name="text", maximum=200)
            payload["text"] = text
        return self._call("answerCallbackQuery", payload)

    def _call(self, method: str, payload: Mapping[str, Any] | None = None) -> Any:
        encoded_token = quote(self._credentials.token, safe=":")
        endpoint = f"{TELEGRAM_API_ROOT}/bot{encoded_token}/{method}"
        try:
            response = self.session.post(
                endpoint,
                json=dict(payload or {}),
                timeout=self.timeout_seconds,
            )
        except requests.RequestException:
            response = None
        if response is None:
            # Las excepciones de requests pueden incluir la URL (y, por tanto, el token).
            # Se levanta el error fuera de except para no retenerla como __context__.
            raise TelegramTransportError(
                "No fue posible obtener una respuesta de Telegram; reintenta de forma segura"
            )

        invalid_document = object()
        try:
            document = response.json()
        except (TypeError, ValueError):
            document = invalid_document
        if document is invalid_document:
            raise TelegramProtocolError(
                f"Telegram respondió {response.status_code} con un cuerpo no válido"
            )

        if not isinstance(document, dict):
            raise TelegramProtocolError(
                f"Telegram respondió {response.status_code} con un cuerpo no válido"
            )
        if not 200 <= response.status_code < 300 or document.get("ok") is not True:
            error_code = document.get("error_code")
            code = error_code if isinstance(error_code, int) else response.status_code
            description = _redacted_description(
                document.get("description"),
                self._credentials.token,
            )
            retry_after = _retry_after(document.get("parameters"))
            suffix = f"; retry_after={retry_after}" if retry_after is not None else ""
            raise TelegramApiError(f"Telegram respondió {code}: {description}{suffix}")
        if "result" not in document:
            raise TelegramProtocolError("Telegram respondió sin el campo result")
        return document["result"]

    def _call_multipart(
        self,
        method: str,
        *,
        data: Mapping[str, Any],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> Any:
        encoded_token = quote(self._credentials.token, safe=":")
        endpoint = f"{TELEGRAM_API_ROOT}/bot{encoded_token}/{method}"
        try:
            response = self.session.post(
                endpoint,
                data=dict(data),
                files=dict(files),
                timeout=self.timeout_seconds,
            )
        except requests.RequestException:
            response = None
        if response is None:
            raise TelegramTransportError(
                "No fue posible obtener una respuesta de Telegram; reintenta de forma segura"
            )
        invalid_document = object()
        try:
            document = response.json()
        except (TypeError, ValueError):
            document = invalid_document
        if document is invalid_document:
            raise TelegramProtocolError(
                f"Telegram respondió {response.status_code} con un cuerpo no válido"
            )
        if not isinstance(document, dict):
            raise TelegramProtocolError(
                f"Telegram respondió {response.status_code} con un cuerpo no válido"
            )
        if not 200 <= response.status_code < 300 or document.get("ok") is not True:
            error_code = document.get("error_code")
            code = error_code if isinstance(error_code, int) else response.status_code
            description = _redacted_description(
                document.get("description"),
                self._credentials.token,
            )
            retry_after = _retry_after(document.get("parameters"))
            suffix = f"; retry_after={retry_after}" if retry_after is not None else ""
            raise TelegramApiError(f"Telegram respondió {code}: {description}{suffix}")
        if "result" not in document:
            raise TelegramProtocolError("Telegram respondió sin el campo result")
        return document["result"]


# Nombre alternativo explícito para adaptadores que distinguen varios clientes Telegram.
TelegramBotApiClient = TelegramApiClient


def _required_text(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} debe ser texto no vacío de hasta {maximum} caracteres")
    return value


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("message_id debe ser un entero positivo")
    return value


def _validate_chat_id(value: object) -> None:
    if isinstance(value, bool):
        raise ValueError("chat_id no es válido")
    if isinstance(value, int) and value != 0:
        return
    if isinstance(value, str) and value.strip() and len(value) <= 128:
        return
    raise ValueError("chat_id no es válido")


def _string_sequence(values: Sequence[str], *, field_name: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} debe ser una secuencia de texto")
    normalized = list(values)
    if any(not isinstance(value, str) or not value for value in normalized):
        raise ValueError(f"{field_name} debe contener únicamente texto no vacío")
    return normalized


def _validate_webhook_url(url: object) -> None:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("La URL del webhook no es válida")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(
            "La URL del webhook debe ser HTTPS y no contener credenciales ni fragmentos"
        )


def _retry_after(parameters: object) -> int | None:
    if not isinstance(parameters, dict):
        return None
    value = parameters.get("retry_after")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _redacted_description(value: object, token: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return "sin detalle"
    sanitized = value.strip()[:500]
    variants = {
        token,
        quote(token, safe=""),
        quote(token, safe=":"),
        f"bot{token}",
    }
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            sanitized = sanitized.replace(variant, "[REDACTED]")
    return sanitized
