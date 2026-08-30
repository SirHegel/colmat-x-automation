from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import requests
from requests_oauthlib import OAuth1Session

from colmat_x.config import XCredentials
from colmat_x.domain import is_canonical_x_post_id
from colmat_x.image_validation import (
    SUPPORTED_IMAGE_MIME_TYPES,
    sniff_supported_image_mime,
)


class XApiError(RuntimeError):
    """X rechazó la solicitud y no creó la publicación."""


class XApiRateLimitError(XApiError):
    """X rechazó la solicitud por límite de uso."""


class AmbiguousPublishError(RuntimeError):
    """No es seguro afirmar si X alcanzó a crear la publicación."""


class AmbiguousMediaError(RuntimeError):
    """No fue posible confirmar una operación de media previa a publicar."""


class XIdentityMismatchError(XApiError):
    """Las credenciales pertenecen a una cuenta distinta de la configurada."""


@dataclass(frozen=True)
class XPostResponse:
    id: str
    text: str


@dataclass(frozen=True)
class XMediaResponse:
    id: str
    media_key: str | None
    expires_after_secs: int | None


@dataclass(frozen=True)
class XUserResponse:
    id: str
    username: str
    name: str


MEDIA_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,18}$")
ALLOWED_IMAGE_TYPES = set(SUPPORTED_IMAGE_MIME_TYPES)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ALT_TEXT_LENGTH = 1000


class XApiClient:
    def __init__(self, credentials: XCredentials, *, timeout_seconds: float = 20.0) -> None:
        self.endpoint = "https://api.x.com/2/tweets"
        self.media_upload_endpoint = "https://api.x.com/2/media/upload"
        self.media_metadata_endpoint = "https://api.x.com/2/media/metadata"
        self.identity_endpoint = "https://api.x.com/2/users/me"
        self.timeout_seconds = timeout_seconds
        self._redactions = (
            credentials.consumer_key,
            credentials.consumer_secret,
            credentials.access_token,
            credentials.access_token_secret,
        )
        self.session = OAuth1Session(
            client_key=credentials.consumer_key,
            client_secret=credentials.consumer_secret,
            resource_owner_key=credentials.access_token,
            resource_owner_secret=credentials.access_token_secret,
        )

    def create_post(
        self,
        text: str,
        *,
        media_ids: Sequence[str] | None = None,
        made_with_ai: bool = False,
    ) -> XPostResponse:
        payload: dict[str, Any] = {"text": text}
        normalized_media_ids = list(media_ids or ())
        if len(normalized_media_ids) > 4:
            raise ValueError("X admite como máximo cuatro imágenes por publicación")
        if any(MEDIA_ID_PATTERN.fullmatch(media_id) is None for media_id in normalized_media_ids):
            raise ValueError("Cada ID de media debe usar de 1 a 19 dígitos, sin ceros iniciales")
        if normalized_media_ids:
            payload["media"] = {"media_ids": normalized_media_ids}
        if made_with_ai:
            if not normalized_media_ids:
                raise ValueError("made_with_ai solo se envía cuando la publicación lleva media")
            payload["made_with_ai"] = True
        try:
            response = self.session.post(
                self.endpoint,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException:
            response = None
        if response is None:
            raise AmbiguousPublishError(
                "La conexión terminó sin una respuesta concluyente; revisa la cuenta de X "
                "antes de reintentar"
            )

        if response.status_code == 429:
            reset = response.headers.get("x-rate-limit-reset")
            detail = _error_detail(response, secrets=self._redactions)
            suffix = f"; reset={reset}" if reset else ""
            raise XApiRateLimitError(f"X respondió 429: {detail}{suffix}")
        if response.status_code in {408, 409, 425} or response.status_code >= 500:
            raise AmbiguousPublishError(
                f"X respondió {response.status_code}; revisa la cuenta antes de reintentar"
            )
        if response.status_code != 201:
            raise XApiError(
                f"X respondió {response.status_code}: "
                f"{_error_detail(response, secrets=self._redactions)}"
            )

        invalid_document = object()
        try:
            payload: Any = response.json()
            data = payload["data"]
            raw_post_id = data["id"]
            if not is_canonical_x_post_id(raw_post_id):
                raise TypeError("data.id no tiene el formato decimal canónico")
            post_id = raw_post_id
            raw_returned_text = data.get("text")
            returned_text = raw_returned_text if isinstance(raw_returned_text, str) else text
        except (ValueError, KeyError, TypeError):
            payload = invalid_document
        if payload is invalid_document:
            raise AmbiguousPublishError(
                "X respondió con éxito, pero no fue posible leer el ID de la publicación"
            )
        return XPostResponse(id=post_id, text=returned_text)

    def upload_image(
        self,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
        alt_text: str,
    ) -> XMediaResponse:
        """Sube una imagen y confirma su texto alternativo antes de devolver el ID."""

        if not content:
            raise ValueError("La imagen no puede estar vacía")
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("La imagen supera el máximo de 5 MB de X")
        if mime_type not in ALLOWED_IMAGE_TYPES:
            raise ValueError("La imagen debe ser JPEG, PNG o WebP")
        detected_mime_type = sniff_supported_image_mime(content)
        if detected_mime_type is None:
            raise ValueError("Los bytes no tienen una firma de imagen JPEG, PNG o WebP válida")
        if detected_mime_type != mime_type:
            raise ValueError("El MIME declarado no coincide con la firma de la imagen")
        normalized_alt_text = alt_text.strip()
        if not normalized_alt_text:
            raise ValueError("El texto alternativo es obligatorio")
        if len(normalized_alt_text) > MAX_ALT_TEXT_LENGTH:
            raise ValueError("El texto alternativo no puede superar 1000 caracteres")

        try:
            response = self.session.post(
                self.media_upload_endpoint,
                data={"media_category": "tweet_image"},
                files={"media": (filename, content, mime_type)},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException:
            response = None
        if response is None:
            raise AmbiguousMediaError(
                "La conexión terminó sin confirmar la carga de la imagen; no se publicó nada"
            )

        _raise_media_response_error(
            response,
            operation="subir la imagen",
            secrets=self._redactions,
        )
        invalid_document = object()
        try:
            payload: Any = response.json()
            data = payload["data"]
            media_id = data["id"]
            if not isinstance(media_id, str) or MEDIA_ID_PATTERN.fullmatch(media_id) is None:
                raise TypeError("data.id no tiene formato canónico")
            raw_media_key = data.get("media_key")
            media_key = raw_media_key if isinstance(raw_media_key, str) else None
            raw_expiry = data.get("expires_after_secs")
            expiry = raw_expiry if isinstance(raw_expiry, int) and raw_expiry >= 0 else None
        except (ValueError, KeyError, TypeError):
            payload = invalid_document
        if payload is invalid_document:
            raise AmbiguousMediaError(
                "X aceptó la imagen, pero no fue posible leer su identificador"
            )

        self.set_media_alt_text(media_id, normalized_alt_text)
        return XMediaResponse(id=media_id, media_key=media_key, expires_after_secs=expiry)

    def set_media_alt_text(self, media_id: str, alt_text: str) -> None:
        if MEDIA_ID_PATTERN.fullmatch(media_id) is None:
            raise ValueError("El ID de media no es canónico")
        normalized_alt_text = alt_text.strip()
        if not normalized_alt_text or len(normalized_alt_text) > MAX_ALT_TEXT_LENGTH:
            raise ValueError("El texto alternativo debe tener entre 1 y 1000 caracteres")
        try:
            response = self.session.post(
                self.media_metadata_endpoint,
                json={"id": media_id, "metadata": {"alt_text": {"text": normalized_alt_text}}},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException:
            response = None
        if response is None:
            raise AmbiguousMediaError(
                "No fue posible confirmar el texto alternativo; la publicación quedó bloqueada"
            )
        _raise_media_response_error(
            response,
            operation="asociar el texto alternativo",
            secrets=self._redactions,
        )

    def verify_identity(
        self,
        *,
        expected_user_id: str | None = None,
        expected_username: str | None = None,
    ) -> XUserResponse:
        """Comprueba en X que los secretos apuntan a la cuenta institucional esperada."""

        try:
            response = self.session.get(self.identity_endpoint, timeout=self.timeout_seconds)
        except requests.RequestException:
            response = None
        if response is None:
            raise XApiError("No fue posible verificar la identidad de la cuenta en X")
        if response.status_code != 200:
            raise XApiError(
                f"X respondió {response.status_code} al verificar la cuenta: "
                f"{_error_detail(response, secrets=self._redactions)}"
            )
        invalid_document = object()
        try:
            payload: Any = response.json()
            data = payload["data"]
            user_id = data["id"]
            username = data["username"]
            name = data["name"]
            if not is_canonical_x_post_id(user_id):
                raise TypeError("data.id no tiene formato canónico")
            if not all(isinstance(value, str) and value.strip() for value in (username, name)):
                raise TypeError("faltan datos de identidad")
        except (ValueError, KeyError, TypeError):
            payload = invalid_document
        if payload is invalid_document:
            raise XApiError("X devolvió una identidad incompleta")

        if expected_user_id and user_id != expected_user_id.strip():
            raise XIdentityMismatchError("Las credenciales de X no pertenecen al user_id esperado")
        if expected_username and username.casefold() != expected_username.lstrip("@").casefold():
            raise XIdentityMismatchError("Las credenciales de X no pertenecen al usuario esperado")
        return XUserResponse(id=user_id, username=username, name=name)


def _error_detail(response: requests.Response, *, secrets: Sequence[str] = ()) -> str:
    try:
        payload = response.json()
    except ValueError:
        detail = response.text.strip()[:500] or "sin detalle"
        return _redact(detail, secrets)
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("title")
        if detail:
            return _redact(str(detail)[:500], secrets)
        errors = payload.get("errors")
        if errors:
            return _redact(str(errors)[:500], secrets)
    return _redact(str(payload)[:500], secrets)


def _raise_media_response_error(
    response: requests.Response,
    *,
    operation: str,
    secrets: Sequence[str] = (),
) -> None:
    if response.status_code == 429:
        reset = response.headers.get("x-rate-limit-reset")
        suffix = f"; reset={reset}" if reset else ""
        raise XApiRateLimitError(
            f"X respondió 429 al {operation}: {_error_detail(response, secrets=secrets)}{suffix}"
        )
    if response.status_code in {408, 409, 425} or response.status_code >= 500:
        raise AmbiguousMediaError(
            f"X respondió {response.status_code} al {operation}; no se continuará a publicar"
        )
    if response.status_code != 200:
        raise XApiError(
            f"X respondió {response.status_code} al {operation}: "
            f"{_error_detail(response, secrets=secrets)}"
        )


def _redact(value: str, secrets: Sequence[str]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
