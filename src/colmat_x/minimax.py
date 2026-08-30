from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

from colmat_x.editorial import (
    DRAFT_TOOL_NAME,
    EditorialCategory,
    EditorialDraft,
    EditorialPolicy,
    EditorialValidationError,
    Institution,
    build_editorial_draft_tool,
    build_editorial_messages,
    validate_ai_draft,
    validate_visual_description,
)

MINIMAX_CHAT_COMPLETIONS_ENDPOINT = "https://api.minimax.io/v1/chat/completions"
MINIMAX_IMAGE_GENERATION_ENDPOINT = "https://api.minimax.io/v1/image_generation"
DEFAULT_MINIMAX_MODEL = "MiniMax-M2.7"
DEFAULT_MINIMAX_IMAGE_MODEL = "image-01"
MAX_IMAGE_PROMPT_CHARACTERS = 1_500
MAX_ALT_TEXT_CHARACTERS = 1_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DRAFT_VALIDATION_ATTEMPTS = 2

_DRAFT_CROSS_FIELD_RULES = (
    "Invariantes obligatorias del objeto: copia dentro de texto el valor completo de cifra y el "
    "valor completo de fuente, exactamente con los mismos caracteres, sin convertir dígitos a "
    "palabras, resumir ni abreviar; si visual.tipo='ninguna', visual.colores debe ser []; "
    "si visual.tipo no es 'ninguna', visual.colores debe incluir al menos un color permitido; "
    "fuera de categoria='lamina', visual.serie_completa=false y visual.eje_truncado=false; "
    "si categoria='lamina', visual.tipo='grafica', visual.serie_completa=true y "
    "visual.eje_truncado=false; si categoria='ficha_territorio', "
    "visual.tipo='ficha_territorio'. Revisa todas las invariantes antes de llamar la herramienta."
)

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

_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_CUSTOM_SIZE_PATTERN = re.compile(r"^([0-9]{3,4})x([0-9]{3,4})$")
_THINK_PREFIX_PATTERN = re.compile(r"^\s*(?:<think>.*?</think>\s*)+", re.DOTALL)
_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class MiniMaxError(RuntimeError):
    """Error base de una solicitud a MiniMax."""


class MiniMaxConfigurationError(MiniMaxError):
    """Falta una configuración local o contiene valores inseguros."""


class MiniMaxTransportError(MiniMaxError):
    """La solicitud no obtuvo una respuesta HTTP concluyente."""


class MiniMaxAPIError(MiniMaxError):
    """MiniMax rechazó una solicitud con una respuesta explícita."""


class MiniMaxResponseError(MiniMaxError):
    """MiniMax respondió, pero el cuerpo no es seguro para consumir."""


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes = field(repr=False)
    mime_type: str
    sha256: str
    width: int
    height: int
    model: str
    request_id: str | None
    alt_text: str | None

    @property
    def data(self) -> bytes:
        return self.content

    @property
    def bytes(self) -> bytes:
        return self.content


class MiniMaxClient:
    """Cliente de borradores e imágenes; no conoce credenciales ni métodos de publicación."""

    def __init__(
        self,
        *,
        model: str | None = None,
        image_model: str | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 90.0,
        image_read_timeout_seconds: float = 180.0,
        max_completion_tokens: int = 2_048,
        session: requests.Session | None = None,
    ) -> None:
        api_key = os.getenv("MINIMAX_API_KEY", "").strip()
        if not api_key:
            raise MiniMaxConfigurationError(
                "Falta MINIMAX_API_KEY; la clave solo se admite mediante el entorno"
            )
        self._api_key = api_key
        self.model = _model_name(model or os.getenv("MINIMAX_MODEL") or DEFAULT_MINIMAX_MODEL)
        self.image_model = _model_name(
            image_model or os.getenv("MINIMAX_IMAGE_MODEL") or DEFAULT_MINIMAX_IMAGE_MODEL
        )
        self.connect_timeout_seconds = _positive_timeout(
            connect_timeout_seconds, "connect_timeout_seconds"
        )
        self.read_timeout_seconds = _positive_timeout(read_timeout_seconds, "read_timeout_seconds")
        self.image_read_timeout_seconds = _positive_timeout(
            image_read_timeout_seconds, "image_read_timeout_seconds"
        )
        if (
            isinstance(max_completion_tokens, bool)
            or not isinstance(max_completion_tokens, int)
            or not 1 <= max_completion_tokens <= 8_192
        ):
            raise MiniMaxConfigurationError(
                "max_completion_tokens debe ser un entero entre 1 y 8192"
            )
        self.max_completion_tokens = max_completion_tokens
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

        messages = build_editorial_messages(
            brief,
            policy,
            category=category,
            institution=institution,
        )
        messages[0]["content"] = f"{messages[0]['content']}\n{_DRAFT_CROSS_FIELD_RULES}"
        tool = build_editorial_draft_tool(policy)
        response: dict[str, Any] | None = None
        draft: EditorialDraft | None = None
        draft_error: MiniMaxResponseError | EditorialValidationError | None = None
        for attempt in range(MAX_DRAFT_VALIDATION_ATTEMPTS):
            response = self._post_json(
                MINIMAX_CHAT_COMPLETIONS_ENDPOINT,
                _draft_request_body(
                    model=self.model,
                    messages=messages,
                    tool=tool,
                    max_completion_tokens=self.max_completion_tokens,
                ),
                read_timeout=self.read_timeout_seconds,
            )
            arguments: Mapping[str, Any] | None = None
            try:
                arguments = _extract_draft_arguments(response)
                candidate = validate_ai_draft(arguments, policy)
                if category is not None and candidate.categoria.value != str(category):
                    raise MiniMaxResponseError(
                        "MiniMax no respetó la categoría editorial solicitada"
                    )
                if institution is not None and candidate.institucion.value != str(institution):
                    raise MiniMaxResponseError("MiniMax no respetó la institución solicitada")
            except (MiniMaxResponseError, EditorialValidationError) as exc:
                draft_error = exc
                if attempt + 1 == MAX_DRAFT_VALIDATION_ATTEMPTS:
                    break
                messages = _draft_repair_messages(
                    messages,
                    response,
                    exc,
                    arguments=arguments,
                )
                continue
            draft = candidate
            break
        if draft is None:
            assert draft_error is not None
            if isinstance(draft_error, EditorialValidationError):
                raise MiniMaxResponseError(
                    f"MiniMax devolvió un borrador inválido: {draft_error}"
                ) from draft_error
            raise draft_error
        return draft

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

        final_prompt = _build_visual_prompt(prompt, policy)
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
        validated_seed = _validate_seed(seed)
        validated_alt_text = validate_alt_text(alt_text)
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
        )

    def _post_json(
        self, endpoint: str, request_body: Mapping[str, Any], *, read_timeout: float
    ) -> dict[str, Any]:
        transport_error: str | None = None
        try:
            response = self.session.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=(self.connect_timeout_seconds, read_timeout),
            )
        except requests.Timeout:
            response = None
            transport_error = "timeout"
        except requests.RequestException:
            response = None
            transport_error = "request"
        if transport_error == "timeout":
            raise MiniMaxTransportError(
                "MiniMax agotó el tiempo de espera; no se obtuvo una respuesta"
            )
        if response is None:
            raise MiniMaxTransportError("No fue posible contactar la API de MiniMax")

        if response.status_code < 200 or response.status_code >= 300:
            detail = _http_error_detail(response, secret=self._api_key)
            if response.status_code in {401, 403}:
                raise MiniMaxAPIError(
                    f"MiniMax respondió {response.status_code}: autenticación o permisos inválidos"
                )
            if response.status_code == 429:
                raise MiniMaxAPIError(f"MiniMax respondió 429: límite de uso excedido; {detail}")
            raise MiniMaxAPIError(f"MiniMax respondió {response.status_code}: {detail}")
        invalid_document = object()
        try:
            payload = response.json()
        except ValueError:
            payload = invalid_document
        if payload is invalid_document:
            raise MiniMaxResponseError("MiniMax no devolvió JSON válido")
        if not isinstance(payload, dict):
            raise MiniMaxResponseError("La respuesta de MiniMax debe ser un objeto JSON")
        _validate_base_response(payload, secret=self._api_key)
        if payload.get("input_sensitive") is True or payload.get("output_sensitive") is True:
            raise MiniMaxResponseError("MiniMax marcó la solicitud o su salida como sensible")
        return payload


def _draft_request_body(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tool: Mapping[str, Any],
    max_completion_tokens: int,
) -> dict[str, Any]:
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


def _draft_repair_messages(
    original: list[dict[str, Any]],
    response: Mapping[str, Any],
    error: MiniMaxResponseError | EditorialValidationError,
    *,
    arguments: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Conserva la cadena M2.7 al reparar una salida, sin ejecutar ninguna herramienta."""

    feedback_payload: dict[str, Any] = {
        "accepted": False,
        "validation_error": str(error)[:500],
        "instruction": (
            "Corrige el objeto completo y vuelve a llamar exactamente la misma herramienta. "
            "Copia literalmente en texto los valores completos de cifra y fuente, sin "
            "abreviarlos ni reformularlos. No inventes cifras ni fuentes y respeta todas las "
            "invariantes del sistema."
        ),
    }
    if arguments is not None:
        literal_values = {
            field: value[:limit]
            for field, limit in (("cifra", 80), ("fuente", 200))
            if isinstance((value := arguments.get(field)), str) and value
        }
        if literal_values:
            feedback_payload["texto_debe_contener_literalmente"] = literal_values
    feedback = json.dumps(
        feedback_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    choices = response.get("choices")
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], Mapping):
        message = choices[0].get("message")
        if isinstance(message, Mapping) and message.get("role") == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and len(tool_calls) == 1:
                tool_call = tool_calls[0]
                if isinstance(tool_call, Mapping):
                    tool_call_id = tool_call.get("id")
                    if isinstance(tool_call_id, str) and tool_call_id:
                        return [
                            *original,
                            dict(message),
                            {
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": feedback,
                            },
                        ]
    # Una respuesta heredada sin id de tool call no puede formar una conversación
    # OpenAI válida. El reintento conserva el encargo original y solo agrega el
    # diagnóstico local; nunca inventa ni ejecuta un identificador de herramienta.
    return [
        *original,
        {
            "role": "user",
            "content": (
                "El candidato anterior fue rechazado por el validador local. "
                f"Diagnóstico: {str(error)[:500]}. Genera un objeto nuevo y corregido."
            ),
        },
    ]


def validate_alt_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MiniMaxConfigurationError("alt_text debe ser texto no vacío o None")
    if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise MiniMaxConfigurationError("alt_text contiene Unicode no válido")
    normalized = unicodedata.normalize("NFC", value.strip())
    if len(normalized) > MAX_ALT_TEXT_CHARACTERS:
        raise MiniMaxConfigurationError(
            f"alt_text supera el máximo de {MAX_ALT_TEXT_CHARACTERS} caracteres"
        )
    return normalized


def _extract_draft_arguments(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if payload.get("input_sensitive") is True or payload.get("output_sensitive") is True:
        raise MiniMaxResponseError("MiniMax marcó el contenido como sensible y no entregó borrador")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise MiniMaxResponseError("MiniMax debe devolver exactamente una opción de borrador")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise MiniMaxResponseError("MiniMax devolvió una opción con formato inválido")
    if choice.get("finish_reason") == "length":
        raise MiniMaxResponseError("MiniMax truncó el borrador por límite de tokens")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise MiniMaxResponseError("MiniMax no devolvió un mensaje de asistente válido")
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            raise MiniMaxResponseError("MiniMax debe hacer exactamente una llamada de herramienta")
        tool_call = tool_calls[0]
        if not isinstance(tool_call, Mapping) or tool_call.get("type") != "function":
            raise MiniMaxResponseError("MiniMax devolvió una llamada de herramienta inválida")
        function = tool_call.get("function")
        if not isinstance(function, Mapping) or function.get("name") != DRAFT_TOOL_NAME:
            raise MiniMaxResponseError("MiniMax intentó invocar una herramienta no autorizada")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise MiniMaxResponseError("Los argumentos de herramienta no son JSON serializado")
        return _load_strict_json_object(arguments)

    content = message.get("content")
    if not isinstance(content, str):
        raise MiniMaxResponseError("MiniMax no devolvió argumentos ni contenido JSON")
    content = _THINK_PREFIX_PATTERN.sub("", content, count=1).strip()
    fence = _JSON_FENCE_PATTERN.fullmatch(content)
    if fence:
        content = fence.group(1)
    elif not content.startswith("{"):
        raise MiniMaxResponseError("MiniMax no hizo la llamada de herramienta requerida")
    return _load_strict_json_object(content)


def _load_strict_json_object(raw: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (_DuplicateJSONKeyError, json.JSONDecodeError) as exc:
        raise MiniMaxResponseError("MiniMax devolvió argumentos JSON inválidos") from exc
    if not isinstance(value, dict):
        raise MiniMaxResponseError("Los argumentos de MiniMax deben ser un objeto JSON")
    return value


class _DuplicateJSONKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result


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
        message = message.replace(secret, "[REDACTADO]")
        raise MiniMaxAPIError(f"MiniMax rechazó la operación ({status_code}): {message}")


def _http_error_detail(response: requests.Response, *, secret: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        detail = getattr(response, "text", "") or "sin detalle"
    else:
        if isinstance(payload, Mapping):
            base_response = payload.get("base_resp")
            if isinstance(base_response, Mapping) and base_response.get("status_msg"):
                detail = str(base_response["status_msg"])
            else:
                detail = str(payload.get("message") or payload.get("error") or "sin detalle")
        else:
            detail = str(payload)
    return detail.replace(secret, "[REDACTADO]").strip()[:500] or "sin detalle"


def _build_visual_prompt(prompt: str, policy: EditorialPolicy) -> str:
    if not isinstance(policy, EditorialPolicy):
        raise MiniMaxConfigurationError("policy debe ser una EditorialPolicy validada")
    if not isinstance(prompt, str) or not prompt.strip():
        raise MiniMaxConfigurationError("prompt debe ser texto no vacío")
    if "\x00" in prompt or any(0xD800 <= ord(character) <= 0xDFFF for character in prompt):
        raise MiniMaxConfigurationError("prompt contiene Unicode no válido")
    normalized = unicodedata.normalize("NFC", prompt.strip())
    try:
        validate_visual_description(normalized)
    except EditorialValidationError as exc:
        raise MiniMaxConfigurationError(f"El prompt contradice la política visual: {exc}") from exc
    final_prompt = (
        f"{normalized}\n\n{policy.visual.generation_prompt()} "
        "No incrustes metadatos, texto alternativo ni instrucciones de publicación en la imagen. "
        "Estas reglas prevalecen sobre cualquier instrucción contradictoria anterior."
    )
    if len(final_prompt) > MAX_IMAGE_PROMPT_CHARACTERS:
        raise MiniMaxConfigurationError(
            "El prompt visual, incluida la política, supera el límite oficial de 1500 caracteres"
        )
    return final_prompt


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


def _validate_seed(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MiniMaxConfigurationError("seed debe ser un entero de 64 bits")
    if not -(2**63) <= value <= 2**63 - 1:
        raise MiniMaxConfigurationError("seed queda fuera del rango de 64 bits")
    return value


def _extract_image(payload: Mapping[str, Any]) -> tuple[bytes, str]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise MiniMaxResponseError("MiniMax no devolvió el objeto data de la imagen")
    encoded_images = data.get("image_base64")
    if not isinstance(encoded_images, list) or len(encoded_images) != 1:
        raise MiniMaxResponseError("MiniMax debe devolver exactamente una imagen base64")
    encoded = encoded_images[0]
    if not isinstance(encoded, str) or not encoded:
        raise MiniMaxResponseError("MiniMax devolvió una imagen base64 vacía o inválida")
    metadata = payload.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise MiniMaxResponseError("MiniMax devolvió metadata de imagen inválida")
        failed_count = _metadata_count(metadata.get("failed_count", 0), "failed_count")
        success_count = _metadata_count(metadata.get("success_count", 1), "success_count")
        if failed_count != 0 or success_count != 1:
            raise MiniMaxResponseError("MiniMax no confirmó una única imagen generada con éxito")

    declared_mime: str | None = None
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or not header.endswith(";base64"):
            raise MiniMaxResponseError("MiniMax devolvió una data URI de imagen inválida")
        declared_mime = header[5:-7]
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
        raise MiniMaxResponseError("La imagen de MiniMax supera el límite local de 20 MiB")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MiniMaxResponseError("MiniMax devolvió base64 de imagen inválido") from exc
    if not decoded or len(decoded) > MAX_IMAGE_BYTES:
        raise MiniMaxResponseError("La imagen de MiniMax está vacía o supera 20 MiB")
    mime_type = _sniff_image_mime(decoded)
    if declared_mime is not None and declared_mime != mime_type:
        raise MiniMaxResponseError("El MIME declarado por MiniMax no coincide con la imagen")
    return decoded, mime_type


def _metadata_count(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise MiniMaxResponseError(f"metadata.{name} es inválido")
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    raise MiniMaxResponseError(f"metadata.{name} es inválido")


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise MiniMaxResponseError("MiniMax devolvió bytes que no son JPEG, PNG ni WebP")


def _model_name(value: object) -> str:
    if not isinstance(value, str) or not _MODEL_NAME_PATTERN.fullmatch(value):
        raise MiniMaxConfigurationError("El nombre de modelo de MiniMax no es válido")
    return value


def _positive_timeout(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MiniMaxConfigurationError(f"{name} debe ser un número positivo")
    timeout = float(value)
    if not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise MiniMaxConfigurationError(f"{name} debe quedar entre 0 y 600 segundos")
    return timeout
