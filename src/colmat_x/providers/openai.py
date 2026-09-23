from __future__ import annotations

import hashlib
import os
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
from colmat_x.providers.catalog import PROVIDER_OPENAI

# Endpoints fijos: ninguna variable de entorno puede sustituirlos.
OPENAI_CHAT_COMPLETIONS_ENDPOINT = "https://api.openai.com/v1/chat/completions"
OPENAI_IMAGE_GENERATIONS_ENDPOINT = "https://api.openai.com/v1/images/generations"
OPENAI_MODELS_ENDPOINT = "https://api.openai.com/v1/models"
OPENAI_API_KEY_VARIABLE = "OPENAI_API_KEY"
DEFAULT_OPENAI_MODEL = "gpt-5"
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-1"
DEFAULT_OPENAI_MAX_COMPLETION_TOKENS = 4_096
MAX_OPENAI_MAX_COMPLETION_TOKENS = 32_768

# gpt-image-1 solo ofrece tres tamaños; las proporciones apaisadas y verticales
# se aproximan al tamaño más cercano y las dimensiones reales quedan en la evidencia.
OPENAI_IMAGE_SIZES: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "3:2": (1536, 1024),
    "2:3": (1024, 1536),
}
OPENAI_ASPECT_RATIO_TO_SIZE: dict[str, str] = {
    "1:1": "1:1",
    "3:2": "3:2",
    "4:3": "3:2",
    "16:9": "3:2",
    "21:9": "3:2",
    "2:3": "2:3",
    "3:4": "2:3",
    "9:16": "2:3",
}


class OpenAIError(ProviderError):
    """Error base de una solicitud a OpenAI."""


class OpenAIConfigurationError(OpenAIError, ProviderConfigurationError):
    """Falta una configuración local o contiene valores inseguros."""


class OpenAITransportError(OpenAIError, ProviderTransportError):
    """La solicitud no obtuvo una respuesta HTTP concluyente."""


class OpenAIAPIError(OpenAIError, ProviderAPIError):
    """OpenAI rechazó una solicitud con una respuesta explícita."""


class OpenAIResponseError(OpenAIError, ProviderResponseError):
    """OpenAI respondió, pero el cuerpo no es seguro para consumir."""


OPENAI_ERRORS = ProviderErrorFamily(
    label="OpenAI",
    configuration=OpenAIConfigurationError,
    transport=OpenAITransportError,
    api=OpenAIAPIError,
    response=OpenAIResponseError,
)


class OpenAIClient:
    """Adaptador de texto (chat completions) e imagen (gpt-image-1)."""

    provider_name = PROVIDER_OPENAI
    supports_images = True

    def __init__(
        self,
        *,
        model: str | None = None,
        image_model: str | None = None,
        api_key: str | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 180.0,
        image_read_timeout_seconds: float = 240.0,
        max_completion_tokens: int = DEFAULT_OPENAI_MAX_COMPLETION_TOKENS,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = shared.api_key_from_environment(
            api_key,
            variable=OPENAI_API_KEY_VARIABLE,
            environ=os.environ,
            errors=OPENAI_ERRORS,
        )
        self.model = shared.model_name(
            model or os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL, OPENAI_ERRORS
        )
        self.image_model = shared.model_name(
            image_model or os.getenv("OPENAI_IMAGE_MODEL") or DEFAULT_OPENAI_IMAGE_MODEL,
            OPENAI_ERRORS,
        )
        self.connect_timeout_seconds = shared.positive_timeout(
            connect_timeout_seconds, "connect_timeout_seconds", OPENAI_ERRORS
        )
        self.read_timeout_seconds = shared.positive_timeout(
            read_timeout_seconds, "read_timeout_seconds", OPENAI_ERRORS
        )
        self.image_read_timeout_seconds = shared.positive_timeout(
            image_read_timeout_seconds, "image_read_timeout_seconds", OPENAI_ERRORS
        )
        self.max_completion_tokens = shared.bounded_tokens(
            max_completion_tokens,
            "max_completion_tokens",
            OPENAI_ERRORS,
            maximum=MAX_OPENAI_MAX_COMPLETION_TOKENS,
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
        tool = shared.editorial_tool(policy)

        def request(messages: list[dict[str, Any]]) -> Mapping[str, Any]:
            # Sin `temperature`: los modelos de razonamiento la rechazan. La herramienta
            # cerrada es obligatoria y no se admiten llamadas paralelas.
            body = {
                "model": self.model,
                "messages": messages,
                "tools": [tool],
                "tool_choice": {"type": "function", "function": {"name": DRAFT_TOOL_NAME}},
                "parallel_tool_calls": False,
                "max_completion_tokens": self.max_completion_tokens,
            }
            return shared.post_json(
                self.session,
                OPENAI_CHAT_COMPLETIONS_ENDPOINT,
                headers=self._headers(),
                body=body,
                connect_timeout=self.connect_timeout_seconds,
                read_timeout=self.read_timeout_seconds,
                errors=OPENAI_ERRORS,
                secret=self._api_key,
            )

        def extract(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            return shared.extract_openai_draft_arguments(
                payload, OPENAI_ERRORS, allow_content_fallback=False
            )

        return shared.generate_validated_draft(
            brief=brief,
            policy=policy,
            category=category,
            institution=institution,
            errors=OPENAI_ERRORS,
            request=request,
            extract=extract,
            repair=shared.openai_repair_messages,
        )

    def generate_image(
        self,
        prompt: str,
        policy: EditorialPolicy,
        *,
        aspect_ratio: str | None = None,
        alt_text: str | None = None,
    ) -> GeneratedImage:
        final_prompt = shared.build_visual_prompt(prompt, policy, OPENAI_ERRORS)
        size_key = _resolve_size_key(aspect_ratio)
        width, height = OPENAI_IMAGE_SIZES[size_key]
        validated_alt_text = shared.validate_alt_text(alt_text, OPENAI_ERRORS)
        body = {
            "model": self.image_model,
            "prompt": final_prompt,
            "n": 1,
            "size": f"{width}x{height}",
            "output_format": "png",
        }
        payload = shared.post_json(
            self.session,
            OPENAI_IMAGE_GENERATIONS_ENDPOINT,
            headers=self._headers(),
            body=body,
            connect_timeout=self.connect_timeout_seconds,
            read_timeout=self.image_read_timeout_seconds,
            errors=OPENAI_ERRORS,
            secret=self._api_key,
        )
        image_bytes, mime_type = _extract_image(payload)
        return GeneratedImage(
            content=image_bytes,
            mime_type=mime_type,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            width=width,
            height=height,
            model=self.image_model,
            request_id=None,
            alt_text=validated_alt_text,
            provider=PROVIDER_OPENAI,
        )

    @staticmethod
    def check_api_key(
        api_key: str,
        *,
        session: Any | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
    ) -> CredentialCheck:
        """Comprueba la clave contra el listado de modelos, sin generar contenido."""

        secret = api_key.strip()
        if not secret:
            return CredentialCheck("invalid", "La clave de OpenAI está vacía.")
        status_code, failure = shared.get_status(
            session or requests.Session(),
            OPENAI_MODELS_ENDPOINT,
            headers={"Authorization": f"Bearer {secret}"},
            connect_timeout=connect_timeout_seconds,
            read_timeout=read_timeout_seconds,
        )
        return CredentialCheck(
            *shared.credential_check_from_status(status_code, failure, label="OpenAI")
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }


def _resolve_size_key(aspect_ratio: str | None) -> str:
    if aspect_ratio is None:
        return "1:1"
    size_key = OPENAI_ASPECT_RATIO_TO_SIZE.get(aspect_ratio)
    if size_key is None:
        allowed = ", ".join(OPENAI_ASPECT_RATIO_TO_SIZE)
        raise OpenAIConfigurationError(f"aspect_ratio debe ser uno de: {allowed}")
    return size_key


def _extract_image(payload: Mapping[str, Any]) -> tuple[bytes, str]:
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
        raise OpenAIResponseError("OpenAI debe devolver exactamente una imagen base64")
    encoded = data[0].get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise OpenAIResponseError("OpenAI devolvió una imagen base64 vacía o inválida")
    return shared.decode_image_base64(encoded, OPENAI_ERRORS)
