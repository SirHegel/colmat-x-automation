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
from colmat_x.providers.catalog import PROVIDER_GEMINI

# Base fija. El único segmento variable es el nombre de modelo, validado con
# MODEL_NAME_PATTERN (sin `/`, `:`, `?` ni `#`), por lo que la URL final nunca
# puede salir de este host ni de esta ruta.
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODELS_ENDPOINT = f"{GEMINI_API_BASE}/models?pageSize=1"
GEMINI_API_KEY_VARIABLE = "GEMINI_API_KEY"
DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"
DEFAULT_GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
DEFAULT_GEMINI_MAX_OUTPUT_TOKENS = 8_192
MAX_GEMINI_MAX_OUTPUT_TOKENS = 65_536

# Dimensiones nominales por proporción; la API no informa el tamaño real.
GEMINI_IMAGE_ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "4:3": (1184, 864),
    "3:4": (864, 1184),
    "3:2": (1248, 832),
    "2:3": (832, 1248),
    "21:9": (1536, 672),
}
_BLOCKING_FINISH_REASONS = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
)
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({"additionalProperties", "uniqueItems"})


class GeminiError(ProviderError):
    """Error base de una solicitud a Gemini."""


class GeminiConfigurationError(GeminiError, ProviderConfigurationError):
    """Falta una configuración local o contiene valores inseguros."""


class GeminiTransportError(GeminiError, ProviderTransportError):
    """La solicitud no obtuvo una respuesta HTTP concluyente."""


class GeminiAPIError(GeminiError, ProviderAPIError):
    """Gemini rechazó una solicitud con una respuesta explícita."""


class GeminiResponseError(GeminiError, ProviderResponseError):
    """Gemini respondió, pero el cuerpo no es seguro para consumir."""


GEMINI_ERRORS = ProviderErrorFamily(
    label="Gemini",
    configuration=GeminiConfigurationError,
    transport=GeminiTransportError,
    api=GeminiAPIError,
    response=GeminiResponseError,
)


class GeminiClient:
    """Adaptador de texto e imagen sobre generateContent con clave en cabecera."""

    provider_name = PROVIDER_GEMINI
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
        max_output_tokens: int = DEFAULT_GEMINI_MAX_OUTPUT_TOKENS,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = shared.api_key_from_environment(
            api_key,
            variable=GEMINI_API_KEY_VARIABLE,
            environ=os.environ,
            errors=GEMINI_ERRORS,
        )
        self.model = shared.model_name(
            model or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL, GEMINI_ERRORS
        )
        self.image_model = shared.model_name(
            image_model or os.getenv("GEMINI_IMAGE_MODEL") or DEFAULT_GEMINI_IMAGE_MODEL,
            GEMINI_ERRORS,
        )
        self.connect_timeout_seconds = shared.positive_timeout(
            connect_timeout_seconds, "connect_timeout_seconds", GEMINI_ERRORS
        )
        self.read_timeout_seconds = shared.positive_timeout(
            read_timeout_seconds, "read_timeout_seconds", GEMINI_ERRORS
        )
        self.image_read_timeout_seconds = shared.positive_timeout(
            image_read_timeout_seconds, "image_read_timeout_seconds", GEMINI_ERRORS
        )
        self.max_output_tokens = shared.bounded_tokens(
            max_output_tokens,
            "max_output_tokens",
            GEMINI_ERRORS,
            maximum=MAX_GEMINI_MAX_OUTPUT_TOKENS,
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
        declaration = _function_declaration(shared.editorial_tool(policy))

        def request(messages: list[dict[str, Any]]) -> Mapping[str, Any]:
            system, contents = _gemini_contents(messages)
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "tools": [{"functionDeclarations": [declaration]}],
                "toolConfig": {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [DRAFT_TOOL_NAME],
                    }
                },
                "generationConfig": {
                    "candidateCount": 1,
                    "maxOutputTokens": self.max_output_tokens,
                },
            }
            return self._post(
                generate_content_endpoint(self.model), body, self.read_timeout_seconds
            )

        return shared.generate_validated_draft(
            brief=brief,
            policy=policy,
            category=category,
            institution=institution,
            errors=GEMINI_ERRORS,
            request=request,
            extract=_extract_draft_arguments,
            repair=_repair_messages,
        )

    def generate_image(
        self,
        prompt: str,
        policy: EditorialPolicy,
        *,
        aspect_ratio: str | None = None,
        alt_text: str | None = None,
    ) -> GeneratedImage:
        final_prompt = shared.build_visual_prompt(prompt, policy, GEMINI_ERRORS)
        ratio = aspect_ratio or "1:1"
        if ratio not in GEMINI_IMAGE_ASPECT_RATIOS:
            allowed = ", ".join(GEMINI_IMAGE_ASPECT_RATIOS)
            raise GeminiConfigurationError(f"aspect_ratio debe ser uno de: {allowed}")
        width, height = GEMINI_IMAGE_ASPECT_RATIOS[ratio]
        validated_alt_text = shared.validate_alt_text(alt_text, GEMINI_ERRORS)
        body = {
            "contents": [{"role": "user", "parts": [{"text": final_prompt}]}],
            "generationConfig": {
                "candidateCount": 1,
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": ratio},
            },
        }
        payload = self._post(
            generate_content_endpoint(self.image_model), body, self.image_read_timeout_seconds
        )
        image_bytes, mime_type = _extract_image(payload)
        return GeneratedImage(
            content=image_bytes,
            mime_type=mime_type,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            width=width,
            height=height,
            model=self.image_model,
            request_id=_response_id(payload),
            alt_text=validated_alt_text,
            provider=PROVIDER_GEMINI,
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
            return CredentialCheck("invalid", "La clave de Gemini está vacía.")
        status_code, failure = shared.get_status(
            session or requests.Session(),
            GEMINI_MODELS_ENDPOINT,
            headers={"x-goog-api-key": secret},
            connect_timeout=connect_timeout_seconds,
            read_timeout=read_timeout_seconds,
        )
        # Gemini responde 400 a una clave con formato inválido; sigue siendo inválida.
        if status_code == 400:
            return CredentialCheck("invalid", "Gemini rechazó la clave (400).")
        return CredentialCheck(
            *shared.credential_check_from_status(status_code, failure, label="Gemini")
        )

    def _post(self, endpoint: str, body: Mapping[str, Any], read_timeout: float) -> dict[str, Any]:
        payload = shared.post_json(
            self.session,
            endpoint,
            headers={"Content-Type": "application/json", "x-goog-api-key": self._api_key},
            body=body,
            connect_timeout=self.connect_timeout_seconds,
            read_timeout=read_timeout,
            errors=GEMINI_ERRORS,
            secret=self._api_key,
        )
        feedback = payload.get("promptFeedback")
        if isinstance(feedback, Mapping) and feedback.get("blockReason"):
            raise GeminiResponseError(
                f"Gemini bloqueó el encargo ({feedback.get('blockReason')}) y no entregó salida"
            )
        return payload


def generate_content_endpoint(model: str) -> str:
    validated = shared.model_name(model, GEMINI_ERRORS)
    return f"{GEMINI_API_BASE}/models/{validated}:generateContent"


def _function_declaration(tool: Mapping[str, Any]) -> dict[str, Any]:
    function = tool.get("function")
    if tool.get("type") != "function" or not isinstance(function, Mapping):
        raise GeminiConfigurationError("La herramienta editorial tiene un formato inválido")
    name = function.get("name")
    description = function.get("description")
    parameters = function.get("parameters")
    if (
        name != DRAFT_TOOL_NAME
        or not isinstance(description, str)
        or not description
        or not isinstance(parameters, Mapping)
    ):
        raise GeminiConfigurationError("La herramienta editorial no es la autorizada")
    return {
        "name": name,
        "description": description,
        "parameters": sanitize_schema(parameters),
    }


def sanitize_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Elimina palabras clave que Gemini rechaza; la validación local sigue completa."""

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYWORDS:
            continue
        if key == "enum" and schema.get("type") != "string":
            continue
        if key == "properties" and isinstance(value, Mapping):
            cleaned[key] = {
                name: sanitize_schema(child) if isinstance(child, Mapping) else child
                for name, child in value.items()
            }
        elif key == "items" and isinstance(value, Mapping):
            cleaned[key] = sanitize_schema(value)
        else:
            cleaned[key] = value
    return cleaned


def _gemini_contents(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if contents or not isinstance(content, str) or not content:
                raise GeminiConfigurationError("Los mensajes system de Gemini son inválidos")
            system_parts.append(content)
            continue
        if role == "gemini":
            # Turnos ya expresados en la forma nativa (model/user con parts).
            if not isinstance(content, Mapping):
                raise GeminiConfigurationError("El turno nativo de Gemini es inválido")
            contents.append(dict(content))
            continue
        if role != "user" or not isinstance(content, str) or not content:
            raise GeminiConfigurationError("El contenido de mensaje para Gemini es inválido")
        contents.append({"role": "user", "parts": [{"text": content}]})
    if not system_parts or not contents:
        raise GeminiConfigurationError("Gemini exige system y conversación")
    return "\n\n".join(system_parts), contents


def _candidate_parts(payload: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], str | None]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise GeminiResponseError("Gemini debe devolver exactamente un candidato")
    candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        raise GeminiResponseError("Gemini devolvió un candidato con formato inválido")
    finish_reason = candidate.get("finishReason")
    if finish_reason == "MAX_TOKENS":
        raise GeminiResponseError("Gemini truncó la salida por límite de tokens")
    if finish_reason in _BLOCKING_FINISH_REASONS:
        raise GeminiResponseError(f"Gemini bloqueó la salida por política ({finish_reason})")
    content = candidate.get("content")
    if not isinstance(content, Mapping):
        raise GeminiResponseError("Gemini no devolvió contenido en el candidato")
    parts = content.get("parts")
    if (
        not isinstance(parts, list)
        or not parts
        or not all(isinstance(part, Mapping) for part in parts)
    ):
        raise GeminiResponseError("Gemini no devolvió partes de contenido válidas")
    return parts, finish_reason if isinstance(finish_reason, str) else None


def _extract_draft_arguments(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    parts, _finish_reason = _candidate_parts(payload)
    calls = [part["functionCall"] for part in parts if "functionCall" in part]
    if len(calls) != 1:
        raise GeminiResponseError("Gemini debe hacer exactamente una llamada de herramienta")
    call = calls[0]
    if not isinstance(call, Mapping):
        raise GeminiResponseError("Gemini devolvió una llamada de herramienta inválida")
    if call.get("name") != DRAFT_TOOL_NAME:
        raise GeminiResponseError("Gemini intentó invocar una herramienta no autorizada")
    arguments = call.get("args")
    if not isinstance(arguments, Mapping):
        raise GeminiResponseError("Los argumentos de herramienta de Gemini no son un objeto JSON")
    return dict(arguments)


def _repair_messages(
    original: list[dict[str, Any]],
    response: Mapping[str, Any],
    error: Exception,
    arguments: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    try:
        parts, _finish_reason = _candidate_parts(response)
    except GeminiResponseError:
        return shared.repair_fallback_messages(original, error)
    calls = [part for part in parts if isinstance(part.get("functionCall"), Mapping)]
    if len(calls) != 1:
        return shared.repair_fallback_messages(original, error)
    # Las partes se devuelven íntegras (incluida una thoughtSignature si existe).
    return [
        *original,
        {"role": "gemini", "content": {"role": "model", "parts": [dict(part) for part in parts]}},
        {
            "role": "gemini",
            "content": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": DRAFT_TOOL_NAME,
                            "response": shared.draft_feedback_payload(error, arguments),
                        }
                    }
                ],
            },
        },
    ]


def _extract_image(payload: Mapping[str, Any]) -> tuple[bytes, str]:
    parts, _finish_reason = _candidate_parts(payload)
    images = [part["inlineData"] for part in parts if "inlineData" in part]
    if len(images) != 1:
        raise GeminiResponseError("Gemini debe devolver exactamente una imagen inline")
    inline = images[0]
    if not isinstance(inline, Mapping):
        raise GeminiResponseError("Gemini devolvió una imagen inline inválida")
    declared = inline.get("mimeType")
    return shared.decode_image_base64(
        inline.get("data"),
        GEMINI_ERRORS,
        declared_mime=declared if isinstance(declared, str) else None,
    )


def _response_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("responseId")
    return value if isinstance(value, str) and value else None
