from __future__ import annotations

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
    ProviderCapabilityError,
    ProviderConfigurationError,
    ProviderError,
    ProviderErrorFamily,
    ProviderResponseError,
    ProviderTransportError,
)
from colmat_x.providers.catalog import PROVIDER_ANTHROPIC

# Endpoints fijos: ninguna variable de entorno puede sustituirlos.
ANTHROPIC_MESSAGES_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_ENDPOINT = "https://api.anthropic.com/v1/models?limit=1"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
# Modelos vigentes: claude-opus-5 (predeterminado), claude-sonnet-5 y claude-haiku-4-5.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
# Con thinking adaptativo activo por defecto, el presupuesto incluye el razonamiento.
DEFAULT_ANTHROPIC_MAX_TOKENS = 8_192
MAX_ANTHROPIC_MAX_TOKENS = 64_000


class AnthropicError(ProviderError):
    """Error base de una solicitud a Anthropic."""


class AnthropicConfigurationError(AnthropicError, ProviderConfigurationError):
    """Falta una configuración local o contiene valores inseguros."""


class AnthropicCapabilityError(AnthropicConfigurationError, ProviderCapabilityError):
    """Claude no genera imágenes."""


class AnthropicTransportError(AnthropicError, ProviderTransportError):
    """La solicitud no obtuvo una respuesta HTTP concluyente."""


class AnthropicAPIError(AnthropicError, ProviderAPIError):
    """Anthropic rechazó una solicitud con una respuesta explícita."""


class AnthropicResponseError(AnthropicError, ProviderResponseError):
    """Anthropic respondió, pero el cuerpo no es seguro para consumir."""


ANTHROPIC_ERRORS = ProviderErrorFamily(
    label="Anthropic",
    configuration=AnthropicConfigurationError,
    transport=AnthropicTransportError,
    api=AnthropicAPIError,
    response=AnthropicResponseError,
)


class AnthropicClient:
    """Adaptador de texto para Claude; declara que no produce imágenes."""

    provider_name = PROVIDER_ANTHROPIC
    supports_images = False

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 180.0,
        max_tokens: int = DEFAULT_ANTHROPIC_MAX_TOKENS,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = shared.api_key_from_environment(
            api_key,
            variable=ANTHROPIC_API_KEY_VARIABLE,
            environ=os.environ,
            errors=ANTHROPIC_ERRORS,
        )
        self.model = shared.model_name(
            model or os.getenv("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL, ANTHROPIC_ERRORS
        )
        self.connect_timeout_seconds = shared.positive_timeout(
            connect_timeout_seconds, "connect_timeout_seconds", ANTHROPIC_ERRORS
        )
        self.read_timeout_seconds = shared.positive_timeout(
            read_timeout_seconds, "read_timeout_seconds", ANTHROPIC_ERRORS
        )
        self.max_tokens = shared.bounded_tokens(
            max_tokens, "max_tokens", ANTHROPIC_ERRORS, maximum=MAX_ANTHROPIC_MAX_TOKENS
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
        tool = shared.anthropic_tool(shared.editorial_tool(policy), ANTHROPIC_ERRORS)

        def request(messages: list[dict[str, Any]]) -> Mapping[str, Any]:
            system, conversation = shared.anthropic_messages(messages, ANTHROPIC_ERRORS)
            # Sin `temperature` ni `thinking`: los modelos vigentes rechazan parámetros de
            # muestreo y ejecutan thinking adaptativo por defecto; la llamada forzada a la
            # herramienta cerrada sigue siendo la única salida admitida.
            body = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": conversation,
                "tools": [tool],
                "tool_choice": {"type": "tool", "name": DRAFT_TOOL_NAME},
            }
            return shared.post_json(
                self.session,
                ANTHROPIC_MESSAGES_ENDPOINT,
                headers=self._headers(),
                body=body,
                connect_timeout=self.connect_timeout_seconds,
                read_timeout=self.read_timeout_seconds,
                errors=ANTHROPIC_ERRORS,
                secret=self._api_key,
            )

        def extract(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            return shared.extract_anthropic_draft_arguments(
                payload,
                ANTHROPIC_ERRORS,
                allowed_block_types=shared.ANTHROPIC_EXTENDED_BLOCK_TYPES,
            )

        def repair(
            messages: list[dict[str, Any]],
            response: Mapping[str, Any],
            error: Exception,
            arguments: Mapping[str, Any] | None,
        ) -> list[dict[str, Any]]:
            return shared.anthropic_repair_messages(
                messages,
                response,
                error,
                arguments,
                allowed_block_types=shared.ANTHROPIC_EXTENDED_BLOCK_TYPES,
            )

        return shared.generate_validated_draft(
            brief=brief,
            policy=policy,
            category=category,
            institution=institution,
            errors=ANTHROPIC_ERRORS,
            request=request,
            extract=extract,
            repair=repair,
        )

    def generate_image(
        self,
        prompt: str,
        policy: EditorialPolicy,
        *,
        aspect_ratio: str | None = None,
        alt_text: str | None = None,
    ) -> GeneratedImage:
        del prompt, policy, aspect_ratio, alt_text
        raise AnthropicCapabilityError(
            "Anthropic (Claude) no genera imágenes; configura un proveedor de imagen "
            "distinto (minimax, openai o gemini)"
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
            return CredentialCheck("invalid", "La clave de Anthropic está vacía.")
        status_code, failure = shared.get_status(
            session or requests.Session(),
            ANTHROPIC_MODELS_ENDPOINT,
            headers={"x-api-key": secret, "anthropic-version": ANTHROPIC_VERSION},
            connect_timeout=connect_timeout_seconds,
            read_timeout=read_timeout_seconds,
        )
        return CredentialCheck(
            *shared.credential_check_from_status(status_code, failure, label="Anthropic")
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
