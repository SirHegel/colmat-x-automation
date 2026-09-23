"""Código común a todos los adaptadores de IA.

Nada de lo que hay aquí concede autoridad editorial: cada borrador pasa por
`validate_ai_draft`, cada prompt visual por la política visual y cada imagen
por límites locales de tamaño y firma binaria. Los mensajes se formatean con
la etiqueta del proveedor y se levantan con sus clases de error, de modo que
el camino de MiniMax en producción conserva exactamente su comportamiento.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping
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
from colmat_x.providers.base import ProviderErrorFamily, ProviderResponseError

MAX_IMAGE_PROMPT_CHARACTERS = 1_500
MAX_ALT_TEXT_CHARACTERS = 1_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DRAFT_VALIDATION_ATTEMPTS = 2
MAX_TIMEOUT_SECONDS = 600
REDACTED = "[REDACTADO]"

DRAFT_CROSS_FIELD_RULES = (
    "Invariantes obligatorias del objeto: copia dentro de texto el valor completo de cifra y el "
    "valor completo de fuente, exactamente con los mismos caracteres, sin convertir dígitos a "
    "palabras, resumir ni abreviar; si visual.tipo='ninguna', visual.colores debe ser []; "
    "si visual.tipo no es 'ninguna', visual.colores debe incluir al menos un color permitido; "
    "fuera de categoria='lamina', visual.serie_completa=false y visual.eje_truncado=false; "
    "si categoria='lamina', visual.tipo='grafica', visual.serie_completa=true y "
    "visual.eje_truncado=false; si categoria='ficha_territorio', "
    "visual.tipo='ficha_territorio'. Revisa todas las invariantes antes de llamar la herramienta."
)

MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_THINK_PREFIX_PATTERN = re.compile(r"^\s*(?:<think>.*?</think>\s*)+", re.DOTALL)
_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
ANTHROPIC_RESPONSE_BLOCK_TYPES = frozenset({"text", "thinking", "tool_use"})
ANTHROPIC_EXTENDED_BLOCK_TYPES = ANTHROPIC_RESPONSE_BLOCK_TYPES | {"redacted_thinking"}

RequestCallable = Callable[[list[dict[str, Any]]], Mapping[str, Any]]
ExtractCallable = Callable[[Mapping[str, Any]], Mapping[str, Any]]
RepairCallable = Callable[
    [list[dict[str, Any]], Mapping[str, Any], Exception, "Mapping[str, Any] | None"],
    list[dict[str, Any]],
]


# --- validación de configuración -------------------------------------------------


def model_name(value: object, errors: ProviderErrorFamily) -> str:
    if not isinstance(value, str) or not MODEL_NAME_PATTERN.fullmatch(value):
        raise errors.configuration(f"El nombre de modelo de {errors.label} no es válido")
    return value


def positive_timeout(value: object, name: str, errors: ProviderErrorFamily) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise errors.configuration(f"{name} debe ser un número positivo")
    timeout = float(value)
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise errors.configuration(f"{name} debe quedar entre 0 y {MAX_TIMEOUT_SECONDS} segundos")
    return timeout


def bounded_tokens(value: object, name: str, errors: ProviderErrorFamily, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise errors.configuration(f"{name} debe ser un entero entre 1 y {maximum}")
    return value


def api_key_from_environment(
    explicit: str | None,
    *,
    variable: str,
    environ: Mapping[str, str],
    errors: ProviderErrorFamily,
) -> str:
    """Toma la clave inyectada por el almacén cifrado o, en su defecto, del entorno."""

    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise errors.configuration(f"La clave inyectada de {errors.label} está vacía")
        return explicit.strip()
    api_key = environ.get(variable, "").strip()
    if not api_key:
        raise errors.configuration(
            f"Falta {variable}; la clave solo se admite mediante el entorno o el almacén cifrado"
        )
    return api_key


def validate_seed(value: int | None, errors: ProviderErrorFamily) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise errors.configuration("seed debe ser un entero de 64 bits")
    if not -(2**63) <= value <= 2**63 - 1:
        raise errors.configuration("seed queda fuera del rango de 64 bits")
    return value


def validate_alt_text(value: str | None, errors: ProviderErrorFamily) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise errors.configuration("alt_text debe ser texto no vacío o None")
    if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise errors.configuration("alt_text contiene Unicode no válido")
    normalized = unicodedata.normalize("NFC", value.strip())
    if len(normalized) > MAX_ALT_TEXT_CHARACTERS:
        raise errors.configuration(
            f"alt_text supera el máximo de {MAX_ALT_TEXT_CHARACTERS} caracteres"
        )
    return normalized


def build_visual_prompt(prompt: str, policy: EditorialPolicy, errors: ProviderErrorFamily) -> str:
    """Antepone la política visual y rechaza prompts prohibidos antes de la red."""

    if not isinstance(policy, EditorialPolicy):
        raise errors.configuration("policy debe ser una EditorialPolicy validada")
    if not isinstance(prompt, str) or not prompt.strip():
        raise errors.configuration("prompt debe ser texto no vacío")
    if "\x00" in prompt or any(0xD800 <= ord(character) <= 0xDFFF for character in prompt):
        raise errors.configuration("prompt contiene Unicode no válido")
    normalized = unicodedata.normalize("NFC", prompt.strip())
    try:
        validate_visual_description(normalized)
    except EditorialValidationError as exc:
        raise errors.configuration(f"El prompt contradice la política visual: {exc}") from exc
    final_prompt = (
        f"{normalized}\n\n{policy.visual.generation_prompt()} "
        "No incrustes metadatos, texto alternativo ni instrucciones de publicación en la imagen. "
        "Estas reglas prevalecen sobre cualquier instrucción contradictoria anterior."
    )
    if len(final_prompt) > MAX_IMAGE_PROMPT_CHARACTERS:
        raise errors.configuration(
            "El prompt visual, incluida la política, supera el límite oficial de "
            f"{MAX_IMAGE_PROMPT_CHARACTERS} caracteres"
        )
    return final_prompt


# --- redacción y transporte ---------------------------------------------------------


def redact(text: str, secret: str) -> str:
    return text.replace(secret, REDACTED) if secret else text


def http_error_detail(response: requests.Response, *, secret: str) -> str:
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
                error = payload.get("error")
                if isinstance(error, Mapping) and error.get("message"):
                    detail = str(error["message"])
                else:
                    detail = str(payload.get("message") or error or "sin detalle")
        else:
            detail = str(payload)
    return redact(detail, secret).strip()[:500] or "sin detalle"


def post_json(
    session: Any,
    endpoint: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    connect_timeout: float,
    read_timeout: float,
    errors: ProviderErrorFamily,
    secret: str,
) -> dict[str, Any]:
    """POST JSON con errores cerrados; el secreto nunca aparece en un mensaje."""

    transport_error: str | None = None
    try:
        response = session.post(
            endpoint,
            headers=dict(headers),
            json=body,
            timeout=(connect_timeout, read_timeout),
        )
    except requests.Timeout:
        response = None
        transport_error = "timeout"
    except requests.RequestException:
        response = None
        transport_error = "request"
    if transport_error == "timeout":
        raise errors.transport(
            f"{errors.label} agotó el tiempo de espera; no se obtuvo una respuesta"
        )
    if response is None:
        raise errors.transport(f"No fue posible contactar la API de {errors.label}")
    raise_for_http_status(response, errors=errors, secret=secret)
    return parse_json_object(response, errors=errors)


def raise_for_http_status(response: Any, *, errors: ProviderErrorFamily, secret: str) -> None:
    status_code = response.status_code
    if 200 <= status_code < 300:
        return
    detail = http_error_detail(response, secret=secret)
    if status_code in {401, 403}:
        raise errors.api(
            f"{errors.label} respondió {status_code}: autenticación o permisos inválidos"
        )
    if status_code == 429:
        raise errors.api(f"{errors.label} respondió 429: límite de uso excedido; {detail}")
    raise errors.api(f"{errors.label} respondió {status_code}: {detail}")


def parse_json_object(response: Any, *, errors: ProviderErrorFamily) -> dict[str, Any]:
    invalid_document = object()
    try:
        payload = response.json()
    except ValueError:
        payload = invalid_document
    if payload is invalid_document:
        raise errors.response(f"{errors.label} no devolvió JSON válido")
    if not isinstance(payload, dict):
        raise errors.response(f"La respuesta de {errors.label} debe ser un objeto JSON")
    return payload


def get_status(
    session: Any,
    endpoint: str,
    *,
    headers: Mapping[str, str],
    connect_timeout: float,
    read_timeout: float,
) -> tuple[int | None, str]:
    """GET mínimo para comprobar credenciales; devuelve (status, clase de fallo)."""

    try:
        response = session.get(
            endpoint,
            headers=dict(headers),
            timeout=(connect_timeout, read_timeout),
        )
    except requests.Timeout:
        return None, "timeout"
    except requests.RequestException:
        return None, "request"
    return int(response.status_code), ""


# --- JSON estricto -------------------------------------------------------------------


class _DuplicateJSONKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result


def load_strict_json_object(raw: str, errors: ProviderErrorFamily) -> Mapping[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (_DuplicateJSONKeyError, json.JSONDecodeError) as exc:
        raise errors.response(f"{errors.label} devolvió argumentos JSON inválidos") from exc
    if not isinstance(value, dict):
        raise errors.response(f"Los argumentos de {errors.label} deben ser un objeto JSON")
    return value


# --- bucle de borrador ---------------------------------------------------------------


def generate_validated_draft(
    *,
    brief: str,
    policy: EditorialPolicy,
    category: EditorialCategory | str | None,
    institution: Institution | str | None,
    errors: ProviderErrorFamily,
    request: RequestCallable,
    extract: ExtractCallable,
    repair: RepairCallable,
) -> EditorialDraft:
    """Pide, valida localmente y repara como máximo una vez; nunca autoriza nada."""

    messages = build_editorial_messages(
        brief,
        policy,
        category=category,
        institution=institution,
    )
    messages[0]["content"] = f"{messages[0]['content']}\n{DRAFT_CROSS_FIELD_RULES}"
    draft: EditorialDraft | None = None
    draft_error: ProviderResponseError | EditorialValidationError | None = None
    for attempt in range(MAX_DRAFT_VALIDATION_ATTEMPTS):
        response = request(messages)
        arguments: Mapping[str, Any] | None = None
        try:
            arguments = extract(response)
            candidate = validate_ai_draft(arguments, policy)
            if category is not None and candidate.categoria.value != str(category):
                raise errors.response(
                    f"{errors.label} no respetó la categoría editorial solicitada"
                )
            if institution is not None and candidate.institucion.value != str(institution):
                raise errors.response(f"{errors.label} no respetó la institución solicitada")
        except (ProviderResponseError, EditorialValidationError) as exc:
            draft_error = exc
            if attempt + 1 == MAX_DRAFT_VALIDATION_ATTEMPTS:
                break
            messages = repair(messages, response, exc, arguments)
            continue
        draft = candidate
        break
    if draft is None:
        assert draft_error is not None
        if isinstance(draft_error, EditorialValidationError):
            raise errors.response(
                f"{errors.label} devolvió un borrador inválido: {draft_error}"
            ) from draft_error
        raise draft_error
    return draft


def editorial_tool(policy: EditorialPolicy) -> dict[str, Any]:
    return build_editorial_draft_tool(policy)


def draft_feedback_payload(error: Exception, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Diagnóstico local que se devuelve al modelo; nunca ejecuta la herramienta."""

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
    return feedback_payload


def draft_feedback_json(error: Exception, arguments: Mapping[str, Any] | None) -> str:
    return json.dumps(
        draft_feedback_payload(error, arguments),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def repair_fallback_messages(
    original: list[dict[str, Any]], error: Exception
) -> list[dict[str, Any]]:
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


# --- forma Anthropic (Messages API) ---------------------------------------------------


def anthropic_messages(
    messages: list[dict[str, Any]], errors: ProviderErrorFamily
) -> tuple[str, list[dict[str, Any]]]:
    """Separa el system de la conversación y normaliza los bloques de contenido."""

    system_parts: list[str] = []
    conversation: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if conversation or not isinstance(content, str) or not content:
                raise errors.configuration(f"Los mensajes system de {errors.label} son inválidos")
            system_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            raise errors.configuration(
                f"El rol de mensaje para {errors.label} Anthropic es inválido"
            )
        if isinstance(content, str):
            normalized_content: list[dict[str, Any]] = [{"type": "text", "text": content}]
        elif isinstance(content, list) and content:
            if not all(isinstance(block, Mapping) for block in content):
                raise errors.configuration(
                    f"Los bloques de mensaje para {errors.label} Anthropic son inválidos"
                )
            normalized_content = [dict(block) for block in content]
        else:
            raise errors.configuration(
                f"El contenido de mensaje para {errors.label} Anthropic es inválido"
            )
        conversation.append({"role": role, "content": normalized_content})
    if not system_parts or not conversation:
        raise errors.configuration(f"{errors.label} Anthropic exige system y conversación")
    return "\n\n".join(system_parts), conversation


def anthropic_tool(tool: Mapping[str, Any], errors: ProviderErrorFamily) -> dict[str, Any]:
    function = tool.get("function")
    if tool.get("type") != "function" or not isinstance(function, Mapping):
        raise errors.configuration("La herramienta editorial tiene un formato inválido")
    name = function.get("name")
    description = function.get("description")
    parameters = function.get("parameters")
    if (
        name != DRAFT_TOOL_NAME
        or not isinstance(description, str)
        or not description
        or not isinstance(parameters, Mapping)
    ):
        raise errors.configuration("La herramienta editorial no es la autorizada")
    return {
        "name": name,
        "description": description,
        "input_schema": dict(parameters),
    }


def extract_anthropic_draft_arguments(
    payload: Mapping[str, Any],
    errors: ProviderErrorFamily,
    *,
    allowed_block_types: frozenset[str] = ANTHROPIC_RESPONSE_BLOCK_TYPES,
) -> Mapping[str, Any]:
    label = errors.label
    if payload.get("type") != "message" or payload.get("role") != "assistant":
        raise errors.response(f"{label} no devolvió un mensaje Anthropic válido")
    stop_reason = payload.get("stop_reason")
    if stop_reason == "max_tokens":
        raise errors.response(f"{label} truncó el borrador por límite de tokens")
    if stop_reason == "refusal":
        raise errors.response(f"{label} rechazó el encargo por política y no entregó borrador")
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        raise errors.response(f"{label} no devolvió bloques de contenido Anthropic")
    if not all(isinstance(block, Mapping) for block in content):
        raise errors.response(f"{label} devolvió un bloque Anthropic inválido")
    if any(block.get("type") not in allowed_block_types for block in content):
        raise errors.response(f"{label} devolvió un tipo de bloque Anthropic no autorizado")
    tool_uses = [block for block in content if block.get("type") == "tool_use"]
    if len(tool_uses) != 1:
        raise errors.response(f"{label} debe hacer exactamente una llamada de herramienta")
    if stop_reason != "tool_use":
        raise errors.response(f"{label} no terminó con la llamada de herramienta requerida")
    tool_use = tool_uses[0]
    if tool_use.get("name") != DRAFT_TOOL_NAME:
        raise errors.response(f"{label} intentó invocar una herramienta no autorizada")
    tool_use_id = tool_use.get("id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise errors.response(f"{label} devolvió una llamada de herramienta sin id válido")
    arguments = tool_use.get("input")
    if not isinstance(arguments, Mapping):
        raise errors.response("Los argumentos Anthropic de herramienta no son un objeto JSON")
    return dict(arguments)


def anthropic_repair_messages(
    original: list[dict[str, Any]],
    response: Mapping[str, Any],
    error: Exception,
    arguments: Mapping[str, Any] | None,
    *,
    allowed_block_types: frozenset[str] = ANTHROPIC_RESPONSE_BLOCK_TYPES,
) -> list[dict[str, Any]]:
    """Conserva la cadena del modelo (incluido su thinking) al devolver el diagnóstico."""

    content = response.get("content")
    if (
        isinstance(content, list)
        and content
        and all(
            isinstance(block, Mapping) and block.get("type") in allowed_block_types
            for block in content
        )
    ):
        tool_uses = [block for block in content if block.get("type") == "tool_use"]
        if len(tool_uses) == 1:
            tool_use_id = tool_uses[0].get("id")
            if isinstance(tool_use_id, str) and tool_use_id:
                return [
                    *original,
                    {"role": "assistant", "content": [dict(block) for block in content]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": draft_feedback_json(error, arguments),
                                "is_error": True,
                            }
                        ],
                    },
                ]
    return repair_fallback_messages(original, error)


# --- forma OpenAI (chat completions) ---------------------------------------------------


def extract_openai_draft_arguments(
    payload: Mapping[str, Any],
    errors: ProviderErrorFamily,
    *,
    allow_content_fallback: bool,
) -> Mapping[str, Any]:
    label = errors.label
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise errors.response(f"{label} debe devolver exactamente una opción de borrador")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise errors.response(f"{label} devolvió una opción con formato inválido")
    if choice.get("finish_reason") == "length":
        raise errors.response(f"{label} truncó el borrador por límite de tokens")
    if choice.get("finish_reason") == "content_filter":
        raise errors.response(f"{label} filtró la salida por política y no entregó borrador")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise errors.response(f"{label} no devolvió un mensaje de asistente válido")
    if isinstance(message.get("refusal"), str) and message["refusal"]:
        raise errors.response(f"{label} rechazó el encargo por política y no entregó borrador")
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            raise errors.response(f"{label} debe hacer exactamente una llamada de herramienta")
        tool_call = tool_calls[0]
        if not isinstance(tool_call, Mapping) or tool_call.get("type") != "function":
            raise errors.response(f"{label} devolvió una llamada de herramienta inválida")
        function = tool_call.get("function")
        if not isinstance(function, Mapping) or function.get("name") != DRAFT_TOOL_NAME:
            raise errors.response(f"{label} intentó invocar una herramienta no autorizada")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise errors.response("Los argumentos de herramienta no son JSON serializado")
        return load_strict_json_object(arguments, errors)

    if not allow_content_fallback:
        raise errors.response(f"{label} no hizo la llamada de herramienta requerida")
    content = message.get("content")
    if not isinstance(content, str):
        raise errors.response(f"{label} no devolvió argumentos ni contenido JSON")
    content = _THINK_PREFIX_PATTERN.sub("", content, count=1).strip()
    fence = _JSON_FENCE_PATTERN.fullmatch(content)
    if fence:
        content = fence.group(1)
    elif not content.startswith("{"):
        raise errors.response(f"{label} no hizo la llamada de herramienta requerida")
    return load_strict_json_object(content, errors)


def openai_repair_messages(
    original: list[dict[str, Any]],
    response: Mapping[str, Any],
    error: Exception,
    arguments: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
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
                                "content": draft_feedback_json(error, arguments),
                            },
                        ]
    # Una respuesta sin id de tool call no puede formar una conversación OpenAI
    # válida. El reintento conserva el encargo original y solo agrega el
    # diagnóstico local; nunca inventa ni ejecuta un identificador de herramienta.
    return repair_fallback_messages(original, error)


# --- imágenes -----------------------------------------------------------------------------


def sniff_image_mime(data: bytes, errors: ProviderErrorFamily) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise errors.response(f"{errors.label} devolvió bytes que no son JPEG, PNG ni WebP")


def decode_image_base64(
    encoded: object,
    errors: ProviderErrorFamily,
    *,
    declared_mime: str | None = None,
) -> tuple[bytes, str]:
    """Decodifica base64 con límite local, firma binaria y MIME declarado coherente."""

    label = errors.label
    if not isinstance(encoded, str) or not encoded:
        raise errors.response(f"{label} devolvió una imagen base64 vacía o inválida")
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or not header.endswith(";base64"):
            raise errors.response(f"{label} devolvió una data URI de imagen inválida")
        declared_mime = header[5:-7]
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
        raise errors.response(f"La imagen de {label} supera el límite local de 20 MiB")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise errors.response(f"{label} devolvió base64 de imagen inválido") from exc
    if not decoded or len(decoded) > MAX_IMAGE_BYTES:
        raise errors.response(f"La imagen de {label} está vacía o supera 20 MiB")
    mime_type = sniff_image_mime(decoded, errors)
    if declared_mime is not None and declared_mime != mime_type:
        raise errors.response(f"El MIME declarado por {label} no coincide con la imagen")
    return decoded, mime_type


def credential_check_from_status(
    status_code: int | None, failure: str, *, label: str
) -> tuple[str, str]:
    """Traduce un GET de comprobación a (estado, detalle) sin filtrar nada."""

    if status_code is None:
        reason = "tiempo de espera agotado" if failure == "timeout" else "sin conexión"
        return "error", f"No fue posible comprobar la clave de {label}: {reason}."
    if 200 <= status_code < 300:
        return "valid", f"{label} aceptó la clave."
    if status_code in {401, 403}:
        return "invalid", f"{label} rechazó la clave ({status_code})."
    return "error", f"{label} respondió {status_code}; la comprobación no fue concluyente."
