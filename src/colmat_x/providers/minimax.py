from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any

import requests

from colmat_x.editorial import (
    DRAFT_TOOL_NAME,
    EditorialCategory,
    EditorialDraft,
    EditorialPolicy,
    Institution,
)
from colmat_x.providers import _shared as shared
from colmat_x.providers.base import (
    CredentialCheck,
    GeneratedImage,
    ProviderAPIError,
    ProviderConfigurationError,
    ProviderError,
    ProviderErrorFamily,
    ProviderResponseError,
    ProviderTransportError,
)
from colmat_x.providers.catalog import PROVIDER_MINIMAX

MINIMAX_ANTHROPIC_MESSAGES_ENDPOINT = "https://api.minimax.io/anthropic/v1/messages"
MINIMAX_CHAT_COMPLETIONS_ENDPOINT = "https://api.minimax.io/v1/chat/completions"
MINIMAX_IMAGE_GENERATION_ENDPOINT = "https://api.minimax.io/v1/image_generation"
MINIMAX_ANTHROPIC_VERSION = "2023-06-01"
MINIMAX_API_KEY_VARIABLE = "MINIMAX_API_KEY"
DEFAULT_MINIMAX_API_STYLE = "anthropic"
DEFAULT_MINIMAX_MODEL = "MiniMax-M2.7"
DEFAULT_MINIMAX_IMAGE_MODEL = "image-01"
MAX_IMAGE_PROMPT_CHARACTERS = shared.MAX_IMAGE_PROMPT_CHARACTERS
MAX_ALT_TEXT_CHARACTERS = shared.MAX_ALT_TEXT_CHARACTERS
MAX_IMAGE_BYTES = shared.MAX_IMAGE_BYTES
MAX_DRAFT_VALIDATION_ATTEMPTS = shared.MAX_DRAFT_VALIDATION_ATTEMPTS

IMAGE_ASPECT_RATIO_DIMENSIONS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "16:9": (1280, 720),
    "4:3": (1152, 864),
    "3:2": (1248, 832),
    "2:3": (832, 1248),
    "3:4": (864, 1152),
    "9:16": (720, 1280),
    "21:9": (1344, 576),
}

_CUSTOM_SIZE_PATTERN = re.compile(r"^([0-9]{3,4})x([0-9]{3,4})$")


class MiniMaxError(ProviderError):
    """Error base de una solicitud a MiniMax."""


class MiniMaxConfigurationError(MiniMaxError, ProviderConfigurationError):
    """Falta una configuración local o contiene valores inseguros."""


class MiniMaxTransportError(MiniMaxError, ProviderTransportError):
    """La solicitud no obtuvo una respuesta HTTP concluyente."""


class MiniMaxAPIError(MiniMaxError, ProviderAPIError):
    """MiniMax rechazó una solicitud con una respuesta explícita."""


class MiniMaxResponseError(MiniMaxError, ProviderResponseError):
    """MiniMax respondió, pero el cuerpo no es seguro para consumir."""


MINIMAX_ERRORS = ProviderErrorFamily(
    label="MiniMax",
    configuration=MiniMaxConfigurationError,
    transport=MiniMaxTransportError,
    api=MiniMaxAPIError,
    response=MiniMaxResponseError,
)


class MiniMaxClient:
    """Cliente de borradores e imágenes; no conoce métodos de publicación."""

    provider_name = PROVIDER_MINIMAX
    supports_images = True

    def __init__(
        self,
        *,
        model: str | None = None,
        image_model: str | None = None,
        api_style: str | None = None,
        api_key: str | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 90.0,
        image_read_timeout_seconds: float = 180.0,
        max_completion_tokens: int = 2_048,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = shared.api_key_from_environment(
            api_key,
            variable=MINIMAX_API_KEY_VARIABLE,
            environ=os.environ,
            errors=MINIMAX_ERRORS,
        )
        self.api_style = _api_style(
            api_style if api_style is not None else os.getenv("MINIMAX_API_STYLE")
        )
        self.model = shared.model_name(
            model or os.getenv("MINIMAX_MODEL") or DEFAULT_MINIMAX_MODEL, MINIMAX_ERRORS
        )
        self.image_model = shared.model_name(
            image_model or os.getenv("MINIMAX_IMAGE_MODEL") or DEFAULT_MINIMAX_IMAGE_MODEL,
            MINIMAX_ERRORS,
        )
        self.connect_timeout_seconds = shared.positive_timeout(
            connect_timeout_seconds, "connect_timeout_seconds", MINIMAX_ERRORS
        )
        self.read_timeout_seconds = shared.positive_timeout(
            read_timeout_seconds, "read_timeout_seconds", MINIMAX_ERRORS
        )
        self.image_read_timeout_seconds = shared.positive_timeout(
            image_read_timeout_seconds, "image_read_timeout_seconds", MINIMAX_ERRORS
        )
        self.max_completion_tokens = shared.bounded_tokens(
            max_completion_tokens, "max_completion_tokens", MINIMAX_ERRORS, maximum=8_192
        )
        self.session = session or requests.Session()

    def generate_draft(
        self,
        brief: str,
        policy: EditorialPolicy,
        *,
        category: EditorialCategory | str | None = None,
        institution: Institution | str | None = None,
    ) -> EditorialDraft:
        """Genera y valida un borrador; no lo aprueba, programa ni publica."""

        tool = shared.editorial_tool(policy)
        endpoint = (
            MINIMAX_ANTHROPIC_MESSAGES_ENDPOINT
            if self.api_style == "anthropic"
            else MINIMAX_CHAT_COMPLETIONS_ENDPOINT
        )

        def request(messages: list[dict[str, Any]]) -> Mapping[str, Any]:
            return self._post_json(
                endpoint,
                _draft_request_body(
                    model=self.model,
                    messages=messages,
                    tool=tool,
                    max_completion_tokens=self.max_completion_tokens,
                    api_style=self.api_style,
                ),
                read_timeout=self.read_timeout_seconds,
                anthropic_auth=self.api_style == "anthropic",
            )

        def extract(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            return _extract_draft_arguments(payload, api_style=self.api_style)

        def repair(
            messages: list[dict[str, Any]],
            response: Mapping[str, Any],
            error: Exception,
            arguments: Mapping[str, Any] | None,
        ) -> list[dict[str, Any]]:
            if self.api_style == "anthropic":
                return shared.anthropic_repair_messages(messages, response, error, arguments)
            return shared.openai_repair_messages(messages, response, error, arguments)

        return shared.generate_validated_draft(
            brief=brief,
            policy=policy,
            category=category,
            institution=institution,
            errors=MINIMAX_ERRORS,
            request=request,
            extract=extract,
            repair=repair,
        )

    def generate_image(
        self,
        prompt: str,
        policy: EditorialPolicy,
        *,
        size: str | tuple[int, int] | None = None,
        aspect_ratio: str | None = None,
        width: int | None = None,
        height: int | None = None,
        seed: int | None = None,
        alt_text: str | None = None,
    ) -> GeneratedImage:
        """Genera una imagen en memoria; el texto alternativo nunca se delega al modelo."""

        final_prompt = shared.build_visual_prompt(prompt, policy, MINIMAX_ERRORS)
        size_body, resolved_width, resolved_height = _resolve_image_size(
            size=size,
            aspect_ratio=aspect_ratio,
            width=width,
            height=height,
        )
        if "width" in size_body and self.image_model != DEFAULT_MINIMAX_IMAGE_MODEL:
            raise MiniMaxConfigurationError(
                "Las dimensiones personalizadas solo están documentadas para image-01"
            )
        validated_seed = shared.validate_seed(seed, MINIMAX_ERRORS)
        validated_alt_text = shared.validate_alt_text(alt_text, MINIMAX_ERRORS)
        request_body: dict[str, Any] = {
            "model": self.image_model,
            "prompt": final_prompt,
            **size_body,
            "response_format": "base64",
            "n": 1,
            "prompt_optimizer": False,
        }
        if validated_seed is not None:
            request_body["seed"] = validated_seed
        response = self._post_json(
            MINIMAX_IMAGE_GENERATION_ENDPOINT,
            request_body,
            read_timeout=self.image_read_timeout_seconds,
        )
        image_bytes, mime_type = _extract_image(response)
        request_id = response.get("id")
        if request_id is not None and not isinstance(request_id, str):
            raise MiniMaxResponseError("MiniMax devolvió un id de imagen inválido")
        return GeneratedImage(
            content=image_bytes,
            mime_type=mime_type,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            width=resolved_width,
            height=resolved_height,
            model=self.image_model,
            request_id=request_id,
            alt_text=validated_alt_text,
            provider=PROVIDER_MINIMAX,
        )

    @staticmethod
    def check_api_key(
        api_key: str,
        *,
        session: Any | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
    ) -> CredentialCheck:
        """Comprueba la clave con una solicitud mínima de un token; nunca la refleja."""

        secret = api_key.strip()
        if not secret:
            return CredentialCheck("invalid", "La clave de MiniMax está vacía.")
        http = session or requests.Session()
        body = {
            "model": DEFAULT_MINIMAX_MODEL,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        try:
            response = http.post(
                MINIMAX_ANTHROPIC_MESSAGES_ENDPOINT,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": secret,
                    "anthropic-version": MINIMAX_ANTHROPIC_VERSION,
                },
                json=body,
                timeout=(connect_timeout_seconds, read_timeout_seconds),
            )
        except requests.Timeout:
            return CredentialCheck(*_check_result(None, "timeout"))
        except requests.RequestException:
            return CredentialCheck(*_check_result(None, "request"))
        status_code = int(response.status_code)
        if 200 <= status_code < 300:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            base_response = payload.get("base_resp") if isinstance(payload, Mapping) else None
            if isinstance(base_response, Mapping) and base_response.get("status_code") not in (
                0,
                None,
            ):
                code = base_response.get("status_code")
                if code in {1004, 2049}:
                    return CredentialCheck("invalid", f"MiniMax rechazó la clave ({code}).")
                return CredentialCheck(
                    "error", f"MiniMax respondió base_resp {code}; comprobación no concluyente."
                )
        return CredentialCheck(*_check_result(status_code, ""))

    def _post_json(
        self,
        endpoint: str,
        request_body: Mapping[str, Any],
        *,
        read_timeout: float,
        anthropic_auth: bool = False,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if anthropic_auth:
            headers.update(
                {
                    "x-api-key": self._api_key,
                    "anthropic-version": MINIMAX_ANTHROPIC_VERSION,
                }
            )
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = shared.post_json(
            self.session,
            endpoint,
            headers=headers,
            body=request_body,
            connect_timeout=self.connect_timeout_seconds,
            read_timeout=read_timeout,
            errors=MINIMAX_ERRORS,
            secret=self._api_key,
        )
        _validate_base_response(payload, secret=self._api_key)
        if payload.get("input_sensitive") is True or payload.get("output_sensitive") is True:
            raise MiniMaxResponseError("MiniMax marcó la solicitud o su salida como sensible")
        return payload


def _check_result(status_code: int | None, failure: str) -> tuple[str, str]:
    return shared.credential_check_from_status(status_code, failure, label="MiniMax")


def _draft_request_body(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tool: Mapping[str, Any],
    max_completion_tokens: int,
    api_style: str,
) -> dict[str, Any]:
    if api_style == "anthropic":
        system, anthropic_messages = shared.anthropic_messages(messages, MINIMAX_ERRORS)
        return {
            "model": model,
            "system": system,
            "messages": anthropic_messages,
            "tools": [shared.anthropic_tool(tool, MINIMAX_ERRORS)],
            "tool_choice": {"type": "tool", "name": DRAFT_TOOL_NAME},
            "thinking": {"type": "disabled"},
            "stream": False,
            "temperature": 0.2,
            "max_tokens": max_completion_tokens,
        }
    return {
        "model": model,
        "messages": messages,
        "tools": [tool],
        "tool_choice": {
            "type": "function",
            "function": {"name": DRAFT_TOOL_NAME},
        },
        "reasoning_split": True,
        "stream": False,
        "temperature": 0.2,
        "max_completion_tokens": max_completion_tokens,
    }


def validate_alt_text(value: str | None) -> str | None:
    return shared.validate_alt_text(value, MINIMAX_ERRORS)


def _extract_draft_arguments(payload: Mapping[str, Any], *, api_style: str) -> Mapping[str, Any]:
    if payload.get("input_sensitive") is True or payload.get("output_sensitive") is True:
        raise MiniMaxResponseError("MiniMax marcó el contenido como sensible y no entregó borrador")
    if api_style == "anthropic":
        return shared.extract_anthropic_draft_arguments(payload, MINIMAX_ERRORS)
    return shared.extract_openai_draft_arguments(
        payload, MINIMAX_ERRORS, allow_content_fallback=True
    )


def _validate_base_response(payload: Mapping[str, Any], *, secret: str) -> None:
    base_response = payload.get("base_resp")
    if base_response is None:
        return
    if not isinstance(base_response, Mapping):
        raise MiniMaxResponseError("MiniMax devolvió base_resp con formato inválido")
    status_code = base_response.get("status_code")
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise MiniMaxResponseError("MiniMax devolvió base_resp.status_code inválido")
    if status_code != 0:
        raw_message = base_response.get("status_msg")
        message = str(raw_message)[:300] if raw_message else "sin detalle"
        message = shared.redact(message, secret)
        raise MiniMaxAPIError(f"MiniMax rechazó la operación ({status_code}): {message}")


def _resolve_image_size(
    *,
    size: str | tuple[int, int] | None,
    aspect_ratio: str | None,
    width: int | None,
    height: int | None,
) -> tuple[dict[str, Any], int, int]:
    explicit_count = sum(
        (
            size is not None,
            aspect_ratio is not None,
            width is not None or height is not None,
        )
    )
    if explicit_count > 1:
        raise MiniMaxConfigurationError(
            "Usa solo size, aspect_ratio o el par width/height para definir dimensiones"
        )
    if size is not None:
        if isinstance(size, str):
            if size in IMAGE_ASPECT_RATIO_DIMENSIONS:
                width_value, height_value = IMAGE_ASPECT_RATIO_DIMENSIONS[size]
                return {"aspect_ratio": size}, width_value, height_value
            matched = _CUSTOM_SIZE_PATTERN.fullmatch(size)
            if not matched:
                raise MiniMaxConfigurationError(
                    "size debe ser una proporción oficial o WIDTHxHEIGHT"
                )
            width = int(matched.group(1))
            height = int(matched.group(2))
        elif isinstance(size, tuple) and len(size) == 2:
            width, height = size
        else:
            raise MiniMaxConfigurationError("size debe ser texto o una tupla (width, height)")
    if aspect_ratio is not None:
        if aspect_ratio not in IMAGE_ASPECT_RATIO_DIMENSIONS:
            allowed = ", ".join(IMAGE_ASPECT_RATIO_DIMENSIONS)
            raise MiniMaxConfigurationError(f"aspect_ratio debe ser uno de: {allowed}")
        width_value, height_value = IMAGE_ASPECT_RATIO_DIMENSIONS[aspect_ratio]
        return {"aspect_ratio": aspect_ratio}, width_value, height_value
    if width is None and height is None:
        default_ratio = "1:1"
        width_value, height_value = IMAGE_ASPECT_RATIO_DIMENSIONS[default_ratio]
        return {"aspect_ratio": default_ratio}, width_value, height_value
    if width is None or height is None:
        raise MiniMaxConfigurationError("width y height deben proporcionarse juntos")
    validated_width = _image_dimension(width, "width")
    validated_height = _image_dimension(height, "height")
    return (
        {"width": validated_width, "height": validated_height},
        validated_width,
        validated_height,
    )


def _image_dimension(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MiniMaxConfigurationError(f"{name} debe ser un entero")
    if not 512 <= value <= 2_048 or value % 8 != 0:
        raise MiniMaxConfigurationError(
            f"{name} debe quedar entre 512 y 2048 y ser divisible por 8"
        )
    return value


def _extract_image(payload: Mapping[str, Any]) -> tuple[bytes, str]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise MiniMaxResponseError("MiniMax no devolvió el objeto data de la imagen")
    encoded_images = data.get("image_base64")
    if not isinstance(encoded_images, list) or len(encoded_images) != 1:
        raise MiniMaxResponseError("MiniMax debe devolver exactamente una imagen base64")
    metadata = payload.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise MiniMaxResponseError("MiniMax devolvió metadata de imagen inválida")
        failed_count = _metadata_count(metadata.get("failed_count", 0), "failed_count")
        success_count = _metadata_count(metadata.get("success_count", 1), "success_count")
        if failed_count != 0 or success_count != 1:
            raise MiniMaxResponseError("MiniMax no confirmó una única imagen generada con éxito")
    return shared.decode_image_base64(encoded_images[0], MINIMAX_ERRORS)


def _metadata_count(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise MiniMaxResponseError(f"metadata.{name} es inválido")
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    raise MiniMaxResponseError(f"metadata.{name} es inválido")


def _api_style(value: object) -> str:
    if value is None:
        return DEFAULT_MINIMAX_API_STYLE
    if not isinstance(value, str):
        raise MiniMaxConfigurationError("MINIMAX_API_STYLE debe ser anthropic u openai")
    normalized = value.strip().casefold()
    if normalized not in {"anthropic", "openai"}:
        raise MiniMaxConfigurationError("MINIMAX_API_STYLE debe ser anthropic u openai")
    return normalized
